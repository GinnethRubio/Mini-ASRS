"""Pruebas del conductor y del registro; no usa PLC ni CoppeliaSim."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import banco_112 as b
import sys


class Log:
    def __init__(self):self.eventos=[]
    def emitir(self,tipo,**datos):self.eventos.append((tipo,datos))


def muestra():
    d={'GVL.Paso_Actual':0,'GVL.Python_Error':False,'GVL.Comunicacion_OK':True,
       'GVL.IN1':28,'GVL.Inventario_Listo':True,'GVL.Maquina_Lista':True}
    d.update({'GVL_HMI.'+n:v for n,v in dict(iOperaciones=0,iAlmacenados=0,iRecuperados=0,iFallos=0,
        iOperacionActual=0,sCodigoActual='',iColumnaActual=1,iFilaActual=1,iEstrategia=0,
        xModoManual=False,xModoPaso=False,xSimularCoppelia=False,xMarcha=False,
        axOcupada=[False]*112,asCodigo=['']*112,uLogId=0,asLog=[],asHistorial=[],sMensaje='',
        rTiempoCicloUltimo_s=30.,rDistanciaUltima_mm=500.).items()})
    return d


class Pruebas(unittest.TestCase):
    def setUp(self):
        self.log=Log();self.escritos=[];self.now=0
        self.c=b.Campana(lambda n,v:self.escritos.append((n,v)),self.log,0,lambda:self.now)
        self.d=muestra()

    def test_estrategia_2_rechaza_plc_antiguo(self):
        plc=SimpleNamespace(client=SimpleNamespace(get_node=lambda n:SimpleNamespace(get_value=lambda:1)))
        with self.assertRaisesRegex(RuntimeError,'incompatible'):
            b.verificar_estrategia_plc(plc,4,2)
        plc.client.get_node=lambda n:SimpleNamespace(get_value=lambda:2)
        b.verificar_estrategia_plc(plc,4,2)

    def test_campana_112_y_112_sin_escribir_entradas(self):
        c=self.c;d=self.d
        self.assertFalse(c.avanzar(d))
        self.assertIn(('GVL.START',True),self.escritos)
        d.update({'GVL.Paso_Actual':50,'GVL_HMI.xMarcha':True,'GVL_HMI.iAlmacenados':111,
                  'GVL_HMI.iOperacionActual':1})
        c.avanzar(d);self.assertTrue(c.paro_enviado)
        d.update({'GVL.Paso_Actual':0,'GVL_HMI.xMarcha':False,'GVL_HMI.iAlmacenados':112,
                  'GVL_HMI.iOperacionActual':0,'GVL_HMI.axOcupada':[True]*112,
                  'GVL_HMI.asCodigo':[f'AS-{i:03}' for i in range(1,113)]})
        c.avanzar(d)
        for i in range(112):
            c.avanzar(d)
            self.assertEqual(c.qr,f'AS-{i+1:03}')
            d.update({'GVL.Paso_Actual':50,'GVL_HMI.xMarcha':True,'GVL_HMI.iOperacionActual':2})
            c.avanzar(d)
            self.assertTrue(c.paro_enviado)
            d.update({'GVL.Paso_Actual':0,'GVL_HMI.xMarcha':False,'GVL_HMI.iOperacionActual':0,
                      'GVL_HMI.iRecuperados':i+1})
            d['GVL_HMI.axOcupada'][i]=False;d['GVL_HMI.asCodigo'][i]=''
            c.avanzar(d);self.now+=5.1
        self.assertTrue(c.avanzar(d));self.assertEqual(c.fase,'completa')
        self.assertEqual(sum(n=='GVL_HMI.xSolicitudRecuperar' for n,v in self.escritos),112)
        self.assertTrue(all(n in {'GVL.START','GVL.STOP','GVL_HMI.iEstrategia',
            'GVL_HMI.sCodigoRecuperar','GVL_HMI.xSolicitudRecuperar'} for n,v in self.escritos))

    def test_rechaza_inventario_viejo_sin_mandos(self):
        self.d['GVL_HMI.axOcupada'][0]=True
        with self.assertRaisesRegex(RuntimeError,'rack vacío'):self.c.avanzar(self.d)
        self.assertEqual(self.escritos,[])

    def test_continuar_conserva_deposito_previo(self):
        self.d['GVL_HMI.axOcupada'][0]=True;self.d['GVL_HMI.asCodigo'][0]='AS-001'
        self.d['GVL_HMI.iAlmacenados']=1
        c=b.Campana(lambda n,v:self.escritos.append((n,v)),self.log,0,lambda:self.now,continuar=True)
        c.avanzar(self.d)
        self.assertEqual(c.base_alm,0)
        self.assertEqual(self.d['GVL_HMI.asCodigo'][0],'AS-001')
        self.assertFalse(any('asCodigo' in n or 'axOcupada' in n for n,v in self.escritos))

    def test_json_reintenta_archivo_abierto(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            log=b.Registro(Path(tmp),'registrar',Path(tmp),{})
            original=Path.replace;veces=[]
            def reemplazar(p,dest):
                veces.append(1)
                if len(veces)==1:raise PermissionError('Lector Windows temporal')
                return original(p,dest)
            try:
                with patch.object(Path,'replace',reemplazar),patch.object(b.time,'sleep'):
                    log.json('avance.json',{'muestras':1})
                self.assertEqual(len(veces),2)
                self.assertTrue((log.path/'avance.json').exists())
            finally:log.cerrar()

    def test_no_rearma_alarma(self):
        self.d['GVL.Paso_Actual']=999
        with self.assertRaisesRegex(RuntimeError,'Alarma'):self.c.avanzar(self.d)
        self.assertEqual(self.escritos,[])

    def test_pierde_permisos_fluid_sin_escrituras(self):
        for palabra in (0,4,8,16,12,24):
            self.d['GVL.IN1']=palabra
            with self.assertRaisesRegex(RuntimeError,'permisos'):self.c.avanzar(self.d)
        self.assertEqual(self.escritos,[])

    def test_sin_progreso_no_sigue(self):
        self.c.avanzar(self.d);self.now=91
        with self.assertRaises(TimeoutError):self.c.avanzar(self.d)

    def test_paro_normal_solo_ultimo_almacenamiento(self):
        self.c.avanzar(self.d)
        self.d.update({'GVL.Paso_Actual':50,'GVL_HMI.iAlmacenados':110,'GVL_HMI.iOperacionActual':1})
        self.c.avanzar(self.d)
        self.assertNotIn(('GVL.STOP',True),self.escritos)

    def test_registrador_guarda_final_sin_confundir_siguiente_operacion(self):
        self._verificar_registro(False)

    def test_recuperacion_conserva_origen_cuando_despacho_publica_cero(self):
        self._verificar_registro(True)

    def _verificar_registro(self, recuperacion):
        a=muestra();a.update({'GVL_HMI.iOperacionActual':1,'GVL_HMI.sCodigoActual':'AS-001'})
        z=muestra();z.update({'GVL_HMI.iOperaciones':1,'GVL_HMI.iAlmacenados':1,
            'GVL_HMI.iOperacionActual':1,'GVL_HMI.sCodigoActual':'AS-002','GVL_HMI.iFilaActual':2})
        z['GVL_HMI.axOcupada'][0]=True;z['GVL_HMI.asCodigo'][0]='AS-001'
        filas_prueba=[a,z]
        if recuperacion:
            a['GVL_HMI.iOperacionActual']=2
            a['GVL_HMI.axOcupada']=[True]+[False]*111
            a['GVL_HMI.asCodigo']=['AS-001']+['']*111
            despacho=dict(a);despacho.update({'GVL_HMI.iColumnaActual':0,'GVL_HMI.iFilaActual':0})
            z=muestra();z.update({'GVL_HMI.iOperaciones':1,'GVL_HMI.iRecuperados':1,
                'GVL_HMI.iColumnaActual':0,'GVL_HMI.iFilaActual':0})
            filas_prueba=[a,despacho,z]
        class Cliente:
            def __init__(self):self.filas=list(filas_prueba);self.cerrado=False
            def get_node(self,n):return n.split('.Application.')[1]
            def get_values(self,nodos):
                if not self.filas:raise KeyboardInterrupt
                fila=self.filas.pop(0);return [fila.get(n,0) for n in nodos]
        cli=Cliente()
        plc=SimpleNamespace(client=cli,nodes={'n':SimpleNamespace(nodeid=SimpleNamespace(NamespaceIndex=4))},
            desconectar=lambda:setattr(cli,'cerrado',True))
        fabrica=lambda **kw:SimpleNamespace(conectar=lambda:plc)
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            log=b.Registro(Path(tmp),'registrar',Path(tmp),{})
            args=SimpleNamespace(opc_url='fake',modo='registrar',estrategia=0,duracion=0,intervalo=.05)
            try:
                with patch.dict(sys.modules,{'codesys_opcua':SimpleNamespace(CodesysOPCUA=fabrica)}),patch.object(b.time,'sleep'):
                    b.registrar(args,log)
            finally:log.cerrar()
            texto=(log.path/'operaciones_observadas.csv').read_text(encoding='utf-8-sig')
            self.assertIn('AS-001,1,1',texto);self.assertNotIn('AS-002',texto)
            self.assertTrue(cli.cerrado)


if __name__=='__main__':unittest.main(verbosity=2)
