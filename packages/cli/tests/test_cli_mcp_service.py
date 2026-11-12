"""``gc-cli`` — хост сервиса MCP: сервис и потребитель на прогон.

Прогон создаёт сервис с политикой «на старте» и ожиданием всех серверов,
открывает потребителя до сборки контекста, передаёт его сборке и на выходе
при любом исходе закрывает потребителя, затем сервис, затем клиенты httpx
(спека §5.1, §5.12, §6). Записывающий двойник сервиса проверяет порядок;
сквозной тест на настоящем сервисе и тестовом stdio-сервере — что тул
доступен в прогоне и после прогона процесса сервера нет.
"""

import sys

# `import cli` разбирает sys.argv на импорте (-q обязателен).
sys.argv = ["gc-cli", "-q", "x"]

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
import yaml
from gc_cli import cli
from gc_core.tools.connect_mcp.registry import mcp_tool_full_name
from gc_core.utils.logging_module import RunLogDirUnavailable
from gc_testkit import scripted_model
from gc_testkit.mcp import alive, exits, launches, stdio_cfg
from gc_testkit.mcp.fixtures import mcp_home, mcp_owner, mcp_reaper

#: Потолок одного прогона в тесте: зависание не повесит набор.
_CEILING_SEC = 60.0


class _FakeConsumer:
    def __init__(self, events: list[str], fail_close: bool) -> None:
        self._events = events
        self._fail_close = fail_close
        #: Задано — закрытие потребителя ждёт его (глухая сессия).
        self.hold: asyncio.Event | None = None

    async def aclose(self) -> None:
        self._events.append("consumer.aclose")
        if self.hold is not None:
            await self.hold.wait()
        if self._fail_close:
            raise RuntimeError("закрытие потребителя упало")


class _FakeService:
    def __init__(self, fake: "_FakeMcp") -> None:
        self._fake = fake

    def open_consumer(self, label: str) -> _FakeConsumer:
        self._fake.events.append("open_consumer")
        self._fake.label = label
        return self._fake.consumer

    async def aclose(self, budget: float = 10.0) -> None:
        self._fake.events.append("service.aclose")
        self._fake.budget = budget
        if self._fake.fail_close:
            raise RuntimeError("закрытие сервиса упало")


class _FakeMcp:
    """Записывающий двойник ``McpService``: настоящие имена методов."""

    def __init__(self, *, fail_close: bool = False) -> None:
        self.events: list[str] = []
        self.kwargs: dict | None = None
        self.label: str | None = None
        self.budget: float | None = None
        self.fail_close = fail_close
        self.consumer = _FakeConsumer(self.events, fail_close)

    def __call__(self, **kwargs) -> _FakeService:
        self.kwargs = kwargs
        return _FakeService(self)

    @property
    def closed(self) -> bool:
        """Потребитель закрыт, затем сервис — ровно по разу."""
        closes = [e for e in self.events if e.endswith(".aclose")]
        return closes == ["consumer.aclose", "service.aclose"]


def _config(tmp_path, servers: dict | None = None) -> str:
    path = tmp_path / "config.yml"
    data = {
        "backend": {
            "api_mode": "chat_completions",
            "completions_url": "http://scripted.invalid/v1/chat/completions",
            "chat_completion_kwargs": {"stream": False},
        },
        "agent": {
            "model": "scripted",
            "max_steps": 6,
            "use_local_subagents": False,
            "strict_turn_finish_validation": False,
        },
        "logging": {"save_logs": False, "console_trace": False},
        "paths": {"threads_data_path": str(tmp_path / "threads")},
    }
    if servers is not None:
        data["mcp"] = {
            "servers": servers,
            "meta": {"discovery_timeout_sec": 30},
        }
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return str(path)


def _set_args(monkeypatch, *argv: str) -> None:
    monkeypatch.setattr(
        cli.arg_parser, "run_args", cli.arg_parser.parser.parse_args(argv)
    )


@pytest.fixture
def host(monkeypatch, tmp_path):
    """Прогон без сети: заглушки служебных шагов запуска."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_ensure_project_gc_dir", lambda: None)
    monkeypatch.setattr(cli, "_ensure_user_skills_dir", lambda: None)
    from gc_core import upgrade_gate

    monkeypatch.setattr(upgrade_gate, "detect_upgrade", lambda path=None: None)
    monkeypatch.setattr(
        upgrade_gate, "mark_current_run", lambda path=None: None
    )
    return tmp_path


@pytest.fixture
def fake(monkeypatch, host):
    """Двойник сервиса, сборка и исполнитель подменены, события по порядку."""
    mcp = _FakeMcp()
    monkeypatch.setattr(cli, "McpService", mcp)
    mcp.build = {}

    async def _build(settings, **kwargs):
        mcp.build = kwargs
        return SimpleNamespace(logger=MagicMock())

    async def _aclose_clients():
        mcp.events.append("aclose_clients")

    async def _arun(*_a, **_kw):
        return "ok"

    monkeypatch.setattr(cli, "acreate_run_context", _build)
    monkeypatch.setattr(
        "gc_core.utils.async_http.aclose_clients", _aclose_clients
    )
    mcp.executor = SimpleNamespace(
        arun=_arun, last_terminal_outcome=None, get_state=lambda: None
    )
    monkeypatch.setattr(cli, "AgentExecutor", lambda _ctx: mcp.executor)
    _set_args(monkeypatch, "-q", "сделай", "--config", _config(host))
    return mcp


async def _arun_cli() -> int:
    return await asyncio.wait_for(cli.arun_cli(), _CEILING_SEC)


async def test_service_waits_all(fake):
    assert await _arun_cli() == 0
    assert fake.kwargs == {"connect_on_start": True, "wait_all": True}
    assert fake.label == "cli"


async def test_run_passes_consumer_and_closes_service(fake):
    assert await _arun_cli() == 0
    assert fake.build["mcp_consumer"] is fake.consumer
    assert "tools_cache" not in fake.build
    assert fake.events == [
        "open_consumer",
        "consumer.aclose",
        "service.aclose",
        "aclose_clients",
    ]


async def test_failed_build_closes_service(fake, monkeypatch):
    async def _boom(settings, **kwargs):
        raise RuntimeError("сборка упала")

    monkeypatch.setattr(cli, "acreate_run_context", _boom)
    with pytest.raises(RuntimeError, match="сборка упала"):
        await _arun_cli()
    assert fake.closed
    assert fake.events[-1] == "aclose_clients"


async def test_no_context_branch_closes_service(fake, monkeypatch, capsys):
    """Каталог логов недоступен: строка в stderr, код 1, уборка полная."""

    async def _no_logs(settings, **kwargs):
        raise RunLogDirUnavailable("каталог логов недоступен")

    monkeypatch.setattr(cli, "acreate_run_context", _no_logs)
    assert await _arun_cli() == 1
    assert "каталог логов недоступен" in capsys.readouterr().err
    assert fake.closed
    assert fake.events[-1] == "aclose_clients"


async def test_failed_turn_closes_service(fake):
    async def _boom(*_a, **_kw):
        raise RuntimeError("ход упал")

    fake.executor.arun = _boom
    with pytest.raises(RuntimeError, match="ход упал"):
        await _arun_cli()
    assert fake.events == [
        "open_consumer",
        "consumer.aclose",
        "service.aclose",
        "aclose_clients",
    ]


async def test_cancelled_run_closes_service(fake):
    """Отмена прогона посреди хода (так ``asyncio.run`` снимает прогон по
    второму Ctrl+C): сервис закрыт до выхода из цикла."""
    started = asyncio.Event()

    async def _hang(*_a, **_kw):
        started.set()
        await asyncio.Event().wait()

    fake.executor.arun = _hang
    run = asyncio.ensure_future(cli.arun_cli())
    await asyncio.wait_for(started.wait(), _CEILING_SEC)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, _CEILING_SEC)
    assert fake.closed
    assert fake.events[-1] == "aclose_clients"


def test_keyboard_interrupt_closes_service(fake):
    """``KeyboardInterrupt`` из хода: закрытие идёт на цикле прогона,
    прерывание выходит наружу как было."""

    async def _interrupt(*_a, **_kw):
        raise KeyboardInterrupt

    fake.executor.arun = _interrupt
    with pytest.raises(KeyboardInterrupt):
        cli.main()
    assert fake.closed
    assert fake.events[-1] == "aclose_clients"


async def test_no_tool_response_exit_after_close(fake, capsys):
    """Выход PR 189 (ход без вызова тулов — код 1) — после уборки."""
    fake.executor.last_terminal_outcome = cli.NO_TOOL_RESPONSE
    assert await _arun_cli() == 1
    assert cli.NO_TOOL_RESPONSE_MESSAGE in capsys.readouterr().err
    assert fake.closed


async def test_close_failure_keeps_exit_code(monkeypatch, fake):
    """Отказ закрытия потребителя и сервиса — в лог; код выхода хода."""
    failing = _FakeMcp(fail_close=True)
    monkeypatch.setattr(cli, "McpService", failing)
    assert await _arun_cli() == 0
    assert failing.closed
    assert fake.events == ["aclose_clients"]


async def test_slow_consumer_close_is_bounded(fake, monkeypatch):
    """Закрытие потребителя не уложилось в бюджет: его не ждут дальше,
    сервис закрывается остатком бюджета (его закрытие возвращается не
    позже бюджета и отсрочки)."""
    monkeypatch.setattr(cli, "_MCP_CLOSE_BUDGET_SEC", 0.05)
    fake.consumer.hold = asyncio.Event()
    try:
        assert await _arun_cli() == 0
        assert fake.events == [
            "open_consumer",
            "consumer.aclose",
            "service.aclose",
            "aclose_clients",
        ]
        assert 0.0 <= fake.budget < 0.05
    finally:
        fake.consumer.hold.set()
        await asyncio.sleep(0)


async def test_init_passes_consumer_and_closes_service(
    fake, host, monkeypatch
):
    _set_args(monkeypatch, "--init", "--config", _config(host))
    monkeypatch.setattr(
        "gc_core.security.confirm_port.confirm_available",
        lambda _ctx: False,
    )
    code = await asyncio.wait_for(cli._run_init_agents_md(), _CEILING_SEC)
    assert code == 0
    assert fake.build["mcp_consumer"] is fake.consumer
    assert "tools_cache" not in fake.build
    assert fake.kwargs == {"connect_on_start": True, "wait_all": True}
    assert fake.events == [
        "open_consumer",
        "consumer.aclose",
        "service.aclose",
        "aclose_clients",
    ]


async def test_init_failed_build_closes_service(fake, host, monkeypatch):
    async def _boom(settings, **kwargs):
        raise RuntimeError("сборка упала")

    monkeypatch.setattr(cli, "acreate_run_context", _boom)
    _set_args(monkeypatch, "--init", "--config", _config(host))
    with pytest.raises(RuntimeError, match="сборка упала"):
        await asyncio.wait_for(cli._run_init_agents_md(), _CEILING_SEC)
    assert fake.closed
    assert fake.events[-1] == "aclose_clients"


# --- сквозной: настоящий сервис и тестовый stdio-сервер -------------------

_ECHO = mcp_tool_full_name("s", "echo")


def _turn(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": f"scripted-{call_id}",
        "object": "chat.completion",
        "model": "scripted",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        },
    }


def _install_echo_model(monkeypatch) -> list[dict]:
    """Модель зовёт ``echo`` сервера, увидев его в тулах, затем кончает
    ход; копит pid сервера из ответов тула в запросах к ней."""
    from gc_core.utils import connect_backend

    pids: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content or b"{}")
        tools = {t["function"]["name"] for t in payload.get("tools") or []}
        outputs = [
            m["content"]
            for m in payload.get("messages", [])
            if m.get("role") == "tool" and m.get("tool_call_id") == "echo"
        ]
        if outputs:
            result = json.loads(outputs[-1])["result"]
            pids.append(json.loads(result["content"][0]["text"])["pid"])
            return httpx.Response(200, json=scripted_model.FINISH)
        if _ECHO in tools:
            turn = _turn("echo", _ECHO, {"text": "привет"})
            return httpx.Response(200, json=turn)
        return httpx.Response(200, json=scripted_model.FINISH)

    monkeypatch.setattr(
        connect_backend,
        "get_async_client",
        lambda **_kw: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )
    return pids


@pytest.mark.skipif(os.name != "posix", reason="процессы POSIX")
async def test_run_owns_server_process(
    host, monkeypatch, mcp_home, mcp_reaper, mcp_owner
):
    """Тул сервера доступен в прогоне; после прогона процесса сервера нет;
    следующий прогон поднимает новый."""
    control = host / "s.json"
    echoed = _install_echo_model(monkeypatch)
    config = _config(host, {"s": stdio_cfg(control)})
    _set_args(monkeypatch, "-q", "сделай", "--config", config)

    assert await _arun_cli() == 0
    (first,) = launches(control)
    assert echoed == [first]
    assert exits(control) == [first]
    assert not alive(first)

    assert await _arun_cli() == 0
    assert launches(control)[0] == first
    (second,) = launches(control)[1:]
    assert second != first
    assert echoed == [first, second]
    assert exits(control) == [first, second]
    assert not alive(second)
