"""Подпись тела запроса по Standard Webhooks (спека сервера, 14.3).

Эталонный вектор сверен с официальной библиотекой ``standardwebhooks``
(отдельное окружение, ``Webhook(secret).sign(...)``).
"""

import base64

import pytest
from fastapi.testclient import TestClient
from gc_server.config import ServerConfigError, WebhookConfig
from gc_server.webhook.signatures import SignatureError, sign, verify
from server_harness import make_webhook

SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
MSG_ID = "msg_p5jXN8AQM9LWM0D4loKWxJek"
TS = 1614265330
BODY = b'{"test": 2432232314}'
REFERENCE = "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="
OTHER = (
    "whsec_" + base64.b64encode(b"another secret key 32 bytes!!!!").decode()
)


def _headers(signature, *, ts=TS, msg_id=MSG_ID):
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": str(ts),
        "webhook-signature": signature,
    }


def test_reference_vector():
    assert sign(SECRET, MSG_ID, TS, BODY) == REFERENCE


def test_valid_signature_passes():
    verify([SECRET], _headers(REFERENCE), BODY, now=TS, tolerance=300)


@pytest.mark.parametrize(
    ("headers", "body", "now"),
    [
        (_headers("v1,AAAA"), BODY, TS),
        (_headers(REFERENCE), b'{"test": 1}', TS),
        (_headers(REFERENCE), BODY, TS + 301),
        (_headers(REFERENCE), BODY, TS - 301),
        ({"webhook-id": MSG_ID}, BODY, TS),
        (_headers(REFERENCE, ts="вчера"), BODY, TS),
    ],
    ids=[
        "чужая",
        "другое тело",
        "старая",
        "из будущего",
        "без заголовков",
        "мусор",
    ],
)
def test_bad_signature_is_refused(headers, body, now):
    with pytest.raises(SignatureError):
        verify([SECRET], headers, body, now=now, tolerance=300)


def test_secret_rotation_accepts_any_listed_secret():
    old = sign(OTHER, MSG_ID, TS, BODY)
    verify([SECRET, OTHER], _headers(old), BODY, now=TS, tolerance=300)


def test_several_signatures_in_the_header():
    header = f"v1,AAAA {REFERENCE}"
    verify([SECRET], _headers(header), BODY, now=TS, tolerance=300)


def test_secret_must_be_base64():
    with pytest.raises(ValueError):
        WebhookConfig(signing_secret="whsec_не-base64!")


def _signed(client_body: bytes, ts: int):
    return {
        **_headers(sign(SECRET, MSG_ID, ts, client_body), ts=ts),
        "content-type": "application/json",
    }


def test_surface_requires_a_signature_when_a_secret_is_set(
    tmp_path, monkeypatch
):
    import time

    surface = make_webhook(
        tmp_path, config={"webhook": {"signing_secret": SECRET}}
    )

    async def fake_start(self, system, task_id, trace_id):
        return None

    monkeypatch.setattr(type(surface), "_start_run", fake_start)
    client = TestClient(surface.app())
    body = b'{"task_id": "t1", "message": "x"}'
    unsigned = client.post(
        "/v1/tasks", content=body, headers={"content-type": "application/json"}
    )
    assert unsigned.status_code == 401
    now = int(time.time())
    signed = client.post("/v1/tasks", content=body, headers=_signed(body, now))
    assert signed.status_code == 202
    stale = client.post(
        "/v1/tasks", content=body, headers=_signed(body, now - 3600)
    )
    assert stale.status_code == 401


def test_without_a_secret_nothing_is_checked(tmp_path, monkeypatch):
    surface = make_webhook(tmp_path)

    async def fake_start(self, system, task_id, trace_id):
        return None

    monkeypatch.setattr(type(surface), "_start_run", fake_start)
    resp = TestClient(surface.app()).post(
        "/v1/tasks", json={"task_id": "t1", "message": "x"}
    )
    assert resp.status_code == 202


def test_bad_secret_in_config_server_is_a_start_error(tmp_path):
    from gc_server.config import load_server_config

    path = tmp_path / "config_server.yml"
    path.write_text("webhook:\n  signing_secret: 'whsec_%%%'\n")
    with pytest.raises(ServerConfigError):
        load_server_config(str(path))


def _signed_surface(tmp_path, monkeypatch):
    surface = make_webhook(
        tmp_path, config={"webhook": {"signing_secret": SECRET}}
    )

    async def fake_start(self, system, task_id, trace_id):
        return None

    monkeypatch.setattr(type(surface), "_start_run", fake_start)
    return surface


def test_signed_replay_with_another_idempotency_key_is_one_message(
    tmp_path, monkeypatch
):
    """Подписан только webhook-id: при включённой подписи ключ
    идемпотентности — он, а не неподписанный Idempotency-Key. Иначе
    перехваченный запрос повторялся бы в окне со свежим ключом."""
    import time

    from gc_core.threads.facade import run_sync
    from gc_core.threads.models import ThreadKey

    surface = _signed_surface(tmp_path, monkeypatch)
    client = TestClient(surface.app())
    body = b'{"task_id": "t1", "message": "x"}'
    headers = _signed(body, int(time.time()))
    client.post("/v1/tasks", content=body, headers=headers)
    client.post(
        "/v1/tasks",
        content=body,
        headers={**headers, "Idempotency-Key": "fresh"},
    )
    entries = run_sync(
        surface.threads().store.read_stream(
            ThreadKey(system="generic", thread="t1"), "inbox"
        )
    )
    assert len(entries) == 1


def test_duplicate_json_keys_are_refused(tmp_path, monkeypatch):
    """Повтор ключа в JSON разные парсеры читают по-разному: подписали одно,
    исполнили бы другое. Такое тело — 400."""
    import time

    surface = _signed_surface(tmp_path, monkeypatch)
    body = b'{"task_id": "t1", "message": "a", "message": "b"}'
    resp = TestClient(surface.app()).post(
        "/v1/tasks", content=body, headers=_signed(body, int(time.time()))
    )
    assert resp.status_code == 400
