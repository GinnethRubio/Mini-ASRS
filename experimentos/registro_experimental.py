"""Registro experimental del Mini AS/RS.

Cada corrida (unidad experimental) se guarda en su propia carpeta con:

  manifiesto.json      versión de código, equipo, software, licencia/entorno, escenario,
                       política, semillas, hash del inventario inicial, perfil v/a, geometría
  operaciones.csv      una fila por operación (STORE/RETRIEVE), con reloj simulado y monotónico
  interrupciones.csv   pausas, cortes y reanudaciones con causa y duración
  fallos.csv           intentos fallidos y su consecuencia; nunca se eliminan
  eventos.jsonl        traza libre con fecha UTC

Al cerrar la corrida se calcula el SHA-256 de cada archivo, se escribe en el manifiesto,
los archivos quedan de solo lectura y se agrega una fila a registro_corridas.csv.
"""
import csv
import hashlib
import json
import os
import platform
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

PROYECTO = Path(__file__).resolve().parent.parent

NIVELES = {
    'A': 'A_pruebas_selector',
    'B': 'B_modelo_rapido',
    'C': 'C_contraste_integrado',
    'D': 'D_cobertura_funcional',
    'E': 'E_fallos_recuperacion',
}

CAMPOS_OPERACION = [
    'run_id', 'nivel', 'replica', 'politica', 'ocupacion', 'fase', 'pareja', 'bloque',
    'qr', 'operacion', 'columna', 'fila',
    'inicio_sim_s', 'fin_sim_s', 'inicio_mono_s', 'fin_mono_s', 'fecha_utc',
    'tiempo_s', 'recorrido_x_mm', 'recorrido_z_mm', 'distancia_mm', 'computo_seleccion_us',
    'resultado', 'fallo', 'reintento', 'intervencion_humana',
]
OPERACIONES = {'STORE', 'RETRIEVE'}
RESULTADOS = {'correcto', 'fallido', 'interrumpido'}
FASES = {'calentamiento', 'medida', 'diagnostico'}
CAMPOS_INTERRUPCION = ['run_id', 'bloque', 'causa', 'inicio_utc', 'fin_utc', 'duracion_s', 'detalle']
CAUSAS = {'pausa_runtime', 'pausa_bloque', 'perdida_comunicacion', 'reconexion_codesys',
          'alarma', 'parada_usuario', 'otra'}
CAMPOS_FALLO = ['run_id', 'fecha_utc', 'tipo', 'operacion', 'qr', 'detalle', 'consecuencia']
CAMPOS_INDICE = ['run_id', 'nivel', 'politica', 'ocupacion', 'replica', 'estado', 'operaciones',
                 'operaciones_medidas', 'fallos', 'interrupciones', 'inicio_utc', 'fin_utc', 'ruta']
PAQUETES = ('numpy', 'scipy', 'opcua', 'openpyxl', 'coppeliasim-zmqremoteapi-client', 'psutil', 'matplotlib')


def ahora_utc():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for bloque in iter(lambda: f.read(1 << 16), b''):
            h.update(bloque)
    return h.hexdigest()


def version_git(raiz=PROYECTO):
    def git(*a):
        return subprocess.run(['git', *a], cwd=raiz, capture_output=True, text=True, check=True).stdout.strip()
    try:
        return dict(commit=git('rev-parse', 'HEAD'), cambios_sin_commit=bool(git('status', '--porcelain')))
    except (OSError, subprocess.CalledProcessError):
        return dict(commit='sin_git', cambios_sin_commit=None)


def versiones_software():
    v = {'python': platform.python_version()}
    for p in PAQUETES:
        try:
            v[p] = metadata.version(p)
        except metadata.PackageNotFoundError:
            v[p] = None
    return v


def huella_codigo(raiz=PROYECTO):
    archivos = sorted(p for p in raiz.rglob('*') if p.is_file() and p.suffix in ('.py', '.xlsx')
                      and not any(s in ('.venv', '__pycache__', 'datos_experimentos', 'logs_112') or s.startswith('respaldo')
                                  for s in p.relative_to(raiz).parts))
    return {str(p.relative_to(raiz)).replace('\\', '/'): sha256(p) for p in archivos}


def perfil_cinematico():
    sys.path.insert(0, str(PROYECTO / 'coordinador'))
    import rutas  # noqa: F401
    import config
    return dict(v_max_m_s=dict(x=config.VEL_MAX_X, y=config.VEL_MAX_Y, z=config.VEL_MAX_Z),
                a_max_m_s2=dict(x=config.ACEL_X, y=config.ACEL_Y, z=config.ACEL_Z),
                t_movimiento_plc_s=config.T_MOVIMIENTO_PLC, t_actuador_plc_s=config.T_ACTUADOR_PLC,
                sondeo_python_s=config.POLL_INTERVAL)


def hash_inventario(ubicaciones):
    """Hash estable de un inventario {qr: indice_celda} o lista de pares."""
    pares = sorted(dict(ubicaciones).items())
    return hashlib.sha256(json.dumps(pares).encode()).hexdigest()


class Corrida:
    def __init__(self, raiz, nivel, run_id, *, politica=None, ocupacion=None, replica=None,
                 semillas=None, inventario_inicial=None, escenario=None, configuracion=None,
                 licencia=None, alcance=None):
        if nivel not in NIVELES:
            raise ValueError('Nivel desconocido: ' + str(nivel))
        self.raiz = Path(raiz)
        self.nivel, self.run_id = nivel, run_id
        self.path = self.raiz / NIVELES[nivel] / run_id
        self.path.mkdir(parents=True, exist_ok=False)
        self.t0 = time.monotonic()
        self.cuentas = dict(operaciones=0, operaciones_medidas=0, fallos=0, interrupciones=0)
        self.base = dict(run_id=run_id, nivel=nivel, politica=politica, ocupacion=ocupacion, replica=replica)
        geometria = PROYECTO / 'datos' / 'Coordenadas_Rack_Mini_ASRS.xlsx'
        self.manifiesto = dict(
            run_id=run_id, nivel=NIVELES[nivel], estado='en_curso', inicio_utc=ahora_utc(),
            equipo=platform.node(), sistema=platform.platform(), codigo=version_git(),
            software=versiones_software(), licencia_entorno=licencia,
            politica=politica, ocupacion=ocupacion, replica=replica, semillas=semillas,
            inventario_inicial_sha256=hash_inventario(inventario_inicial) if inventario_inicial is not None else None,
            perfil=perfil_cinematico(),
            geometria=dict(archivo=geometria.name, sha256=sha256(geometria)) if geometria.exists() else None,
            escenario=escenario, configuracion=configuracion, alcance=alcance,
            huella_codigo=huella_codigo())
        self._escribir_manifiesto()
        self._tablas = {}
        self._tabla('operaciones.csv', CAMPOS_OPERACION)
        self._tabla('interrupciones.csv', CAMPOS_INTERRUPCION)
        self._tabla('fallos.csv', CAMPOS_FALLO)
        self._eventos = (self.path / 'eventos.jsonl').open('w', encoding='utf-8', buffering=1)
        self.evento('inicio_corrida')

    def _escribir_manifiesto(self):
        tmp = self.path / 'manifiesto.json.tmp'
        tmp.write_text(json.dumps(self.manifiesto, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
        tmp.replace(self.path / 'manifiesto.json')

    def _tabla(self, nombre, campos):
        f = (self.path / nombre).open('w', newline='', encoding='utf-8')
        w = csv.DictWriter(f, fieldnames=campos)
        w.writeheader()
        f.flush()
        self._tablas[nombre] = (f, w)
        return w

    def tabla(self, nombre, campos):
        """Tabla adicional propia del nivel (por ejemplo, casos de prueba del nivel A o E)."""
        if nombre in self._tablas:
            raise ValueError('Tabla ya creada: ' + nombre)
        self._tabla(nombre, campos)

    def fila(self, nombre, **valores):
        f, w = self._tablas[nombre]
        desconocidos = set(valores) - set(w.fieldnames)
        if desconocidos:
            raise ValueError(f'{nombre}: campos no definidos {sorted(desconocidos)}')
        w.writerow(valores)
        f.flush()

    def reloj(self):
        """Segundos monotónicos desde el inicio de la corrida."""
        return round(time.monotonic() - self.t0, 4)

    def evento(self, tipo, **datos):
        self._eventos.write(json.dumps({'fecha_utc': ahora_utc(), 'tipo': tipo, **datos},
                                       ensure_ascii=False, default=str) + '\n')

    def operacion(self, **v):
        fila = {**self.base, 'resultado': 'correcto', 'reintento': 0, 'intervencion_humana': False,
                'fecha_utc': ahora_utc(), **v}
        if fila['operacion'] not in OPERACIONES:
            raise ValueError('operacion debe ser STORE o RETRIEVE')
        if fila['resultado'] not in RESULTADOS:
            raise ValueError('resultado no válido: ' + str(fila['resultado']))
        if fila.get('fase') not in FASES:
            raise ValueError('fase no válida: ' + str(fila.get('fase')))
        self.fila('operaciones.csv', **fila)
        self.cuentas['operaciones'] += 1
        if fila['fase'] == 'medida' and fila['resultado'] == 'correcto':
            self.cuentas['operaciones_medidas'] += 1
        if fila['resultado'] != 'correcto':
            self.fallo(tipo='operacion_' + fila['resultado'], operacion=fila['operacion'], qr=fila.get('qr'),
                       detalle=fila.get('fallo'), consecuencia='operación registrada sin descartar')

    def interrupcion(self, causa, inicio_utc, fin_utc=None, bloque=None, detalle=None):
        if causa not in CAUSAS:
            raise ValueError('Causa de interrupción no válida: ' + causa)
        fin_utc = fin_utc or ahora_utc()
        duracion = (datetime.fromisoformat(fin_utc) - datetime.fromisoformat(inicio_utc)).total_seconds()
        self.fila('interrupciones.csv', run_id=self.run_id, bloque=bloque, causa=causa, inicio_utc=inicio_utc,
                  fin_utc=fin_utc, duracion_s=round(duracion, 3), detalle=detalle)
        self.cuentas['interrupciones'] += 1
        self.evento('interrupcion', causa=causa, duracion_s=duracion)

    def fallo(self, tipo, detalle=None, consecuencia=None, operacion=None, qr=None):
        self.fila('fallos.csv', run_id=self.run_id, fecha_utc=ahora_utc(), tipo=tipo, operacion=operacion,
                  qr=qr, detalle=detalle, consecuencia=consecuencia)
        self.cuentas['fallos'] += 1

    def cerrar(self, estado, resumen=None):
        self.evento('fin_corrida', estado=estado)
        self._eventos.close()
        for f, _ in self._tablas.values():
            f.close()
        fin = ahora_utc()
        self.manifiesto.update(estado=estado, fin_utc=fin, duracion_total_s=self.reloj(),
                               conteos=self.cuentas, resumen=resumen,
                               sha256_archivos={p.name: sha256(p) for p in sorted(self.path.iterdir())
                                                if p.name != 'manifiesto.json'})
        self._escribir_manifiesto()
        for p in self.path.iterdir():
            os.chmod(p, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
        indice = self.raiz / 'registro_corridas.csv'
        nuevo = not indice.exists()
        with indice.open('a', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=CAMPOS_INDICE)
            if nuevo:
                w.writeheader()
            w.writerow(dict(run_id=self.run_id, nivel=self.nivel, politica=self.base['politica'],
                            ocupacion=self.base['ocupacion'], replica=self.base['replica'], estado=estado,
                            inicio_utc=self.manifiesto['inicio_utc'], fin_utc=fin,
                            ruta=str(self.path.relative_to(self.raiz)).replace('\\', '/'), **self.cuentas))
        return self.path

    def __enter__(self):
        return self

    def __exit__(self, tipo, valor, traza):
        if not self._eventos.closed:
            if tipo is None:
                self.cerrar('completa')
            else:
                self.fallo(tipo='excepcion', detalle=repr(valor), consecuencia='corrida incompleta')
                self.cerrar('incompleta' if tipo is KeyboardInterrupt else 'error')
        return False


def verificar(path):
    """Comprueba que los archivos de una corrida cerrada no cambiaron después del sellado."""
    m = json.loads((Path(path) / 'manifiesto.json').read_text(encoding='utf-8'))
    return {n: sha256(Path(path) / n) == h for n, h in m.get('sha256_archivos', {}).items()}
