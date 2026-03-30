![Protocol](https://img.shields.io/badge/Protocol-Modbus-blue)
![Type](https://img.shields.io/badge/Type-Sensor%20Simulator-important)
![Environment](https://img.shields.io/badge/Environment-Industrial%20OT-critical)
![Use Case](https://img.shields.io/badge/Use%20Case-Testing%20%7C%20SCADA-success)
![Language](https://img.shields.io/badge/Language-Python-green)
![License](https://img.shields.io/github/license/p2610/modbus-slave-simulator)
![Platform](https://img.shields.io/badge/Platform-Linux-lightgrey)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey)
![ICS](https://img.shields.io/badge/ICS-Ready-blueviolet)
![Integration](https://img.shields.io/badge/Integration-PLC%20%7C%20SCADA-orange)

# Modbus Slave Simulator / Simulador de esclavo Modbus

Simulador de esclavo Modbus con soporte TCP y RTU, generación dinámica de valores y handlers personalizados.

<img width="1908" height="884" alt="Captura desde 2026-03-29 23-26-49" src="https://github.com/user-attachments/assets/791ceb00-34d9-436a-9234-a6058b912ff0" />

## Tabla de contenidos / Table of Contents

- Descripción
- Características
- Requisitos
- Instalación
- Quickstart
- Configuración
- Arquitectura
- API
- Tests
- Troubleshooting
- Maintainer
- Contribuir
- Licencia

## Descripción / Description

Este repositorio contiene un simulador de esclavo Modbus diseñado para pruebas, desarrollo y demostraciones. Implementa servidores Modbus TCP y RTU (si hay puerto serie disponible), mantiene un datastore configurable, genera valores según diferentes modos (static, random, sine, ramp, manual) y añade handlers personalizados para funciones avanzadas.

## Features / Características

- Soporte Modbus TCP y Modbus RTU
- Generación dinámica de valores: `static`, `random`, `sine`, `ramp`, `manual`
- Handlers personalizados: FC08 (Diagnostics), FC22 (Mask Write), FC23 (Read/Write Multiple), FC43 (Read Device Identification)
- Monitor en consola (Rich si está instalado) y modo sin monitor
- Registro opcional de transacciones en CSV
- API mínima para integrar y construir `SimulationState` desde JSON
- Encoding configurable por sensor: ABCD, CDAB, DCBA, BADC
- Compatible con notación Modicon clásica (40001, 30001...)

## Requisitos / Requirements

- Python 3.8+
- Dependencias listadas en `requirements.txt`

## Instalación / Installation

Instala las dependencias:

```bash
python -m pip install -r requirements.txt
```

> Nota: se recomienda crear un entorno virtual mínimo en `app/` que puede usarse para aislar dependencias.

## Quickstart / Ejecución rápida

Crear puertos serie virtuales para pruebas (Linux) — ejemplo con `socat`:

```bash
socat -d -d pty,link=/tmp/ttyV2,raw,echo=0 pty,link=/tmp/ttyV3,raw,echo=0
# esto crea dos dispositivos pty; conecta uno al simulador y usa el otro desde la aplicación cliente
```

Ejecuta el simulador con el archivo de configuración de ejemplo:

```bash
python simulator.py --config simulator_config.json
```

Opciones comunes:

```bash
python simulator.py --tcp-only          # solo servidor TCP
python simulator.py --rtu-only          # solo servidor RTU
python simulator.py --no-monitor        # deshabilita el monitor en consola
python simulator.py --log-file txn.csv  # guarda transacciones en CSV
python simulator.py --refresh 0.5       # intervalo de actualización (segundos)
python simulator.py --inspect-mem       # inspeccionar espejo de memoria
python simulator.py --help              # ver todas las opciones
```

## Configuración / Configuration

La configuración principal está en `simulator_config.json`. Es un JSON con dos bloques principales: `simulator` y `slaves`.

Ejemplo (abreviado):

```json
{
  "simulator": {
    "tcp_host": "0.0.0.0",
    "tcp_port": 5020,
    "serial_port": "/dev/ttyV2",
    "baudrate": 19200,
    "update_interval_s": 1.0,
    "device_id": 1,
    "vendor_name": "Acme",
    "product_code": "MBSIM",
    "major_minor_revision": "1.0"
  },
  "slaves": [
    {
      "unit_id": 1,
      "registers": [
        {
          "name": "Temp",
          "modicon_address": 40001,
          "storage_address": 1000,
          "value_type": 4,
          "byte_order": "CDAB",
          "value_mode": "sine",
          "value": 20.0,
          "min": 10.0,
          "max": 30.0,
          "period_s": 60,
          "unit": "C"
        }
      ]
    }
  ]
}
```

Campos importantes:

- `value_type` mapping: `0=int16`, `1=uint16`, `2=int32`, `3=uint32`, `4=float32`.
- `byte_order`: para 32-bit soporta `ABCD`, `CDAB`, `DCBA`, `BADC`; para 16-bit usar `AB`.
- `modicon_address`: dirección Modicon (p. ej. 40001 para HR)
- `storage_address`: dirección interna en el datastore (mapeo del simulador)

Para detalles completos revisa `simulator_config.json` en la raíz.

## Arquitectura / Architecture

Principales módulos en `core/`:

- `core/datastore.py` — `SimulationState`, carga de configuración y mirror de memoria
- `core/encoder.py` — `EncoderDecoder` (encode/decode, byte order)
- `core/value_engine.py` — `ValueEngine` (modos de generación de datos)
- `core/fc_handlers.py` — handlers Modbus personalizados (FC08/22/23/43)
- `core/server_tcp.py` — servidor Modbus TCP
- `core/server_rtu.py` — servidor Modbus RTU
- `core/monitor.py` — monitor de consola (Rich/Plain/Null)
- `simulator.py` — entrypoint CLI y REPL runtime

## API / Integration

Funciones y clases útiles para integraciones desde Python:

- `core.datastore.load_configuration(path)` — carga JSON de configuración.
- `core.datastore.build_state_from_config(config, ...)` — construye `SimulationState` a partir del JSON.
- `core.encoder.EncoderDecoder.encode/decode` — codifica y decodifica entre valores y registros Modbus.
- `core.value_engine.ValueEngine.compute(sensor, now=None)` — computa el valor actual de un sensor según su modo.

Consulta los módulos en `core/` para ver firmas y opciones.

## Tests

Pruebas unitarias básicas (ej.: `EncoderDecoder`) están en `tests/`.

Ejecuta las pruebas con:

```bash
pytest -q
```

o, si usas el virtualenv incluido:

```bash
./app/bin/pytest -q
```

## Troubleshooting / Resolución de problemas

- Si el puerto serie indicado en la configuración no existe, el servidor RTU emite una advertencia y el simulador continúa en modo TCP.
- `rich` es opcional: si no está instalado, el simulador usa un monitor simplificado; usa `--no-monitor` para desactivar completamente la salida.
- Para depurar transacciones, usa `--log-file <archivo.csv>` para guardar transacciones en CSV.

## Casos de uso / Use Cases

- Integración y pruebas automáticas de clientes Modbus (CI).
- Desarrollo y depuración de interfaces SCADA/RTU sin hardware físico.
- Enseñanza y demostraciones de protocolo Modbus.
- Mock para pruebas de rendimiento y resiliencia (fuzzing, pruebas de error en bus).
- Hardware-in-the-loop: simular periféricos mientras se desarrolla controladores.

----

## Maintainer / Mantenedor

- Paolo Arrunategui

---

## Contribuir / Contributing

Pull requests bienvenidos. Para cambios mayores, abre un issue primero para discutir la propuesta.

## Licencia / License

Este proyecto está bajo la licencia MIT. Véase el archivo `LICENSE`.
