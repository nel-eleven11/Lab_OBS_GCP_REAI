"""Real Functions Framework process + simulator CLI, with no cloud dependencies."""

import csv
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def test_real_framework_and_simulator(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    log_file = tmp_path / "backend.jsonl"
    env = {
        **os.environ,
        "FAILURE_RATE": "0",
        "EXTRA_LATENCY_MS": "30",
        "WORKERS": "1",
        "THREADS": "8",
    }
    with log_file.open("w") as stdout, (tmp_path / "server.stderr").open("w") as stderr:
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "functions_framework",
                "--source",
                str(ROOT / "lab/backend/main.py"),
                "--target",
                "handle",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            env=env,
            stdout=stdout,
            stderr=stderr,
            cwd=ROOT,
        )
        try:
            with httpx.Client(trust_env=False) as client:
                for _ in range(100):
                    if server.poll() is not None:
                        raise AssertionError((tmp_path / "server.stderr").read_text())
                    try:
                        client.get(url + "/ready", timeout=0.2)
                        break
                    except httpx.TransportError:
                        time.sleep(0.05)
                else:
                    raise AssertionError("Local server did not become ready")
            config = json.loads((ROOT / "lab/simulator/config.json").read_text())
            config["invalid_share"] = 0.35
            cfg = tmp_path / "config.json"
            cfg.write_text(json.dumps(config))
            csv_path = tmp_path / "requests.csv"
            before = (ROOT / "lab/simulator/config.json").read_bytes()
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "lab/simulator/simulator.py"),
                    "--config",
                    str(cfg),
                    "--target-url",
                    url,
                    "--n-requests",
                    "180",
                    "--duration-seconds",
                    "3",
                    "--output",
                    str(csv_path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=ROOT,
            )
            assert result.returncode == 0, result.stderr
            assert before == (ROOT / "lab/simulator/config.json").read_bytes()
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
    with csv_path.open() as stream:
        rows = list(csv.DictReader(stream))
    logs = [json.loads(line) for line in log_file.read_text().splitlines() if line.startswith("{")]
    by_id = {log["request_id"]: log for log in logs}
    assert len(rows) == 180
    assert len({row["request_id"] for row in rows}) == 180
    assert {row["status_code"] for row in rows} == {"200", "201", "400", "404", "409"}
    assert {row["operation"] for row in rows} == {"create", "get", "cancel"}
    for row in rows:
        assert not row["transport_error"]
        log = by_id[row["request_id"]]
        assert int(row["status_code"]) == log["status_code"]
        assert row["category"] == log["category"]
        assert row["customer_tier"] == log["customer_tier"]
        if row["intended_event"] == "valid":
            assert row["status_code"] in {"200", "201"}
    assert len(logs) == len(rows) + 1  # readiness probe
    # At least one later request starts while a previous one is still pending.
    ordered = sorted(rows, key=lambda row: float(row["sent_offset_s"]))
    assert any(
        float(b["sent_offset_s"]) < float(a["sent_offset_s"]) + float(a["client_latency_ms"]) / 1000
        for a, b in zip(ordered, ordered[1:])
    )
