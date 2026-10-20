"""Поверхность a2a: агент как пир A2A (карточка, JSON-RPC ``/rpc``).

Канал объявляет о себе: человека нет — подтверждение отказывает само;
слова пира — запрос внешней системы. ``turn_ask_human`` остаётся: его текст —
вопрос задачи (``INPUT_REQUIRED``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from gc_core.backends.factory import resolve_os_backend
from gc_core.config_modules.auth import build_auth_connector
from gc_core.constants.tool_names import INTERMEDIATE_REPLY_TOOL_NAME
from gc_core.settings import LoadedSettings
from gc_core.tools.connect_mcp.registry import ToolsCache

from gc_server.a2a.facade import A2aFacade
from gc_server.a2a.http_api import create_app
from gc_server.chat.surface import backend_rejection, stop_channel_tools
from gc_server.config import (
    A2aConfig,
    ServerConfigError,
    reject_moved_section,
)

if TYPE_CHECKING:
    from gc_server.app import ServerRuntime

_CHANNEL_HIDDEN_TOOLS = (INTERMEDIATE_REPLY_TOOL_NAME,)
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def apply_a2a_runtime_defaults(
    loaded: LoadedSettings, section: A2aConfig | None = None
) -> LoadedSettings:
    """Канальные умолчания (секция ``a2a``) → настройки и рантайм хода."""
    section = section or A2aConfig()
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
        human_channel=False,
        external_user_words=True,
    )


def auth_rejection(section: Any) -> str | None:
    """Не-loopback без токена — отказ до старта: агента с тулами нельзя
    светить в сеть без авторизации."""
    if section.auth_token:
        return None
    host = str(section.host or "").strip().lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if host in _LOOPBACK_HOSTS:
        return None
    return f"a2a: host {section.host} не loopback — нужен непустой auth_token"


class A2aSurface:
    name = "a2a"

    def __init__(self, runtime: ServerRuntime) -> None:
        self.runtime = runtime
        self.section = runtime.config.surface("a2a")
        reject_moved_section(runtime.base, "a2a")
        loaded = runtime.surface_settings("a2a")
        for rejection in (
            backend_rejection(loaded, "a2a"),
            auth_rejection(self.section),
        ):
            if rejection is not None:
                raise ServerConfigError(rejection)
        self.settings = apply_a2a_runtime_defaults(loaded, self.section)
        backend = resolve_os_backend(self.settings)
        self.facade = A2aFacade(
            settings=self.settings,
            agent_factory=runtime.agent_factory(self.settings),
            auth_connector=build_auth_connector(self.settings),
            tools_cache=ToolsCache(os_backend=backend),
            os_backend=backend,
            section=self.section,
        )

    def app(self) -> FastAPI:
        return create_app(self.facade)

    async def astart(self) -> None:
        return None

    async def astop(self) -> None:
        await self.facade.shutdown()
