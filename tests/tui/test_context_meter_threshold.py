"""_context_meter нормирует бар на ЖЁСТКИЙ порог (summary_trigger), а не turn-end."""
import json
import pytest


def _write_task(tmp_path, tid, content):
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "system_id": "cli"}), encoding="utf-8")
    rec = {"type": "message", "thread_id": tid, "system": "cli", "seq": 1, "ts": "t",
           "payload": {"role": "user", "content": content}}
    (tmp_path / f"data_cli_{tid}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _cfg(trigger, turn_end):
    return {"model": "M", "backend_api_mode": "chat_completions",
            "summary_params": {"chat_completions_summary_trigger_chars": trigger,
                               "chat_completions_summary_turn_end_trigger_chars": turn_end}}


@pytest.mark.asyncio
async def test_meter_uses_hard_threshold_not_turn_end(tmp_path):
    """Бар нормируется от summary_trigger (10M), а не turn-end (1) → ~0%, не 100%."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    tid = "ctxm0000000000000000000000000001a"
    _write_task(tmp_path, tid, "hello")
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config=_cfg(trigger=10_000_000, turn_end=1))
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        meter = scr._context_meter()
        assert meter is not None
        text, _ = meter
        assert "0%" in text   # summary 10M в знаменателе; на старом коде (turn_end=1) было бы 100%


@pytest.mark.asyncio
async def test_meter_falls_back_to_turn_end_when_no_hard_threshold(tmp_path):
    """summary_trigger=0 (форс-сжатие выкл) → fallback на turn-end, бар виден."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    tid = "ctxm0000000000000000000000000002a"
    _write_task(tmp_path, tid, "hello")
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config=_cfg(trigger=0, turn_end=10_000_000))
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        meter = scr._context_meter()
        assert meter is not None   # fallback на turn_end (иначе порог 0 → None/скрыт)
        text, _ = meter
        assert "%" in text
