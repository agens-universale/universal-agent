"""Build the gc-cli settings (``LoadedSettings``) for a bench run.

There is no YAML on a bench runner, so ``load_settings`` cannot be used — the adapter
assembles the typed settings itself. It starts from the schema defaults (``AppConfig()``
with paths resolved, exactly what an empty config file yields) and layers bench
specifics on top through ``LoadedSettings.with_overrides`` — the same validation as
every other override path; a value replaces the one below at its path (sections are
expanded into leaves, so other fields stay; dict values and lists are replaced whole):

    schema defaults  <  bench defaults  <  profile gc_cli_config  <  runtime (url/model)

Every layer is validated by ``AppConfig``: a misspelt key or a bad value in a profile
fails the trial at build time instead of being silently ignored by the agent.

The profile's ``gc_cli_config`` keys are setting paths — dotted (``agent.max_steps``)
and/or nested sections (``summary: {max_message_chars: …}``), exactly as in
``config.yml``. The old flat ``config_dict`` names (``max_steps``, ``summary_params``)
are gone: an unknown name fails the build with a path hint. Keys that are runtime
facts rather than settings (``os_backend``) go to ``RuntimeEnv``.

Kept free of any ``harbor`` import on purpose: the config contract is testable without
the harbor runtime installed.
"""

from __future__ import annotations

import copy
import dataclasses
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from gc_core.config_modules.compat import apply_compat, manual_edit_fact
from gc_core.config_modules.env_interpolation import resolve_env_placeholders
from gc_core.config_modules.model_registry import validate_registry_pairs
from gc_core.config_modules.path_hints import check_setting_path
from gc_core.config_modules.root import AppConfig, resolve_paths
from gc_core.settings import (
    LoadedSettings,
    RuntimeEnv,
    expand_section_updates,
)

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
    client sends none. Pin generation overrides per model in the profile's
    main and auxiliary sections instead.
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
        "paths.chat_completions_routing_pair_judge_prompt_path": str(
            prompts_dir / "chat_completions_routing_pair_judge_prompt.md"
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
        "os_backend.kind": "harbor",
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

    Ключ — путь через точку (``agent.max_steps``) или вложенная секция
    (``summary: {...}``). Профиль сливается с умолчаниями бенча (правка по
    пути заменяет значение), поэтому секции раскладываются на листья
    (``expand_section_updates``): заданное заменяется, прочее остаётся.
    Незнакомое схеме имя (прежнее плоское ``max_steps``) —
    ``UnknownSettingError`` с подсказкой пути. Значения копируются: сборка не
    должна менять словарь профиля.
    """
    updates: Dict[str, Any] = {}
    runtime: Dict[str, Any] = {}
    for key, value in overrides.items():
        if key in _RUNTIME_KEYS:
            runtime[key] = value
            continue
        updates[key] = copy.deepcopy(value)
    expanded = expand_section_updates(updates)
    for path in expanded:
        check_setting_path(path)
    return expanded, runtime


def profile_value(overrides: Mapping[str, Any], path: str) -> Any:
    """Значение настройки ``path`` из профильного ``gc_cli_config`` (путь через
    точку или вложенная секция); не задано — ``None``. Без валидации: адаптеру
    нужно сырое значение профиля (``agent.model``, ``paths.skills_data_path``)."""
    if path in overrides:
        return overrides[path]
    parts = path.split(".")
    for depth in range(len(parts) - 1, 0, -1):
        node: Any = overrides.get(".".join(parts[:depth]))
        for part in parts[depth:]:
            if not isinstance(node, Mapping) or part not in node:
                node = None
                break
            node = node[part]
        if node is not None:
            return node
    return None


def build_gc_cli_settings(
    *,
    completions_url: str,
    model: str,
    prompts_dir: Path,
    skills_dir: Path,
    logs_dir: Path,
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
    # Compat работает с файловым документом; профиль принимает также dotted-пути.
    profile: Dict[str, Any] = {}
    runtime_overrides: Dict[str, Any] = {}
    for path, value in (overrides or {}).items():
        if path in _RUNTIME_KEYS:
            runtime_overrides[path] = copy.deepcopy(value)
            continue
        node = profile
        parts = path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = copy.deepcopy(value)
    raw_profile = copy.deepcopy(profile)
    profile = resolve_env_placeholders(profile)
    validate_registry_pairs(profile, source="gc_cli_config", raw_source=raw_profile)
    compat_facts = apply_compat(profile)
    updates, runtime = profile_updates({
        **expand_section_updates(profile), **resolve_env_placeholders(runtime_overrides),
    })
    loaded = loaded.with_overrides(updates, layer="bench_profile")
    if runtime:
        loaded = loaded.with_runtime(**runtime)
    if compat_facts:
        loaded = loaded.with_runtime(
            notices=(*loaded.runtime.notices, *(
                f"бенч-профиль: {manual_edit_fact(fact)}" for fact in compat_facts
            ))
        )
    # Runtime wins: a stale completions_url/model in a profile must not beat what the
    # runner actually passed (api_base / --model).
    return loaded.with_overrides(
        {
            "backend.completions_url": completions_url,
            "agent.model": model,
            "paths.logs_dump_dir": str(logs_dir),
        },
        layer="runtime",
    )
