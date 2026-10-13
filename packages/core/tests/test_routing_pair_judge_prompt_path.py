"""Инструкция судьи пары: путь настраивается как у судьи уровня; когда лестница пар работает
(auto и три модели), без файла — отказ при сборке контекста (спека pair-ladder §4.1)."""
import os

import pytest

from gc_core import run_context as rc
from gc_core.auto_routing.pair_judge import DEFAULT_PROMPT_PATH, prompt_path, require_prompt
from gc_core.auto_routing.policy import RoutingParams
from gc_core.config_modules.root import AppConfig, resolve_paths
from gc_testkit import run_coro

NAME = "chat_completions_routing_pair_judge_prompt.md"
AUTO = RoutingParams(safe_level="auto")


def test_the_default_path_resolves_to_the_bundled_asset(tmp_path):
    cfg = AppConfig.model_validate({})
    cfg = resolve_paths(cfg, str(tmp_path))
    resolved = cfg.paths.chat_completions_routing_pair_judge_prompt_path
    assert resolved.endswith(os.path.join("prompts", NAME)) and os.path.isfile(resolved)


def test_a_configured_path_wins_and_reaches_the_runner(tmp_path):
    own = tmp_path / "own.md"
    own.write_text("CUSTOM", encoding="utf-8")
    cfg = AppConfig.model_validate({"paths": {"chat_completions_routing_pair_judge_prompt_path": str(own)}})
    cfg = resolve_paths(cfg, str(tmp_path))
    assert cfg.paths.chat_completions_routing_pair_judge_prompt_path == str(own)
    assert prompt_path(cfg) == str(own)
    assert prompt_path(AppConfig()) == DEFAULT_PROMPT_PATH


def test_a_missing_prompt_is_an_error_only_under_auto(tmp_path):
    missing = AppConfig.model_validate({
        "backend": {"api_mode": "chat_completions"},
        "models": {"fast": {}, "mid": {}, "big": {}},
        "slots": {"light": "fast", "default": "mid", "max": "big"},
        "paths": {"chat_completions_routing_pair_judge_prompt_path": str(tmp_path / "nope.md")},
    })
    with pytest.raises(ValueError, match="nope.md"):
        require_prompt(missing, AUTO)
    require_prompt(missing, RoutingParams(safe_level="default"))


def test_a_missing_prompt_fails_the_run_context_under_auto(tmp_path):
    raw = {"models": {"fast": {}, "mid": {}, "big": {}}, "slots": {"light": "fast", "default": "mid", "max": "big"},
           "backend": {"api_mode": "chat_completions"}, "level": "auto", "logging": {"save_logs": False},
           "agent": {"auto_routing_safe_level": "auto"}}
    raw["paths"] = {"chat_completions_routing_pair_judge_prompt_path": str(tmp_path / "typo.md")}
    cfg = AppConfig.model_validate(raw)
    with pytest.raises(ValueError, match="typo.md"):
        run_coro(rc.acreate_run_context(cfg, level_provider=lambda: "auto"))


def test_two_models_under_auto_run_without_the_pair_prompt(tmp_path):
    """С двумя моделями `auto` — обычная пара без судьи (`levels.resolve_routing_params`),
    инструкция судьи пары не нужна: её отсутствие прогон не роняет."""
    raw = {"models": {"fast": {}, "mid": {}}, "slots": {"light": "fast", "default": "mid"},
           "backend": {"api_mode": "chat_completions"}, "level": "auto", "logging": {"save_logs": False},
           "agent": {"auto_routing_safe_level": "auto"}}
    raw["paths"] = {"chat_completions_routing_pair_judge_prompt_path": str(tmp_path / "typo.md")}
    cfg = AppConfig.model_validate(raw)
    ctx = run_coro(rc.acreate_run_context(cfg, level_provider=lambda: "auto"))
    assert ctx.routing_params.safe_level == "default"


def test_an_empty_prompt_is_an_error_too(tmp_path):
    empty = tmp_path / "empty.md"
    empty.write_text("  \n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty.md"):
        require_prompt(AppConfig.model_validate({
            "backend": {"api_mode": "chat_completions"},
            "models": {"fast": {}, "mid": {}, "big": {}},
            "slots": {"light": "fast", "default": "mid", "max": "big"},
            "paths": {"chat_completions_routing_pair_judge_prompt_path": str(empty)},
        }), AUTO)


def test_the_prompt_is_checked_before_anything_is_started(tmp_path, monkeypatch):
    """Как проверка хранилища: битый конфиг не должен поднимать MCP и логгер."""
    raw = {"models": {"fast": {}, "mid": {}, "big": {}}, "slots": {"light": "fast", "default": "mid", "max": "big"},
           "backend": {"api_mode": "chat_completions"}, "level": "auto", "logging": {"save_logs": False},
           "agent": {"auto_routing_safe_level": "auto"}}
    raw["paths"] = {"chat_completions_routing_pair_judge_prompt_path": str(tmp_path / "typo.md")}
    cfg = AppConfig.model_validate(raw)

    def started(*a, **k):
        raise AssertionError("сборка контекста началась до проверки инструкции судьи")

    monkeypatch.setattr(rc, "_prepare_run_context", started)
    with pytest.raises(ValueError, match="typo.md"):
        run_coro(rc.acreate_run_context(cfg, level_provider=lambda: "auto"))
