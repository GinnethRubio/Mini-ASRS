"""Traslado de los bloques del banco conectado al registro experimental.

Cada bloque de banco_112.py deja su carpeta de logs (eventos.jsonl, operaciones_observadas.csv,
resumen.json). Este módulo copia esas operaciones a la corrida abierta en registro_experimental,
con el número de bloque y los tiempos en el reloj monotónico de la corrida, y registra como
fallo cualquier error del bloque.
"""
import csv
import json
from pathlib import Path

OPERACION = {'almacenar': 'STORE', 'recuperar': 'RETRIEVE'}


def importar_bloque(corrida, carpeta, bloque, t0_monotonic):
    """Copia las operaciones observadas de un bloque. Devuelve cuántas importó."""
    carpeta = Path(carpeta)
    archivo = carpeta / 'operaciones_observadas.csv'
    n = 0
    if archivo.exists():
        with archivo.open(encoding='utf-8-sig') as f:
            for r in csv.DictReader(f):
                tiempo = float(r['tiempo_plc_s'] or 0)
                fin = float(r['monotonic_s']) - t0_monotonic if r.get('monotonic_s') else None
                ok = r['verificacion_inventario'] == 'True'
                corrida.operacion(
                    fase='medida', bloque=bloque, qr=r['codigo'], operacion=OPERACION[r['operacion']],
                    columna=r['columna'], fila=r['fila'], fecha_utc=r['fecha_utc'],
                    inicio_mono_s=round(fin - tiempo, 3) if fin is not None else None,
                    fin_mono_s=round(fin, 3) if fin is not None else None,
                    tiempo_s=tiempo, distancia_mm=r['distancia_plc_mm'],
                    resultado='correcto' if ok else 'fallido',
                    fallo=None if ok else 'registro PLC e inventario no coinciden')
                n += 1
    eventos = carpeta / 'eventos.jsonl'
    if eventos.exists():
        for linea in eventos.read_text(encoding='utf-8').splitlines():
            e = json.loads(linea)
            if e['tipo'] in ('error_registrador', 'alarma_observada', 'hueco_log_plc', 'paro_no_entregado'):
                corrida.fallo(tipo=e['tipo'], detalle=e.get('error') or e.get('mensaje') or e.get('eventos_perdidos'),
                              consecuencia=f'bloque {bloque}: ver {carpeta.name}/eventos.jsonl')
    corrida.evento('bloque_importado', bloque=bloque, carpeta=str(carpeta), operaciones=n)
    return n
