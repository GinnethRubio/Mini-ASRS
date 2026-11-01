"""Temporizador del tiempo de uso del runtime CODESYS.

Muestra el tiempo restante desde el arranque del runtime y avisa al llegar a cada
umbral. No detiene el PLC; el cierre ordenado lo hace banco_112.py campana.

python temporizador_runtime.py                       # detecta el arranque del proceso
python temporizador_runtime.py --inicio 2026-11-01T14:05:00-05:00 --limite-min 60
"""
import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from sesiones_runtime import detectar_inicio_runtime


def avisar(texto):
    print('\a' + texto, flush=True)
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except ImportError:
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--inicio', default='auto', help="ISO 8601 con zona, o 'auto'")
    ap.add_argument('--limite-min', type=float, default=60)
    ap.add_argument('--avisos-min', default='20,10,5,2', help='Minutos restantes en los que se avisa')
    ap.add_argument('--intervalo-s', type=float, default=30)
    args = ap.parse_args()
    inicio_txt = detectar_inicio_runtime() if args.inicio == 'auto' else args.inicio
    inicio = datetime.fromisoformat(inicio_txt.replace('Z', '+00:00'))
    if inicio.tzinfo is None:
        ap.error('La hora de inicio requiere zona horaria, por ejemplo -05:00')
    fin = inicio + timedelta(minutes=args.limite_min)
    restante = (fin - datetime.now(timezone.utc)).total_seconds()
    pendientes = sorted((float(m) for m in args.avisos_min.split(',') if float(m) * 60 < restante), reverse=True)
    print(f'Runtime iniciado: {inicio.isoformat()}  |  límite: {fin.astimezone().strftime("%H:%M:%S")}', flush=True)
    while True:
        restante = (fin - datetime.now(timezone.utc)).total_seconds()
        if restante <= 0:
            avisar('TIEMPO DEL RUNTIME AGOTADO: reiniciar el runtime CODESYS antes de continuar.')
            return 1
        while pendientes and restante <= pendientes[0] * 60:
            avisar(f'AVISO: quedan {pendientes.pop(0):g} min de runtime CODESYS.')
        m, s = divmod(int(restante), 60)
        print(f'{datetime.now().strftime("%H:%M:%S")}  restante {m:02d}:{s:02d}', flush=True)
        time.sleep(min(args.intervalo_s, max(1, restante)))


if __name__ == '__main__':
    sys.exit(main())
