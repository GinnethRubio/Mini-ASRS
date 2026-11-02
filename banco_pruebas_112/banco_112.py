"""Cobertura del controlador Python sin conexiones, y registro OPC UA de solo lectura.

python banco_112.py modelo
python banco_112.py registrar
python banco_112.py campana --estrategia 0 --confirmar-movimientos --inicio-runtime auto

Por defecto usa la carpeta del repositorio, guarda los logs en logs_112/ y se conecta al
runtime CODESYS del mismo equipo (ASRS_OPC_URL permite indicar otro servidor).
"""
import argparse
import contextlib
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import statistics
import sys
import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch


def fecha():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def verificar_estrategia_plc(plc, ns, estrategia):
    if estrategia != 2:
        return
    try:
        nodo = plc.client.get_node(f'ns={ns};s=|var|CODESYS Control Win V3.Application.GVL_HMI.iVersionEstrategias')
        version = int(nodo.get_value())
    except Exception as exc:
        raise RuntimeError('Estrategia 2 requiere PLC actualizado y simbolo iVersionEstrategias publicado') from exc
    if version != 2:
        raise RuntimeError('Version de estrategias PLC incompatible')


class Registro:
    def __init__(self, raiz, modo, proyecto, parametros):
        self.path = raiz / (datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '_' + modo)
        self.path.mkdir(parents=True, exist_ok=False)
        self.f = (self.path/'eventos.jsonl').open('w', encoding='utf-8', buffering=1)
        self.modo = modo
        archivos = {}
        fuentes = sorted([*(proyecto/'coordinador').rglob('*.py'), *(proyecto/'datos').glob('*.xlsx'),
                          *(proyecto/'datos').glob('*.json')])
        for p in fuentes:
            if '__pycache__' not in p.parts:
                contenido = p.read_bytes()
                archivos[p.name] = hashlib.sha256(contenido).hexdigest()
                dest = self.path/'fuentes'/p.name
                dest.parent.mkdir(exist_ok=True)
                dest.write_bytes(contenido)
        for p in Path(__file__).parent.glob('*.py'):
            dest=self.path/'fuentes_banco'/p.name
            dest.parent.mkdir(exist_ok=True)
            dest.write_bytes(p.read_bytes())
        self.json('metadatos.json', dict(inicio=fecha(), modo=modo,
            proyecto=str(proyecto), parametros=parametros, sha256=archivos,
            escribe_plc=modo=='campana', conecta_coppelia=False,
            alcance=('Controlador Python real con PLC/escena dobles de prueba; tiempos virtuales. '
                     'No valida PLC, FluidSIM, QR óptico, colisiones ni sensores reales.'
                     if modo=='modelo' else 'OPC UA; campaña usa START/STOP y solicitudes HMI; registrar solo lee. Sin inyección de fallas conectadas.')))
        self.emitir('inicio')

    def emitir(self, tipo, **datos):
        self.f.write(json.dumps({'fecha_utc':fecha(), 'tipo':tipo, 'modo':self.modo, **datos},
                                ensure_ascii=False, default=str)+'\n')

    def json(self, nombre, valor):
        temporal = self.path/(nombre+'.tmp')
        temporal.write_text(json.dumps(valor, ensure_ascii=False, indent=2), encoding='utf-8')
        if nombre=='continuar_campana.json':
            with temporal.open('r+b') as archivo:
                archivo.flush();os.fsync(archivo.fileno())
        for intento in range(8):
            try:
                temporal.replace(self.path/nombre)
                return
            except PermissionError:
                time.sleep(.02*(intento+1))
        # Un lector Windows puede mantener abierto el destino sin permitir renombrarlo.
        # Conservar una instantánea alternativa; un panel de progreso no debe parar el PLC.
        alternativo=self.path/(Path(nombre).stem+'_'+datetime.now().strftime('%H%M%S_%f')+'.json')
        temporal.replace(alternativo)
        self.emitir('instantanea_alternativa',archivo=nombre,guardado_como=alternativo.name)

    def cerrar(self):
        self.emitir('fin')
        self.f.flush()
        os.fsync(self.f.fileno())
        self.f.close()


def estadisticas(path, filas, tiempo, distancia):
    grupos={}
    for r in filas:
        if r.get('resultado','correcto')!='correcto':continue
        grupos.setdefault((r['estrategia'],r['operacion']),[]).append(r)
    with (path/'estadisticas.csv').open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.writer(f);w.writerow(['estrategia','operacion','n','tiempo_promedio_s','desv_tiempo_s',
                                   'distancia_promedio_m','operaciones_hora_derivadas'])
        for (estrategia,op),rs in sorted(grupos.items()):
            ts=[float(r[tiempo]) for r in rs];media=statistics.mean(ts)
            escala=.001 if distancia.endswith('_mm') else 1
            w.writerow([estrategia,op,len(rs),media,statistics.stdev(ts) if len(ts)>1 else 0,
                        statistics.mean(float(r[distancia])*escala for r in rs),3600/media if media else ''])


def modelo(args, log):
    os.environ['ASRS_TEST_PROJECT'] = str(args.proyecto)
    import pruebas_fallas as base
    import main as controlador
    import inventario as inv
    import recuperacion as rec
    from control_portico import ControlPortico, ARCHIVO_COORDENADAS
    from estrategias_colocacion import ordenar_celdas
    coords = ControlPortico._cargar_coordenadas(ARCHIVO_COORDENADAS)
    if set(coords) != {(c, f) for c in range(1,5) for f in range(1,29)}:
        raise RuntimeError('El Excel no contiene las 112 celdas únicas esperadas')
    # Libro congelado para toda la campaña: se lee una sola vez, como main.py.
    lectura_excel = ControlPortico._cargar_coordenadas
    ControlPortico._cargar_coordenadas = staticmethod(lambda archivo: {k:dict(v) for k,v in coords.items()})
    campos = ['repeticion','estrategia','columna','fila','operacion','resultado',
              'tiempo_virtual_s','distancia_xz_m','distancia_xyz_m','error']
    operaciones = []
    with (log.path/'operaciones_modelo.csv').open('w', newline='', encoding='utf-8-sig') as archivo:
        w = csv.DictWriter(archivo, fieldnames=campos); w.writeheader()
        for repeticion in range(1,args.repeticiones+1):
            for estrategia in (0,1,2):
                # Orden de llenado de cada estrategia, con el mismo desempate por índice que el PLC.
                por_indice = {(c-1)*28+f:v for (c,f),v in coords.items()}
                orden = [((i-1)//28+1, (i-1)%28+1) for i in ordenar_celdas(por_indice, estrategia)]
                for n, (col, fila) in enumerate(orden):
                    # Las celdas anteriores del orden quedan ocupadas: el destino del PLC doble es
                    # el que corresponde a la estrategia y main.py lo verifica cuando es la 2.
                    previas = {(c,f):k for k,(c,f) in enumerate(orden[:n], 1)}
                    t = base.Tests('test_ciclo_completo_y_registro_al_final')
                    salida = io.StringIO()
                    try:
                        with contextlib.redirect_stdout(salida):
                            t.setUp()
                            datos=inv.validar_inventario(t.file)
                            for celda in datos['celdas']:
                                k=previas.get((int(celda['columna']),int(celda['fila'])))
                                celda.update(ocupada=k is not None,qr=f'AS-{900+k}' if k else '')
                            t.file.write_text(json.dumps(datos),encoding='utf-8')
                            patch.object(controlador,'validar_inventario',lambda:inv.validar_inventario(t.file)).start()
                            patch.object(controlador,'marcar_deposito_confirmado',lambda qr,i:inv.marcar_deposito_confirmado(qr,i,t.file)).start()
                            t.plc.d.update(Columna_Destino=col,Fila_Destino=fila,Estrategia=estrategia)
                            t.c.qr_actual='AS-001'
                            t.c.banda=SimpleNamespace(sobre=6,qr='AS-001',_moviendo=False)
                            t.sim.pos[6]=list(rec.POSICION_FINAL)
                            t.sim.removeObjects=lambda hs:[t.sim.pos.pop(h,None) for h in hs]
                            for op, secuencia in (
                                ('almacenar',('Y_Retraer','G_Soltar','Ir_Recogida','Y_Extender','G_Ajustar',
                                             'Y_Retraer','Ir_Almacenamiento','Y_Extender','Z_Ajuste','G_Soltar','Y_Retraer')),
                                ('recuperar',('Y_Retraer','G_Soltar','Ir_Almacenamiento','Y_Extender','G_Ajustar',
                                             'Y_Retraer','Ir_Despacho','Y_Extender','Z_Ajuste','G_Soltar','Y_Retraer'))):
                                t.plc.d['Operacion_Actual']=1 if op=='almacenar' else 2
                                t.plc.d['Deposito_Confirmado']=False
                                inicio=t.now; dxz=dxyz=0.0
                                row=dict(repeticion=repeticion,estrategia=estrategia,columna=col,fila=fila,
                                         operacion=op,resultado='fallo',error='')
                                try:
                                    for orden_nombre in secuencia:
                                        log.emitir('orden_modelo',celda=[col,fila],operacion=op,
                                                   orden=orden_nombre,t_virtual=t.now)
                                        for k in controlador.ORDENES:t.plc.d[k]=k==orden_nombre
                                        t.c._ordenes()
                                        pasos=0
                                        while t.c.portico.en_movimiento():
                                            antes=[t.sim.j[h] for h in (1,2,3)]
                                            t.now+=.05; t.c.portico.avanzar(); pasos+=1
                                            delta=[abs(t.sim.j[h]-v) for h,v in zip((1,2,3),antes)]
                                            dxz+=delta[0]+delta[2];dxyz+=sum(delta)
                                            if pasos%4==0:
                                                log.emitir('muestra_modelo',celda=[col,fila],operacion=op,
                                                    t_virtual=t.now,x=t.sim.j[1],y=t.sim.j[2],z=t.sim.j[3])
                                            if pasos>2400:raise TimeoutError('Movimiento excede 120 segundos virtuales')
                                    if not t.plc.d['Deposito_Confirmado']:
                                        raise AssertionError('Falta confirmación final')
                                    registro=inv.buscar_qr('AS-001',t.file)
                                    if op=='almacenar':
                                        if registro is None or (registro['columna'],registro['fila'])!=(col,fila):
                                            raise AssertionError('Inventario no corresponde al destino')
                                        c=coords[col,fila]
                                        esperado=[rec.POSICION_FINAL[0]+c['x']+.262,rec.POSICION_FINAL[1],
                                                  rec.POSICION_FINAL[2]+c['z_salida']+.675]
                                    else:
                                        if registro is not None:raise AssertionError('Inventario no liberado')
                                        esperado=rec.POS_SOBRE_DESPACHO
                                    if t.sim.parent[6]!=-1 or max(abs(a-b) for a,b in zip(t.sim.pos[6],esperado))>.002:
                                        raise AssertionError('Posición física o parentesco incorrectos')
                                    if op=='recuperar':
                                        t.now+=4.1;t.c.recuperacion.salida.ciclo()
                                        if 6 in t.sim.pos:raise AssertionError('Sobre no desapareció de banda dos')
                                    row['resultado']='correcto'
                                except Exception as e:
                                    row['error']=repr(e)
                                row.update(tiempo_virtual_s=round(t.now-inicio,3),
                                           distancia_xz_m=round(dxz,6),distancia_xyz_m=round(dxyz,6))
                                operaciones.append(row);w.writerow(row);archivo.flush()
                                log.emitir('operacion_modelo',**row)
                                if row['resultado']!='correcto':break
                    finally:
                        t.doCleanups()
                        if salida.getvalue():log.emitir('stdout_controlador',texto=salida.getvalue())
                print(f'Repetición {repeticion}, estrategia {estrategia}: cobertura terminada',flush=True)
    # Fallas exclusivamente en dobles y JSON temporales: mismas defensas del controlador.
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(base.Tests)
    class Resultado(unittest.TextTestResult):
        def addSuccess(self,test):
            super().addSuccess(test);log.emitir('prueba_falla',caso=test.id(),resultado='correcto')
        def addFailure(self,test,err):
            super().addFailure(test,err);log.emitir('prueba_falla',caso=test.id(),resultado='fallo',error=str(err[1]))
        def addError(self,test,err):
            super().addError(test,err);log.emitir('prueba_falla',caso=test.id(),resultado='error',error=str(err[1]))
    with (log.path/'pruebas_fallas.txt').open('w',encoding='utf-8') as f, contextlib.redirect_stdout(f):
        resultado=unittest.TextTestRunner(stream=f,verbosity=2,resultclass=Resultado).run(suite)
    correctas=[r for r in operaciones if r['resultado']=='correcto']
    resumen=dict(modo='modelo',operaciones=len(operaciones),esperadas=672*args.repeticiones,
                 correctas=len(correctas),fallos=len(operaciones)-len(correctas),
                 pruebas_defensas=resultado.testsRun,defensas_correctas=resultado.wasSuccessful(),
                 celdas_almacenadas=len({(r['columna'],r['fila']) for r in correctas if r['operacion']=='almacenar'}),
                 celdas_recuperadas=len({(r['columna'],r['fila']) for r in correctas if r['operacion']=='recuperar'}),
                 advertencia='Tiempos virtuales del controlador, sin esperas de PLC/FluidSIM/QR. No son resultados experimentales conectados. Cada celda se prueba con las anteriores del orden de la estrategia marcadas como ocupadas en el JSON; la escena doble solo contiene el sobre en curso.')
    log.json('resumen.json',resumen)
    estadisticas(log.path,operaciones,'tiempo_virtual_s','distancia_xz_m')
    ControlPortico._cargar_coordenadas = staticmethod(lectura_excel)
    return 0 if len(correctas)==672*args.repeticiones and resultado.wasSuccessful() else 1


class Campana:
    """Llenar y recuperar, con paro normal al final de cada extracción.

    No escribe confirmaciones, inventario, IN/OUT, heartbeat ni rearme.
    El PLC y main.py conservan la ejecución y las protecciones.
    """
    def __init__(self, escribir, log, estrategia, reloj=time.monotonic, continuar=False, espera_comunicacion_s=300):
        self.escribir=escribir;self.log=log;self.estrategia=estrategia;self.reloj=reloj
        # Con FluidSIM sin permisos el PLC congela el paso; se espera su regreso sin mandar órdenes.
        self.espera_comunicacion_s=espera_comunicacion_s;self.pausa_desde=None
        self.fase='preparar';self.iniciada=False;self.cola=[];self.paro_enviado=False
        self.base_alm=0;self.base_rec=0;self.ultimo_progreso=reloj();self.firma=None
        self.esperar_hasta=0
        self.continuar=continuar

    def mandar(self,n,v):
        self.escribir(n,v);self.log.emitir('mando_campana',simbolo=n,valor=v)

    def avanzar(self,d):
        h=lambda n:d['GVL_HMI.'+n]
        firma=(d['GVL.Paso_Actual'],h('iOperaciones'),h('iAlmacenados'),h('iRecuperados'))
        if firma!=self.firma:self.firma=firma;self.ultimo_progreso=self.reloj()
        if d['GVL.Paso_Actual']==999 or d['GVL.Python_Error']:raise RuntimeError('Alarma: campaña detenida sin rearme automático')
        if not d['GVL.Comunicacion_OK'] or int(d['GVL.IN1']) & 28 != 28:
            if self.pausa_desde is None:
                self.pausa_desde=self.reloj()
                self.log.emitir('pausa_comunicacion',comunicacion_ok=d['GVL.Comunicacion_OK'],in1=int(d['GVL.IN1']))
            if self.reloj()-self.pausa_desde>self.espera_comunicacion_s:
                raise RuntimeError(f'Comunicación o permisos FluidSIM ausentes por más de {self.espera_comunicacion_s:.0f} s')
            self.ultimo_progreso=self.reloj()
            return False
        if self.pausa_desde is not None:
            self.log.emitir('comunicacion_restablecida',duracion_s=round(self.reloj()-self.pausa_desde,3))
            self.pausa_desde=None
        if self.reloj()-self.ultimo_progreso>90:raise TimeoutError('90 s sin avance; revisar PLC, Python y FluidSIM')
        if h('xModoManual') or h('xModoPaso') or h('xSimularCoppelia'):
            raise RuntimeError('La campaña requiere automático continuo y CoppeliaSim real')
        if self.fase=='preparar':
            if d['GVL.Paso_Actual']!=0 or h('xMarcha'):raise RuntimeError('Iniciar campaña con PLC en espera y detenido')
            if len(h('axOcupada'))!=112 or len(h('asCodigo'))!=112:raise RuntimeError('Inventario HMI inválido')
            n_inicial=sum(bool(x) for x in h('axOcupada'))
            if (n_inicial or any(h('asCodigo'))) and not self.continuar:raise RuntimeError('La campaña completa requiere rack vacío; no se vacía automáticamente')
            if self.continuar:
                if n_inicial==112:raise RuntimeError('Continuación con rack lleno requiere revisar primero el plan de recuperación')
                codigos=[qr for ocupada,qr in zip(h('axOcupada'),h('asCodigo')) if ocupada]
                if len(codigos)!=len(set(codigos)) or not all(codigos):raise RuntimeError('QR iniciales inválidos')
            if not d['GVL.Inventario_Listo'] or not d['GVL.Maquina_Lista']:raise RuntimeError('main.py no está listo')
            self.base_alm=int(h('iAlmacenados'))-n_inicial;self.base_rec=int(h('iRecuperados'))
            self.mandar('GVL_HMI.iEstrategia',self.estrategia)
            self.iniciada=True;self.mandar('GVL.START',True);self.fase='llenando'
        elif self.fase=='llenando':
            n=int(h('iAlmacenados'))-self.base_alm
            if n>112:raise RuntimeError('Se excedió el número de depósitos esperado')
            # Solicitar STOP durante el último ciclo permite acabarlo y evita un sobre 113.
            if not self.paro_enviado and n>=111 and h('iOperacionActual')==1 and d['GVL.Paso_Actual']>=40:
                self.mandar('GVL.STOP',True);self.paro_enviado=True
            if n==112 and d['GVL.Paso_Actual']==0 and not h('xMarcha'):
                if not all(h('axOcupada')) or len(set(h('asCodigo')))!=112 or not all(h('asCodigo')):
                    raise RuntimeError('No hay 112 QR únicos al terminar el llenado')
                self.cola=list(h('asCodigo'));self.fase='siguiente';self.paro_enviado=False
                self.log.emitir('rack_lleno',codigos=self.cola)
        elif self.fase=='siguiente':
            if d['GVL.Paso_Actual']!=0 or h('xMarcha'):return False
            if self.reloj()<self.esperar_hasta:return False
            if not self.cola:
                if any(h('axOcupada')) or int(h('iRecuperados'))-self.base_rec!=112:
                    raise RuntimeError('La recuperación completa no dejó rack vacío')
                self.fase='completa';self.log.emitir('campana_completa',almacenados=112,recuperados=112)
                return True
            self.qr=self.cola[0];self.cuenta_rec=int(h('iRecuperados'))
            self.mandar('GVL_HMI.sCodigoRecuperar',self.qr)
            self.mandar('GVL_HMI.xSolicitudRecuperar',True)
            self.mandar('GVL.START',True);self.paro_enviado=False;self.fase='extrayendo'
        elif self.fase=='extrayendo':
            if not self.paro_enviado and h('iOperacionActual')==2 and d['GVL.Paso_Actual']>=40:
                self.mandar('GVL.STOP',True);self.paro_enviado=True
            n=int(h('iRecuperados'))
            if n>self.cuenta_rec+1:raise RuntimeError('Recuperaciones ajenas a la campaña')
            if n==self.cuenta_rec+1 and d['GVL.Paso_Actual']==0 and not h('xMarcha'):
                if self.qr in h('asCodigo'):raise RuntimeError('QR no fue retirado del inventario')
                self.cola.pop(0);self.fase='siguiente';self.esperar_hasta=self.reloj()+5
        return False


def registrar(args, log):
    from sesiones_runtime import VentanaRuntime, punto_continuacion, restaurar
    ventana=None
    if args.modo=='campana':
        if args.inicio_runtime=='auto':
            from sesiones_runtime import detectar_inicio_runtime
            args.inicio_runtime=detectar_inicio_runtime()
            log.emitir('inicio_runtime_detectado',inicio_runtime=args.inicio_runtime)
        ventana=VentanaRuntime(args.inicio_runtime,limite_min=getattr(args,'limite_runtime_min',60),
                               margen_min=args.margen_runtime_min,bloque_min=args.bloque_min)
        log.emitir('presupuesto_runtime',inicio_runtime=args.inicio_runtime,
                   paro_preventivo_utc=ventana.hora_paro,motivo=ventana.motivo)
        print('Paro preventivo previsto:',ventana.hora_paro,flush=True)
    from codesys_opcua import CodesysOPCUA
    plc=CodesysOPCUA(url=args.opc_url).conectar()
    ns=next(iter(plc.nodes.values())).nodeid.NamespaceIndex
    nombres=['GVL.Paso_Actual','GVL.Python_Error','GVL.Python_Mensaje','GVL.Comunicacion_OK',
             'GVL.PYTHON_Heartbeat','GVL.EJE_X_POSICION','GVL.EJE_Z_POSICION','GVL.IN0','GVL.IN1','GVL.OUT0',
             'GVL_HMI.iOperaciones','GVL_HMI.iAlmacenados','GVL_HMI.iRecuperados','GVL_HMI.iFallos',
             'GVL_HMI.iOperacionActual','GVL_HMI.sCodigoActual','GVL_HMI.iColumnaActual','GVL_HMI.iFilaActual',
             'GVL_HMI.iEstrategia','GVL_HMI.rTiempoCicloUltimo_s','GVL_HMI.rDistanciaUltima_mm',
             'GVL_HMI.axOcupada','GVL_HMI.asCodigo','GVL_HMI.uLogId','GVL_HMI.asLog',
             'GVL_HMI.asHistorial','GVL_HMI.sMensaje','GVL_HMI.xModoManual','GVL_HMI.xModoPaso',
             'GVL_HMI.xSimularCoppelia','GVL_HMI.xMarcha','GVL.Inventario_Listo','GVL.Maquina_Lista',
             'GVL.Y_ESTADO_VALIDO','GVL.EJE_Y_ESTADO','GVL.GRIPPER_ESTADO_VALIDO','GVL.GRIPPER_ESTADO']
    nodos=[plc.client.get_node(f'ns={ns};s=|var|CODESYS Control Win V3.Application.'+n) for n in nombres]
    previo=None;ultimo_id=None;activa=None;inicio=time.monotonic();muestras=0;perdidas=0
    cobertura={'almacenar':set(),'recuperar':set()};ops=[];estado='interrumpido';iniciales=[]
    def escribir(n,v):
        from opcua import ua
        permitidos={'GVL.START','GVL.STOP','GVL_HMI.iEstrategia','GVL_HMI.sCodigoRecuperar','GVL_HMI.xSolicitudRecuperar'}
        if n not in permitidos:raise RuntimeError('Escritura no permitida: '+n)
        nodo=plc.client.get_node(f'ns={ns};s=|var|CODESYS Control Win V3.Application.'+n)
        nodo.set_value(ua.Variant(v,nodo.get_data_type_as_variant_type()))
    campana=Campana(escribir,log,args.estrategia,continuar=args.continuar,
                    espera_comunicacion_s=getattr(args,'espera_comunicacion_s',300)) if args.modo=='campana' else None
    if campana is not None:
        verificar_estrategia_plc(plc, ns, args.estrategia)
    punto_previo=json.loads(args.reanudar.read_text(encoding='utf-8')) if getattr(args,'reanudar',None) else None
    def get(d,n):return d['GVL_HMI.'+n]
    try:
        with (log.path/'operaciones_observadas.csv').open('w',newline='',encoding='utf-8-sig') as f:
            campos=['fecha_utc','monotonic_s','contador','operacion','codigo','columna','fila','estrategia',
                    'tiempo_plc_s','distancia_plc_mm','verificacion_inventario']
            w=csv.DictWriter(f,fieldnames=campos);w.writeheader()
            while args.duracion==0 or time.monotonic()-inicio<args.duracion:
                tic=time.monotonic()
                valores=plc.client.get_values(nodos)
                d=dict(zip(nombres,valores));muestras+=1
                log.emitir('muestra_opc',duracion_lectura_s=time.monotonic()-tic,valores=d)
                eventos=[]
                for linea in get(d,'asLog'):
                    try:i=int(str(linea).split(';',1)[0])
                    except ValueError:continue
                    eventos.append((i,linea))
                eventos.sort()
                actual_id=int(get(d,'uLogId'))
                if ultimo_id is not None and actual_id<ultimo_id:
                    raise RuntimeError('Contador de eventos reiniciado: iniciar otra sesión de registro')
                nuevos=[e for e in eventos if ultimo_id is None or e[0]>ultimo_id]
                if ultimo_id is not None and actual_id>ultimo_id and not nuevos:
                    faltan=actual_id-ultimo_id;perdidas+=faltan
                    log.emitir('hueco_log_plc',eventos_perdidos=faltan,causa='buffer vacío o borrado')
                if ultimo_id is not None and nuevos and nuevos[0][0]>ultimo_id+1:
                    faltan=nuevos[0][0]-ultimo_id-1;perdidas+=faltan
                    log.emitir('hueco_log_plc',eventos_perdidos=faltan)
                for i,linea in nuevos:log.emitir('evento_plc',id=i,linea=linea)
                ultimo_id=actual_id
                if previo is not None:
                    n=int(get(d,'iOperaciones'));anterior=int(get(previo,'iOperaciones'))
                    if n<anterior:raise RuntimeError('Contador de operaciones reiniciado')
                    if n>anterior:
                        if n-anterior!=1 or activa is None:
                            raise RuntimeError('Operaciones omitidas o inicio no observado; sesión incompleta')
                        col=int(activa['iColumnaActual']);fila=int(activa['iFilaActual']);idx=(col-1)*28+fila-1
                        if not (1<=col<=4 and 1<=fila<=28):raise RuntimeError('Destino observado fuera de rango')
                        op='almacenar' if activa['iOperacionActual']==1 else 'recuperar'
                        qr=activa['sCodigoActual'];ocupadas=get(d,'axOcupada');codigos=get(d,'asCodigo')
                        if len(ocupadas)!=112 or len(codigos)!=112:raise RuntimeError('Arrays HMI de tamaño incorrecto')
                        ok=(bool(ocupadas[idx]) and codigos[idx]==qr) if op=='almacenar' else (not ocupadas[idx] and not codigos[idx])
                        row=dict(fecha_utc=fecha(),monotonic_s=round(time.monotonic(),3),contador=n,operacion=op,codigo=qr,columna=col,fila=fila,
                                 estrategia=activa['iEstrategia'],tiempo_plc_s=get(d,'rTiempoCicloUltimo_s'),
                                 distancia_plc_mm=get(d,'rDistanciaUltima_mm'),verificacion_inventario=bool(ok))
                        w.writerow(row);f.flush();ops.append(row);log.emitir('operacion_observada',**row)
                        if ok:cobertura[op].add((col,fila))
                        activa=None
                        if not ok:raise RuntimeError('Registro PLC e inventario no coinciden; revisar muestras antes de aceptar')
                if int(get(d,'iOperacionActual')) in (1,2):
                    candidata={k:get(d,k) for k in ('iOperacionActual','sCodigoActual','iColumnaActual','iFilaActual','iEstrategia')}
                    # En recuperación el destino de despacho publica columna/fila 0.
                    # Conservar la última celda válida del origen hasta el registro final.
                    if (1<=int(candidata['iColumnaActual'])<=4 and 1<=int(candidata['iFilaActual'])<=28) or activa is None:
                        activa=candidata
                previo=d
                log.json('avance.json',dict(muestras=muestras,operaciones=len(ops),
                    cobertura={k:len(v) for k,v in cobertura.items()},eventos_perdidos=perdidas,ultima_muestra=fecha(),
                    paro_preventivo_utc=ventana.hora_paro if ventana else None,
                    cierre_solicitado=ventana.pausa if ventana else False))
                if d['GVL.Paso_Actual']==999 or d['GVL.Python_Error']:
                    log.emitir('alarma_observada',mensaje=d['GVL.Python_Mensaje'],historial=get(d,'asHistorial'))
                if campana is not None and campana.fase=='preparar':
                    if ventana.reloj()>=ventana.parar_en:
                        raise RuntimeError('Se consumió la ventana durante la preparación; no se envía START')
                    from inventario import validar_inventario
                    datos=validar_inventario(args.proyecto/'datos'/'inventario_mini_asrs.json')
                    ordenadas=sorted(datos['celdas'],key=lambda c:int(c['indice']))
                    if ([bool(c['ocupada']) for c in ordenadas]!=get(d,'axOcupada') or
                        [str(c.get('qr') or '') for c in ordenadas]!=get(d,'asCodigo')):
                        raise RuntimeError('JSON y HMI difieren; reiniciar/sincronizar antes de probar')
                    if punto_previo is not None:
                        # Las comprobaciones de permisos se hacen antes de cualquier START.
                        if (d['GVL.Paso_Actual']==999 or d['GVL.Python_Error'] or not d['GVL.Comunicacion_OK']
                            or int(d['GVL.IN1']) & 28 != 28
                            or any(get(d,n) for n in ('xModoManual','xModoPaso','xSimularCoppelia'))):
                            raise RuntimeError('No se puede reanudar sin permisos y automático continuo')
                        cobertura=restaurar(campana,punto_previo,d)
                        log.emitir('campana_reanudada',punto=str(args.reanudar),sesion_anterior=punto_previo['log_anterior'])
                    elif args.continuar:
                        for i,ocupada in enumerate(get(d,'axOcupada')):
                            if ocupada:cobertura['almacenar'].add((i//28+1,i%28+1))
                        iniciales=sorted(cobertura['almacenar'])
                        log.emitir('inventario_inicial_continuacion',celdas=sorted(cobertura['almacenar']),
                            nota='Depósitos previos: cobertura inicial, sin inventar tiempos ni operaciones observadas.')
                if ventana is not None and ventana.actualizar(d,campana.mandar):
                    punto=punto_continuacion(campana,d,cobertura,log.path.resolve())
                    punto.update(motivo=ventana.motivo,inicio_runtime=args.inicio_runtime)
                    log.json('continuar_campana.json',punto)
                    estado=ventana.motivo
                    log.emitir('bloque_cerrado',punto='continuar_campana.json',motivo=estado)
                    print('BLOQUE CERRADO. Continuación:',(log.path/'continuar_campana.json').resolve(),flush=True)
                    break
                if ventana is not None and ventana.pausa:
                    # Seguir registrando el último ciclo sin emitir más órdenes de producción.
                    time.sleep(max(0,args.intervalo-(time.monotonic()-tic)))
                    continue
                if campana is not None and campana.avanzar(d):
                    if any(len(v)!=112 for v in cobertura.values()):
                        raise RuntimeError('La campaña terminó pero falta cobertura observada de las 112 celdas')
                    estado='campana_completa';break
                time.sleep(max(0,args.intervalo-(time.monotonic()-tic)))
            if estado not in ('campana_completa','pausa_runtime','pausa_bloque'):estado='duracion_cumplida'
    except KeyboardInterrupt:
        estado='fin_por_usuario'
    except Exception as e:
        estado='error';log.emitir('error_registrador',error=repr(e))
        if campana is not None and campana.iniciada and campana.fase in ('llenando','siguiente','extrayendo'):
            from sesiones_runtime import estado_campana
            log.json('estado_interrumpido.json',estado_campana(campana,cobertura,log.path.resolve(),repr(e)))
        raise
    finally:
        if campana is not None and campana.iniciada and estado not in ('campana_completa','pausa_runtime','pausa_bloque'):
            try:campana.mandar('GVL.STOP',True)
            except Exception as e:log.emitir('paro_no_entregado',error=repr(e))
        try:plc.desconectar()
        except Exception as e:log.emitir('desconexion_incompleta',error=repr(e))
        estadisticas(log.path,ops,'tiempo_plc_s','distancia_plc_mm')
        log.json('resumen.json',dict(estado=estado,muestras=muestras,operaciones=len(ops),eventos_perdidos=perdidas,
            depositos_preexistentes_sin_metricas=iniciales,
            origen_continuacion=str(getattr(args,'reanudar',None)),
            cobertura={k:dict(cantidad=len(v),pendientes=[[c,f] for c in range(1,5) for f in range(1,29) if (c,f) not in v])
                       for k,v in cobertura.items()},
            nota='Cobertura según registros PLC y arrays HMI; no se verificó la geometría de la escena. Lecturas no atómicas; distancias/tiempos son los reportados por el PLC.'))
    return 3 if estado in ('pausa_runtime','pausa_bloque') else (0 if campana is None or estado=='campana_completa' else 2)


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('modo',choices=['modelo','registrar','campana'])
    raiz=Path(__file__).resolve().parent.parent
    ap.add_argument('--proyecto',type=Path,default=raiz)
    ap.add_argument('--salida',type=Path,default=raiz/'logs_112')
    ap.add_argument('--repeticiones',type=int,default=1)
    ap.add_argument('--opc-url',default=os.environ.get('ASRS_OPC_URL') or 'opc.tcp://localhost:4840')
    ap.add_argument('--intervalo',type=float,default=.2)
    ap.add_argument('--duracion',type=float,default=0,help='Segundos; 0 registra hasta Ctrl+C')
    ap.add_argument('--estrategia',type=int,choices=[0,1,2],default=0,
                    help='0 secuencial; 1 distancia; 2 tiempo (requiere PLC actualizado)')
    ap.add_argument('--confirmar-movimientos',action='store_true',help='Autoriza START/STOP y recuperación HMI en modo campaña')
    ap.add_argument('--continuar',action='store_true',help='Completar llenado desde inventario actual conservándolo; las celdas iniciales no tienen métricas nuevas')
    ap.add_argument('--inicio-runtime',help="Hora verificada de arranque del runtime, ISO 8601 con zona, o 'auto' para leerla del proceso CODESYS")
    ap.add_argument('--limite-runtime-min',type=float,default=60,help='Tiempo de uso continuo del runtime CODESYS en minutos (máximo 120)')
    ap.add_argument('--margen-runtime-min',type=float,default=10,help='Margen antes de la hora límite; mínimo 2 min')
    ap.add_argument('--bloque-min',type=float,default=45,help='Duración máxima de un bloque, además del límite del runtime')
    ap.add_argument('--espera-comunicacion-s',type=float,default=300,help='Pausa máxima esperando FluidSIM o la comunicación antes de cerrar el bloque')
    ap.add_argument('--reanudar',type=Path,help='continuar_campana.json de un bloque cerrado en reposo')
    args=ap.parse_args();args.proyecto=args.proyecto.resolve()
    if args.repeticiones<1 or args.intervalo<.05 or args.duracion<0:ap.error('Parámetros fuera de rango')
    if not (args.proyecto/'coordinador'/'main.py').is_file():ap.error('Proyecto inválido: falta coordinador/main.py')
    if args.modo=='campana' and not args.confirmar_movimientos:ap.error('campana requiere --confirmar-movimientos: mueve la simulación')
    if args.modo=='campana' and not args.inicio_runtime:ap.error('campana requiere --inicio-runtime con la hora verificada del runtime CODESYS')
    if args.reanudar and args.continuar:ap.error('Usar --reanudar o --continuar, no ambos')
    sys.path.insert(0,str(args.proyecto/'coordinador'))
    import rutas  # noqa: F401
    log=Registro(args.salida,args.modo,args.proyecto,{k:str(v) for k,v in vars(args).items()})
    print('Logs:',log.path.resolve(),flush=True)
    try:
        return modelo(args,log) if args.modo=='modelo' else registrar(args,log)
    except Exception as e:
        log.emitir('error_fatal',error=repr(e));print('ERROR:',e,file=sys.stderr);return 1
    finally:log.cerrar()


if __name__=='__main__':sys.exit(main())
