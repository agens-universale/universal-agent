"""T15: technical user message после undo + UX ack/CTA helpers."""
from __future__ import annotations

import pytest

from config_modules import env_vars
from diff_tui.feedback import (
    build_undo_feedback_body,
    build_undo_feedback_message,
    files_word,
    format_approve_ack,
    format_pending_cta,
    format_undo_ack,
)
from diff_tui.service import DiffService
from diff_tui.store import DiffStore
from message_policy.user_message_policy import is_technical_user_message_text
from runtime_notices import clear_runtime_notices
from thread_store import load_thread_messages


@pytest.fixture
def cache_proj(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "src").mkdir()
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    clear_runtime_notices()
    return root


@pytest.mark.parametrize(
    "n,word",
    [(1, "файл"), (2, "файла"), (5, "файлов"), (11, "файлов"), (21, "файл"), (22, "файла")],
)
def test_files_word(n, word):
    assert files_word(n) == word


def test_format_undo_ack_one_and_many():
    assert format_undo_ack(["src/a.py"]) == "Отменено: src/a.py"
    assert format_undo_ack(["a", "b"]) == "Отменено 2 файла"
    assert format_undo_ack([]) is None


def test_format_approve_ack_and_cta():
    assert format_approve_ack(1, "src/a.py") == "Принято: src/a.py"
    assert format_approve_ack(3) == "Принято 3 файла"
    assert format_approve_ack(0) is None
    assert format_pending_cta(1) == "Изменено 1 файл. /diff для просмотра"
    assert format_pending_cta(0) is None


def test_build_undo_feedback_message_wraps_technical():
    msg = build_undo_feedback_message(["src/foo.py", "src/bar.py"])
    assert msg is not None
    assert msg["role"] == "user"
    assert is_technical_user_message_text(msg["content"])
    assert "src/foo.py" in msg["content"]
    assert "src/bar.py" in msg["content"]
    assert "Re-read" in msg["content"]


def test_build_undo_feedback_empty_keys():
    assert build_undo_feedback_message([]) is None
    assert "src/a.py" in build_undo_feedback_body(["src/a.py"])


def test_service_undo_returns_undone_keys(cache_proj):
    target = cache_proj / "src" / "foo.py"
    target.write_text("base\n", encoding="utf-8")
    DiffStore(str(cache_proj)).register_mutation(str(target))
    target.write_text("changed\n", encoding="utf-8")

    svc = DiffService(str(cache_proj), emit_notices=False)
    result = svc.undo(str(target))
    assert result.ok
    assert result.undone_keys == ("src/foo.py",)


def test_service_undo_created_no_undone_keys(cache_proj):
    target = cache_proj / "src" / "new.py"
    DiffStore(str(cache_proj)).register_mutation(str(target))
    target.write_text("x\n", encoding="utf-8")
    svc = DiffService(str(cache_proj), emit_notices=False)
    result = svc.undo(str(target))
    assert result.ok is False
    assert result.undone_keys == ()


@pytest.mark.asyncio
async def test_t15_diff_undo_appends_technical_to_thread(tmp_path, cache_proj):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.diff import DiffScreen
    from thread_store import append_new_thread_messages

    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    tid = "diffundo1"
    append_new_thread_messages(
        str(threads_dir),
        "cli",
        tid,
        [{"role": "user", "content": "hi", "id": "m1"}],
        set(),
    )

    target = cache_proj / "src" / "foo.py"
    target.write_text("base\n", encoding="utf-8")
    DiffStore(str(cache_proj)).register_mutation(str(target))
    target.write_text("changed\n", encoding="utf-8")

    cfg = {
        "launch_root": str(cache_proj),
        "model": "M",
        "write_guard": {"enabled": False, "mode": "deny", "extra_paths": []},
        "diff_tui": {
            "cache_max_orig": 500,
            "preview_context_lines": 3,
            "max_diff_chars": 8000,
        },
    }
    app = TuiApp(threads_dir=str(threads_dir), system="cli", config=cfg)
    async with app.run_test() as pilot:
        pilot.app.screen.attach_active_task(tid)
        await pilot.pause()

        feedback: list = []
        trace = pilot.app.screen

        def _capture(keys):
            feedback.append(list(keys))
            trace._on_diff_undo_feedback(keys)

        scr = DiffScreen(
            launch_root=str(cache_proj),
            config=cfg,
            on_undo_feedback=_capture,
        )
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()

        assert feedback == [["src/foo.py"]]
        assert scr._status == "Отменено: src/foo.py"
        msgs, _keys, _seq = load_thread_messages(str(threads_dir), "cli", tid)
        technical = [
            m for m in msgs if is_technical_user_message_text(m.get("content", ""))
        ]
        assert len(technical) == 1
        assert "src/foo.py" in technical[0]["content"]
