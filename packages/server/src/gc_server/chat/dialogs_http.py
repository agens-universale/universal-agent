"""Ручки списка диалогов, каталога хода, папки и удаления."""

from __future__ import annotations

import json
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import Response

from gc_server.chat.dialogs import (
    delete_path,
    dialog_folder,
    dialog_list,
    dialog_turns,
    file_bytes,
    list_folder,
    make_directory,
    make_folder,
    read_file,
    remove_entry,
)
from gc_server.chat.responses.errors import ApiError, error_payload
from gc_server.chat.responses.http_api import _unauthorized
from gc_server.chat.responses.index_store import ResponseIndex

_JSON = "application/json"
_NOT_FOUND = "dialog not found"
_PATH_NOT_FOUND = "path not found"
_WORKSPACE_ARTIFACTS = "/v1/workspace/artifacts"
_DIALOG_ARTIFACTS = "/v1/dialogs/{thread_id}/artifacts"


def mount(app: FastAPI) -> None:
    app.add_api_route(_WORKSPACE_ARTIFACTS, read_workspace, methods=["GET"])
    app.add_api_route(_WORKSPACE_ARTIFACTS, create_workspace, methods=["POST"])
    app.add_api_route(
        _WORKSPACE_ARTIFACTS, delete_workspace, methods=["DELETE"]
    )
    app.add_api_route(
        "/v1/workspace/artifacts/download",
        download_workspace,
        methods=["GET"],
    )
    app.add_api_route("/v1/dialogs", list_dialogs, methods=["GET"])
    app.add_api_route("/v1/dialogs/{thread_id}", read_dialog, methods=["GET"])
    app.add_api_route(_DIALOG_ARTIFACTS, read_artifacts, methods=["GET"])
    app.add_api_route(_DIALOG_ARTIFACTS, create_artifact, methods=["POST"])
    app.add_api_route(_DIALOG_ARTIFACTS, delete_artifact, methods=["DELETE"])
    app.add_api_route(
        "/v1/dialogs/{thread_id}/artifacts/download",
        download_artifact,
        methods=["GET"],
    )


def _index(request: Request) -> ResponseIndex:
    return request.app.state.responses_index


def _guard(request: Request) -> Response | None:
    token = request.app.state.responses_facade.auth_token
    return _unauthorized(request, token)


async def list_dialogs(request: Request) -> Response:
    denied = _guard(request)
    if denied is not None:
        return denied
    return _body(await dialog_list(_index(request)))


async def read_dialog(thread_id: str, request: Request) -> Response:
    denied = _guard(request)
    if denied is not None:
        return denied
    turns = await dialog_turns(_index(request), thread_id)
    if turns is None:
        return _missing()
    return _body(turns)


async def read_workspace(request: Request, path: str = "") -> Response:
    denied = _guard(request)
    if denied is not None:
        return denied
    root = _root(request)
    folder = list_folder(root, path)
    if folder is None:
        return _missing(_PATH_NOT_FOUND)
    return _body(
        {"cwd": root, "path": folder["path"], "entries": folder["entries"]}
    )


async def create_workspace(request: Request) -> Response:
    denied = _guard(request)
    if denied is not None:
        return denied
    body, rejected = await _object_body(request)
    if rejected is not None:
        return rejected
    parent, name, invalid = _folder_fields(body)
    if invalid is not None:
        return invalid
    try:
        created = make_folder(_root(request), parent, name)
    except ApiError as exc:
        return _body(error_payload(exc), exc.status)
    if created is None:
        return _missing(_PATH_NOT_FOUND)
    return _body({"path": created, "kind": "dir"})


async def delete_workspace(request: Request, path: str = "") -> Response:
    denied = _guard(request)
    if denied is not None:
        return denied
    if not delete_path(_root(request), path):
        return _missing(_PATH_NOT_FOUND)
    return _body({"ok": True})


async def download_workspace(request: Request, path: str = "") -> Response:
    denied = _guard(request)
    if denied is not None:
        return denied
    found = read_file(_root(request), path)
    if found is None:
        return _missing(_PATH_NOT_FOUND)
    return _download(found)


async def read_artifacts(
    thread_id: str, request: Request, path: str = ""
) -> Response:
    denied = _guard(request)
    if denied is not None:
        return denied
    folder = await dialog_folder(_index(request), thread_id, path)
    if folder is None:
        return _missing()
    return _body(folder)


async def create_artifact(thread_id: str, request: Request) -> Response:
    denied = _guard(request)
    if denied is not None:
        return denied
    running = _running(request, thread_id)
    if running is not None:
        return running
    body, rejected = await _object_body(request)
    if rejected is not None:
        return rejected
    parent, name, invalid = _folder_fields(body)
    if invalid is not None:
        return invalid
    try:
        created = await make_directory(
            _index(request), thread_id, parent, name
        )
    except ApiError as exc:
        return _body(error_payload(exc), exc.status)
    if created is None:
        return _missing()
    return _body({"path": created, "kind": "dir"})


async def delete_artifact(
    thread_id: str, request: Request, path: str = ""
) -> Response:
    denied = _guard(request)
    if denied is not None:
        return denied
    running = _running(request, thread_id)
    if running is not None:
        return running
    if not await remove_entry(_index(request), thread_id, path):
        return _missing()
    return _body({"ok": True})


async def download_artifact(
    thread_id: str, request: Request, path: str = ""
) -> Response:
    denied = _guard(request)
    if denied is not None:
        return denied
    found = await file_bytes(_index(request), thread_id, path)
    if found is None:
        return _missing()
    return _download(found)


def _root(request: Request) -> str:
    return request.app.state.responses_facade.new_dialog_cwd()


def _folder_fields(
    body: dict,
) -> tuple[str, str, Response | None]:
    parent = body.get("path", "")
    name = body.get("name", "")
    if not isinstance(parent, str) or not isinstance(name, str):
        return "", "", _invalid("invalid name")
    return parent, name, None


def _download(found: tuple[str, bytes]) -> Response:
    name, data = found
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": _disposition(name)},
    )


def _running(request: Request, thread_id: str) -> Response | None:
    facade = request.app.state.responses_facade
    if not facade.turn_busy(thread_id):
        return None
    exc = ApiError(409, "conflict", "turn already running")
    return _body(error_payload(exc), 409)


def _missing(message: str = _NOT_FOUND) -> Response:
    exc = ApiError(404, "not_found", message)
    return _body(error_payload(exc), 404)


def _invalid(message: str) -> Response:
    exc = ApiError(400, "invalid_request", message)
    return _body(error_payload(exc), 400)


async def _object_body(
    request: Request,
) -> tuple[dict | None, Response | None]:
    try:
        body = await request.json()
    except ValueError:
        return None, _invalid("request body must be an object")
    if not isinstance(body, dict):
        return None, _invalid("request body must be an object")
    return body, None


def _disposition(name: str) -> str:
    cleaned = (
        name.replace("\\", "")
        .replace('"', "")
        .replace("\r", "")
        .replace("\n", "")
    )
    if cleaned == "":
        return "attachment"
    if cleaned.isascii():
        return 'attachment; filename="' + cleaned + '"'
    # Windows и Mac берут имя из filename*, не из latin-1 в filename.
    encoded = quote(cleaned, safe="")
    return "attachment; filename*=UTF-8''" + encoded


def _body(payload: object, status_code: int = 200) -> Response:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return Response(content=raw, status_code=status_code, media_type=_JSON)
