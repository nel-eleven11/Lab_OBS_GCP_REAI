# Phase 2 — experiment results

## Run

- Project: `observableapi`; service: `orders-api` in `us-central1`.
- Configuration: 6,000 requests, 60-minute schedule, `N(30, 10)` minutes
  truncated to `[0, 60]`, 8% base intentional-invalid probability, seed 42,
  and `double_errors` for electronics during minutes 35–45.
- Run interval: `2026-10-16T06:08:34Z` through `2026-10-16T07:08:24Z` for
  first and last dispatched requests.
- Full dataset: `lab/simulator/requests-phase2-full-20261016-network-enabled.csv`.
  It contains 6,000 rows, 6,000 unique request IDs, and all 6,000 requests
  completed as either an HTTP response or a recorded transport error.

The existing `lab/simulator/requests.csv` contained 179 rows from an earlier
run. It was left untouched. A first attempt inside the restricted network
sandbox produced 7 connection failures and was stopped; the successful,
network-enabled run is stored in the separate file above.

## Outcomes

| Result | Requests |
| --- | ---: |
| HTTP 201 | 1,921 |
| HTTP 200 | 3,311 |
| HTTP 400 | 171 |
| HTTP 404 | 291 |
| HTTP 409 | 191 |
| HTTP 500 | 102 |
| Client transport errors | 13 |

The 5,987 HTTP responses comprise 5,232 successes, 653 client errors, and 102
server errors. The 102 HTTP 500 responses are 1.70% of all HTTP responses,
close to the configured 2% simulated failure rate.

The simulator marked 548 requests as intentionally invalid: 462 outside the
scenario (8.40% of 5,497 requests) and 86 inside it (17.10% of 503 requests).
This is close to the configured 8% base probability and 16% during the
`double_errors` scenario. Among HTTP responses, the observed error share was
11.94% outside the scenario and 20.00% inside it. These observed error rates
also include simulated server errors and the effects of in-memory order state.

## Latency and transport

Cloud Logging contained 5,987 structured `request_completed` records. Each
HTTP response in the CSV had exactly one log with the same `request_id` and
status code; no server log IDs were missing or extra. All 13 rows without an
HTTP response had no matching structured application log.

The application-measured `latency_ms` was P50 0.10 ms, P95 0.21 ms, and maximum
6.81 ms. Client-measured latency for the 5,987 HTTP responses was P50 81.91 ms
and P95 229.16 ms. Across all 6,000 attempts, including transport failures,
client P95 was 234.15 ms and the maximum was 28.53 seconds. These metrics use
different measurement boundaries: the server metric measures handler time,
while the client metric also includes network and platform time.

Thirteen transport errors (8 `ConnectError`, 5 `RemoteProtocolError`) clustered
around scheduled minute 40.2–40.36. The 13 requests have no application log;
a bounded Cloud Run request-log query found no platform 5xx entries in the
exact burst window. The cause remains undetermined. They account for 0.22% of
the experiment and should be visible as client-side failures in the CSV, but
not in the application outcome metric.

## In-memory state behavior

Among 5,452 requests marked valid, 108 returned 404. Six requests marked
`invalid_state` also returned 404 instead of 409. This is consistent with the
known deployment limitation: in-memory orders are not shared across Cloud Run
instances, so a later operation can reach an instance without the order state
created or cancelled elsewhere. The counts should be interpreted as an
observed effect consistent with instance-local state, not as proof that every
404 had that cause.

## Observability

The Cloud Monitoring dashboard is
`3c0f0fd3-535e-4bb7-abd1-7ebe70ba02f8` ("Observable Orders API"). It contains
native Cloud Run request count and latency, the two log-based metrics, two Log
Analytics charts, and a Logs Panel. The metrics were created before this run,
so they can include these events. The server log and CSV comparisons above
confirmed the request-level data; opening the dashboard in the console remains
the way to visually inspect its rendered charts.

## Follow-up

- Review the dashboard and the 13 transport failures in the Cloud Console.
- Keep in mind that orders remain in process memory, as chosen for this lab.
- Review infrastructure and potential charges before cleanup. The `_Default`
  bucket's Log Analytics upgrade is permanent and is not reversed by
  `terraform destroy`.
