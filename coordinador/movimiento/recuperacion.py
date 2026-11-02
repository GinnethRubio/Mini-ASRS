"""Recuperación del rack y entrega en la segunda banda; comparte conexiones."""
import math
import time
from inventario import validar_inventario, buscar_qr, liberar_celda
from control_portico import Y_RETRAIDO, Y_EXTENDIDO
from config import T_ACTUADOR_PLC, T_MOVIMIENTO_PLC
from control_banda_integrado import POSICION_FINAL
from control_banda_salida import ControlBandaSalida

# Posiciones ARTICULARES. La banda de salida está 0.525 m a la derecha
# de la de entrada en la escena inspeccionada (misma altura y orientación).
X_DESPACHO = -0.262 + 0.525
Z_DESPACHO = -0.675
HOLGURA_DESPACHO = 0.020
POS_SOBRE_DESPACHO = [POSICION_FINAL[0] + 0.525, POSICION_FINAL[1], POSICION_FINAL[2]]


class Recuperacion:
    def __init__(self, coordinador):
        self.c = coordinador
        self.celda = None
        self.qr = None
        self.abierto = False
        self.extraido = False
        self.sobre_despachado = None
        self.salida = ControlBandaSalida(coordinador.sim)

    @property
    def p(self):
        return self.c.portico

    def _y(self, objetivo):
        if abs(self.p.posicion_y() - objetivo) > self.p.tolerancia_y:
            raise RuntimeError('Recuperación: posición Y incorrecta')

    def _pos(self, x, z):
        if not self.c._en_posicion(x, z):
            raise RuntimeError('Recuperación: posición X/Z incorrecta')

    def _objeto(self, qr):
        if not qr.startswith('AS-') or not qr[3:].isdigit():
            raise RuntimeError('QR de recuperación inválido')
        ruta = '/Sobre_' + qr[3:]
        h = self.c.sim.getObject(ruta, {'noError': True})
        if h == -1:
            raise RuntimeError('No existe en la escena: ' + ruta)
        return h

    def _validar_despacho(self):
        if self.salida.sobre is not None:
            raise RuntimeError('Segunda banda transportando un sobre; espere su salida')
        sim = self.c.sim
        entrada = sim.getObject('/Prismatico_Banda_Entrada')
        salida = sim.getObject('/Prismatico_Banda_Salida')
        a, b = sim.getObjectMatrix(entrada, -1), sim.getObjectMatrix(salida, -1)
        if any(abs(b[i] - a[i] - d) > 0.003 for i, d in ((3, .525), (7, 0), (11, 0))):
            raise RuntimeError('La geometría de despacho cambió; recalibrar coordenadas')
        if any(abs(a[i] - b[i]) > .001 for i in (0, 1, 2, 4, 5, 6, 8, 9, 10)):
            raise RuntimeError('Las bandas tienen distinta orientación')
        for joint, objetivo in ((self.p.joint_x, X_DESPACHO), (self.p.joint_z, Z_DESPACHO),
                                (self.p.joint_z, Z_DESPACHO + HOLGURA_DESPACHO)):
            ciclico, limites = sim.getJointInterval(joint)
            if ciclico or not all(math.isfinite(float(v)) for v in limites):
                raise RuntimeError('Límites articulares no válidos')
            if not limites[0] <= objetivo <= sum(limites):
                raise RuntimeError('Despacho fuera de límites articulares')
        # Con el rack lleno, consultar la posición de cada sobre tarda más que el
        # heartbeat. Los sobres registrados están en su celda; solo se buscan los
        # demás. La banda de entrada numera con el menor número libre, así que un
        # sobre sin registrar nunca supera registrados + sobres en escena.
        inicio_validacion=time.monotonic()
        registrados={int(q[3:]) for c in validar_inventario()['celdas']
                     for q in [str(c.get('qr') or '').strip()] if c['ocupada'] and q[3:].isdigit()}
        tope=max(registrados|{len(registrados)})+16
        for n in range(tope+1):
            if n in registrados:
                continue
            if time.monotonic()-inicio_validacion>7:
                raise RuntimeError('Validación de despacho lenta; revisar conexión con CoppeliaSim')
            h=sim.getObject(f'/Sobre_{n:03d}',{'noError':True})
            if h==-1 or h == self.p._sobre_sujeto:
                continue
            pos = sim.getObjectPosition(h, -1)
            if all(abs(pos[i] - POS_SOBRE_DESPACHO[i]) < t for i, t in enumerate((.15, .25, .08))):
                raise RuntimeError('Segunda banda ocupada: retire el sobre de despacho')

    def ir_al_origen(self):
        if self.celda is not None or self.p._sobre_sujeto is not None:
            raise RuntimeError('Ya hay una recuperación o un sobre en curso')
        self._y(Y_RETRAIDO)
        if self.c.abierto_en_deposito or self.c.celda_actual is not None:
            raise RuntimeError('No se puede recuperar con depósito pendiente')
        qr = self.c.plc.leer_string('Codigo_Operacion').strip()
        celda = buscar_qr(qr)
        if celda is None:
            raise RuntimeError('QR no registrado para recuperación: ' + qr)
        col = int(self.c.plc.leer('Columna_Destino'))
        fila = int(self.c.plc.leer('Fila_Destino'))
        if (col, fila) != (int(celda['columna']), int(celda['fila'])):
            raise RuntimeError('Origen PLC distinto del inventario JSON')
        obj = self._objeto(qr)
        coord = self.p.coordenadas[(col, fila)]
        esperado = [POSICION_FINAL[0] + coord['x'] + .262,
                    POSICION_FINAL[1], POSICION_FINAL[2] + coord['z_salida'] + .675]
        pos = self.c.sim.getObjectPosition(obj, -1)
        if any(abs(pos[i] - esperado[i]) > .015 for i in range(3)):
            raise RuntimeError('Inventario y escena no coinciden: ' + qr + ' fuera de su celda')
        if coord['z_entrada'] <= coord['z_salida']:
            raise RuntimeError('La altura de extracción debe superar la de apoyo')
        self._validar_despacho()
        self.celda, self.qr = celda, qr
        self.p.seleccionar_celda(col, fila)
        self.p._iniciar([
            (self.p.joint_z, coord['z_salida'], self.p.tolerancia_xz, 'Z al origen'),
            (self.p.joint_x, coord['x'], self.p.tolerancia_xz, 'X al origen'),
        ], T_MOVIMIENTO_PLC, lambda: self.c.plc.escribir_bool('Pos_Almacenamiento', True), 'Ir_Almacenamiento')

    def sujetar(self):
        if self.celda is None or self.extraido:
            raise RuntimeError('Recuperación sin origen válido')
        coord = self.p.celda_objetivo
        self._y(Y_EXTENDIDO)
        self._pos(coord['x'], coord['z_salida'])
        self._objeto(self.qr)
        if not self.c.gripper.mover(True):
            raise RuntimeError('Cierre de recuperación no confirmado')
        self.p.sujetar_sobre('/Sobre_' + self.qr[3:])

    def retraer(self):
        if self.abierto:
            self.p.retraer_y(self.confirmar)
        elif not self.extraido and self.celda is not None:
            if self.p._sobre_sujeto is None:
                raise RuntimeError('Extracción sin sobre sujeto')
            self._y(Y_EXTENDIDO)
            coord = self.p.celda_objetivo
            self._pos(coord['x'], coord['z_salida'])
            def fin():
                self.extraido = True
                self.c.plc.actualizar_estado_y(False)
            # Elevar antes de retraer para liberar el apoyo del rack.
            self.p._iniciar([
                (self.p.joint_z, coord['z_entrada'], self.p.tolerancia_xz, 'Elevar sobre del rack'),
                (self.p.joint_y, Y_RETRAIDO, self.p.tolerancia_y, 'Extraer sobre'),
            ], T_ACTUADOR_PLC, fin, 'Y_Retraer')
        else:
            self.p.retraer_y()

    def ir_a_despacho(self):
        if not self.extraido or self.p._sobre_sujeto is None:
            raise RuntimeError('Despacho sin extracción confirmada')
        self._y(Y_RETRAIDO)
        self._validar_despacho()
        self.p._iniciar([
            (self.p.joint_x, X_DESPACHO, self.p.tolerancia_xz, 'X a segunda banda'),
            (self.p.joint_z, Z_DESPACHO + HOLGURA_DESPACHO, self.p.tolerancia_xz, 'Z a segunda banda'),
        ], T_MOVIMIENTO_PLC, lambda: self.c.plc.escribir_bool('Pos_Despacho', True), 'Ir_Despacho')

    def ajustar(self):
        if not self.extraido or self.p._sobre_sujeto is None:
            raise RuntimeError('Ajuste de despacho sin sobre')
        self._y(Y_EXTENDIDO)
        self._pos(X_DESPACHO, Z_DESPACHO + HOLGURA_DESPACHO)
        self.p._iniciar([(self.p.joint_z, Z_DESPACHO, self.p.tolerancia_xz, 'Apoyar en segunda banda')],
                        T_MOVIMIENTO_PLC, lambda: self.c.plc.escribir_bool('Z_Ajuste_Terminado', True), 'Z_Ajuste')

    def soltar(self):
        if self.p._sobre_sujeto is None:
            if self.celda is not None:
                raise RuntimeError('Recuperación sin sobre para soltar')
            if not self.c.gripper.mover(False):
                raise RuntimeError('Apertura inicial no confirmada')
            return
        self._y(Y_EXTENDIDO)
        self._pos(X_DESPACHO, Z_DESPACHO)
        pos = self.c.sim.getObjectPosition(self.p._sobre_sujeto, -1)
        if any(abs(pos[i] - POS_SOBRE_DESPACHO[i]) > .015 for i in range(3)):
            raise RuntimeError('Sobre fuera de la posición de entrega calculada')
        if not self.c.gripper.mover(False):
            raise RuntimeError('Apertura de despacho no confirmada')
        self.sobre_despachado = self.p._sobre_sujeto
        self.p.liberar_sobre()
        self.abierto = True

    def confirmar(self):
        self._y(Y_RETRAIDO)
        if not self.abierto or self.celda is None or self.p._sobre_sujeto is not None:
            raise RuntimeError('Despacho incompleto')
        actual = buscar_qr(self.qr)
        if actual is None or int(actual['indice']) != int(self.celda['indice']):
            raise RuntimeError('Inventario cambió durante la recuperación')
        liberar_celda(int(self.celda['indice']))
        self.salida.iniciar(self.sobre_despachado)
        # El PLC actualiza su tabla al recibir esta confirmación; JSON ya guardado.
        self.c.plc.escribir_bool('Deposito_Confirmado', True)
        print('RECUPERACIÓN CONFIRMADA:', self.qr, 'en segunda banda')
        self.celda = self.qr = None
        self.abierto = self.extraido = False
        self.sobre_despachado = None

    def ordenes(self, a, flanco):
        if flanco('Ir_Recogida'):
            raise RuntimeError('Ir_Recogida no corresponde a recuperación')
        if flanco('Ir_Almacenamiento'): self.ir_al_origen()
        if flanco('Ir_Despacho'): self.ir_a_despacho()
        if flanco('Y_Extender'): self.p.extender_y()
        if flanco('Y_Retraer'): self.retraer()
        if flanco('Z_Ajuste'): self.ajustar()
        if flanco('G_Ajustar'): self.sujetar()
        if flanco('G_Soltar'): self.soltar()
