"""Строки итога хода одним кадром: два осевших хода и индикатор последнего.

Тред пишется настоящими функциями `gc_core.thread_store`: три хода, каждый
закрыт записью итога. Первые два (`✓ Выполнил за 1м 12с`, `■ Отменено`)
оседают в ленте тусклыми строками без сводки правок; последний
(`✕ Ошибка`) — строка индикатора с причиной и сводкой правок справа.

Время закреплено с двух сторон: концы ходов заданы на местных часах
(кадр не зависит от часового пояса машины), «сейчас» — подмена
`relative_time._now_local` в тот же день.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from gc_testkit.tui_support import SNAPSHOT_NOW, tui_settings

pytest.importorskip("textual")
pytest.importorskip("pytest_textual_snapshot")

from gc_core.persistence.turn_outcome import USER_STOP, TurnOutcome
from gc_core.thread_store import (
    append_new_thread_messages,
    append_turn_outcome_record,
)

_SYS = "cli"
_TID = "7f3a9b1c"
# День — в поясе эталонов (`pinned_snapshot_clock`), а не машины: модуль
# импортируется до закрепления часов.
_DAY = datetime(2026, 10, 22, tzinfo=SNAPSHOT_NOW.tzinfo)


def _utc_iso(moment: datetime) -> str:
    """Момент строкой UTC, как пишет ядро."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _outcome(opened_by, end, duration, status="done", **kw) -> TurnOutcome:
    ended = _DAY.replace(hour=end[0], minute=end[1])
    started = ended - timedelta(seconds=duration)
    return TurnOutcome(
        status=status,
        reason=kw.get("reason"),
        opened_by=opened_by,
        started_at=_utc_iso(started),
        ended_at=_utc_iso(ended),
        duration_sec=float(duration),
        error=kw.get("error"),
    )


def _turn(tmp_path, seq, n, question, answer, outcome) -> int:
    append_new_thread_messages(
        str(tmp_path),
        _SYS,
        _TID,
        [
            {"id": f"u{n}", "role": "user", "content": question},
            {"id": f"a{n}", "role": "assistant", "content": answer},
        ],
        set(),
        start_seq=seq,
    )
    seq += 2
    append_turn_outcome_record(str(tmp_path), _SYS, _TID, outcome, seq=seq)
    return seq


def _write_thread(tmp_path) -> None:
    meta = {
        "thread_id": _TID,
        "system_id": _SYS,
        "tui": {"status": "error", "last_indicator": {"edits": [2, 14, 3]}},
    }
    (tmp_path / f"meta_{_SYS}_{_TID}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    seq = _turn(
        tmp_path,
        0,
        1,
        "подними таймаут клиента до 30 секунд",
        "Готово: таймаут 30 с.",
        _outcome("u1", (14, 32), 72),
    )
    seq = _turn(
        tmp_path,
        seq,
        2,
        "прогони все тесты",
        "Запускаю тесты.",
        _outcome("u2", (14, 41), 20, "stopped", reason=USER_STOP),
    )
    _turn(
        tmp_path,
        seq,
        3,
        "обнови документацию",
        "Читаю документацию.",
        _outcome(
            "u3",
            (14, 50),
            9,
            "error",
            error={
                "type": "ReadTimeout",
                "message": "ReadTimeout: сервер модели не ответил за 60 с",
            },
        ),
    )


def test_turn_lines_snapshot(snap_compare, tmp_path, monkeypatch):
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import TraceScreen
    from gc_tui.services import relative_time

    monkeypatch.setattr(
        TraceScreen, "_current_cwd", lambda self: "/work/demo-project"
    )
    now = _DAY.replace(hour=14, minute=55)
    monkeypatch.setattr(relative_time, "_now_local", lambda: now)
    _write_thread(tmp_path)

    app = TuiApp(
        threads_dir=str(tmp_path),
        system=_SYS,
        settings=tui_settings(
            {
                "agent.model": "CodeAgent",
                "backend.api_mode": "chat_completions",
            }
        ),
    )

    async def setup(pilot):
        await pilot.pause()
        pilot.app.screen.attach_active_task(_TID, is_resume=True)
        # Мигание курсора поля — таймер 0.5 с, фаза в кадре от нагрузки;
        # без мигания курсор виден всегда (приём `test_snapshot_forms`).
        pilot.app.screen.query_one("#cmdinput").cursor_blink = False
        await pilot.pause(0.5)

    assert snap_compare(app, terminal_size=(100, 30), run_before=setup)
