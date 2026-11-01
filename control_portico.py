"""Control de los ejes X/Y/Z del Mini AS/RS.

Este módulo reutiliza las conexiones ya abiertas:
    portico = ControlPortico(sim, plc)

No abre conexiones propias ni escribe señales IN/OUT de FluidSIM.
Las coordenadas X/Z se leen del Excel de coordenadas del rack.
Las órdenes de CODESYS se atienden por flanco ascendente para evitar
repetir un movimiento mientras una orden permanezca activa.

IMPORTANTE:
- Las posiciones X/Z del Excel son posiciones ARTICULARES, comprobadas
  por el usuario en CoppeliaSim.
- Los movimientos de ciclo siguen un perfil trapezoidal (o triangular si no
  se alcanza la velocidad máxima) por eje, con velocidad y aceleración de
  config.py. No bloquean: el coordinador llama avanzar() en cada vuelta, así
  el heartbeat sigue publicándose, y la confirmación se emite al terminar.
- Antes de habilitar el ciclo completo, debe verificarse el orden de
  celdas del inventario y probarse cada movimiento con el área despejada.
"""

from pathlib import Path
import math
import time

from config import (PROJECT_DIR, VEL_MAX_X, VEL_MAX_Y, VEL_MAX_Z, ACEL_X, ACEL_Y, ACEL_Z,
                    T_MOVIMIENTO_PLC, T_ACTUADOR_PLC, MARGEN_SUPERVISION)

# Rutas confirmadas en los scripts de prueba del proyecto.
RUTA_X = "/Portico/Prismatico_Z/Carro_en_Z/Prismatico_X"
RUTA_Y = "/Portico/Prismatico_Z/Carro_en_Z/Prismatico_X/Prismatico_Y"
RUTA_Z = "/Portico/Prismatico_Z"

# Límites Y medidos en CoppeliaSim.
Y_RETRAIDO = -0.050
Y_EXTENDIDO = 0.280

# Tolerancias para comprobar posiciones articulares.
TOLERANCIA_XZ = 0.002
TOLERANCIA_Y = 0.002

# El Excel compartido por el usuario.
ARCHIVO_COORDENADAS = PROJECT_DIR / "Coordenadas_Rack_Mini_ASRS.xlsx"

# Hoja de cálculo: columnas A:I = salida/depósito; K:S = entrada con sobre.
FILA_INICIO_EXCEL = 4
NUM_FILAS = 28
NUM_COLUMNAS = 4
COL_INICIO_SALIDA = 2      # B; pares X/Z: B:C, D:E, F:G, H:I
COL_INICIO_ENTRADA = 12    # L; pares X/Z: L:M, N:O, P:Q, R:S


def duracion_perfil(distancia, v_max, a_max):
    """Duración (s) del perfil trapezoidal, o triangular si d < v²/a."""
    d = abs(distancia)
    if d * a_max < v_max ** 2:
        return 2 * math.sqrt(d / a_max)
    return d / v_max + v_max / a_max


def avance_perfil(t, distancia, v_max, a_max):
    """Desplazamiento con signo recorrido en el instante t del perfil."""
    d = abs(distancia)
    t_total = duracion_perfil(d, v_max, a_max)
    if t >= t_total:
        return distancia
    if t <= 0:
        return 0.0
    v = min(v_max, math.sqrt(d * a_max))
    t_acel = v / a_max
    if t < t_acel:
        s = 0.5 * a_max * t * t
    elif t <= t_total - t_acel:
        s = 0.5 * v * t_acel + v * (t - t_acel)
    else:
        s = d - 0.5 * a_max * (t_total - t) ** 2
    return math.copysign(s, distancia)


class ControlPortico:
    """Controla X/Y/Z con una API sim y una instancia OPC UA compartidas."""
    
    
    def sujetar_sobre(self, ruta_sobre):
        """Asocia el sobre al gripper conservando su posición mundial."""
        if getattr(self, "_sobre_sujeto", None) is not None:
            raise RuntimeError("Ya hay un sobre asociado al gripper.")

        sobre = self.sim.getObject(ruta_sobre)
        gripper = self.sim.getObject(
            "/Portico/Prismatico_Z/Carro_en_Z/"
            "Prismatico_X/Prismatico_Y/Gripper"
        )

        self.sim.setObjectParent(sobre, gripper, True)
        self._sobre_sujeto = sobre
        print(f"Sobre sujeto al gripper: {ruta_sobre}")

    def liberar_sobre(self):
        """Libera el sobre conservando su posición mundial."""
        sobre = getattr(self, "_sobre_sujeto", None)
        if sobre is None:
            raise RuntimeError("No hay ningún sobre sujeto al gripper.")

        self.sim.setObjectParent(sobre, -1, True)
        self._sobre_sujeto = None
        print("Sobre liberado en su posición actual.")


    def __init__(
        self,
        sim,
        plc,
        archivo_coordenadas=ARCHIVO_COORDENADAS,
        tolerancia_xz=TOLERANCIA_XZ,
        tolerancia_y=TOLERANCIA_Y,
    ):
        if sim is None:
            raise ValueError("Se necesita la API sim de CoppeliaSim.")
        if plc is None or not getattr(plc, "conectado", False):
            raise ValueError("Se necesita una conexión OPC UA activa (plc).")

        self.sim = sim
        self.plc = plc
        self.tolerancia_xz = float(tolerancia_xz)
        self.tolerancia_y = float(tolerancia_y)

        self.joint_x = sim.getObject(RUTA_X)
        self.joint_y = sim.getObject(RUTA_Y)
        self.joint_z = sim.getObject(RUTA_Z)

        self.coordenadas = self._cargar_coordenadas(Path(archivo_coordenadas))
        self.celda_objetivo = None
        self._sobre_sujeto = None
        self._cinematica = {
            self.joint_x: (VEL_MAX_X, ACEL_X),
            self.joint_y: (VEL_MAX_Y, ACEL_Y),
            self.joint_z: (VEL_MAX_Z, ACEL_Z),
        }
        self._segmentos = []
        self._t_inicio = None
        self._pos_inicio = None
        self._al_terminar = None
        self.orden_activa = None

        # No ejecutar órdenes que ya estaban activas al crear el controlador.
        self._ordenes_anteriores = {
            nombre: self.plc.leer_bool(nombre)
            for nombre in ("Ir_Recogida", "Ir_Almacenamiento", "Y_Extender", "Y_Retraer", "Z_Ajuste")
        }

        self._publicar_posiciones_hmi()
        self._publicar_estado_y_si_conocido()
        print(
            f"ControlPortico preparado. X={self.posicion_x():.4f} m, "
            f"Y={self.posicion_y():.4f} m, Z={self.posicion_z():.4f} m."
        )
        print(f"Coordenadas cargadas: {len(self.coordenadas)} celdas.")

    @staticmethod
    def _cargar_coordenadas(archivo):
        """Carga las 112 celdas sin asumir un orden de inventario por índice."""
        if not archivo.is_file():
            raise FileNotFoundError(
                f"No se encontró el Excel de coordenadas: {archivo}"
            )

        try:
            from openpyxl import load_workbook
        except ImportError as exc:
            raise RuntimeError(
                "Falta openpyxl. Instálalo en el entorno virtual con: "
                "python -m pip install openpyxl"
            ) from exc

        libro = load_workbook(archivo, read_only=True, data_only=True)
        try:
            hoja = libro.active
            resultado = {}

            for fila in range(1, NUM_FILAS + 1):
                fila_excel = FILA_INICIO_EXCEL + fila - 1
                for columna in range(1, NUM_COLUMNAS + 1):
                    col_x_salida = COL_INICIO_SALIDA + (columna - 1) * 2
                    col_x_entrada = COL_INICIO_ENTRADA + (columna - 1) * 2

                    x_salida = hoja.cell(fila_excel, col_x_salida).value
                    z_salida = hoja.cell(fila_excel, col_x_salida + 1).value
                    x_entrada = hoja.cell(fila_excel, col_x_entrada).value
                    z_entrada = hoja.cell(fila_excel, col_x_entrada + 1).value

                    valores = (x_salida, z_salida, x_entrada, z_entrada)
                    if any(v is None for v in valores):
                        raise ValueError(
                            f"Faltan coordenadas en fila {fila}, columna {columna} "
                            f"(fila Excel {fila_excel})."
                        )

                    x_salida, z_salida, x_entrada, z_entrada = map(float, valores)
                    if abs(x_salida - x_entrada) > 0.0005:
                        raise ValueError(
                            f"X de entrada/salida no coincide en fila {fila}, "
                            f"columna {columna}: {x_entrada} vs {x_salida}."
                        )

                    resultado[(columna, fila)] = {
                        "columna": columna,
                        "fila": fila,
                        "x": x_entrada,
                        "z_entrada": z_entrada,
                        "z_salida": z_salida,
                    }

            return resultado
        finally:
            libro.close()

    def posicion_x(self):
        return float(self.sim.getJointPosition(self.joint_x))

    def posicion_y(self):
        return float(self.sim.getJointPosition(self.joint_y))

    def posicion_z(self):
        return float(self.sim.getJointPosition(self.joint_z))

    def _publicar_posiciones_hmi(self):
        self.plc.actualizar_posicion_x(self.posicion_x())
        self.plc.actualizar_posicion_z(self.posicion_z())

    def _publicar_estado_y_si_conocido(self):
        y = self.posicion_y()
        if abs(y - Y_RETRAIDO) <= self.tolerancia_y:
            self.plc.actualizar_estado_y(False)
        elif abs(y - Y_EXTENDIDO) <= self.tolerancia_y:
            self.plc.actualizar_estado_y(True)
        # Si Y está entre extremos, no declarar retraído ni extendido.

    def _mover_joint_y_verificar(self, joint, objetivo, tolerancia, nombre):
        self.sim.setJointPosition(joint, float(objetivo))
        real = float(self.sim.getJointPosition(joint))
        if abs(real - objetivo) > tolerancia:
            raise RuntimeError(
                f"{nombre} no alcanzó el objetivo: esperado {objetivo:.4f} m, "
                f"leído {real:.4f} m."
            )
        return real

    def en_movimiento(self):
        return bool(self._segmentos)

    def _iniciar(self, segmentos, limite_plc, al_terminar, orden):
        """Encola ejes secuenciales [(joint, objetivo, tolerancia, nombre)] sin bloquear."""
        if self.en_movimiento():
            raise RuntimeError(f"{segmentos[0][3]}: ya hay un movimiento en curso.")
        posiciones = {}
        total = 0.0
        for joint, objetivo, _, _ in segmentos:
            inicio = posiciones.get(joint, float(self.sim.getJointPosition(joint)))
            total += duracion_perfil(objetivo - inicio, *self._cinematica[joint])
            posiciones[joint] = objetivo
        if total > MARGEN_SUPERVISION * limite_plc:
            raise RuntimeError(f"{segmentos[0][3]}: {total:.1f} s supera la supervisión del PLC.")
        self._segmentos = [(j, float(o), t, n) for j, o, t, n in segmentos]
        self._al_terminar = al_terminar
        self.orden_activa = orden
        self.avanzar()

    def avanzar(self):
        """Avanza la interpolación según el tiempo real; confirma al terminar el último eje."""
        while self._segmentos:
            joint, objetivo, tolerancia, nombre = self._segmentos[0]
            if self._t_inicio is None:
                self._t_inicio = time.monotonic()
                self._pos_inicio = float(self.sim.getJointPosition(joint))
            v_max, a_max = self._cinematica[joint]
            distancia = objetivo - self._pos_inicio
            t = time.monotonic() - self._t_inicio
            if t < duracion_perfil(distancia, v_max, a_max):
                self.sim.setJointPosition(joint, self._pos_inicio + avance_perfil(t, distancia, v_max, a_max))
                return
            self._t_inicio = None
            self._mover_joint_y_verificar(joint, objetivo, tolerancia, nombre)
            self._segmentos.pop(0)
        al_terminar = self._al_terminar
        self._al_terminar = self.orden_activa = None
        if al_terminar is not None:
            al_terminar()

    def detener(self):
        """Detiene el movimiento en la posición actual sin confirmar."""
        self._segmentos = []
        self._t_inicio = self._al_terminar = self.orden_activa = None

    def mover_xz(self, x, z):
        """Mueve X y Z a posiciones articulares conocidas y verifica ambas."""
        x = float(x)
        z = float(z)

        # No se han definido límites seguros completos para X/Z todavía;
        # por ello se verifica que el joint acepte la posición y se lee el resultado.
        self._mover_joint_y_verificar(
            self.joint_x, x, self.tolerancia_xz, "Eje X"
        )
        self._mover_joint_y_verificar(
            self.joint_z, z, self.tolerancia_xz, "Eje Z"
        )
        self._publicar_posiciones_hmi()

    def _mover_y(self, extendido, al_terminar, orden):
        def fin():
            self.plc.actualizar_estado_y(extendido)
            if al_terminar:
                al_terminar()
        objetivo = Y_EXTENDIDO if extendido else Y_RETRAIDO
        self._iniciar([(self.joint_y, objetivo, self.tolerancia_y, "Eje Y")], T_ACTUADOR_PLC, fin, orden)

    def extender_y(self, al_terminar=None, orden="Y_Extender"):
        """Extiende Y y publica TRUE al verificar el extremo."""
        self._mover_y(True, al_terminar, orden)

    def retraer_y(self, al_terminar=None, orden="Y_Retraer"):
        """Retrae Y y publica FALSE al verificar el extremo."""
        self._mover_y(False, al_terminar, orden)

    def seleccionar_celda(self, columna, fila):
        """Define la celda destino (columna 1..4, fila 1..28)."""
        clave = (int(columna), int(fila))
        if clave not in self.coordenadas:
            raise ValueError(
                f"Celda fuera de rango: columna={columna}, fila={fila}."
            )
        self.celda_objetivo = self.coordenadas[clave]
        print(
            "Celda objetivo seleccionada: "
            f"columna {columna}, fila {fila}; "
            f"X={self.celda_objetivo['x']:.3f}, "
            f"Z entrada={self.celda_objetivo['z_entrada']:.3f}, "
            f"Z salida={self.celda_objetivo['z_salida']:.3f}."
        )
        return dict(self.celda_objetivo)

    def ir_a_recogida(self, x=-0.262, z=-0.675, al_terminar=None):
        """Va a recogida: primero alinea X y después baja Z."""
        if abs(self.posicion_y() - Y_RETRAIDO) > self.tolerancia_y:
            raise RuntimeError(
                "No se puede desplazar a recogida: Y no está retraído."
            )
        # Recorrido más seguro: trasladarse en X con Z en su altura actual,
        # después bajar Z hasta la estación de recogida.
        def fin():
            self._publicar_posiciones_hmi()
            if al_terminar:
                al_terminar()
        self._iniciar([
            (self.joint_x, x, self.tolerancia_xz, "Eje X hacia recogida"),
            (self.joint_z, z, self.tolerancia_xz, "Eje Z hacia recogida"),
        ], T_MOVIMIENTO_PLC, fin, "Ir_Recogida")

    def ir_a_entrada_celda(self):
        """Va a entrada de celda elevando Z antes de desplazarse en X."""
        if self.celda_objetivo is None:
            raise RuntimeError(
                "No se ha seleccionado una celda. Llama seleccionar_celda(columna, fila)."
            )
        if abs(self.posicion_y() - Y_RETRAIDO) > self.tolerancia_y:
            raise RuntimeError("Y debe estar retraído antes de desplazar X/Z al rack.")

        def fin():
            self._publicar_posiciones_hmi()
            self.plc.escribir_bool("Pos_Almacenamiento", True)
        # Primero elevar Z a la altura de entrada; luego trasladar X sobre el rack.
        self._iniciar([
            (self.joint_z, self.celda_objetivo["z_entrada"], self.tolerancia_xz, "Eje Z hacia entrada de celda"),
            (self.joint_x, self.celda_objetivo["x"], self.tolerancia_xz, "Eje X hacia celda"),
        ], T_MOVIMIENTO_PLC, fin, "Ir_Almacenamiento")

    def ajustar_z_a_salida(self):
        """Baja Z desde Entrada con sobre hasta Salida para apoyar el sobre."""
        if self.celda_objetivo is None:
            raise RuntimeError("No se ha seleccionado una celda de almacenamiento.")

        def fin():
            self._publicar_posiciones_hmi()
            self.plc.escribir_bool("Z_Ajuste_Terminado", True)
        self._iniciar([(self.joint_z, self.celda_objetivo["z_salida"], self.tolerancia_xz, "Ajuste Z")],
                      T_MOVIMIENTO_PLC, fin, "Z_Ajuste")

    def procesar_ordenes(self):
        """Procesa flancos ascendentes de las órdenes de movimiento de CODESYS.

        Nota: el ciclo de almacenamiento debe seleccionar la celda destino
        antes de que CODESYS active Ir_Almacenamiento.
        """
        ordenes = {
            nombre: self.plc.leer_bool(nombre)
            for nombre in self._ordenes_anteriores
        }

        self.avanzar()
        self._publicar_posiciones_hmi()

        # Priorizar un conflicto seguro: no ejecutar órdenes opuestas a la vez.
        if ordenes["Y_Extender"] and ordenes["Y_Retraer"]:
            print("ERROR: Y_Extender y Y_Retraer activas simultáneamente; no se mueve Y.")
        else:
            if ordenes["Ir_Recogida"] and not self._ordenes_anteriores["Ir_Recogida"]:
                self.ir_a_recogida()

            if ordenes["Ir_Almacenamiento"] and not self._ordenes_anteriores["Ir_Almacenamiento"]:
                self.ir_a_entrada_celda()

            if ordenes["Y_Extender"] and not self._ordenes_anteriores["Y_Extender"]:
                self.extender_y()

            if ordenes["Y_Retraer"] and not self._ordenes_anteriores["Y_Retraer"]:
                self.retraer_y()

            if ordenes["Z_Ajuste"] and not self._ordenes_anteriores["Z_Ajuste"]:
                self.ajustar_z_a_salida()

        # Retirar confirmaciones cuando el PLC retire la orden correspondiente.
        # Esto permite que las señales de confirmación vuelvan a FALSE para el
        # siguiente ciclo, sin escribir IN/OUT de FluidSIM.
        if not ordenes["Ir_Recogida"]:
            self.plc.escribir_bool("Pos_Recogida", False)
        if not ordenes["Ir_Almacenamiento"]:
            self.plc.escribir_bool("Pos_Almacenamiento", False)
        if not ordenes["Z_Ajuste"]:
            self.plc.escribir_bool("Z_Ajuste_Terminado", False)

        self._ordenes_anteriores = ordenes

    def cerrar_ciclo_confirmaciones(self):
        """Retira las confirmaciones cuyo comando asociado ya fue retirado."""
        if not self.plc.leer_bool("Ir_Recogida"):
            self.plc.escribir_bool("Pos_Recogida", False)
        if not self.plc.leer_bool("Ir_Almacenamiento"):
            self.plc.escribir_bool("Pos_Almacenamiento", False)
        if not self.plc.leer_bool("Z_Ajuste"):
            self.plc.escribir_bool("Z_Ajuste_Terminado", False)
