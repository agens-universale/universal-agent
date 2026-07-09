"""DiffScreen + /diff: открытие, approve/undo smoke."""
from __future__ import annotations

from pathlib import Path

import pytest

from config_modules import env_vars
from diff_tui.store import DiffStore
from runtime_notices import clear_runtime_notices


@pytest.fixture
def launch_root(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "src").mkdir()
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    clear_runtime_notices()
    return root


def _cfg(launch_root: Path) -> dict:
    return {
        "launch_root": str(launch_root),
        "model": "M",
        "write_guard": {"enabled": False, "mode": "deny", "extra_paths": []},
        "diff_tui": {
            "cache_max_orig": 500,
            "preview_context_lines": 3,
            "max_diff_chars": 8000,
        },
    }


def _seed_pending(launch_root: Path) -> None:
    target = launch_root / "src" / "foo.py"
    target.write_text("base\n", encoding="utf-8")
    DiffStore(str(launch_root)).register_mutation(str(target))
    target.write_text("changed\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_diff_command_opens_screen(tmp_path, launch_root):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.diff import DiffScreen
    from tui.screens.main import TraceScreen

    cfg = _cfg(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        assert isinstance(pilot.app.screen, TraceScreen)
        pilot.app.screen._handle_command("diff")
        await pilot.pause()
        assert isinstance(pilot.app.screen, DiffScreen)


@pytest.mark.asyncio
async def test_diff_screen_lists_pending_and_approve(tmp_path, launch_root):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.diff import DiffScreen

    _seed_pending(launch_root)
    cfg = _cfg(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        scr = DiffScreen(launch_root=str(launch_root), config=cfg)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert any(e.key == "src/foo.py" for e in scr._entries)
        assert len(scr._entries) == 1
        label = str(scr._row_label(scr._entries[0], selected=True))
        assert "[изменено]" in label
        assert "ожидает" in label
        assert "src/foo.py" in label

        await pilot.press("a")
        await pilot.pause()
        # после approve disk==.orig → pending пуст
        assert scr._entries == []
        assert (launch_root / "src" / "foo.py").read_text(encoding="utf-8") == "changed\n"


@pytest.mark.asyncio
async def test_diff_screen_undo_restores(tmp_path, launch_root):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.diff import DiffScreen

    _seed_pending(launch_root)
    cfg = _cfg(launch_root)
    target = launch_root / "src" / "foo.py"
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        scr = DiffScreen(launch_root=str(launch_root), config=cfg)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        assert target.read_text(encoding="utf-8") == "base\n"


@pytest.mark.asyncio
async def test_diff_screen_enter_opens_viewer(tmp_path, launch_root):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.diff import DiffScreen, DiffViewerScreen
    from textual.widgets import Static

    _seed_pending(launch_root)
    cfg = _cfg(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        await pilot.app.push_screen(
            DiffScreen(launch_root=str(launch_root), config=cfg)
        )
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(pilot.app.screen, DiffViewerScreen)
        footer = str(pilot.app.screen.query_one("#footer", Static).render())
        assert "[a] принять" in footer
        assert "[r] откатить" in footer
        assert "[Ctrl+a] все" in footer


@pytest.mark.asyncio
async def test_diff_viewer_approve_closes_and_clears(tmp_path, launch_root):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.diff import DiffScreen, DiffViewerScreen

    _seed_pending(launch_root)
    cfg = _cfg(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        scr = DiffScreen(launch_root=str(launch_root), config=cfg)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(pilot.app.screen, DiffViewerScreen)
        await pilot.press("a")
        await pilot.pause()
        assert isinstance(pilot.app.screen, DiffScreen)
        assert scr._entries == []
        assert (launch_root / "src" / "foo.py").read_text(encoding="utf-8") == "changed\n"


@pytest.mark.asyncio
async def test_diff_viewer_undo_restores(tmp_path, launch_root):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.diff import DiffScreen, DiffViewerScreen

    _seed_pending(launch_root)
    cfg = _cfg(launch_root)
    target = launch_root / "src" / "foo.py"
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        await pilot.app.push_screen(
            DiffScreen(launch_root=str(launch_root), config=cfg)
        )
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(pilot.app.screen, DiffViewerScreen)
        await pilot.press("r")
        await pilot.pause()
        assert isinstance(pilot.app.screen, DiffScreen)
        assert target.read_text(encoding="utf-8") == "base\n"


@pytest.mark.asyncio
async def test_diff_screen_ru_layout_approve(tmp_path, launch_root):
    """ЙЦУКЕН: ф/к = a/r на тех же физических клавишах."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.diff import DiffScreen

    _seed_pending(launch_root)
    cfg = _cfg(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        scr = DiffScreen(launch_root=str(launch_root), config=cfg)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.press("ф")
        await pilot.pause()
        assert scr._entries == []
        assert (launch_root / "src" / "foo.py").read_text(encoding="utf-8") == "changed\n"


@pytest.mark.asyncio
async def test_diff_viewer_ru_layout_undo(tmp_path, launch_root):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.diff import DiffScreen, DiffViewerScreen

    _seed_pending(launch_root)
    cfg = _cfg(launch_root)
    target = launch_root / "src" / "foo.py"
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        await pilot.app.push_screen(
            DiffScreen(launch_root=str(launch_root), config=cfg)
        )
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(pilot.app.screen, DiffViewerScreen)
        await pilot.press("к")
        await pilot.pause()
        assert isinstance(pilot.app.screen, DiffScreen)
        assert target.read_text(encoding="utf-8") == "base\n"


@pytest.mark.asyncio
async def test_diff_viewer_caps_ф_approves_one_not_all(tmp_path, launch_root):
    """CapsLock/Shift: Ф не должен делать approve all — только текущий файл."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.diff import DiffScreen, DiffViewerScreen
    from diff_tui.store import DiffStore

    _seed_pending(launch_root)
    other = launch_root / "src" / "bar.py"
    other.write_text("base2\n", encoding="utf-8")
    DiffStore(str(launch_root)).register_mutation(str(other))
    other.write_text("changed2\n", encoding="utf-8")

    cfg = _cfg(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        scr = DiffScreen(launch_root=str(launch_root), config=cfg)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert len(scr._entries) == 2
        # выбрать foo (первый)
        scr._selected_idx = next(
            i for i, e in enumerate(scr._entries) if e.key == "src/foo.py"
        )
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(pilot.app.screen, DiffViewerScreen)
        await pilot.press("Ф")
        await pilot.pause()
        assert isinstance(pilot.app.screen, DiffScreen)
        keys = {e.key for e in scr._entries}
        assert "src/foo.py" not in keys
        assert "src/bar.py" in keys


@pytest.mark.asyncio
async def test_diff_screen_ctrl_a_approves_all(tmp_path, launch_root):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.diff import DiffScreen
    from diff_tui.store import DiffStore

    _seed_pending(launch_root)
    other = launch_root / "src" / "bar.py"
    other.write_text("base2\n", encoding="utf-8")
    DiffStore(str(launch_root)).register_mutation(str(other))
    other.write_text("changed2\n", encoding="utf-8")

    cfg = _cfg(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        scr = DiffScreen(launch_root=str(launch_root), config=cfg)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert len(scr._entries) == 2
        await pilot.press("ctrl+a")
        await pilot.pause()
        assert scr._entries == []


@pytest.mark.asyncio
async def test_diff_screen_undo_shows_status(tmp_path, launch_root):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.diff import DiffScreen
    from textual.widgets import Static

    _seed_pending(launch_root)
    cfg = _cfg(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        scr = DiffScreen(launch_root=str(launch_root), config=cfg)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        assert scr._status == "Отменено: src/foo.py"
        status = str(scr.query_one("#diff-status", Static).render())
        assert "Отменено" in status


@pytest.mark.asyncio
async def test_diff_screen_approve_shows_status(tmp_path, launch_root):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.diff import DiffScreen

    _seed_pending(launch_root)
    cfg = _cfg(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        scr = DiffScreen(launch_root=str(launch_root), config=cfg)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        assert scr._status == "Принято: src/foo.py"


@pytest.mark.asyncio
async def test_trace_mounts_pending_cta(tmp_path, launch_root):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.main import TraceScreen
    from tui.widgets.trace_row import TraceRow

    _seed_pending(launch_root)
    cfg = _cfg(launch_root)
    cfg["launch_root"] = str(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        assert isinstance(pilot.app.screen, TraceScreen)
        pilot.app.screen._maybe_mount_diff_pending_cta()
        await pilot.pause()
        found = any(
            "Изменено" in str(row._content) and "/diff" in str(row._content)
            for row in pilot.app.screen.query(TraceRow)
        )
        assert found, "expected pending CTA in trace"


@pytest.mark.asyncio
async def test_flush_cta_survives_after_clear_trace(tmp_path, launch_root):
    """CTA после falling-edge должна пережить wipe трейса (compaction)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.main import TraceScreen
    from tui.widgets.trace_row import TraceRow

    _seed_pending(launch_root)
    cfg = _cfg(launch_root)
    cfg["launch_root"] = str(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        scr = pilot.app.screen
        assert isinstance(scr, TraceScreen)
        scr._diff_pending_cta_pending = True
        # имитация: смонтировали слишком рано, потом wipe
        scr._maybe_mount_diff_pending_cta()
        await pilot.pause()
        scr._clear_trace()
        await pilot.pause()
        # флаг уже сброшен первым mount — выставляем снова как после falling-edge
        # до flush после rebuild
        scr._diff_pending_cta_pending = True
        scr._flush_diff_pending_cta()
        await pilot.pause()
        found = any(
            "Изменено" in str(row._content) and "/diff" in str(row._content)
            for row in scr.query(TraceRow)
        )
        assert found


@pytest.mark.asyncio
async def test_diff_viewer_undo_created_shows_noop(tmp_path, launch_root):
    """Созданный файл: undo no-op → явный статус, файл остаётся в списке."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.diff import DiffScreen, DiffViewerScreen
    from diff_tui.store import DiffStore

    created = launch_root / "diff-test.txt"
    DiffStore(str(launch_root)).register_mutation(str(created))
    created.write_text("new\n", encoding="utf-8")

    cfg = _cfg(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        scr = DiffScreen(launch_root=str(launch_root), config=cfg)
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert any(e.key == "diff-test.txt" for e in scr._entries)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(pilot.app.screen, DiffViewerScreen)
        await pilot.press("r")
        await pilot.pause()
        assert isinstance(pilot.app.screen, DiffScreen)
        assert any(e.key == "diff-test.txt" for e in scr._entries)
        assert "diff-test.txt" in scr._status
        assert "удалите файл вручную" in scr._status
        assert created.is_file()


@pytest.mark.asyncio
async def test_trace_ctrl_a_approves_all(tmp_path, launch_root):
    """С экрана сессии Ctrl+a принимает все pending."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.main import TraceScreen
    from tui.widgets.trace_row import TraceRow
    from diff_tui.store import DiffStore

    _seed_pending(launch_root)
    other = launch_root / "src" / "bar.py"
    other.write_text("base2\n", encoding="utf-8")
    DiffStore(str(launch_root)).register_mutation(str(other))
    other.write_text("changed2\n", encoding="utf-8")

    cfg = _cfg(launch_root)
    cfg["launch_root"] = str(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        assert isinstance(pilot.app.screen, TraceScreen)
        await pilot.press("ctrl+a")
        await pilot.pause()
        found = any(
            "Принято" in str(row._content) for row in pilot.app.screen.query(TraceRow)
        )
        assert found
        from tui.screens.diff import _service_from_config

        svc = _service_from_config(str(launch_root), cfg)
        assert svc.list_entries(pending_only=True) == []


@pytest.mark.asyncio
async def test_diff_screen_esc_returns(tmp_path, launch_root):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.diff import DiffScreen
    from tui.screens.main import TraceScreen

    cfg = _cfg(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        await pilot.app.push_screen(
            DiffScreen(launch_root=str(launch_root), config=cfg)
        )
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(pilot.app.screen, TraceScreen)
