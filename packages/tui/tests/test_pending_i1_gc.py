"""I1/A: GC pending на stream_reset, wire-match fallback на meta, set_tui_* не воскрешает."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _read_pending(threads_dir, tid):
    p = Path(threads_dir) / f"meta_cli_{tid}.json"
    if not p.exists():
        return []
    meta = json.loads(p.read_text(encoding="utf-8"))
    return (meta.get("tui") or {}).get("pending_user_messages", [])


def test_wire_match_falls_back_to_meta_wire_content():
    """Пустой wire-cache, в meta есть wire_content → match работает."""
    from gc_tui.screens.main import TraceScreen

    tid = "wire-meta-fallback"
    store = SimpleNamespace(
        get_task_meta=lambda _tid: {
            "tui": {
                "pending_user_messages": [
                    {
                        "client_id": "c1",
                        "content": "hello",
                        "wire_content": "wire-hello\n",
                        "status": "pending",
                    }
                ]
            }
        }
    )
    scr = TraceScreen()
    scr._active_task_id = tid
    scr._store = store
    assert "c1" not in scr._pending_wire_cache
    assert scr._pending_wire_match("c1", "wire-hello")


async def test_set_tui_finalizing_does_not_resurrect_cleared_pending(tmp_path):
    """TUI снял pending с диска → set_tui_* их не возвращает (I1)."""
    from gc_core.run_summary_coordinator import aset_tui_finalizing
    from gc_core.thread_store import ThreadSession, save_thread_meta, update_tui_meta

    tid = "t1"
    ts = ThreadSession(
        threads_dir=str(tmp_path),
        system_id="cli",
        thread_id=tid,
        history_messages=[],
        known_message_keys=set(),
        persisted_messages_count=0,
        meta={
            "tui": {
                "pending_user_messages": [
                    {"client_id": "stale", "content": "x", "status": "pending"}
                ]
            }
        },
        previous_response_id=None,
        backend_api_mode="chat_completions",
    )
    save_thread_meta(str(tmp_path), "cli", tid, ts.meta)
    update_tui_meta(str(tmp_path), "cli", tid, {"pending_user_messages": []})

    await aset_tui_finalizing(ts, True)

    meta = json.loads((tmp_path / f"meta_cli_{tid}.json").read_text(encoding="utf-8"))
    assert (meta.get("runtime") or {}).get("finalizing") is True
    assert (meta.get("tui") or {}).get("pending_user_messages") == []


def test_patch_runtime_flag_preserves_disk_tui(tmp_path):
    """Канон I1: runtime flag не воскрешает tui.pending."""
    from gc_core.thread_store import patch_runtime_flag, save_thread_meta, update_tui_meta

    tid = "t2"
    session_meta = {
        "tui": {"pending_user_messages": [{"client_id": "stale"}]},
    }
    save_thread_meta(str(tmp_path), "cli", tid, session_meta)
    update_tui_meta(str(tmp_path), "cli", tid, {"pending_user_messages": []})

    patch_runtime_flag(
        str(tmp_path),
        "cli",
        tid,
        "summarizing",
        active=True,
        session_meta=session_meta,
    )

    meta = json.loads((tmp_path / f"meta_cli_{tid}.json").read_text(encoding="utf-8"))
    assert (meta.get("runtime") or {}).get("summarizing") is True
    assert (meta.get("tui") or {}).get("pending_user_messages") == []
    assert (session_meta.get("runtime") or {}).get("summarizing") is True
    assert session_meta.get("tui", {}).get("pending_user_messages") == []


@pytest.mark.asyncio
async def test_stream_reset_gcs_delivered_pending_from_user_ask(tmp_path):
    """stream_reset: pending, чей content ∈ user_ask, удаляется из meta без re-attach."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "t-gc-delivered"
    with open(tmp_path / f"data_cli_{tid}.jsonl", "w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "type": "message",
                    "thread_id": tid,
                    "system": "cli",
                    "seq": 1,
                    "ts": "2026-06-11T10:00:00+00:00",
                    "payload": {"id": "u1", "role": "user", "content": "доставленное"},
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    meta_path = tmp_path / f"meta_cli_{tid}.json"
    meta_path.write_text(
        json.dumps(
            {
                "thread_id": tid,
                "system_id": "cli",
                "updated_at": "2026-06-11T10:00:00+00:00",
                "messages_count": 1,
                "user_intent_log": [
                    {
                        "turn": 1,
                        "ts": "2026-06-11T10:00:00+00:00",
                        "action": "user_ask",
                        "tool_name": None,
                        "content": "доставленное",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    app._executor_factory = lambda: None
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        screen.attach_active_task(tid)
        await pilot.pause()

        # Инжектим orphan-pending после attach (как mid-run stale meta).
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta.setdefault("tui", {})["pending_user_messages"] = [
            {
                "client_id": "cid-delivered",
                "content": "доставленное",
                "wire_content": "доставленное",
                "status": "pending",
                "ts_utc_iso": "2026-06-11T09:59:00+00:00",
            }
        ]
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        assert len(_read_pending(tmp_path, tid)) == 1

        # Компакция → stream_reset
        (tmp_path / f"data_cli_{tid}.jsonl").write_text(
            json.dumps(
                {
                    "type": "message",
                    "thread_id": tid,
                    "system": "cli",
                    "seq": 1,
                    "ts": "2026-06-11T11:00:00+00:00",
                    "payload": {
                        "id": "s1",
                        "role": "assistant",
                        "content": "после-сжатия",
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta.update(
            {
                "updated_at": "2026-06-11T11:00:00+00:00",
                "messages_count": 1,
                "last_summary_archive_suffix": "2026-06-11T11-00-00",
            }
        )
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

        def trace_text():
            trace = pilot.app.query_one("#trace")
            return "\n".join(str(c.render()) for c in trace.walk_children())

        for _ in range(200):
            if "после-сжатия" in trace_text() and _read_pending(tmp_path, tid) == []:
                break
            await pilot.pause(0.05)
        else:
            raise AssertionError(
                f"timeout: stream_reset GC, pending={_read_pending(tmp_path, tid)!r}"
            )
