"""Contrato OPC UA de la HMI exportada de ginneth .project."""
import os
from pathlib import Path
PROJECT_DIR = Path(__file__).resolve().parent.parent
DATOS_DIR = PROJECT_DIR / 'datos'
JSON_PATH = DATOS_DIR / 'inventario_mini_asrs.json'
# Runtime CODESYS del mismo equipo; ASRS_OPC_URL permite apuntar a otro servidor.
OPC_URL = os.environ.get('ASRS_OPC_URL') or 'opc.tcp://localhost:4840'
OPC_TIMEOUT = 5
POLL_INTERVAL = 0.2
NUM_CELDAS = 112
# Perfil trapezoidal por eje: velocidad máxima (m/s) y aceleración (m/s²).
VEL_MAX_X = 0.25
VEL_MAX_Z = 0.25
VEL_MAX_Y = 0.20  # cilindro neumático
ACEL_X = 0.5
ACEL_Z = 0.5
ACEL_Y = 0.5
# Supervisión de PLC_PRG (T_MOVIMIENTO, T_ACTUADOR); un movimiento más largo se rechaza.
T_MOVIMIENTO_PLC = 30.0
T_ACTUADOR_PLC = 15.0
MARGEN_SUPERVISION = 0.8
# El índice de namespace se descubre al conectar; 4 es el preferido original.
OPC_PREFIX = 'ns=4;s=|var|CODESYS Control Win V3.Application.'
NOMBRES_GVL = '''Maquina_Lista START STOP SOBRE_POSIN SOBRE_POS_FINAL
Pos_Recogida Pos_Almacenamiento Z_Ajuste_Terminado BANDA_ENTRADA
Ir_Recogida Ir_Almacenamiento Y_Extender Y_Retraer Z_Ajuste G_Ajustar G_Soltar
Columna_Destino Fila_Destino EJE_X_POSICION EJE_Z_POSICION EJE_Y_ESTADO
GRIPPER_ESTADO Y_ESTADO_VALIDO GRIPPER_ESTADO_VALIDO PYTHON_Heartbeat
PYTHON_Conectado COPPELIA_Conectado Comunicacion_OK PLC_Listo
Inventario_Listo Python_Error Deposito_Confirmado Manual_Permitido
Manual_Orden Manual_Ack Ciclo_Terminado Crear_Sobre CICLO_AUTO Paso_Actual'''.split()
OPC_NODES = {n: OPC_PREFIX+'GVL.'+n for n in NOMBRES_GVL}
OPC_NODES.update({
    'QR_DETECTADO': OPC_PREFIX+'GVL.QR_Leido',
    'NUM_QR': OPC_PREFIX+'GVL.Codigo_QR',
    'Python_Mensaje': OPC_PREFIX+'GVL.Python_Mensaje',
    'Inventario_Ocupada': OPC_PREFIX+'GVL_HMI.axOcupada',
    'Inventario_Codigo': OPC_PREFIX+'GVL_HMI.asCodigo',
})

# Recuperación: símbolos existentes del proyecto CODESYS.
OPC_NODES.update({
    'Ir_Despacho': OPC_PREFIX+'GVL.Ir_Despacho',
    'Pos_Despacho': OPC_PREFIX+'GVL.Pos_Despacho',
    'Operacion_Actual': OPC_PREFIX+'GVL_HMI.iOperacionActual',
    'Codigo_Operacion': OPC_PREFIX+'GVL_HMI.sCodigoActual',
    'Entradas_FluidSIM': OPC_PREFIX+'GVL.IN1',
})
