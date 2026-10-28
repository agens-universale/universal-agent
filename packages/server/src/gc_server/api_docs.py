"""Документация и выключенные ручки поверхности (``api_catalog``).

``<поверхность>.docs`` — ``/docs``, ``/redoc``, ``/openapi.json`` (по
умолчанию их нет); схема OpenAPI собирается из ручек приложения и
описывается каталогом: заголовок поверхности, группы, сводки, тела запросов,
параметры и ответы. При заданном токене — схема Bearer (кнопка Authorize).
``<поверхность>.disabled_routes`` — ручки снимаются с приложения: не
обслуживаются и в документацию не попадают.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute

from gc_server.api_catalog import (
    SURFACES,
    RouteDoc,
    SurfaceDoc,
    split_route_entry,
)

#: Пути документации FastAPI: без ``docs`` их нет, с ним — без токена.
DOC_PATHS = ("/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json")


def docs_paths(section: Any) -> tuple[str, ...]:
    """Пути, открытые без токена: документация, если она включена."""
    return DOC_PATHS if getattr(section, "docs", False) else ()


def apply_api_docs(app: FastAPI, surface: str, section: Any) -> None:
    """Снять выключенные ручки и включить (или убрать) документацию."""
    doc = SURFACES[surface]
    app.state.gc_surface = surface
    _drop_disabled(app, doc, list(getattr(section, "disabled_routes", [])))
    if getattr(section, "docs", False):
        _install_openapi(app, doc, secured=bool(section.auth_token))
    else:
        app.router.routes[:] = [
            route
            for route in app.router.routes
            if getattr(route, "path", None) not in DOC_PATHS
        ]


def _drop_disabled(app: FastAPI, doc: SurfaceDoc, entries: list[str]) -> None:
    if not entries:
        return
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if not (isinstance(route, APIRoute) and _disabled(route, doc, entries))
    ]


def _disabled(route: APIRoute, doc: SurfaceDoc, entries: list[str]) -> bool:
    for method in route.methods:
        described = doc.routes.get((method, route.path))
        for entry in entries:
            if described is not None and entry == described.group:
                return True
            wanted, pattern = split_route_entry(entry)
            if pattern is not None and (
                wanted in (None, method) and fnmatchcase(route.path, pattern)
            ):
                return True
    return False


def _install_openapi(app: FastAPI, doc: SurfaceDoc, *, secured: bool) -> None:
    app.title = doc.title
    app.version = _version()
    app.description = doc.description

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = _schema(app, doc, secured=secured)
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]


def _schema(app: FastAPI, doc: SurfaceDoc, *, secured: bool) -> dict:
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        tags=[
            {"name": group.tag, "description": group.description}
            for group in doc.groups.values()
        ],
    )
    for path, item in schema.get("paths", {}).items():
        for method, operation in item.items():
            described = doc.routes.get((method.upper(), path))
            if described is not None:
                _describe(operation, described, doc)
    if secured:
        components = schema.setdefault("components", {})
        components.setdefault("securitySchemes", {})["bearer"] = {
            "type": "http",
            "scheme": "bearer",
        }
        schema["security"] = [{"bearer": []}]
    return schema


def _describe(operation: dict, described: RouteDoc, doc: SurfaceDoc) -> None:
    """Операция схемы — по каталогу. Ручки разбирают тело и параметры сами,
    поэтому всё, что FastAPI вывел из сигнатуры (``request``, ответ 200
    «string»), заменяется описанием."""
    operation["summary"] = described.summary
    operation["tags"] = [doc.groups[described.group].tag]
    if described.description:
        operation["description"] = described.description
    operation.pop("parameters", None)
    operation.pop("requestBody", None)
    if described.parameters:
        operation["parameters"] = list(described.parameters)
    if described.body is not None:
        operation["requestBody"] = described.body
    if described.responses:
        operation["responses"] = dict(described.responses)


def combined_docs(apps: list[FastAPI]) -> FastAPI | None:
    """Общий Swagger поверхностей одного адреса (у каждой свои ``/docs`` —
    диспетчер отдал бы только первую): ручки всех поверхностей с
    ``docs``, группы — с именем поверхности, у каждой своя схема Bearer
    (токены поверхностей адреса бывают разными). Нет ни одной — ``None``."""
    documented = [app for app in apps if _has_docs(app)]
    if not documented:
        return None
    names = [_short(app) for app in documented]
    docs = FastAPI(
        title="GigaCode · gc-serve: " + ", ".join(names),
        version=_version(),
        description=_combined_description(documented),
    )

    def openapi() -> dict[str, Any]:
        if docs.openapi_schema is None:
            docs.openapi_schema = _merged(docs, documented)
        return docs.openapi_schema

    docs.openapi = openapi  # type: ignore[method-assign]
    return docs


def _has_docs(app: FastAPI) -> bool:
    return any(
        getattr(route, "path", None) == "/openapi.json" for route in app.routes
    )


def _short(app: FastAPI) -> str:
    return app.title.removeprefix("GigaCode · ")


def _combined_description(apps: list[FastAPI]) -> str:
    lines = ["Несколько поверхностей на одном адресе:", ""]
    for app in apps:
        first = (app.description or "").split("\n\n", 1)[0]
        lines.append(f"- **{_short(app)}** — {first}")
    lines += [
        "",
        "Группы ручек подписаны именем поверхности; токен — у каждой свой "
        "(Authorize).",
    ]
    return "\n".join(lines)


def _merged(docs: FastAPI, apps: list[FastAPI]) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {
            "title": docs.title,
            "version": docs.version,
            "description": docs.description,
        },
        "paths": {},
        "tags": [],
    }
    components: dict[str, Any] = {}
    for app in apps:
        _merge_one(schema, components, app)
    if components:
        schema["components"] = components
    return schema


def _merge_one(schema: dict, components: dict, app: FastAPI) -> None:
    """Схема поверхности — в общую: первая поверхность адреса выигрывает
    совпадающий путь (как и диспетчер)."""
    one = app.openapi()
    short = _short(app)
    security = None
    if one.get("security"):
        name = f"bearer_{getattr(app.state, 'gc_surface', short.lower())}"
        components.setdefault("securitySchemes", {})[name] = {
            "type": "http",
            "scheme": "bearer",
            "description": f"Токен поверхности {short}",
        }
        security = [{name: []}]
    for tag in one.get("tags", []):
        schema["tags"].append({**tag, "name": f"{short} · {tag['name']}"})
    for path, item in one.get("paths", {}).items():
        target = schema["paths"].setdefault(path, {})
        for method, operation in item.items():
            if method in target:
                continue
            merged = {
                **operation,
                "tags": [
                    f"{short} · {tag}" for tag in operation.get("tags", [])
                ],
            }
            if security is not None:
                merged["security"] = security
            target[method] = merged
    for name, value in one.get("components", {}).get("schemas", {}).items():
        components.setdefault("schemas", {}).setdefault(name, value)


def _version() -> str:
    try:
        return version("gc-server")
    except PackageNotFoundError:
        return "0"


__all__ = ["DOC_PATHS", "apply_api_docs", "combined_docs", "docs_paths"]
