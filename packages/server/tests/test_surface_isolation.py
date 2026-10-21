"""Безопасность поверхностей: старая секция с токеном не теряется молча,
вебхук не видит треды других поверхностей и интерактивных сессий."""

import json
import pytest
from fastapi.testclient import TestClient
from gc_core.config import load_settings
from gc_core.threads.facade import Threads, run_sync
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import ThreadKey
from gc_server.a2a.surface import A2aSurface
from gc_server.app import ServerRuntime
from gc_server.chat.surface import ChatSurface
from gc_server.config import ServerConfig, ServerConfigError
from server_harness import LOOPBACK_URL, make_webhook


@pytest.mark.parametrize(
    ("section", "surface"),
    [("chat_api", ChatSurface), ("a2a", A2aSurface)],
)
def test_moved_section_in_config_yml_refuses_the_surface(
    tmp_path, section, surface
):
    """Обновившийся сервер с токеном в config.yml поднял бы поверхность без
    авторизации: секция больше не читается. Отказ называет новое место."""
    path = tmp_path / "config.yml"
    path.write_text(f"{section}:\n  auth_token: secret\n", encoding="utf-8")
    runtime = ServerRuntime(
        base=load_settings(str(path)), config=ServerConfig()
    )
    with pytest.raises(ServerConfigError) as err:
        surface(runtime)
    text = str(err.value)
    assert "config_server.yml" in text and section in text
    assert "auth_token" in text


def _seed(threads_dir, system: str, thread: str) -> None:
    run_sync(
        Threads(FileThreadStore(str(threads_dir))).append_messages(
            ThreadKey(system=system, thread=thread),
            [{"role": "user", "content": "секрет", "id": f"m-{system}"}],
        )
    )


@pytest.fixture
def client(tmp_path):
    threads = tmp_path / "threads"
    for system in (
        "generic",
        "responses",
        "a2a",
        "completions",
        "acp",
        "cli",
        "tui",
    ):
        _seed(threads, system, "t1")
    return TestClient(make_webhook(tmp_path).app(), base_url=LOOPBACK_URL)


@pytest.mark.parametrize(
    "system", ["responses", "a2a", "completions", "acp", "cli", "tui"]
)
def test_webhook_does_not_read_foreign_threads(client, system):
    assert client.get(f"/v1/tasks/{system}/t1/messages").status_code == 400
    assert client.get(f"/v1/tasks/{system}/t1").status_code == 400


def test_webhook_lists_only_its_own_threads(client):
    systems = {
        entry["system"] for entry in client.get("/v1/tasks").json()["tasks"]
    }
    assert systems == {"generic"}
    assert client.get("/v1/tasks/generic/t1/messages").status_code == 200


def test_webhook_does_not_write_into_interactive_sessions(client):
    resp = client.post("/v1/tasks", json={"system": "cli", "task_id": "t"})
    assert resp.status_code == 400


@pytest.mark.parametrize(
    "system", ["cli!", "CLI.", ".cli", "cli_", "Cli​", "a2a!", "responses."]
)
def test_reserved_check_sees_the_normalized_system(client, system):
    """Хранилище нормализует system (``sanitize_system_id``): «cli!» и
    «.cli» — это тред «cli». Проверка обязана видеть то же, что хранилище."""
    from urllib.parse import quote

    assert (
        client.get(f"/v1/tasks/{quote(system)}/t1/messages").status_code == 400
    )
    assert client.get(f"/v1/tasks/{quote(system)}/t1").status_code == 400
    resp = client.post("/v1/tasks", json={"system": system, "task_id": "x"})
    assert resp.status_code == 400


def test_webhook_does_not_touch_swarm_task_threads(tmp_path):
    """Тред задачи swarm в общей базе (помечен ``swarm``) вебхуку чужой: не
    читается, не отменяется и не получает сообщений — иначе вебхук
    вмешивался бы в песочницу."""
    store = FileThreadStore(str(tmp_path / "threads"))
    key = ThreadKey(system="generic", thread="sw")
    threads = Threads(store)
    run_sync(
        threads.append_messages(
            key, [{"role": "user", "content": "песочница", "id": "m1"}]
        )
    )
    run_sync(threads.update_frontend(key, "swarm", {"swarm_id": "swarm_x"}))
    client = TestClient(make_webhook(tmp_path).app(), base_url=LOOPBACK_URL)
    assert client.get("/v1/tasks/generic/sw/messages").status_code == 404
    assert client.get("/v1/tasks/generic/sw").status_code == 404
    assert (
        client.post("/v1/tasks/generic/sw/cancel", json={}).status_code == 404
    )
    resp = client.post("/v1/tasks", json={"task_id": "sw", "message": "x"})
    assert resp.status_code == 409
    assert run_sync(store.read_stream(key, "inbox")) == []
    assert run_sync(store.read_stream(key, "control")) == []


@pytest.mark.parametrize("surface_kind", ["webhook", "swarm"])
def test_browser_cannot_start_a_task(tmp_path, surface_kind):
    """POST без ``Content-Type: application/json`` браузер шлёт без
    preflight — такой запрос (CSRF со страницы в браузере пользователя) не
    принимается; на loopback чужой Host (DNS rebinding) — тоже."""
    from server_harness import make_swarm

    if surface_kind == "webhook":
        surface = make_webhook(tmp_path)
    else:
        surface = make_swarm(
            {"threads_dir": str(tmp_path), "sandbox_root_dir": str(tmp_path)},
            tmp_path,
        )
    client = TestClient(surface.app(), base_url="http://127.0.0.1:8000")
    resp = client.post(
        "/v1/tasks",
        content=b'{"task_id": "t", "message": "x"}',
        headers={"content-type": "text/plain"},
    )
    assert resp.status_code == 415
    rebound = client.post(
        "/v1/tasks",
        json={"task_id": "t", "message": "x"},
        headers={"host": "evil.example"},
    )
    assert rebound.status_code == 400


def test_cancel_without_body_needs_authorization(tmp_path):
    """``POST …/cancel`` без тела: клиенту (curl, SDK) не нужно слать
    пустой JSON. Пропускается с ``Authorization`` — браузер не пришлёт его
    со сторонней страницы без preflight; без него — 415, как раньше."""
    surface = make_webhook(tmp_path, config={"server": {"auth_token": "t0k"}})
    client = TestClient(surface.app(), base_url="http://127.0.0.1:8000")
    path = "/v1/tasks/cli/t1/cancel"
    authorized = client.post(path, headers={"authorization": "Bearer t0k"})
    assert authorized.status_code != 415
    assert client.post(path).status_code == 415
    typed = client.post(
        path,
        content=b"x",
        headers={"authorization": "Bearer t0k", "content-type": "text/plain"},
    )
    assert typed.status_code == 415


@pytest.mark.parametrize("chunked", [False, True])
def test_request_body_over_the_limit_is_413(tmp_path, chunked):
    """Тело больше ``server.max_body_bytes`` — 413 до разбора: и с
    ``Content-Length``, и потоком без длины."""
    surface = make_webhook(tmp_path, config={"server": {"max_body_bytes": 64}})
    client = TestClient(surface.app(), base_url="http://127.0.0.1:8000")
    body = json.dumps({"task_id": "t", "message": "x" * 200}).encode()
    content = iter([body[:50], body[50:]]) if chunked else body
    resp = client.post(
        "/v1/tasks",
        content=content,
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 413
    small = client.post("/v1/tasks/cli/t1/cancel", json={})
    assert small.status_code != 413


def test_body_limit_defaults_to_ten_mib():
    from gc_server.config import ServerConfig

    config = ServerConfig.model_validate({})
    assert config.surface("webhook").max_body_bytes == 10 * 1024 * 1024
    own = ServerConfig.model_validate(
        {"server": {"max_body_bytes": 100}, "chat": {"max_body_bytes": 5}}
    )
    assert own.surface("webhook").max_body_bytes == 100
    assert own.surface("chat").max_body_bytes == 5
