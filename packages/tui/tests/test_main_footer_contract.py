"""Контракт главного футера (спека P5 §1.3–1.4).

Пересборка — только при изменении входных данных: тик опроса зовёт
`_refresh_footer` каждые 250 мс, и без изменений лесенка правой колонки не
строится, а уведомления за сборку собираются один раз на обе колонки.

Страж вида на минимальной ширине: тексты слота — одна строка или честное
многоточие; пояснение режима встаёт в две строки левой части рядом с самой
узкой правой; строка 1 не теряет хвост команд.
"""

import json

import pytest
from footer_text import footer_rows, slot_texts
from gc_core.runtime_notices import clear_runtime_notices, push_runtime_notice
from gc_core.startup_notices import Notice
from gc_testkit.tui_support import tui_settings
from gc_tui.services.footer_slot import MAIN_FOOTER_MIN_COLS, TRUNCATABLE
from gc_tui.services.mode_pill import MODE_HINTS, mode_hint_rows
from rich.cells import cell_len

pytest.importorskip("textual")


class _RunningRunner:
    def is_running(self) -> bool:
        return True

    def stop(self) -> None:
        pass


def _count_calls(monkeypatch, owner, name: str) -> list[int]:
    """Обернуть `owner.name` счётчиком; список из одного числа — вызовы."""
    calls = [0]
    original = getattr(owner, name)

    def counted(*args, **kwargs):
        calls[0] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, counted)
    return calls


def _write_meta(tmp_path, tid, chars):
    meta = {
        "thread_id": tid,
        "system_id": "cli",
        "runtime": {"live_history_chars": chars, "live_msg_count": 1},
    }
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


def _meter_settings():
    return tui_settings(
        {
            "agent.model": "M",
            "backend.api_mode": "chat_completions",
            "summary": {
                "chat_completions_summary_trigger_chars": 1000,
                "chat_completions_summary_turn_end_trigger_chars": 1,
            },
        }
    )


def _shown(scr, selector: str) -> str:
    """Что терминал реально показывает в строке виджета (после обрезки)."""
    widget = scr.query_one(selector)
    strips = scr._compositor.render_strips()
    row = "".join(seg.text for seg in strips[widget.region.y])
    return row[widget.region.x : widget.region.right].rstrip()


@pytest.mark.asyncio
async def test_ticks_without_changes_build_once(tmp_path, monkeypatch):
    from gc_tui.app import TuiApp

    clear_runtime_notices()
    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        builds = _count_calls(monkeypatch, scr, "_compose_footer_right")
        push_runtime_notice(Notice("warning", "w1", "тест"))
        for _ in range(10):
            scr._poll_active()
        assert builds[0] == 1
    clear_runtime_notices()


@pytest.mark.asyncio
async def test_notices_collected_once_per_build(tmp_path, monkeypatch):
    from gc_tui.app import TuiApp

    clear_runtime_notices()
    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        builds = _count_calls(monkeypatch, scr, "_compose_footer_right")
        collects = _count_calls(monkeypatch, app, "collect_footer_notices")
        push_runtime_notice(Notice("warning", "w1", "тест"))
        scr._refresh_footer()
        assert (builds[0], collects[0]) == (1, 1)
    clear_runtime_notices()


@pytest.mark.asyncio
async def test_notices_event_rebuilds_right_column(tmp_path, monkeypatch):
    """Событийное обновление бейджа идёт через ту же сборку: бейдж влияет
    и на правую колонку (её лесенка резервирует его ширину)."""
    from gc_tui.app import TuiApp

    clear_runtime_notices()
    app = TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        builds = _count_calls(monkeypatch, scr, "_compose_footer_right")
        push_runtime_notice(Notice("warning", "w1", "тест"))
        scr._refresh_notices_footer()
        assert builds[0] == 1
    clear_runtime_notices()


@pytest.mark.asyncio
async def test_context_meter_change_rebuilds(tmp_path, monkeypatch):
    from gc_tui.app import TuiApp

    tid = "ctxm0000000000000000000000000009b"
    _write_meta(tmp_path, tid, 500)
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=_meter_settings()
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        scr._refresh_footer()
        builds = _count_calls(monkeypatch, scr, "_compose_footer_right")
        scr._refresh_footer()
        assert builds[0] == 0
        _write_meta(tmp_path, tid, 800)
        scr._refresh_footer()
        assert builds[0] == 1
        assert "80%" in str(scr.query_one("#footer-right").render())


def _slot_samples() -> list[tuple[str, bool]]:
    """(текст для показа, можно ли обрезать) по всему реестру слота."""
    return [
        (
            text.format(n=4, total=12, name="giga-max"),
            text in TRUNCATABLE,
        )
        for text in slot_texts()
    ]


@pytest.mark.asyncio
async def test_slot_texts_one_row_or_ellipsis_at_min_width():
    from gc_tui.app import TuiApp

    clear_runtime_notices()
    app = TuiApp(settings=tui_settings({"agent.model": "M"}))
    size = (MAIN_FOOTER_MIN_COLS, 24)
    async with app.run_test(size=size) as pilot:
        scr = pilot.app.screen
        for text, truncatable in _slot_samples():
            scr._show_footer_hint(text, seconds=60)
            await pilot.pause()
            assert scr.query_one("#footer").region.height == 2
            shown = _shown(scr, "#footer-slot")
            if truncatable and shown != text:
                assert shown.endswith("…"), shown
                assert text.startswith(shown[:-1]), (shown, text)
            else:
                assert shown == text


def _narrowest_right(scr):
    """Пол лесенки правой колонки: точка режима и плашка контекста — их
    лесенка не снимает никогда."""
    from gc_tui.services.context_meter import render_context_meter

    return scr._footer_head(
        with_profile=False,
        with_model_name=False,
        with_level_block=False,
        meter=render_context_meter(999, 1000),
    )[0]


# Пояснение edit (75 колонок) не встаёт в 2 × 34 рядом с полом правой
# (точка + плашка контекста): вторая строка — 42 колонки. Принятое
# исключение: на самом узком окне вторая строка обрезается многоточием.
# strict — если текст станет короче, тест обязан снять пометку.
_EDIT_HINT_TOO_LONG = pytest.mark.xfail(
    strict=True, reason="MODE_HINTS['edit'] длиннее двух строк на 64 кол."
)


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param(m, marks=_EDIT_HINT_TOO_LONG) if m == "edit" else m
        for m in sorted(MODE_HINTS)
    ],
)
@pytest.mark.asyncio
async def test_mode_hint_fits_two_rows_beside_narrowest_right(
    mode, monkeypatch
):
    """Реальная раскладка: правая колонка на полу лесенки, ширину строк
    левой даёт layout, пояснение показано так же, как после Shift+Tab."""
    from gc_tui.app import TuiApp
    from gc_tui.services.mode_pill import render_mode_pill

    app = TuiApp(settings=_meter_settings())
    async with app.run_test(size=(MAIN_FOOTER_MIN_COLS, 24)) as pilot:
        scr = pilot.app.screen
        floor = _narrowest_right(scr)
        monkeypatch.setattr(scr, "_compose_footer_right", lambda *_: floor)
        scr._footer_key = None
        scr._refresh_footer()
        await pilot.pause()
        scr.on_permission_mode_changed(mode)
        await pilot.pause()
        first, second = (
            _shown(scr, "#footer-mode"),
            _shown(scr, "#footer-slot"),
        )
    full = f"{render_mode_pill(mode, hint=False).plain} — {MODE_HINTS[mode]}"
    assert f"{first} {second}".split() == full.split(), (first, second)


def _fault_app(tmp_path):
    from gc_tui.app import TuiApp

    tid = "ctxm0000000000000000000000000009d"
    _write_meta(tmp_path, tid, 500)
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=_meter_settings()
    )
    return app, tid


def _boom(*_args, **_kwargs):
    raise RuntimeError("сбой данных футера")


def _footer_errors(caplog) -> int:
    return sum(
        1
        for r in caplog.records
        if r.name == "gc_tui.screens.main" and "футер" in r.getMessage()
    )


@pytest.mark.asyncio
async def test_meter_fault_keeps_left_alive_and_logs_once(
    tmp_path, monkeypatch, caplog
):
    app, tid = _fault_app(tmp_path)
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        caplog.clear()
        with monkeypatch.context() as patch:
            patch.setattr(scr, "_context_meter", _boom)
            scr._show_footer_hint("ПОДСКАЗКА", seconds=60)
            for _ in range(3):
                scr._refresh_footer()
            assert footer_rows(scr)[1] == "ПОДСКАЗКА"
            assert "50%" not in str(scr.query_one("#footer-right").render())
            assert _footer_errors(caplog) == 1
        scr._refresh_footer()
        assert "50%" in str(scr.query_one("#footer-right").render())


@pytest.mark.asyncio
async def test_right_fault_keeps_left_alive_and_retries(
    tmp_path, monkeypatch, caplog
):
    app, tid = _fault_app(tmp_path)
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        scr._refresh_footer()
        caplog.clear()
        with monkeypatch.context() as patch:
            patch.setattr(scr, "_compose_footer_right", _boom)
            scr._show_footer_hint("ПОДСКАЗКА", seconds=60)
            for _ in range(3):
                scr._refresh_footer()
            assert footer_rows(scr)[1] == "ПОДСКАЗКА"
            assert _footer_errors(caplog) == 1
        builds = _count_calls(monkeypatch, scr, "_compose_footer_right")
        scr._refresh_footer()
        assert builds[0] == 1
        scr._refresh_footer()
        assert builds[0] == 1


@pytest.mark.parametrize(
    ("ask_human", "tail"),
    [(False, "Ctrl+C/stop"), (True, "Esc/stop  Enter/ответить")],
)
@pytest.mark.asyncio
async def test_mode_row_keeps_tail_at_min_width(tmp_path, ask_human, tail):
    from gc_tui.app import TuiApp
    from gc_tui.state import AskHumanState

    tid = "ctxm0000000000000000000000000009c"
    _write_meta(tmp_path, tid, 990)
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=_meter_settings()
    )
    async with app.run_test(size=(MAIN_FOOTER_MIN_COLS, 24)) as pilot:
        pilot.app._update_available = True
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        pilot.app.tui_state.active_task_id = tid
        pilot.app.active_runner = _RunningRunner()
        if ask_human:
            pilot.app.tui_state.ask_human_state = AskHumanState(
                call_id="c1", question="Q?"
            )
        await pilot.pause(0.4)
        assert _shown(scr, "#footer-mode").endswith(tail)
        assert footer_rows(scr)[1].startswith("ℹ 1")


def test_no_text_promises_enter_toggle():
    texts = [*slot_texts(), *MODE_HINTS.values()]
    assert [t for t in texts if "Enter — переключить" in t] == []
