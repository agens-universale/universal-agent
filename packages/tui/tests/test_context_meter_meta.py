"""_context_meter / compact-count читают только meta.runtime.live_*, не data_*."""

import json

import pytest


def _write_task(
    tmp_path,
    tid,
    *,
    content="hello",
    runtime=None,
    tui=None,
    data_content=None,
):
    meta = {"thread_id": tid, "system_id": "cli"}
    if runtime is not None:
        meta["runtime"] = runtime
    if tui is not None:
        meta["tui"] = tui
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    body = data_content if data_content is not None else content
    rec = {
        "type": "message",
        "thread_id": tid,
        "system": "cli",
        "seq": 1,
        "ts": "t",
        "payload": {"role": "user", "content": body},
    }
    (tmp_path / f"data_cli_{tid}.jsonl").write_text(
        json.dumps(rec) + "\n", encoding="utf-8"
    )


def _cfg(trigger=10_000, turn_end=1):
    return {
        "model": "M",
        "backend_api_mode": "chat_completions",
        "summary_params": {
            "chat_completions_summary_trigger_chars": trigger,
            "chat_completions_summary_turn_end_trigger_chars": turn_end,
        },
    }


@pytest.mark.asyncio
async def test_context_meter_hidden_without_live_chars_key(tmp_path):
    """Нет meta.runtime.live_history_chars → метр скрыт; data_* не читаем."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "ctxm0000000000000000000000000003a"
    _write_task(tmp_path, tid, content="x" * 5000)  # data есть, ключа нет
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config=_cfg())
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        calls = []
        orig = scr._store.read_live_messages

        def _spy(task_id, system=None):
            calls.append(task_id)
            return orig(task_id, system=system)

        scr._store.read_live_messages = _spy
        assert scr._context_meter() is None
        assert calls == []


@pytest.mark.asyncio
async def test_context_meter_reads_meta_not_data(tmp_path):
    """Метр берёт chars из meta; расхождение с data_* игнорируется."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "ctxm0000000000000000000000000004a"
    # meta говорит 500; data — огромный текст (если бы сканировали — другой %)
    _write_task(
        tmp_path,
        tid,
        runtime={"live_history_chars": 500, "live_msg_count": 1},
        data_content="Z" * 50_000,
    )
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config=_cfg(trigger=1000)
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        calls = []
        orig = scr._store.read_live_messages

        def _spy(task_id, system=None):
            calls.append(task_id)
            return orig(task_id, system=system)

        scr._store.read_live_messages = _spy
        meter = scr._context_meter()
        assert meter is not None
        assert "50%" in meter.plain  # 500/1000, не 50_000/1000
        assert calls == []


@pytest.mark.asyncio
async def test_cmd_compact_antispam_uses_live_msg_count(tmp_path):
    """Антиспам сравнивает live_msg_count с last_compact_msg_count, не data_*."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "cmp00000000000000000000000000007a"
    # data пустой / 0 сообщений, но meta говорит count=3, baseline=3 → блок
    meta = {
        "thread_id": tid,
        "system_id": "cli",
        "runtime": {"live_msg_count": 3},
        "tui": {"last_compact_msg_count": 3},
    }
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")

    class _Runner:
        def __init__(self):
            self.compacted = False

        def is_running(self):
            return False

        def compact_now(self, tid_, system):
            self.compacted = True
            return True

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        config={"model": "M", "backend_api_mode": "chat_completions"},
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        runner = _Runner()
        pilot.app.active_runner = runner
        calls = []
        orig = scr._store.read_live_messages

        def _spy(task_id, system=None):
            calls.append(task_id)
            return orig(task_id, system=system)

        scr._store.read_live_messages = _spy
        scr._cmd_compact()
        assert runner.compacted is False
        assert calls == []
