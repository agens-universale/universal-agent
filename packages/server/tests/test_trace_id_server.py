import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch, tmp_path):
    from gc_server import server

    captured = {}

    async def _fake_create_run_context(loaded, **kwargs):
        # Сервер отдаёт контексту LoadedSettings; его рантайм — то, что увидит ран.
        captured["loaded"] = loaded
        return object()

    async def _fake_start_if_absent(**kwargs):
        captured["start_kwargs"] = kwargs
        return True

    def _fake_add_user_message(**kwargs):
        captured.setdefault("add_calls", []).append(kwargs)
        return captured.get("live_run", False)

    # Эндпоинт собирает контекст асинхронно: сборка включает MCP-дискавери
    # и синхронный вызов морозил бы loop uvicorn на всё это время.
    monkeypatch.setattr(
        server, "acreate_run_context", _fake_create_run_context
    )
    monkeypatch.setattr(server, "build_auth_connector", lambda settings: None)
    monkeypatch.setattr(
        server.agent_run_registry, "astart_if_absent", _fake_start_if_absent
    )
    monkeypatch.setattr(
        server.agent_run_registry, "add_user_message", _fake_add_user_message
    )
    return TestClient(server.app), captured


def test_trace_id_reaches_run_config(client):
    api, captured = client
    resp = api.post(
        "/events/webhook", json={"task_id": "t1", "trace_id": "wf-42"}
    )
    assert resp.status_code == 200
    assert captured["loaded"].runtime.trace_id == "wf-42"


def test_trace_id_is_removed_from_the_prompt(client):
    api, captured = client
    api.post(
        "/events/webhook", json={"task_id": "t1", "trace_id": "wf-42", "x": 1}
    )
    message = captured["start_kwargs"]["initial_user_message"]
    assert "wf-42" not in message
    assert "trace_id" not in json.loads(message)


def test_absent_trace_id_leaves_no_key(client):
    api, captured = client
    api.post("/events/webhook", json={"task_id": "t1"})
    assert captured["loaded"].runtime.trace_id is None


def test_invalid_trace_id_is_rejected(client):
    api, _ = client
    resp = api.post(
        "/events/webhook", json={"task_id": "t1", "trace_id": "wf\n42"}
    )
    assert resp.status_code == 400


def test_second_webhook_into_live_run_drops_trace_and_logs_it(client, caplog):
    api, captured = client
    captured["live_run"] = True
    with caplog.at_level("WARNING"):
        resp = api.post(
            "/events/webhook", json={"task_id": "t1", "trace_id": "wf-99"}
        )
    assert resp.json()["status"] == "queued"
    assert "trace_id_ignored" in caplog.text
    assert "wf-99" in caplog.text
    assert "t1" in caplog.text
