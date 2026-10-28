"""Один FastAPI: Responses и Chat Completions на одном сокете."""

from __future__ import annotations

from fastapi import FastAPI

from gc_server.chat.completions.facade import CompletionsFacade
from gc_server.chat.completions.http_api import mount as mount_completions
from gc_server.chat.dialogs_http import mount as mount_dialogs
from gc_server.chat.http_guard import install_http_guard
from gc_server.chat.page import mount as mount_page
from gc_server.chat.responses.facade import ResponsesFacade
from gc_server.chat.responses.http_api import models
from gc_server.chat.responses.http_api import mount as mount_responses


def create_app(
    responses: ResponsesFacade, completions: CompletionsFacade
) -> FastAPI:
    app = FastAPI()
    app.state.responses_facade = responses
    app.state.completions_facade = completions
    app.state.responses_index = responses._index
    install_http_guard(
        app,
        cors_origins=responses.cors_origins,
        bind_host=responses.bind_host,
        max_body_bytes=responses.max_body_bytes,
    )
    mount_page(app)
    app.add_api_route("/v1/models", models, methods=["GET"])
    mount_responses(app)
    mount_completions(app)
    mount_dialogs(app)
    return app
