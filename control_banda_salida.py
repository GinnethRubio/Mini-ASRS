"""Transporte progresivo por la segunda banda, sin bloquear el coordinador."""
import time

# Sentido opuesto al de entrada: desde el rack hacia el extremo posterior.
Y_FINAL_SALIDA = 2.150
DURACION_SALIDA = 4.0
X_SALIDA = -0.7786352577209473 + .525
Z_SALIDA = .32950
Y_INICIO_SALIDA = .775


class ControlBandaSalida:
    def __init__(self, sim):
        self.sim = sim
        self.sobre = None
        self.inicio = None
        self.transcurrido = 0.0
        self.ultimo = None

    def buscar_pendiente(self):
        """Retoma un sobre que quedó en la segunda banda antes del reinicio."""
        candidatos = []
        for h in self.sim.getObjectsInTree(self.sim.handle_scene, self.sim.handle_all, 0):
            alias = self.sim.getObjectAlias(h)
            if not alias.startswith('Sobre_') or alias == 'Sobre_000':
                continue
            if self.sim.getObjectParent(h) != -1:
                continue
            pos = self.sim.getObjectPosition(h, -1)
            if (abs(pos[0] - X_SALIDA) <= .015 and abs(pos[2] - Z_SALIDA) <= .015
                    and Y_INICIO_SALIDA - .015 <= pos[1] <= Y_FINAL_SALIDA):
                candidatos.append(h)
        if len(candidatos) > 1:
            raise RuntimeError('Hay varios sobres en la banda de salida; revisar escena')
        if candidatos:
            self.iniciar(candidatos[0])

    def iniciar(self, sobre):
        if self.sobre is not None:
            raise RuntimeError('Banda de salida todavía ocupada')
        if self.sim.getObjectAlias(sobre) == 'Sobre_000':
            raise RuntimeError('La plantilla no se transporta ni se elimina')
        if self.sim.getObjectParent(sobre) != -1:
            raise RuntimeError('El sobre de salida todavía está sujeto')
        self.inicio = list(self.sim.getObjectPosition(sobre, -1))
        if Y_FINAL_SALIDA < self.inicio[1]:
            raise RuntimeError('Posición inicial de salida inválida')
        self.sobre = sobre
        self.transcurrido = 0.0
        self.ultimo = time.monotonic()
        print('BANDA DE SALIDA: transporte hacia atrás iniciado')

    def pausar(self):
        self.ultimo = None

    def ciclo(self, permitido=True):
        if not permitido:
            self.pausar()
            return
        if self.sobre is None:
            return
        ahora = time.monotonic()
        if self.ultimo is not None:
            self.transcurrido += max(0.0, ahora - self.ultimo)
        self.ultimo = ahora
        progreso = min(1.0, self.transcurrido / DURACION_SALIDA)
        pos = list(self.inicio)
        pos[1] += (Y_FINAL_SALIDA - pos[1]) * progreso
        self.sim.setObjectPosition(self.sobre, -1, pos)
        if progreso >= 1.0:
            # Eliminar también QR y marcador; nunca la plantilla ni otros sobres.
            objetos = self.sim.getObjectsInTree(self.sobre, self.sim.handle_all, 0)
            self.sim.removeObjects(objetos)
            print('BANDA DE SALIDA: sobre entregado y retirado de la escena')
            self.sobre = self.inicio = self.ultimo = None
            self.transcurrido = 0.0
