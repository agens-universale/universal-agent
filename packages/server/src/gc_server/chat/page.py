"""Страница чата: один HTML из пакета, без авторизации."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import Response

_PAGE = Path(__file__).with_name("chat_page.html")
_HTML = "text/html; charset=utf-8"


def mount(app: FastAPI) -> None:
    app.add_api_route("/", chat_page, methods=["GET"])


def chat_page() -> Response:
    return Response(content=_PAGE.read_bytes(), media_type=_HTML)
