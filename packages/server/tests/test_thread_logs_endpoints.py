"""Ручки задач ``/v1/tasks…`` читают треды через фасад ``Threads``. Треды
develop — сессии других поверхностей, интерактивные и SDK: задачей вебхука
(его статусом в треде) ни один из них не помечен, и вебхук их не отдаёт; запрос
по чужому id ничего не создаёт на диске."""

import pytest
from fastapi.testclient import TestClient
from gc_testkit.legacy_threads import LEGACY_THREADS, install_legacy_threads
from server_harness import LOOPBACK_URL


@pytest.fixture()
def threads_dir(tmp_path):
    return install_legacy_threads(tmp_path)


@pytest.fixture()
def client(threads_dir):
    from server_harness import make_webhook

    surface = make_webhook(**{"paths.threads_data_path": str(threads_dir)})
    return TestClient(surface.app(), base_url=LOOPBACK_URL)


def _files(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


@pytest.mark.parametrize("name", sorted(LEGACY_THREADS))
def test_develop_thread_is_not_served(client, name):
    fixture = LEGACY_THREADS[name]
    response = client.get(
        f"/v1/tasks/{fixture.system}/{fixture.thread_id}/messages"
    )
    assert response.status_code == 400


def test_unknown_thread_is_404_and_writes_nothing(client, threads_dir):
    before = _files(threads_dir)
    for path in ("", "/messages"):
        response = client.get(f"/v1/tasks/generic/no-such-thread{path}")
        assert response.status_code == 404
    assert _files(threads_dir) == before


def test_develop_threads_are_not_listed(client):
    response = client.get("/v1/tasks", params={"limit": 1000})
    assert response.status_code == 200
    assert response.json()["tasks"] == []
