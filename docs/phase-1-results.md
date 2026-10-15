# Resultados de Fase 1

Validación local con CPython 3.13.16. No se ejecutaron comandos de Terraform
ni operaciones de Google Cloud. Cambios sin commit en `implementation/phase-1-python`.

## Archivos y funcionalidades

- `lab/backend/main.py` y `requirements.txt`: crear, consultar y cancelar pedidos;
  validación, 400/404/409/500, fallas probabilísticas, latencia y un log JSON por solicitud.
- `lab/simulator/simulator.py`, `requirements.txt`, `config.json`: agenda normal truncada,
  HTTP asíncrono, escenarios, IDs únicos, transporte y CSV. Parámetros originales preservados.
- `lab/infra/`: cinco archivos de referencia extraídos del laboratorio, sin ejecutar;
  `terraform.tfvars.example` permite reproducir la plantilla ignorada por Git.
- `tests/`: pruebas de backend, simulación e integración HTTP real.
- `README.md`, configuración de pytest/ruff y dependencias de desarrollo: instrucciones
  reproducibles y límites documentados.

## Evidencia

- Suite completa: **39 pruebas aprobadas en 5.23 s**.
- `ruff check lab tests`: aprobado.
- `ruff format --check lab tests`: 5 archivos formateados.
- `python -m compileall -q lab`: aprobado.
- `git diff --check`: aprobado.
- Integración: **180 solicitudes**, ventana local de 3 segundos, 0 errores de transporte.
- Códigos observados: `{"200": 67, "201": 52, "400": 32, "404": 18, "409": 11}`.
- Cada request_id del CSV tiene exactamente un log de aplicación; un log adicional
  corresponde al sondeo de disponibilidad. Se verificó superposición de solicitudes.
- Latencia y fallas 500 verificadas además en pruebas del backend; la integración
  desactiva fallas aleatorias para comprobar las secuencias de negocio.
- Artefactos reales: `artifacts/final-validation/test_real_framework_and_simula0/requests.csv`, `backend.jsonl`, `server.stderr`.
  El CSV generado también se copió a `lab/simulator/requests.csv`; no contiene datos ficticios.
  Los artefactos se ignoran en Git.

## Decisiones y límites

1. Memoria local protegida por lock: evita carreras dentro del proceso, pero pierde
   pedidos al reiniciar y no comparte datos entre workers/instancias. Si se requiere
   persistencia, habrá que añadir almacenamiento compartido en Fase 2.
2. `double_errors` duplica la probabilidad de invalidez solo en la categoría y ventana
   indicadas. No altera `FAILURE_RATE`; una interpretación distinta requiere ajustar
   el escenario antes del experimento.
3. Los tiempos rápidos escalan los mismos 60 minutos lógicos. La semilla reproduce
   agenda/elecciones, no el comportamiento de la red ni la disponibilidad de pedidos.
4. Infraestructura copiada como referencia, no validada por Terraform ni desplegada.
   La compatibilidad real con proveedores y GCP queda pendiente.
5. Trabajo conservado en el checkout actual, en rama de implementación y sin commit.

## Revisión independiente

Se corrigieron ambas observaciones con pruebas que fallaron antes del arreglo:
normalización coherente de atributos con espacios y registro explícito de
`CancelledError` en CSV para solicitudes interrumpidas. No quedan observaciones
pendientes de esa revisión.

## Próxima fase

Requiere autorización explícita conforme a `plan.md`: revisar estado/persistencia,
proyecto/región, permisos, Terraform y despliegue; crear métricas basadas en logs
antes del experimento; construir el dashboard. La simulación de 60 minutos necesita
autorización separada. No se verificó ningún recurso en GCP en Fase 1.
