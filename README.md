# TFM - Monitoreo y modelado de consumo electrico

Repositorio del trabajo de fin de master: Diseño e implementación de un sistema de procesamiento de datos para la captura modelado y predicción del consumo eléctrico en tiempo real

## Estructura del repositorio

```text
.
|-- LICENCE
|-- README
|-- Arquitectura/
|   |-- AWS/
|   |   |-- <Scripts IaC>
|   |   `-- certs/
|   |       `-- <Claves para autenticación>
|   `-- Dispositivo/
|       |-- Codigo/
|       |   `-- <Scripts para microcontrolador>
|       `-- PCB/
|           `-- <Información para diseño de la PCB>
|-- Codigo/
|   |-- requirements.txt
|   |-- data/
|   |   `-- conjuntos/
|   |       `-- <Datos para entrenamiento>
|   |-- notebooks/
|   |   `-- <Cuadernos para experimentación de modelos>
|   `-- scripts/
|       `-- <Scripts para entrenamiento de modelos>
`-- mlfow/
    `-- <Artefactos del entrenamiento en Mlflow>
```

## Descripcion breve por modulo

- Arquitectura/AWS: Infraestructura y procesos de nube para ingestion, ETL/ELT y orquestacion de datos/modelos (Terraform, Lambda y Glue).
- Arquitectura/AWS/certs: Material criptografico del dispositivo IoT para autenticacion/seguridad.
- Arquitectura/Dispositivo/Codigo: Firmware del microcontrolador para lectura y envio de datos de sensores electricos.
- Arquitectura/Dispositivo/PCB: Diseno electronico y documentacion de la placa de sensores.
- Codigo/data: Conjuntos de entrenamiento y prueba usados por notebooks y scripts.
- Codigo/notebooks: Flujo exploratorio y comparativo de modelos de series temporales.
- Codigo/scripts: Automatizaciones de entrenamiento y evaluacion (enfocadas en AutoARIMA por fase).
- mlfow: Artefactos de experimentacion y salidas de modelado (metricas, graficos, resumenes y predicciones).

## Tableros

En esta sección se encuentran los enlaces a los tableros construidos como parte del proyecto en el aplicativo Grafana:

1. [Tablero de parámetros eléctricos](https://tfm-uoc.matiaslara.com/public-dashboards/49b5be07474c41a68b8e9d6bb1102106)
2. [Tablero de proyecciones](https://tfm-uoc.matiaslara.com/public-dashboards/cbe3b65494014bbe94f6d87ed4529e8c)

## Flujo general

1. Captura de mediciones desde el dispositivo.
2. Ingestion y transformacion en AWS.
3. Preparacion de datasets para modelado.
4. Entrenamiento y evaluacion de modelos en notebooks/scripts.
5. Almacenamiento de artefactos y predicciones para analisis posterior.
