import importlib
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from flask import Flask, request


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("FAILURE_RATE", "0")
    monkeypatch.setenv("EXTRA_LATENCY_MS", "0")
    from lab.backend import main

    main = importlib.reload(main)
    app = Flask(__name__)
    app.add_url_rule(
        "/",
        endpoint="root",
        defaults={"path": ""},
        view_func=lambda path: main.handle(request),
        methods=["GET", "POST", "DELETE"],
    )
    app.add_url_rule(
        "/<path:path>",
        endpoint="paths",
        view_func=lambda path: main.handle(request),
        methods=["GET", "POST", "DELETE"],
    )
    return app


PAYLOAD = {"category": "electronics", "customer_tier": "premium", "amount": 12.50}


def test_order_lifecycle_and_exactly_one_log_per_request(api, capsys):
    client = api.test_client()
    created = client.post("/orders", json=PAYLOAD, headers={"X-Request-ID": "test-create"})
    assert created.status_code == 201
    order = created.get_json()["order"]
    path = f"/orders/{order['id']}"
    assert client.get(path).get_json()["order"]["status"] == "created"
    assert client.post(path + "/cancel").get_json()["order"]["status"] == "cancelled"
    assert client.post(path + "/cancel").status_code == 409
    assert client.get("/orders/missing").status_code == 404
    logs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(logs) == 5
    assert [log["status_code"] for log in logs] == [201, 200, 200, 409, 404]
    assert logs[0]["request_id"] == "test-create"
    assert logs[0]["category"] == "electronics"
    assert logs[0]["customer_tier"] == "premium"
    required = {
        "severity",
        "message",
        "request_id",
        "endpoint",
        "status_code",
        "outcome",
        "error_type",
        "latency_ms",
        "category",
        "customer_tier",
    }
    assert all(required <= log.keys() and log["message"] == "request_completed" for log in logs)
    assert logs[3]["outcome"] == "client_error"
    assert logs[3]["error_type"] == "invalid_state"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {**PAYLOAD, "amount": True},
        {**PAYLOAD, "amount": -1},
        {**PAYLOAD, "amount": float("inf")},
        {**PAYLOAD, "category": ""},
        {**PAYLOAD, "customer_tier": []},
    ],
)
def test_invalid_payload_returns_400(api, payload):
    assert api.test_client().post("/orders", json=payload).status_code == 400


def test_malformed_json_and_routes(api):
    client = api.test_client()
    assert client.post("/orders", data="{bad", content_type="application/json").status_code == 400
    assert client.get("/orders").status_code == 405
    assert client.get("/unknown").status_code == 404


def test_failure_injection_and_latency(api, monkeypatch, capsys):
    monkeypatch.setenv("FAILURE_RATE", "1")
    monkeypatch.setenv("EXTRA_LATENCY_MS", "25")
    start = time.perf_counter()
    result = api.test_client().post("/orders", json=PAYLOAD)
    assert result.status_code == 500
    assert time.perf_counter() - start >= 0.025
    log = json.loads(capsys.readouterr().out)
    assert log["outcome"] == "server_error"
    assert log["error_type"] == "simulated_failure"
    assert log["latency_ms"] >= 25
    assert log["category"] == "electronics"


@pytest.mark.parametrize(
    "name,value",
    [
        ("FAILURE_RATE", "nan"),
        ("FAILURE_RATE", "2"),
        ("EXTRA_LATENCY_MS", "-1"),
        ("EXTRA_LATENCY_MS", "bad"),
    ],
)
def test_invalid_environment_fails_with_structured_log(api, monkeypatch, capsys, name, value):
    monkeypatch.setenv(name, value)
    assert api.test_client().post("/orders", json=PAYLOAD).status_code == 500
    assert json.loads(capsys.readouterr().out)["error_type"] == "configuration_error"


def test_concurrent_cancel_is_atomic(api):
    order = api.test_client().post("/orders", json=PAYLOAD).get_json()["order"]

    def cancel(_):
        return api.test_client().post(f"/orders/{order['id']}/cancel").status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(cancel, range(8)))
    assert statuses.count(200) == 1
    assert statuses.count(409) == 7


def test_business_fields_are_normalized_before_storage(api, capsys):
    payload = {**PAYLOAD, "category": " " * 80 + "electronics", "customer_tier": " premium "}
    response = api.test_client().post("/orders", json=payload)
    assert response.status_code == 201
    assert response.get_json()["order"]["category"] == "electronics"
    assert response.get_json()["order"]["customer_tier"] == "premium"
    log = json.loads(capsys.readouterr().out)
    assert log["category"] == "electronics"
