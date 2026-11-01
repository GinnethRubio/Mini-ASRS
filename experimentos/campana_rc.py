"""Campaña pareada de asignación de ubicaciones sobre el modelo rápido de tiempos.

Políticas:
  R  asignación aleatoria uniforme entre las celdas libres.
  C  mínimo costo de ubicación: C_tiempo (tiempo previsto del ciclo de almacenamiento)
     o C_dist (distancia X+Z a la estación de recogida, como la estrategia 1 del PLC).

Cada corrida parte de un inventario inicial con K celdas ocupadas y ejecuta parejas
recuperar + almacenar, de modo que la ocupación oscila entre K y K-1. Ambas políticas
reciben el mismo inventario inicial y la misma secuencia de solicitudes por
identificador; la aleatoriedad de R usa un flujo propio. Unidad experimental: la corrida.

python experimentos/campana_rc.py --replicas 10
"""
import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

PROYECTO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROYECTO))

from control_portico import ControlPortico, ARCHIVO_COORDENADAS, NUM_FILAS  # noqa: E402
from evaluar_estrategias import (ciclo_almacenamiento, t_eje, T_SONDEO, T_CONFIRMACION,  # noqa: E402
                                 T_PINZA, T_ESCANEO, T_BANDA, T_LECTURA_QR, CARRERA_Y,
                                 X_RECOGIDA, Z_RECOGIDA)
from registro_experimental import Corrida  # noqa: E402

# Estación de despacho (recuperacion.py): segunda banda 0,525 m a la derecha de la recogida.
X_DESPACHO = X_RECOGIDA + 0.525
Z_DESPACHO = Z_RECOGIDA
HOLGURA_DESPACHO = 0.020
OCUPACIONES = (28, 56, 84)
PAREJAS_MEDIDAS = 200
POLITICAS = ('R', 'C')


def cargar_celdas():
    coords = ControlPortico._cargar_coordenadas(ARCHIVO_COORDENADAS)
    return {(c - 1) * NUM_FILAS + f: dict(v, columna=c, fila=f) for (c, f), v in coords.items()}


def ciclo_recuperacion(c):
    """Tiempo (s) y recorrido X+Z (mm): de la celda de origen a la segunda banda y regreso a recogida."""
    x, ze, zs = c['x'], c['z_entrada'], c['z_salida']
    zd = Z_DESPACHO + HOLGURA_DESPACHO
    t_y = T_SONDEO + t_eje(CARRERA_Y, 'y')
    pasos = (
        T_ESCANEO,                                                                  # preparar
        max(T_CONFIRMACION, T_SONDEO + t_eje(zs - Z_RECOGIDA, 'z') + t_eje(x - X_RECOGIDA, 'x')),  # ir al origen
        t_y,                                                                        # Y extender
        T_SONDEO + T_PINZA,                                                         # sujetar
        T_SONDEO + t_eje(ze - zs, 'z') + t_eje(CARRERA_Y, 'y'),                     # elevar y extraer
        max(T_CONFIRMACION, T_SONDEO + t_eje(X_DESPACHO - x, 'x') + t_eje(zd - ze, 'z')),  # ir a despacho
        t_y,                                                                        # Y extender
        T_SONDEO + t_eje(HOLGURA_DESPACHO, 'z'),                                    # apoyar en banda
        T_SONDEO + T_PINZA,                                                         # soltar
        t_y,                                                                        # Y retraer
        T_ESCANEO,                                                                  # registrar
        max(T_CONFIRMACION, T_SONDEO + t_eje(X_RECOGIDA - X_DESPACHO, 'x') + t_eje(Z_RECOGIDA - Z_DESPACHO, 'z')),  # regreso
    )
    distancia = (abs(zs - Z_RECOGIDA) + abs(x - X_RECOGIDA) + abs(ze - zs) + abs(X_DESPACHO - x)
                 + abs(zd - ze) + HOLGURA_DESPACHO + abs(X_RECOGIDA - X_DESPACHO) + abs(Z_RECOGIDA - Z_DESPACHO))
    return sum(pasos), distancia * 1000


def costos_c(celdas, variante):
    if variante == 'tiempo':
        return {i: ciclo_almacenamiento(c)[0] for i, c in celdas.items()}
    return {i: abs(c['x'] - X_RECOGIDA) + abs(c['z_entrada'] - Z_RECOGIDA) for i, c in celdas.items()}


def elegir(politica, libres, costo, rng_r):
    if politica == 'R':
        ordenadas = sorted(libres)
        return ordenadas[int(rng_r.integers(len(ordenadas)))]
    # Desempate por índice de celda, igual que el PLC.
    return min(libres, key=lambda i: (round(costo[i], 9), i))


def generar_entradas(k, semillas, calentamiento):
    """Inventario inicial y fracciones de demanda comunes a ambas políticas."""
    inv, dem = semillas[:2]
    celdas_iniciales = sorted(int(i) for i in inv.choice(np.arange(1, 113), size=k, replace=False))
    fracciones = dem.random(calentamiento + PAREJAS_MEDIDAS)
    return celdas_iniciales, fracciones


def recorridos(c, operacion):
    """Recorrido X y Z (mm) de un ciclo; mismas trayectorias que ciclo_almacenamiento / ciclo_recuperacion."""
    x, ze, zs = c['x'], c['z_entrada'], c['z_salida']
    if operacion == 'STORE':
        rx = 2 * abs(x - X_RECOGIDA)
        rz = abs(ze - Z_RECOGIDA) + abs(zs - ze) + abs(Z_RECOGIDA - zs)
    else:
        zd = Z_DESPACHO + HOLGURA_DESPACHO
        rx = abs(x - X_RECOGIDA) + abs(X_DESPACHO - x) + abs(X_RECOGIDA - X_DESPACHO)
        rz = (abs(zs - Z_RECOGIDA) + abs(ze - zs) + abs(zd - ze) + HOLGURA_DESPACHO
              + abs(Z_RECOGIDA - Z_DESPACHO))
    return rx * 1000, rz * 1000


def correr(corrida, politica, k, semillas, celdas, costo, calentamiento):
    celdas_iniciales, fracciones = generar_entradas(k, [np.random.default_rng(s) for s in semillas], calentamiento)
    rng_r = np.random.default_rng(semillas[2])
    ubicacion = {f'AS-{n:04d}': i for n, i in enumerate(celdas_iniciales, 1)}
    siguiente = k + 1
    reloj_sim = 0.0
    medidas = []

    def registrar(p, fase, operacion, qr, i, computo):
        nonlocal reloj_sim
        if operacion == 'STORE':
            t, d = ciclo_almacenamiento(celdas[i])
        else:
            t, d = ciclo_recuperacion(celdas[i])
        rx, rz = recorridos(celdas[i], operacion)
        corrida.operacion(fase=fase, pareja=p, qr=qr, operacion=operacion,
                          columna=celdas[i]['columna'], fila=celdas[i]['fila'],
                          inicio_sim_s=round(reloj_sim, 4), fin_sim_s=round(reloj_sim + t, 4),
                          tiempo_s=round(t, 4), recorrido_x_mm=round(rx, 1), recorrido_z_mm=round(rz, 1),
                          distancia_mm=round(d, 1), computo_seleccion_us=computo)
        reloj_sim += t
        if fase == 'medida':
            medidas.append((operacion, t, d))

    for p, u in enumerate(fracciones):
        fase = 'calentamiento' if p < calentamiento else 'medida'
        # La solicitud se define por identificador, igual para ambas políticas.
        existentes = sorted(ubicacion)
        qr = existentes[int(u * len(existentes))]
        registrar(p, fase, 'RETRIEVE', qr, ubicacion.pop(qr), None)
        libres = set(celdas) - set(ubicacion.values())
        tic = time.perf_counter_ns()
        destino = elegir(politica, libres, costo, rng_r)
        computo = round((time.perf_counter_ns() - tic) / 1000, 2)
        nuevo = f'AS-{siguiente:04d}'
        siguiente += 1
        ubicacion[nuevo] = destino
        registrar(p, fase, 'STORE', nuevo, destino, computo)

    media = lambda xs: sum(xs) / len(xs)
    tiempos = [t for _, t, _ in medidas]
    return dict(operaciones_medidas=len(medidas), tiempo_medio_s=media(tiempos),
                tiempo_medio_almacenar_s=media([t for o, t, _ in medidas if o == 'STORE']),
                tiempo_medio_recuperar_s=media([t for o, t, _ in medidas if o == 'RETRIEVE']),
                tiempo_medio_pareja_s=2 * media(tiempos),
                distancia_media_mm=media([d for _, _, d in medidas]),
                operaciones_hora=3600 / media(tiempos), fallos=0)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--replicas', type=int, default=10)
    ap.add_argument('--semilla', type=int, default=20261101)
    ap.add_argument('--ocupaciones', default=','.join(map(str, OCUPACIONES)))
    ap.add_argument('--costo-c', choices=['tiempo', 'dist'], default='tiempo')
    ap.add_argument('--calentamiento', type=int, default=3, help='Parejas de calentamiento = este factor por K')
    ap.add_argument('--salida', type=Path, default=PROYECTO / 'datos_experimentos')
    args = ap.parse_args()
    ocupaciones = [int(k) for k in args.ocupaciones.split(',')]
    if args.replicas < 2 or any(not 1 <= k <= 111 for k in ocupaciones) or args.calentamiento < 0:
        ap.error('Se requieren al menos 2 réplicas, ocupaciones entre 1 y 111 y calentamiento no negativo')
    celdas = cargar_celdas()
    if len(celdas) != 112:
        raise RuntimeError('El Excel no contiene las 112 celdas esperadas')
    costo = costos_c(celdas, args.costo_c)
    campana_id = datetime.now().strftime('%Y%m%d_%H%M%S') + '_rc'
    escenario = dict(
        campana_id=campana_id, politicas={'R': 'aleatoria uniforme entre celdas libres',
            'C': f'mínimo costo de ubicación, C_{args.costo_c}; desempate por índice de celda'},
        flujos='SeedSequence por ocupación y réplica: inventario inicial, demanda, selección de R',
        semilla_base=args.semilla, parejas_calentamiento=f'{args.calentamiento}K', parejas_medidas=PAREJAS_MEDIDAS,
        tiempos_fijos_s=dict(sondeo=T_SONDEO, confirmacion=T_CONFIRMACION, pinza=T_PINZA, escaneo=T_ESCANEO,
                             banda=T_BANDA, lectura_qr=T_LECTURA_QR),
        estaciones_m=dict(recogida=[X_RECOGIDA, Z_RECOGIDA], despacho=[X_DESPACHO, Z_DESPACHO],
                          holgura_despacho=HOLGURA_DESPACHO),
        ejes='X y Z secuenciales; cada operación inicia y termina en la estación de recogida')
    raiz = np.random.SeedSequence(args.semilla)
    for k, sk in zip(ocupaciones, raiz.spawn(len(ocupaciones))):
        for replica, sr in enumerate(sk.spawn(args.replicas), 1):
            semillas = sr.spawn(3)
            inventario, _ = generar_entradas(k, [np.random.default_rng(s) for s in semillas], 0)
            for politica in POLITICAS:
                run_id = f'{campana_id}_K{k:03d}_r{replica:02d}_{politica}'
                with Corrida(args.salida, 'B', run_id, politica=politica, ocupacion=k, replica=replica,
                             semillas=dict(base=args.semilla, ocupacion=list(sk.spawn_key),
                                           replica=[list(s.spawn_key) for s in semillas]),
                             inventario_inicial={f'AS-{n:04d}': i for n, i in enumerate(inventario, 1)},
                             escenario=escenario, licencia='No aplica: modelo rápido en Python',
                             alcance='Modelo rápido de tiempos; no incluye latencias de OPC UA, FluidSIM ni CoppeliaSim.') as c:
                    resumen = correr(c, politica, k, semillas, celdas, costo, args.calentamiento * k)
                    c.cerrar('completa', resumen)
    print('Campaña:', campana_id, '| datos en', args.salida.resolve())
    return campana_id


if __name__ == '__main__':
    main()
