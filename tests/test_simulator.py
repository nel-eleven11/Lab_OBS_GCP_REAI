import asyncio
import csv
from dataclasses import replace

import httpx
import numpy as np
import pytest

from lab.simulator import simulator as sim


def config(**updates):
    data = dict(
        target_url="http://localhost:8080",
        n_requests=40,
        mu_min=30,
        sigma_min=10,
        invalid_share=0.08,
        seed=42,
        scenarios=[],
    )
    data.update(updates)
    return sim.Config.from_dict(data)


def test_truncated_normal_is_reproducible_and_not_clipped():
    cfg = config(n_requests=10000, mu_min=0, sigma_min=20)
    a = sim.make_schedule(cfg)
    b = sim.make_schedule(cfg)
    times = np.array([e.minute for e in a])
    assert a == b
    assert len(times) == 10000
    assert np.all((times > 0) & (times < 60))
    assert np.all(times[1:] >= times[:-1])
    assert 14 < times.mean() < 18


def test_scenario_only_doubles_matching_category_in_window():
    cfg = config(
        scenarios=[dict(start_min=35, end_min=45, effect="double_errors", category="electronics")]
    )
    assert sim.invalid_probability(cfg, 35, "electronics") == 0.16
    assert sim.invalid_probability(cfg, 44.9, "electronics") == 0.16
    assert sim.invalid_probability(cfg, 45, "electronics") == 0.08
    assert sim.invalid_probability(cfg, 36, "books") == 0.08
    assert sim.invalid_probability(cfg, 34, "electronics") == 0.08


@pytest.mark.parametrize(
    "updates",
    [
        dict(n_requests=-1),
        dict(n_requests=True),
        dict(sigma_min=0),
        dict(mu_min=100),
        dict(invalid_share=1.1),
        dict(invalid_share=float("nan")),
        dict(target_url="file:///tmp/x"),
        dict(target_url="https://<salida function_url de terraform>"),
        dict(
            scenarios=[
                dict(start_min=45, end_min=35, effect="double_errors", category="electronics")
            ]
        ),
        dict(scenarios=[dict(start_min=1, end_min=2, effect="unknown", category="electronics")]),
    ],
)
def test_invalid_configuration_is_rejected(updates):
    with pytest.raises(ValueError):
        config(**updates)


def test_async_dispatch_csv_and_transport_errors(tmp_path):
    active = peak = calls = 0

    async def endpoint(request):
        nonlocal active, peak, calls
        calls += 1
        call = calls
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.02)
            if call == 1:
                raise httpx.ReadTimeout("timeout", request=request)
            if call == 2:
                raise httpx.ConnectError("offline", request=request)
            return httpx.Response(500, json={"error": "simulated_failure"})
        finally:
            active -= 1

    out = tmp_path / "requests.csv"
    cfg = config(n_requests=12)
    schedule = [replace(e, minute=0) for e in sim.make_schedule(cfg)]
    asyncio.run(
        sim.run_simulation(
            cfg,
            out,
            duration_seconds=0.1,
            transport=httpx.MockTransport(endpoint),
            schedule=schedule,
        )
    )
    rows = list(csv.DictReader(out.open()))
    assert len(rows) == 12
    assert peak > 1
    assert len({r["request_id"] for r in rows}) == 12
    assert sum(r["transport_error"] == "ReadTimeout" for r in rows) == 1
    assert sum(r["transport_error"] == "ConnectError" for r in rows) == 1
    assert sum(r["status_code"] == "500" for r in rows) == 10
    assert all(float(r["client_latency_ms"]) >= 0 for r in rows)
    assert all(float(r["sent_offset_s"]) >= float(r["scheduled_offset_s"]) for r in rows)
    assert all(
        r["sent_at"] and r["scheduled_at"] and r["operation"] and r["intended_event"] for r in rows
    )


def test_empty_run_writes_header_and_existing_output_is_protected(tmp_path):
    out = tmp_path / "empty.csv"
    asyncio.run(sim.run_simulation(config(n_requests=0), out, duration_seconds=0.01))
    assert list(csv.DictReader(out.open())) == []
    with pytest.raises(FileExistsError):
        asyncio.run(sim.run_simulation(config(n_requests=0), out, duration_seconds=0.01))


@pytest.mark.parametrize("data", [None, [], "bad"])
def test_config_requires_json_object(data):
    with pytest.raises(ValueError):
        sim.Config.from_dict(data)


def test_invalid_url_port_is_rejected():
    with pytest.raises(ValueError):
        config(target_url="http://localhost:not-a-port")


def test_scenario_probability_caps_and_overlaps_do_not_compound():
    scenario = dict(start_min=1, end_min=59, effect="double_errors", category="electronics")
    cfg = config(invalid_share=0.7, scenarios=[scenario, scenario])
    assert sim.invalid_probability(cfg, 30, "electronics") == 1


def test_order_pool_never_invents_conflict_or_reuses_pending_cancellation():
    pool = sim.OrderPool()
    event = sim.Event(30, "electronics", "standard", 25, "cancel", "invalid_state", False)
    prepared = pool.prepare(event)
    assert prepared[5] == "invalid_payload"
    key = ("electronics", "standard")
    pool.active[key] = ["known-order"]
    valid = replace(event, intended_event="valid")
    cancel = pool.prepare(valid)
    assert cancel[1] == "/orders/known-order/cancel"
    assert pool.prepare(valid)[4] == "create"
    pool.observe("cancel", "valid", "known-order", key, httpx.Response(200))
    assert pool.prepare(event)[1] == "/orders/known-order/cancel"
    assert pool.prepare(event)[5] == "invalid_state"


def test_interrupted_inflight_request_has_explicit_csv_error(tmp_path):
    async def exercise():
        entered = asyncio.Event()

        async def endpoint(request):
            entered.set()
            await asyncio.sleep(100)

        out = tmp_path / "interrupted.csv"
        cfg = config(n_requests=1)
        schedule = [replace(sim.make_schedule(cfg)[0], minute=0)]
        task = asyncio.create_task(
            sim.run_simulation(
                cfg,
                out,
                duration_seconds=1,
                transport=httpx.MockTransport(endpoint),
                schedule=schedule,
            )
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with out.open() as stream:
            rows = list(csv.DictReader(stream))
        assert len(rows) == 1
        assert rows[0]["status_code"] == ""
        assert rows[0]["transport_error"] == "CancelledError"

    asyncio.run(exercise())
