"""Lector QR integrado: función bloqueante que devuelve un QR nuevo."""
import cv2
import numpy as np
import time
from typing import Protocol

class SensorVisionSim(Protocol):
    """Parte de la API remota que necesita el lector QR."""

    def getVisionSensorImg(self, sensor_handle: int) -> tuple[bytes, list[int]]: ...


def leer_qr(sim: SensorVisionSim, sensor_handle: int, timeout: float | None = None,
            mostrar_ventana: bool = True) -> str | None:
    detector = cv2.QRCodeDetector()
    inicio = time.monotonic()
    ultimo_qr = None
    frames_sin_qr = 0
    print("Esperando QR en el sensor...")

    while timeout is None or time.monotonic() - inicio < timeout:
        imagen_bytes, resolucion = sim.getVisionSensorImg(sensor_handle)
        imagen = np.frombuffer(imagen_bytes, dtype=np.uint8).reshape(resolucion[1], resolucion[0], 3)
        imagen = cv2.flip(imagen, 0)
        contenido, esquinas, _ = detector.detectAndDecode(imagen)

        if contenido:
            frames_sin_qr = 0
            if mostrar_ventana:
                if esquinas is not None:
                    puntos = esquinas.astype(int).reshape(-1, 2)
                    for i in range(len(puntos)):
                        cv2.line(imagen, tuple(puntos[i]), tuple(puntos[(i + 1) % len(puntos)]), (0, 255, 0), 2)
                cv2.putText(imagen, "QR: " + contenido, (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
                cv2.imshow("Sensor QR - Mini AS/RS", imagen)
                cv2.waitKey(1)
            if contenido != ultimo_qr:
                print("QR LEÍDO:", contenido)
                return contenido
        else:
            frames_sin_qr += 1
            if mostrar_ventana:
                cv2.putText(imagen, "Buscando QR...", (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
                cv2.imshow("Sensor QR - Mini AS/RS", imagen)
                cv2.waitKey(1)
            if frames_sin_qr >= 5:
                ultimo_qr = None
        time.sleep(0.01)

    return None
