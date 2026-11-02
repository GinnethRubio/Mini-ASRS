"""Rutas de importación del coordinador: permite importar los módulos de cada carpeta por su nombre."""
import sys
from pathlib import Path

COORDINADOR = Path(__file__).resolve().parent
CARPETAS = ('comunicacion', 'movimiento', 'almacen', 'estrategias')

for carpeta in (COORDINADOR, *(COORDINADOR / c for c in CARPETAS)):
    if str(carpeta) not in sys.path:
        sys.path.insert(0, str(carpeta))
