"""Ручки списка диалогов и скачивания файлов хода."""

from __future__ import annotations

import json

from fastapi import FastAPI, Request
from fastapi.responses import Response

from gc_chat.dialogs import (
    artifact_bytes,
    dialog_artifacts,
    dialog_list,
    dialog_turns,
)
from gc_chat.responses.errors import ApiError, error_payload
from gc_chat.responses.http_api import _unauthorized
from gc_chat.responses.index_store import ResponseIndex

_JSON = "application/json"
_NOT_FOUND = "dialog not found"


def mount(app: FastAPI) -> None:
    app.add_api_route("/v1/dialogs", list_dialogs, methods=["GET"])
    app.add_api_route("/v1/dialogs/{thread_id}", read_dialog, methods=["GET"])
    app.add_api_route(
        "/v1/dialogs/{thread_id}/artifacts",
        read_artifacts,
        methods=["GET"],
    )
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
    return _body(dialog_list(_index(request)))


async def read_dialog(thread_id: str, request: Request) -> Response:
    denied = _guard(request)
    if denied is not None:
        return denied
    turns = dialog_turns(_index(request), thread_id)
    if turns is None:
        return _missing()
    return _body(turns)


async def read_artifacts(thread_id: str, request: Request) -> Response:
    denied = _guard(request)
    if denied is not None:
        return denied
    artifacts = dialog_artifacts(_index(request), thread_id)
    if artifacts is None:
        return _missing()
    return _body(artifacts)


async def download_artifact(
    thread_id: str, request: Request, path: str = ""
) -> Response:
    denied = _guard(request)
    if denied is not None:
        return denied
    found = artifact_bytes(_index(request), thread_id, path)
    if found is None:
        return _missing()
    name, data = found
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": _disposition(name)},
    )


def _missing() -> Response:
    exc = ApiError(404, "not_found", _NOT_FOUND)
    return _body(error_payload(exc), 404)


def _disposition(name: str) -> str:
    cleaned = (
        name.replace("\\", "")
        .replace('"', "")
        .replace("\r", "")
        .replace("\n", "")
    )
    if cleaned == "":
        return "attachment"
    return 'attachment; filename="' + cleaned + '"'


def _body(payload: object, status_code: int = 200) -> Response:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return Response(content=raw, status_code=status_code, media_type=_JSON)
