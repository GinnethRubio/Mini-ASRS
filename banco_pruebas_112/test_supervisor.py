import csv
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
import supervisor_campana as sv  # noqa: E402
from registro_experimental import Corrida  # noqa: E402


def reposo(n=0):
    d = {n_: False for n_ in sv.NOMBRES}
    d.update({'GVL.Paso_Actual': 0, 'GVL.Comunicacion_OK': True, 'GVL.IN0': 5, 'GVL.IN1': 28,
              'GVL.Maquina_Lista': True, 'GVL.Inventario_Listo': True, 'GVL.Y_ESTADO_VALIDO': True,
              'GVL.GRIPPER_ESTADO_VALIDO': True, 'GVL_HMI.iOperacionActual': 0,
              'GVL_HMI.axOcupada': [i < n for i in range(112)],
              'GVL_HMI.asCodigo': [f'AS-{i+1:03}' if i < n else '' for i in range(112)],
              'GVL_HMI.iAlmacenados': 0, 'GVL_HMI.iRecuperados': 0})
    return d


class Escenario:
    """CODESYS, runtime y banco simulados; el tiempo avanza con cada espera."""
    def __init__(self, tmp):
        self.tmp = Path(tmp)
        self.t = 0.0
        self.caido_hasta = None
        self.inicio = datetime.now(timezone.utc) - timedelta(minutes=5)
        self.d = reposo(30)
        self.bloques = []
        self.coordinador = SimpleNamespace(asegurar=lambda relanzar: self.lanzados.append(relanzar),
                                           descartar=lambda: None)
        self.lanzados = []

    def dormir(self, s):
        self.t += s

    def reloj(self):
        return self.t

    def leer(self):
        if self.caido_hasta is not None and self.t < self.caido_hasta:
            return None
        return dict(self.d)

    def inicio_runtime(self):
        return None if self.leer() is None else self.inicio.isoformat()

    def carpeta(self, n):
        c = self.tmp / f'bloque{n}'
        c.mkdir()
        return c


class Pruebas(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.e = Escenario(self.tmp.name)
        self.args = SimpleNamespace(reanudar=None, continuar=False, espera_max_min=60,
                                    limite_runtime_min=60, margen_runtime_min=10, bloque_min=45)

    def tearDown(self):
        self.tmp.cleanup()

    def supervisor(self, ejecutar, corrida=None):
        return sv.Supervisor(self.args, self.tmp.name, corrida, self.e.leer, self.e.inicio_runtime, ejecutar,
                             self.e.coordinador, dormir=self.e.dormir, reloj=self.e.reloj)

    def test_corte_de_codesys_se_recupera_y_continua_desde_la_escena(self):
        e = self.e

        def ejecutar(n, punto, continuar, inicio):
            e.bloques.append(punto)
            c = e.carpeta(n)
            if n == 1:
                (c / 'estado_interrumpido.json').write_text(json.dumps(dict(
                    version=1, estado='interrumpido', fase='llenando', cola=[], estrategia=2,
                    cobertura={'almacenar': [[1, i] for i in range(1, 29)], 'recuperar': []},
                    campana_id='c1', log_anterior=str(c), motivo="ConnectionRefusedError(10061)")), encoding='utf-8')
                # El runtime se reinicia: 3 min sin servidor OPC y nueva hora de arranque.
                e.caido_hasta = e.t + 180
                e.inicio = datetime.now(timezone.utc)
                return 1, str(c)
            return 0, str(c)
        with tempfile.TemporaryDirectory() as datos:
            corrida = Corrida(datos, 'D', 'prueba_supervisor', politica='E2')
            sup = self.supervisor(ejecutar, corrida)
            self.assertEqual(sup.ejecutar(), 0)
            sup.cerrar()
            corrida.cerrar('completa')
            with (corrida.path / 'interrupciones.csv').open(encoding='utf-8') as f:
                filas = list(csv.DictReader(f))
        self.assertEqual(e.bloques[0], None)
        self.assertEqual(Path(e.bloques[1]).name, 'continuar_tras_reconexion.json')
        punto = json.loads(Path(e.bloques[1]).read_text(encoding='utf-8'))
        self.assertEqual(punto['fase'], 'llenando')
        self.assertEqual(sum(punto['ocupadas']), 30)
        self.assertEqual([f['causa'] for f in filas], ['perdida_comunicacion'])
        self.assertGreaterEqual(float(filas[0]['duracion_s']), 0)
        eventos = [json.loads(x)['tipo'] for x in (Path(self.tmp.name) / 'supervisor.jsonl').read_text().splitlines()]
        self.assertIn('desconexion_codesys', eventos)
        self.assertIn('reanudacion', eventos)

    def test_pausa_por_runtime_espera_reinicio_y_reanuda(self):
        e = self.e
        e.inicio = datetime.now(timezone.utc) - timedelta(minutes=49)

        def ejecutar(n, punto, continuar, inicio):
            e.bloques.append(punto)
            c = e.carpeta(n)
            if n == 1:
                (c / 'continuar_campana.json').write_text(json.dumps({'motivo': 'pausa_runtime'}), encoding='utf-8')
                e.inicio = datetime.now(timezone.utc) - timedelta(minutes=55)
                e.caido_hasta = e.t + 600
                return 3, str(c)
            return 0, str(c)
        nuevo = {'listo': False}
        leer = e.leer

        def leer_con_reinicio():
            d = leer()
            if d is not None and e.caido_hasta is not None and not nuevo['listo']:
                e.inicio = datetime.now(timezone.utc)
                nuevo['listo'] = True
            return d
        e.leer = leer_con_reinicio
        sup = sv.Supervisor(self.args, self.tmp.name, None, e.leer, e.inicio_runtime, ejecutar,
                            e.coordinador, dormir=e.dormir, reloj=e.reloj)
        self.assertEqual(sup.ejecutar(), 0)
        sup.cerrar()
        self.assertEqual(Path(e.bloques[1]).name, 'continuar_campana.json')
        self.assertGreaterEqual(e.t, 600)

    def test_alarma_del_proceso_no_se_rearma(self):
        e = self.e

        def ejecutar(n, punto, continuar, inicio):
            c = e.carpeta(n)
            (c / 'estado_interrumpido.json').write_text(json.dumps(
                {'motivo': "RuntimeError('Alarma: campaña detenida sin rearme automático')"}), encoding='utf-8')
            return 1, str(c)
        sup = self.supervisor(ejecutar)
        self.assertEqual(sup.ejecutar(), 1)
        sup.cerrar()

    def test_relanza_main_si_termina_durante_la_espera(self):
        lanzados = []

        class Proceso:
            def __init__(self, vivo):
                self.vivo, self.returncode = vivo, 1

            def poll(self):
                return None if self.vivo else 1
        listo = {'v': False}

        def leer():
            d = reposo(0)
            d['GVL.Maquina_Lista'] = listo['v']
            return d
        coord = sv.Coordinador(self.tmp.name, leer, lanzar=True, gracia_s=0, dormir=lambda s: None)
        coord.proceso = Proceso(False)
        original = sv.subprocess.Popen

        def popen(*a, **k):
            lanzados.append(a[0])
            listo['v'] = True
            return Proceso(True)
        sv.subprocess.Popen = popen
        try:
            coord.vigilar()
            coord.vigilar()
        finally:
            sv.subprocess.Popen = original
        self.assertEqual(len(lanzados), 1)
        self.assertTrue(lanzados[0][-1].endswith('main.py'))

    def test_reanuda_desde_estado_interrumpido_por_alarma(self):
        e = self.e
        estado = Path(self.tmp.name) / 'estado_interrumpido.json'
        estado.write_text(json.dumps(dict(
            version=1, estado='interrumpido', fase='extrayendo', cola=[f'AS-{i+1:03}' for i in range(30)],
            estrategia=2, cobertura={'almacenar': [[c, f] for c in range(1, 5) for f in range(1, 29)],
                                     'recuperar': [[1, 1]]},
            campana_id='c1', log_anterior=self.tmp.name, motivo="RuntimeError('Alarma')")), encoding='utf-8')
        self.args.reanudar = str(estado)

        def ejecutar(n, punto, continuar, inicio):
            e.bloques.append(punto)
            return 0, str(e.carpeta(n))
        sup = self.supervisor(ejecutar)
        self.assertEqual(sup.ejecutar(), 0)
        sup.cerrar()
        self.assertEqual(Path(e.bloques[0]).name, 'continuar_tras_reconexion.json')
        punto = json.loads(Path(e.bloques[0]).read_text(encoding='utf-8'))
        self.assertEqual(punto['fase'], 'siguiente')
        self.assertEqual(sum(punto['ocupadas']), 30)

    def test_contraste_sin_tiempo_espera_reinicio_aunque_quede_margen(self):
        e = self.e
        self.args.modo = 'contraste'

        def ejecutar(n, punto, continuar, inicio):
            e.bloques.append(inicio)
            c = e.carpeta(n)
            if n == 1:
                (c / 'continuar_campana.json').write_text(json.dumps({'motivo': 'pausa_runtime'}), encoding='utf-8')
                e.caido_hasta = e.t + 120
                return 3, str(c)
            return 0, str(c)
        nuevo = {'listo': False}
        leer = e.leer

        def leer_con_reinicio():
            d = leer()
            if d is not None and e.caido_hasta is not None and not nuevo['listo'] and e.t >= e.caido_hasta:
                e.inicio = datetime.now(timezone.utc)
                nuevo['listo'] = True
            return d
        e.leer = leer_con_reinicio
        sup = sv.Supervisor(self.args, self.tmp.name, None, e.leer, e.inicio_runtime, ejecutar,
                            e.coordinador, dormir=e.dormir, reloj=e.reloj)
        self.assertEqual(sup.ejecutar(), 0)
        sup.cerrar()
        self.assertEqual(len(e.bloques), 2)
        self.assertNotEqual(e.bloques[0], e.bloques[1])

    def test_clasifica_cortes(self):
        self.assertTrue(sv.es_corte_comunicacion('ConnectionResetError(10054)'))
        self.assertTrue(sv.es_corte_comunicacion("RuntimeError('Contador de operaciones reiniciado')"))
        self.assertFalse(sv.es_corte_comunicacion("TimeoutError('90 s sin avance; revisar PLC')"))
        self.assertFalse(sv.es_corte_comunicacion("RuntimeError('Alarma: campaña detenida')"))


if __name__ == '__main__':
    unittest.main(verbosity=2)
