"""Banda de entrada integrada al coordinador Mini AS/RS."""
import time
from pathlib import Path
import qrcode
from config import DATOS_DIR
from inventario import validar_inventario

RUTA_BANDA = "/Prismatico_Banda_Entrada"
RUTA_PLANTILLA = "/Sobre_000"
POSICION_INICIAL = [-0.7786352577209473, 2.150, 0.32950]
POSICION_FINAL = [-0.7786352577209473, 0.775, 0.32950]
ORIENTACION = [0.5, 0.5, 0.5, -0.5]
DURACION_TRANSPORTE = 4.0  # segundos; recorrido interpolado de la prueba funcional
TOL_INICIO = 0.005
TOL_FINAL = 0.002
MARGEN_LIMITE = 0.001
CARPETA_QR = DATOS_DIR / "QR_Mini_ASRS"

class ControlBanda:
    def __init__(self, sim, plc):
        self.sim, self.plc = sim, plc
        self.joint = sim.getObject(RUTA_BANDA)
        self.sobre = None
        self.qr = None
        self._banda_anterior = plc.leer_bool("BANDA_ENTRADA")
        self._crear_anterior = plc.leer_bool("Crear_Sobre")
        self._terminado_anterior = plc.leer_bool("Ciclo_Terminado")
        self._moviendo = False
        self._transporte_inicio = None
        self._posicion_inicio_transporte = None
        self._cargar_sobre_en_inicio()
        print("ControlBanda integrado preparado.")

    def _try_object(self, path):
        h = self.sim.getObject(path, {'noError': True})
        return None if h == -1 else h

    def _cargar_sobre_en_inicio(self):
        """Recupera un sobre existente tanto en el inicio como en la estación final."""
        candidatos = []
        # Los sobres se numeran de forma consecutiva; se detiene tras 3 números libres seguidos.
        n, libres = 0, 0
        while libres < 3 and n < 999:
            n += 1
            h = self._try_object(f"/Sobre_{n:03d}")
            if h is None:
                libres += 1
                continue
            libres = 0
            pos = self.sim.getObjectPosition(h, -1)
            en_inicio = all(abs(pos[i] - POSICION_INICIAL[i]) <= TOL_INICIO for i in range(3))
            en_final = all(abs(pos[i] - POSICION_FINAL[i]) <= TOL_FINAL for i in range(3))
            if en_inicio or en_final:
                candidatos.append((n, h, en_inicio, en_final))
        if candidatos:
            n, self.sobre, en_inicio, en_final = min(candidatos, key=lambda x: x[0])
            self.qr = f"AS-{n:03d}"
            self._publicar_posiciones(self.sim.getObjectPosition(self.sobre, -1))
            ubicacion = "entrada" if en_inicio else "estación de recogida"
            print(f"Sobre existente detectado: Sobre_{n:03d}, QR={self.qr}, ubicación={ubicacion}")

    def _siguiente_numero(self):
        registrados = {
            int(qr[3:])
            for celda in validar_inventario()['celdas']
            for qr in [str(celda.get('qr') or '').strip()]
            if qr.startswith('AS-') and qr[3:].isdigit()
        }
        n = 1
        while n in registrados or self._try_object(f"/Sobre_{n:03d}") is not None:
            n += 1
        return n

    def crear_sobre(self):
        if self.sobre is not None:
            print("No se genera otro sobre: hay uno pendiente en la banda.")
            return False
        plantilla = self.sim.getObject(RUTA_PLANTILLA)
        n = self._siguiente_numero()
        qr_texto = f"AS-{n:03d}"
        CARPETA_QR.mkdir(parents=True, exist_ok=True)
        archivo = CARPETA_QR / f"{qr_texto}.png"
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=12, border=4)
        qr.add_data(qr_texto)
        qr.make(fit=True)
        qr.make_image(fill_color="black", back_color="white").convert("RGB").save(archivo)

        arbol = self.sim.getObjectsInTree(plantilla, self.sim.handle_all, 0)
        copias = self.sim.copyPasteObjects(arbol, 0)
        if len(copias) != len(arbol):
            raise RuntimeError("No coincide la jerarquía copiada desde Sobre_000.")
        mapeo = dict(zip(arbol, copias))
        for orig, copia in mapeo.items():
            padre = self.sim.getObjectParent(orig)
            if padre in mapeo:
                self.sim.setObjectParent(copia, mapeo[padre], True)

        etiqueta = None
        for orig, copia in mapeo.items():
            alias = self.sim.getObjectAlias(orig)
            if orig == plantilla:
                self.sim.setObjectAlias(copia, f"Sobre_{n:03d}")
            elif "Final_Sobre" in alias:
                self.sim.setObjectAlias(copia, f"Final_Sobre{n:03d}")
            elif "Codigo_QR" in alias:
                self.sim.setObjectAlias(copia, f"Codigo_QR{n:03d}")
                etiqueta = copia
        if etiqueta is None:
            raise RuntimeError("Sobre_000 no contiene una geometría Codigo_QR.")

        self.sim.setObjectParent(mapeo[plantilla], self.sim.getObject(RUTA_BANDA), True)
        self.sim.setObjectPosition(mapeo[plantilla], self.sim.handle_world, POSICION_INICIAL)
        self.sim.setObjectQuaternion(mapeo[plantilla], self.sim.handle_world, ORIENTACION)
        plano, textura, resolucion = self.sim.createTexture(str(archivo), 3, [0.1, 0.1])
        self.sim.setShapeTexture(etiqueta, textura, self.sim.texturemap_plane, 3, [0.1, 0.1])

        self.sobre, self.qr = mapeo[plantilla], qr_texto
        self._publicar_posiciones(self.sim.getObjectPosition(self.sobre, -1))
        self.plc.escribir_string("NUM_QR", qr_texto)
        self.plc.escribir_bool("QR_DETECTADO", False)
        print(f"Sobre creado: Sobre_{n:03d}; QR={qr_texto}; textura={resolucion}")
        return True

    def _publicar_posiciones(self, pos):
        en_inicio = all(abs(pos[i]-POSICION_INICIAL[i]) <= TOL_INICIO for i in range(3))
        en_final = all(abs(pos[i]-POSICION_FINAL[i]) <= TOL_FINAL for i in range(3))
        self.plc.escribir_bool("SOBRE_POSIN", bool(en_inicio))
        self.plc.escribir_bool("SOBRE_POS_FINAL", bool(en_final))

    def _paso_transporte(self):
        """Transporta el objeto con interpolación temporal; no mueve el joint de banda.

        El objeto se mueve directamente en coordenadas globales, igual que en la
        prueba independiente que ya funcionó. Así se evita aplicar el mismo avance
        a la vez a la articulación y al objeto.
        """
        if self.sobre is None:
            raise RuntimeError("BANDA_ENTRADA activa sin sobre generado.")
        if self._transporte_inicio is None or self._posicion_inicio_transporte is None:
            self._transporte_inicio = time.monotonic()
            self._posicion_inicio_transporte = list(
                self.sim.getObjectPosition(self.sobre, -1)
            )

        transcurrido = time.monotonic() - self._transporte_inicio
        progreso = min(1.0, transcurrido / DURACION_TRANSPORTE)
        inicio = self._posicion_inicio_transporte
        destino = POSICION_FINAL
        posicion = [
            inicio[i] + (destino[i] - inicio[i]) * progreso
            for i in range(3)
        ]
        self.sim.setObjectPosition(self.sobre, self.sim.handle_world, posicion)
        posicion_real = self.sim.getObjectPosition(self.sobre, -1)
        self._publicar_posiciones(posicion_real)

        if progreso >= 1.0:
            self._moviendo = False
            self._transporte_inicio = None
            self._posicion_inicio_transporte = None
            self.plc.escribir_bool("SOBRE_POSIN", False)
            self.plc.escribir_bool("SOBRE_POS_FINAL", True)
            print(f"Sobre alcanzó estación de recogida: {posicion_real}")

    def ciclo(self):
        banda = self.plc.leer_bool("BANDA_ENTRADA")
        crear = self.plc.leer_bool("Crear_Sobre")
        terminado = self.plc.leer_bool("Ciclo_Terminado")

        if terminado and not self._terminado_anterior:
            self.sobre = None
            self.qr = None
            self._moviendo = False
            self._transporte_inicio = None
            self._posicion_inicio_transporte = None
            self.plc.escribir_bool("SOBRE_POSIN", False)
            self.plc.escribir_bool("SOBRE_POS_FINAL", False)
            self.plc.escribir_bool("QR_DETECTADO", False)

        # Al arrancar, preparar un primer sobre solo si START está activo.
        if (self.sobre is None and self.plc.leer_bool("Maquina_Lista")
                and self.plc.leer_bool("CICLO_AUTO")
                and int(self.plc.leer("Paso_Actual")) == 10 and crear):
            self.crear_sobre()

        if banda and not self._banda_anterior:
            if self.sobre is None:
                raise RuntimeError("BANDA_ENTRADA activada sin sobre en la banda.")
            pos = self.sim.getObjectPosition(self.sobre, -1)
            if abs(pos[1] - POSICION_FINAL[1]) <= TOL_FINAL:
                # Ya está en la estación: no volver a transportar el mismo sobre.
                self._publicar_posiciones(pos)
                self._moviendo = False
            else:
                if not self._moviendo and abs(pos[1] - POSICION_INICIAL[1]) > TOL_INICIO:
                    raise RuntimeError(f"Sobre fuera de posición inicial al arrancar banda: {pos}")
                self._moviendo = True
                self._transporte_inicio = time.monotonic()
                self._posicion_inicio_transporte = list(pos)

        if not banda and self._moviendo:
            # OFF pausa el sobre en su posicion; ON continua desde ahi.
            self._transporte_inicio = None
            self._posicion_inicio_transporte = None

        if banda and self._moviendo:
            self._paso_transporte()

        self._banda_anterior, self._crear_anterior, self._terminado_anterior = banda, crear, terminado
