"""Эндпоинты журнала тредов ``/logs/...`` читают треды через фасад
``Threads``: старые треды develop отдаются как раньше, треды других
поверхностей и интерактивных сессий — нет, а запрос по чужому id ничего не
создаёт на диске."""

import pytest
from fastapi.testclient import TestClient
from gc_server.config import RESERVED_SYSTEMS
from gc_testkit.legacy_threads import LEGACY_THREADS, install_legacy_threads


@pytest.fixture()
def threads_dir(tmp_path):
    return install_legacy_threads(tmp_path)


@pytest.fixture()
def client(threads_dir):
    from server_harness import make_webhook

    surface = make_webhook(**{"paths.threads_data_path": str(threads_dir)})
    return TestClient(surface.app())


def _files(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


@pytest.mark.parametrize("name", ["sdk_image"])
def test_thread_log_of_develop_thread(client, name):
    fixture = LEGACY_THREADS[name]
    response = client.get(f"/logs/{fixture.system}/{fixture.thread_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["task_id"] == fixture.thread_id
    assert body["meta"]["thread_id"] == fixture.thread_id
    assert len(body["messages"]) == fixture.messages


@pytest.mark.parametrize(
    "name", ["cli_compacted", "chat_dialog", "tui_session"]
)
def test_foreign_develop_thread_is_not_served(client, name):
    fixture = LEGACY_THREADS[name]
    response = client.get(f"/logs/{fixture.system}/{fixture.thread_id}")
    assert response.status_code == 400


def test_unknown_thread_is_404_and_writes_nothing(client, threads_dir):
    before = _files(threads_dir)
    response = client.get("/logs/generic/no-such-thread")
    assert response.status_code == 404
    assert _files(threads_dir) == before


def test_all_logs_list_every_thread_newest_first(client):
    response = client.get("/logs/all")
    assert response.status_code == 200
    logs = response.json()["logs"]
    found = {(entry["system"], entry["task_id"]) for entry in logs}
    assert found == {
        (fixture.system, fixture.thread_id)
        for fixture in LEGACY_THREADS.values()
        if fixture.system not in RESERVED_SYSTEMS
    }
    stamps = [str(entry["updated_at"] or "") for entry in logs]
    assert stamps == sorted(stamps, reverse=True)
    entry = logs[0]
    assert entry["logs_url"] == f"/logs/{entry['system']}/{entry['task_id']}"
