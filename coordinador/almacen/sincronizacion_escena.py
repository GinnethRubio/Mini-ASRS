"""Reconstruye la ocupación desde la escena sin mover sobres ni articulaciones."""
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from config import JSON_PATH
from inventario import validar_inventario
from control_banda_integrado import POSICION_INICIAL, POSICION_FINAL
from control_banda_salida import X_SALIDA, Z_SALIDA, Y_INICIO_SALIDA, Y_FINAL_SALIDA


def inventario_desde_escena(sim, coordenadas, datos):
    resultado = json.loads(json.dumps(datos))
    celdas = {(int(c['columna']), int(c['fila'])): c for c in resultado['celdas']}
    if len(celdas) != len(resultado['celdas']) or set(celdas) != set(coordenadas):
        raise RuntimeError('Las coordenadas del inventario no coinciden con el Excel')
    for celda in celdas.values():
        celda['ocupada'], celda['qr'] = False, ''
    vistos = set()
    for h in sim.getObjectsInTree(sim.handle_scene, sim.handle_all, 0):
        alias = sim.getObjectAlias(h)
        if not alias.startswith('Sobre_') or alias == 'Sobre_000':
            continue
        sufijo = alias[6:]
        if not sufijo.isdigit() or len(sufijo) > 17:
            raise RuntimeError('Alias de sobre no válido: ' + alias)
        qr = 'AS-' + sufijo
        if qr in vistos:
            raise RuntimeError('Sobres duplicados en escena: ' + qr)
        vistos.add(qr)
        padre = sim.getObjectParent(h)
        if padre != -1 and 'Gripper' in sim.getObjectAlias(padre):
            raise RuntimeError(qr + ' sigue sujeto al gripper; reconciliar el ciclo interrumpido')
        pos = sim.getObjectPosition(h, -1)
        # Estaciones de entrada y salida: no son celdas del inventario.
        if any(all(abs(pos[i] - destino[i]) <= .015 for i in range(3))
               for destino in (POSICION_INICIAL, POSICION_FINAL)):
            continue
        if (abs(pos[0] - X_SALIDA) <= .015 and abs(pos[2] - Z_SALIDA) <= .015
                and Y_INICIO_SALIDA - .015 <= pos[1] <= Y_FINAL_SALIDA):
            continue
        coincidencias = []
        for clave, coord in coordenadas.items():
            esperado = [POSICION_FINAL[0] + coord['x'] + .262,
                        POSICION_FINAL[1], POSICION_FINAL[2] + coord['z_salida'] + .675]
            if all(abs(pos[i] - esperado[i]) <= .015 for i in range(3)):
                coincidencias.append(clave)
        if len(coincidencias) != 1:
            raise RuntimeError(qr + ' fuera de una ubicación conocida; no se cambia el inventario')
        celda = celdas[coincidencias[0]]
        if celda['ocupada']:
            raise RuntimeError('Dos sobres en la misma celda; revisar escena')
        celda['ocupada'], celda['qr'] = True, qr
    ocupadas = sum(c['ocupada'] for c in celdas.values())
    resultado['resumen'] = {'celdas_ocupadas': ocupadas, 'celdas_libres': len(celdas) - ocupadas}
    return resultado


def sincronizar_inventario(sim, coordenadas, path=JSON_PATH):
    path = Path(path)
    anteriores = validar_inventario(path)
    actuales = inventario_desde_escena(sim, coordenadas, anteriores)
    if actuales != anteriores:
        ahora = datetime.now(timezone(timedelta(hours=-5)))
        respaldo = path.with_name('inventario_antes_sincronizacion_' + ahora.strftime('%Y%m%d_%H%M%S_%f') + '.json')
        with respaldo.open('xb') as f:
            f.write(path.read_bytes())
        actuales['ultima_actualizacion'] = ahora.isoformat(timespec='seconds')
        temporal = path.with_name(path.name + '.sincronizacion.tmp')
        temporal.write_text(json.dumps(actuales, ensure_ascii=False, indent=2), encoding='utf-8')
        temporal.replace(path)
        print('INVENTARIO SINCRONIZADO CON LA ESCENA. Respaldo:', respaldo.name)
    print('ESCENA ACTUAL:', actuales['resumen']['celdas_ocupadas'], 'ocupadas;',
          actuales['resumen']['celdas_libres'], 'libres')
    return actuales
