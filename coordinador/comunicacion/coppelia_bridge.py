"""Conexión compartida con CoppeliaSim. No mueve ejes todavía."""
import time
from coppeliasim_zmqremoteapi_client import RemoteAPIClient

class CoppeliaBridge:
    def __init__(self):
        self.client = None
        self.sim = None
        self.sensor_qr = None

    def conectar(self, sensor_alias="/Sensor_QR"):
        self.client = RemoteAPIClient()
        self.sim = self.client.require("sim")
        self.sensor_qr = self.sim.getObject(sensor_alias)
        if self.sim.getSimulationState() == self.sim.simulation_stopped:
            self.sim.startSimulation()
            limite = time.monotonic() + 5.0
            while self.sim.getSimulationState() == self.sim.simulation_stopped:
                if time.monotonic() > limite:
                    raise RuntimeError("CoppeliaSim no inició la simulación.")
                time.sleep(0.05)
            print("Simulación de CoppeliaSim iniciada automáticamente.")
        print("Conexión con CoppeliaSim establecida.")
        return self

    def cerrar(self):
        # El cliente ZMQ no requiere el mismo cierre que OPC UA.
        self.client = None
        self.sim = None
        self.sensor_qr = None
