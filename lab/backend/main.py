"""Observable orders lab. State is intentionally local to this Python process."""

import json
import math
import os
import random
import re
import threading
import time
import uuid

import functions_framework
from flask import jsonify

_orders = {}
_lock = threading.Lock()
_log_lock = threading.Lock()
_ORDER_PATH = re.compile(r"^/orders/([^/]+)(/cancel)?$")


class APIError(Exception):
    def __init__(self, status, error_type):
        self.status = status
        self.error_type = error_type


def _settings():
    try:
        rate = float(os.getenv("FAILURE_RATE", "0.02"))
        latency = float(os.getenv("EXTRA_LATENCY_MS", "0"))
        if not math.isfinite(rate) or not 0 <= rate <= 1:
            raise ValueError
        if not math.isfinite(latency) or not 0 <= latency <= 55000:
            raise ValueError
        return rate, latency
    except ValueError:
        raise APIError(500, "configuration_error") from None


def _business_fields(data):
    """Never emit arbitrary objects or non-finite floats into structured logs."""
    fields = {"category": None, "customer_tier": None, "amount": None}
    if isinstance(data, dict):
        for key in ("category", "customer_tier"):
            if isinstance(data.get(key), str):
                fields[key] = data[key].strip()[:80]
        value = data.get("amount")
        if type(value) in (int, float):
            try:
                if math.isfinite(value):
                    fields["amount"] = value
            except OverflowError:
                pass
    return fields


def _validate(data):
    if not isinstance(data, dict):
        raise APIError(400, "invalid_payload")
    for key in ("category", "customer_tier"):
        if not isinstance(data.get(key), str) or not 1 <= len(data[key].strip()) <= 80:
            raise APIError(400, "invalid_payload")
    amount = data.get("amount")
    if type(amount) not in (int, float) or not 0 < amount <= 1_000_000_000:
        raise APIError(400, "invalid_payload")


@functions_framework.http
def handle(request):
    started = time.perf_counter()
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    status, error_type, body = 500, None, {}
    fields = _business_fields(None)
    endpoint = "unmatched"
    try:
        data = request.get_json(silent=True)
        fields.update(_business_fields(data))
        rate, latency = _settings()
        time.sleep(latency / 1000)
        match = _ORDER_PATH.fullmatch(request.path)
        if request.path == "/orders":
            endpoint = "/orders"
            if request.method != "POST":
                raise APIError(405, "method_not_allowed")
            _validate(data)
            if random.random() < rate:
                raise APIError(500, "simulated_failure")
            order = {"id": str(uuid.uuid4()), "status": "created", **_business_fields(data)}
            with _lock:
                _orders[order["id"]] = order
                body = {"order": dict(order)}
            status = 201
        elif match:
            order_id, cancel = match.groups()
            endpoint = "/orders/{id}/cancel" if cancel else "/orders/{id}"
            if request.method != ("POST" if cancel else "GET"):
                raise APIError(405, "method_not_allowed")
            with _lock:
                order = _orders.get(order_id)
                if order is None:
                    # Simulator supplies context for intentionally missing IDs.
                    fields.update(_business_fields(request.args.to_dict()))
                    raise APIError(404, "not_found")
                fields.update(_business_fields(order))
                if cancel and order["status"] != "created":
                    raise APIError(409, "invalid_state")
                if random.random() < rate:
                    raise APIError(500, "simulated_failure")
                if cancel:
                    order["status"] = "cancelled"
                body = {"order": dict(order)}
            status = 200
        else:
            raise APIError(404, "not_found")
    except APIError as exc:
        status, error_type = exc.status, exc.error_type
        body = {"error": error_type}
    except Exception:
        status, error_type = 500, "internal_error"
        body = {"error": error_type}
    finally:
        outcome = (
            "server_error" if status >= 500 else "client_error" if status >= 400 else "success"
        )
        log = {
            "severity": "ERROR" if status >= 500 else "WARNING" if status >= 400 else "INFO",
            "message": "request_completed",
            "request_id": request_id,
            "endpoint": endpoint,
            "status_code": status,
            "outcome": outcome,
            "error_type": error_type,
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            **fields,
        }
        with _log_lock:
            print(json.dumps(log, allow_nan=False), flush=True)
    response = jsonify({**body, "request_id": request_id})
    response.status_code = status
    response.headers["X-Request-ID"] = request_id
    return response
