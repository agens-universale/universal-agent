"""Один FastAPI: Responses и Chat Completions на одном сокете."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from gc_chat.completions.facade import CompletionsFacade
from gc_chat.completions.http_api import mount as mount_completions
from gc_chat.responses.facade import ResponsesFacade
from gc_chat.responses.http_api import models
from gc_chat.responses.http_api import mount as mount_responses


def create_app(
    responses: ResponsesFacade, completions: CompletionsFacade
) -> FastAPI:
    app = FastAPI()
    app.state.responses_facade = responses
    app.state.completions_facade = completions
    _install_cors(app, responses.cors_origins)
    app.add_api_route("/v1/models", models, methods=["GET"])
    mount_responses(app)
    mount_completions(app)
    return app


def _install_cors(app: FastAPI, origins: list[str]) -> None:
    if not origins:
        return
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(origins),
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )
