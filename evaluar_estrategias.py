"""Evaluación del objetivo 4: estrategia base (0) frente a optimizada (1).

No requiere CoppeliaSim ni PLC. Usa las coordenadas reales del Excel (misma lectura
que ControlPortico._cargar_coordenadas), la asignación de celdas de PLC_PRG con
esa geometría y el mismo perfil trapezoidal de control_portico.
"""
import csv
import random
import statistics
from config import (PROJECT_DIR, POLL_INTERVAL, VEL_MAX_X, VEL_MAX_Y, VEL_MAX_Z,
                    ACEL_X, ACEL_Y, ACEL_Z)
from control_portico import (ControlPortico, ARCHIVO_COORDENADAS, NUM_FILAS,
                             Y_EXTENDIDO, Y_RETRAIDO, duracion_perfil)

X_RECOGIDA, Z_RECOGIDA = -0.262, -0.675  # main.py / ControlPortico.ir_a_recogida
T_BANDA = 4.0                 # control_banda_integrado.DURACION_TRANSPORTE
T_SONDEO = POLL_INTERVAL / 2  # retardo medio con que Python detecta una orden o el fin de un eje
T_LECTURA_QR = 0.05           # timeout de leer_qr en CoordinadorMiniASRS._qr
T_CONFIRMACION = 0.3          # PLC: mínimo en P_IDENTIFICAR, P_IR_ORIGEN y P_IR_DESTINO
T_PINZA = 0.5                 # supuesto: carrera del cilindro 2A en FluidSIM (en CoppeliaSim es inmediata)
T_ESCANEO = 0.01              # pasos del PLC sin espera (P_PREPARAR, P_REGISTRAR)
CARRERA_Y = Y_EXTENDIDO - Y_RETRAIDO
CINEMATICA = {'x': (VEL_MAX_X, ACEL_X), 'y': (VEL_MAX_Y, ACEL_Y), 'z': (VEL_MAX_Z, ACEL_Z)}
LLENADOS = (28, 56, 112)
OCUPACION_INICIAL = 0.5
SOBRES_ALEATORIO = 30
SEMILLAS = range(1, 11)
ESTRATEGIAS = {0: 'base', 1: 'optimizada'}
CSV_OPERACIONES = PROJECT_DIR / 'resultados_operaciones.csv'
CSV_RESUMEN = PROJECT_DIR / 'resultados_resumen.csv'


def t_eje(distancia, eje):
    """Perfil del eje más la detección del fin en el siguiente sondeo (0 si no hay recorrido)."""
    return duracion_perfil(distancia, *CINEMATICA[eje]) + (T_SONDEO if distancia else 0.0)


def elegir_celda(libres, estrategia, celdas):
    if estrategia == 0:
        return min(libres)
    # Igual que el FOR del PLC con '<': ante empate gana el menor índice.
    return min(libres, key=lambda i: (abs(celdas[i]['x'] - X_RECOGIDA)
                                      + abs(celdas[i]['z_entrada'] - Z_RECOGIDA), i))


def ciclo_almacenamiento(c):
    """Tiempo (s) y recorrido X+Z (mm) de un ciclo, de P_BANDA a P_REGISTRAR más el regreso."""
    x, ze, zs = c['x'], c['z_entrada'], c['z_salida']
    t_y = T_SONDEO + t_eje(CARRERA_Y, 'y')
    pasos = (
        T_SONDEO + T_BANDA,                                        # P_BANDA
        max(T_CONFIRMACION, T_SONDEO + T_LECTURA_QR),              # P_IDENTIFICAR
        T_ESCANEO,                                                 # P_PREPARAR: Y retraído y pinza abierta
        t_y,                                                       # P_Y_EXT_RECOGER
        T_SONDEO + T_PINZA,                                        # P_SUJETAR
        t_y,                                                       # P_Y_RET_RECOGER
        max(T_CONFIRMACION, T_SONDEO + t_eje(ze - Z_RECOGIDA, 'z') + t_eje(x - X_RECOGIDA, 'x')),  # P_IR_DESTINO
        t_y,                                                       # P_Y_EXT_DEPOSITAR
        T_SONDEO + t_eje(zs - ze, 'z'),                            # P_Z_AJUSTE
        T_SONDEO + T_PINZA,                                        # P_LIBERAR
        t_y,                                                       # P_Y_RET_DEPOSITAR
        T_ESCANEO,                                                 # P_REGISTRAR
        max(T_CONFIRMACION, T_SONDEO + t_eje(X_RECOGIDA - x, 'x') + t_eje(Z_RECOGIDA - zs, 'z')),  # regreso (P_IR_ORIGEN)
    )
    distancia = (abs(ze - Z_RECOGIDA) + abs(x - X_RECOGIDA) + abs(zs - ze)
                 + abs(X_RECOGIDA - x) + abs(Z_RECOGIDA - zs))
    return sum(pasos), distancia * 1000


def simular(estrategia, escenario, semilla, celdas, ocupadas, n_sobres):
    libres = set(celdas) - ocupadas
    registro = []
    for k in range(1, n_sobres + 1):
        fila = {'estrategia': ESTRATEGIAS[estrategia], 'escenario': escenario, 'semilla': semilla,
                'codigo_sobre': f'AS-{k:03d}'}
        if not libres:
            registro.append(fila | {'columna': '', 'fila': '', 'tiempo_ciclo_s': '',
                                    'distancia_mm': '', 'resultado': 'rack_lleno'})
            continue
        i = elegir_celda(libres, estrategia, celdas)
        libres.remove(i)
        t, d = ciclo_almacenamiento(celdas[i])
        registro.append(fila | {'columna': celdas[i]['columna'], 'fila': celdas[i]['fila'],
                                'tiempo_ciclo_s': round(t, 3), 'distancia_mm': round(d, 1),
                                'resultado': 'almacenado'})
    return registro


def resumir(registro, escenario, estrategia):
    ops = [r for r in registro if r['escenario'] == escenario and r['estrategia'] == estrategia
           and r['resultado'] == 'almacenado']
    tiempos = [r['tiempo_ciclo_s'] for r in ops]
    por_semilla = {}
    for r in ops:
        por_semilla.setdefault(r['semilla'], []).append(r['tiempo_ciclo_s'])
    medias = [statistics.mean(v) for v in por_semilla.values()]
    t_prom = statistics.mean(tiempos)
    return {'escenario': escenario, 'estrategia': estrategia, 'operaciones': len(ops),
            't_ciclo_prom_s': t_prom, 't_ciclo_desv_s': statistics.stdev(tiempos),
            'desv_entre_semillas_s': statistics.stdev(medias) if len(medias) > 1 else '',
            'distancia_prom_mm': statistics.mean(r['distancia_mm'] for r in ops),
            'ops_hora': 3600 / t_prom}


def main():
    coordenadas = ControlPortico._cargar_coordenadas(ARCHIVO_COORDENADAS)
    celdas = {(c['columna'] - 1) * NUM_FILAS + c['fila']: c for c in coordenadas.values()}
    registro = []
    for estrategia in ESTRATEGIAS:
        for n in LLENADOS:
            registro += simular(estrategia, f'llenado_{n}', '', celdas, set(), n)
        for s in SEMILLAS:
            ocupadas = set(random.Random(s).sample(sorted(celdas), round(OCUPACION_INICIAL * len(celdas))))
            registro += simular(estrategia, 'aleatorio_50', s, celdas, ocupadas, SOBRES_ALEATORIO)

    resumen = []
    for escenario in [f'llenado_{n}' for n in LLENADOS] + ['aleatorio_50']:
        base, opt = (resumir(registro, escenario, e) for e in ESTRATEGIAS.values())
        for r in (base, opt):
            r['reduccion_tiempo_pct'] = (base['t_ciclo_prom_s'] - r['t_ciclo_prom_s']) / base['t_ciclo_prom_s'] * 100
            r['reduccion_distancia_pct'] = (base['distancia_prom_mm'] - r['distancia_prom_mm']) / base['distancia_prom_mm'] * 100
            resumen.append(r)

    with open(CSV_OPERACIONES, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(registro[0]))
        w.writeheader()
        w.writerows(registro)
    with open(CSV_RESUMEN, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(resumen[0]))
        w.writeheader()
        w.writerows({k: round(v, 3) if isinstance(v, float) else v for k, v in r.items()} for r in resumen)

    print(f"{'escenario':<14}{'estrategia':<12}{'ops':>5}{'t_prom s':>10}{'desv s':>8}{'desv sem':>10}"
          f"{'dist mm':>9}{'ops/h':>8}{'red t %':>9}{'red d %':>9}")
    for r in resumen:
        dsem = f"{r['desv_entre_semillas_s']:.3f}" if r['desv_entre_semillas_s'] != '' else '-'
        print(f"{r['escenario']:<14}{r['estrategia']:<12}{r['operaciones']:>5}{r['t_ciclo_prom_s']:>10.2f}"
              f"{r['t_ciclo_desv_s']:>8.2f}{dsem:>10}{r['distancia_prom_mm']:>9.0f}{r['ops_hora']:>8.1f}"
              f"{r['reduccion_tiempo_pct']:>9.1f}{r['reduccion_distancia_pct']:>9.1f}")
    print('CSV:', CSV_OPERACIONES, CSV_RESUMEN)


if __name__ == '__main__':
    main()
