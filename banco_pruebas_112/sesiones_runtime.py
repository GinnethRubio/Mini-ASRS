"""Presupuesto temporal de CODESYS y continuación desde reposo confirmado."""
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
import time


PROCESOS_RUNTIME = ('codesyscontrolservice', 'codesyscontrol')


def detectar_inicio_runtime(procesos=None):
    """Hora de creación del proceso del runtime CODESYS, ISO 8601 con zona local.

    Se toma el proceso más reciente: el plazo de uso corre desde su último arranque.
    """
    import psutil
    candidatos=[]
    for p in (procesos if procesos is not None else psutil.process_iter(['name','create_time'])):
        nombre=(p.info.get('name') or '').lower()
        if nombre.startswith(PROCESOS_RUNTIME):candidatos.append(p.info['create_time'])
    if not candidatos:
        raise RuntimeError('No se encontró el proceso del runtime CODESYS; iniciarlo o indicar --inicio-runtime')
    return datetime.fromtimestamp(max(candidatos)).astimezone().isoformat(timespec='seconds')


def en_reposo(d):
    """PLC en espera, sin marcha ni operación, Y retraído y pinza abierta con sensores coherentes."""
    return (d['GVL.Paso_Actual']==0 and not d['GVL_HMI.xMarcha']
            and not d['GVL_HMI.iOperacionActual']
            and d.get('GVL.Y_ESTADO_VALIDO',False)
            and not d.get('GVL.EJE_Y_ESTADO',True)
            and d.get('GVL.GRIPPER_ESTADO_VALIDO',False)
            and not d.get('GVL.GRIPPER_ESTADO',True)
            and int(d['GVL.IN0']) & 15 == 5)


def listo_para_reanudar(d):
    """Coordinador preparado, permisos FluidSIM, automático continuo y actuadores en reposo."""
    return (en_reposo(d) and d['GVL.Maquina_Lista'] and d['GVL.Inventario_Listo']
            and d['GVL.Comunicacion_OK'] and not d['GVL.Python_Error']
            and int(d['GVL.IN1']) & 28 == 28
            and not any(d['GVL_HMI.'+n] for n in ('xModoManual','xModoPaso','xSimularCoppelia')))


def estado_campana(campana, cobertura, log_origen, motivo):
    """Plan de la campaña en el instante de una interrupción no planificada; no certifica reposo."""
    return dict(version=1,estado='interrumpido',fase=campana.fase,cola=list(campana.cola),
                estrategia=campana.estrategia,
                cobertura={k:[list(c) for c in sorted(v)] for k,v in cobertura.items()},
                campana_id=getattr(campana,'campana_id',str(log_origen)),
                log_anterior=str(log_origen),motivo=motivo,
                fecha_utc=datetime.now(timezone.utc).isoformat())


def punto_tras_reconexion(estado, d):
    """Punto de continuación a partir del plan interrumpido y del inventario resincronizado con la escena.

    El ciclo que estaba en curso no se cuenta: se toma lo que la escena muestra en el rack.
    """
    if estado.get('version')!=1 or estado.get('estado')!='interrumpido':raise ValueError('Estado interrumpido no válido')
    if estado['fase'] not in ('llenando','siguiente','extrayendo'):raise ValueError('Fase no continuable: '+estado['fase'])
    if not listo_para_reanudar(d):raise ValueError('PLC, coordinador o actuadores no están listos tras la reconexión')
    campana=SimpleNamespace(fase=estado['fase'],cola=estado['cola'],estrategia=estado['estrategia'],
                            campana_id=estado['campana_id'])
    cobertura={k:{tuple(c) for c in v} for k,v in estado['cobertura'].items()}
    punto=punto_continuacion(campana,d,cobertura,estado['log_anterior'])
    punto.update(motivo='reconexion_codesys',interrupcion=estado.get('motivo'))
    return punto


class VentanaRuntime:
    def __init__(self, inicio, limite_min=60, margen_min=10, bloque_min=45,
                 ahora=None, reloj=time.monotonic):
        self.reloj=reloj
        ahora=ahora or datetime.now(timezone.utc)
        inicio=datetime.fromisoformat(inicio.replace('Z','+00:00'))
        if inicio.tzinfo is None:raise ValueError('Inicio del runtime requiere zona horaria, por ejemplo -05:00')
        if not 0<margen_min<limite_min<=120 or bloque_min<=0 or margen_min<2:
            raise ValueError('Límite máximo 120 min y margen mínimo 2 min')
        edad=(ahora-inicio).total_seconds()
        if edad < -5:raise ValueError('Inicio del runtime está en el futuro')
        self.restantes=limite_min*60-max(0,edad)
        if self.restantes<=margen_min*60:
            raise ValueError('Runtime sin margen suficiente: restablecerlo y confirmar su nueva hora de inicio')
        hasta_paro=min(bloque_min*60,self.restantes-margen_min*60)
        self.motivo='pausa_runtime' if self.restantes-margen_min*60<=bloque_min*60 else 'pausa_bloque'
        self.t0=reloj();self.parar_en=self.t0+hasta_paro
        self.hora_paro=(ahora+timedelta(seconds=hasta_paro)).isoformat()
        self.pausa=False;self.listo_desde=None

    def actualizar(self, d, mandar):
        if not self.pausa and self.reloj()>=self.parar_en:
            mandar('GVL.STOP',True);self.pausa=True
        if not self.pausa:return False
        if self.reloj()-self.parar_en>120:
            raise TimeoutError('No se confirmó cierre del bloque en 120 s; revisar el sobre y no reanudar automáticamente')
        # No se considera un STOP enviado como una parada confirmada.
        if d['GVL.Paso_Actual']==999 or d['GVL.Python_Error']:
            raise RuntimeError('Alarma durante el cierre: no emitir punto de continuación válido')
        if not en_reposo(d):self.listo_desde=None;return False
        if self.listo_desde is None:self.listo_desde=self.reloj()
        # Permitir que banda dos termine de evacuar el último sobre.
        return self.reloj()-self.listo_desde>=5


def punto_continuacion(campana, d, cobertura, log_origen):
    ocupadas=list(d['GVL_HMI.axOcupada']);codigos=list(d['GVL_HMI.asCodigo'])
    if len(ocupadas)!=112 or len(codigos)!=112:raise ValueError('Inventario incompleto')
    presentes=[qr for o,qr in zip(ocupadas,codigos) if o]
    if len(presentes)!=len(set(presentes)) or not all(presentes):raise ValueError('QR inválidos')
    if any(qr for o,qr in zip(ocupadas,codigos) if not o):raise ValueError('QR en celda libre')
    fase=campana.fase
    if fase=='llenando':
        fase='siguiente' if all(ocupadas) else 'llenando'
        cola=codigos[:] if fase=='siguiente' else []
    elif fase in ('siguiente','extrayendo'):
        if not set(presentes).issubset(set(campana.cola)):raise ValueError('Sobres ajenos al plan de recuperación')
        fase='siguiente';cola=[qr for qr in campana.cola if qr in presentes]
    else:raise ValueError('Fase no continuable: '+fase)
    return dict(version=1,estado='reposo_confirmado',fase=fase,cola=cola,
                estrategia=campana.estrategia,ocupadas=ocupadas,codigos=codigos,
                cobertura={k:[list(c) for c in sorted(v)] for k,v in cobertura.items()},
                campana_id=getattr(campana,'campana_id',str(log_origen)),
                log_anterior=str(log_origen),fecha_utc=datetime.now(timezone.utc).isoformat(),
                nota='Punto entre ciclos. Validar escena y JSON antes de continuar; no reanuda movimientos interrumpidos.')


def restaurar(campana, punto, d):
    if punto.get('version')!=1 or punto.get('estado')!='reposo_confirmado':raise ValueError('Punto no válido')
    if punto['estrategia']!=campana.estrategia:raise ValueError('Estrategia distinta a la del punto')
    if punto['ocupadas']!=d['GVL_HMI.axOcupada'] or punto['codigos']!=d['GVL_HMI.asCodigo']:
        raise ValueError('Inventario actual no coincide con el punto: revisar escena y sincronización')
    if d['GVL.Paso_Actual']!=0 or d['GVL_HMI.xMarcha']:raise ValueError('Reanudar exclusivamente con PLC detenido en espera')
    if not d['GVL.Maquina_Lista'] or not d['GVL.Inventario_Listo']:raise ValueError('Coordinador no preparado')
    if (not d.get('GVL.Y_ESTADO_VALIDO',False) or d.get('GVL.EJE_Y_ESTADO',True)
        or not d.get('GVL.GRIPPER_ESTADO_VALIDO',False) or d.get('GVL.GRIPPER_ESTADO',True)
        or int(d['GVL.IN0']) & 15 != 5):raise ValueError('Y o gripper no están en reposo confirmado')
    fase=punto['fase']
    if fase not in ('llenando','siguiente'):raise ValueError('Fase guardada no válida')
    ocupacion=sum(bool(v) for v in punto['ocupadas'])
    if fase=='siguiente':
        presentes=[qr for o,qr in zip(punto['ocupadas'],punto['codigos']) if o]
        if sorted(punto['cola'])!=sorted(presentes):raise ValueError('Cola no coincide con inventario')
    cobertura={k:{tuple(c) for c in v} for k,v in punto['cobertura'].items()}
    if set(cobertura)!={'almacenar','recuperar'} or any(
        len(c)!=2 or not (1<=c[0]<=4 and 1<=c[1]<=28) for v in cobertura.values() for c in v):
        raise ValueError('Cobertura guardada inválida')
    campana.base_alm=int(d['GVL_HMI.iAlmacenados'])-ocupacion
    campana.base_rec=int(d['GVL_HMI.iRecuperados'])-(112-len(punto['cola']) if fase=='siguiente' else 0)
    campana.fase=fase;campana.cola=list(punto['cola']);campana.paro_enviado=False
    campana.iniciada=True
    campana.campana_id=punto.get('campana_id',punto['log_anterior'])
    campana.mandar('GVL_HMI.iEstrategia',campana.estrategia)
    if fase=='llenando':campana.mandar('GVL.START',True)
    return cobertura
