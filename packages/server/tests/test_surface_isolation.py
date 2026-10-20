"""Безопасность поверхностей: старая секция с токеном не теряется молча,
вебхук не видит треды других поверхностей и интерактивных сессий."""

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
from server_harness import make_webhook


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
    return TestClient(make_webhook(tmp_path).app())


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
