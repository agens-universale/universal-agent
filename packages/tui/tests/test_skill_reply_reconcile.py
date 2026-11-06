"""Тесты для двух регрессий skill-сообщений.

Bug 1: Skill-реплика дублируется в трейсе (реконсайл по ev.summary вместо wire).
Bug 2: Skill-реплика на resume показывается как [!] failed (wire ∉ delivered_wires).
"""

from gc_testkit.tui_support import tui_settings
import json

import pytest

# ---------------------------------------------------------------------------
# Bug 1: дублирование user-reply для skill-сообщений
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skill_reply_no_duplicate_on_reconcile(tmp_path):
    """Полный live-цикл: отправка skill-реплики → polling видит её в .jsonl →
    реконсайл по wire (не по display) → НЕТ дубликата, pending исчезает."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.models import TraceEvent
    from gc_tui.widgets.user_reply import UserReplyContainer

    wire = "Используй skill `x`.\nИНСТР\nдавай"
    display = "давай"

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen

        # Имитируем состояние после submit: pending-widget смонтирован,
        # wire лежит в кэше.
        scr._pending_wire_cache["c1"] = wire
        scr._mount_user_reply(display, state="pending", client_id="c1")
        await pilot.pause()

        count_before = len(list(scr.query(UserReplyContainer)))
        assert count_before == 1, (
            f"должен быть 1 pending-виджет, получили {count_before}"
        )

        # Polling возвращает TraceEvent, у которого summary = display_content,
        # но full_payload содержит оригинальный wire.
        ev = TraceEvent(
            no=1,
            event_ts_utc_iso="2026-06-26T10:00:00+00:00",
            kind="user",
            title="",
            summary=display,
            full_payload={
                "role": "user",
                "content": wire,
                "display_content": display,
            },
            message_id="m1",
        )
        scr._render_event(ev, live=True)
        await pilot.pause()

        count_after = len(list(scr.query(UserReplyContainer)))
        assert count_after == 1, (
            f"НЕ должен появляться дубликат: до={count_before}, после={count_after}"
        )
        assert "c1" not in scr._pending_user_widgets, (
            "После реконсайла c1 должен быть удалён из _pending_user_widgets"
        )


@pytest.mark.asyncio
async def test_skill_reply_without_payload_falls_back_to_summary(tmp_path):
    """Если full_payload не dict — реконсайл идёт по ev.summary (старый путь)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.models import TraceEvent
    from gc_tui.widgets.user_reply import UserReplyContainer

    text = "обычное сообщение"

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen

        scr._pending_wire_cache["c2"] = text
        scr._mount_user_reply(text, state="pending", client_id="c2")
        await pilot.pause()

        # full_payload — строка, а не dict
        ev = TraceEvent(
            no=2,
            event_ts_utc_iso="2026-06-26T10:00:01+00:00",
            kind="user",
            title="",
            summary=text,
            full_payload=text,
            message_id="m2",
        )
        scr._render_event(ev, live=True)
        await pilot.pause()

        count = len(list(scr.query(UserReplyContainer)))
        assert count == 1, f"Без дубликата и со старым fallback: {count}"
        assert "c2" not in scr._pending_user_widgets


# ---------------------------------------------------------------------------
# Bug 2: skill-реплика на resume показывается как [!] failed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mount_failed_skips_skill_message_via_display_content(tmp_path):
    """Skill-запись: content='давай' (clean), wire_content=wire.
    user_messages_log хранит display_content='давай' (clean).
    После фикса: clean-контент находится в delivered_wires → НЕ монтируется как failed."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.user_reply import UserReplyContainer

    wire = "Используй skill `x`.\nИНСТР\nдавай"
    display = "давай"

    thread_id = "skill-resume-clean"
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()

    (threads_dir / f"data_cli_{thread_id}.jsonl").write_text(
        "", encoding="utf-8"
    )
    (threads_dir / f"meta_cli_{thread_id}.json").write_text(
        json.dumps(
            {
                "thread_id": thread_id,
                "status": "stopped",
                "updated_at": "2026-06-26T10:00:00+00:00",
                "messages_count": 0,
                # user_messages_log хранит display_content (clean)
                "user_messages_log": [display],
                "tui": {
                    "pending_user_messages": [
                        {
                            "client_id": "c1",
                            "content": display,  # clean (display_content)
                            "wire_content": wire,  # wire (длинный с инструкцией)
                            "status": "pending",
                            "ts_utc_iso": "2026-06-26T10:00:00+00:00",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    app = TuiApp(
        threads_dir=str(threads_dir), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        screen.attach_active_task(thread_id)
        await pilot.pause()

        failed = [
            w for w in screen.query(UserReplyContainer) if w._state == "failed"
        ]
        assert len(failed) == 0, (
            f"Skill-реплика не должна монтироваться как failed (content совпал с delivered): "
            f"{[w._text for w in failed]}"
        )


@pytest.mark.asyncio
async def test_mount_failed_mounts_truly_orphaned_skill(tmp_path):
    """Негативный тест: skill-реплика, чей content НЕ в user_messages_log →
    настоящий orphan, должен быть смонтирован как [!] failed."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.user_reply import UserReplyContainer

    wire = "Используй skill `x`.\nИНСТР\nдавай"
    display = "давай"

    thread_id = "skill-resume-orphan"
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()

    (threads_dir / f"data_cli_{thread_id}.jsonl").write_text(
        "", encoding="utf-8"
    )
    (threads_dir / f"meta_cli_{thread_id}.json").write_text(
        json.dumps(
            {
                "thread_id": thread_id,
                "status": "stopped",
                "updated_at": "2026-06-26T10:00:00+00:00",
                "messages_count": 0,
                # user_messages_log пуст — сообщение так и не дошло до агента
                "user_messages_log": [],
                "tui": {
                    "pending_user_messages": [
                        {
                            "client_id": "c1",
                            "content": display,
                            "wire_content": wire,
                            "status": "pending",
                            "ts_utc_iso": "2026-06-26T10:00:00+00:00",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    app = TuiApp(
        threads_dir=str(threads_dir), system="cli", settings=tui_settings({})
    )
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        screen.attach_active_task(thread_id)
        await pilot.pause()

        failed = [
            w for w in screen.query(UserReplyContainer) if w._state == "failed"
        ]
        assert len(failed) == 1, (
            f"Настоящий orphan должен быть смонтирован как failed, "
            f"got {len(failed)} failed: {[w._text for w in screen.query(UserReplyContainer)]}"
        )
        assert failed[0]._text == display
