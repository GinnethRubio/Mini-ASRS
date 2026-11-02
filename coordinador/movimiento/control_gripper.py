"""Control reutilizable del gripper para el coordinador Mini AS/RS.

No abre conexiones propias en la integración principal. Recibe la API `sim`
ya conectada y la instancia compartida `plc` de CodesysOPCUA.
"""

from config import POLL_INTERVAL

RUTA_GRIPPER = (
    "/Portico/Prismatico_Z/Carro_en_Z/"
    "Prismatico_X/Prismatico_Y/Gripper/"
    "Prismatico_Sujetador"
)

POSICION_AJUSTAR = -0.015  # Cerrado / sujetar
POSICION_SOLTAR = 0.000    # Abierto / liberar
TOLERANCIA = 0.001


class ControlGripper:
    """Ejecuta órdenes del gripper usando conexiones compartidas."""

    def __init__(self, sim, plc, ruta_joint=RUTA_GRIPPER):
        if sim is None:
            raise ValueError("Se necesita la API sim de CoppeliaSim.")
        if plc is None or not getattr(plc, "conectado", False):
            raise ValueError("Se necesita una conexión OPC UA activa (plc).")

        self.sim = sim
        self.plc = plc
        self.joint = sim.getObject(ruta_joint)

        # No actuar sobre órdenes que ya estaban activas al iniciar.
        self._ajustar_anterior = plc.leer_bool("G_Ajustar")
        self._soltar_anterior = plc.leer_bool("G_Soltar")
        self._conflicto_anterior = False

        # Publicar estado inicial solo si está en uno de los extremos conocidos.
        self._publicar_estado_si_conocido(sim.getJointPosition(self.joint))
        print("ControlGripper preparado. Joint:", self.joint)
        print("Posición inicial:", sim.getJointPosition(self.joint))

    def _publicar_estado_si_conocido(self, posicion):
        if abs(posicion - POSICION_SOLTAR) <= TOLERANCIA:
            self.plc.actualizar_estado_gripper(cerrado=False)
        elif abs(posicion - POSICION_AJUSTAR) <= TOLERANCIA:
            self.plc.actualizar_estado_gripper(cerrado=True)
        # Si está entre extremos, no escribir un estado que no esté confirmado.

    def mover(self, cerrado):
        """Mueve el joint, verifica el extremo y publica GRIPPER_ESTADO.

        cerrado=True: posición de G_Ajustar (-0.015 m).
        cerrado=False: posición de G_Soltar (0.000 m).
        Devuelve True únicamente si la posición objetivo fue verificada.
        """
        posicion_objetivo = POSICION_AJUSTAR if cerrado else POSICION_SOLTAR
        descripcion = "cerrar/sujetar" if cerrado else "abrir/liberar"
        print(f"Gripper: orden de {descripcion}.")

        self.sim.setJointPosition(self.joint, posicion_objetivo)
        posicion_real = self.sim.getJointPosition(self.joint)
        print(
            f"Gripper: objetivo={posicion_objetivo:.3f} m, "
            f"posición leída={posicion_real:.3f} m"
        )

        if abs(posicion_real - posicion_objetivo) > TOLERANCIA:
            print("ADVERTENCIA: el gripper no alcanzó la posición objetivo.")
            return False

        self.plc.actualizar_estado_gripper(cerrado=cerrado)
        print("Gripper confirmado:", "cerrado" if cerrado else "abierto")
        return True

    def procesar_ordenes(self):
        """Procesa flancos de G_Ajustar/G_Soltar desde el bucle del coordinador.

        Si ambas órdenes están activas a la vez, no se ejecuta movimiento.
        Devuelve True si una orden produjo una acción confirmada.
        """
        ajustar = self.plc.leer_bool("G_Ajustar")
        soltar = self.plc.leer_bool("G_Soltar")
        hubo_accion = False

        if ajustar and soltar:
            if not self._conflicto_anterior:
                print(
                    "ADVERTENCIA: G_Ajustar y G_Soltar están activas "
                    "simultáneamente. No se moverá el gripper."
                )
            self._conflicto_anterior = True
        else:
            self._conflicto_anterior = False
            if ajustar and not self._ajustar_anterior:
                hubo_accion = self.mover(cerrado=True)
            elif soltar and not self._soltar_anterior:
                hubo_accion = self.mover(cerrado=False)

        self._ajustar_anterior = ajustar
        self._soltar_anterior = soltar
        return hubo_accion


def mover_gripper(sim, plc, cerrado):
    """Ayudante para una acción puntual desde el coordinador."""
    controlador = ControlGripper(sim, plc)
    return controlador.mover(cerrado=cerrado)


def main():
    """Modo de prueba independiente; la integración debe compartir conexiones."""
    import time
    from coppeliasim_zmqremoteapi_client import RemoteAPIClient
    from codesys_opcua import CodesysOPCUA

    plc = None
    try:
        plc = CodesysOPCUA().conectar()
        if plc is None:
            print("No fue posible conectar con CODESYS.")
            return

        client_sim = RemoteAPIClient()
        sim = client_sim.require("sim")
        gripper = ControlGripper(sim, plc)
        print("Prueba independiente del gripper iniciada. Ctrl+C para salir.")

        while True:
            gripper.procesar_ordenes()
            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\nPrueba del gripper detenida.")
    except Exception as exc:
        print(f"Error en la prueba del gripper: {exc}")
    finally:
        if plc is not None:
            plc.desconectar()


if __name__ == "__main__":
    main()
