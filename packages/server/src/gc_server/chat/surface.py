"""Поверхность chat: Responses и Chat Completions на одном адресе.

Канальные умолчания (секция ``chat`` config_server.yml) ложатся на настройки
агента; ``turn_ask_human`` и ``turn_intermediate_reply`` снимаются — канал
их не показывает.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from gc_core.backends.factory import parse_os_backend_kind, resolve_os_backend
from gc_core.config_modules.auth import build_auth_connector
from gc_core.constants.tool_names import (
    ASK_HUMAN_TOOL_NAME,
    INTERMEDIATE_REPLY_TOOL_NAME,
)
from gc_core.settings import LoadedSettings
from gc_core.tools.connect_mcp.service import McpService

from gc_server.chat.app import create_app
from gc_server.chat.completions.facade import CompletionsFacade
from gc_server.chat.model_route import reserved_model_reason
from gc_server.chat.responses.facade import ResponsesFacade
from gc_server.config import (
    ChatConfig,
    ServerConfigError,
    reject_moved_section,
)
from gc_server.surface import aclose_mcp_service

if TYPE_CHECKING:
    from gc_server.app import ServerRuntime

# Вопрос человеку и промежуточная реплика в схему не входят: вопрос уходит
# прозой, прогресс тоже.
_CHANNEL_HIDDEN_TOOLS = (ASK_HUMAN_TOOL_NAME, INTERMEDIATE_REPLY_TOOL_NAME)


def apply_chat_runtime_defaults(
    loaded: LoadedSettings, section: ChatConfig | None = None
) -> LoadedSettings:
    """Канальные умолчания (секция ``chat``) → настройки и рантайм хода."""
    section = section or ChatConfig()
    gate = section.finish_gate_mode
    return loaded.with_overrides(
        {
            "agent.strict_turn_finish_validation": gate != "off",
            "todo.reminder_steps": section.todo_reminder_steps,
            "agent.stop_tools": stop_channel_tools(
                loaded.settings.agent.stop_tools, _CHANNEL_HIDDEN_TOOLS
            ),
        },
        layer="runtime",
    ).with_runtime(
        text_answer_mode=section.text_answer_mode,
        finish_gate_smart=gate == "smart",
    )


def stop_channel_tools(current: Any, hidden: tuple[str, ...]) -> list[str]:
    """Полный список ``agent.stop_tools``: правка заменяет, а не сливает."""
    names = list(current or [])
    names.extend(name for name in hidden if name not in names)
    return names


def backend_rejection(loaded: Any, surface: str) -> str | None:
    """Только ``os_backend: local``: каталог хода — путь на машине этого
    процесса. Иначе — отказ до открытия порта."""
    try:
        kind = parse_os_backend_kind(loaded)
    except ValueError as exc:
        return str(exc)
    if kind == "local":
        return None
    return (
        f"{surface}: os_backend {kind} на этом запуске не поддерживается: "
        "рабочий каталог хода — путь на машине этого процесса"
    )


class ChatSurface:
    name = "chat"

    def __init__(self, runtime: ServerRuntime) -> None:
        self.runtime = runtime
        self.section = runtime.config.surface("chat")
        reject_moved_section(runtime.base, "chat")
        loaded = runtime.surface_settings("chat")
        rejection = backend_rejection(loaded, "chat")
        if rejection is not None:
            raise ServerConfigError(rejection)
        collision = reserved_model_reason(
            str(loaded.settings.agent.model or "")
        )
        if collision is not None:
            raise ServerConfigError(collision)
        self.settings = apply_chat_runtime_defaults(loaded, self.section)
        auth = build_auth_connector(self.settings)
        backend = resolve_os_backend(self.settings)
        # Один сервис на поверхность, общий для Responses и Completions;
        # сервер подключается первым вызовом тула, если тулы известны.
        self._mcp_service = McpService(
            os_backend=backend, connect_on_start=False
        )
        shared = {
            "settings": self.settings,
            "agent_factory": runtime.agent_factory(
                self.settings, surface="chat"
            ),
            "auth_connector": auth,
            "mcp_service": self._mcp_service,
            "os_backend": backend,
            "section": self.section,
        }
        self.responses = ResponsesFacade(
            **shared, index=runtime.response_index()
        )
        self.completions = CompletionsFacade(
            **shared,
            threads=(
                runtime.threads("chat")
                if "thread_store" in runtime.store_kwargs()
                else None
            ),
        )

    def app(self) -> FastAPI:
        from gc_server.api_docs import apply_api_docs

        app = create_app(self.responses, self.completions)
        apply_api_docs(app, "chat", self.section)
        return app

    async def astart(self) -> None:
        self.responses.recovery.start()

    async def astop(self) -> None:
        # Порядок — у ``aclose_mcp_service``: ходы, сервис, агенты.
        try:
            await self.responses.stop_turns()
        finally:
            await aclose_mcp_service(self.name, self._mcp_service)
        await self.responses.shutdown()
