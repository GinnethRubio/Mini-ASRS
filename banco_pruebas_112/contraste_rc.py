"""Contraste integrado R vs C: parejas recuperar + almacenar en el sistema conectado.

Usa las mismas semillas y la misma lógica de solicitudes que experimentos/campana_rc.py. Python
decide la celda de cada almacenamiento (R aleatoria uniforme, C mínimo costo) y la envía al PLC
como destino propuesto (estrategia 3); el PLC verifica que esté libre, la acepta una sola vez y
ejecuta el ciclo. Antes de cada corrida el rack se lleva al inventario inicial de la semilla con
almacenamientos y recuperaciones reales.

python banco_pruebas_112/contraste_rc.py --confirmar-movimientos --inicio-runtime auto

Cierra el bloque (código 3) cuando el tiempo restante del runtime no alcanza para la corrida
siguiente; el progreso queda en estado_contraste.json y una nueva ejecución continúa desde allí.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

AQUI = Path(__file__).resolve().parent
RAIZ = AQUI.parent
for carpeta in (AQUI, RAIZ / 'coordinador', RAIZ / 'experimentos'):
    sys.path.insert(0, str(carpeta))

from campana_rc import cargar_celdas, recorridos  # noqa: E402

T_OPERACION_S = 40          # estimación conservadora por ciclo, para decidir si una corrida cabe en el bloque
ESPERA_BANDA_S = 5          # salida del sobre por la segunda banda tras una recuperación
SIN_AVANCE_S = 120
NOMBRES = ['GVL.Paso_Actual', 'GVL.Python_Error', 'GVL.Python_Mensaje', 'GVL.Comunicacion_OK', 'GVL.IN0',
           'GVL.IN1', 'GVL.Maquina_Lista', 'GVL.Inventario_Listo', 'GVL_HMI.xMarcha', 'GVL_HMI.iOperacionActual',
           'GVL_HMI.axOcupada', 'GVL_HMI.asCodigo', 'GVL_HMI.iAlmacenados', 'GVL_HMI.iRecuperados',
           'GVL_HMI.rTiempoCicloUltimo_s', 'GVL_HMI.rDistanciaUltima_mm', 'GVL_HMI.uSolicitudAceptada',
           'GVL_HMI.xModoManual', 'GVL_HMI.xModoPaso', 'GVL_HMI.xSimularCoppelia']
ESCRITURAS = {'GVL.START', 'GVL.STOP', 'GVL_HMI.iEstrategia', 'GVL_HMI.iDestinoPropuesto',
              'GVL_HMI.uSolicitudDestino', 'GVL_HMI.sCodigoRecuperar', 'GVL_HMI.xSolicitudRecuperar'}


def ahora_utc():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def plan_contraste(semilla, ocupaciones, replicas, parejas, costo_c='tiempo'):
    """Corridas en orden de ejecución, con inventario inicial y operaciones medidas por celda (1..112)."""
    import numpy as np
    from campana_rc import costos_c, elegir, generar_entradas
    celdas = cargar_celdas()
    costo = costos_c(celdas, costo_c)
    corridas = []
    for k, sk in zip(ocupaciones, np.random.SeedSequence(semilla).spawn(len(ocupaciones))):
        for replica, sr in enumerate(sk.spawn(replicas), 1):
            semillas = sr.spawn(3)
            # Orden alternado por réplica (R-C, C-R) para que la deriva del sistema durante la
            # campaña no favorezca a una política; la transición restituye el inventario inicial.
            for politica in (('R', 'C') if replica % 2 else ('C', 'R')):
                # Mismo inventario inicial y mismos primeros valores de demanda que la réplica del modelo.
                iniciales, _ = generar_entradas(k, [np.random.default_rng(s) for s in semillas], 0)
                fracciones = np.random.default_rng(semillas[1]).random(parejas)
                rng_r = np.random.default_rng(semillas[2])
                ubicacion = {f'AS-{n:04d}': i for n, i in enumerate(iniciales, 1)}
                siguiente, ops = k + 1, []
                for p, u in enumerate(fracciones):
                    existentes = sorted(ubicacion)
                    qr = existentes[int(u * len(existentes))]
                    ops.append(dict(tipo='RETRIEVE', celda=ubicacion.pop(qr), pareja=p))
                    destino = elegir(politica, set(celdas) - set(ubicacion.values()), costo, rng_r)
                    ubicacion[f'AS-{siguiente:04d}'] = destino
                    siguiente += 1
                    ops.append(dict(tipo='STORE', celda=destino, pareja=p))
                corridas.append(dict(ocupacion=k, replica=replica, politica=politica, inicial=sorted(iniciales),
                                     semillas=dict(ocupacion=list(sk.spawn_key),
                                                   replica=[list(x.spawn_key) for x in semillas]),
                                     operaciones=ops))
    return corridas


def transicion(actuales, objetivo):
    """Recuperar lo que sobra y almacenar lo que falta para llegar al inventario objetivo."""
    actuales, objetivo = set(actuales), set(objetivo)
    return ([dict(tipo='RETRIEVE', celda=c) for c in sorted(actuales - objetivo)]
            + [dict(tipo='STORE', celda=c) for c in sorted(objetivo - actuales)])


class Ejecutor:
    """Ejecuta una operación a la vez con START y paro normal, como la campaña del banco."""
    def __init__(self, leer, escribir, log, reloj=time.monotonic, dormir=time.sleep,
                 espera_comunicacion_s=300, intervalo=.2):
        self.leer, self.escribir, self.log = leer, escribir, log
        self.reloj, self.dormir = reloj, dormir
        self.espera_comunicacion_s, self.intervalo = espera_comunicacion_s, intervalo
        self.solicitud = None

    def mandar(self, n, v):
        if n not in ESCRITURAS:
            raise RuntimeError('Escritura no permitida: ' + n)
        self.escribir(n, v)
        self.log('mando', simbolo=n, valor=v)

    def comprobar(self, d):
        if d['GVL.Paso_Actual'] == 999 or d['GVL.Python_Error']:
            raise RuntimeError('Alarma: ' + str(d.get('GVL.Python_Mensaje') or 'secuencia en alarma') + '; sin rearme automático')
        if any(d['GVL_HMI.' + n] for n in ('xModoManual', 'xModoPaso', 'xSimularCoppelia')):
            raise RuntimeError('El contraste requiere automático continuo y CoppeliaSim real')
        return d['GVL.Comunicacion_OK'] and int(d['GVL.IN1']) & 28 == 28

    def en_espera(self, d):
        return d['GVL.Paso_Actual'] == 0 and not d['GVL_HMI.xMarcha']

    def ciclo(self, operacion, fin):
        """Espera el fin del ciclo; paro normal durante el ciclo para no encadenar otro."""
        paro, pausa, ultimo, firma = False, None, self.reloj(), None
        while True:
            d = self.leer()
            if not self.comprobar(d):
                pausa = pausa or self.reloj()
                if self.reloj() - pausa > self.espera_comunicacion_s:
                    raise RuntimeError(f'Comunicación o permisos FluidSIM ausentes por más de {self.espera_comunicacion_s:.0f} s')
                ultimo = self.reloj()
                self.dormir(self.intervalo)
                continue
            pausa = None
            f = (d['GVL.Paso_Actual'], d['GVL_HMI.iAlmacenados'], d['GVL_HMI.iRecuperados'])
            if f != firma:
                firma, ultimo = f, self.reloj()
            if self.reloj() - ultimo > SIN_AVANCE_S:
                raise TimeoutError(f'{SIN_AVANCE_S} s sin avance; revisar PLC, Python y FluidSIM')
            if not paro and d['GVL_HMI.iOperacionActual'] == operacion and d['GVL.Paso_Actual'] >= 40:
                self.mandar('GVL.STOP', True)
                paro = True
            if fin(d) and self.en_espera(d):
                return d
            self.dormir(self.intervalo)

    def preparar(self):
        d = self.leer()
        self.comprobar(d)
        if not self.en_espera(d):
            raise RuntimeError('El PLC debe estar en espera y detenido')
        if not d['GVL.Maquina_Lista'] or not d['GVL.Inventario_Listo']:
            raise RuntimeError('main.py no está listo')
        self.solicitud = int(d['GVL_HMI.uSolicitudAceptada'])
        self.mandar('GVL_HMI.iEstrategia', 3)
        return d

    def almacenar(self, celda):
        d0 = self.leer()
        if d0['GVL_HMI.axOcupada'][celda - 1]:
            raise RuntimeError(f'Celda {celda} ocupada antes de almacenar')
        self.solicitud += 1
        self.mandar('GVL_HMI.iDestinoPropuesto', celda)
        self.mandar('GVL_HMI.uSolicitudDestino', self.solicitud)
        n0 = d0['GVL_HMI.iAlmacenados']
        t0 = self.reloj()
        self.mandar('GVL.START', True)
        d = self.ciclo(1, lambda d: d['GVL_HMI.iAlmacenados'] == n0 + 1)
        if not d['GVL_HMI.axOcupada'][celda - 1] or not d['GVL_HMI.asCodigo'][celda - 1]:
            raise RuntimeError(f'El sobre no quedó en la celda propuesta {celda}')
        if int(d['GVL_HMI.uSolicitudAceptada']) != self.solicitud:
            raise RuntimeError('El PLC no confirmó la solicitud de destino')
        return dict(qr=d['GVL_HMI.asCodigo'][celda - 1], tiempo_s=d['GVL_HMI.rTiempoCicloUltimo_s'],
                    distancia_mm=d['GVL_HMI.rDistanciaUltima_mm'], inicio=t0, fin=self.reloj())

    def recuperar(self, celda):
        d0 = self.leer()
        qr = d0['GVL_HMI.asCodigo'][celda - 1]
        if not d0['GVL_HMI.axOcupada'][celda - 1] or not qr:
            raise RuntimeError(f'Celda {celda} vacía antes de recuperar')
        n0 = d0['GVL_HMI.iRecuperados']
        t0 = self.reloj()
        self.mandar('GVL_HMI.sCodigoRecuperar', qr)
        self.mandar('GVL_HMI.xSolicitudRecuperar', True)
        self.mandar('GVL.START', True)
        d = self.ciclo(2, lambda d: d['GVL_HMI.iRecuperados'] == n0 + 1)
        if d['GVL_HMI.axOcupada'][celda - 1]:
            raise RuntimeError(f'La celda {celda} sigue ocupada tras recuperar {qr}')
        resultado = dict(qr=qr, tiempo_s=d['GVL_HMI.rTiempoCicloUltimo_s'],
                         distancia_mm=d['GVL_HMI.rDistanciaUltima_mm'], inicio=t0, fin=self.reloj())
        self.dormir(ESPERA_BANDA_S)
        return resultado

    def ejecutar(self, op):
        return (self.almacenar if op['tipo'] == 'STORE' else self.recuperar)(op['celda'])


class Contraste:
    def __init__(self, args, ejecutor, plan, estado_path, nuevo_registro, ventana, log, reloj=time.monotonic,
                 completadas=None):
        self.args, self.ejecutor, self.plan = args, ejecutor, plan
        self.completadas_externas = completadas
        self.estado_path, self.nuevo_registro, self.ventana = Path(estado_path), nuevo_registro, ventana
        self.log, self.reloj = log, reloj
        try:
            self.celdas = cargar_celdas()
        except Exception:
            self.celdas = None
        self.estado = (json.loads(self.estado_path.read_text(encoding='utf-8')) if self.estado_path.exists()
                       else dict(campana=args.campana, siguiente=0, intentos={}, bloques=0))
        self.estado['bloques'] += 1

    def guardar(self):
        tmp = self.estado_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.estado, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(self.estado_path)

    def recorridos(self, op):
        if self.celdas is None:
            return None, None
        rx, rz = recorridos(self.celdas[op['celda']], op['tipo'])
        return round(rx, 1), round(rz, 1)

    def base(self, corrida):
        return f"{self.args.campana}_K{corrida['ocupacion']:03d}_r{corrida['replica']:02d}_{corrida['politica']}"

    def completadas(self):
        hechas = set(self.estado.get('completas', []))
        if self.completadas_externas is not None:
            hechas |= self.completadas_externas()
        return hechas

    def ocupadas(self):
        return sorted(i + 1 for i, o in enumerate(self.ejecutor.leer()['GVL_HMI.axOcupada']) if o)

    def cabe(self, n_operaciones):
        t = getattr(self.args, 't_operacion_s', T_OPERACION_S)
        return self.ventana is None or self.ventana.restante_s() >= n_operaciones * t

    def registrar(self, registro, op, r, fase, extra):
        rx, rz = self.recorridos(op)
        registro.operacion(recorrido_x_mm=rx, recorrido_z_mm=rz,fase=fase, operacion=op['tipo'], qr=r['qr'], columna=(op['celda'] - 1) // 28 + 1,
                           fila=(op['celda'] - 1) % 28 + 1, tiempo_s=r['tiempo_s'], distancia_mm=r['distancia_mm'],
                           inicio_mono_s=round(r['inicio'] - registro.t0, 3), fin_mono_s=round(r['fin'] - registro.t0, 3),
                           bloque=self.estado['bloques'], **extra)

    def ejecutar(self):
        """0 campaña completa, 3 bloque cerrado por tiempo de runtime; las excepciones se propagan."""
        self.ejecutor.preparar()
        self.guardar()
        preparacion = None
        try:
            while True:
                # Las corridas se identifican por K, réplica y política, no por su posición en el plan.
                hechas = self.completadas()
                pendientes = [j for j, c in enumerate(self.plan) if self.base(c) not in hechas]
                if not pendientes:
                    break
                i = pendientes[0]
                corrida = self.plan[i]
                # La transición es preparación: puede repartirse entre bloques del runtime.
                for op in transicion(self.ocupadas(), corrida['inicial']):
                    if not self.cabe(1):
                        self.log('bloque_cerrado', motivo='tiempo de runtime agotado durante la preparación', siguiente=i)
                        return 3
                    preparacion = preparacion or self.nuevo_registro(
                        f"prep_{self.args.campana}_b{self.estado['bloques']:02d}", None)
                    self.registrar(preparacion, op, self.ejecutor.ejecutar(op), 'diagnostico', {})
                if self.ocupadas() != corrida['inicial']:
                    raise RuntimeError('El inventario no coincide con el inicial de la corrida')
                if not self.cabe(len(corrida['operaciones'])):
                    self.log('bloque_cerrado', motivo='tiempo de runtime insuficiente para la corrida', siguiente=i)
                    return 3
                base = self.base(corrida)
                intento = self.estado['intentos'].get(base, 0)
                while True:
                    intento += 1
                    run_id = base + (f'_i{intento}' if intento > 1 else '')
                    try:
                        registro = self.nuevo_registro(run_id, corrida)
                        break
                    except FileExistsError:
                        continue    # intento previo registrado por otro bloque
                self.estado['intentos'][base] = intento
                self.guardar()
                try:
                    for op in corrida['operaciones']:
                        self.registrar(registro, op, self.ejecutor.ejecutar(op), 'medida', dict(pareja=op['pareja']))
                except BaseException as exc:
                    registro.fallo(tipo='corrida_interrumpida', detalle=repr(exc), consecuencia='se repite completa')
                    registro.cerrar('incompleta')
                    raise
                registro.cerrar('completa')
                self.estado.setdefault('completas', []).append(self.base(corrida))
                self.estado['siguiente'] = len(self.estado['completas'])
                self.guardar()
                self.log('corrida_completa', run_id=run_id, completas=len(self.estado['completas']), total=len(self.plan))
            # Rack vacío al final: deja el sistema listo para la campaña de cobertura.
            for op in transicion(self.ocupadas(), []) if getattr(self.args, 'vaciar', False) else []:
                if not self.cabe(1):
                    self.log('bloque_cerrado', motivo='tiempo de runtime agotado durante el vaciado', siguiente=len(self.plan))
                    return 3
                preparacion = preparacion or self.nuevo_registro(
                    f"prep_{self.args.campana}_b{self.estado['bloques']:02d}", None)
                self.registrar(preparacion, op, self.ejecutor.ejecutar(op), 'diagnostico', {})
            self.log('contraste_completo', corridas=len(self.plan))
            return 0
        finally:
            if preparacion is not None:
                preparacion.cerrar('completa')


class Ventana:
    def __init__(self, inicio, limite_min, margen_min):
        inicio = datetime.fromisoformat(inicio.replace('Z', '+00:00'))
        self.fin = inicio.timestamp() + (limite_min - margen_min) * 60

    def restante_s(self):
        return self.fin - time.time()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--confirmar-movimientos', action='store_true')
    ap.add_argument('--protocolo', type=Path, default=RAIZ / 'experimento.json')
    ap.add_argument('--campana', help='Identificador fijo de la campaña de contraste; por defecto se crea uno nuevo')
    ap.add_argument('--estado', type=Path, help='estado_contraste.json de un bloque anterior')
    ap.add_argument('--salida', type=Path, default=RAIZ / 'logs_112')
    ap.add_argument('--datos', type=Path, default=RAIZ / 'datos_experimentos')
    ap.add_argument('--opc-url', default=os.environ.get('ASRS_OPC_URL') or 'opc.tcp://localhost:4840')
    ap.add_argument('--inicio-runtime', default='auto')
    ap.add_argument('--limite-runtime-min', type=float, default=60)
    ap.add_argument('--margen-runtime-min', type=float, default=10)
    ap.add_argument('--espera-comunicacion-s', type=float, default=300)
    ap.add_argument('--vaciar', action='store_true', help='Recuperar todos los sobres al terminar')
    ap.add_argument('--solo-vaciar', action='store_true', help='Solo recuperar todos los sobres del rack')
    args = ap.parse_args()
    if not args.confirmar_movimientos:
        ap.error('El contraste mueve la simulación: agregar --confirmar-movimientos')
    p = json.loads(args.protocolo.read_text(encoding='utf-8'))
    c = p['contraste']
    estado_previo = json.loads(args.estado.read_text(encoding='utf-8')) if args.estado and args.estado.exists() else None
    args.campana = args.campana or (estado_previo or {}).get('campana') or datetime.now().strftime('%Y%m%d_%H%M%S') + '_rcC'
    args.t_operacion_s = c.get('t_operacion_estimado_s', T_OPERACION_S)
    if 2 * c['parejas'] * args.t_operacion_s > (args.limite_runtime_min - args.margen_runtime_min) * 60:
        ap.error('Una corrida no cabe en una ventana del runtime: reducir parejas o ampliar el límite')
    # Con vaciar_al_final = false el rack queda ocupado y la cobertura continúa desde ahí (sin vaciar y rellenar).
    args.vaciar = (args.vaciar or args.solo_vaciar) and c.get('vaciar_al_final', True)
    plan = [] if args.solo_vaciar else plan_contraste(c['semilla'], p['ocupaciones'], c['replicas'], c['parejas'], p['costo_c'])
    carpeta = args.salida / (datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '_contraste')
    carpeta.mkdir(parents=True)
    print('Logs:', carpeta.resolve(), flush=True)
    eventos = (carpeta / 'eventos.jsonl').open('w', encoding='utf-8', buffering=1)

    def log(tipo, **datos):
        eventos.write(json.dumps({'fecha_utc': ahora_utc(), 'tipo': tipo, **datos}, ensure_ascii=False, default=str) + '\n')
        if tipo != 'mando':
            print(tipo, datos, flush=True)

    inicio = args.inicio_runtime
    if inicio == 'auto':
        from sesiones_runtime import detectar_inicio_runtime
        inicio = detectar_inicio_runtime()
    ventana = Ventana(inicio, args.limite_runtime_min, args.margen_runtime_min)
    from codesys_opcua import CodesysOPCUA
    from opcua import ua
    from registro_experimental import Corrida
    plc = CodesysOPCUA(url=args.opc_url).conectar()
    ns = next(iter(plc.nodes.values())).nodeid.NamespaceIndex
    nodo = lambda n: plc.client.get_node(f'ns={ns};s=|var|CODESYS Control Win V3.Application.' + n)  # noqa: E731
    nodos = [nodo(n) for n in NOMBRES]

    def leer():
        d = dict(zip(NOMBRES, plc.client.get_values(nodos)))
        faltan = [n for n, v in d.items() if v is None]
        if faltan:
            raise RuntimeError('Símbolos sin publicar en el runtime (descargar el proyecto y crear la aplicación '
                               'de arranque): ' + ', '.join(faltan))
        return d

    def escribir(n, v):
        x = nodo(n)
        x.set_value(ua.Variant(v, x.get_data_type_as_variant_type()))

    import banco_112
    banco_112.verificar_estrategia_plc(plc, ns, 3)

    def nuevo_registro(run_id, corrida):
        return Corrida(args.datos, 'C', run_id, politica=corrida and corrida['politica'],
                       ocupacion=corrida and corrida['ocupacion'], replica=corrida and corrida['replica'],
                       semillas=corrida and dict(base=c['semilla'], **corrida['semillas']),
                       inventario_inicial=corrida and {f'AS-{n:04d}': i for n, i in enumerate(corrida['inicial'], 1)},
                       escenario=dict(campana=args.campana, parejas=c['parejas'], logs=str(carpeta)),
                       licencia=p['conectado']['licencia'],
                       alcance='Contraste integrado CODESYS-FluidSIM-CoppeliaSim; destino propuesto por Python (estrategia 3).')
    def completadas():
        """Corridas completas de la campaña según el índice del registro experimental."""
        import csv
        import re
        indice = args.datos / 'registro_corridas.csv'
        if not indice.exists():
            return set()
        with indice.open(encoding='utf-8') as f:
            return {re.sub(r'_i\d+$', '', r['run_id']) for r in csv.DictReader(f)
                    if r['estado'] == 'completa' and r['run_id'].startswith(args.campana + '_K')}
    estado_path = args.estado or (args.salida / f'estado_contraste_{args.campana}.json')
    contraste = Contraste(args, Ejecutor(leer, escribir, log, espera_comunicacion_s=args.espera_comunicacion_s),
                          plan, estado_path, nuevo_registro, ventana, log, completadas=completadas)
    try:
        codigo = contraste.ejecutar()
        if codigo == 3:
            (carpeta / 'continuar_campana.json').write_text(json.dumps(dict(
                motivo='pausa_runtime', estado=str(estado_path), siguiente=contraste.estado['siguiente'])), encoding='utf-8')
    except Exception as exc:
        (carpeta / 'estado_interrumpido.json').write_text(json.dumps(dict(motivo=repr(exc), estado=str(estado_path))),
                                                         encoding='utf-8')
        import traceback
        log('error', error=repr(exc), traza=traceback.format_exc())
        try:
            escribir('GVL.STOP', True)
        except Exception:
            pass
        print('ERROR:', exc, file=sys.stderr)
        codigo = 1
    finally:
        contraste.guardar()
        try:
            plc.desconectar()
        except Exception:
            pass
        eventos.close()
    print('Estado:', estado_path.resolve(), flush=True)
    return codigo


if __name__ == '__main__':
    sys.exit(main())
