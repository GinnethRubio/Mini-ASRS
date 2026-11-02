import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI))
import contraste_rc as cr  # noqa: E402
from campana_rc import cargar_celdas, costos_c  # noqa: E402
from registro_experimental import Corrida  # noqa: E402


class PLCSimulado:
    """PLC con estrategia 3: acepta el destino propuesto una vez por solicitud y exige paro normal."""
    def __init__(self, ocupadas=()):
        self.d = {n: False for n in cr.NOMBRES}
        self.d.update({'GVL.Paso_Actual': 0, 'GVL.Comunicacion_OK': True, 'GVL.IN0': 5, 'GVL.IN1': 28,
                       'GVL.Python_Mensaje': '', 'GVL.Maquina_Lista': True, 'GVL.Inventario_Listo': True,
                       'GVL_HMI.iOperacionActual': 0, 'GVL_HMI.iAlmacenados': 0, 'GVL_HMI.iRecuperados': 0,
                       'GVL_HMI.rTiempoCicloUltimo_s': 0.0, 'GVL_HMI.rDistanciaUltima_mm': 0.0,
                       'GVL_HMI.uSolicitudAceptada': 7})
        self.ocupada = [False] * 112
        self.codigo = [''] * 112
        for c in ocupadas:
            self.depositar(c)
        self.w = {'GVL_HMI.iEstrategia': 0, 'GVL_HMI.iDestinoPropuesto': 0, 'GVL_HMI.uSolicitudDestino': 0,
                  'GVL_HMI.sCodigoRecuperar': '', 'GVL_HMI.xSolicitudRecuperar': False}
        self.ciclo = None
        self.stop = False
        self.cortar_en = None
        self.lecturas = 0
        self.inicios = 0

    def depositar(self, celda):
        usados = {int(q[3:]) for q in self.codigo if q}
        n = next(i for i in range(1, 1000) if i not in usados)
        self.ocupada[celda - 1], self.codigo[celda - 1] = True, f'AS-{n:03d}'

    def escribir(self, n, v):
        if n == 'GVL.START':
            self.inicios += 1
            self.d['GVL_HMI.xMarcha'], self.stop = True, False
            self.iniciar()
        elif n == 'GVL.STOP':
            self.stop = True
        else:
            self.w[n] = v

    def iniciar(self):
        if self.w['GVL_HMI.xSolicitudRecuperar']:
            celda = self.codigo.index(self.w['GVL_HMI.sCodigoRecuperar']) + 1
            self.w['GVL_HMI.xSolicitudRecuperar'] = False
            self.ciclo = ('RETRIEVE', celda, 0)
            return
        destino = self.w['GVL_HMI.iDestinoPropuesto']
        if (self.w['GVL_HMI.iEstrategia'] != 3 or self.ocupada[destino - 1]
                or self.w['GVL_HMI.uSolicitudDestino'] == self.d['GVL_HMI.uSolicitudAceptada']):
            self.d['GVL.Paso_Actual'] = 999
            self.d['GVL.Python_Mensaje'] = 'DESTINO PROPUESTO INVALIDO'
            return
        self.d['GVL_HMI.uSolicitudAceptada'] = self.w['GVL_HMI.uSolicitudDestino']
        self.ciclo = ('STORE', destino, 0)

    def leer(self):
        self.lecturas += 1
        if self.cortar_en is not None and self.lecturas >= self.cortar_en:
            self.cortar_en = None
            raise ConnectionResetError(10054, 'enlace OPC UA perdido')
        if self.ciclo:
            tipo, celda, paso = self.ciclo
            if paso < 2:
                self.d['GVL.Paso_Actual'] = 50
                self.d['GVL_HMI.iOperacionActual'] = 1 if tipo == 'STORE' else 2
                self.ciclo = (tipo, celda, paso + 1)
            else:
                if tipo == 'STORE':
                    self.depositar(celda)
                    self.d['GVL_HMI.iAlmacenados'] += 1
                else:
                    self.ocupada[celda - 1], self.codigo[celda - 1] = False, ''
                    self.d['GVL_HMI.iRecuperados'] += 1
                self.d['GVL_HMI.rTiempoCicloUltimo_s'] = 20.0 + celda / 10
                self.d['GVL.Paso_Actual'], self.d['GVL_HMI.iOperacionActual'] = 0, 0
                if not self.stop:
                    raise AssertionError('Sin paro normal el PLC encadenaría otro ciclo')
                self.d['GVL_HMI.xMarcha'], self.ciclo = False, None
        d = dict(self.d)
        d['GVL_HMI.axOcupada'], d['GVL_HMI.asCodigo'] = list(self.ocupada), list(self.codigo)
        return d


class VentanaSimulada:
    """Cada ciclo consume 40 s del runtime."""
    def __init__(self, plc, segundos):
        self.plc, self.s = plc, segundos + 40 * plc.inicios

    def restante_s(self):
        return self.s - 40 * self.plc.inicios


class Pruebas(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.raiz = Path(self.tmp.name)
        self.args = SimpleNamespace(campana='prueba_rcC', t_operacion_s=40)
        self.t = 0.0

    def tearDown(self):
        self.tmp.cleanup()

    def reloj(self):
        self.t += .5
        return self.t

    def contraste(self, plc, plan, ventana=None):
        ejecutor = cr.Ejecutor(plc.leer, plc.escribir, lambda *a, **k: None, reloj=self.reloj,
                               dormir=lambda s: None, intervalo=0)

        def nuevo(run_id, corrida):
            return Corrida(self.raiz / 'datos', 'C', run_id, politica=corrida and corrida['politica'],
                           ocupacion=corrida and corrida['ocupacion'], replica=corrida and corrida['replica'])
        return cr.Contraste(self.args, ejecutor, plan, self.raiz / 'estado.json', nuevo, ventana,
                            lambda *a, **k: None)

    def indice(self):
        with (self.raiz / 'datos' / 'registro_corridas.csv').open(encoding='utf-8') as f:
            return list(csv.DictReader(f))

    def test_plan_pareado_y_reproducible(self):
        plan = cr.plan_contraste(20261103, [28, 56, 84], 2, 10)
        self.assertEqual(plan, cr.plan_contraste(20261103, [28, 56, 84], 2, 10))
        self.assertEqual(len(plan), 12)
        costo = costos_c(cargar_celdas(), 'tiempo')
        for r, c in zip(plan[::2], plan[1::2]):
            self.assertEqual((r['politica'], c['politica']), ('R', 'C'))
            self.assertEqual(r['inicial'], c['inicial'])
            self.assertEqual(len(r['inicial']), r['ocupacion'])
            self.assertEqual(len(c['operaciones']), 20)
            self.assertEqual(r['operaciones'][0], c['operaciones'][0])
            ocupadas = set(c['inicial'])
            for op in c['operaciones']:
                if op['tipo'] == 'RETRIEVE':
                    ocupadas.remove(op['celda'])
                else:
                    libres = set(range(1, 113)) - ocupadas
                    self.assertEqual(op['celda'], min(libres, key=lambda i: (round(costo[i], 9), i)))
                    ocupadas.add(op['celda'])

    def test_transicion(self):
        self.assertEqual(cr.transicion([1, 2, 3], [2, 3, 4]),
                         [dict(tipo='RETRIEVE', celda=1), dict(tipo='STORE', celda=4)])
        self.assertEqual(cr.transicion([5], [5]), [])

    def test_campana_completa_desde_rack_vacio(self):
        plan = cr.plan_contraste(7, [3], 2, 2)
        plc = PLCSimulado()
        self.assertEqual(self.contraste(plc, plan).ejecutar(), 0)
        filas = {r['run_id']: r for r in self.indice()}
        medidas = [r for r in filas if r.startswith('prueba_rcC')]
        self.assertEqual(sorted(medidas), sorted(f"prueba_rcC_K003_r{p['replica']:02d}_{p['politica']}" for p in plan))
        self.assertTrue(all(filas[r]['estado'] == 'completa' and filas[r]['operaciones_medidas'] == '4' for r in medidas))
        self.assertTrue(any(r.startswith('prep_prueba_rcC') for r in filas))
        estado = json.loads((self.raiz / 'estado.json').read_text(encoding='utf-8'))
        self.assertEqual(estado['siguiente'], len(plan))
        ruta = self.raiz / 'datos' / filas[medidas[0]]['ruta'] / 'operaciones.csv'
        with ruta.open(encoding='utf-8') as f:
            ops = list(csv.DictReader(f))
        self.assertEqual([o['operacion'] for o in ops], ['RETRIEVE', 'STORE'] * 2)
        self.assertTrue(all(o['recorrido_x_mm'] and o['tiempo_s'] for o in ops))

    def test_cierra_bloque_por_runtime_y_continua(self):
        plan = cr.plan_contraste(7, [3], 1, 2)
        plc = PLCSimulado()
        self.assertEqual(self.contraste(plc, plan, VentanaSimulada(plc, 100)).ejecutar(), 3)
        self.assertEqual(sum(plc.ocupada), 2)   # dos almacenamientos de preparación caben; el tercero no
        self.assertEqual(self.contraste(plc, plan, VentanaSimulada(plc, 10_000)).ejecutar(), 0)
        estado = json.loads((self.raiz / 'estado.json').read_text(encoding='utf-8'))
        self.assertEqual((estado['siguiente'], estado['bloques']), (2, 2))

    def test_corte_repite_la_corrida_completa(self):
        plan = cr.plan_contraste(7, [3], 1, 2)
        plc = PLCSimulado(plan[0]['inicial'])
        plc.cortar_en = 8
        with self.assertRaises(ConnectionResetError):
            self.contraste(plc, plan).ejecutar()
        self.assertEqual(self.contraste(plc, plan).ejecutar(), 0)
        estados = {r['run_id']: r['estado'] for r in self.indice()}
        self.assertEqual(estados['prueba_rcC_K003_r01_R'], 'incompleta')
        self.assertEqual(estados['prueba_rcC_K003_r01_R_i2'], 'completa')
        self.assertEqual(estados['prueba_rcC_K003_r01_C'], 'completa')

    def test_calibracion_recupera_la_recta(self):
        from calibracion import ajustar, aplicar
        puntos = [(op, x, 3.0 + 1.1 * x) for op in ('STORE', 'RETRIEVE') for x in range(10, 40, 2)]
        par = ajustar(puntos)
        self.assertAlmostEqual(par['STORE']['a_s'], 3.0, places=6)
        self.assertAlmostEqual(par['RETRIEVE']['b'], 1.1, places=6)
        self.assertAlmostEqual(aplicar(par, 'STORE', 20), 25.0, places=6)
        self.assertEqual(aplicar(None, 'STORE', 20), 20)

    def test_vaciado_deja_el_rack_vacio(self):
        plc = PLCSimulado([3, 40, 90])
        self.args.vaciar = True
        self.assertEqual(self.contraste(plc, []).ejecutar(), 0)
        self.assertFalse(any(plc.ocupada))
        self.assertEqual(plc.d['GVL_HMI.iRecuperados'], 3)

    def test_destino_rechazado_por_el_plc_detiene(self):
        plc = PLCSimulado([1, 2])
        ejecutor = cr.Ejecutor(plc.leer, plc.escribir, lambda *a, **k: None, reloj=self.reloj,
                               dormir=lambda s: None, intervalo=0)
        ejecutor.preparar()
        with self.assertRaisesRegex(RuntimeError, 'ocupada'):
            ejecutor.almacenar(2)
        plc.w['GVL_HMI.iEstrategia'] = 2      # otra estrategia activa en el HMI: el PLC no acepta el destino
        with self.assertRaisesRegex(RuntimeError, 'Alarma: DESTINO PROPUESTO INVALIDO'):
            ejecutor.almacenar(5)
        self.assertFalse(plc.ocupada[4])

if __name__ == '__main__':
    unittest.main(verbosity=2)
