# API serverless observable en GCP

Implementación de la **Fase 1** de [plan.md](plan.md), siguiendo [instructions.md](instructions.md).
Backend de pedidos en Python 3.13, simulador asíncrono y pruebas locales.

## Estructura

```text
lab/
  backend/main.py             # entrada HTTP handle
  backend/requirements.txt
  simulator/simulator.py
  simulator/config.json       # parámetros originales, URL pendiente
  simulator/requirements.txt
  simulator/requests.csv      # generado al ejecutar; ignorado por Git
  infra/versions.tf
  infra/variables.tf
  infra/main.tf
  infra/outputs.tf
  infra/terraform.tfvars      # plantilla local; ignorada por Git
  infra/terraform.tfvars.example
tests/                        # backend, simulador e integración HTTP real
```

## Preparación local

Con Python 3.13 instalado:

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
```

Alternativa con `uv`: `uv venv --python 3.13 .venv`, luego
`uv pip install --python .venv/bin/python -r requirements-dev.txt`.

Desde la raíz, iniciar el backend en una terminal:

```bash
source .venv/bin/activate
FAILURE_RATE=0 EXTRA_LATENCY_MS=30 WORKERS=1 THREADS=8 \
  functions-framework --source lab/backend/main.py --target handle --host 127.0.0.1 --port 8080
```

En otra terminal, simulación local de 120 solicitudes en 5 segundos:

```bash
source .venv/bin/activate
python lab/simulator/simulator.py --target-url http://127.0.0.1:8080 \
  --n-requests 120 --duration-seconds 5 --output artifacts/quick-run.csv
```

El destino por defecto del CSV es `lab/simulator/requests.csv`. El simulador
rechaza sobrescribir un CSV existente; elegir un nombre nuevo para cada ejecución.
Los argumentos no cambian `config.json`. La URL de ejemplo se rechaza hasta indicar
una URL real. El script no despliega ni autentica contra GCP.

## API

| Operación | Solicitud | Resultado |
|---|---|---|
| Crear | `POST /orders` con JSON | 201 y `order` con `id` y `status: created` |
| Consultar | `GET /orders/{id}` | 200 y `order` |
| Cancelar | `POST /orders/{id}/cancel` | 200 y `status: cancelled` |
| Payload inválido | `POST /orders` | 400 `invalid_payload` |
| ID inexistente | consulta/cancelación | 404 `not_found` |
| Cancelar otra vez | pedido ya cancelado | 409 `invalid_state` |
| Método incorrecto | ruta conocida | 405 `method_not_allowed` |
| Falla simulada | operación válida | 500 `simulated_failure` |

Payload de creación:

```json
{"category": "electronics", "customer_tier": "premium", "amount": 49.90}
```

`category` y `customer_tier`: texto no vacío de hasta 80 caracteres después de eliminar espacios exteriores.
`amount`: número positivo y finito, máximo 1 000 000 000, excluye booleanos.
Campos adicionales no se almacenan. La API acepta otras categorías/tipos de cliente;
el simulador utiliza `electronics`, `books`, `clothing` y `standard`, `premium`.
El importe es un atributo de observabilidad, no un sistema contable.

Todas las respuestas incluyen `request_id`, también en `X-Request-ID`.
Si falta el header se genera un UUID. Cada solicitud que llega a `handle` emite
una sola línea JSON de aplicación con `severity`, `message: request_completed`,
`request_id`, `endpoint`, `status_code`, `outcome`, `error_type`, `latency_ms`,
`category`, `customer_tier`, `amount`. Los valores desconocidos son `null`.
Las rutas de log usan `{id}` para no multiplicar etiquetas por pedido.
Los registros de acceso del servidor son independientes de este registro de aplicación.

`FAILURE_RATE` (por defecto 0.02) acepta [0, 1]. Se aplica a operaciones válidas
antes de modificar estado: los errores deliberados conservan 400/404/409.
`EXTRA_LATENCY_MS` (por defecto 0) acepta [0, 55000], y se añade antes de validar
la operación. Configuración inválida produce 500 `configuration_error` con log.

## Estado y límites del laboratorio

Los pedidos viven **en memoria de un proceso**, protegidos por un lock para crear,
leer y cancelar atómicamente. Un reinicio los pierde. Otro worker o instancia no
los conoce. `max_instances = 1` por sí solo tampoco garantiza persistencia.
No se añadió una base de datos porque la Fase 1 no requiere servicios adicionales.

Las pruebas de ciclo de vida usan un proceso. El Terraform original permite hasta
10 instancias: antes de la Fase 2 se debe decidir entre observar explícitamente
los 404 por pérdida/aislamiento de estado o añadir almacenamiento compartido.
No interpretar esos 404 como errores intencionales del simulador. El diccionario
crece durante la ejecución; esta implementación es para experimentos acotados,
no para una API de pedidos en producción.

## Simulación y CSV

- Conserva 6000 solicitudes, μ=30, σ=10, `invalid_share=0.08`, `seed=42`.
- Usa `numpy.random.Generator.normal` con rechazo fuera de [0,60]; no recorta a
  los extremos. Ordena los instantes y agenda tareas independientes con reloj monotónico.
- `--duration-seconds` comprime esos 60 minutos lógicos; por defecto son 3600 segundos.
  Espera hasta el fin de la ventana y hasta que terminen las solicitudes pendientes.
- `double_errors` duplica la probabilidad de un error intencional para la categoría
  indicada durante `[start_min, end_min)`. Se limita a 1; escenarios superpuestos
  no se multiplican entre sí. No duplica las fallas internas del backend.
- La proporción es probabilística, no una cuota exacta. La semilla reproduce la
  agenda y las elecciones iniciales; latencias/respuestas afectan los IDs disponibles.
- Solo reutiliza IDs devueltos por creaciones exitosas. Reserva cada cancelación
  para evitar conflictos involuntarios entre tareas. Si no hay un pedido disponible,
  una operación válida pasa a creación. Si no hay cancelados para provocar 409,
  genera un payload inválido y lo registra como tal. No agrega solicitudes ocultas.
- Tras timeout de cancelación no reutiliza el ID: el servidor pudo haberla aplicado.
- Una única `httpx.AsyncClient`, sin reintentos ni redirecciones automáticas;
  timeouts/errores de transporte producen una fila con código HTTP vacío y tipo de
  excepción. El timeout por defecto es 30 s, modificable con `--timeout-seconds`.
- CSV en orden de finalización, con instantes UTC programado/enviado, minuto lógico,
  offsets monotónicos, retraso de despacho, operación efectiva, evento intencional,
  código, latencia, error de transporte, categoría, segmento, escenario e ID de pedido.
  `request_id` es único por corrida y sirve para cruzar con logs.
- No impone un semáforo que retrase artificialmente la agenda. A tasas extremas,
  el sistema operativo y el cliente pueden saturarse: observar `dispatch_delay_ms`
  y errores de transporte. Cada fila se escribe y vacía al disco al finalizar.
- Ctrl+C conserva filas completadas y de solicitudes ya iniciadas; las aún no
  enviadas no se inventan. Un CSV interrumpido puede tener menos filas.

## Verificación

```bash
source .venv/bin/activate
python -m pytest -q
ruff check lab tests
ruff format --check lab tests
```

La prueba de integración abre un puerto loopback, inicia Functions Framework,
ejecuta el CLI del simulador con 180 solicitudes en 3 segundos y cierra el servidor.
Comprueba códigos, operaciones, concurrencia y correlación de cada fila con el log.
No necesita credenciales ni acceso a GCP. En entornos restringidos necesita permiso
para abrir sockets locales. Los tests unitarios cubren también fallas 500, latencia,
configuración inválida, errores de transporte y distribución truncada.

## Preparación para Fase 2

Los cinco archivos Terraform se extrajeron de `instructions.md` como referencia;
**no se ejecutaron ni validaron con Terraform**. `terraform.tfvars.example` contiene
valores de ejemplo; la copia local `terraform.tfvars` está ignorada por Git.
La compatibilidad real del proveedor, permisos y salidas se revisará en Fase 2.

Pendiente con autorización: proyecto/región, autenticación y permisos, decisión de
estado compartido, revisión de Terraform, despliegue, métricas basadas en logs
**antes** del tráfico, dashboard único, experimento de 60 minutos y limpieza.
El experimento completo y los cambios en GCP requieren autorización explícita según
`plan.md`. No hay resultados de nube en esta entrega.

Referencias consultadas: [Functions Framework](https://github.com/GoogleCloudPlatform/functions-framework-python),
[HTTPX asíncrono](https://www.python-httpx.org/async/) y
[NumPy normal](https://numpy.org/doc/stable/reference/random/generated/numpy.random.Generator.normal.html).
