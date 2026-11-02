"""Tres políticas de asignación; sin conexiones ni escritura de inventario.

La estrategia 2 minimiza el costo X/Z de atender una celda y volver a entrada,
manteniendo el orden de ejes y los límites del controlador. No estima energía.
"""
import math
from config import VEL_MAX_X, VEL_MAX_Z, ACEL_X, ACEL_Z
from control_portico import duracion_perfil

ESTRATEGIAS = {0: 'secuencial', 1: 'menor_distancia', 2: 'menor_tiempo'}
# El PLC calcula en REAL (32 bits): costos que difieren menos que esto se tratan como empate.
TOLERANCIA_EMPATE_S = 1e-3
X_RECOGIDA, Z_RECOGIDA = -.262, -.675


def costo_tiempo(celda, vx=VEL_MAX_X, vz=VEL_MAX_Z, ax=ACEL_X, az=ACEL_Z):
    """Segundos de perfiles X/Z, sin neumática, sondeo ni esperas de comunicación."""
    if not all(math.isfinite(v) and v > 0 for v in (vx, vz, ax, az)):
        raise ValueError('Velocidades y aceleraciones deben ser positivas y finitas')
    x, ze, zs = (float(celda[k]) for k in ('x', 'z_entrada', 'z_salida'))
    if not all(math.isfinite(v) for v in (x, ze, zs)):
        raise ValueError('Coordenadas no finitas')
    return (2 * duracion_perfil(x-X_RECOGIDA, vx, ax)
            + duracion_perfil(ze-Z_RECOGIDA, vz, az)
            + duracion_perfil(zs-ze, vz, az)
            + duracion_perfil(zs-Z_RECOGIDA, vz, az))


def elegir_celda(libres, estrategia, celdas, **cinematica):
    """Devuelve índice libre o None si el rack está lleno; desempata por índice."""
    if estrategia not in ESTRATEGIAS:
        raise ValueError('Estrategia desconocida: '+str(estrategia))
    mejor, costo_mejor = None, math.inf
    for indice in sorted(libres):
        c = celdas[indice]
        if estrategia == 0:
            costo = float(indice)
        elif estrategia == 1:
            costo = abs(c['x']-X_RECOGIDA) + abs(c['z_entrada']-Z_RECOGIDA)
        else:
            costo = costo_tiempo(c, **cinematica)
        if not math.isfinite(costo):
            raise ValueError('Costo no finito')
        # Misma tolerancia que en PLC: evita desempates accidentales por coma flotante.
        if costo < costo_mejor - 1e-6:
            mejor, costo_mejor = indice, costo
    return mejor


def ordenar_celdas(celdas, estrategia, **cinematica):
    libres = set(celdas)
    while libres:
        indice = elegir_celda(libres, estrategia, celdas, **cinematica)
        yield indice
        libres.remove(indice)


def destino_compatible(libres, elegido, celdas, **cinematica):
    """True si la celda que eligió el PLC con la estrategia 2 está libre y su costo empata con el mínimo.

    No exige el mismo índice: en celdas con costo igual, el redondeo de REAL en el PLC
    puede preferir otra celda equivalente sin que haya un error de contrato.
    """
    libres = set(libres)
    if elegido not in libres:
        return False
    minimo = min(costo_tiempo(celdas[i], **cinematica) for i in libres)
    return costo_tiempo(celdas[elegido], **cinematica) <= minimo + TOLERANCIA_EMPATE_S
