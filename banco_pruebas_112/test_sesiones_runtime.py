import unittest
import tempfile,json,sys
from types import SimpleNamespace
from unittest.mock import patch
from datetime import datetime,timezone,timedelta
from pathlib import Path
from sesiones_runtime import VentanaRuntime,punto_continuacion,restaurar
from banco_112 import Campana
import banco_112 as banco
import sesiones_runtime as sesiones
from test_banco import muestra,Log


class PruebasRuntime(unittest.TestCase):
    def setUp(self):
        self.now=0.;self.fecha=datetime(2026,10,23,17,0,tzinfo=timezone.utc)
        self.mandos=[];self.log=Log();self.d=muestra()
        self.d.update({'GVL.Y_ESTADO_VALIDO':True,'GVL.EJE_Y_ESTADO':False,
                       'GVL.GRIPPER_ESTADO_VALIDO':True,'GVL.GRIPPER_ESTADO':False,'GVL.IN0':5})
        self.c=Campana(self.escribir,self.log,0,reloj=lambda:self.now)
    def escribir(self,n,v):self.mandos.append((n,v))
    def ventana(self,edad=0):
        return VentanaRuntime((self.fecha-timedelta(minutes=edad)).isoformat(),ahora=self.fecha,reloj=lambda:self.now)
    def llenar(self,n):
        self.d['GVL_HMI.axOcupada']=[i<n for i in range(112)]
        self.d['GVL_HMI.asCodigo']=[f'AS-{i+1:03}' if i<n else '' for i in range(112)]

    def test_usa_edad_del_runtime_no_del_python(self):
        v=self.ventana(30)
        self.assertEqual(v.parar_en,20*60)
        self.assertEqual(v.motivo,'pausa_runtime')
    def test_bloque_45_min_con_runtime_nuevo(self):
        v=self.ventana()
        self.assertEqual(v.parar_en,45*60)
        self.assertEqual(v.motivo,'pausa_bloque')
    def test_no_arranca_si_runtime_agotado_o_futuro(self):
        with self.assertRaisesRegex(ValueError,'margen'):self.ventana(51)
        with self.assertRaisesRegex(ValueError,'futuro'):self.ventana(-1)
        self.assertEqual(self.mandos,[])
    def test_exige_zona_horaria(self):
        with self.assertRaisesRegex(ValueError,'zona'):VentanaRuntime('2026-10-23T12:00:00',ahora=self.fecha)
    def test_stop_unico_espera_fin_y_banda(self):
        v=self.ventana(30);self.now=1200
        self.d.update({'GVL.Paso_Actual':90,'GVL_HMI.xMarcha':True,'GVL_HMI.iOperacionActual':1})
        self.assertFalse(v.actualizar(self.d,self.escribir))
        self.now+=30;self.assertFalse(v.actualizar(self.d,self.escribir))
        self.d.update({'GVL.Paso_Actual':0,'GVL_HMI.xMarcha':False,'GVL_HMI.iOperacionActual':0})
        self.assertFalse(v.actualizar(self.d,self.escribir))
        self.now+=5;self.assertTrue(v.actualizar(self.d,self.escribir))
        self.assertEqual(self.mandos,[('GVL.STOP',True)])
    def test_no_certifica_gripper_cerrado(self):
        v=self.ventana(30);self.now=1200
        self.d['GVL.GRIPPER_ESTADO']=True
        self.assertFalse(v.actualizar(self.d,self.escribir))
        self.now+=121
        with self.assertRaises(TimeoutError):v.actualizar(self.d,self.escribir)
    def test_pausa_alarma_no_es_punto_valido(self):
        v=self.ventana(30);self.now=1200;self.d['GVL.Paso_Actual']=999
        with self.assertRaises(RuntimeError):v.actualizar(self.d,self.escribir)
    def test_reanuda_llenado_33_con_contadores_reiniciados(self):
        self.llenar(33);self.c.fase='llenando'
        cobertura={'almacenar':{(1,i) for i in range(1,29)},'recuperar':set()}
        p=punto_continuacion(self.c,self.d,cobertura,Path('bloque1'))
        c=Campana(self.escribir,self.log,0,reloj=lambda:self.now)
        restaurar(c,p,self.d)
        self.assertEqual(c.fase,'llenando');self.assertEqual(c.base_alm,-33)
        self.assertIn(('GVL.START',True),self.mandos)
    def test_reanuda_recuperacion_con_cola_pendiente(self):
        self.llenar(2);self.c.fase='extrayendo';self.c.cola=['AS-001','AS-002','AS-003']
        p=punto_continuacion(self.c,self.d,{'almacenar':{(c,f) for c in range(1,5) for f in range(1,29)},'recuperar':set()},Path('b'))
        c=Campana(self.escribir,self.log,0,reloj=lambda:self.now)
        restaurar(c,p,self.d)
        self.assertEqual(c.cola,['AS-001','AS-002']);self.assertEqual(c.base_rec,-110)
        c.avanzar(self.d)
        self.assertIn(('GVL_HMI.sCodigoRecuperar','AS-001'),self.mandos)
    def test_rechaza_inventario_distinto_antes_de_start(self):
        self.llenar(1);self.c.fase='llenando'
        p=punto_continuacion(self.c,self.d,{'almacenar':set(),'recuperar':set()},Path('b'))
        self.d['GVL_HMI.asCodigo'][0]='AS-999'
        with self.assertRaisesRegex(ValueError,'no coincide'):restaurar(self.c,p,self.d)
        self.assertEqual(self.mandos,[])
    def test_llenado_completo_pasa_a_recuperar(self):
        self.llenar(112);self.c.fase='llenando'
        p=punto_continuacion(self.c,self.d,{'almacenar':set(),'recuperar':set()},Path('b'))
        self.assertEqual(p['fase'],'siguiente');self.assertEqual(len(p['cola']),112)

    def test_registrador_cierra_bloque_con_ultimo_ciclo_registrado(self):
        a=dict(self.d)
        b=dict(a);b.update({'GVL.Paso_Actual':90,'GVL_HMI.xMarcha':True,
            'GVL_HMI.iOperacionActual':1,'GVL_HMI.sCodigoActual':'AS-001'})
        c=dict(a);c.update({'GVL_HMI.iOperaciones':1,'GVL_HMI.iAlmacenados':1})
        c['GVL_HMI.axOcupada']=[True]+[False]*111;c['GVL_HMI.asCodigo']=['AS-001']+['']*111
        muestras=[a,b,c,c]
        exterior=self
        class Nodo:
            def __init__(self,n):self.n=n
            def get_data_type_as_variant_type(self):return 'tipo'
            def set_value(self,v):exterior.mandos.append((self.n,v))
        class Cliente:
            def get_node(self,n):return Nodo(n.split('.Application.')[1])
            def get_values(self,nodos):
                if not muestras:raise AssertionError('El registrador no cerró el bloque')
                d=muestras.pop(0);return [d.get(n.n,0) for n in nodos]
        plc=SimpleNamespace(client=Cliente(),nodes={'x':SimpleNamespace(nodeid=SimpleNamespace(NamespaceIndex=4))},desconectar=lambda:None)
        cod=SimpleNamespace(CodesysOPCUA=lambda **kw:SimpleNamespace(conectar=lambda:plc))
        inventario=SimpleNamespace(validar_inventario=lambda p:{'celdas':[
            dict(indice=i,ocupada=False,qr='') for i in range(1,113)]})
        def dormir(segundos):self.now+=1200 if self.now==0 else 5
        construir=sesiones.VentanaRuntime
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            args=SimpleNamespace(modo='campana',opc_url='fake',estrategia=0,continuar=False,reanudar=None,
                inicio_runtime=(self.fecha-timedelta(minutes=30)).isoformat(),margen_runtime_min=10,
                bloque_min=45,proyecto=Path(tmp),duracion=0,intervalo=.2)
            log=banco.Registro(Path(tmp),'campana',Path(tmp),{})
            try:
                with patch.dict(sys.modules,{'codesys_opcua':cod,'inventario':inventario,
                    'opcua':SimpleNamespace(ua=SimpleNamespace(Variant=lambda v,t:v))}), \
                    patch.object(banco.time,'monotonic',lambda:self.now), \
                    patch.object(banco.time,'sleep',dormir), \
                    patch.object(sesiones,'VentanaRuntime',lambda *a,**kw:construir(*a,**kw,ahora=self.fecha,reloj=lambda:self.now)):
                    resultado=banco.registrar(args,log)
                self.assertEqual(resultado,3)
                p=json.loads((log.path/'continuar_campana.json').read_text())
                self.assertEqual(p['fase'],'llenando');self.assertTrue(p['ocupadas'][0])
                self.assertEqual(p['cobertura']['almacenar'],[[1,1]])
                self.assertEqual(sum(n=='GVL.START' for n,v in self.mandos),1)
                self.assertEqual(sum(n=='GVL.STOP' for n,v in self.mandos),1)
                self.assertIn('AS-001',(log.path/'operaciones_observadas.csv').read_text(encoding='utf-8-sig'))
            finally:log.cerrar()


if __name__=='__main__':unittest.main(verbosity=2)
