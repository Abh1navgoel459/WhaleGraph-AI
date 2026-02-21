import json
import os
import importlib

from fastapi.testclient import TestClient


def load_app():
    # Ensure deterministic behavior for tests
    os.environ.setdefault("USE_LLM", "false")
    os.environ.setdefault("USE_DATADOG", "false")
    import app.main as main
    importlib.reload(main)
    return main.app


def test_health():
    app = load_app()
    client = TestClient(app)
    res = client.get("/api/health")
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True


def test_integrations_shape():
    app = load_app()
    client = TestClient(app)
    res = client.get("/api/integrations")
    assert res.status_code == 200
    data = res.json()
    for key in ["neo4j", "datadog", "copilotkit", "bedrock", "model_id"]:
        assert key in data


def test_replay_basic():
    app = load_app()
    client = TestClient(app)
    res = client.post(
        "/api/replay",
        json={"token": "SOL", "scenario": "pump_and_dump", "batch_size": 10, "dataset": "test"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["token"] == "SOL"
    assert "risk" in data
    assert "market" in data


def test_stream_emits_event():
    app = load_app()
    client = TestClient(app)
    with client.stream(
        "GET",
        "/api/stream?token=SOL&scenario=pump_and_dump&speed_ms=1&dataset=test",
    ) as resp:
        assert resp.status_code == 200
        # read first SSE event
        lines = []
        for line in resp.iter_lines():
            if not line:
                if lines:
                    break
                continue
            lines.append(line)
        payload = "\n".join(lines)
        if payload.startswith("data:"):
            payload = payload.replace("data:", "", 1).strip()
        event = json.loads(payload)
        assert "trade" in event
        assert "price" in event
        assert "top_tokens" in event
