"""Документация API поверхностей (Swagger) и выключение ручек.

``<поверхность>.docs`` — выключатель ``/docs``, ``/redoc`` и
``/openapi.json`` (по умолчанию выключены); включённые открываются без
токена, вызовы из Swagger — с токеном (схема Bearer). Описание — по каталогу
ручек: заголовок поверхности, группы, сводки, тела запросов, параметры и
ответы. ``<поверхность>.disabled_routes`` — группы ручек или пути
(``"/v1/dialogs*"``, ``"DELETE /v1/dialogs/{thread_id}/artifacts"``): такая
ручка не обслуживается и в документации её нет.
"""

import sys
from importlib.metadata import version

import pytest
from fastapi.testclient import TestClient
from gc_server.api_catalog import SURFACES
from gc_server.app import ServerRuntime, build_surface
from gc_server.config import ServerConfig, ServerConfigError
from gc_testkit.settings import make_settings
from server_harness import LOOPBACK_URL

_DOCUMENTED = ("webhook", "chat") + (
    () if sys.platform == "win32" else ("swarm",)
)
_DOC_PATHS = ("/docs", "/redoc", "/openapi.json")


def _client(tmp_path, name, **section):
    runtime = ServerRuntime(
        base=make_settings(
            {
                "agent.model": "m",
                "paths.threads_data_path": str(tmp_path / "threads"),
            }
        ),
        config=ServerConfig.model_validate({name: section}),
    )
    app = build_surface(runtime, name).app()
    return TestClient(app, base_url=LOOPBACK_URL)


@pytest.mark.parametrize("name", _DOCUMENTED)
def test_docs_are_off_by_default(tmp_path, name):
    client = _client(tmp_path, name)
    for path in _DOC_PATHS:
        assert client.get(path).status_code == 404, path


@pytest.mark.parametrize("name", _DOCUMENTED)
def test_every_route_is_described_and_the_catalog_has_no_stale_entries(
    tmp_path, name
):
    """Каталог и приложение совпадают: у каждой ручки — сводка и группа из
    каталога, в каталоге нет ручек, которых нет в приложении."""
    schema = _client(tmp_path, name, docs=True).get("/openapi.json").json()
    catalog = SURFACES[name]
    served = {
        (method.upper(), path)
        for path, item in schema["paths"].items()
        for method in item
    }
    assert served == set(catalog.routes)
    tags = {tag["name"] for tag in schema["tags"]}
    for path, item in schema["paths"].items():
        for method, operation in item.items():
            doc = catalog.routes[(method.upper(), path)]
            assert operation["summary"] == doc.summary
            assert operation["tags"] == [catalog.groups[doc.group].tag]
            assert operation["tags"][0] in tags


def test_webhook_docs_show_the_task_body_parameters_and_answers(tmp_path):
    schema = _client(tmp_path, "webhook", docs=True).get("/openapi.json")
    schema = schema.json()
    assert schema["info"]["title"] == "GigaCode · Вебхук"
    assert schema["info"]["version"] == version("gc-server")
    assert "Bearer" in schema["info"]["description"]
    create = schema["paths"]["/v1/tasks"]["post"]
    body = create["requestBody"]["content"]["application/json"]
    fields = body["schema"]["properties"]
    assert {"message", "task_id", "system", "metadata"} <= set(fields)
    assert body["examples"], "у тела задачи нет примера"
    assert "202" in create["responses"]
    assert "200" not in create["responses"], "ответ по умолчанию FastAPI"
    headers = {p["name"] for p in create["parameters"] if p["in"] == "header"}
    assert "Idempotency-Key" in headers
    listing = schema["paths"]["/v1/tasks"]["get"]
    query = {p["name"] for p in listing["parameters"] if p["in"] == "query"}
    assert query == {"system", "limit", "cursor"}


def test_chat_docs_show_the_response_body(tmp_path):
    schema = _client(tmp_path, "chat", docs=True).get("/openapi.json").json()
    assert schema["info"]["title"] == "GigaCode · Chat"
    create = schema["paths"]["/v1/responses"]["post"]
    fields = create["requestBody"]["content"]["application/json"]["schema"]
    assert {"model", "input", "stream", "previous_response_id"} <= set(
        fields["properties"]
    )
    completions = schema["paths"]["/v1/chat/completions"]["post"]
    assert (
        "messages"
        in (
            completions["requestBody"]["content"]["application/json"][
                "schema"
            ]["properties"]
        )
    )


def test_docs_open_without_the_token_but_the_api_needs_it(tmp_path):
    client = _client(tmp_path, "webhook", docs=True, auth_token="s3cret")
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json")
    assert schema.status_code == 200
    assert schema.json()["components"]["securitySchemes"]["bearer"] == {
        "type": "http",
        "scheme": "bearer",
    }
    assert client.get("/v1/tasks").status_code == 403
    authorized = client.get(
        "/v1/tasks", headers={"Authorization": "Bearer s3cret"}
    )
    assert authorized.status_code == 200


def test_disabled_routes_are_neither_served_nor_documented(tmp_path):
    client = _client(
        tmp_path,
        "chat",
        docs=True,
        disabled_routes=["files", "/", "GET /v1/dialogs/{thread_id}"],
    )
    assert client.get("/").status_code == 404
    assert client.get("/v1/workspace/artifacts").status_code == 404
    assert client.get("/v1/dialogs/t1").status_code == 404
    assert client.get("/v1/models").status_code == 200
    assert client.get("/v1/dialogs").status_code == 200
    paths = client.get("/openapi.json").json()["paths"]
    assert "/" not in paths
    assert not any("artifacts" in path for path in paths)
    assert "/v1/dialogs/{thread_id}" not in paths
    assert "/v1/models" in paths and "/v1/dialogs" in paths


def test_disabled_group_of_the_webhook_is_not_served(tmp_path):
    client = _client(tmp_path, "webhook", disabled_routes=["history"])
    assert client.get("/v1/tasks/generic/t/messages").status_code == 404
    assert client.get("/healthcheck").status_code == 200


@pytest.mark.parametrize(
    ("entry", "hint"),
    [
        ("nope", "files"),
        ("/v1/nothing*", "/v1/nothing*"),
        ("PATCH /v1/models", "PATCH /v1/models"),
    ],
)
def test_unknown_disabled_entry_is_a_config_error(entry, hint):
    """Опечатка в имени группы или пути — отказ старта с подсказкой, а не
    молча обслуживаемая ручка."""
    with pytest.raises((ServerConfigError, ValueError)) as err:
        ServerConfig.model_validate({"chat": {"disabled_routes": [entry]}})
    assert hint in str(err.value)


def test_a2a_has_no_docs_switch():
    """У A2A Swagger нет — описание протокола — карточка агента."""
    with pytest.raises((ServerConfigError, ValueError)):
        ServerConfig.model_validate({"a2a": {"docs": True}})
