"""Schedule one hour of asynchronous lab traffic, with optional time compression."""

import argparse
import asyncio
import csv
import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import numpy as np

CATEGORIES = ("electronics", "books", "clothing")
TIERS = ("standard", "premium")
CSV_FIELDS = (
    "request_id",
    "scheduled_at",
    "sent_at",
    "scheduled_minute",
    "scheduled_offset_s",
    "sent_offset_s",
    "dispatch_delay_ms",
    "operation",
    "intended_event",
    "status_code",
    "client_latency_ms",
    "transport_error",
    "category",
    "customer_tier",
    "scenario_active",
    "order_id",
)


def _number(value, name, lower, upper):
    if type(value) not in (int, float) or not math.isfinite(value) or not lower <= value <= upper:
        raise ValueError(f"{name} must be a finite number between {lower} and {upper}")
    return value


@dataclass(frozen=True)
class Config:
    target_url: str
    n_requests: int
    mu_min: float
    sigma_min: float
    invalid_share: float
    seed: int
    scenarios: tuple

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ValueError("configuration must be a JSON object")
        url = data.get("target_url", "")
        if not isinstance(url, str):
            raise ValueError("target_url must be an HTTP(S) URL")
        parsed = urlsplit(url)
        # Accessing port validates numeric syntax and the permitted port range.
        _ = parsed.port
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or any(c in url for c in "<> \n\r\t")
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ValueError(
                "Set target_url to a real HTTP(S) API base URL without credentials/query/fragment"
            )
        count = data.get("n_requests")
        if type(count) is not int or not 0 <= count <= 1_000_000:
            raise ValueError("n_requests must be an integer between 0 and 1000000")
        seed = data.get("seed")
        if type(seed) is not int or seed < 0:
            raise ValueError("seed must be a nonnegative integer")
        mu = _number(data.get("mu_min"), "mu_min", 0, 60)
        sigma = _number(data.get("sigma_min"), "sigma_min", 0.001, 1000)
        share = _number(data.get("invalid_share"), "invalid_share", 0, 1)
        scenarios = data.get("scenarios", [])
        if not isinstance(scenarios, list):
            raise ValueError("scenarios must be a list")
        for scenario in scenarios:
            if not isinstance(scenario, dict):
                raise ValueError("each scenario must be an object")
            start = _number(scenario.get("start_min"), "start_min", 0, 60)
            end = _number(scenario.get("end_min"), "end_min", 0, 60)
            if (
                start >= end
                or scenario.get("effect") != "double_errors"
                or scenario.get("category") not in CATEGORIES
            ):
                raise ValueError(
                    "scenario requires start < end, double_errors and a supported category"
                )
        return cls(
            url.rstrip("/"), count, mu, sigma, share, seed, tuple(dict(s) for s in scenarios)
        )


def invalid_probability(config, minute, category):
    active = any(
        s["start_min"] <= minute < s["end_min"] and s["category"] == category
        for s in config.scenarios
    )
    return min(1.0, config.invalid_share * (2 if active else 1))


@dataclass(frozen=True)
class Event:
    minute: float
    category: str
    customer_tier: str
    amount: float
    operation: str
    intended_event: str
    scenario_active: bool


def make_schedule(config):
    rng = np.random.default_rng(config.seed)
    accepted = []
    while len(accepted) < config.n_requests:
        candidates = rng.normal(
            config.mu_min, config.sigma_min, max(64, config.n_requests - len(accepted))
        )
        accepted.extend(candidates[(candidates >= 0) & (candidates <= 60)].tolist())
    events = []
    for minute in sorted(accepted[: config.n_requests]):
        category = str(rng.choice(CATEGORIES))
        tier = str(rng.choice(TIERS))
        invalid = rng.random() < invalid_probability(config, minute, category)
        intention = (
            str(rng.choice(("invalid_payload", "missing_id", "invalid_state")))
            if invalid
            else "valid"
        )
        events.append(
            Event(
                minute,
                category,
                tier,
                round(float(rng.uniform(1, 500)), 2),
                str(rng.choice(("create", "get", "cancel"))),
                intention,
                any(
                    s["start_min"] <= minute < s["end_min"] and s["category"] == category
                    for s in config.scenarios
                ),
            )
        )
    return events


class OrderPool:
    """Event-loop-owned state; selecting/reserving an order never awaits."""

    def __init__(self):
        self.active = {}
        self.cancelled = {}

    def prepare(self, event):
        key = (event.category, event.customer_tier)
        active = self.active.setdefault(key, [])
        cancelled = self.cancelled.setdefault(key, [])
        operation, intention, order_id = event.operation, event.intended_event, ""
        payload = {
            "category": event.category,
            "customer_tier": event.customer_tier,
            "amount": event.amount,
        }
        params = None
        if intention == "invalid_state" and not cancelled:
            # Do not invent an ID and mislabel a 404 as an invalid-state 409.
            intention = "invalid_payload"
        if intention == "invalid_payload":
            return (
                "POST",
                "/orders",
                {**payload, "amount": -1},
                params,
                "create",
                intention,
                order_id,
                key,
            )
        if intention == "missing_id":
            order_id = "missing-" + str(uuid.uuid4())
            return "GET", f"/orders/{order_id}", None, payload, "get", intention, order_id, key
        if intention == "invalid_state":
            order_id = cancelled[-1]
            operation = "cancel"
        elif operation in ("get", "cancel"):
            if active:
                order_id = active.pop() if operation == "cancel" else active[-1]
            else:
                operation = "create"
        path = (
            "/orders"
            if operation == "create"
            else f"/orders/{order_id}" + ("/cancel" if operation == "cancel" else "")
        )
        return (
            "GET" if operation == "get" else "POST",
            path,
            payload if operation == "create" else None,
            params,
            operation,
            intention,
            order_id,
            key,
        )

    def observe(self, operation, intention, order_id, key, response):
        if response is None:
            # A timed-out cancellation may have succeeded: do not reuse it.
            return
        if operation == "create" and intention == "valid" and response.status_code == 201:
            try:
                order_id = response.json()["order"]["id"]
                if isinstance(order_id, str):
                    self.active[key].append(order_id)
            except (ValueError, KeyError, TypeError):
                pass
        elif operation == "cancel" and intention == "valid" and response.status_code == 200:
            self.cancelled[key].append(order_id)
        elif operation == "get" and intention == "valid" and response.status_code == 404:
            self.active[key] = [oid for oid in self.active[key] if oid != order_id]


async def run_simulation(
    config, output, *, duration_seconds=3600, timeout_seconds=30, transport=None, schedule=None
):
    _number(duration_seconds, "duration_seconds", 0.001, 86400)
    _number(timeout_seconds, "timeout_seconds", 0.001, 300)
    events = make_schedule(config) if schedule is None else schedule
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    pool = OrderPool()
    run_id = uuid.uuid4().hex
    loop = asyncio.get_running_loop()
    async with httpx.AsyncClient(
        timeout=timeout_seconds,
        transport=transport,
        limits=httpx.Limits(max_connections=None, max_keepalive_connections=100),
        trust_env=False,
    ) as client:
        # Exclusive creation prevents destroying earlier experiment evidence.
        with output.open("x", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
            writer.writeheader()
            started = loop.time()
            wall_start = datetime.now(timezone.utc)

            async def send(index, event):
                offset = event.minute / 60 * duration_seconds
                await asyncio.sleep(max(0, started + offset - loop.time()))
                method, path, payload, params, operation, intention, order_id, key = pool.prepare(
                    event
                )
                request_id = f"{run_id}-{index:06d}"
                sent = loop.time()
                sent_at = datetime.now(timezone.utc).isoformat()
                response, transport_error = None, ""
                try:
                    response = await client.request(
                        method,
                        config.target_url + path,
                        json=payload,
                        params=params,
                        headers={"X-Request-ID": request_id},
                    )
                except asyncio.CancelledError:
                    transport_error = "CancelledError"
                    raise
                except httpx.HTTPError as exc:
                    transport_error = type(exc).__name__
                except Exception as exc:
                    # Preserve a row even for an unexpected per-request failure.
                    transport_error = type(exc).__name__
                finally:
                    latency = (loop.time() - sent) * 1000
                    pool.observe(operation, intention, order_id, key, response)
                    writer.writerow(
                        dict(
                            request_id=request_id,
                            scheduled_at=(wall_start + timedelta(seconds=offset)).isoformat(),
                            sent_at=sent_at,
                            scheduled_minute=event.minute,
                            scheduled_offset_s=offset,
                            sent_offset_s=sent - started,
                            dispatch_delay_ms=max(0, (sent - started - offset) * 1000),
                            operation=operation,
                            intended_event=intention,
                            status_code=response.status_code if response is not None else "",
                            client_latency_ms=round(latency, 3),
                            transport_error=transport_error,
                            category=event.category,
                            customer_tier=event.customer_tier,
                            scenario_active=event.scenario_active,
                            order_id=order_id,
                        )
                    )
                    stream.flush()

            tasks = [asyncio.create_task(send(i, event)) for i, event in enumerate(events)]
            try:
                await asyncio.gather(*tasks)
                if events:
                    await asyncio.sleep(max(0, started + duration_seconds - loop.time()))
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    return len(events)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--target-url")
    parser.add_argument("--n-requests", type=int)
    parser.add_argument("--duration-seconds", type=float, default=3600)
    parser.add_argument("--timeout-seconds", type=float, default=30)
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("requests.csv"))
    args = parser.parse_args()
    try:
        data = json.loads(args.config.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("configuration must be a JSON object")
        if args.target_url is not None:
            data["target_url"] = args.target_url
        if args.n_requests is not None:
            data["n_requests"] = args.n_requests
        config = Config.from_dict(data)
        count = asyncio.run(
            run_simulation(
                config,
                args.output,
                duration_seconds=args.duration_seconds,
                timeout_seconds=args.timeout_seconds,
            )
        )
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Error: {exc}\n")
    except KeyboardInterrupt:
        parser.exit(130, "Interrupted; completed/in-flight request rows were preserved.\n")
    print(f"Recorded {count} requests in {args.output}")


if __name__ == "__main__":
    main()
