# Funcionamiento y arquitectura del Mini AS/RS

## Funcionalidad

El sistema coordina almacenamiento y recuperación de sobres en un rack de cuatro columnas por 28 filas. La HMI permite iniciar, detener, rearmar y solicitar recuperaciones; muestra el estado operativo, sensores, salidas, ocupación y alarmas. El ciclo identifica el sobre por QR, selecciona una ubicación, confirma recogida y depósito, actualiza el inventario y despacha las recuperaciones.

## Arquitectura y comunicación

- **CODESYS Control Win V3:** conserva la secuencia de operación, permisos, interbloqueos, selección de ubicación y datos de HMI.
- **FluidSIM 4.2p/1.67 Pneumática:** representa los actuadores neumáticos del eje Y y la pinza, con sus finales de carrera, presión y mandos.
- **Python:** coordina el ciclo y comunica los subsistemas; procesa confirmaciones, lectura QR, bandas y sincronización del inventario.
- **CoppeliaSim:** representa la planta 3D, movimientos del pórtico, bandas, sobres y sensor QR.
- **Datos locales:** el inventario se guarda en JSON; las coordenadas de las 112 celdas están en el libro Excel del proyecto.

Python intercambia órdenes y estados con CODESYS por OPC UA. CODESYS intercambia señales de entrada/salida con FluidSIM por OPC DA. Python se conecta con CoppeliaSim por ZeroMQ Remote API. El diagrama lógico de señales está en [mapa_io_y_errores.md](mapa_io_y_errores.md) y el circuito funcional en [arquitectura_neumatica.md](arquitectura_neumatica.md).

## Secuencia de almacenamiento y recuperación

1. La banda de entrada presenta un sobre y se valida su QR.
2. El PLC determina la operación y la celda de destino según la estrategia seleccionada.
3. Python coordina el movimiento del pórtico hasta el punto de recogida o almacenamiento.
4. El PLC acciona la extensión de Y y el cierre de pinza; las señales de posición confirman cada transición.
5. El sobre se transporta hasta su celda o estación de despacho y se libera.
6. Tras la confirmación, se actualiza la relación entre QR y ubicación. Al iniciar una nueva sesión, el inventario se sincroniza con la ocupación de la escena.

## Heartbeat y condiciones operativas

Python incrementa la variable de heartbeat en el ciclo de coordinación, con una consulta nominal cada 0.2 s. CODESYS considera que Python no responde si el contador permanece sin cambios por 10 s. La condición de comunicación operativa combina Python conectado, CoppeliaSim conectado, inventario listo y ausencia de error reportado por Python. La secuencia se ejecuta en modo automático, paso a paso o emulación para verificación funcional.

## Experimentos de asignación

El módulo experimental compara R, selección aleatoria uniforme de una celda libre, con C, selección de mínimo costo de ubicación. La campaña parametriza niveles de ocupación K=28, 56 y 84, calentamiento de 3K parejas y 200 parejas medidas por corrida; el número de réplicas se especifica al ejecutar. Cada corrida registra su configuración, semillas, operaciones y huellas de archivos. El análisis pareado toma la corrida como unidad y resume tiempos, distancias, diferencias entre políticas y fallos.

Los resultados de esta campaña describen el modelo temporal utilizado en el experimento. Las campañas conectadas al PLC y los ensayos de cobertura funcional se registran en sus carpetas de datos separadas.

## Requerimientos de ejecución

- Windows y Python 3.11 o superior, con paquetes listados en `requirements.txt`.
- CODESYS Control Win V3 con variables OPC UA del sistema.
- FluidSIM 4.2p/1.67 Pneumática con comunicación OPC DA.
- CoppeliaSim con la escena del Mini AS/RS y ZeroMQ Remote API.

## Versiones de software

CODESYS V3.5 SP9 Patch 1+ y FluidSIM 4.2p/1.67 Pneumática, con licencias académicas del laboratorio de la Universitaria Agustiniana.
