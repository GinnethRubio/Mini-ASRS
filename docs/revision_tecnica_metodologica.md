# Revisión técnica y metodológica del Mini AS/RS

## Alcance y estado de evidencia

Esta revisión contrasta el código disponible en este repositorio con el manuscrito `Documento.pdf` y la guía de rescate recibidos el 1 de noviembre de 2026. La guía se trata como recomendación editorial/metodológica, no como evidencia de resultados ni como autorización para cambiar los objetivos. Se conservaron sin cambios los objetivos general y específicos del manuscrito. No se encontraron en el repositorio los proyectos CODESYS, FluidSIM ni la escena de CoppeliaSim: su integración no es reproducible desde este repositorio por sí solo.

El código implementa la coordinación Python–CODESYS por OPC UA y Python–CoppeliaSim por ZeroMQ. El enlace FluidSIM–CODESYS se describe como OPC DA y depende de un proyecto externo. Los ejes X/Z se ejecutan como movimiento de la escena; no se halló en el código un accionamiento de motores físicos por STEP/DIR. Por ello, resultados de simulación, pruebas con dobles y operación conectada al PLC deben informarse por separado y no llamarse ensayos físicos del mecanismo.

## Funcionalidad y arquitectura del código

- `main.py`: ciclo de coordinación, control de permisos y órdenes, lectura QR, heartbeat, confirmaciones y gestión de errores.
- `codesys_opcua.py` y `config.py`: conexión OPC UA, descubrimiento/validación de nodos, mapa de variables, endpoint, límites y perfiles.
- `coppelia_bridge.py`, `control_portico.py`, `control_gripper.py`: conexión con la escena, cinemática de ejes X/Y/Z y pinza simulada.
- `control_banda_integrado.py`, `control_banda_salida.py`, `recuperacion.py`: ingreso, recuperación y despacho de sobres.
- `lector_qr.py`: lectura y validación de identificadores QR.
- `inventario.py`, `sincronizacion_escena.py`: persistencia JSON y reconstrucción al conectar desde la ocupación de la escena.
- `evaluar_estrategias.py`, `experimentos/`: modelo de tiempos y campaña pareada R/C; las campañas de esta carpeta son simulaciones/modelos, no mediciones físicas.
- `banco_pruebas_112/`: pruebas del modelo, registro OPC UA y campaña conectada por bloques.
- `Coordenadas_Rack_Mini_ASRS.xlsx`: fuente de coordenadas; `QR_Mini_ASRS/`: identificadores QR de la demostración.

### Contrato de comunicación y heartbeat

El PLC conserva la secuencia y selección de celda; Python verifica precondiciones, realiza acciones en CoppeliaSim y publica confirmaciones por OPC UA. El ciclo de Python tiene `POLL_INTERVAL = 0.2 s`; `PYTHON_Heartbeat` se incrementa durante la coordinación. La guía/manuscrito cita un timeout del PLC de 10 s. Debe documentarse y comprobarse como ajuste del proyecto CODESYS (no está incluido aquí), junto con las condiciones de rearme. La confirmación de depósito actualiza JSON y notifica al PLC después de la acción simulada.

El inventario local se sincroniza desde la escena al inicio. Esto hace a la escena la fuente de ocupación de la conexión nueva y puede reemplazar el estado anterior, respaldándolo si difiere; el procedimiento operativo debe preservar y comparar el JSON antes de una sesión si la escena no es autoritativa para la operación pretendida.

## Dependencias, rutas y configuración

Las importaciones observadas requieren los paquetes declarados en `requirements.txt` y también `pyzmq` y `cbor2`, usados por el cliente ZeroMQ de CoppeliaSim. Se añadieron al archivo de dependencias para permitir una instalación reproducible. El endpoint de `config.py` es `opc.tcp://192.168.0.21:4840`; el README anterior atribuía a VS Code un valor localhost como predeterminado. La tarea puede sobreescribirlo mediante `ASRS_OPC_URL`; se debe verificar el runtime real antes de conectar. No cambiar la IP de red sin confirmar la dirección del PLC.

`config.py` deriva `PROJECT_DIR` de la ruta del módulo, y las rutas de inventario y coordenadas son relativas al proyecto; este diseño evita depender del directorio de trabajo. Los registros experimentales se escriben bajo `datos_experimentos/` en la raíz del repositorio, directorio generado al ejecutar la campaña. No encontré código de scraping ni uso de una fuente web scrapeada; por tanto, no hay una referencia de scraping atribuible al sistema.

Requisitos funcionales externos: Windows, Python 3.11 o superior, runtime CODESYS con símbolos compatibles, FluidSIM y CoppeliaSim con la escena y objetos esperados. Las capturas identifican CODESYS V3.5 SP9 Patch 1+ y FluidSIM 4.2p/1.67 Pneumática. El usuario indica que son licencias académicas del laboratorio de la Uniagustiniana; no se adjuntaron datos de titularidad ni archivos fuente para verificar edición, opciones OPC o compatibilidad. Ver [mapa lógico de E/S y alarmas](mapa_io_y_errores.md).

## Revisión metodológica y lectura válida de resultados

El manuscrito mezcla evidencia de distinta naturaleza. Reporta comparación por modelo de tiempos, cobertura con dobles de software y una ejecución conectada al PLC. Deben presentarse en secciones separadas. Los conteos de acciones no son réplicas independientes: para comparación R/C, la unidad experimental correcta documentada en `campana_rc.py` es la corrida pareada. La campaña usa inventario inicial y demanda común por semilla; las solicitudes siguientes se eligen entre los QR aún existentes, y R selecciona uniformemente entre celdas libres. C minimiza el costo parametrizado (tiempo previsto o distancia), con desempate por índice.

La campaña incluida fija K=28, 56 y 84, calienta 3K parejas por defecto y mide 200 parejas por corrida; son parámetros del protocolo, no resultados ya obtenidos ni valores universales. `--replicas` permite configurarlos. El análisis reporta media por corrida, diferencias pareadas e IC t al 95 %, además de exclusiones y fallos. Antes de inferir, se deben publicar datos crudos/manifiestos, número real de pares completos por nivel, diagnóstico de valores faltantes y distribución de diferencias. Si se prueban varias ocupaciones o desenlaces, especificar desenlace primario y tratar multiplicidad. No interpretar p>0.05 como equivalencia.

Los valores de mejora histórica citados en el manuscrito (p. ej., porcentaje de distancia/tiempo entre políticas) proceden de su modelo y no son confirmación física. No hay base para afirmar que C gane en todos los niveles o con hardware real. Reportar el resultado observado aunque sea nulo o desfavorable. Validar el modelo contra corridas integradas pareadas en el mismo recorrido, con tolerancia predefinida y perturbaciones realistas; explicar que 0.5 s/5 % es una regla propuesta en el documento, no una norma industrial. Los resultados conectados interrumpidos no equivalen a cobertura completa ni a ciclo completo de almacenamiento y recuperación.

## Arquitectura y comunicación

El sistema combina la secuencia del PLC, el circuito neumático modelado en FluidSIM, la coordinación en Python y la planta 3D en CoppeliaSim. Las tablas funcionales de señales y alarmas están en [mapa lógico de E/S y alarmas](mapa_io_y_errores.md), y los componentes del circuito y sus conexiones se detallan en [arquitectura neumática e integración](arquitectura_neumatica.md).

Se recomienda conservar en el manuscrito las tablas O de arquitectura, variables, protocolos y heartbeat; P de campañas y trazabilidad experimental; Q de estructura económica y supuestos; además de las tablas de dimensionamiento, geometría y comparación multicriterio que sustentan decisiones.
## Motores: alternativas, cálculo y decisión pendiente

El manuscrito propone como candidatos para X el Leadshine CS-M22323 con CS-D508 y para Z el CS-M23485B con CS-D1008 y reducción 5:1. Son componentes distintos para dos ejes; los datos de holding torque no prueban par disponible a la velocidad de operación. Las fichas oficiales publican curvas como imagen; no se extrajo un valor verificable de par dinámico en el punto de trabajo. Por regla de evidencia, ese valor es `null` y no se declara ganador ni compatibilidad dinámica.

| Eje | Candidato condicional | Datos oficiales consultables | Cálculo y brecha |
|---|---|---|---|
| X | CS-M22323 + CS-D508 | Motor: 2.3 Nm de retención, 5 A/fase. Driver: 20–50 VDC, hasta 8 A. | Perfil de simulación: 0.25 m/s y 0.5 m/s². Masa móvil de 65 kg y relación 5:1 son supuestos del cálculo, no masa medida ni transmisión definida. Con resistencia no medida de 10 N, radio 0.02 m, eficiencia 0.90 y factor 1.5: demanda estimada 0.283 Nm a 596.8 rpm del motor; sin esa resistencia, 0.217 Nm. Par disponible allí = `null`; transmisión real, inercia, fricción y ciclo térmico pendientes. |
| Z | CS-M23485B + CS-D1008 + reductor 5:1 | Motor: 8.5 Nm de retención en tabla (resumen del fabricante indica 8.0 Nm); freno 24 V y 4 Nm reportados en documentación asociada. Driver: hasta 8 A; alimentación depende de la variante y manual. | Perfil actual de simulación 0.25 m/s y 0.5 m/s²; masa 41 kg, radio 0.02 m, eficiencia 0.90 y relación 5:1 provisionales. F=422.71 N; torque de salida 8.454 Nm; demanda de diseño de motor 2.818 Nm a aprox. 596.8 rpm. Freno reflejado idealmente a salida = 18 Nm, pero esto no valida frenado seguro ni protege transmisión/correa. Par dinámico disponible y capacidad térmica = `null`. |

La evaluación multicriterio anterior calificaba categorías genéricas de motor y no estos modelos con curvas, geometría y cotizaciones verificadas. No se deben heredar esas puntuaciones como evidencia de selección. Matriz de decisión auditable en el estado actual:

| Criterio por eje | CS-M22323/CS-D508 (X) | CS-M23485B/CS-D1008 (Z) |
|---|---:|---:|
| Ajuste de corriente nominal motor-driver según ficha | pendiente de confirmar conexión/configuración | pendiente de confirmar conexión/configuración |
| Par dinámico disponible en rpm de trabajo | `null` | `null` |
| Verificación de inercia, aceleración y ciclo térmico | `null` | `null` |
| Dimensionamiento de reductor, correa y eje | `null` | `null` |
| Protección ante pérdida de energía/falla mecánica | parcial; diseño requerido | `null`; freno no sustituye análisis de seguridad |
| Precio puesto en Colombia con impuestos y transporte | `null` | `null` |
| Puntaje total/peso multicriterio y ganador | `null` | `null` |

Los espacios pendientes deben medirse/obtenerse: masa de cada conjunto, radio efectivo, fricción, inercias, aceleración real, trayectorias y ciclo de trabajo; curva par-velocidad con tensión/corriente del driver; tolerancia y dimensionado de transmisión; freno para la carga vertical y análisis de falla; cotización local comparable para los dos motores, drivers y transmisión. Después se puede asignar escala y pesos publicados y calcular puntajes. Forzar un ganador ahora violaría el criterio de no respaldar afirmaciones.

### ROI y costos

Anexo Q estima CAPEX de 49,741,434 COP y OPEX de 268,407 COP/mes. En el escenario hipotético de reducción de tres operarios a dos, el ahorro declarado es 4,510,677 COP/mes; el PRI simple calculado es 49,741,434 / 4,510,677 = 11.03 meses (12.03 incluyendo un mes de puesta en marcha). Son resultados del escenario supuesto, no flujo de caja observado. Si se mantienen tres operarios, con los demás supuestos del anexo, se reportan 1,499,593 COP/mes y PRI ≈33.17 meses. Sin ahorro laboral y sin reducción de pérdidas verificadas, el flujo neto es −268,407 COP/mes y el PRI no existe.

No se dispone de costo puesto en Colombia para los motores, drivers, reductores y transmisión propuestos, ni de evidencia para sustituir las hipótesis laborales/mermas. Así, CAPEX actualizado, ahorro validado y ROI final son `null`. No se incorpora precio minorista extranjero como si fuera precio local instalado. Fórmula a completar con cotizaciones y mediciones: CAPEX revisado = CAPEX base − ítems retirados + nuevos motores/drivers/transmisión/seguridad/instalación; flujo neto mensual = ahorros monetarios medidos − OPEX incremental; PRI simple = CAPEX revisado / flujo neto, solo si este es positivo. La incertidumbre de beneficios debe reportarse por escenarios sustentados, sin forzar retorno positivo.

## Referencias trazables de producto y método

- Leadshine, ficha CS-M22323: https://www.leadshine.com/product-detail/CS-M22323.html
- Leadshine, ficha CS-M23485B: https://www.leadshine.com/product-detail/CS-M23485B.html
- Leadshine, ficha CS-D508: https://leadshine.com/product-detail/closed-loop-stepper-drive/closed-loop-stepper/CS-D508.html
- Leadshine, manual CS-D1008 (tensiones, pulsos y límites): https://www.leadshine.com/upfiles/downloads/01662380903d5d0194c3919856d9668f_1665570042922.pdf
- Leadshine, alternativa CS-M234120B (12 Nm de retención; no seleccionada): https://www.leadshine.com/product-detail/closed-loop-stepper-motor/closed-loop-stepper/CS-M234120B.html
- ZeroMQ Remote API de CoppeliaSim: https://manual.coppeliarobotics.com/en/zmqRemoteApiOverview.htm
- OPC Foundation, OPC UA: https://opcfoundation.org/about/opc-technologies/opc-ua/
- NumPy SeedSequence y flujos aleatorios: https://numpy.org/doc/stable/reference/random/parallel.html
- SciPy `ttest_rel` e intervalo de confianza: https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.ttest_rel.html
- Law, A. M., “Statistical Analysis of Simulation Output Data”: https://www.informs-sim.org/wsc15papers/001.pdf
- DOI de referencia para diseño/gestión de experimentos de simulación: https://doi.org/10.1287/mnsc.44.2.176

Los enlaces Google Drive de los anexos I y L no constituyen referencias verificables mientras no se publiquen con acceso estable o se incluyan los archivos versionados. No se halló scraping en el código. La existencia de una URL por sí sola no valida que un dato sea apropiado para el modelo: cada dato usado en una decisión debe conservar documento, revisión, fecha de consulta y condición de ensayo.\n