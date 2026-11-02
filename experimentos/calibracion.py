"""Calibración del modelo rápido con tiempos medidos en el sistema integrado.

Por tipo de operación se ajusta t_integrado = a + b * t_modelo por mínimos cuadrados. El término a
recoge los tiempos fijos que el modelo no ve (sondeo de OPC UA, FluidSIM, escena) y b la diferencia
de escala de los movimientos. Con menos de MIN_PUNTOS operaciones solo se ajusta a (b = 1).
"""
import json
import statistics
from pathlib import Path

MIN_PUNTOS = 10


def tiempo_modelo(celda, operacion):
    from campana_rc import ciclo_recuperacion
    from evaluar_estrategias import ciclo_almacenamiento
    return (ciclo_almacenamiento if operacion == 'STORE' else ciclo_recuperacion)(celda)[0]


def ajustar(puntos):
    """puntos: [(operacion, t_modelo, t_integrado)] -> {operacion: parámetros y error antes y después}."""
    resultado = {}
    for op in sorted({p[0] for p in puntos}):
        xs = [p[1] for p in puntos if p[0] == op]
        ys = [p[2] for p in puntos if p[0] == op]
        mx, my = statistics.mean(xs), statistics.mean(ys)
        sxx = sum((x - mx) ** 2 for x in xs)
        if len(xs) >= MIN_PUNTOS and sxx > 0:
            b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
        else:
            b = 1.0
        a = my - b * mx
        resultado[op] = dict(a_s=a, b=b, n=len(xs),
                             mae_antes_s=statistics.mean(abs(y - x) for x, y in zip(xs, ys)),
                             mae_despues_s=statistics.mean(abs(y - (a + b * x)) for x, y in zip(xs, ys)),
                             media_integrado_s=my, max_integrado_s=max(ys))
    return resultado


def cargar(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))['parametros'] if path else None


def aplicar(parametros, operacion, t):
    if not parametros or operacion not in parametros:
        return t
    p = parametros[operacion]
    return p['a_s'] + p['b'] * t
