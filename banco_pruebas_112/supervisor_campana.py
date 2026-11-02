"""Supervisor de la campaña conectada: bloques por tiempo de runtime y reanudación tras reconexión.

Ejecuta banco_112.py campana por bloques y, entre bloques:
  - antes del límite del runtime CODESYS el bloque se cierra con paro normal y punto de continuación;
  - si CODESYS se desconecta, espera su regreso con un temporizador de reintentos, relanza main.py,
    reconstruye el punto de continuación desde el inventario resincronizado con la escena y sigue;
  - una alarma del proceso, un inventario incoherente o un sobre sujeto detienen la campaña: no rearma.

python banco_pruebas_112/supervisor_campana.py --estrategia 2 --confirmar-movimientos
python banco_pruebas_112/supervisor_campana.py --estrategia 2 --confirmar-movimientos --reanudar RUTA/continuar_campana.json
python banco_pruebas_112/supervisor_campana.py --modo contraste --confirmar-movimientos [--campana ID]

En modo contraste ejecuta contraste_rc.py con el mismo esquema de bloques y reconexión; el progreso
queda en logs_112/estado_contraste_<campaña>.json y la corrida interrumpida se repite completa.

Logs: logs_112/<fecha>_supervisor/ (supervisor.jsonl, estado.json, main_*.log y bloques/) y la
corrida de nivel D en datos_experimentos/ con todas las operaciones, interrupciones y fallos.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

AQUI = Path(__file__).resolve().parent
RAIZ = AQUI.parent
sys.path.insert(0, str(AQUI))
sys.path.insert(0, str(RAIZ / 'coordinador'))
sys.path.insert(0, str(RAIZ / 'experimentos'))
from sesiones_runtime import VentanaRuntime, detectar_inicio_runtime, listo_para_reanudar, punto_tras_reconexion  # noqa: E402

NOMBRES = ['GVL.Paso_Actual', 'GVL.Python_Error', 'GVL.Comunicacion_OK', 'GVL.IN0', 'GVL.IN1',
           'GVL.Maquina_Lista', 'GVL.Inventario_Listo', 'GVL.Y_ESTADO_VALIDO', 'GVL.EJE_Y_ESTADO',
           'GVL.GRIPPER_ESTADO_VALIDO', 'GVL.GRIPPER_ESTADO', 'GVL_HMI.xMarcha', 'GVL_HMI.iOperacionActual',
           'GVL_HMI.axOcupada', 'GVL_HMI.asCodigo', 'GVL_HMI.xModoManual', 'GVL_HMI.xModoPaso',
           'GVL_HMI.xSimularCoppelia', 'GVL_HMI.iAlmacenados', 'GVL_HMI.iRecuperados']


def ahora_utc():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def leer_plc(url):
    """Muestra OPC UA de las variables de estado, o None si CODESYS no responde."""
    try:
        from opcua import Client
        cliente = Client(url, timeout=4)
        cliente.connect()
    except Exception:
        return None
    try:
        nodos = [cliente.get_node(f'ns=4;s=|var|CODESYS Control Win V3.Application.{n}') for n in NOMBRES]
        return dict(zip(NOMBRES, cliente.get_values(nodos)))
    except Exception:
        return None
    finally:
        try:
            cliente.disconnect()
        except Exception:
            pass


class Supervisor:
    def __init__(self, args, carpeta, corrida, leer, inicio_runtime, ejecutar_bloque, coordinador,
                 dormir=time.sleep, reloj=time.monotonic):
        self.args, self.carpeta, self.corrida = args, Path(carpeta), corrida
        self.leer, self.inicio_runtime, self.ejecutar_bloque = leer, inicio_runtime, ejecutar_bloque
        self.coordinador, self.dormir, self.reloj = coordinador, dormir, reloj
        self.bloque = 0
        self.punto = Path(args.reanudar) if args.reanudar else None
        self.continuar = args.continuar
        self.cobertura = getattr(args, 'modo', 'cobertura') == 'cobertura'
        self.agotado = None
        self.estado = {}
        self.log = (self.carpeta / 'supervisor.jsonl').open('a', encoding='utf-8', buffering=1)

    def emitir(self, tipo, **datos):
        linea = {'fecha_utc': ahora_utc(), 'tipo': tipo, **datos}
        self.log.write(json.dumps(linea, ensure_ascii=False, default=str) + '\n')
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {tipo}", *(f'{k}={v}' for k, v in datos.items()), flush=True)
        self.estado.update(ultimo_evento=tipo, fecha_utc=linea['fecha_utc'], bloque=self.bloque,
                           punto=str(self.punto) if self.punto else None)
        tmp = self.carpeta / 'estado.json.tmp'
        tmp.write_text(json.dumps(self.estado, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
        tmp.replace(self.carpeta / 'estado.json')
        if self.corrida is not None:
            self.corrida.evento('supervisor_' + tipo, **{k: str(v) for k, v in datos.items()})

    def esperar(self, condicion, descripcion, limite_s):
        """Reintenta hasta que condicion() devuelva un valor verdadero; espera creciente de 5 a 30 s."""
        inicio, espera, avisado = self.reloj(), 5.0, 0.0
        while True:
            valor = condicion()
            if valor:
                return valor
            transcurrido = self.reloj() - inicio
            if limite_s and transcurrido > limite_s:
                raise TimeoutError(f'{descripcion}: sin respuesta en {limite_s/60:.0f} min')
            if transcurrido - avisado >= 60 or avisado == 0:
                self.emitir('esperando', motivo=descripcion, transcurrido_s=round(transcurrido))
                avisado = transcurrido or 1e-9
            self.dormir(espera)
            espera = min(30.0, espera * 1.5)

    def margen_suficiente(self, inicio):
        try:
            VentanaRuntime(inicio, limite_min=self.args.limite_runtime_min,
                           margen_min=self.args.margen_runtime_min, bloque_min=self.args.bloque_min)
            return True
        except ValueError:
            return False

    def esperar_reinicio_runtime(self, inicio_previo, forzar=False):
        """Sigue si el runtime aún tiene tiempo; si no, espera a que CODESYS caiga y vuelva a arrancar.

        Con forzar, el arranque anterior no sirve aunque le quede margen: el bloque ya dijo que no alcanza."""
        estado = {'caida': False}

        def listo():
            inicio = self.inicio_runtime()
            if inicio and self.margen_suficiente(inicio) and not (forzar and inicio == inicio_previo):
                return inicio
            if self.leer() is None:
                estado['caida'] = True
            elif estado['caida']:
                # Sin hora de proceso utilizable, el plazo nuevo se cuenta desde la reconexión observada.
                return inicio if inicio and self.margen_suficiente(inicio) else ahora_utc()
            return None
        if forzar or not (inicio_previo and self.margen_suficiente(inicio_previo)):
            self.emitir('runtime_sin_margen', accion='Reiniciar el runtime CODESYS; la campaña continúa sola al volver')
            self.avisar()
        return self.esperar(listo, 'reinicio del runtime CODESYS', self.args.espera_max_min * 60)

    def avisar(self):
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except ImportError:
            pass

    def preparar(self):
        """PLC accesible, coordinador listo, FluidSIM con permisos y actuadores en reposo."""
        d = self.esperar(self.leer, 'conexión OPC UA con CODESYS', self.args.espera_max_min * 60)
        self.coordinador.asegurar(relanzar=not d['GVL.Maquina_Lista'])
        def listo():
            # main.py se cierra si la escena se reinicia; se relanza mientras se espera.
            vigilar = getattr(self.coordinador, 'vigilar', None)
            if vigilar is not None:
                vigilar()
            x = self.leer()
            return x if x is not None and listo_para_reanudar(x) else None
        return self.esperar(listo, 'PLC en espera, main.py listo, FluidSIM con permisos y actuadores en reposo',
                            self.args.espera_max_min * 60)

    def hubo_corte(self, inicio_bloque, carpeta):
        if self.leer() is None:
            return True
        inicio = self.inicio_runtime()
        if inicio and inicio_bloque and inicio != inicio_bloque:
            return True
        estado = Path(carpeta) / 'estado_interrumpido.json' if carpeta else None
        motivo = json.loads(estado.read_text(encoding='utf-8')).get('motivo', '') if estado and estado.exists() else ''
        return es_corte_comunicacion(motivo)

    def ejecutar(self):
        interrupcion = None
        pendiente = None
        if self.punto is not None and self.cobertura and self.punto.exists():
            # Un estado interrumpido (alarma o corte) se convierte en punto de continuación
            # con el inventario de la escena cuando el PLC vuelve a estar listo.
            if json.loads(self.punto.read_text(encoding='utf-8')).get('estado') == 'interrumpido':
                pendiente, self.punto = self.punto, None
        while True:
            inicio = self.inicio_runtime()
            if inicio and inicio == self.agotado:
                inicio = self.esperar_reinicio_runtime(inicio, forzar=True)
            elif not (inicio and self.margen_suficiente(inicio)):
                inicio = self.esperar_reinicio_runtime(inicio)
            d = self.preparar()
            if pendiente is not None:
                punto = punto_tras_reconexion(json.loads(pendiente.read_text(encoding='utf-8')), d)
                self.punto = pendiente.with_name('continuar_tras_reconexion.json')
                tmp = self.punto.with_suffix('.tmp')
                tmp.write_text(json.dumps(punto, ensure_ascii=False, indent=2), encoding='utf-8')
                tmp.replace(self.punto)
                pendiente = None
            if interrupcion is not None:
                self.registrar_interrupcion(*interrupcion)
                interrupcion = None
            self.bloque += 1
            self.emitir('bloque_inicio', inicio_runtime=inicio, punto=self.punto)
            codigo, carpeta = self.ejecutar_bloque(self.bloque, self.punto, self.continuar and self.bloque == 1, inicio)
            self.emitir('bloque_fin', codigo=codigo, carpeta=carpeta)
            if self.corrida is not None and carpeta and self.cobertura:
                from puente_banco import importar_bloque
                importar_bloque(self.corrida, carpeta, self.bloque, self.corrida.t0)
            if codigo == 0:
                self.emitir('campana_completa', bloques=self.bloque)
                return 0
            if codigo == 3:
                if not self.cobertura:
                    # El contraste cierra el bloque solo si la corrida siguiente no cabe en este arranque.
                    self.agotado = inicio
                self.punto = Path(carpeta) / 'continuar_campana.json'
                motivo = json.loads(self.punto.read_text(encoding='utf-8')).get('motivo', 'pausa_runtime')
                interrupcion = (motivo, ahora_utc(), 'Bloque cerrado en reposo; continuación ' + str(self.punto))
                continue
            if not self.hubo_corte(inicio, carpeta):
                self.emitir('campana_detenida', motivo='Error del proceso sin corte de comunicación: revisar el bloque',
                            carpeta=carpeta)
                self.avisar()
                return codigo
            self.emitir('desconexion_codesys', accion='Esperando reconexión; no rearmar ni mover la escena')
            self.avisar()
            interrupcion = ('perdida_comunicacion', ahora_utc(), 'Ciclo en curso no contabilizado; inventario tomado de la escena')
            estado = Path(carpeta) / 'estado_interrumpido.json' if carpeta else None
            # El contraste reanuda desde su propio estado y repite completa la corrida interrumpida.
            if estado is not None and estado.exists() and self.cobertura:
                pendiente = estado
            self.coordinador.descartar()

    def cerrar(self):
        self.log.close()

    def registrar_interrupcion(self, causa, inicio_utc, detalle):
        self.emitir('reanudacion', causa=causa, desde=inicio_utc, punto=self.punto)
        if self.corrida is not None:
            self.corrida.interrupcion(causa, inicio_utc, ahora_utc(), bloque=self.bloque, detalle=detalle)


def es_corte_comunicacion(motivo):
    """Errores de enlace OPC UA o reinicio del runtime; no alarmas ni falta de avance del proceso."""
    if any(k in motivo for k in ('sin avance', 'Alarma', 'no coinciden', 'inventario', 'Inventario')):
        return False
    return any(k in motivo for k in ('Connection', 'socket', 'BrokenPipe', 'BadSession', 'BadConnection',
                                     'closed', 'reiniciado', 'TimeoutError', 'OSError', 'EOF'))


class Coordinador:
    """Lanza main.py cuando hace falta; nunca deja dos coordinadores a la vez."""
    def __init__(self, carpeta, leer, lanzar=True, gracia_s=10, dormir=time.sleep):
        self.carpeta, self.leer, self.lanzar, self.gracia_s, self.dormir = carpeta, leer, lanzar, gracia_s, dormir
        self.proceso = None

    def descartar(self):
        """Tras un corte, el main.py lanzado por el supervisor pierde su sesión OPC UA: se detiene."""
        if self.proceso is not None and self.proceso.poll() is None:
            try:
                self.proceso.wait(15)
            except subprocess.TimeoutExpired:
                self.proceso.terminate()
                self.proceso.wait(10)
        self.proceso = None

    def vigilar(self):
        """Relanza main.py si el proceso que inició el supervisor terminó."""
        if self.proceso is not None and self.proceso.poll() is not None:
            print(f'main.py terminó con código {self.proceso.returncode}; se relanza.', flush=True)
            self.proceso = None
            self.asegurar(relanzar=False)

    def asegurar(self, relanzar):
        if self.proceso is not None and self.proceso.poll() is None:
            if not relanzar:
                return
            self.descartar()
        # Un main.py externo que ya se reconectó publica Maquina_Lista en pocos segundos.
        self.dormir(self.gracia_s)
        d = self.leer()
        if d is not None and d['GVL.Maquina_Lista']:
            return
        if not self.lanzar:
            print('Iniciar main.py manualmente; el supervisor espera.', flush=True)
            return
        salida = (Path(self.carpeta) / f"main_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log").open('w', encoding='utf-8')
        banderas = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
        self.proceso = subprocess.Popen([sys.executable, str(RAIZ / 'coordinador' / 'main.py')], cwd=RAIZ,
                                        stdout=salida, stderr=subprocess.STDOUT, creationflags=banderas,
                                        env={**os.environ, 'PYTHONUTF8': '1'})


def bloque_banco(args, carpeta):
    def ejecutar(numero, punto, continuar, inicio):
        cmd = [sys.executable, str(AQUI / 'banco_112.py'), 'campana', '--proyecto', str(RAIZ),
               '--salida', str(Path(carpeta) / 'bloques'), '--opc-url', args.opc_url,
               '--estrategia', str(args.estrategia), '--confirmar-movimientos', '--inicio-runtime', inicio,
               '--limite-runtime-min', str(args.limite_runtime_min), '--margen-runtime-min', str(args.margen_runtime_min),
               '--bloque-min', str(args.bloque_min)]
        if punto:
            cmd += ['--reanudar', str(punto)]
        elif continuar:
            cmd.append('--continuar')
        return lanzar_bloque(cmd, numero)
    return ejecutar


def bloque_contraste(args, carpeta):
    """Bloques del contraste R vs C; todos comparten la campaña y el archivo de estado."""
    def ejecutar(numero, punto, continuar, inicio):
        cmd = [sys.executable, str(AQUI / 'contraste_rc.py'), '--confirmar-movimientos', '--campana', args.campana,
               '--estado', str(args.estado_contraste), '--protocolo', str(args.protocolo),
               '--salida', str(Path(carpeta) / 'bloques'), '--datos', str(args.datos), '--opc-url', args.opc_url,
               '--inicio-runtime', inicio, '--limite-runtime-min', str(args.limite_runtime_min),
               '--margen-runtime-min', str(args.margen_runtime_min)]
        cmd += ['--solo-vaciar'] if args.solo_vaciar else ['--vaciar'] if args.vaciar else []
        return lanzar_bloque(cmd, numero)
    return ejecutar


def lanzar_bloque(cmd, numero):
    proceso = subprocess.Popen(cmd, cwd=RAIZ, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding='utf-8', errors='replace')
    carpeta_bloque = None
    for linea in proceso.stdout:
        print(f'  [bloque {numero}] {linea.rstrip()}', flush=True)
        if linea.startswith('Logs:'):
            carpeta_bloque = linea.split('Logs:', 1)[1].strip()
    return proceso.wait(), carpeta_bloque


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--modo', choices=['cobertura', 'contraste'], default='cobertura',
                    help='cobertura: 112 almacenamientos y 112 recuperaciones (nivel D); contraste: parejas R vs C (nivel C)')
    ap.add_argument('--estrategia', type=int, choices=[0, 1, 2], default=2)
    ap.add_argument('--campana', help='Contraste: identificador de la campaña a crear o continuar')
    ap.add_argument('--protocolo', type=Path, default=RAIZ / 'experimento.json')
    ap.add_argument('--vaciar', action='store_true', help='Contraste: recuperar todos los sobres al terminar')
    ap.add_argument('--solo-vaciar', action='store_true', help='Contraste: solo vaciar el rack')
    ap.add_argument('--confirmar-movimientos', action='store_true')
    ap.add_argument('--continuar', action='store_true')
    ap.add_argument('--reanudar', type=Path)
    ap.add_argument('--opc-url', default=os.environ.get('ASRS_OPC_URL') or 'opc.tcp://localhost:4840')
    ap.add_argument('--inicio-runtime', default='auto', help="'auto' lee el arranque del proceso CODESYS")
    ap.add_argument('--limite-runtime-min', type=float, default=60)
    ap.add_argument('--margen-runtime-min', type=float, default=10)
    ap.add_argument('--bloque-min', type=float, default=45)
    ap.add_argument('--espera-max-min', type=float, default=60, help='Tiempo máximo de espera por reconexión; 0 sin límite')
    ap.add_argument('--sin-lanzar-main', action='store_true', help='No iniciar main.py; esperar que el operador lo haga')
    ap.add_argument('--licencia', default='CODESYS Control Win V3, licencia académica del laboratorio')
    ap.add_argument('--salida', type=Path, default=RAIZ / 'logs_112')
    ap.add_argument('--datos', type=Path, default=RAIZ / 'datos_experimentos')
    args = ap.parse_args()
    if not args.confirmar_movimientos:
        ap.error('La campaña mueve la simulación: agregar --confirmar-movimientos')
    if args.reanudar and args.continuar:
        ap.error('Usar --reanudar o --continuar, no ambos')
    carpeta = args.salida / (datetime.now().strftime('%Y%m%d_%H%M%S') + '_supervisor')
    carpeta.mkdir(parents=True)
    leer = lambda: leer_plc(args.opc_url)  # noqa: E731

    def inicio_runtime():
        if args.inicio_runtime != 'auto':
            return args.inicio_runtime
        try:
            return detectar_inicio_runtime()
        except (ImportError, RuntimeError):
            return None
    from registro_experimental import Corrida
    if args.modo == 'contraste':
        args.campana = args.campana or carpeta.name.replace('_supervisor', '_rcC')
        args.estado_contraste = args.salida / f'estado_contraste_{args.campana}.json'
        # El prefijo sup_ deja esta corrida fuera del pareo de la campaña.
        corrida = Corrida(args.datos, 'C', f'sup_{args.campana}_{carpeta.name}', licencia=args.licencia,
                          configuracion={k: str(v) for k, v in vars(args).items()},
                          escenario=dict(campana=args.campana, logs=str(carpeta)),
                          alcance='Supervisión del contraste integrado: bloques, pausas y reconexiones.')
        ejecutar_bloque = bloque_contraste(args, carpeta)
    else:
        run_id = carpeta.name.replace('_supervisor', f'_D_E{args.estrategia}')
        corrida = Corrida(args.datos, 'D', run_id, politica=f'E{args.estrategia}', licencia=args.licencia,
                          configuracion={k: str(v) for k, v in vars(args).items()},
                          escenario=dict(campana='llenado de 112 sobres y 112 recuperaciones', logs=str(carpeta)),
                          alcance='Campaña conectada CODESYS-FluidSIM-CoppeliaSim; tiempos reportados por el PLC.')
        ejecutar_bloque = bloque_banco(args, carpeta)
    print('Supervisor:', carpeta.resolve(), '| corrida:', corrida.path, flush=True)
    if args.modo == 'contraste':
        print('Campaña de contraste:', args.campana, flush=True)
    sup = Supervisor(args, carpeta, corrida, leer, inicio_runtime, ejecutar_bloque,
                     Coordinador(carpeta, leer, lanzar=not args.sin_lanzar_main))
    estado = 'error'
    try:
        codigo = sup.ejecutar()
        estado = 'completa' if codigo == 0 else 'incompleta'
        return codigo
    except KeyboardInterrupt:
        estado = 'incompleta'
        sup.emitir('detenido_por_usuario')
        return 130
    except Exception as exc:
        sup.emitir('error_supervisor', error=repr(exc))
        corrida.fallo(tipo='supervisor', detalle=repr(exc), consecuencia='campaña detenida')
        return 1
    finally:
        corrida.cerrar(estado, resumen=dict(bloques=sup.bloque, logs=str(carpeta)))
        sup.cerrar()


if __name__ == '__main__':
    sys.exit(main())
