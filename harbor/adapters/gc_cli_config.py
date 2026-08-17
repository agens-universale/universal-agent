"""Build the gc-cli ``config_dict`` for a bench run.

There is no YAML on a bench runner, so ``load_config`` cannot be used — but the shape it
produces is the contract the agent layer reads. Hand-rolling that dict here meant every
key added to ``AppConfig`` and read hard by a consumer (``cfg["backend_request_timeout_sec"]``)
was simply absent, and the run died with KeyError. So we start from the schema defaults
(the same projection ``load_config`` applies) and layer bench specifics on top:

    schema defaults  <  bench defaults  <  profile gc_cli_config  <  runtime (url/model)

Layering uses ``config_modules.merge.deep_merge`` — the same rules the project-local
``.gc/config.yml`` overlay follows, so a bench profile and a project overlay behave alike.

Kept free of any ``harbor`` import on purpose: the config contract is testable without
the harbor runtime installed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from gc_core.config import flatten_app_config
from gc_core.config_modules.merge import deep_merge
from gc_core.config_modules.root import AppConfig, resolve_paths


def schema_defaults(config_dir: str) -> Dict[str, Any]:
    """Flat config_dict for an empty YAML — every key of the schema, at its default."""
    app_cfg = AppConfig.model_validate({})
    resolve_paths(app_cfg, config_dir)
    return flatten_app_config(app_cfg)


def bench_defaults(prompts_dir: Path, skills_dir: Path) -> Dict[str, Any]:
    """What a bench needs differently from a release default.

    NO sampling defaults (temperature/top_p/top_k): open models ship their recommended
    values in generation_config.json, which the backend applies when the client sends
    none. Hardcoding sampling here would override every model's own recommended params
    (e.g. force MiniMax's 1.0/0.95/40 onto Qwen-Coder, whose card recommends
    0.7/0.8/20/1.05). Pin per-model via the profile's chat_completion_kwargs instead.
    """
    return {
        "backend_api_mode": "chat_completions",
        "chat_completions_system_prompt_path": str(prompts_dir / "chat_completions_system_prompt.md"),
        "chat_completions_intent_guard_prompt_path": str(prompts_dir / "chat_completions_intent_guard_prompt.md"),
        "skills_data_path": str(skills_dir),
        "summary_params": {
            "chat_completions_summary_prompt_path": str(prompts_dir / "chat_completions_summary_prompt.md"),
            "chat_completions_memory_patch_replace_prompt_path": str(prompts_dir / "chat_completions_memory_patch_replace.md"),
        },
        "chat_completions_max_tokens": 32768,
        "use_max_tokens_param": True,
        "chat_completion_kwargs": {
            "chat_template_kwargs": {"enable_thinking": False},
            "tool_choice": "auto",
        },
        "use_ssl": False,
        "use_retry": True,
        "use_local_subagents": False,
        "os_backend": "harbor",
        "system_prompt_id": "v1",
        "no_tool_call_max_count": 3,
        "max_steps": 200,
        "save_logs": False,
        "console_trace": False,
        # Guards pinned OFF for benches regardless of release defaults: host-path guards
        # resolve against the RUNNER's fs tree (container paths always "out of perimeter"),
        # and "ask" auto-declines on non-tty == deny. Profiles may re-enable them.
        "read_guard": {"enabled": False},
        "write_guard": {"enabled": False},
        "url_policy": {"enabled": False},
        "command_moderation": {"mode": "allow"},
        # Длинные контейнерные сборки/тесты; schema default = 120.
        "exec_timeout_sec": 600,
    }


def build_gc_cli_config(
    *,
    completions_url: str,
    model: str,
    prompts_dir: Path,
    skills_dir: Path,
    config_dir: str,
    overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble the config_dict a bench run hands to gc-cli."""
    config = deep_merge(
        schema_defaults(config_dir),
        bench_defaults(prompts_dir, skills_dir),
    )
    config = deep_merge(config, dict(overrides or {}))
    # Runtime wins: a stale completions_url/model in a profile must not beat what the
    # runner actually passed (api_base / --model).
    config["completions_url"] = completions_url
    config["model"] = model
    return config
