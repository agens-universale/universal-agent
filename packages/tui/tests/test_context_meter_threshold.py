"""_context_meter нормирует бар на ЖЁСТКИЙ порог (summary_trigger), а не turn-end."""

import json
import re

import pytest


def _write_task(tmp_path, tid, content):
    # Метр читает только meta.runtime.live_history_chars (не data_*).
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps(
            {
                "thread_id": tid,
                "system_id": "cli",
                "runtime": {
                    "live_history_chars": len(content),
                    "live_msg_count": 1,
                },
            }
        ),
        encoding="utf-8",
    )
    rec = {
        "type": "message",
        "thread_id": tid,
        "system": "cli",
        "seq": 1,
        "ts": "t",
        "payload": {"role": "user", "content": content},
    }
    (tmp_path / f"data_cli_{tid}.jsonl").write_text(
        json.dumps(rec) + "\n", encoding="utf-8"
    )


def _meter_percent(meter) -> int:
    match = re.search(r"(\d+)%", meter.plain)
    assert match is not None
    return int(match.group(1))


def _cfg(trigger, turn_end):
    return {
        "model": "M",
        "backend_api_mode": "chat_completions",
        "summary_params": {
            "chat_completions_summary_trigger_chars": trigger,
            "chat_completions_summary_turn_end_trigger_chars": turn_end,
        },
    }


@pytest.mark.asyncio
async def test_meter_uses_hard_threshold_not_turn_end(tmp_path):
    """Бар нормируется от summary_trigger (10M), а не turn-end (1) → ~0%, не 100%."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "ctxm0000000000000000000000000001a"
    _write_task(tmp_path, tid, "hello")
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        config=_cfg(trigger=10_000_000, turn_end=1),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        meter = scr._context_meter()
        assert meter is not None
        assert (
            "0%" in meter.plain
        )  # summary 10M в знаменателе; на старом коде (turn_end=1) было бы 100%


@pytest.mark.asyncio
async def test_meter_falls_back_to_turn_end_when_no_hard_threshold(tmp_path):
    """summary_trigger=0 (форс-сжатие выкл) → fallback на turn-end, бар виден."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "ctxm0000000000000000000000000002a"
    _write_task(tmp_path, tid, "hello")
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        config=_cfg(trigger=0, turn_end=10_000_000),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        meter = scr._context_meter()
        assert (
            meter is not None
        )  # fallback на turn_end (иначе порог 0 → None/скрыт)
        assert "%" in meter.plain


def _cfg_tokens(trigger, turn_end, *, chars=0, chars_end=0):
    return {
        "model": "M",
        "backend_api_mode": "chat_completions",
        "summary_params": {
            "chat_completions_summary_trigger_chars": chars,
            "chat_completions_summary_turn_end_trigger_chars": chars_end,
            "chat_completions_summary_trigger_tokens": trigger,
            "chat_completions_summary_turn_end_trigger_tokens": turn_end,
        },
    }


@pytest.mark.asyncio
async def test_meter_uses_saved_prompt_tokens_without_tail(tmp_path):
    """Положительное число и включённый токенный порог — доля от числа, без хвоста."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "ctxm0000000000000000000000000005a"
    meta = {
        "thread_id": tid,
        "system_id": "cli",
        "last_main_prompt_tokens": 500,
        "runtime": {"live_history_chars": 9_000, "live_msg_count": 1},
    }
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        config=_cfg_tokens(trigger=1000, turn_end=1),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        meter = scr._context_meter()
        assert _meter_percent(meter) == 50


@pytest.mark.asyncio
async def test_meter_falls_back_to_chars_without_saved_tokens(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "ctxm0000000000000000000000000006a"
    _write_task(tmp_path, tid, "hello")
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        config=_cfg_tokens(trigger=1000, turn_end=1, chars=1000, chars_end=1),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        meter = scr._context_meter()
        assert _meter_percent(meter) == 0


@pytest.mark.asyncio
async def test_meter_token_mid_turn_not_replaced_by_turn_end(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "ctxm0000000000000000000000000007a"
    meta = {
        "thread_id": tid,
        "system_id": "cli",
        "last_main_prompt_tokens": 100,
        "runtime": {"live_history_chars": 1},
    }
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        config=_cfg_tokens(trigger=10_000_000, turn_end=1),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        meter = scr._context_meter()
        assert _meter_percent(meter) == 0


@pytest.mark.asyncio
async def test_meter_uses_per_model_mid_turn_threshold(tmp_path):
    """Бар делит сэмпл на mid-turn активного алиаса, не на глобальный summary."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "ctxm0000000000000000000000000008a"
    # 500 символов при пороге модели 1000 → 50%.
    content = "x" * 500
    _write_task(tmp_path, tid, content)
    config = {
        "model": "M",
        "backend_api_mode": "chat_completions",
        "summary_params": {
            "chat_completions_summary_trigger_chars": 10_000_000,
            "chat_completions_summary_turn_end_trigger_chars": 1,
        },
        "models": {
            "m": {
                "model": "m",
                "url": "http://x/v1/chat/completions",
                "authorization_header": "",
                "backend_overrides": {},
                "summary_trigger_chars": 1000,
            }
        },
        "slots": {"default": "m"},
    }
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config=config)
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        meter = scr._context_meter()
        assert _meter_percent(meter) == 50
