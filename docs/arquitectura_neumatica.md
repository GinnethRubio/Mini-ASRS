# Arquitectura neumática e integración de señales

## Alcance y procedencia

Descripción basada en el diagrama funcional de FluidSIM remitido como `Diagrama Mi ars.pdf` (una página). El PDF no se copia al repositorio. La descripción presenta los componentes y conexiones funcionales representados en el plano.

## Componentes representados

| Identificador | Componente | Función indicada en el diagrama |
|---|---|---|
| 0P1 | Compresor | Fuente de aire comprimido |
| 0Z1 | Unidad de mantenimiento, ajuste indicado 6 bar | Preparación del aire antes de distribuirlo al circuito |
| 0B1 | Presostato, umbral indicado 4 bar | Devuelve al PLC la señal de presión suficiente; su pérdida puede inhibir o interrumpir el ciclo |
| 1A1 | Cilindro de doble efecto | Eje neumático Y para avanzar a recoger/depositar y retraer |
| 1B1 / 1B2 | Sensores de fin de carrera | Confirman Y retraído / Y extendido |
| 1V1 / 1V2 | Reguladores de caudal | Ajustan el caudal de avance/retorno de 1A1 según el diagrama |
| 1M1 / 1M2 | Bobinas de la válvula 5/2 biestable | Mandan avance / retorno de 1A1 |
| 1A2 | Actuador de simple efecto de la pinza | Cierra para sujetar y abre para liberar el sobre |
| 2B1 / 2B2 | Sensores de pinza | Confirman pinza abierta / cerrada |
| 2V1 | Regulador de caudal | Ajuste del movimiento del actuador de pinza |
| 2M1 / 2M2 | Bobinas de la válvula 3/2 biestable | Mandan cierre / liberación de la pinza según asignación del plano |
| S1 / S4 / S2 / S3 | Mandos marcha, recuperar, paro y rearme | Órdenes del operador; S1, S4 y S3 se indican NA, S2 NC |
| S0 / K0 | Paro de emergencia y relé/confirmación de emergencia | Al accionar S0 cae K0; el PLC lee el estado de permiso de emergencia |
| H1–H4 | Pilotos | Indican marcha, movimiento, recuperación y alarma |

Las presiones de 6 bar y 4 bar son valores escritos en el diagrama remitido. No son mediciones de presión, criterios normativos ni valores de desempeño verificados.

## Mapa de interconexión

El dibujo asigna tres canales lógicos de comunicación de FluidSIM con el PLC:

| Enlace representado | Contenido |
|---|---|
| FluidSIM Out, módulo 1 puerto 1 → PLC `IN0` | Fin de carrera 1B1, fin de carrera 1B2, pinza abierta 2B1 y pinza cerrada 2B2 en bits 0–3 |
| FluidSIM Out, módulo 1 puerto 2 → PLC `IN1` | Marcha S1, recuperar S4, paro S2, confirmación de emergencia K0, presión 0B1 y rearme S3 en bits 0–5 |
| PLC `OUT0` → FluidSIM In, módulo 1 puerto 1 | Accionamientos 1M1, 1M2, 2M1 y 2M2 en bits 0–3; pilotos H1–H4 en bits 4–7 |

El mapa lógico de entrada coincide con el texto de PLC: la confirmación K0 se asigna a `IN1.3`.

La cadena entre los subsistemas es:

```mermaid
%%{init: {"flowchart": {"htmlLabels": true, "nodeSpacing": 80, "rankSpacing": 100, "curve": "linear"}}}%%
flowchart LR
    FS["FluidSIM<br/>actuadores Y y pinza"] <-->|"OPC DA<br/>IN0 · IN1 · OUT0"| PLC["CODESYS<br/>secuencia e interbloqueos"] <-->|"OPC UA<br/>órdenes, estados y confirmaciones"| PY["Coordinador Python"] <-->|"ZeroMQ Remote API<br/>órdenes y estado de escena"| SIM["CoppeliaSim<br/>ejes X/Z · bandas · QR"]
    classDef ext fill:#eef4ff,stroke:#4263a5,color:#172b4d;
    classDef ctl fill:#e9f7ef,stroke:#2f855a,color:#173b2a;
    class FS,SIM ext;
    class PLC,PY ctl;
```

FluidSIM modela las órdenes y confirmaciones del cilindro Y y de la pinza. Python intercambia variables con CODESYS por OPC UA y conecta la planta 3D de CoppeliaSim por ZeroMQ Remote API; refleja allí los movimientos mecánicos coordinados (incluidos Y y la pinza) y ejecuta la cinemática X/Z, bandas y QR.

## Secuencia funcional resumida

1. La banda recibe el sobre y se lee el código QR.
2. El PLC selecciona la celda o procesa la solicitud de recuperación.
3. Python ordena a la escena el movimiento X/Z hasta el origen.
4. El PLC energiza 1M1; 1A1 avanza y 1B2 confirma extensión.
5. El PLC acciona 2M1 para sujetar y espera 2B2.
6. Se acciona 1M2 para retraer el sobre y se confirma con 1B1.
7. Python desplaza X/Z hasta la celda de depósito o la estación de despacho.
8. Se extiende Y, se ajusta Z cuando corresponde, se libera con 2M2 y se retrae Y.
9. La operación registra el código QR asociado con la ubicación.

## Versiones

Las capturas de versiones aportadas identifican CODESYS V3.5 SP9 Patch 1+ y FluidSIM 4.2p/1.67 Pneumática. El usuario indica que son licencias académicas del laboratorio de la Uniagustiniana.\n