"""Análisis reproducible de las corridas registradas.

python experimentos/analizar_experimentos.py pareado  --campana 20261101_200035_rc
python experimentos/analizar_experimentos.py potencia --campana ID_PILOTO --delta 1.0
python experimentos/analizar_experimentos.py contraste --corridas RUN_ID_INTEGRADO [RUN_ID ...]
python experimentos/analizar_experimentos.py verificar

Los resultados se escriben en datos_experimentos/analisis/<fecha>_<tipo>/ junto con un
manifiesto que lista el SHA-256 de cada archivo de entrada. Las tablas y gráficos se
regeneran desde los CSV originales; no se editan a mano.
"""
import argparse
import csv
import json
import math
import statistics
import sys
from datetime import datetime
from pathlib import Path

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI))
from registro_experimental import PROYECTO, sha256, verificar, ahora_utc  # noqa: E402

RAIZ = PROYECTO / 'datos_experimentos'


def leer_csv(path):
    with Path(path).open(encoding='utf-8') as f:
        return list(csv.DictReader(f))


def escribir_csv(path, filas):
    if not filas:
        return
    with Path(path).open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(filas[0]))
        w.writeheader()
        w.writerows({k: round(v, 4) if isinstance(v, float) else v for k, v in r.items()} for r in filas)


def corridas(raiz, filtro):
    indice = leer_csv(raiz / 'registro_corridas.csv')
    return [r for r in indice if filtro(r)]


def salida(raiz, tipo, entradas, parametros):
    destino = raiz / 'analisis' / (datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + tipo)
    destino.mkdir(parents=True)
    (destino / 'manifiesto_analisis.json').write_text(json.dumps(dict(
        tipo=tipo, fecha_utc=ahora_utc(), parametros=parametros,
        entradas={str(p.relative_to(raiz)).replace('\\', '/'): sha256(p) for p in entradas}),
        ensure_ascii=False, indent=2), encoding='utf-8')
    return destino


def resumen_corrida(raiz, r):
    ops = leer_csv(raiz / r['ruta'] / 'operaciones.csv')
    medidas = [o for o in ops if o['fase'] == 'medida']
    exitosas = [o for o in medidas if o['resultado'] == 'correcto']
    t = [float(o['tiempo_s']) for o in exitosas]
    por_op = lambda op: [float(o['tiempo_s']) for o in exitosas if o['operacion'] == op]
    media = lambda xs: statistics.mean(xs) if xs else math.nan
    return dict(run_id=r['run_id'], politica=r['politica'], ocupacion=int(r['ocupacion']),
                replica=int(r['replica']), operaciones_medidas=len(medidas), exitosas=len(exitosas),
                fallos=len(medidas) - len(exitosas), tiempo_medio_s=media(t),
                tiempo_medio_store_s=media(por_op('STORE')), tiempo_medio_retrieve_s=media(por_op('RETRIEVE')),
                distancia_media_mm=media([float(o['distancia_mm']) for o in exitosas if o['distancia_mm']]),
                operaciones_hora=3600 / media(t) if t else math.nan), raiz / r['ruta'] / 'operaciones.csv'


def pareado(args):
    from scipy.stats import ttest_rel
    raiz = args.raiz
    sel = corridas(raiz, lambda r: r['run_id'].startswith(args.campana))
    if not sel:
        raise SystemExit('No hay corridas de la campaña ' + args.campana)
    resumenes, entradas, excluidas = [], [], []
    for r in sel:
        if r['estado'] != 'completa':
            excluidas.append(dict(run_id=r['run_id'], motivo='estado ' + r['estado']))
            continue
        res, p = resumen_corrida(raiz, r)
        resumenes.append(res)
        entradas.append(p)
    pares, tabla, diferencias = {}, [], []
    for res in resumenes:
        pares.setdefault((res['ocupacion'], res['replica']), {})[res['politica']] = res
    for clave, par in sorted(pares.items()):
        if set(par) != {'R', 'C'}:
            excluidas.append(dict(run_id=';'.join(p['run_id'] for p in par.values()), motivo='pareja incompleta'))
    completos = {k: v for k, v in pares.items() if set(v) == {'R', 'C'}}
    for k in sorted({o for o, _ in completos}):
        filas = [completos[c] for c in sorted(completos) if c[0] == k]
        tr = [f['R']['tiempo_medio_s'] for f in filas]
        tc = [f['C']['tiempo_medio_s'] for f in filas]
        d = [a - b for a, b in zip(tr, tc)]
        for f, di in zip(filas, d):
            diferencias.append(dict(ocupacion=k, replica=f['R']['replica'], tiempo_R_s=f['R']['tiempo_medio_s'],
                                    tiempo_C_s=f['C']['tiempo_medio_s'], diferencia_s=di,
                                    fallos_R=f['R']['fallos'], fallos_C=f['C']['fallos']))
        fila = dict(ocupacion=k, n_parejas=len(d),
                    operaciones_medidas=sum(f['R']['operaciones_medidas'] + f['C']['operaciones_medidas'] for f in filas),
                    media_R_s=statistics.mean(tr), desv_R_s=statistics.stdev(tr) if len(tr) > 1 else math.nan,
                    media_C_s=statistics.mean(tc), desv_C_s=statistics.stdev(tc) if len(tc) > 1 else math.nan,
                    diferencia_media_s=statistics.mean(d), desv_diferencia_s=statistics.stdev(d) if len(d) > 1 else math.nan,
                    ic95_inferior_s=math.nan, ic95_superior_s=math.nan, p_bilateral=math.nan,
                    diferencia_relativa_pct=100 * statistics.mean(d) / statistics.mean(tr),
                    fallos_R=sum(f['R']['fallos'] for f in filas), fallos_C=sum(f['C']['fallos'] for f in filas))
        if len(d) > 1 and statistics.stdev(d) > 0:
            prueba = ttest_rel(tr, tc, alternative='two-sided', nan_policy='raise')
            ic = prueba.confidence_interval(0.95)
            fila.update(ic95_inferior_s=ic.low, ic95_superior_s=ic.high, p_bilateral=prueba.pvalue)
        tabla.append(fila)
    destino = salida(raiz, 'pareado', entradas, vars(args) | {'raiz': str(raiz)})
    escribir_csv(destino / 'resumen_por_corrida.csv', resumenes)
    escribir_csv(destino / 'diferencias_pareadas.csv', diferencias)
    escribir_csv(destino / 'tabla_resultados.csv', tabla)
    escribir_csv(destino / 'excluidas.csv', excluidas)
    grafico_pareado(destino / 'diferencias_pareadas.png', diferencias, tabla)
    for f in tabla:
        print(f"K={f['ocupacion']:>3}  n={f['n_parejas']:>2}  d={f['diferencia_media_s']:.3f} s  "
              f"IC95=[{f['ic95_inferior_s']:.3f}, {f['ic95_superior_s']:.3f}]  "
              f"{f['diferencia_relativa_pct']:.1f} %  fallos R/C={f['fallos_R']}/{f['fallos_C']}")
    if excluidas:
        print('Corridas excluidas del pareo (ver excluidas.csv):', len(excluidas))
    print('Análisis:', destino)


def grafico_pareado(path, diferencias, tabla):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    ks = [f['ocupacion'] for f in tabla]
    fig, ax = plt.subplots(figsize=(7, 4))
    for i, k in enumerate(ks):
        ys = [d['diferencia_s'] for d in diferencias if d['ocupacion'] == k]
        ax.scatter([i] * len(ys), ys, s=14, alpha=.6, color='#4C72B0')
        f = tabla[i]
        if not math.isnan(f['ic95_inferior_s']):
            ax.errorbar(i + .15, f['diferencia_media_s'], color='black', capsize=4, fmt='o',
                        yerr=[[f['diferencia_media_s'] - f['ic95_inferior_s']],
                              [f['ic95_superior_s'] - f['diferencia_media_s']]])
    ax.axhline(0, color='grey', lw=.8)
    ax.set_xticks(range(len(ks)), [f'K = {k}' for k in ks])
    ax.set_ylabel('d = T_R − T_C por réplica (s)')
    ax.set_title('Diferencias pareadas por réplica e IC 95 %')
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def parejas_necesarias(efecto, alfa=.05, potencia=.8):
    """n de parejas para prueba t pareada bilateral, con efecto estandarizado delta/s_d."""
    try:
        from statsmodels.stats.power import TTestPower
        return math.ceil(TTestPower().solve_power(effect_size=efecto, alpha=alfa, power=potencia,
                                                  alternative='two-sided'))
    except ImportError:
        from scipy.stats import t, nct
        for n in range(2, 100000):
            crit = t.ppf(1 - alfa / 2, n - 1)
            nc = efecto * math.sqrt(n)
            if 1 - nct.cdf(crit, n - 1, nc) + nct.cdf(-crit, n - 1, nc) >= potencia:
                return n
        raise ValueError('Efecto demasiado pequeño')


def potencia(args):
    raiz = args.raiz
    sel = [r for r in corridas(raiz, lambda r: r['run_id'].startswith(args.campana)) if r['estado'] == 'completa']
    res = [resumen_corrida(raiz, r) for r in sel]
    pares = {}
    for r, _ in res:
        pares.setdefault((r['ocupacion'], r['replica']), {})[r['politica']] = r['tiempo_medio_s']
    filas = []
    for k in sorted({o for o, _ in pares}):
        d = [v['R'] - v['C'] for (o, _), v in sorted(pares.items()) if o == k and set(v) == {'R', 'C'}]
        if len(d) < 2:
            continue
        sd = statistics.stdev(d)
        n = parejas_necesarias(args.delta / sd, args.alfa, args.potencia) if sd > 0 else 2
        filas.append(dict(ocupacion=k, parejas_piloto=len(d), sd_diferencias_s=sd, delta_min_s=args.delta,
                          alfa=args.alfa, potencia=args.potencia, parejas_requeridas=n))
        print(f'K={k}: piloto {len(d)} parejas, s_d={sd:.3f} s -> {n} parejas para delta={args.delta} s')
    destino = salida(raiz, 'potencia', [raiz / r['ruta'] / 'operaciones.csv' for r in sel], vars(args) | {'raiz': str(raiz)})
    escribir_csv(destino / 'tamano_muestra.csv', filas)
    print('Análisis:', destino)


def contraste(args):
    sys.path.insert(0, str(AQUI))
    from campana_rc import cargar_celdas, ciclo_recuperacion
    from evaluar_estrategias import ciclo_almacenamiento
    raiz = args.raiz
    celdas = {(c['columna'], c['fila']): c for c in cargar_celdas().values()}
    if args.campana:
        sel = corridas(raiz, lambda r: r['run_id'].startswith(args.campana) and r['estado'] == 'completa')
    else:
        sel = corridas(raiz, lambda r: r['run_id'] in args.corridas)
        if len(sel) != len(args.corridas):
            raise SystemExit('Corridas no encontradas en el índice')
    if not sel:
        raise SystemExit('Sin corridas para contrastar')
    filas, entradas = [], []
    for r in sel:
        p = raiz / r['ruta'] / 'operaciones.csv'
        entradas.append(p)
        for o in leer_csv(p):
            if o['resultado'] != 'correcto' or not o['tiempo_s']:
                continue
            c = celdas[(int(o['columna']), int(o['fila']))]
            t_mod = (ciclo_almacenamiento if o['operacion'] == 'STORE' else ciclo_recuperacion)(c)[0]
            t_int = float(o['tiempo_s'])
            filas.append(dict(run_id=r['run_id'], operacion=o['operacion'], columna=o['columna'], fila=o['fila'],
                              tiempo_modelo_s=t_mod, tiempo_integrado_s=t_int, error_s=t_mod - t_int,
                              tolerancia_s=max(args.tol_abs, args.tol_rel * t_int)))
    if not filas:
        raise SystemExit('Sin operaciones correctas para contrastar')
    for f in filas:
        f['dentro_tolerancia'] = abs(f['error_s']) <= f['tolerancia_s']
    err = [f['error_s'] for f in filas]
    absolutos = sorted(abs(e) for e in err)
    resumen = dict(operaciones=len(filas), rutas=len({(f['operacion'], f['columna'], f['fila']) for f in filas}),
                   mae_s=statistics.mean(absolutos), sesgo_s=statistics.mean(err), max_abs_s=absolutos[-1],
                   p95_abs_s=absolutos[min(len(absolutos) - 1, math.ceil(.95 * len(absolutos)) - 1)],
                   fraccion_dentro_tolerancia=sum(f['dentro_tolerancia'] for f in filas) / len(filas),
                   tolerancia=f'max({args.tol_abs} s, {args.tol_rel:.0%} del tiempo integrado), fijada antes del contraste')
    destino = salida(raiz, 'contraste', entradas, vars(args) | {'raiz': str(raiz)})
    escribir_csv(destino / 'contraste_por_operacion.csv', filas)
    (destino / 'resumen_contraste.json').write_text(json.dumps(resumen, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(resumen, ensure_ascii=False, indent=2))
    print('Análisis:', destino)


def verificar_todo(args):
    malas = 0
    for r in leer_csv(args.raiz / 'registro_corridas.csv'):
        estado = verificar(args.raiz / r['ruta'])
        if not all(estado.values()):
            malas += 1
            print('MODIFICADA:', r['run_id'], [n for n, ok in estado.items() if not ok])
    print('Corridas modificadas tras el sellado:', malas)
    return 1 if malas else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--raiz', type=Path, default=RAIZ)
    sub = ap.add_subparsers(dest='tipo', required=True)
    p = sub.add_parser('pareado')
    p.add_argument('--campana', required=True, help='Prefijo de run_id de la campaña')
    p = sub.add_parser('potencia')
    p.add_argument('--campana', required=True)
    p.add_argument('--delta', type=float, required=True, help='Diferencia mínima relevante (s), fijada antes de la campaña')
    p.add_argument('--alfa', type=float, default=.05)
    p.add_argument('--potencia', type=float, default=.8)
    p = sub.add_parser('contraste')
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--corridas', nargs='+', help='run_id de corridas integradas')
    g.add_argument('--campana', help='Prefijo de una campaña integrada; usa sus corridas completas')
    p.add_argument('--tol-abs', type=float, default=.5)
    p.add_argument('--tol-rel', type=float, default=.05)
    sub.add_parser('verificar')
    args = ap.parse_args()
    return dict(pareado=pareado, potencia=potencia, contraste=contraste, verificar=verificar_todo)[args.tipo](args)


if __name__ == '__main__':
    sys.exit(main() or 0)
