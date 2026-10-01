"""Один FastAPI: Responses и Chat Completions на одном сокете."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from gc_chat.completions.facade import CompletionsFacade
from gc_chat.completions.http_api import mount as mount_completions
from gc_chat.dialogs_http import mount as mount_dialogs
from gc_chat.page import mount as mount_page
from gc_chat.responses.facade import ResponsesFacade
from gc_chat.responses.http_api import models
from gc_chat.responses.http_api import mount as mount_responses


def create_app(
    responses: ResponsesFacade, completions: CompletionsFacade
) -> FastAPI:
    app = FastAPI()
    app.state.responses_facade = responses
    app.state.completions_facade = completions
    app.state.responses_index = responses._index
    _install_cors(app, responses.cors_origins)
    mount_page(app)
    app.add_api_route("/v1/models", models, methods=["GET"])
    mount_responses(app)
    mount_completions(app)
    mount_dialogs(app)
    return app


def _install_cors(app: FastAPI, origins: list[str]) -> None:
    """Пустой список — любой origin. Непустой — только перечисленные.

    ``allow_private_network`` отвечает на preflight Chrome к loopback.
    Страница чата этого процесса идёт с того же origin и CORS не использует.
    Заголовок нужен чужим клиентам.
    """
    allow = list(origins) if origins else ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        allow_private_network=True,
    )
