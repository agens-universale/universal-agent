"""Показ на экране читает один снимок меты за тик опроса.

Снимок живёт только внутри `_poll_active`; вне тика — свежее чтение;
запись показываемых полей внутри тика сбрасывает снимок; запись ядра
в том же тике не теряется при `update_tui_meta`.
"""

import json

import pytest
from gc_testkit.tui_support import tui_settings

_TID = "tickmeta0000000000000000000000001"


def _write_task(tmp_path, tid=_TID, tui=None):
    meta = {"thread_id": tid, "system_id": "cli"}
    if tui is not None:
        meta["tui"] = tui
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


def _app(tmp_path):
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )


class _Runner:
    def __init__(self, running):
        self._running = running

    def is_running(self):
        return self._running


@pytest.mark.asyncio
async def test_poll_tick_reads_display_meta_once(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    _write_task(tmp_path)
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(_TID)
        await pilot.pause()
        reads = []
        original = scr._store.get_task_meta

        def counting(*args, **kwargs):
            reads.append(args)
            return original(*args, **kwargs)

        monkeypatch.setattr(scr._store, "get_task_meta", counting)
        scr._poll_active()
        # Только снимок показа: эпоху хвоста фасад ``Threads`` берёт из
        # курсора (поколение и версия истории), meta для неё не читается.
        assert len(reads) == 1


@pytest.mark.asyncio
async def test_outside_tick_reads_fresh(tmp_path):
    pytest.importorskip("textual")
    _write_task(tmp_path)
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(_TID)
        scr._poll_active()
        scr._store.update_tui_meta(_TID, {"title": "Новое имя"})
        assert scr._session_display_name() == "Новое имя"


@pytest.mark.asyncio
async def test_tick_snapshot_follows_active_task(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    other = "tickmeta0000000000000000000000002"
    _write_task(tmp_path, tui={"title": "Первая"})
    _write_task(tmp_path, tid=other, tui={"title": "Вторая"})
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(_TID)
        await pilot.pause()
        names = []

        def switch_in_tick():
            names.append(scr._session_display_name())
            scr._active_task_id = other
            names.append(scr._session_display_name())

        monkeypatch.setattr(scr, "_poll_tick", switch_in_tick)
        scr._poll_active()
        assert names == ["Первая", "Вторая"]
        assert scr._tick_meta_cache is None


@pytest.mark.asyncio
async def test_falling_edge_status_visible_same_tick(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    _write_task(tmp_path, tui={"status": "running"})
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(_TID)
        await pilot.pause()
        runner = _Runner(running=False)
        runner._last_target = (_TID, "cli")
        pilot.app.active_runner = runner
        pilot.app.last_stop_requested = False
        pilot.app.last_runner_error = None
        scr._was_running = True
        seen = []
        original = scr._update_terminal_title

        def recording():
            seen.append(scr._session_status_word())
            original()

        monkeypatch.setattr(scr, "_update_terminal_title", recording)
        scr._poll_active()
        assert seen
        assert set(seen) == {"completed"}


@pytest.mark.asyncio
async def test_core_write_survives_tui_write_in_tick(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from gc_testkit.develop_files import save_thread_meta

    _write_task(tmp_path)
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(_TID)
        await pilot.pause()
        original = scr._drain_subagent_progress

        def core_then_tui_write():
            scr._session_display_name()  # снимок показа в этом тике
            meta = json.loads(
                (tmp_path / f"meta_cli_{_TID}.json").read_text("utf-8")
            )
            meta["core_key"] = "от ядра"
            save_thread_meta(str(tmp_path), "cli", _TID, meta)
            scr._store.update_tui_meta(_TID, {"title": "имя"})
            original()

        monkeypatch.setattr(
            scr, "_drain_subagent_progress", core_then_tui_write
        )
        scr._poll_active()
        meta = scr._store.get_task_meta(_TID)
        assert meta["core_key"] == "от ядра"
        assert meta["tui"]["title"] == "имя"


@pytest.mark.asyncio
async def test_submit_task_name_visible_same_tick(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.models import TraceEvent

    _write_task(tmp_path, tui={"last_actual": "старое имя"})
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(_TID)
        await pilot.pause()
        ev = TraceEvent(
            no=1,
            event_ts_utc_iso="t",
            kind="agent",
            title="",
            summary="готово",
            full_payload={
                "arguments": json.dumps(
                    {"task": "Новое имя", "task_report": "готово"},
                    ensure_ascii=False,
                )
            },
            call_id="c1",
            tool_name="submit_solution",
        )
        names = []
        original = scr._drain_subagent_progress

        def submit_in_tick():
            names.append(scr._session_display_name())  # снимок тика
            scr._seed_or_update_task_from_submit(ev)
            names.append(scr._session_display_name())
            original()

        monkeypatch.setattr(scr, "_drain_subagent_progress", submit_in_tick)
        scr._poll_active()
        assert names == ["старое имя", "Новое имя"]


@pytest.mark.asyncio
async def test_screen_write_visible_same_tick(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    _write_task(tmp_path, tui={"title": "Старое"})
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(_TID)
        await pilot.pause()
        names = []

        def rename_in_tick():
            names.append(scr._session_display_name())
            scr._write_tui_meta(_TID, {"title": "Новое"})
            names.append(scr._session_display_name())

        monkeypatch.setattr(scr, "_poll_tick", rename_in_tick)
        scr._poll_active()
        assert names == ["Старое", "Новое"]


def test_screen_writes_tui_meta_only_through_helper():
    import inspect

    from gc_tui.screens import main

    source = inspect.getsource(main)
    assert source.count("_store.update_tui_meta(") == 1
