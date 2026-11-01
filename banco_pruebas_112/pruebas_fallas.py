import sys
import os
from pathlib import Path
PROYECTO = Path(os.environ.get('ASRS_TEST_PROJECT', Path(__file__).resolve().parent.parent)).resolve()
sys.path.insert(0, str(PROYECTO))
import json
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import main
import recuperacion as r
import inventario as inv
from control_portico import ControlPortico, RUTA_X, RUTA_Y, RUTA_Z
from control_gripper import ControlGripper, RUTA_GRIPPER


class PLC:
    conectado = True
    def __init__(self):
        self.d={n:False for n in main.ORDENES}
        self.d.update(Operacion_Actual=2, Codigo_Operacion='AS-001', Columna_Destino=1,
                      Fila_Destino=1, PYTHON_Heartbeat=0, Manual_Orden=0, CICLO_AUTO=False)
    def leer(self,n): return self.d.get(n,False)
    def leer_bool(self,n): return bool(self.leer(n))
    def leer_string(self,n): return str(self.leer(n))
    def escribir_bool(self,n,v): self.d[n]=v
    escribir_numero=escribir_bool
    escribir_string=escribir_bool
    def actualizar_posicion_x(self,v): self.d['EJE_X_POSICION']=v
    def actualizar_posicion_z(self,v): self.d['EJE_Z_POSICION']=v
    def actualizar_estado_y(self,v):
        self.d['EJE_Y_ESTADO']=v
        self.d['Y_ESTADO_VALIDO']=True
    def actualizar_estado_gripper(self,cerrado): self.d['GRIPPER_ESTADO']=cerrado


class Sim:
    handle_scene=-1
    handle_all=-2
    simulation_stopped=0
    def __init__(self):
        self.paths={RUTA_X:1,RUTA_Y:2,RUTA_Z:3,RUTA_GRIPPER:4,
                    '/Portico/Prismatico_Z/Carro_en_Z/Prismatico_X/Prismatico_Y/Gripper':5,
                    '/Sobre_001':6,'/Prismatico_Banda_Entrada':7,'/Prismatico_Banda_Salida':8}
        self.j={1:-.262,2:-.05,3:-.675,4:0.}
        self.pos={6:[0,0,0]}
        self.parent={6:-1}
    def getObject(self,path,*a):
        if path=='/Sobre_*':
            encontrados=[h for h in self.pos if self.getObjectAlias(h).startswith('Sobre_')]
            i=a[0].get('index',0) if a else 0
            return encontrados[i] if i<len(encontrados) else -1
        return self.paths.get(path,-1)
    def getJointPosition(self,h): return self.j[h]
    def setJointPosition(self,h,v):
        delta=v-self.j[h]
        self.j[h]=v
        if h in (1,2,3):
            for obj,p in self.parent.items():
                if p==5:self.pos[obj][{1:0,2:1,3:2}[h]]+=delta
    def getJointInterval(self,h): return [False,[-2.,4.]]
    def getObjectMatrix(self,h,*a): return [1,0,0,.525 if h==8 else 0,0,1,0,0,0,0,1,0]
    def getObjectPosition(self,h,*a): return self.pos[h][:]
    def getObjectParent(self,h): return self.parent.get(h,-1)
    def setObjectPosition(self,h,ref,pos): self.pos[h]=list(pos)
    def setObjectParent(self,h,p,*a): self.parent[h]=p
    def getObjectsInTree(self,*a): return list(self.pos)
    def getObjectAlias(self,h): return {6:'Sobre_001',9:'Sobre_002'}.get(h,'')
    def getSimulationState(self): return 1
    def getSimulationTime(self): return 1.0


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file=Path(self.tmp.name)/'inventario.json'
        cells=[dict(indice=i,columna=(i-1)//28+1,fila=(i-1)%28+1,ocupada=i==1,qr='AS-001' if i==1 else '') for i in range(1,113)]
        self.file.write_text(json.dumps(dict(celdas=cells)),encoding='utf-8')
        self.addCleanup(patch.stopall)
        patch.object(r,'buscar_qr',lambda qr:inv.buscar_qr(qr,self.file)).start()
        patch.object(r,'liberar_celda',lambda i:inv.liberar_celda(i,self.file)).start()
        self.plc=PLC();self.sim=Sim();self.now=0
        patch('control_portico.time.monotonic',lambda:self.now).start()
        c=main.CoordinadorMiniASRS.__new__(main.CoordinadorMiniASRS)
        c.plc=self.plc;c.sim=self.sim
        c.portico=ControlPortico(self.sim,self.plc,PROYECTO/'Coordenadas_Rack_Mini_ASRS.xlsx')
        c.gripper=ControlGripper(self.sim,self.plc)
        c.qr_actual=c.celda_actual=None;c.abierto_en_deposito=False
        c.anteriores={n:False for n in main.ORDENES}
        c.recuperacion=r.Recuperacion(c)
        self.c=c
        coord=c.portico.coordenadas[(1,1)]
        self.sim.pos[6]=[r.POSICION_FINAL[0]+coord['x']+.262,r.POSICION_FINAL[1],r.POSICION_FINAL[2]+coord['z_salida']+.675]

    def orden(self,n,terminar=True):
        for k in main.ORDENES:self.plc.d[k]=k==n
        self.c._ordenes()
        if terminar:
            # Los segmentos son secuenciales y cada uno necesita iniciar su reloj.
            for _ in range(6):self.now+=100;self.c.portico.avanzar()

    def preparar_extraccion(self):
        self.orden('Y_Retraer'); self.orden('G_Soltar')
        self.orden('Ir_Almacenamiento'); self.orden('Y_Extender'); self.orden('G_Ajustar')

    def test_ciclo_completo_y_registro_al_final(self):
        self.preparar_extraccion()
        self.orden('Y_Retraer')
        self.assertTrue(self.c.recuperacion.extraido)
        self.assertIsNotNone(inv.buscar_qr('AS-001',self.file))
        self.orden('Ir_Despacho');self.assertTrue(self.plc.d['Pos_Despacho'])
        self.orden('Y_Extender');self.assertFalse(self.plc.d['Pos_Despacho'])
        self.orden('Z_Ajuste');self.orden('G_Soltar')
        self.assertIsNotNone(inv.buscar_qr('AS-001',self.file))
        self.orden('Y_Retraer')
        self.assertIsNone(inv.buscar_qr('AS-001',self.file))
        self.assertTrue(self.plc.d['Deposito_Confirmado'])
        self.assertEqual(self.sim.parent[6],-1)
        for a,b in zip(self.sim.pos[6],r.POS_SOBRE_DESPACHO):self.assertAlmostEqual(a,b)

    def test_sobre_ausente_no_mueve(self):
        del self.sim.paths['/Sobre_001']
        inicio=self.sim.j.copy()
        with self.assertRaisesRegex(RuntimeError,'No existe'):self.orden('Ir_Almacenamiento')
        self.assertEqual(inicio,self.sim.j)

    def test_inventario_no_coincide_con_escena(self):
        self.sim.pos[6]=list(r.POSICION_FINAL)
        with self.assertRaisesRegex(RuntimeError,'no coinciden'):self.orden('Ir_Almacenamiento')

    def test_despacho_ocupado_no_inicia(self):
        self.sim.pos[9]=list(r.POS_SOBRE_DESPACHO)
        with self.assertRaisesRegex(RuntimeError,'ocupada'):self.orden('Ir_Almacenamiento')

    def test_origen_plc_distinto(self):
        self.plc.d['Fila_Destino']=2
        with self.assertRaisesRegex(RuntimeError,'Origen PLC distinto'):self.orden('Ir_Almacenamiento')

    def test_retirar_orden_detiene_sin_confirmar(self):
        self.orden('Ir_Almacenamiento',False)
        self.assertTrue(self.c.portico.en_movimiento())
        self.plc.d['Ir_Almacenamiento']=False
        self.c._ordenes()
        self.assertFalse(self.c.portico.en_movimiento())
        self.assertFalse(self.plc.d['Pos_Almacenamiento'])
        self.assertIsNotNone(inv.buscar_qr('AS-001',self.file))

    def test_no_libera_si_falla_guardado(self):
        self.preparar_extraccion();self.orden('Y_Retraer');self.orden('Ir_Despacho')
        self.orden('Y_Extender');self.orden('Z_Ajuste');self.orden('G_Soltar')
        patch.object(r,'liberar_celda',side_effect=OSError('disco')).start()
        with self.assertRaises(OSError):self.orden('Y_Retraer')
        self.assertFalse(self.plc.d.get('Deposito_Confirmado',False))
        self.assertIsNotNone(inv.buscar_qr('AS-001',self.file))

    def test_ordenes_opuestas(self):
        self.plc.d['Ir_Recogida']=self.plc.d['Ir_Despacho']=True
        with self.assertRaisesRegex(RuntimeError,'Dos destinos'):self.c._ordenes()

    def test_almacenamiento_sigue_funcionando(self):
        patch.object(main,'validar_inventario',lambda:inv.validar_inventario(self.file)).start()
        patch.object(main,'marcar_deposito_confirmado',lambda qr,i:inv.marcar_deposito_confirmado(qr,i,self.file)).start()
        self.plc.d['Operacion_Actual']=1
        self.plc.d['Fila_Destino']=2
        self.c.qr_actual='AS-002'
        self.c.banda=SimpleNamespace(sobre=6,qr='AS-002',_moviendo=False)
        self.sim.paths['/Sobre_002']=6
        self.sim.pos[6]=list(r.POSICION_FINAL)
        for n in ('Y_Retraer','G_Soltar','Ir_Recogida','Y_Extender','G_Ajustar',
                  'Y_Retraer','Ir_Almacenamiento','Y_Extender','Z_Ajuste','G_Soltar','Y_Retraer'):
            self.orden(n)
        self.assertIsNotNone(inv.buscar_qr('AS-002',self.file))
        self.assertTrue(self.plc.d['Deposito_Confirmado'])
        self.assertIsNone(self.c.qr_actual)

    def test_banda_no_crea_durante_recuperacion(self):
        from control_banda_integrado import ControlBanda
        b=ControlBanda.__new__(ControlBanda)
        b.plc=self.plc;b.sobre=None;b._crear_anterior=False
        b._terminado_anterior=False;b._banda_anterior=False;b._moviendo=False
        self.plc.d.update(Maquina_Lista=True,CICLO_AUTO=True,Paso_Actual=10,Crear_Sobre=False)
        with patch.object(b,'crear_sobre') as crear:
            b.ciclo();crear.assert_not_called()
            self.plc.d['Crear_Sobre']=True
            b.ciclo();crear.assert_called_once()

    def test_numeracion_respeta_json_y_escena(self):
        import control_banda_integrado as banda
        b=banda.ControlBanda.__new__(banda.ControlBanda)
        with patch.object(banda,'validar_inventario',return_value={'celdas':[
            {'qr':'AS-001','ocupada':True},{'qr':'AS-002','ocupada':True}]}), \
             patch.object(b,'_try_object',side_effect=lambda path: 123 if path=='/Sobre_003' else None):
            self.assertEqual(b._siguiente_numero(),4)

    def test_no_anticipa_fin_extraccion_por_tolerancia_y(self):
        from control_portico import duracion_perfil
        self.preparar_extraccion()
        self.orden('Y_Retraer',terminar=False)
        self.now+=100
        self.c.portico.avanzar()  # Finaliza elevación e inicia retracción.
        duracion=duracion_perfil(.330,*self.c.portico._cinematica[self.c.portico.joint_y])
        self.now+=duracion-.05
        self.c.portico.avanzar()
        self.assertLess(abs(self.c.portico.posicion_y()+.05),.002)
        self.assertFalse(self.c.recuperacion.extraido)
        self.c.heartbeat=0
        self.c.banda=SimpleNamespace(ciclo=lambda:None)
        self.plc.d['Comunicacion_OK']=True
        with patch.object(self.c,'_manual'),patch.object(self.c,'_qr'):
            self.c.ciclo()
            # El PLC no debe saltar a despacho mientras falta ejecutar el callback.
            self.assertFalse(self.plc.d['Y_ESTADO_VALIDO'])
            self.now+=.1
            self.c.ciclo()
        self.assertTrue(self.c.recuperacion.extraido)
        self.assertFalse(self.plc.d['EJE_Y_ESTADO'])
        self.assertTrue(self.plc.d['Y_ESTADO_VALIDO'])
        self.orden('Ir_Despacho')

    def test_banda_salida_avanza_pausa_y_elimina_solo_sobre(self):
        from control_banda_salida import ControlBandaSalida
        estado={'pos':[r.POS_SOBRE_DESPACHO[0],.775,.3295], 'borrados':[]}
        sim=SimpleNamespace(
            handle_all=-2,
            getObjectAlias=lambda h:'Sobre_001',
            getObjectParent=lambda h:-1,
            getObjectPosition=lambda h,ref:list(estado['pos']),
            setObjectPosition=lambda h,ref,pos:estado.update(pos=list(pos)),
            getObjectsInTree=lambda *a:[6,7,8],
            removeObjects=lambda objetos:estado['borrados'].extend(objetos))
        b=ControlBandaSalida(sim)
        b.iniciar(6)
        self.now+=2;b.ciclo()
        self.assertAlmostEqual(estado['pos'][1],(.775+2.150)/2)
        self.assertEqual(estado['borrados'],[])
        b.ciclo(False)
        anterior=estado['pos'][:]
        self.now+=100;b.ciclo(False)
        self.assertEqual(estado['pos'],anterior)
        b.ciclo(True)
        self.assertEqual(estado['pos'],anterior)
        self.now+=2;b.ciclo()
        self.assertAlmostEqual(estado['pos'][1],2.150)
        self.assertEqual(estado['borrados'],[6,7,8])
        self.assertIsNone(b.sobre)

    def test_salida_nunca_elimina_plantilla(self):
        from control_banda_salida import ControlBandaSalida
        b=ControlBandaSalida(SimpleNamespace(getObjectAlias=lambda h:'Sobre_000'))
        with self.assertRaisesRegex(RuntimeError,'plantilla'):b.iniciar(6)

    def test_salida_reconoce_sobre_pendiente_tras_reinicio(self):
        from control_banda_salida import ControlBandaSalida
        self.sim.pos[6]=list(r.POS_SOBRE_DESPACHO)
        b=ControlBandaSalida(self.sim)
        b.buscar_pendiente()
        self.assertEqual(b.sobre,6)

    def test_sincroniza_corrida_nueva_y_respalda_anterior(self):
        from sincronizacion_escena import sincronizar_inventario
        self.sim.pos[6]=list(r.POSICION_FINAL)
        original=self.file.read_bytes()
        datos=sincronizar_inventario(self.sim,self.c.portico.coordenadas,self.file)
        self.assertEqual(datos['resumen']['celdas_ocupadas'],0)
        self.assertFalse(datos['celdas'][0]['ocupada'])
        respaldos=list(self.file.parent.glob('inventario_antes_sincronizacion_*.json'))
        self.assertEqual(len(respaldos),1)
        self.assertEqual(respaldos[0].read_bytes(),original)

    def test_sincroniza_sobre_real_sin_registro_previo(self):
        from sincronizacion_escena import sincronizar_inventario
        inv.liberar_celda(1,self.file)
        datos=sincronizar_inventario(self.sim,self.c.portico.coordenadas,self.file)
        self.assertEqual(datos['celdas'][0]['qr'],'AS-001')
        self.assertEqual(datos['resumen']['celdas_ocupadas'],1)

    def test_sincronizacion_no_modifica_datos_si_hay_sobre_sujeto(self):
        from sincronizacion_escena import sincronizar_inventario
        self.sim.parent[6]=5
        alias_original=self.sim.getObjectAlias
        original=self.file.read_bytes()
        with patch.object(self.sim,'getObjectAlias',side_effect=lambda h:'Gripper' if h==5 else alias_original(h)):
            with self.assertRaisesRegex(RuntimeError,'sigue sujeto'):
                sincronizar_inventario(self.sim,self.c.portico.coordenadas,self.file)
        self.assertEqual(self.file.read_bytes(),original)

    def test_sincronizacion_rechaza_posicion_desconocida(self):
        from sincronizacion_escena import sincronizar_inventario
        self.sim.pos[6]=[100,100,100]
        original=self.file.read_bytes()
        with self.assertRaisesRegex(RuntimeError,'ubicación conocida'):
            sincronizar_inventario(self.sim,self.c.portico.coordenadas,self.file)
        self.assertEqual(self.file.read_bytes(),original)

    def test_sincronizacion_estable_no_reescribe_json(self):
        from sincronizacion_escena import sincronizar_inventario
        sincronizar_inventario(self.sim,self.c.portico.coordenadas,self.file)
        original=self.file.read_bytes()
        n=len(list(self.file.parent.glob('inventario_antes_sincronizacion_*.json')))
        sincronizar_inventario(self.sim,self.c.portico.coordenadas,self.file)
        self.assertEqual(self.file.read_bytes(),original)
        self.assertEqual(len(list(self.file.parent.glob('inventario_antes_sincronizacion_*.json'))),n)

    def test_qr_duplicado_no_confirma_lectura(self):
        self.plc.d.update(Operacion_Actual=1,SOBRE_POS_FINAL=True)
        self.c.bridge=SimpleNamespace(sensor_qr=123)
        with patch.object(main,'leer_qr',return_value='AS-001'),patch.object(main,'validar_inventario',lambda:inv.validar_inventario(self.file)):
            with self.assertRaisesRegex(RuntimeError,'QR duplicado'):self.c._qr()
        self.assertFalse(self.plc.d.get('QR_DETECTADO',False))
        self.assertIsNone(self.c.qr_actual)

    def test_qr_invalido_no_confirma(self):
        self.plc.d.update(Operacion_Actual=1,SOBRE_POS_FINAL=True)
        self.c.bridge=SimpleNamespace(sensor_qr=123)
        with patch.object(main,'leer_qr',return_value='CODIGO-INVALIDO'):
            with self.assertRaisesRegex(RuntimeError,'QR inválido'):self.c._qr()
        self.assertFalse(self.plc.d.get('QR_DETECTADO',False))

    def test_lector_sin_qr_permanece_a_la_espera(self):
        self.plc.d.update(Operacion_Actual=1,SOBRE_POS_FINAL=True)
        self.c.bridge=SimpleNamespace(sensor_qr=123)
        with patch.object(main,'leer_qr',return_value=None):self.c._qr()
        self.assertFalse(self.plc.d.get('QR_DETECTADO',False))
        self.assertFalse(self.c.portico.en_movimiento())

    def test_rack_lleno_no_sobrescribe_inventario(self):
        datos=inv.validar_inventario(self.file)
        for celda in datos['celdas']:celda.update(ocupada=True,qr=f"AS-{celda['indice']:03}")
        self.file.write_text(json.dumps(datos),encoding='utf-8')
        antes=self.file.read_bytes()
        self.assertIsNone(inv.buscar_celda_libre(self.file))
        with self.assertRaisesRegex(ValueError,'ocupada'):inv.marcar_deposito_confirmado('AS-999',112,self.file)
        self.assertEqual(self.file.read_bytes(),antes)

    def test_corte_comunicacion_detiene_movimiento(self):
        self.orden('Ir_Almacenamiento',terminar=False)
        self.c.heartbeat=0
        self.c.manual_anterior=0
        self.plc.d['Comunicacion_OK']=False
        self.c.ciclo()
        self.assertFalse(self.c.portico.en_movimiento())
        self.assertFalse(self.plc.d.get('Pos_Almacenamiento',False))
        self.assertIsNotNone(inv.buscar_qr('AS-001',self.file))

    def test_reinicio_escena_invalida_movimiento_anterior(self):
        self.orden('Ir_Almacenamiento',terminar=False)
        self.assertTrue(self.c.portico.en_movimiento())
        self.c._tiempo_escena=50.0
        with self.assertRaisesRegex(RuntimeError,'Escena reiniciada'):
            self.c._verificar_escena()
        self.assertFalse(self.c.portico.en_movimiento())

if __name__=='__main__':unittest.main(verbosity=2)
