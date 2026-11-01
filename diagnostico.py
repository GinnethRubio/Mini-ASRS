"""Comprobación de archivos y lectura OPC UA; no escribe órdenes ni mueve ejes."""
from codesys_opcua import CodesysOPCUA
from inventario import validar_inventario
from control_portico import ControlPortico, ARCHIVO_COORDENADAS

def main():
    datos = validar_inventario()
    coordenadas = ControlPortico._cargar_coordenadas(ARCHIVO_COORDENADAS)
    print('Inventario:', len(datos['celdas']), 'celdas; Excel:', len(coordenadas), 'coordenadas')
    plc = CodesysOPCUA()
    try:
        plc.conectar()
        print('Todos los nodos configurados existen y se pueden leer.')
        for n in ('PLC_Listo','Paso_Actual','CICLO_AUTO','PYTHON_Conectado','COPPELIA_Conectado','Inventario_Listo','Python_Error','Python_Mensaje'):
            print(n, '=', plc.leer(n))
    finally:
        plc.desconectar()

if __name__ == '__main__': main()
