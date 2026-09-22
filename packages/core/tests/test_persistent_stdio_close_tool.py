"""Закрытие persistent-stdio с настроенным close-тулом должно быть быстрым.

Регрессия, которую не видел ни один прежний тест: `_close_prologue_head`
отцеплял читателя ДО того, как close-тул записан и его ответ прочитан.
Первый `_LineReader` при этом жив и стоит в `readline()` на той же трубе — он
и забирал ответ в очередь, которую после `forget` уже никто не читает. А
`_read_matching_on` не находил читателя, заводил ВТОРОЙ на той же трубе и
честно ждал свои 10 секунд.

Цена: каждое закрытие сервера с `persistent_session_close_tool` стоило ровно
10 с, а внешние дети сервера (документированный случай — Chrome под
@playwright/mcp) не гасились: ответ close-тула не был увиден, дальше шёл
SIGTERM/SIGKILL. То есть ломалось ровно то, ради чего close-тул и заводили.
Детерминированно, не гонка.

`test_persistent_stdio_aclose.py` рядом close-тул не настраивает и поломку
не видит — поэтому здесь отдельный файл с сервером, который на close-тул
ОТВЕЧАЕТ.
"""
import json
import sys
import time

import pytest
from gc_core.tools.connect_mcp import persistent_stdio as ps

#: Отвечает на всё мгновенно и по id — как настоящий сервер. Ответ на
#: close-тул помечает, чтобы отличить «прочитали» от «дождались таймаута».
_RESPONSIVE = r'''
import json, sys

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
    except Exception:
        continue
    rid = req.get("id")
    if rid is None:
        continue
    name = ((req.get("params") or {}).get("name")) or ""
    sys.stdout.write(json.dumps({
        "jsonrpc": "2.0", "id": rid,
        "result": {"content": [{"type": "text", "text": f"done:{name}"}]},
    }) + "\n")
    sys.stdout.flush()
'''

#: Заведомо меньше десятисекундного таймаута close-тула и заведомо больше
#: любого честного обмена по трубе на живой машине.
_BUDGET_SEC = 2.0


@pytest.fixture
def session_cfg(tmp_path):
    script = tmp_path / "responsive_stdio_server.py"
    script.write_text(_RESPONSIVE, encoding="utf-8")
    return {
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(script)],
        "persistent_session": True,
        "persistent_session_close_tool": "browser_close_all",
    }


def _started(cfg) -> ps.PersistentStdioSession:
    session = ps.PersistentStdioSession(cfg)
    session.call("tools/call", {"name": "ping", "arguments": {}}, timeout=10.0)
    return session


def test_sync_close_with_close_tool_does_not_wait_out_the_timeout(session_cfg):
    """Синхронное закрытие укладывается в бюджет, а не в таймаут close-тула."""
    session = _started(session_cfg)

    started = time.perf_counter()
    session.close()
    spent = time.perf_counter() - started

    assert spent < _BUDGET_SEC, (
        f"закрытие заняло {spent:.2f}с — ответ close-тула снова уходит "
        f"дублирующему читателю, и мы ждём полный таймаут"
    )


async def test_async_close_with_close_tool_does_not_wait_out_the_timeout(session_cfg):
    """То же для асинхронного входа — его и зовёт `arun` в своём finally."""
    session = _started(session_cfg)

    started = time.perf_counter()
    await session.aclose()
    spent = time.perf_counter() - started

    assert spent < _BUDGET_SEC, f"закрытие заняло {spent:.2f}с"


def test_close_tool_answer_is_actually_read(session_cfg, monkeypatch):
    """Мало быть быстрым — ответ close-тула обязан быть ПРОЧИТАН.

    Без этой проверки быстрым стало бы и закрытие, которое просто перестало
    ждать: сервер бы не успел погасить своих детей, а тест бы молчал.
    """
    session = _started(session_cfg)

    seen = {}
    real_read = ps.PersistentStdioSession._read_matching_on

    def _spy(self, proc, req_id, timeout):
        body = real_read(self, proc, req_id, timeout)
        seen[req_id] = body
        return body

    monkeypatch.setattr(ps.PersistentStdioSession, "_read_matching_on", _spy)

    session.close()

    answered = [
        b for b in seen.values()
        if "browser_close_all" in json.dumps(b, ensure_ascii=False)
    ]
    assert answered, f"ответ close-тула не прочитан, видели: {seen}"


def test_no_second_reader_is_created_during_close(session_cfg, monkeypatch):
    """И причина, а не только симптом: второй читатель на ту же трубу.

    Два `_LineReader` на одном `TextIOWrapper` — это не только потерянный
    ответ, но и две нити, читающие один буфер.
    """
    from gc_core.tools.connect_mcp import transport

    session = _started(session_cfg)

    created = []
    real_reader = transport._LineReader

    class _Counting(real_reader):
        def __init__(self, *a, **kw):
            created.append(1)
            super().__init__(*a, **kw)

    monkeypatch.setattr(transport, "_LineReader", _Counting)

    session.close()

    assert created == [], "во время закрытия завёлся ещё один читатель трубы"
