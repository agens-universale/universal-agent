"""Пороги суммаризации на models.<alias>: резолв mid/turn-end/батч/диалог."""

from gc_core.config import flatten_app_config
from gc_core.config_modules.root import AppConfig, resolve_paths
from gc_core.config_modules.summary import DEFAULT_INCREMENTAL_BATCH_MULTIPLIER
from gc_core.model_router import ModelRouter
from gc_core.summarize.config import (
    resolve_summary_limits,
    resolve_summary_limits_at_level,
)
from pydantic import ValidationError
import pytest


def _cfg(raw: dict) -> dict:
    app = AppConfig.model_validate(raw)
    resolve_paths(app, ".")
    return flatten_app_config(app)


def _global_summary(
    *,
    chars: int = 200000,
    tokens: int = 0,
    batch: int | None = None,
    dialogue: int = 1000000,
) -> dict:
    summary = {
        "chat_completions_summary_trigger_chars": chars,
        "chat_completions_summary_trigger_tokens": tokens,
        "max_dialogue_chars": dialogue,
    }
    if batch is not None:
        summary["incremental_batch_chars"] = batch
    return summary


def test_default_incremental_batch_multiplier_is_1_2():
    assert DEFAULT_INCREMENTAL_BATCH_MULTIPLIER == 1.2


def test_model_entry_reads_null_summary_trigger_as_unset():
    """`summary_trigger_tokens:` без значения — YAML null. У соседних ключей записи
    null значит «не задано», и здесь загрузка конфига не должна падать на схеме."""
    cfg = AppConfig.model_validate(
        {
            "models": {
                "m": {
                    "url": "http://x/v1/chat/completions",
                    "token": "",
                    "summary_trigger_chars": None,
                    "summary_trigger_tokens": None,
                }
            }
        }
    )
    assert cfg.models["m"].summary_trigger_chars == 0
    assert cfg.models["m"].summary_trigger_tokens == 0


def test_model_entry_rejects_negative_summary_triggers():
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "models": {
                    "m": {
                        "url": "http://x/v1/chat/completions",
                        "token": "",
                        "summary_trigger_chars": -1,
                    }
                }
            }
        )
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "models": {
                    "m": {
                        "url": "http://x/v1/chat/completions",
                        "token": "",
                        "summary_trigger_tokens": -5,
                    }
                }
            }
        )


def test_both_zero_or_missing_uses_global_summary_params():
    cfg = _cfg(
        {
            "summary": _global_summary(chars=200000, tokens=50000, batch=111),
            "models": {
                "a": {
                    "url": "http://a/v1/chat/completions",
                    "token": "",
                    "summary_trigger_chars": 0,
                    "summary_trigger_tokens": 0,
                },
                "b": {"url": "http://b/v1/chat/completions", "token": ""},
            },
            "slots": {"default": "a"},
        }
    )
    for alias in ("a", "b"):
        entry = cfg["models"][alias]
        limits = resolve_summary_limits(
            cfg,
            model_chars=entry.get("summary_trigger_chars", 0),
            model_tokens=entry.get("summary_trigger_tokens", 0),
        )
        assert limits.chars_during == 200000
        assert limits.tokens_during == 50000
        assert (
            limits.chars_after
            == cfg["summary_params"][
                "chat_completions_summary_turn_end_trigger_chars"
            ]
        )
        assert (
            limits.tokens_after
            == cfg["summary_params"][
                "chat_completions_summary_turn_end_trigger_tokens"
            ]
        )
        assert limits.incremental_batch_chars == 111
        assert limits.max_dialogue_chars == 1000000


def test_chars_only_on_model_zeros_token_scale_and_derives_batch_dialogue():
    cfg = _cfg(
        {
            "summary": _global_summary(chars=999999, tokens=888888, batch=1),
            "models": {
                "m": {
                    "url": "http://m/v1/chat/completions",
                    "token": "",
                    "summary_trigger_chars": 100000,
                    "summary_trigger_tokens": 0,
                }
            },
            "slots": {"default": "m"},
        }
    )
    entry = cfg["models"]["m"]
    limits = resolve_summary_limits(
        cfg,
        model_chars=entry["summary_trigger_chars"],
        model_tokens=entry.get("summary_trigger_tokens", 0),
    )
    assert limits.chars_during == 100000
    assert limits.tokens_during == 0
    assert limits.chars_after == int(100000 * 0.6)
    assert limits.tokens_after == 0
    assert limits.incremental_batch_chars == int(100000 * 1.2)
    assert limits.max_dialogue_chars == int(100000 * 5)


def test_tokens_only_on_model_zeros_char_scale_and_uses_chars_per_token():
    cfg = _cfg(
        {
            "summary": _global_summary(chars=999999, tokens=888888, batch=1),
            "models": {
                "m": {
                    "url": "http://m/v1/chat/completions",
                    "token": "",
                    "summary_trigger_chars": 0,
                    "summary_trigger_tokens": 116128,
                }
            },
            "slots": {"default": "m"},
        }
    )
    entry = cfg["models"]["m"]
    limits = resolve_summary_limits(
        cfg,
        model_chars=entry.get("summary_trigger_chars", 0),
        model_tokens=entry["summary_trigger_tokens"],
    )
    assert limits.chars_during == 0
    assert limits.tokens_during == 116128
    assert limits.chars_after == 0
    assert limits.tokens_after == int(116128 * 0.6)
    char_base = 116128 * 2
    assert limits.incremental_batch_chars == int(char_base * 1.2)
    assert limits.max_dialogue_chars == int(char_base * 5)


def test_resolved_model_exposes_summary_triggers_zero_means_unset():
    cfg = _cfg(
        {
            "models": {
                "set": {
                    "url": "http://set/v1/chat/completions",
                    "token": "",
                    "summary_trigger_chars": 100000,
                    "summary_trigger_tokens": 116128,
                },
                "unset": {
                    "url": "http://unset/v1/chat/completions",
                    "token": "",
                },
            },
            "slots": {"light": "set", "default": "unset"},
        }
    )
    router = ModelRouter(cfg, level_provider=lambda: "light")
    set_m = router.resolve("main")
    assert set_m.summary_trigger_chars == 100000
    assert set_m.summary_trigger_tokens == 116128

    router = ModelRouter(cfg, level_provider=lambda: "default")
    unset = router.resolve("main")
    assert unset.summary_trigger_chars == 0
    assert unset.summary_trigger_tokens == 0
    assert "summary_trigger_chars" not in cfg["models"]["unset"]
    assert cfg["models"]["set"]["summary_trigger_chars"] == 100000


def test_snapshot_zero_and_missing_key_resolve_alike():
    cfg = _cfg(
        {
            "summary": _global_summary(chars=50000),
            "models": {
                "explicit_zero": {
                    "url": "http://z/v1/chat/completions",
                    "token": "",
                    "summary_trigger_chars": 0,
                    "summary_trigger_tokens": 0,
                },
                "missing": {
                    "url": "http://m/v1/chat/completions",
                    "token": "",
                },
            },
            "slots": {"default": "explicit_zero"},
        }
    )
    zero = cfg["models"]["explicit_zero"]
    missing = cfg["models"]["missing"]
    a = resolve_summary_limits(
        cfg,
        model_chars=zero.get("summary_trigger_chars", 0),
        model_tokens=zero.get("summary_trigger_tokens", 0),
    )
    b = resolve_summary_limits(
        cfg,
        model_chars=missing.get("summary_trigger_chars", 0),
        model_tokens=missing.get("summary_trigger_tokens", 0),
    )
    assert a == b


@pytest.mark.asyncio
def _two_level_cfg() -> dict:
    return _cfg(
        {
            "summary": _global_summary(chars=999999),
            "models": {
                "low": {
                    "url": "http://low/v1/chat/completions",
                    "token": "",
                    "summary_trigger_chars": 10,
                },
                "high": {
                    "url": "http://high/v1/chat/completions",
                    "token": "",
                    "summary_trigger_chars": 1_000_000,
                },
            },
            "slots": {"light": "low", "default": "high"},
        }
    )


def _chat_state(chars: int = 100):
    from gc_core.states import SharedStateData, State

    state = State(
        verbose=False,
        model="m",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=10,
        system_prompt_id="t",
        use_server_subagents=False,
        shared=SharedStateData(),
    )
    state.backend_api_mode = "chat_completions"
    state.messages = [{"role": "user", "content": "x" * chars}]
    return state


async def test_threshold_belongs_to_the_model_pinned_for_the_turn():
    """Порог сжатия — от той модели, в которую идут запросы хода. `/level` посреди
    хода модель не меняет (уровень закреплён `begin_turn`), значит и порог обязан
    остаться прежним: с порогом новой, большой модели лёгкая переполнила бы окно.
    Новый порог начинает действовать с началом следующего хода."""
    from types import SimpleNamespace
    from unittest.mock import patch

    from gc_core.run_summary_coordinator import RunSummaryCoordinator
    from gc_core.turn_boundary import begin_turn

    level = {"current": "light"}
    cfg = _two_level_cfg()
    router = ModelRouter(cfg, level_provider=lambda: level["current"])
    state = _chat_state()
    ctx = SimpleNamespace(config_dict=cfg, model_router=router, logger=None)
    begin_turn(state, ctx)

    def provider():
        return resolve_summary_limits_at_level(cfg, router, state.turn_level)

    first = provider()
    assert first.chars_during == 10
    coord = RunSummaryCoordinator(
        ctx=ctx,
        state=state,
        thread_session=None,
        summary_trigger_chars=first.chars_during,
        summary_trigger_tokens=first.tokens_during,
        turn_end_trigger_chars=first.chars_after,
        turn_end_trigger_tokens=first.tokens_after,
        is_cli_mode=False,
        pre_backend_trigger_reason="pre_backend",
        limits_provider=provider,
    )
    compact_calls = []

    async def fake_compact(**kwargs):
        compact_calls.append(kwargs.get("trigger_threshold_chars"))

    with patch(
        "gc_core.run_summary_coordinator.arun_summary_compaction",
        side_effect=fake_compact,
    ):
        # Посреди хода живой уровень ушёл на большую модель — запросы хода
        # по-прежнему идут в лёгкую, и сжимать надо по её порогу.
        level["current"] = "default"
        await coord.apre_backend_compaction_hook(
            [state.messages[-1]], "user_ask"
        )
        assert compact_calls == [10]

        # Новый ход: уровень закреплён заново, действует порог большой модели.
        begin_turn(state, ctx)
        compact_calls.clear()
        await coord.apre_backend_compaction_hook(
            [state.messages[-1]], "user_ask"
        )
        assert compact_calls == []


def test_executor_reports_the_limits_its_turn_runs_on():
    """`summary_limits()` — то, по чему этот executor сжимает: его конфиг, его
    Router и уровень хода. Индикатор контекста читает именно это: живой `/level`
    экрана меняется раньше, чем его увидит ход."""
    from types import SimpleNamespace

    from gc_core.agent_executor import AgentExecutor
    from gc_core.turn_boundary import begin_turn

    level = {"current": "light"}
    cfg = _two_level_cfg()
    router = ModelRouter(cfg, level_provider=lambda: level["current"])
    ctx = SimpleNamespace(config_dict=cfg, model_router=router)
    executor = AgentExecutor(ctx)
    assert executor.summary_limits().chars_during == 10  # хода ещё нет — живой

    state = _chat_state()
    begin_turn(state, ctx)
    executor._bind_state(state, discard_writes_after_stop=False)
    level["current"] = "default"
    assert executor.summary_limits().chars_during == 10
    begin_turn(state, ctx)
    assert executor.summary_limits().chars_during == 1_000_000


def test_limits_without_a_pinned_level_follow_the_live_one():
    """Уровня вызова нет (ход ещё не начинали) — обычный резолв `main`."""
    cfg = _two_level_cfg()
    router = ModelRouter(cfg, level_provider=lambda: "light")
    assert resolve_summary_limits_at_level(cfg, router, None).chars_during == 10
    assert resolve_summary_limits_at_level(cfg, None, None).chars_during == 999999


def test_limits_survive_an_open_breaker_on_the_alias():
    """Открытый предохранитель роняет запросы, а не чтение порога: иначе порог
    молча съезжал бы на общий ровно в аварии."""
    cfg = _two_level_cfg()
    router = ModelRouter(cfg, level_provider=lambda: "default")
    router.breaker.trip("low", "5xx после ретраев")
    assert (
        resolve_summary_limits_at_level(cfg, router, "light").chars_during == 10
    )


def test_subagent_uses_own_alias_threshold():
    """Субагент ведёт историю своей моделью: дочерний Router отдаёт её пороги,
    уровень хода родителя на них не влияет."""
    cfg = _cfg(
        {
            "summary": _global_summary(chars=999999),
            "models": {
                "main_m": {
                    "url": "http://main/v1/chat/completions",
                    "token": "",
                    "summary_trigger_chars": 10,
                },
                "sub_m": {
                    "url": "http://sub/v1/chat/completions",
                    "token": "",
                    "summary_trigger_chars": 50000,
                },
            },
            "slots": {"default": "main_m"},
            "assign": {"subagents": {"subagent_worker": "sub_m"}},
        }
    )
    router = ModelRouter(cfg, level_provider=lambda: "default")
    child = router.with_override(router.resolve("subagent:subagent_worker"))
    assert (
        resolve_summary_limits_at_level(cfg, router, "default").chars_during
        == 10
    )
    assert (
        resolve_summary_limits_at_level(cfg, child, "default").chars_during
        == 50000
    )


# --- батч и лимит диалога модели доходят до ЛЮБОГО сжатия ----------------------


class _SummarizerReached(Exception):
    """Суммаризатор вызван: аргументы сняты, дальше сжатие идти не обязано."""


def _capture_summarizer(monkeypatch, ctx) -> dict:
    """Подменить суммаризатор в общей точке сжатия и снять, с чем его позвали."""
    from gc_core.summarize import compaction

    seen: dict = {}

    async def _fake(**kwargs):
        seen["batch_chars"] = kwargs.get("batch_chars")
        seen["max_dialogue_chars"] = kwargs.get("max_dialogue_chars")
        seen["summary_params"] = ctx.config_dict.get("summary_params")
        raise _SummarizerReached

    monkeypatch.setattr(
        compaction, "asummarize_dialog_via_chat_completions", _fake
    )
    return seen


def _light_turn(level: dict):
    """Ход, начатый на лёгкой модели (порог 10 символов → батч 12, диалог 50)."""
    from types import SimpleNamespace

    from gc_core.turn_boundary import begin_turn

    cfg = _two_level_cfg()
    router = ModelRouter(cfg, level_provider=lambda: level["current"])
    ctx = SimpleNamespace(
        config_dict=cfg,
        model_router=router,
        logger=None,
        on_summary_applied=None,
        is_cli_mode=False,
    )
    state = _chat_state()
    begin_turn(state, ctx)
    return cfg, ctx, state


async def test_compaction_hands_the_models_batch_to_the_summarizer(monkeypatch):
    """Батч и лимит диалога считаются от порога модели, чтобы кусок истории
    помещался в её окно. Доходят они параметрами, от модели ХОДА: живой уровень
    переключён на большую модель, а сжимается история лёгкой."""
    from gc_core.summarize.compaction import arun_summary_compaction

    level = {"current": "light"}
    _cfg_dict, ctx, state = _light_turn(level)
    seen = _capture_summarizer(monkeypatch, ctx)
    level["current"] = "default"
    with pytest.raises(_SummarizerReached):
        await arun_summary_compaction(
            ctx=ctx, state=state, trigger_reason="t", is_cli_mode=False
        )
    assert (seen["batch_chars"], seen["max_dialogue_chars"]) == (12, 50)


async def test_compaction_keeps_global_values_for_a_model_without_thresholds(
    monkeypatch,
):
    from types import SimpleNamespace

    from gc_core.summarize.compaction import arun_summary_compaction

    cfg = _cfg(
        {
            "summary": _global_summary(chars=200000, batch=77000),
            "models": {"m": {"url": "http://m/v1/chat/completions", "token": ""}},
            "slots": {"default": "m"},
        }
    )
    ctx = SimpleNamespace(
        config_dict=cfg,
        model_router=ModelRouter(cfg, level_provider=lambda: "default"),
        logger=None,
    )
    seen = _capture_summarizer(monkeypatch, ctx)
    with pytest.raises(_SummarizerReached):
        await arun_summary_compaction(
            ctx=ctx, state=_chat_state(), trigger_reason="t", is_cli_mode=False
        )
    assert (seen["batch_chars"], seen["max_dialogue_chars"]) == (77000, 1000000)


async def test_automatic_compaction_does_not_rewrite_the_shared_config(
    monkeypatch,
):
    """Раньше батч модели доезжал подменой `config_dict["summary_params"]` на
    время сжатия. Общий конфиг сжатие не трогает: раздел — тот же объект."""
    from gc_core.run_summary_coordinator import RunSummaryCoordinator

    level = {"current": "light"}
    cfg, ctx, state = _light_turn(level)
    original = cfg["summary_params"]
    snapshot = dict(original)
    seen = _capture_summarizer(monkeypatch, ctx)

    def provider():
        return resolve_summary_limits_at_level(
            cfg, ctx.model_router, state.turn_level
        )

    first = provider()
    coord = RunSummaryCoordinator(
        ctx=ctx,
        state=state,
        thread_session=None,
        summary_trigger_chars=first.chars_during,
        summary_trigger_tokens=first.tokens_during,
        turn_end_trigger_chars=first.chars_after,
        turn_end_trigger_tokens=first.tokens_after,
        is_cli_mode=False,
        pre_backend_trigger_reason="pre_backend",
        limits_provider=provider,
    )
    with pytest.raises(_SummarizerReached):
        await coord.aforce_compact_once(trigger_reason="t")
    assert seen["summary_params"] is original
    assert cfg["summary_params"] is original
    assert original == snapshot
    assert seen["batch_chars"] == 12


async def test_manual_compaction_uses_the_models_batch(monkeypatch):
    """Ручное сжатие строит координатор БЕЗ провайдера порогов
    (`manual_compaction.py`) — батч модели обязан дойти и по этому пути."""
    from gc_core.run_summary_coordinator import RunSummaryCoordinator

    level = {"current": "light"}
    _cfg_dict, ctx, state = _light_turn(level)
    seen = _capture_summarizer(monkeypatch, ctx)
    coord = RunSummaryCoordinator(
        ctx=ctx,
        state=state,
        thread_session=None,
        summary_trigger_chars=999999,
        is_cli_mode=True,
        pre_backend_trigger_reason="pre_backend",
    )
    with pytest.raises(_SummarizerReached):
        await coord.aforce_compact_once(trigger_reason="manual")
    assert (seen["batch_chars"], seen["max_dialogue_chars"]) == (12, 50)


def test_session_compact_tool_uses_the_models_batch(monkeypatch):
    """Тул зовёт сжатие напрямую, мимо координатора — та же общая точка."""
    import json

    from gc_core.tools.session.session_compact import SessionCompact
    from gc_testkit.helpers.tool_ctx import CallableTool

    level = {"current": "light"}
    _cfg_dict, ctx, state = _light_turn(level)
    seen = _capture_summarizer(monkeypatch, ctx)
    result = json.loads(
        CallableTool(SessionCompact(ctx=ctx, state=state))(reason="фаза")
    )
    assert result["ok"] is False  # суммаризатор в тесте дальше не идёт
    assert (seen["batch_chars"], seen["max_dialogue_chars"]) == (12, 50)


def test_summarizer_limits_prefer_explicit_values_over_the_config():
    """Положительное значение от вызывающего главнее `summary_params`; 0 — «не
    задано», берётся конфиг, а без него — дефолт суммаризатора."""
    from gc_core.summarize.thread_summarizer import _SummaryLimits

    params = {"incremental_batch_chars": 500, "max_dialogue_chars": 900}
    explicit = _SummaryLimits(params, batch_chars=72, max_dialogue_chars=300)
    assert (explicit.batch_chars_limit, explicit.max_dialogue_chars) == (72, 300)
    from_config = _SummaryLimits(params, batch_chars=0, max_dialogue_chars=0)
    assert (from_config.batch_chars_limit, from_config.max_dialogue_chars) == (
        500,
        900,
    )
    defaults = _SummaryLimits({})
    assert (defaults.batch_chars_limit, defaults.max_dialogue_chars) == (
        10**12,
        1000000,
    )
