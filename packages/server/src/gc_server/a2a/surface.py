"""Поверхность a2a: агент как пир A2A на официальном ``a2a-sdk``.

Карточка ``/.well-known/agent-card.json``, JSON-RPC ``/rpc`` и REST, стрим
(``SendStreamingMessage``, ``SubscribeToTask``) и push-уведомления. Задачи —
в хранилище тредов: видны с любого инстанса; стрим идущей задачи — на
инстансе, который её ведёт.

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

from gc_server.chat.surface import backend_rejection, stop_channel_tools
from gc_server.config import (
    A2aConfig,
    ServerConfigError,
    reject_moved_section,
)
from gc_server.surface import install_bearer

if TYPE_CHECKING:
    from gc_server.app import ServerRuntime

_CHANNEL_HIDDEN_TOOLS = (INTERMEDIATE_REPLY_TOOL_NAME,)
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
NO_SDK = (
    "поверхность a2a работает на a2a-sdk: установите gc-server[a2a] "
    "(uv tool install 'gc-server[a2a] @ …')"
)


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


def allowed_push_url(hosts: list[str], validator: Any) -> Any:
    """Проверка адреса push: хост из списка и проверка SDK."""
    from urllib.parse import urlparse

    allowed = frozenset(hosts)

    async def check(url: str) -> bool:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
        return host in allowed and await validator(url)

    return check


def _require_sdk() -> None:
    try:
        import a2a.server.request_handlers  # noqa: F401  # проверка extra
    except ImportError:
        raise ServerConfigError(NO_SDK) from None


class A2aSurface:
    name = "a2a"

    def __init__(
        self,
        runtime: ServerRuntime,
        *,
        push_client: Any = None,
        push_url_validator: Any = None,
    ) -> None:
        _require_sdk()
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
        self._push_client = push_client
        self._push_url_validator = push_url_validator
        self._own_push_client = False
        self.executor = self._executor()

    def _executor(self):
        from gc_server.a2a.executor import GcAgentExecutor

        backend = resolve_os_backend(self.settings)
        return GcAgentExecutor(
            settings=self.settings,
            agent_factory=self.runtime.agent_factory(self.settings),
            threads=self.runtime.threads(),
            launch_root=self.section.launch_root,
            auth_connector=build_auth_connector(self.settings),
            tools_cache=ToolsCache(os_backend=backend),
            os_backend=backend,
        )

    def card(self):
        from gc_server.a2a.card import agent_card, interface_urls

        return agent_card(
            name=self.section.name,
            description=self.section.description,
            urls=interface_urls(
                self.section.public_url,
                str(self.section.host),
                self.section.port,
            ),
            secured=bool(self.section.auth_token),
            push=bool(self.section.push_allowed_hosts),
        )

    def app(self) -> FastAPI:
        import httpx
        from a2a.server.request_handlers.default_request_handler_v2 import (
            DefaultRequestHandlerV2,
        )
        from a2a.server.routes.agent_card_routes import (
            create_agent_card_routes,
        )
        from a2a.server.routes.fastapi_routes import add_a2a_routes_to_fastapi
        from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
        from a2a.server.routes.rest_routes import create_rest_routes
        from a2a.server.tasks import BasePushNotificationSender
        from a2a.utils.push_url_validator import (
            validate_push_notification_url,
        )

        from gc_server.a2a.card import CARD_PATH, RPC_PATH
        from gc_server.a2a.task_store import (
            ThreadPushConfigStore,
            ThreadTaskStore,
        )
        from gc_server.chat.http_guard import install_http_guard

        threads = self.runtime.threads()
        card = self.card()
        push: dict = {}
        if self.section.push_allowed_hosts:
            push_store = ThreadPushConfigStore(threads)
            if self._push_client is None:
                self._push_client = httpx.AsyncClient(timeout=10.0)
                self._own_push_client = True
            # Адрес push задаёт пир, а ходит по нему сервер: только хосты из
            # a2a.push_allowed_hosts, и те не во внутреннюю сеть (проверка
            # SDK: метаданные облака, локальные сервисы).
            validator = allowed_push_url(
                self.section.push_allowed_hosts,
                self._push_url_validator or validate_push_notification_url,
            )
            push = {
                "push_config_store": push_store,
                "push_sender": BasePushNotificationSender(
                    self._push_client,
                    push_store,
                    push_url_validator=validator,
                ),
                "push_url_validator": validator,
            }
        handler = DefaultRequestHandlerV2(
            agent_executor=self.executor,
            task_store=ThreadTaskStore(threads),
            agent_card=card,
            **push,
        )
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        add_a2a_routes_to_fastapi(
            app,
            agent_card_routes=create_agent_card_routes(card),
            jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url=RPC_PATH),
            rest_routes=create_rest_routes(handler),
        )
        install_http_guard(
            app, cors_origins=[], bind_host=str(self.section.host)
        )
        install_bearer(app, self.section.auth_token, public_paths=(CARD_PATH,))
        return app

    async def astart(self) -> None:
        return None

    async def astop(self) -> None:
        await self.executor.shutdown()
        if self._own_push_client and self._push_client is not None:
            await self._push_client.aclose()
