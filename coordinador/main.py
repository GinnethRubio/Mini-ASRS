"""HMI -> PLC -> Python -> CoppeliaSim, con confirmaciones de retorno.

El PLC conserva la secuencia y el destino. FluidSIM conserva IN0/IN1/OUT0.
No ejecutar simultáneamente los main independientes de los actuadores.
"""
import math
import time
import rutas  # noqa: F401
from config import POLL_INTERVAL
from codesys_opcua import CodesysOPCUA
from coppelia_bridge import CoppeliaBridge
from control_portico import ControlPortico, Y_RETRAIDO, Y_EXTENDIDO
from control_gripper import ControlGripper, POSICION_AJUSTAR, POSICION_SOLTAR
from control_banda_integrado import ControlBanda
from lector_qr import leer_qr
from recuperacion import Recuperacion
from inventario import validar_inventario, marcar_deposito_confirmado
from sincronizacion_escena import sincronizar_inventario
from estrategias_colocacion import destino_compatible

ORDENES = ('Ir_Recogida','Ir_Almacenamiento','Y_Extender','Y_Retraer','Z_Ajuste','G_Ajustar','G_Soltar')
ORDENES = ORDENES + ('Ir_Despacho',)
MOVIMIENTOS = ORDENES[:5] + ('Ir_Despacho',)
X_RECOGIDA, Z_RECOGIDA = -0.262, -0.675

class CoordinadorMiniASRS:
    def __init__(self, plc, bridge):
        self.plc, self.bridge, self.sim = plc, bridge, bridge.sim
        self.portico = ControlPortico(self.sim, plc)
        self.gripper = ControlGripper(self.sim, plc)
        self.banda = ControlBanda(self.sim, plc)
        self.anteriores = {n: plc.leer_bool(n) for n in ORDENES}
        if any(self.anteriores.values()) or plc.leer_bool('CICLO_AUTO'):
            raise RuntimeError('El PLC debe estar detenido y sin órdenes antes de conectar Python.')
        self.qr_actual = None
        self.celda_actual = None
        self.abierto_en_deposito = False
        self.recuperacion = Recuperacion(self)
        self.recuperacion.salida.buscar_pendiente()
        self.heartbeat = int(plc.leer('PYTHON_Heartbeat'))
        self.manual_anterior = int(plc.leer('Manual_Orden'))
        self.plc.escribir_numero('Manual_Ack', self.manual_anterior)
        self._tiempo_escena = self.sim.getSimulationTime()

    def _en_posicion(self, x, z):
        return (abs(self.portico.posicion_x()-x) <= self.portico.tolerancia_xz
                and abs(self.portico.posicion_z()-z) <= self.portico.tolerancia_xz)

    def _ordenes(self):
        a = {n: self.plc.leer_bool(n) for n in ORDENES}
        if (a['Y_Extender'] and a['Y_Retraer']) or (a['G_Ajustar'] and a['G_Soltar']):
            raise RuntimeError('Órdenes opuestas simultáneas')
        if sum(a[n] for n in ('Ir_Recogida', 'Ir_Almacenamiento', 'Ir_Despacho')) > 1:
            raise RuntimeError('Dos destinos simultáneos')
        flanco = lambda n: a[n] and not self.anteriores[n]
        orden = self.portico.orden_activa
        if orden in a and not a[orden]:
            # El PLC retiró la orden (alarma o parada): el eje se detiene sin confirmar.
            self.portico.detener()
            self.plc.escribir_string('Python_Mensaje', 'Movimiento detenido: '+orden+' retirada')
        if self.portico.en_movimiento() and any(flanco(n) for n in MOVIMIENTOS):
            raise RuntimeError('Orden de movimiento con otro movimiento en curso')
        if int(self.plc.leer('Operacion_Actual')) == 2:
            self.recuperacion.ordenes(a, flanco)
            self._limpiar_confirmaciones(a)
            self.anteriores = a
            return
        if self.recuperacion.celda is not None:
            raise RuntimeError('Recuperación interrumpida; reconciliar sobre e inventario')
        if flanco('Ir_Despacho'):
            raise RuntimeError('Despacho solicitado fuera de recuperación')
        if flanco('Ir_Recogida'):
            if not self.qr_actual:
                raise RuntimeError('Recogida sin QR validado')
            col, fila = int(self.plc.leer('Columna_Destino')), int(self.plc.leer('Fila_Destino'))
            datos = validar_inventario()
            coincidencias = [c for c in datos['celdas'] if int(c['columna']) == col and int(c['fila']) == fila]
            if len(coincidencias) != 1 or coincidencias[0]['ocupada'] or coincidencias[0].get('qr'):
                raise RuntimeError('El destino elegido por el PLC no está libre en el JSON')
            if int(self.plc.leer('Estrategia')) == 2:
                # Verificar el contrato sin escribir el destino: el PLC sigue siendo su dueño.
                geometria = {(c-1)*28+f: v for (c, f), v in self.portico.coordenadas.items()}
                libres = [int(c['indice']) for c in datos['celdas'] if not c['ocupada']]
                if not destino_compatible(libres, int(coincidencias[0]['indice']), geometria):
                    raise RuntimeError('Estrategia 2 no coincide: revisar PLC, Excel y velocidades')
            self.celda_actual = coincidencias[0]
            self.portico.seleccionar_celda(col, fila)
            self.portico.ir_a_recogida(X_RECOGIDA, Z_RECOGIDA,
                                       lambda: self.plc.escribir_bool('Pos_Recogida', True))
        if flanco('Ir_Almacenamiento'):
            if self.celda_actual is None or self.portico._sobre_sujeto is None:
                raise RuntimeError('Almacenamiento sin celda o sin sobre sujeto')
            self.portico.ir_a_entrada_celda()
        if flanco('Y_Extender'): self.portico.extender_y()
        if flanco('Y_Retraer'):
            self.portico.retraer_y(self._registrar_deposito if self.abierto_en_deposito else None)
        if flanco('Z_Ajuste'):
            if abs(self.portico.posicion_y()-Y_EXTENDIDO) > self.portico.tolerancia_y:
                raise RuntimeError('Ajuste Z requiere Y extendido')
            self.portico.ajustar_z_a_salida()
        if flanco('G_Ajustar'):
            # Validar ANTES de mover la pinza.
            if not self.qr_actual or not self._en_posicion(X_RECOGIDA, Z_RECOGIDA):
                raise RuntimeError('Sujeción fuera de la estación de recogida')
            if abs(self.portico.posicion_y()-Y_EXTENDIDO) > self.portico.tolerancia_y:
                raise RuntimeError('Sujeción requiere Y extendido')
            if not self.gripper.mover(True): raise RuntimeError('Cierre no confirmado')
            self.portico.sujetar_sobre('/Sobre_'+self.qr_actual[3:])
        if flanco('G_Soltar'):
            if self.portico._sobre_sujeto is not None:
                obj = self.portico.celda_objetivo
                if not obj or not self._en_posicion(obj['x'], obj['z_salida']):
                    raise RuntimeError('Apertura fuera de la celda destino')
                if abs(self.portico.posicion_y()-Y_EXTENDIDO) > self.portico.tolerancia_y:
                    raise RuntimeError('Depósito requiere Y extendido')
                if not self.gripper.mover(False): raise RuntimeError('Apertura no confirmada')
                self.portico.liberar_sobre()
                self.abierto_en_deposito = True
            else:
                if not self.gripper.mover(False): raise RuntimeError('Apertura inicial no confirmada')
        self._limpiar_confirmaciones(a)
        self.anteriores = a

    def _limpiar_confirmaciones(self, a):
        for orden, ack in [('Ir_Recogida','Pos_Recogida'),
                           ('Ir_Almacenamiento','Pos_Almacenamiento'),
                           ('Ir_Despacho','Pos_Despacho'),
                           ('Z_Ajuste','Z_Ajuste_Terminado')]:
            if not a[orden]: self.plc.escribir_bool(ack, False)

    def _registrar_deposito(self):
        if not self.celda_actual or not self.qr_actual or self.portico._sobre_sujeto is not None:
            raise RuntimeError('Depósito incompleto')
        marcar_deposito_confirmado(self.qr_actual, int(self.celda_actual['indice']))
        self.plc.escribir_bool('Deposito_Confirmado', True)
        print('DEPÓSITO REGISTRADO:', self.qr_actual, self.celda_actual['indice'])
        self.qr_actual = self.celda_actual = None
        self.abierto_en_deposito = False
        self.banda.sobre = self.banda.qr = None
        self.banda._moviendo = False
        for n in ('SOBRE_POSIN','SOBRE_POS_FINAL','QR_DETECTADO'):
            self.plc.escribir_bool(n, False)

    def _qr(self):
        # Lectura breve desde SOBRE_POS_FINAL: evita el interbloqueo con P_IDENTIFICAR.
        if int(self.plc.leer('Operacion_Actual')) == 2:
            return
        if self.qr_actual or not self.plc.leer_bool('SOBRE_POS_FINAL'):
            return
        qr = leer_qr(self.sim, self.bridge.sensor_qr, timeout=0.05, mostrar_ventana=False)
        if not qr: return
        qr = qr.strip()
        if not (qr.startswith('AS-') and qr[3:].isdigit() and len(qr) <= 20):
            raise RuntimeError('QR inválido: '+qr)
        if any(c['ocupada'] and c.get('qr') == qr for c in validar_inventario()['celdas']):
            raise RuntimeError('QR duplicado: '+qr)
        self.qr_actual = qr
        self.plc.escribir_string('NUM_QR', qr)
        self.plc.escribir_bool('QR_DETECTADO', True)

    def _manual(self):
        orden = int(self.plc.leer('Manual_Orden'))
        if orden == self.manual_anterior: return
        self.manual_anterior = orden
        if orden == 0:
            if self.portico.orden_activa == 'Manual':
                self.portico.detener()
            self.plc.escribir_numero('Manual_Ack', 0)
            return
        def confirmar():
            self.plc.escribir_string('Python_Mensaje', 'Mando manual confirmado')
            self.plc.escribir_numero('Manual_Ack', orden)
        try:
            if not self.plc.leer_bool('Manual_Permitido'):
                raise RuntimeError('Mando manual no permitido por el PLC')
            if orden in (1,2,3,4):
                if self.portico._sobre_sujeto is not None or abs(self.portico.posicion_y()-Y_RETRAIDO) > self.portico.tolerancia_y:
                    raise RuntimeError('X/Z manual requiere Y retraído y pinza sin sobre')
                joint = self.portico.joint_x if orden in (1,2) else self.portico.joint_z
                actual = float(self.sim.getJointPosition(joint))
                objetivo = actual + (0.005 if orden in (2,4) else -0.005)
                ciclico, limites = self.sim.getJointInterval(joint)
                if ciclico or not all(math.isfinite(float(x)) for x in limites):
                    raise RuntimeError('Límites articulares inválidos')
                if not limites[0] <= objetivo <= limites[0]+limites[1]:
                    raise RuntimeError('Límite de recorrido alcanzado')
                self.portico._mover_joint_y_verificar(joint, objetivo, 0.002, 'Manual X/Z')
            elif orden in (5,6):
                # Y confirma (Manual_Ack) al terminar su recorrido; el PLC no emite otra orden antes.
                (self.portico.extender_y if orden == 5 else self.portico.retraer_y)(confirmar, 'Manual')
                return
            elif orden in (7,8):
                if self.portico._sobre_sujeto is not None:
                    raise RuntimeError('Pinza manual bloqueada con sobre sujeto')
                if not self.gripper.mover(orden == 7): raise RuntimeError('Pinza no confirmó')
            elif orden == 9: self.banda.crear_sobre()
            else: raise RuntimeError('Orden manual desconocida')
            confirmar()
        except RuntimeError as exc:
            self.plc.escribir_string('Python_Mensaje', str(exc)[:80])
            self.plc.escribir_numero('Manual_Ack', orden)

    def _verificar_escena(self):
        estado = self.sim.getSimulationState()
        tiempo = self.sim.getSimulationTime()
        anterior = getattr(self, '_tiempo_escena', tiempo)
        if estado == self.sim.simulation_stopped or tiempo < anterior - .001:
            self.portico.detener()
            self.recuperacion.salida.pausar()
            raise RuntimeError('Escena reiniciada: detenga PLC y reinicie main.py para sincronizar')
        self._tiempo_escena = tiempo

    def ciclo(self):
        # No reutilizar trayectorias, QR ni handles tras reiniciar CoppeliaSim.
        self._verificar_escena()
        self.heartbeat = (self.heartbeat+1) % 4294967296
        self.plc.escribir_numero('PYTHON_Heartbeat', self.heartbeat)
        self.plc.escribir_bool('COPPELIA_Conectado', True)
        self.plc.actualizar_posicion_x(self.portico.posicion_x())
        self.plc.actualizar_posicion_z(self.portico.posicion_z())
        y = self.portico.posicion_y()
        # La tolerancia puede alcanzarse antes de finalizar el perfil. No publicar
        # un extremo que permita al PLC retirar la orden y cancelar su callback.
        y_pendiente = any(segmento[0] == self.portico.joint_y
                          for segmento in self.portico._segmentos)
        y_valido = (not y_pendiente and
                    min(abs(y-Y_RETRAIDO), abs(y-Y_EXTENDIDO)) <= self.portico.tolerancia_y)
        self.plc.escribir_bool('Y_ESTADO_VALIDO', y_valido)
        if y_valido: self.plc.actualizar_estado_y(abs(y-Y_EXTENDIDO) <= self.portico.tolerancia_y)
        g = float(self.sim.getJointPosition(self.gripper.joint))
        g_valido = min(abs(g-POSICION_SOLTAR), abs(g-POSICION_AJUSTAR)) <= 0.001
        self.plc.escribir_bool('GRIPPER_ESTADO_VALIDO', g_valido)
        if g_valido: self.plc.actualizar_estado_gripper(abs(g-POSICION_AJUSTAR) <= 0.001)
        self._manual()
        if not self.plc.leer_bool('Comunicacion_OK'):
            self.portico.detener()
            self.recuperacion.salida.pausar()
            return
        if self.plc.leer_bool('Manual_Permitido'):
            # Las ordenes manuales ya fueron atendidas por su protocolo ACK.
            self.anteriores = {n: self.plc.leer_bool(n) for n in ORDENES}
        else:
            self._ordenes()
        self.portico.avanzar()
        # Solo transportar con comunicación vigente y permisos de FluidSIM.
        entradas = int(self.plc.leer('Entradas_FluidSIM'))
        paso = int(self.plc.leer('Paso_Actual'))
        permitido_salida = (paso != 999 and entradas & 0x1C == 0x1C
                            and not self.plc.leer_bool('Python_Error'))
        self.recuperacion.salida.ciclo(permitido_salida)
        self.banda.ciclo()
        self._qr()
        # Solo P_Y_RET_DEPOSITAR (130) y P_REGISTRAR (140) pueden ver la confirmación; fuera de ellos se borra siempre.
        if int(self.plc.leer('Paso_Actual')) not in (130,140) and not self.abierto_en_deposito and not self.recuperacion.abierto:
            self.plc.escribir_bool('Deposito_Confirmado', False)

def main():
    plc, bridge = None, None
    try:
        datos = validar_inventario()
        plc = CodesysOPCUA().conectar()
        if plc.leer_bool('CICLO_AUTO') or int(plc.leer('Paso_Actual')) not in (0,999):
            raise RuntimeError('Detenga y rearme el PLC antes de iniciar el coordinador')
        # Limpia confirmaciones que pudieron quedar activas de una ejecución anterior.
        for n in ('Maquina_Lista','Inventario_Listo','Python_Error','COPPELIA_Conectado',
                  'SOBRE_POSIN','SOBRE_POS_FINAL','QR_DETECTADO','Deposito_Confirmado',
                  'Pos_Recogida','Pos_Almacenamiento','Pos_Despacho','Z_Ajuste_Terminado'):
            plc.escribir_bool(n, False)
        bridge = CoppeliaBridge().conectar()
        coordinador = CoordinadorMiniASRS(plc, bridge)
        # La escena es la fuente de ocupación en cada conexión nueva. El JSON
        # anterior se respalda si difiere; no se mueve ni se crea ningún sobre.
        datos = sincronizar_inventario(bridge.sim, coordinador.portico.coordenadas)
        plc.publicar_inventario(datos)
        plc.escribir_string('NUM_QR', '')
        plc.escribir_string('Python_Mensaje', 'Python y CoppeliaSim preparados')
        plc.escribir_bool('Maquina_Lista', True)
        print('Listo. Arranque desde la HMI; no se inicia automáticamente.')
        while True:
            coordinador.ciclo()
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        print('Coordinador detenido por el usuario')
    except Exception as exc:
        print('ERROR:', exc)
        if plc and plc.conectado:
            try:
                plc.escribir_string('Python_Mensaje', str(exc)[:80])
                plc.escribir_bool('Python_Error', True)
            except Exception: pass
    finally:
        if plc and plc.conectado:
            for n in ('Maquina_Lista','COPPELIA_Conectado'):
                try: plc.escribir_bool(n, False)
                except Exception: pass
            plc.desconectar()
        if bridge: bridge.cerrar()

if __name__ == '__main__': main()
