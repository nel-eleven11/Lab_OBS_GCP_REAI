# Phase 2 — deployment and observability

Branch `implementation/phase-2-gcp` is based on fetched `origin/main` at
`8275805`, which contains Phase 1. The GCP project is `observableapi`, in
`us-central1`. The API keeps orders in process memory as agreed; separate
instances do not share order state, so the simulator should create and use a
given order within the same request flow where possible.

## Completed

- Refreshed gcloud login and ADC for the project; active account is
  `nel2003501@gmail.com`. No credential material is stored in the repository.
- Confirmed the project is active and verified enabled services. Logging,
  Monitoring and Storage were already enabled; Terraform enabled the remaining
  APIs required by the deployment.
- Reviewed the Terraform plan with the user, then applied the saved plan:
  19 resources added, zero changed, zero destroyed. This deployed the public
  `orders-api` Cloud Run function, runtime/build identities, IAM grants, source
  bucket and APIs, and upgraded the `_Default` log bucket to Log Analytics with
  30-day retention. That bucket upgrade is permanent.
- Function URL: `https://us-central1-observableapi.cloudfunctions.net/orders-api`.
  It is set in `lab/simulator/config.json`; simulator parameters remain intact.
- Sent a malformed `POST /orders` with request ID
  `phase2-smoke-20261015-02`. The API returned HTTP 400 with
  `invalid_payload`, and Cloud Logging returned the matching structured
  `request_completed` record with the same request ID.
- A fresh Terraform plan reported no changes.
- Confirmed no existing log-based metrics or Monitoring dashboards in the
  project.

## Observability preparation

The local JSON definitions in `lab/observability/` specify two log-based
metrics (request outcome counter and backend latency distribution) and one
dashboard with native Cloud Run request count/latency, those log metrics,
Log Analytics charts for response codes and business attributes, and a Logs
Panel. The full simulator has not been run.

Both requested log-based metrics are now created in `observableapi`:
`orders_api_request_outcomes` and `orders_api_backend_latency_ms`. The dashboard
JSON passed `gcloud monitoring dashboards create --validate-only` and was then
created as dashboard `3c0f0fd3-535e-4bb7-abd1-7ebe70ba02f8`. A subsequent
describe returned the expected eight widgets. Both metric definitions were
also described successfully from the project.

After metric creation, one canary request (`phase2-metric-canary-20261015-01`)
returned HTTP 400 `invalid_payload`; Cloud Logging contains the corresponding
structured record with outcome `client_error`, status 400, endpoint `/orders`,
and the matching request ID. The dashboard and metrics are configured, though
full metric time-series chart rendering remains to be visually checked in the
Cloud Console.

The user explicitly authorized and the full 60-minute experiment has now run.
The completed dataset has 6,000 rows and unique request IDs at
`lab/simulator/requests-phase2-full-20261016-network-enabled.csv`. It recorded
5,987 HTTP responses and 13 transport errors. A Cloud Logging comparison found
exactly one matching structured completion log for every HTTP response and no
application log for the transport failures. Detailed counts, latency analysis,
scenario results, and the in-memory-state findings are in
`docs/phase-2-results.md`.

GCP metric-descriptor discovery through the live Monitoring MCP tool is
unavailable in this session, and this gcloud installation lacks a
`monitoring metrics` descriptor command. The dashboard uses the official metric
catalog as a fallback; native metric schemas should be confirmed by the
dashboard validation and first live data. Native metric data can lag and does
not count requests that fail before reaching the container. Logs-based metrics
only count log entries written after metric creation.

## Remaining

1. Visually inspect the saved dashboard in the Cloud Console; its API
   configuration was validated and saved, but the rendered charts were not
   opened in a browser during this run.
2. Review the 13 transport failures and observed instance-local order behavior.
3. Later review Terraform destroy and irreversible resources before cleanup.
