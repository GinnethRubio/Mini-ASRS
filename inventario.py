"""Validación y operaciones básicas sobre inventario_mini_asrs.json."""
import json
from datetime import datetime
from pathlib import Path

from config import JSON_PATH, NUM_CELDAS


def cargar_inventario(path=JSON_PATH):
    path = Path(path)
    if not path.is_file():
        plantilla = Path(__file__).resolve().parent / "inventario_plantilla.json"
        if path.resolve() != Path(JSON_PATH).resolve() or not plantilla.is_file():
            raise FileNotFoundError(f"No existe el inventario: {path}")
        # Primera ejecución: rack vacío; main.py lo sincroniza luego con la escena.
        path.write_bytes(plantilla.read_bytes())
    with path.open("r", encoding="utf-8") as f:
        datos = json.load(f)
    if not isinstance(datos, dict) or not isinstance(datos.get("celdas"), list):
        raise ValueError("El JSON debe contener una lista bajo la clave 'celdas'.")
    return datos


def validar_inventario(path=JSON_PATH):
    datos = cargar_inventario(path)
    celdas = datos["celdas"]
    if len(celdas) != NUM_CELDAS:
        raise ValueError(f"Se esperaban {NUM_CELDAS} celdas; hay {len(celdas)}.")

    indices, qr_ocupados = set(), set()
    for pos, celda in enumerate(celdas, start=1):
        if not isinstance(celda, dict):
            raise ValueError(f"Registro {pos} no es un objeto.")
        indice = int(celda.get("indice", pos))
        if not 1 <= indice <= NUM_CELDAS:
            raise ValueError(f"Índice fuera de rango: {indice}")
        if indice in indices:
            raise ValueError(f"Índice duplicado: {indice}")
        indices.add(indice)

        qr = str(celda.get("qr") or "").strip()
        ocupada = celda.get("ocupada", False)
        if not isinstance(ocupada, bool):
            raise ValueError(f"El campo 'ocupada' de la celda {indice} debe ser booleano.")
        if len(qr) > 20:
            raise ValueError(f"QR demasiado largo en celda {indice}.")
        if ocupada and not qr:
            raise ValueError(f"Celda {indice} ocupada sin QR.")
        if ocupada:
            if qr in qr_ocupados:
                raise ValueError(f"QR duplicado en inventario: {qr}")
            qr_ocupados.add(qr)

    if indices != set(range(1, NUM_CELDAS + 1)):
        raise ValueError("Faltan índices de celda.")
    return datos


def buscar_celda_libre(path=JSON_PATH):
    datos = validar_inventario(path)
    for celda in sorted(datos["celdas"], key=lambda c: int(c["indice"])):
        if not celda["ocupada"] and not str(celda.get("qr", "")).strip():
            return dict(celda)
    return None


def buscar_qr(qr, path=JSON_PATH):
    qr = str(qr).strip()
    datos = validar_inventario(path)
    for celda in datos["celdas"]:
        if celda["ocupada"] and str(celda.get("qr", "")).strip() == qr:
            return dict(celda)
    return None


def marcar_deposito_confirmado(qr, indice, path=JSON_PATH):
    """Marca ocupación SOLO después de confirmar físicamente el depósito."""
    qr = str(qr).strip()
    if not qr or len(qr) > 20:
        raise ValueError("El identificador QR debe tener entre 1 y 20 caracteres.")

    datos = validar_inventario(path)
    objetivo = next((c for c in datos["celdas"] if int(c["indice"]) == int(indice)), None)
    if objetivo is None:
        raise ValueError(f"No existe la celda {indice}.")
    if objetivo["ocupada"] or str(objetivo.get("qr", "")).strip():
        raise ValueError(f"La celda {indice} ya está ocupada.")
    if any(c["ocupada"] and str(c.get("qr", "")).strip() == qr for c in datos["celdas"]):
        raise ValueError(f"El QR {qr} ya está registrado en otra celda.")

    objetivo["qr"] = qr
    objetivo["ocupada"] = True
    ocupadas = sum(1 for c in datos["celdas"] if c["ocupada"])
    datos["resumen"] = {"celdas_ocupadas": ocupadas, "celdas_libres": NUM_CELDAS - ocupadas}
    datos["ultima_actualizacion"] = datetime.now().astimezone().isoformat(timespec="seconds")

    path = Path(path)
    temporal = path.with_suffix(path.suffix + ".tmp")
    temporal.write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")
    temporal.replace(path)
    return dict(objetivo)


def liberar_celda(indice, path=JSON_PATH):
    """Libera una celda solo cuando la retirada física se haya confirmado."""
    datos = validar_inventario(path)
    objetivo = next((c for c in datos["celdas"] if int(c["indice"]) == int(indice)), None)
    if objetivo is None:
        raise ValueError(f"No existe la celda {indice}.")
    objetivo["qr"] = ""
    objetivo["ocupada"] = False
    ocupadas = sum(1 for c in datos["celdas"] if c["ocupada"])
    datos["resumen"] = {"celdas_ocupadas": ocupadas, "celdas_libres": NUM_CELDAS - ocupadas}
    datos["ultima_actualizacion"] = datetime.now().astimezone().isoformat(timespec="seconds")
    path = Path(path)
    temporal = path.with_suffix(path.suffix + ".tmp")
    temporal.write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")
    temporal.replace(path)
    return dict(objetivo)
