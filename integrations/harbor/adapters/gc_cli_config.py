"""Build the gc-cli settings (``LoadedSettings``) for a bench run.

There is no YAML on a bench runner, so ``load_settings`` cannot be used — the adapter
assembles the typed settings itself. It starts from the schema defaults (``AppConfig()``
with paths resolved, exactly what an empty config file yields) and layers bench
specifics on top through ``LoadedSettings.with_overrides`` — the same validation and
merge (``config_modules.merge.deep_merge``) every other override path uses:

    schema defaults  <  bench defaults  <  profile gc_cli_config  <  runtime (url/model)

Every layer is validated by ``AppConfig``: a misspelt key or a bad value in a profile
fails the trial at build time instead of being silently ignored by the agent.

The profile's ``gc_cli_config`` keeps its historical shape — the old flat
``config_dict`` names (``max_steps``, ``summary_params``, ``mcp-servers``) — and is
translated through ``FLAT_TO_PATH``; a dotted path (``agent.max_steps``) is taken as
is. Keys that are runtime facts rather than settings (``os_backend``) go to
``RuntimeEnv``.

Kept free of any ``harbor`` import on purpose: the config contract is testable without
the harbor runtime installed.
"""

from __future__ import annotations

import copy
import dataclasses
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from gc_core.config_modules.env_interpolation import resolve_env_placeholders
from gc_core.config_modules.flat_keys import setting_path
from gc_core.config_modules.root import AppConfig, resolve_paths
from gc_core.settings import LoadedSettings, RuntimeEnv

#: Профильные ключи, которые не настройки, а факты запуска (``RuntimeEnv``).
_RUNTIME_KEYS = frozenset(f.name for f in dataclasses.fields(RuntimeEnv))

#: Рантайм бенча: файловые и exec-тулы работают в контейнере задачи.
BENCH_RUNTIME: Dict[str, Any] = {"os_backend": "harbor"}


def schema_defaults(config_dir: str) -> AppConfig:
    """Settings of an empty YAML — every field at its default, paths resolved."""
    return resolve_paths(AppConfig(), config_dir)


def bench_defaults(prompts_dir: Path, skills_dir: Path) -> Dict[str, Any]:
    """What a bench needs differently from a release default (dotted setting paths).

    Generation kwargs belong to the model profile. Open models ship recommended
    sampling values in generation_config.json, which the backend applies when the
    client sends none. Pin any generation override per model in the profile's
    chat_completion_kwargs instead.
    """
    return {
        "backend.api_mode": "chat_completions",
        # Text benches keep their tool set; vision experiments opt in explicitly.
        "backend.vision": False,
        "paths.chat_completions_system_prompt_path": str(
            prompts_dir / "chat_completions_system_prompt.md"
        ),
        "paths.chat_completions_intent_guard_prompt_path": str(
            prompts_dir / "chat_completions_intent_guard_prompt.md"
        ),
        "paths.skills_data_path": str(skills_dir),
        "summary.chat_completions_summary_prompt_path": str(
            prompts_dir / "chat_completions_summary_prompt.md"
        ),
        "summary.chat_completions_memory_patch_replace_prompt_path": str(
            prompts_dir / "chat_completions_memory_patch_replace.md"
        ),
        "backend.use_ssl": False,
        "backend.use_retry": True,
        "agent.system_prompt_id": "v1",
        "logging.save_logs": False,
        "logging.console_trace": False,
        # Guards pinned OFF for benches regardless of release defaults: host-path guards
        # resolve against the RUNNER's fs tree (container paths always "out of perimeter"),
        # and "ask" auto-declines on non-tty == deny. Profiles may re-enable them.
        "security.read_guard.enabled": False,
        "security.write_guard.enabled": False,
        "security.url_policy.enabled": False,
        "security.command_moderation.mode": "allow",
        # Длинные контейнерные сборки/тесты; schema default = 120.
        "agent.exec_timeout_sec": 600,
    }


def profile_updates(
    overrides: Mapping[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Профильный ``gc_cli_config`` → (правки настроек по путям, поля ``RuntimeEnv``).

    Плоские имена прежнего ``config_dict`` переводятся по ``FLAT_TO_PATH``, путь
    с точкой берётся как есть. Секции (``summary_params``) накладываются раньше
    своих листьев (``summary.max_message_chars``), иначе лист затёрла бы секция.
    Значения копируются: сборка не должна менять словарь профиля.
    """
    updates: Dict[str, Any] = {}
    runtime: Dict[str, Any] = {}
    for key, value in overrides.items():
        if key in _RUNTIME_KEYS:
            runtime[key] = value
            continue
        updates[setting_path(key)] = copy.deepcopy(value)
    ordered = dict(sorted(updates.items(), key=lambda kv: kv[0].count(".")))
    return ordered, runtime


def build_gc_cli_settings(
    *,
    completions_url: str,
    model: str,
    prompts_dir: Path,
    skills_dir: Path,
    config_dir: str,
    overrides: Optional[Dict[str, Any]] = None,
) -> LoadedSettings:
    """Assemble the settings a bench run hands to gc-cli."""
    # `config_dir` — якорь умолчаний схемы (как каталог config.yml у загрузчика).
    # В RuntimeEnv его НЕ кладём: относительный путь из профиля раньше доходил до
    # потребителя как есть и читался от cwd раннера — `with_overrides` без
    # `runtime.config_dir` резолвит его от cwd же.
    loaded = LoadedSettings(
        settings=schema_defaults(config_dir),
        runtime=RuntimeEnv(**BENCH_RUNTIME),
    )
    loaded = loaded.with_overrides(
        bench_defaults(prompts_dir, skills_dir), layer="bench"
    )
    # `${ENV:…}` и `${FILE:…}` — как на файловом пути ядра (`load_settings`).
    # Бенч-путь их не разворачивал, и профиль был обязан держать секрет открытым:
    # `agent_kwargs.gc_cli_config` уезжает в `config.json` трайла на диск, а через
    # `--ak` ключ виден ещё и в `ps`. Теперь профиль пишет `"${ENV:GC_API_KEY}"`,
    # а сам ключ живёт в окружении раннера.
    updates, runtime = profile_updates(
        resolve_env_placeholders(dict(overrides or {}))
    )
    loaded = loaded.with_overrides(updates, layer="bench_profile")
    if runtime:
        loaded = loaded.with_runtime(**runtime)
    # Runtime wins: a stale completions_url/model in a profile must not beat what the
    # runner actually passed (api_base / --model).
    return loaded.with_overrides(
        {"backend.completions_url": completions_url, "agent.model": model},
        layer="runtime",
    )
