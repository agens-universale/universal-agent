"""Идемпотентность ``POST /v1/tasks`` (спека сервера, 14.2).

``Idempotency-Key`` или ``webhook-id`` — ключ операции записи во входящий
поток: повтор с тем же телом — та же квитанция и тот же ``message_seq``,
второго сообщения нет; тот же ключ с другим телом — 422. Окно — срок
хранения квитанций.
"""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from gc_core.threads.facade import run_sync
from gc_core.threads.models import ThreadKey
from server_harness import LOOPBACK_URL, make_webhook

KEY = ThreadKey(system="generic", thread="t1")


@pytest.fixture()
def surface(tmp_path, monkeypatch):
    surface = make_webhook(tmp_path)
    starts: list = []

    async def fake_start(self, system, task_id, trace_id, **_kw):
        starts.append(task_id)

    monkeypatch.setattr(type(surface), "_start_run", fake_start)
    surface.starts = starts
    return surface


def _inbox(surface) -> list:
    return run_sync(surface.threads().store.read_stream(KEY, "inbox"))


@pytest.mark.parametrize("header", ["Idempotency-Key", "webhook-id"])
def test_repeat_with_the_same_key_is_one_message(surface, header):
    client = TestClient(surface.app(), base_url=LOOPBACK_URL)
    body = {"task_id": "t1", "message": "сделай"}
    first = client.post("/v1/tasks", json=body, headers={header: "k-1"})
    again = client.post("/v1/tasks", json=body, headers={header: "k-1"})
    assert first.status_code == again.status_code == 202
    assert first.json()["message_seq"] == again.json()["message_seq"] == 1
    assert len(_inbox(surface)) == 1


def test_same_key_with_another_body_is_422(surface):
    client = TestClient(surface.app(), base_url=LOOPBACK_URL)
    headers = {"Idempotency-Key": "k-2"}
    client.post(
        "/v1/tasks", json={"task_id": "t1", "message": "а"}, headers=headers
    )
    other = client.post(
        "/v1/tasks", json={"task_id": "t1", "message": "б"}, headers=headers
    )
    assert other.status_code == 422
    assert len(_inbox(surface)) == 1


def test_without_a_key_each_request_is_a_message(surface):
    client = TestClient(surface.app(), base_url=LOOPBACK_URL)
    body = {"task_id": "t1", "message": "сделай"}
    client.post("/v1/tasks", json=body)
    client.post("/v1/tasks", json=body)
    assert len(_inbox(surface)) == 2


def test_delivered_repeat_does_not_start_a_run(surface):
    """Повтор уже доставленного сообщения — без нового прогона."""
    client = TestClient(surface.app(), base_url=LOOPBACK_URL)
    body = {"task_id": "t1", "message": "сделай"}
    headers = {"Idempotency-Key": "k-3"}
    client.post("/v1/tasks", json=body, headers=headers)
    _mark_delivered(surface)
    again = client.post("/v1/tasks", json=body, headers=headers)
    assert again.json() == {
        "status": "duplicate",
        "system": "generic",
        "task_id": "t1",
        "message_seq": 1,
    }
    assert surface.starts == ["t1"]


def test_window_is_the_receipt_lifetime(surface):
    client = TestClient(surface.app(), base_url=LOOPBACK_URL)
    body = {"task_id": "t1", "message": "сделай"}
    headers = {"Idempotency-Key": "k-4"}
    client.post("/v1/tasks", json=body, headers=headers)
    run_sync(surface.aprune_receipts(older_than=timedelta(seconds=-1)))
    client.post("/v1/tasks", json=body, headers=headers)
    assert len(_inbox(surface)) == 2


def _mark_delivered(surface) -> None:
    """Ход забрал сообщение: отметка доставки — его запись."""
    from gc_core.threads.session_writer import SessionWriter

    async def mark() -> None:
        writer = SessionWriter(surface.threads().store, KEY, owner="runner")
        record = await writer.aclaim()
        delivered = record.state.model_copy(update={"inbox_seq": 1})
        await writer.acommit(set_state=delivered)
        await writer.arelease()

    run_sync(mark())
