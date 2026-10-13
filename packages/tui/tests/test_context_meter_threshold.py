"""_context_meter нормирует бар на ЖЁСТКИЙ порог (summary_trigger), а не turn-end."""

from gc_testkit.tui_support import tui_settings
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
        "agent.model": "M",
        "backend.api_mode": "chat_completions",
        "summary": {
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
        settings=tui_settings(_cfg(trigger=10_000_000, turn_end=1)),
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
        settings=tui_settings(_cfg(trigger=0, turn_end=10_000_000)),
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
        "agent.model": "M",
        "backend.api_mode": "chat_completions",
        "summary": {
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
        settings=tui_settings(_cfg_tokens(trigger=1000, turn_end=1)),
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
        settings=tui_settings(
            _cfg_tokens(trigger=1000, turn_end=1, chars=1000, chars_end=1)
        ),
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
        settings=tui_settings(_cfg_tokens(trigger=10_000_000, turn_end=1)),
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
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {
                "agent.model": "M",
                "backend.api_mode": "chat_completions",
                "summary": {
                    "chat_completions_summary_trigger_chars": 10_000_000,
                    "chat_completions_summary_turn_end_trigger_chars": 1,
                },
                "models": {
                    "m": {
                        "model": "m",
                        "url": "http://x/v1/chat/completions",
                        "token": "",
                        "summary_trigger_chars": 1000,
                    }
                },
                "slots": {"default": "m"},
            }
        ),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        meter = scr._context_meter()
        assert _meter_percent(meter) == 50


@pytest.mark.asyncio
async def test_meter_follows_the_running_agent_not_the_live_screen(tmp_path):
    """Пока ход идёт, знаменатель — пороги РАБОТАЮЩЕГО агента: он сжимает по своему
    снимку конфига и по уровню, закреплённому на ход, а экран видит новый `/level`
    и новый профиль сразу. По живым данным бар показал бы не то, по чему реально
    сработает сжатие. Ход кончился — снова живой конфиг."""
    pytest.importorskip("textual")
    from types import SimpleNamespace

    from gc_core.summarize.config import ResolvedSummaryLimits
    from gc_tui.app import TuiApp

    tid = "ctxm0000000000000000000000000009a"
    _write_task(tmp_path, tid, "x" * 500)
    settings = tui_settings(_cfg(trigger=1000, turn_end=600))
    pinned = ResolvedSummaryLimits(
        chars_during=2000,
        tokens_during=0,
        chars_after=1200,
        tokens_after=0,
        incremental_batch_chars=0,
        max_dialogue_chars=0,
    )
    running = {"value": True}
    app = TuiApp(threads_dir=str(tmp_path), system="cli", settings=settings)
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        app.active_runner = SimpleNamespace(
            is_running=lambda: running["value"],
            pinned_summary_limits=lambda: pinned,
        )
        assert _meter_percent(scr._context_meter()) == 25  # 500 / 2000
        running["value"] = False
        assert _meter_percent(scr._context_meter()) == 50  # 500 / 1000
        app.active_runner = None


def test_runner_passes_the_executor_limits_through():
    from types import SimpleNamespace

    from gc_tui.services.agent_runner import AgentRunner

    runner = AgentRunner.__new__(AgentRunner)
    runner._executor = None
    assert runner.pinned_summary_limits() is None
    runner._executor = SimpleNamespace(summary_limits=lambda: "limits")
    assert runner.pinned_summary_limits() == "limits"


@pytest.mark.asyncio
async def test_idle_meter_uses_the_session_profile_global_threshold(tmp_path, monkeypatch):
    from gc_core.config import load_settings
    from gc_tui.app import TuiApp

    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        "backend:\n  api_mode: chat_completions\n"
        "  completions_url: http://local/v1/chat/completions\n"
        "models:\n  main: {}\nslots:\n  default: main\n"
        "summary:\n  chat_completions_summary_trigger_chars: 1000\n",
        encoding="utf-8",
    )
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "large.yml").write_text(
        "summary:\n  chat_completions_summary_trigger_chars: 2000\n",
        encoding="utf-8",
    )
    tid = "ctxm0000000000000000000000000010a"
    threads = tmp_path / "threads"
    threads.mkdir()
    _write_task(threads, tid, "x" * 500)
    app = TuiApp(settings=load_settings(str(config_path)), threads_dir=str(threads))
    app.set_config_path(str(config_path))
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        assert _meter_percent(scr._context_meter()) == 50

        app.switch_session_profile("large")

        assert app.effective_settings().settings.summary.chat_completions_summary_trigger_chars == 2000
        assert _meter_percent(scr._context_meter()) == 25

        app.switch_session_profile(None)
        assert _meter_percent(scr._context_meter()) == 50


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["light", "auto"])
async def test_meter_uses_prepared_call_thresholds_and_next_call_level(tmp_path, monkeypatch, mode):
    from gc_core.agent_executor import AgentExecutor
    from gc_core.run_context import acreate_run_context
    from gc_core.states import State
    from gc_core.turn_boundary import begin_turn
    from gc_core.processing_responses import aprepare_main_call
    from gc_core.auto_routing.state_ops import close_tick
    from gc_tui.app import TuiApp
    from gc_tui.services.agent_runner import AgentRunner

    loaded = tui_settings({
        "agent.model": "m", "level": mode,
        "backend.api_mode": "chat_completions",
        "models": {
            "fast": {"summary_trigger_chars": 1000},
            "strong": {"summary_trigger_tokens": 2000},
        },
        "slots": {"light": "fast", "default": "strong", "max": "strong"},
    })
    tid = "ctxm0000000000000000000000000011a"
    _write_task(tmp_path, tid, "x" * 500)
    meta_path = tmp_path / f"meta_cli_{tid}.json"
    meta = json.loads(meta_path.read_text())
    meta["last_main_prompt_tokens"] = 500
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    app = TuiApp(settings=loaded, threads_dir=str(tmp_path))
    ctx = await acreate_run_context(loaded, is_tui=True, level_provider=app._level_provider)
    state = State(
        verbose=False, model="m", tools_spec=ctx.tools_spec,
        tools_descr=ctx.tools_description, skills_list=[], max_steps=10,
        system_prompt_id="test", use_server_subagents=False,
    )
    executor = AgentExecutor(ctx)
    executor._bind_state(state, discard_writes_after_stop=False)
    begin_turn(state, ctx)
    await aprepare_main_call(state, ctx)
    runner = AgentRunner(executor=executor)
    monkeypatch.setattr(runner, "is_running", lambda: True)
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        app.active_runner = runner
        if mode == "light":
            assert _meter_percent(scr._context_meter()) == 50
            app.set_session_override("level", "max")
            assert _meter_percent(scr._context_meter()) == 50
            assert state.turn_level is None
            close_tick(state, state._call_level)
            await aprepare_main_call(state, ctx)
            assert _meter_percent(scr._context_meter()) == 25
        else:
            assert _meter_percent(scr._context_meter()) == 25
            close_tick(state, state._call_level)
            await aprepare_main_call(state, ctx)
            assert _meter_percent(scr._context_meter()) == 25
            close_tick(state, state._call_level)
            await aprepare_main_call(state, ctx)
            assert _meter_percent(scr._context_meter()) == 50
            assert state.turn_level is None
        app.active_runner = None
