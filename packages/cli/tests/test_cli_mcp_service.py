"""``gc-cli`` — хост сервиса MCP: сервис и потребитель на прогон.

Прогон создаёт сервис с политикой «на старте» и ожиданием всех серверов,
открывает потребителя до сборки контекста, передаёт его сборке и на выходе
при любом исходе закрывает потребителя, затем сервис, затем клиенты httpx
(спека §5.1, §5.12, §6). Записывающий двойник сервиса проверяет порядок;
сквозные тесты на настоящем сервисе и тестовом stdio-сервере — что тул
доступен в прогоне, после прогона процесса сервера нет, а отказавший
сервер печатается прежней строкой и в набор тулов не попадает. Ctrl+C —
настоящим сигналом в отдельном процессе (``cli_sigint_driver.py``).
"""

import sys

# `import cli` разбирает sys.argv на импорте (-q обязателен).
sys.argv = ["gc-cli", "-q", "x"]

import asyncio
import json
import logging
import os
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
import yaml
from gc_cli import cli
from gc_core.tools.connect_mcp.registry import mcp_tool_full_name
from gc_core.utils.logging_module import RunLogDirUnavailable
from gc_testkit import scripted_model
from gc_testkit.mcp import (
    alive,
    exits,
    launches,
    set_behavior,
    stdio_cfg,
)
from gc_testkit.mcp.fixtures import mcp_home, mcp_owner, mcp_reaper

#: Потолок одного прогона в тесте: зависание не повесит набор.
_CEILING_SEC = 60.0


class _FakeConsumer:
    def __init__(self, events: list[str], fail_close: bool) -> None:
        self._events = events
        self._fail_close = fail_close
        #: Задано — закрытие потребителя ждёт его (глухая сессия).
        self.hold: asyncio.Event | None = None

    def servers(self) -> dict:
        return {}

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

    def kill_processes_now(self) -> None:
        self._fake.events.append("service.kill")


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


def _config(
    tmp_path, servers: dict | None = None, *, discovery: int = 30
) -> str:
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
            "meta": {"discovery_timeout_sec": discovery},
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
        mcp.events.append("turn")
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
        "turn",
        "consumer.aclose",
        "service.aclose",
        "service.kill",
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
        fake.events.append("turn")
        raise RuntimeError("ход упал")

    fake.executor.arun = _boom
    with pytest.raises(RuntimeError, match="ход упал"):
        await _arun_cli()
    assert fake.events == [
        "open_consumer",
        "turn",
        "consumer.aclose",
        "service.aclose",
        "service.kill",
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
    assert fake.budget == 0.0
    assert fake.events[-1] == "aclose_clients"


def test_keyboard_interrupt_closes_service(fake):
    """``KeyboardInterrupt`` из хода: закрытие идёт на цикле прогона без
    бюджета, прерывание выходит наружу как было."""

    async def _interrupt(*_a, **_kw):
        raise KeyboardInterrupt

    fake.executor.arun = _interrupt
    outcome: list[BaseException] = []

    def _main() -> None:
        try:
            cli.main()
        except BaseException as exc:
            outcome.append(exc)

    # Свой поток — ради потолка: синхронный `main` иначе не ограничить.
    runner = threading.Thread(target=_main, daemon=True)
    runner.start()
    runner.join(_CEILING_SEC)
    assert not runner.is_alive()
    assert [type(exc) for exc in outcome] == [KeyboardInterrupt]
    assert fake.closed
    assert fake.budget == 0.0
    assert fake.events[-1] == "aclose_clients"


async def test_no_tool_response_exit_after_close(fake, capsys):
    """Выход PR 189 (ход без вызова тулов — код 1) — после уборки."""
    fake.executor.last_terminal_outcome = cli.NO_TOOL_RESPONSE
    assert await _arun_cli() == 1
    assert cli.NO_TOOL_RESPONSE_MESSAGE in capsys.readouterr().err
    assert fake.closed


async def test_close_failure_keeps_exit_code(monkeypatch, fake, caplog):
    """Отказ закрытия потребителя и сервиса — в лог; код выхода хода."""
    failing = _FakeMcp(fail_close=True)
    monkeypatch.setattr(cli, "McpService", failing)
    with caplog.at_level(logging.WARNING, logger="gc_cli.cli"):
        assert await _arun_cli() == 0
    assert failing.closed
    assert fake.events == ["turn", "aclose_clients"]
    logged = [r.getMessage() for r in caplog.records]
    assert any(
        "потребитель MCP не закрылся" in m
        and "закрытие потребителя упало" in m
        for m in logged
    )
    assert any(
        "сервис MCP не закрылся" in m and "закрытие сервиса упало" in m
        for m in logged
    )


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
            "turn",
            "consumer.aclose",
            "service.aclose",
            "service.kill",
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
        "turn",
        "consumer.aclose",
        "service.aclose",
        "service.kill",
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


# --- отказавший сервер: прежняя строка, тулов нет ------------------------


def _install_tools_model(monkeypatch) -> list[list[str]]:
    """Модель сразу кончает ход; копит имена MCP-тулов каждого запроса."""
    from gc_core.utils import connect_backend

    seen: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content or b"{}")
        names = [t["function"]["name"] for t in payload.get("tools") or []]
        seen.append(sorted(n for n in names if n.startswith("mcp_")))
        return httpx.Response(200, json=scripted_model.FINISH)

    monkeypatch.setattr(
        connect_backend,
        "get_async_client",
        lambda **_kw: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )
    return seen


def _skipped(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith("[MCP] ")]


_LIVE_TOOLS = [mcp_tool_full_name("b", "echo"), mcp_tool_full_name("b", "nap")]


@pytest.mark.skipif(os.name != "posix", reason="процессы POSIX")
async def test_failed_server_is_printed_with_reason(
    host, monkeypatch, capsys, mcp_home, mcp_reaper, mcp_owner
):
    """Команда сервера не найдена: строка с причиной в stdout, как у
    прежнего обнаружения, и уведомление в stderr; живой сервер рядом —
    его тулы в наборе."""
    seen = _install_tools_model(monkeypatch)
    missing = {"transport": "stdio", "command": str(host / "нет"), "args": []}
    servers = {"bad": missing, "b": stdio_cfg(host / "b.json")}
    _set_args(monkeypatch, "-q", "x", "--config", _config(host, servers))

    assert await _arun_cli() == 0

    out, err = capsys.readouterr()
    (line,) = _skipped(out)
    assert line.startswith("[MCP] bad: недоступен — ")
    assert "не найдена" in line
    assert line.endswith(" (пропущен)")
    assert "MCP не подключились: bad" in err
    assert seen[0] == _LIVE_TOOLS


@pytest.mark.skipif(os.name != "posix", reason="процессы POSIX")
async def test_init_prints_failed_server_with_reason(
    host, monkeypatch, capsys, mcp_home, mcp_reaper, mcp_owner
):
    """``--init`` печатает отказ сервера той же строкой, что и прогон."""
    _install_tools_model(monkeypatch)
    missing = {"transport": "stdio", "command": str(host / "нет"), "args": []}
    _set_args(
        monkeypatch, "--init", "--config", _config(host, {"bad": missing})
    )
    monkeypatch.setattr(
        "gc_core.security.confirm_port.confirm_available",
        lambda _ctx: False,
    )

    code = await asyncio.wait_for(cli._run_init_agents_md(), _CEILING_SEC)

    assert code == 0
    (line,) = _skipped(capsys.readouterr().out)
    assert line.startswith("[MCP] bad: недоступен — ")
    assert "не найдена" in line
    assert line.endswith(" (пропущен)")


@pytest.mark.skipif(os.name != "posix", reason="процессы POSIX")
@pytest.mark.parametrize("cataloged", [False, True])
async def test_late_server_is_skipped(
    cataloged, host, monkeypatch, capsys, mcp_home, mcp_reaper, mcp_owner
):
    """Сервер не подключился к сроку обнаружения — те же две строки с
    причиной срока, его тулов в наборе первого хода нет, даже при
    каталоге от прошлого прогона; процесс сервера не остаётся."""
    slow = host / "a.json"
    seen = _install_tools_model(monkeypatch)
    servers = {"a": stdio_cfg(slow), "b": stdio_cfg(host / "b.json")}
    _set_args(
        monkeypatch, "-q", "x", "--config", _config(host, servers, discovery=1)
    )
    if cataloged:
        assert await _arun_cli() == 0
        capsys.readouterr()
        assert seen[-1] == sorted([*_LIVE_TOOLS, "mcp_a_echo", "mcp_a_nap"])
    set_behavior(slow, start_delay=6)

    assert await _arun_cli() == 0

    out, err = capsys.readouterr()
    assert _skipped(out) == [
        "[MCP] a: недоступен — таймаут подключения (пропущен)"
    ]
    assert "MCP не подключились: a" in err
    assert seen[-1] == _LIVE_TOOLS
    assert not any(alive(pid) for pid in launches(slow))


# --- Ctrl+C: настоящий сигнал в отдельном процессе -----------------------

_DRIVER = Path(__file__).with_name("cli_sigint_driver.py")


def _drive(scenario: str, workdir: Path) -> tuple[list[str], str]:
    """События прогона (строки stdout драйвера) и его stderr."""
    done = subprocess.run(
        [sys.executable, str(_DRIVER), scenario, str(workdir)],
        capture_output=True,
        text=True,
        timeout=_CEILING_SEC,
        check=False,
    )
    return done.stdout.splitlines(), done.stderr


def _at(lines: list[str], what: str) -> float:
    """Время последнего события ``what`` (секунды от старта драйвера)."""
    return [
        float(t) for t, _, w in (s.partition(" ") for s in lines) if w == what
    ][-1]


@pytest.mark.skipif(os.name != "posix", reason="сигналы POSIX")
def test_press_in_close_after_success_keeps_answer(tmp_path):
    """Нажатие во время закрытия после удачного хода — стоп хода
    (вхолостую): ответ напечатан, код выхода прежний."""
    lines, err = _drive("close_after_success", tmp_path)
    assert "ответ хода" in lines
    assert lines[-1].endswith(" rc 0")
    assert any(line.endswith(" stop_run") for line in lines)
    assert "Task was destroyed" not in err


@pytest.mark.skipif(os.name != "posix", reason="сигналы POSIX")
@pytest.mark.parametrize("scenario", ["second_in_close", "second_in_turn"])
def test_second_press_exits_without_waiting_sessions(scenario, tmp_path):
    """Второе нажатие (во время хода или закрытия) — прерывание: закрытие
    MCP без бюджета, выход не ждёт глухую сессию дольше отсрочки."""
    lines, err = _drive(scenario, tmp_path)
    assert lines[-1].endswith(" KeyboardInterrupt")
    # Первое нажатие — по-прежнему стоп хода.
    assert any(line.endswith(" stop_run") for line in lines)
    assert _at(lines, "KeyboardInterrupt") - _at(lines, "SIGINT") < 4.0
    assert any(line.endswith(" service.aclose 0.00") for line in lines)
    # Прерванное закрытие — и всё же гашение процессов (спека §5.12).
    assert any(line.endswith(" service.kill") for line in lines)
    assert any(line.endswith(" aclose_clients") for line in lines)
    assert "Task was destroyed" not in err
