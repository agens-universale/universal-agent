"""force_compact_once: один безусловный проход сжатия (без проверки порога)."""

import importlib
import types

from gc_testkit import run_coro
from gc_testkit.settings import fake_ctx


def _coord(monkeypatch, calls, backend="chat_completions"):
    # Разрешаем модуль в CALL-time из sys.modules: другой тест мог перезагрузить
    # run_summary_coordinator (новый объект модуля). Если брать класс из module-top
    # импорта (старый объект), его force_compact_once читал бы run_summary_compaction
    # из СТАРОГО namespace, а monkeypatch по строке пропатчил бы НОВЫЙ → патч мимо,
    # звался бы реальный компактор. Берём класс и патчим атрибут ОДНОГО объекта.
    rsc = importlib.import_module("gc_core.run_summary_coordinator")
    state = types.SimpleNamespace(backend_api_mode=backend)
    coord = rsc.RunSummaryCoordinator(
        ctx=fake_ctx(),
        state=state,
        thread_session=None,
        summary_trigger_chars=10_000,
        is_cli_mode=True,
        pre_backend_trigger_reason="pre_backend",
        logger=None,
    )

    async def _fake_compaction(**kw):
        calls.append(kw)

    monkeypatch.setattr(rsc, "arun_summary_compaction", _fake_compaction)
    return coord


def test_force_compacts_once_ignoring_threshold(monkeypatch):
    calls = []
    coord = _coord(monkeypatch, calls)
    run_coro(coord.aforce_compact_once())
    assert len(calls) == 1
    assert calls[0]["trigger_reason"] == "manual"
    # порог НЕ проверяется — сжали несмотря на пустую/малую историю
    assert calls[0]["trigger_threshold_chars"] == 10_000


def test_force_noop_in_responses_mode(monkeypatch):
    calls = []
    coord = _coord(monkeypatch, calls, backend="responses")
    run_coro(coord.aforce_compact_once())
    assert calls == []


def test_force_compact_leaves_nudge_counters_to_the_caller(monkeypatch):
    """Сводка не знает про подсказки: счётчики остаются вызывающему."""
    calls = []
    coord = _coord(monkeypatch, calls)
    coord._state.tool_usage_notice_cnt = {"session_memory": 4}
    coord._state.todo_reminder_cnt = 3

    applied = run_coro(coord.aforce_compact_once())

    assert applied is True
    assert coord._state.tool_usage_notice_cnt == {"session_memory": 4}
    assert coord._state.todo_reminder_cnt == 3
