"""Причина падения stdio MCP-сервера при старте видна на экране ``/mcp``.

Сервер, упавший до ``initialize`` (``ModuleNotFoundError`` в его venv),
раньше выглядел как «нет соединения с ddgs-mcp»: stderr процесса уходил в
``DEVNULL``, и настоящая причина не доходила никуда. Теперь stderr пишется в
лог сервера в каталоге состояния gc, а последняя непустая строка лога
попадает в ``reason`` дискавери.

Каталог состояния — изолированный ``GC_HOME`` набора (плагин gc_testkit).
"""

import stat
import subprocess
import sys

import pytest
from gc_core.tools.connect_mcp import persistent_stdio, transport
from gc_core.tools.connect_mcp.registry import _adiscover_one

_CRASH_LINE = "ModuleNotFoundError: No module named mcp.server.fastmcp"

_CRASH_SCRIPT = (
    "import sys; sys.stderr.write("
    "'ModuleNotFoundError: No module named mcp.server.fastmcp\\n'); "
    "sys.exit(1)"
)

#: Долгоживущий сервер, который до ответа на ``initialize`` пишет в stderr
#: заметно больше ёмкости pipe (64 КиБ). С непрочитанным ``PIPE`` он встал
#: бы на записи навсегда, и ``initialize`` не дождался бы ответа.
_CHATTY_SCRIPT = """
import json, sys
for _ in range(4096):
    sys.stderr.write("x" * 511 + "\\n")
sys.stderr.flush()
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    if msg.get("method") == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {},
                  "serverInfo": {"name": "chatty", "version": "0"}}
    else:
        result = {"tools": [{"name": "ping", "inputSchema": {}}]}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                                 "result": result}) + "\\n")
    sys.stdout.flush()
"""


def _cfg(script: str, *, persistent: bool = False) -> dict:
    cfg = {
        "transport": "stdio",
        "command": sys.executable,
        "args": ["-c", script],
    }
    if persistent:
        cfg["persistent_session"] = True
    return cfg


@pytest.fixture
async def persistent_registry():
    """Долгоживущие сессии теста гасятся в любом исходе."""
    registry = persistent_stdio.PersistentStdioRegistry.instance()
    yield registry
    await registry.aclose_all()


async def test_crash_before_initialize_names_the_real_cause():
    tools, status = await _adiscover_one(
        "ddg", _cfg(_CRASH_SCRIPT), timeout=10
    )

    assert tools == []
    assert status["status"] == "failed"
    reason = status["reason"]
    assert "сервер завершился при старте" in reason, reason
    assert _CRASH_LINE in reason, reason
    log_path = transport._stderr_log_path(_cfg(_CRASH_SCRIPT))
    assert str(log_path) in reason, reason
    assert _CRASH_LINE in log_path.read_text(encoding="utf-8")


async def test_persistent_crash_before_initialize_names_the_real_cause(
    persistent_registry,
):
    cfg = _cfg(_CRASH_SCRIPT, persistent=True)
    tools, status = await _adiscover_one("ddg_persist", cfg, timeout=10)

    assert tools == []
    assert status["status"] == "failed"
    assert "сервер завершился при старте" in status["reason"], status
    assert _CRASH_LINE in status["reason"], status
    assert str(transport._stderr_log_path(cfg)) in status["reason"], status


async def test_silent_crash_keeps_the_old_reason():
    """Пустой stderr — причины нет, сообщение прежнее."""
    _tools, status = await _adiscover_one(
        "mute", _cfg("import sys; sys.exit(1)"), timeout=10
    )

    assert status["status"] == "failed"
    assert "завершился при старте" not in (status["reason"] or "")


async def test_chatty_stderr_does_not_block_a_one_shot_server():
    request = transport.aget_transport(_cfg(_CHATTY_SCRIPT))

    _headers, body = await request("", {}, dict(transport._INIT_PAYLOAD), 10.0)

    assert body.get("result"), body


async def test_chatty_stderr_does_not_block_a_persistent_server(
    persistent_registry,
):
    cfg = _cfg(_CHATTY_SCRIPT, persistent=True)
    tools, status = await _adiscover_one("chatty", cfg, timeout=10)

    assert status["status"] == "connected", status
    assert [t["name"] for t in tools] == ["ping"]
    log_text = transport._stderr_log_path(cfg).read_text(encoding="utf-8")
    assert len(log_text) > 1024 * 1024


_SECRET = "sk-abc123def456ghi789jkl012"
_LEAKY_SCRIPT = (
    "import sys; sys.stderr.write("
    f"'auth failed: Authorization: Bearer {_SECRET}\\n'); sys.exit(1)"
)


async def test_secret_in_stderr_is_redacted_in_the_reason():
    """Строка причины видна на экране ``/mcp`` — секрет из stderr маскируется.

    Серверы печатают в stderr ошибки авторизации вместе с токеном; лог —
    файл владельца, а ``reason`` уходит в UI и в ``print`` дискавери.
    """
    _tools, status = await _adiscover_one(
        "leaky", _cfg(_LEAKY_SCRIPT), timeout=10
    )

    reason = status["reason"]
    assert "сервер завершился при старте" in reason, reason
    assert "auth failed" in reason, reason
    assert _SECRET not in reason, reason
    assert "***REDACTED***" in reason, reason


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-права файла")
async def test_stderr_log_is_private_to_the_owner(tmp_path, monkeypatch):
    """Лог stderr может держать секреты — только владельцу: 0600, каталог 0700."""
    log_path = tmp_path / "mcp" / "server.stderr.log"
    monkeypatch.setattr(transport, "_stderr_log_path", lambda _cfg: log_path)

    with transport._stderr_log(_cfg("pass")) as (_stderr, path):
        assert path == log_path

    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(log_path.parent.stat().st_mode) == 0o700


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-права файла")
async def test_stale_world_readable_log_is_tightened(tmp_path, monkeypatch):
    """Лог, оставшийся с правами 0644, при новом старте становится 0600."""
    log_path = tmp_path / "server.stderr.log"
    log_path.write_text("old\n", encoding="utf-8")
    log_path.chmod(0o644)
    monkeypatch.setattr(transport, "_stderr_log_path", lambda _cfg: log_path)

    with transport._stderr_log(_cfg("pass")):
        pass

    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    assert log_path.read_text(encoding="utf-8") == ""


@pytest.mark.skipif(sys.platform == "win32", reason="симлинки POSIX")
async def test_symlinked_log_is_not_followed(tmp_path, monkeypatch):
    """На месте лога симлинк — не пишем по нему, а уходим в ``DEVNULL``."""
    victim = tmp_path / "victim.txt"
    victim.write_text("keep\n", encoding="utf-8")
    log_path = tmp_path / "server.stderr.log"
    log_path.symlink_to(victim)
    monkeypatch.setattr(transport, "_stderr_log_path", lambda _cfg: log_path)

    with transport._stderr_log(_cfg("pass")) as (stderr, path):
        assert stderr == subprocess.DEVNULL
        assert path is None

    assert victim.read_text(encoding="utf-8") == "keep\n"
