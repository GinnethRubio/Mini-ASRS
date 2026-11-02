"""Ejecución completa del experimento del Mini AS/RS con un solo comando.

python experimento.py                    fases sin conexión: A, E, B
python experimento.py --conectado        además la fase D con CODESYS, FluidSIM y CoppeliaSim

Fases
  A  pruebas del selector, del banco y del supervisor (unitarias)
  E  fallos y recuperación del controlador con dobles de prueba
  B  modelo rápido R vs C: piloto, tamaño de muestra con delta_min_s, campaña final y análisis pareado
  D  campaña conectada de 112 almacenamientos y 112 recuperaciones, por bloques del runtime, con
     reanudación automática tras desconexión de CODESYS

El protocolo (delta, semillas, réplicas, ocupaciones) se lee de experimento.json y su hash queda en
cada corrida. Todo se registra en datos_experimentos/; el resumen final queda en
datos_experimentos/ejecuciones/<fecha>/resumen.json.
"""
import argparse
import hashlib
import json
import subprocess
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
for carpeta in ('coordinador', 'experimentos', 'banco_pruebas_112'):
    sys.path.insert(0, str(RAIZ / carpeta))
import rutas  # noqa: E402,F401  (rutas de importación del coordinador)
from registro_experimental import Corrida  # noqa: E402

SUITES_A = [('coordinador/estrategias', 'test_estrategias_colocacion'),
            ('banco_pruebas_112', 'test_banco'),
            ('banco_pruebas_112', 'test_sesiones_runtime'),
            ('banco_pruebas_112', 'test_supervisor')]
SUITES_E = [('banco_pruebas_112', 'pruebas_fallas')]


class Ejecucion:
    def __init__(self, args, protocolo, texto_protocolo):
        self.args, self.p = args, protocolo
        self.id = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.datos = args.datos
        self.carpeta = self.datos / 'ejecuciones' / self.id
        self.carpeta.mkdir(parents=True)
        (self.carpeta / 'experimento.json').write_text(texto_protocolo, encoding='utf-8')
        self.hash_protocolo = hashlib.sha256(texto_protocolo.encode('utf-8')).hexdigest()
        self.resumen = dict(ejecucion=self.id, protocolo_sha256=self.hash_protocolo, fases={})

    def guardar(self):
        (self.carpeta / 'resumen.json').write_text(json.dumps(self.resumen, ensure_ascii=False, indent=2), encoding='utf-8')

    def fase(self, nombre, funcion):
        print(f'\n=== Fase {nombre} ===', flush=True)
        t0 = time.monotonic()
        try:
            resultado = funcion()
            estado = 'completa' if resultado.pop('ok', True) else 'con_fallos'
        except Exception as exc:
            resultado, estado = dict(error=repr(exc)), 'error'
        resultado.update(estado=estado, duracion_s=round(time.monotonic() - t0, 1))
        self.resumen['fases'][nombre] = resultado
        self.guardar()
        print(f'Fase {nombre}: {estado} ({resultado["duracion_s"]} s)', flush=True)
        return estado

    def suites(self, nivel, suites):
        run_id = f'{self.id}_{nivel}_pruebas'
        with Corrida(self.datos, nivel, run_id, escenario=dict(suites=[s for _, s in suites]),
                     configuracion=dict(protocolo_sha256=self.hash_protocolo),
                     alcance='Pruebas automatizadas con dobles de prueba; sin PLC, FluidSIM ni CoppeliaSim.') as c:
            c.tabla('casos.csv', ['suite', 'prueba', 'resultado', 'detalle'])
            total = fallidas = 0
            for carpeta, modulo in suites:
                sys.path.insert(0, str(RAIZ / carpeta))
                suite = unittest.defaultTestLoader.loadTestsFromName(modulo)
                pruebas = list(iterar(suite))
                r = unittest.TextTestRunner(stream=open(self.carpeta / f'{modulo}.txt', 'w', encoding='utf-8'),
                                            verbosity=2).run(suite)
                malas = {str(t): d for t, d in r.failures + r.errors}
                for t in pruebas:
                    total += 1
                    detalle = malas.get(str(t))
                    fallidas += detalle is not None
                    c.fila('casos.csv', suite=modulo, prueba=t.id().split('.')[-1],
                           resultado='fallida' if detalle else 'correcta', detalle=(detalle or '')[-500:])
                    if detalle:
                        c.fallo(tipo='prueba_fallida', detalle=t.id(), consecuencia='revisar antes de la campaña')
            c.cerrar('completa' if not fallidas else 'con_fallos', dict(pruebas=total, fallidas=fallidas))
        return dict(ok=not fallidas, corrida=run_id, pruebas=total, fallidas=fallidas)

    def campana_modelo(self, replicas, semilla):
        cmd = [sys.executable, str(RAIZ / 'experimentos' / 'campana_rc.py'), '--replicas', str(replicas),
               '--semilla', str(semilla), '--ocupaciones', ','.join(map(str, self.p['ocupaciones'])),
               '--costo-c', self.p['costo_c'], '--calentamiento', str(self.p['calentamiento_factor_k']),
               '--salida', str(self.datos)]
        salida = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', check=True).stdout
        return salida.split('Campaña:', 1)[1].split('|', 1)[0].strip()

    def analizar(self, *args):
        cmd = [sys.executable, str(RAIZ / 'experimentos' / 'analizar_experimentos.py'), '--raiz', str(self.datos), *args]
        salida = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', check=True).stdout
        print(salida, flush=True)
        return salida.strip().splitlines()[-1].split('Análisis:', 1)[-1].strip()

    def fase_b(self):
        piloto = self.campana_modelo(self.p['replicas_piloto'], self.p['semilla_piloto'])
        carpeta_potencia = Path(self.analizar('potencia', '--campana', piloto, '--delta', str(self.p['delta_min_s']),
                                              '--alfa', str(self.p['alfa']), '--potencia', str(self.p['potencia'])))
        import csv
        with (carpeta_potencia / 'tamano_muestra.csv').open(encoding='utf-8') as f:
            requeridas = max([int(r['parejas_requeridas']) for r in csv.DictReader(f)] or [0])
        replicas = min(self.p['replicas_maximas'], max(self.p['replicas_minimas'], requeridas))
        final = self.campana_modelo(replicas, self.p['semilla_final'])
        carpeta_pareado = self.analizar('pareado', '--campana', final)
        return dict(piloto=piloto, parejas_requeridas=requeridas, replicas_final=replicas,
                    campana_final=final, analisis=carpeta_pareado, potencia=str(carpeta_potencia))

    def fase_d(self):
        c = self.p['conectado']
        cmd = [sys.executable, str(RAIZ / 'banco_pruebas_112' / 'supervisor_campana.py'), '--confirmar-movimientos',
               '--estrategia', str(c['estrategia']), '--limite-runtime-min', str(c['limite_runtime_min']),
               '--margen-runtime-min', str(c['margen_runtime_min']), '--bloque-min', str(c['bloque_min']),
               '--espera-max-min', str(c['espera_max_min']), '--licencia', c['licencia'], '--datos', str(self.datos)]
        if self.args.reanudar:
            cmd += ['--reanudar', str(self.args.reanudar)]
        codigo = subprocess.run(cmd, cwd=RAIZ).returncode
        return dict(ok=codigo == 0, codigo=codigo)


def iterar(suite):
    for t in suite:
        if isinstance(t, unittest.TestSuite):
            yield from iterar(t)
        else:
            yield t


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--protocolo', type=Path, default=RAIZ / 'experimento.json')
    ap.add_argument('--fases', default=None, help='Lista separada por comas; por defecto A,E,B y D si --conectado')
    ap.add_argument('--conectado', action='store_true', help='Incluye la fase D con el sistema integrado')
    ap.add_argument('--reanudar', type=Path, help='Punto de continuación para retomar la fase D')
    ap.add_argument('--datos', type=Path, default=RAIZ / 'datos_experimentos')
    args = ap.parse_args()
    texto = args.protocolo.read_text(encoding='utf-8')
    protocolo = json.loads(texto)
    fases = (args.fases.split(',') if args.fases else ['A', 'E', 'B'] + (['D'] if args.conectado else []))
    e = Ejecucion(args, protocolo, texto)
    print('Ejecución:', e.carpeta, flush=True)
    funciones = dict(A=lambda: e.suites('A', SUITES_A), E=lambda: e.suites('E', SUITES_E), B=e.fase_b, D=e.fase_d)
    for f in fases:
        estado = e.fase(f, funciones[f])
        if f == 'A' and estado != 'completa':
            print('Las pruebas de la fase A fallaron; no se ejecutan campañas.', flush=True)
            break
    print('\nResumen:', e.carpeta / 'resumen.json')
    return 0 if all(v['estado'] == 'completa' for v in e.resumen['fases'].values()) else 1


if __name__ == '__main__':
    sys.exit(main())
