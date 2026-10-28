"""Конец прогона треда, который сейчас не на экране.

Прогон B кончился, пока человек смотрит A: статус и сводка правок
записываются в B (исход, длительность и план — в его записи итога, её
пишет ядро); индикатор, баннер плана и сводка A не меняются. Возврат на B
показывает его исход."""

from __future__ import annotations

import json

import pytest
from gc_core.agent_edits.journal import EditRecord, EditsJournal
from gc_core.persistence.turn_outcome import TurnOutcome
from gc_core.thread_store import edits_journal_path
from gc_testkit.develop_files import save_thread_meta
from gc_testkit.tui_support import tui_settings
from gc_tui.services.turn_outcome import DONE_LABEL_PLAN

_A = "aaaa0000000000000000000000000001a"
_B = "bbbb0000000000000000000000000002b"


def _write_a_with_plan(tmp_path):
    """A: прошлый ход «✓ Выполнил за 5с» и предложенный план — баннер
    плана поднялся бы на A, если бы конец прогона B его звал."""
    from gc_core.execution.state import SharedStateData
    from gc_core.persistence.schema import serialize_shared_block
    from gc_core.plan_store import new_plan_block

    shared = SharedStateData(plan=new_plan_block("1. шаг A"))
    save_thread_meta(
        str(tmp_path),
        "cli",
        _A,
        {
            "shared": serialize_shared_block(shared),
            "tui": {"status": "completed", "last_indicator": {"edits": None}},
        },
    )
    _turn(tmp_path, _A, reason=None, duration=5.0)


def _turn(tmp_path, tid, *, reason, duration):
    """Закрытый ход треда: реплика и запись итога ядра за ней."""
    append_new_thread_messages(
        str(tmp_path),
        "cli",
        tid,
        [{"id": f"u_{tid}", "role": "user", "content": "q"}],
        set(),
    )
    outcome = TurnOutcome(
        status="done",
        reason=reason,
        opened_by=f"u_{tid}",
        started_at="2026-10-22T11:32:00Z",
        ended_at="",
        duration_sec=duration,
        error=None,
    )
    append_turn_outcome_record(str(tmp_path), "cli", tid, outcome, seq=1)


def _write_b(tmp_path):
    meta = {"thread_id": _B, "system_id": "cli", "tui": {}}
    (tmp_path / f"meta_cli_{_B}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    (tmp_path / f"data_cli_{_B}.jsonl").write_text("", encoding="utf-8")


def _b_creates_file(tmp_path):
    """Ход B создал файл из двух строк: сводка «1 файл · +2 −0»."""
    root = tmp_path / "proj"
    root.mkdir(exist_ok=True)
    (root / "b.py").write_text("one\ntwo\n", encoding="utf-8", newline="\n")
    EditsJournal(edits_journal_path(str(tmp_path), "cli", _B)).append(
        EditRecord(
            path="b.py",
            turn_id="TB",
            ts=1.0,
            call_id="cb",
            op="create",
            hunks="@@ -0,0 +1,2 @@\n+one\n+two\n",
            add=0,
            del_=0,
            before_sha256="",
            after_sha256="y",
            flags=[],
        )
    )


def _meta(tmp_path, tid):
    path = tmp_path / f"meta_cli_{tid}.json"
    return json.loads(path.read_text(encoding="utf-8"))["tui"]


def _fresh(scr):
    """Свежая строка итога последнего хода открытого треда."""
    from gc_tui.widgets.turn_line import TurnLine

    (line,) = scr.query(TurnLine).filter(".-latest")
    return line


class _Runner:
    """Прогон треда B."""

    _thread_kind = "run"

    def __init__(self):
        self._last_target = (_B, "cli")
        self._running = True

    def is_running(self):
        return self._running

    def stop(self):
        pass


@pytest.mark.asyncio
async def test_plan_end_of_other_thread_stays_with_it(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    _write_a_with_plan(tmp_path)
    _write_b(tmp_path)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "launch_root": str(tmp_path / "proj")}
        ),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(_B, is_resume=True)
        runner = _Runner()
        pilot.app.active_runner = runner
        scr._poll_active()  # тик застал прогон B: граница журнала правок
        _b_creates_file(tmp_path)
        _turn(tmp_path, _B, reason="plan_proposed", duration=42.0)
        # B кончился планом между тиками, и человек сразу открыл A: идущего
        # хода при переходе нет (решение 25), исход B тик ещё не записал.
        runner._running = False
        pilot.app.last_terminal_outcome = "plan_proposed"
        scr.attach_active_task(_A, is_resume=True)
        await pilot.pause()
        a_before = _fresh(scr).render().plain
        assert a_before.startswith("✓ Выполнил за 5с")
        scr._poll_active()
        await pilot.pause()
        await pilot.pause()
        assert _fresh(scr).render().plain == a_before
        assert "изменено" not in _fresh(scr).render().plain
        assert scr._plan_banner is None
        assert _meta(tmp_path, _A)["last_indicator"] == {"edits": None}
        b = _meta(tmp_path, _B)
        assert b["status"] == "completed"
        assert b["last_indicator"] == {"edits": [1, 2, 0]}
        scr.attach_active_task(_B, is_resume=True)
        await pilot.pause()
        plain = _fresh(scr).render().plain
        assert plain.startswith(f"✓ {DONE_LABEL_PLAN} за 42с")
        assert plain.endswith("изменено 1 файл · +2 −0")
