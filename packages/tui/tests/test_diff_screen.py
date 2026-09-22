"""DiffScreen + /diff: открытие, approve/undo smoke."""
from __future__ import annotations

from pathlib import Path

import pytest
from gc_core.config_modules import env_vars
from gc_core.runtime_notices import clear_runtime_notices
from gc_tui.diff_tui.store import DiffStore


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


def _seed_pending(
    launch_root: Path,
    threads_dir: str,
    *,
    system: str = "cli",
    thread_id: str = "t1",
    key: str = "src/foo.py",
) -> None:
    from gc_tui.diff_tui.touched import TouchedJournal

    target = launch_root / key
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("base\n", encoding="utf-8")
    DiffStore(str(launch_root)).register_mutation(str(target))
    target.write_text("changed\n", encoding="utf-8")
    TouchedJournal(threads_dir, system, thread_id).add(key)


def _diff_kwargs(tmp_path, *, tid: str = "t1") -> dict:
    threads = tmp_path / "threads"
    threads.mkdir(exist_ok=True)
    return {
        "threads_dir": str(threads),
        "system_id": "cli",
        "thread_id": tid,
    }


@pytest.mark.asyncio
async def test_diff_command_opens_screen(tmp_path, launch_root):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import DiffScreen
    from gc_tui.screens.main import TraceScreen

    cfg = _cfg(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        assert isinstance(pilot.app.screen, TraceScreen)
        await pilot.app.screen._handle_command("diff")
        await pilot.pause()
        assert isinstance(pilot.app.screen, DiffScreen)


@pytest.mark.asyncio
async def test_diff_screen_lists_pending_and_approve(tmp_path, launch_root):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import DiffScreen

    _seed_pending(launch_root, str(tmp_path / "threads"))
    cfg = _cfg(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        scr = DiffScreen(launch_root=str(launch_root), config=cfg, **_diff_kwargs(tmp_path))
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
async def test_diff_screen_hybrid_shows_i_marker(tmp_path, launch_root):
    """В git-mode cache-backed (ignored) помечается маркером ``i``."""
    pytest.importorskip("textual")
    import subprocess

    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import DiffScreen

    def _has_git() -> bool:
        try:
            subprocess.run(["git", "--version"], capture_output=True, check=True)
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    if not _has_git():
        pytest.skip("git недоступен")

    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=launch_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "t@t"],
        cwd=launch_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "t"],
        cwd=launch_root,
        check=True,
        capture_output=True,
    )
    (launch_root / ".gitignore").write_text("src/local.md\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", ".gitignore"],
        cwd=launch_root,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=launch_root,
        check=True,
        capture_output=True,
    )

    target = launch_root / "src" / "local.md"
    target.write_text("v1\n", encoding="utf-8")
    DiffStore(str(launch_root)).register_mutation(str(target))
    target.write_text("v2\n", encoding="utf-8")
    from gc_tui.diff_tui.touched import TouchedJournal
    TouchedJournal(str(tmp_path / "threads"), "cli", "t1").add("src/local.md")

    cfg = _cfg(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        scr = DiffScreen(launch_root=str(launch_root), config=cfg, **_diff_kwargs(tmp_path))
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert any(e.key == "src/local.md" and e.backend == "cache" for e in scr._entries)
        label = str(scr._row_label(scr._entries[0], selected=True))
        assert " i" in label


@pytest.mark.asyncio
async def test_diff_screen_undo_restores(tmp_path, launch_root):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import DiffScreen

    _seed_pending(launch_root, str(tmp_path / "threads"))
    cfg = _cfg(launch_root)
    target = launch_root / "src" / "foo.py"
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        scr = DiffScreen(launch_root=str(launch_root), config=cfg, **_diff_kwargs(tmp_path))
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        assert target.read_text(encoding="utf-8") == "base\n"


@pytest.mark.asyncio
async def test_diff_screen_enter_opens_viewer(tmp_path, launch_root):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import DiffScreen, DiffViewerScreen
    from textual.widgets import Static

    _seed_pending(launch_root, str(tmp_path / "threads"))
    cfg = _cfg(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        await pilot.app.push_screen(
            DiffScreen(launch_root=str(launch_root), config=cfg, **_diff_kwargs(tmp_path))
        )
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(pilot.app.screen, DiffViewerScreen)
        footer = str(pilot.app.screen.query_one("#footer", Static).render())
        assert "[a] принять" in footer
        assert "[r] откатить" in footer
        assert "[Ctrl+a] принять все" in footer
        assert pilot.app.screen.clipboard_source_cite() == ("diff", "src/foo.py")



@pytest.mark.asyncio
async def test_diff_viewer_approve_closes_and_clears(tmp_path, launch_root):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import DiffScreen, DiffViewerScreen

    _seed_pending(launch_root, str(tmp_path / "threads"))
    cfg = _cfg(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        scr = DiffScreen(launch_root=str(launch_root), config=cfg, **_diff_kwargs(tmp_path))
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
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import DiffScreen, DiffViewerScreen

    _seed_pending(launch_root, str(tmp_path / "threads"))
    cfg = _cfg(launch_root)
    target = launch_root / "src" / "foo.py"
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        await pilot.app.push_screen(
            DiffScreen(launch_root=str(launch_root), config=cfg, **_diff_kwargs(tmp_path))
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
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import DiffScreen

    _seed_pending(launch_root, str(tmp_path / "threads"))
    cfg = _cfg(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        scr = DiffScreen(launch_root=str(launch_root), config=cfg, **_diff_kwargs(tmp_path))
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.press("ф")
        await pilot.pause()
        assert scr._entries == []
        assert (launch_root / "src" / "foo.py").read_text(encoding="utf-8") == "changed\n"


@pytest.mark.asyncio
async def test_diff_viewer_ru_layout_undo(tmp_path, launch_root):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import DiffScreen, DiffViewerScreen

    _seed_pending(launch_root, str(tmp_path / "threads"))
    cfg = _cfg(launch_root)
    target = launch_root / "src" / "foo.py"
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        await pilot.app.push_screen(
            DiffScreen(launch_root=str(launch_root), config=cfg, **_diff_kwargs(tmp_path))
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
    from gc_tui.app import TuiApp
    from gc_tui.diff_tui.store import DiffStore
    from gc_tui.screens.diff import DiffScreen, DiffViewerScreen

    _seed_pending(launch_root, str(tmp_path / "threads"))
    other = launch_root / "src" / "bar.py"
    other.write_text("base2\n", encoding="utf-8")
    DiffStore(str(launch_root)).register_mutation(str(other))
    other.write_text("changed2\n", encoding="utf-8")
    from gc_tui.diff_tui.touched import TouchedJournal
    TouchedJournal(str(tmp_path / "threads"), "cli", "t1").add("src/bar.py")

    cfg = _cfg(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        scr = DiffScreen(launch_root=str(launch_root), config=cfg, **_diff_kwargs(tmp_path))
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
    from gc_tui.app import TuiApp
    from gc_tui.diff_tui.store import DiffStore
    from gc_tui.screens.diff import DiffScreen

    _seed_pending(launch_root, str(tmp_path / "threads"))
    other = launch_root / "src" / "bar.py"
    other.write_text("base2\n", encoding="utf-8")
    DiffStore(str(launch_root)).register_mutation(str(other))
    other.write_text("changed2\n", encoding="utf-8")
    from gc_tui.diff_tui.touched import TouchedJournal
    TouchedJournal(str(tmp_path / "threads"), "cli", "t1").add("src/bar.py")

    cfg = _cfg(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        scr = DiffScreen(launch_root=str(launch_root), config=cfg, **_diff_kwargs(tmp_path))
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert len(scr._entries) == 2
        await pilot.press("ctrl+a")
        await pilot.pause()
        assert scr._entries == []


@pytest.mark.asyncio
async def test_diff_screen_undo_shows_status(tmp_path, launch_root):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import DiffScreen
    from textual.widgets import Static

    _seed_pending(launch_root, str(tmp_path / "threads"))
    cfg = _cfg(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        scr = DiffScreen(launch_root=str(launch_root), config=cfg, **_diff_kwargs(tmp_path))
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
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import DiffScreen
    from gc_tui.widgets.trace_row import TraceRow

    _seed_pending(launch_root, str(tmp_path / "threads"))
    cfg = _cfg(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        scr = DiffScreen(
            launch_root=str(launch_root),
            config=cfg,
            on_ui_ack=pilot.app.screen._on_diff_ui_ack,
            **_diff_kwargs(tmp_path),
        )
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        assert scr._status == "Принято: src/foo.py"
        # Esc назад — ack уже в трейсе под DiffScreen stack; после pop виден на TraceScreen
        await pilot.press("escape")
        await pilot.pause()
        found = any(
            "Принято: src/foo.py" in str(row._content)
            for row in pilot.app.screen.query(TraceRow)
        )
        assert found


@pytest.mark.asyncio
async def test_diff_screen_undo_shows_trace_ack(tmp_path, launch_root):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import DiffScreen
    from gc_tui.widgets.trace_row import TraceRow

    _seed_pending(launch_root, str(tmp_path / "threads"))
    cfg = _cfg(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        scr = DiffScreen(
            launch_root=str(launch_root),
            config=cfg,
            on_ui_ack=pilot.app.screen._on_diff_ui_ack,
            **_diff_kwargs(tmp_path),
        )
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        assert scr._status == "Отменено: src/foo.py"
        await pilot.press("escape")
        await pilot.pause()
        found = any(
            "Отменено: src/foo.py" in str(row._content)
            for row in pilot.app.screen.query(TraceRow)
        )
        assert found


@pytest.mark.asyncio
async def test_trace_mounts_pending_cta(tmp_path, launch_root):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import TraceScreen
    from gc_tui.widgets.trace_row import TraceRow

    _seed_pending(launch_root, str(tmp_path / "threads"))
    cfg = _cfg(launch_root)
    cfg["launch_root"] = str(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        assert isinstance(pilot.app.screen, TraceScreen)
        pilot.app.screen.attach_active_task("t1")
        pilot.app.screen._maybe_mount_diff_pending_cta()
        await pilot.pause()
        found = any(
            "F3/diff" in str(row._content)
            for row in pilot.app.screen.query(TraceRow)
        )
        assert found, "expected pending CTA in trace"


@pytest.mark.asyncio
async def test_flush_cta_survives_after_clear_trace(tmp_path, launch_root):
    """CTA после falling-edge должна пережить wipe трейса (compaction)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import TraceScreen
    from gc_tui.widgets.trace_row import TraceRow

    _seed_pending(launch_root, str(tmp_path / "threads"))
    cfg = _cfg(launch_root)
    cfg["launch_root"] = str(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        scr = pilot.app.screen
        assert isinstance(scr, TraceScreen)
        scr.attach_active_task("t1")
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
            "F3/diff" in str(row._content)
            for row in scr.query(TraceRow)
        )
        assert found


@pytest.mark.asyncio
async def test_cta_shows_when_pending(tmp_path, launch_root):
    """При pending>0 после turn монтируется ровно одна CTA-строка.

    Дедуп повторных одинаковых CTA подряд (правило A) и связанный gate по
    счётчику событий (правило B) — отдельный контракт, живёт в
    tests/tui/test_diff_cta_dedup.py; здесь не про дедуп, а про то, что CTA
    вообще появляется."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import TraceScreen
    from gc_tui.widgets.trace_row import TraceRow

    _seed_pending(launch_root, str(tmp_path / "threads"))
    cfg = _cfg(launch_root)
    cfg["launch_root"] = str(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        scr = pilot.app.screen
        assert isinstance(scr, TraceScreen)
        scr.attach_active_task("t1")
        scr._maybe_mount_diff_pending_cta()
        await pilot.pause()
        ctas = [
            row
            for row in scr.query(TraceRow)
            if "F3/diff" in str(row._content)
        ]
        assert len(ctas) == 1


@pytest.mark.asyncio
async def test_diff_viewer_created_shows_file_content(tmp_path, launch_root):
    """Cache-mode created: viewer показывает содержимое файла как addition."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.diff_tui.store import DiffStore
    from gc_tui.screens.diff import DiffScreen, DiffViewerScreen
    from textual.widgets import Static

    created = launch_root / "diff-test.txt"
    DiffStore(str(launch_root)).register_mutation(str(created))
    created.write_text("hello created body\n", encoding="utf-8")
    from gc_tui.diff_tui.touched import TouchedJournal
    TouchedJournal(str(tmp_path / "threads"), "cli", "t1").add("diff-test.txt")

    cfg = _cfg(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        scr = DiffScreen(launch_root=str(launch_root), config=cfg, **_diff_kwargs(tmp_path))
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(pilot.app.screen, DiffViewerScreen)
        body = "\n".join(str(st.render()) for st in pilot.app.screen.query(Static))
        assert "+hello created body" in body
        assert "нет содержимого диффа" not in body


@pytest.mark.asyncio
async def test_diff_viewer_undo_created_deletes_file(tmp_path, launch_root):
    """Созданный файл: undo удаляет с диска и убирает из списка."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.diff_tui.store import DiffStore
    from gc_tui.screens.diff import DiffScreen, DiffViewerScreen

    created = launch_root / "diff-test.txt"
    DiffStore(str(launch_root)).register_mutation(str(created))
    created.write_text("new\n", encoding="utf-8")
    from gc_tui.diff_tui.touched import TouchedJournal
    TouchedJournal(str(tmp_path / "threads"), "cli", "t1").add("diff-test.txt")

    cfg = _cfg(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        scr = DiffScreen(launch_root=str(launch_root), config=cfg, **_diff_kwargs(tmp_path))
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert any(e.key == "diff-test.txt" for e in scr._entries)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(pilot.app.screen, DiffViewerScreen)
        await pilot.press("r")
        await pilot.pause()
        assert isinstance(pilot.app.screen, DiffScreen)
        assert all(e.key != "diff-test.txt" for e in scr._entries)
        assert scr._status == "Отменено: diff-test.txt"
        assert not created.exists()


@pytest.mark.asyncio
async def test_trace_ctrl_a_selects_input_not_approve(tmp_path, launch_root):
    """На TraceScreen Ctrl+A — select-all во вводе; pending не approve."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.diff_tui.store import DiffStore
    from gc_tui.screens.main import TraceScreen
    from gc_tui.widgets.command_input import CommandTextArea

    _seed_pending(launch_root, str(tmp_path / "threads"))
    other = launch_root / "src" / "bar.py"
    other.write_text("base2\n", encoding="utf-8")
    DiffStore(str(launch_root)).register_mutation(str(other))
    other.write_text("changed2\n", encoding="utf-8")
    from gc_tui.diff_tui.touched import TouchedJournal
    TouchedJournal(str(tmp_path / "threads"), "cli", "t1").add("src/bar.py")

    cfg = _cfg(launch_root)
    cfg["launch_root"] = str(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        assert isinstance(pilot.app.screen, TraceScreen)
        inp = pilot.app.screen.query_one("#cmdinput", CommandTextArea)
        inp.text = "hello draft"
        inp.focus()
        await pilot.pause()
        await pilot.press("ctrl+a")
        await pilot.pause()
        assert inp.selected_text == "hello draft"
        from gc_tui.screens.diff import _service_from_config

        kw = _diff_kwargs(tmp_path)
        svc = _service_from_config(str(launch_root), cfg, **kw)
        assert len(svc.list_entries(pending_only=True)) >= 2


@pytest.mark.asyncio
async def test_f3_opens_diff(tmp_path, launch_root):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import DiffScreen
    from gc_tui.screens.main import TraceScreen

    _seed_pending(launch_root, str(tmp_path / "threads"))
    cfg = _cfg(launch_root)
    cfg["launch_root"] = str(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        assert isinstance(pilot.app.screen, TraceScreen)
        await pilot.press("f3")
        await pilot.pause()
        assert isinstance(pilot.app.screen, DiffScreen)
        await pilot.press("f3")
        await pilot.pause()
        assert sum(1 for s in pilot.app.screen_stack if isinstance(s, DiffScreen)) == 1


@pytest.mark.asyncio
async def test_diff_screen_esc_returns(tmp_path, launch_root):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import DiffScreen
    from gc_tui.screens.main import TraceScreen

    cfg = _cfg(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        await pilot.app.push_screen(
            DiffScreen(launch_root=str(launch_root), config=cfg, **_diff_kwargs(tmp_path))
        )
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(pilot.app.screen, TraceScreen)


# --- SPEC §5 шаг 4: `f` → push full-viewer, Esc=pop, footer [f] ----------

@pytest.mark.asyncio
async def test_diff_viewer_footer_has_f_hint(tmp_path, launch_root):
    """На адаптивном viewer footer содержит подсказку [f]."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import DiffScreen, DiffViewerScreen
    from textual.widgets import Static

    _seed_pending(launch_root, str(tmp_path / "threads"))
    cfg = _cfg(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        await pilot.app.push_screen(
            DiffScreen(launch_root=str(launch_root), config=cfg, **_diff_kwargs(tmp_path))
        )
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(pilot.app.screen, DiffViewerScreen)
        footer = str(pilot.app.screen.query_one("#footer", Static).render())
        assert "[f]" in footer


@pytest.mark.asyncio
async def test_f_pushes_full_viewer_and_not_again(tmp_path, launch_root):
    """`f` на адаптивном → push full-viewer; повторный `f` на full не пушит (D3)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import DiffScreen, DiffViewerScreen

    _seed_pending(launch_root, str(tmp_path / "threads"))
    cfg = _cfg(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        await pilot.app.push_screen(
            DiffScreen(launch_root=str(launch_root), config=cfg, **_diff_kwargs(tmp_path))
        )
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        first = pilot.app.screen
        assert isinstance(first, DiffViewerScreen)
        assert first.mode == "adaptive"
        await pilot.press("f")
        await pilot.pause()
        second = pilot.app.screen
        assert isinstance(second, DiffViewerScreen)
        assert second.mode == "full"
        assert second is not first
        # повторный `f` на full — не пушим ещё один слой
        await pilot.press("f")
        await pilot.pause()
        assert pilot.app.screen is second


@pytest.mark.asyncio
async def test_esc_from_full_returns_to_adaptive(tmp_path, launch_root):
    """Esc на full → pop на адаптивный; ещё Esc → список (стек pop, не toggle)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import DiffScreen, DiffViewerScreen

    _seed_pending(launch_root, str(tmp_path / "threads"))
    cfg = _cfg(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        await pilot.app.push_screen(
            DiffScreen(launch_root=str(launch_root), config=cfg, **_diff_kwargs(tmp_path))
        )
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("f")
        await pilot.pause()
        assert pilot.app.screen.mode == "full"
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(pilot.app.screen, DiffViewerScreen)
        assert pilot.app.screen.mode == "adaptive"
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(pilot.app.screen, DiffScreen)


@pytest.mark.asyncio
async def test_full_viewer_approve_closes_stack(tmp_path, launch_root):
    """a/r на full → действие + закрытие viewer-стека до списка (D3)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import DiffScreen

    _seed_pending(launch_root, str(tmp_path / "threads"))
    cfg = _cfg(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        scr = DiffScreen(launch_root=str(launch_root), config=cfg, **_diff_kwargs(tmp_path))
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("f")
        await pilot.pause()
        assert pilot.app.screen.mode == "full"
        await pilot.press("a")
        await pilot.pause()
        # viewer-стек закрыт полностью → DiffScreen
        assert isinstance(pilot.app.screen, DiffScreen)
        assert scr._entries == []
        assert (launch_root / "src" / "foo.py").read_text(encoding="utf-8") == "changed\n"


@pytest.mark.asyncio
async def test_full_viewer_undo_restores(tmp_path, launch_root):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import DiffScreen

    _seed_pending(launch_root, str(tmp_path / "threads"))
    cfg = _cfg(launch_root)
    target = launch_root / "src" / "foo.py"
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        await pilot.app.push_screen(
            DiffScreen(launch_root=str(launch_root), config=cfg, **_diff_kwargs(tmp_path))
        )
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("f")
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        assert isinstance(pilot.app.screen, DiffScreen)
        assert target.read_text(encoding="utf-8") == "base\n"


@pytest.mark.asyncio
async def test_full_viewer_ctrl_a_approves_all(tmp_path, launch_root):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.diff_tui.store import DiffStore
    from gc_tui.screens.diff import DiffScreen

    _seed_pending(launch_root, str(tmp_path / "threads"))
    other = launch_root / "src" / "bar.py"
    other.write_text("base2\n", encoding="utf-8")
    DiffStore(str(launch_root)).register_mutation(str(other))
    other.write_text("changed2\n", encoding="utf-8")
    from gc_tui.diff_tui.touched import TouchedJournal
    TouchedJournal(str(tmp_path / "threads"), "cli", "t1").add("src/bar.py")

    cfg = _cfg(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        scr = DiffScreen(launch_root=str(launch_root), config=cfg, **_diff_kwargs(tmp_path))
        await pilot.app.push_screen(scr)
        await pilot.pause()
        assert len(scr._entries) == 2
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("f")
        await pilot.pause()
        await pilot.press("ctrl+a")
        await pilot.pause()
        assert isinstance(pilot.app.screen, DiffScreen)
        assert scr._entries == []


@pytest.mark.asyncio
async def test_f_ru_layout_alias(tmp_path, launch_root):
    """ЙЦУКЕН: «а» (физическая клавиша F) = `f` → push full-viewer."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import DiffScreen, DiffViewerScreen

    _seed_pending(launch_root, str(tmp_path / "threads"))
    cfg = _cfg(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        await pilot.app.push_screen(
            DiffScreen(launch_root=str(launch_root), config=cfg, **_diff_kwargs(tmp_path))
        )
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        # «а» в ЙЦУКЕН на физической клавише F
        await pilot.press("а")
        await pilot.pause()
        assert isinstance(pilot.app.screen, DiffViewerScreen)
        assert pilot.app.screen.mode == "full"


@pytest.mark.asyncio
async def test_full_viewer_header_marks_full(tmp_path, launch_root):
    """Header на full-viewer содержит «полный файл»."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import DiffScreen
    from textual.widgets import Static

    _seed_pending(launch_root, str(tmp_path / "threads"))
    cfg = _cfg(launch_root)
    async with TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", config=cfg
    ).run_test() as pilot:
        await pilot.app.push_screen(
            DiffScreen(launch_root=str(launch_root), config=cfg, **_diff_kwargs(tmp_path))
        )
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("f")
        await pilot.pause()
        header = str(pilot.app.screen.query_one("#header-left", Static).render())
        assert "полный файл" in header
