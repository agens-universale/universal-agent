"""Фасад плагинов для цикла агента.

Цикл зовёт функции: ``asession_start_messages`` (до первого запроса к
модели), ``auser_prompt_messages`` (на сообщении пользователя),
``apre_tool_call`` / ``apost_tool_call`` (до и после инструмента; при ошибке
инструмента вместо PostToolUse идёт PostToolUseFailure) и ``astop_decision``
(перед завершением хода). Без настроенных плагинов все они возвращаются сразу
и ничего не делают.

Хуки вызываются ДО встроенных проверок безопасности инструмента, поэтому плагин
может только запретить или изменить вызов, а не обойти ``security``.

В durable-исполнении (Temporal) хуки отключены: запуск процесса внутри workflow
нарушил бы детерминизм, а портов для этого пока нет.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
from dataclasses import dataclass
from typing import Any

from gc_core.plugins.hook_runner import HookResult, run_hook
from gc_core.plugins.manifest import (
    EVENT_POST_TOOL_USE,
    EVENT_POST_TOOL_USE_FAILURE,
    EVENT_PRE_TOOL_USE,
    EVENT_SESSION_START,
    EVENT_STOP,
    EVENT_USER_PROMPT_SUBMIT,
    Plugin,
    load_plugin,
)

logger = logging.getLogger(__name__)

# Префикс id сообщения SessionStart (см. ``message_id_suffix`` ниже): по нему
# находим уже выданный контекст в истории треда.
SESSION_START_ID_PREFIX = "m_plugin_session_start"
MAX_STOP_CONTINUATIONS = 3
_SESSION_SOURCE = "startup"
BLOCKED_ERROR = "blocked_by_plugin"

_cache: dict[tuple[str, ...], tuple[Plugin, ...]] = {}


@dataclass(frozen=True)
class PreToolDecision:
    arguments: str
    blocked_output: str | None = None


def _plugins_config(ctx: Any) -> Any | None:
    if ctx is None:
        return None
    try:
        from gc_core.settings import settings_of

        return settings_of(ctx).plugins
    except Exception:  # плагины не должны ронять агента
        return None


def configured_paths(cfg: Any, *, fetch: bool = False) -> list[str]:
    """Каталоги плагинов: ``paths`` и ``sources`` из конфига плюс установленные
    командой ``gc-plugin install`` (включённые). Git-источники клонируются
    только при ``fetch=True`` (загрузка настроек), иначе берётся кэш."""
    paths = list(cfg.paths) if cfg is not None else []
    sources = list(getattr(cfg, "sources", ()) or ())
    if sources:
        from gc_core.plugins.sources import resolve_sources

        paths += resolve_sources(sources, fetch=fetch)
    if getattr(cfg, "use_installed", True):
        try:
            from gc_core.plugins.installed import enabled_paths

            paths += [p for p in enabled_paths() if p not in paths]
        except Exception:  # хранилище не должно ронять агента
            logger.warning("installed plugins unavailable", exc_info=True)
    return paths


def load_plugins(paths: list[str]) -> tuple[Plugin, ...]:
    key = tuple(paths)
    if key not in _cache:
        unique: dict[str, Plugin] = {}
        for path in paths:
            plugin = load_plugin(path)
            # одноимённый плагин берётся один раз (первый из списка)
            if plugin is not None and plugin.name not in unique:
                unique[plugin.name] = plugin
        _cache[key] = tuple(unique.values())
    return _cache[key]


def reset_plugin_cache() -> None:
    """Сбросить кэш прочитанных плагинов (для тестов и перезагрузки)."""
    from gc_core.plugins import sources

    _cache.clear()
    sources._warned.clear()


def active_plugins(ctx: Any) -> tuple[Plugin, ...]:
    if getattr(ctx, "durable_execution", False):
        return ()
    cfg = _plugins_config(ctx)
    paths = configured_paths(cfg)
    return load_plugins(paths) if paths else ()


def plugin_skill_roots(settings: Any) -> list[tuple[str, str]]:
    """``(имя плагина, каталог skills/)`` включённых плагинов для реестра."""
    if settings is None:
        return []
    try:
        from gc_core.settings import settings_of

        cfg = settings_of(settings).plugins
    except Exception:
        return []
    paths = configured_paths(cfg)
    if not paths:
        return []
    return [
        (p.name, str(p.skills_dir))
        for p in load_plugins(paths)
        if p.skills_dir is not None
    ]


def plugin_command_defs(settings: Any) -> list[tuple[str, Any]]:
    """``(имя плагина, PluginCommand)`` команд включённых плагинов."""
    if settings is None:
        return []
    try:
        from gc_core.settings import settings_of

        cfg = settings_of(settings).plugins
    except Exception:
        return []
    paths = configured_paths(cfg)
    if not paths:
        return []
    return [
        (plugin.name, command)
        for plugin in load_plugins(paths)
        for command in plugin.commands
    ]


_COMMAND_RE = re.compile(r"^/([\w.-]+(?::[\w.-]+)?)(?:\s+(.*))?$", re.DOTALL)


def find_command(
    plugins: tuple[Plugin, ...], token: str
) -> tuple[Plugin, Any] | None:
    """Команда по ``плагин:команда`` либо по короткому имени, если оно одно."""
    owner, sep, name = token.partition(":")
    if not sep:
        owner, name = "", token
    found = [
        (plugin, command)
        for plugin in plugins
        if not owner or plugin.name == owner
        for command in plugin.commands
        if command.name == name
    ]
    return found[0] if len(found) == 1 else None


def expand_command(command: Any, arguments: str) -> str:
    """Текст команды с ``$ARGUMENTS`` и ``$1``..``$9`` вместо аргументов."""
    try:
        words = shlex.split(arguments)
    except ValueError:
        words = arguments.split()
    body = command.body.replace("$ARGUMENTS", arguments)
    return re.sub(
        r"\$([1-9])",
        lambda m: (
            words[int(m.group(1)) - 1] if int(m.group(1)) <= len(words) else ""
        ),
        body,
    )


def command_prompt_text(
    plugins: tuple[Plugin, ...], prompt: str
) -> str | None:
    """Если запрос ``/плагин:команда аргументы``, текст команды для модели."""
    match = _COMMAND_RE.match(prompt.strip())
    if not match:
        return None
    found = find_command(plugins, match.group(1))
    if found is None:
        return None
    plugin, command = found
    arguments = (match.group(2) or "").strip()
    return (
        f"[команда плагина {plugin.name}:{command.name}] Пользователь вызвал "
        "команду. Выполни её инструкции для его запроса.\n\n"
        + expand_command(command, arguments)
    )


def _cwd(ctx: Any) -> str:
    import os

    try:
        from gc_core.execution_workspace import launch_root_from_config

        return str(launch_root_from_config(ctx) or os.getcwd())
    except Exception:
        return os.getcwd()


def _base_payload(ctx: Any, state: Any, event: str) -> dict[str, Any]:
    return {
        "hook_event_name": event,
        "session_id": str(getattr(state, "state_id", "")),
        "cwd": _cwd(ctx),
    }


async def _run_event(
    ctx: Any,
    event: str,
    value: str | tuple[str, ...] | None,
    payload: dict[str, Any],
) -> list[HookResult]:
    cfg = _plugins_config(ctx)
    timeout = cfg.hook_timeout_sec if cfg is not None else 30
    results: list[HookResult] = []
    for plugin in active_plugins(ctx):
        hooks: list[Any] = []
        for v in value if isinstance(value, tuple) else (value,):
            hooks.extend(
                h for h in plugin.hooks_for(event, v) if h not in hooks
            )
        for hook in hooks:
            results.append(
                await run_hook(
                    plugin,
                    hook,
                    payload,
                    cwd=_cwd(ctx),
                    default_timeout_sec=timeout,
                )
            )
    return results


async def asession_start_messages(
    state: Any, ctx: Any, started: bool = False
) -> tuple[list[dict[str, Any]], bool]:
    """Сообщения, которые плагины кладут в начало сессии, и флаг «запускали».

    Между ходами «уже было» определяем по истории: State создаётся на каждый
    ход заново, а сообщения плагина сохраняются в тред. Внутри хода
    (``started`` держит цикл) хук без вывода не перезапускается на каждом шаге:
    итого раз на тред, если хук вернул контекст, иначе раз на ход.
    Субагентам хук не запускается.
    """
    if started or _is_subagent(state) or not active_plugins(ctx):
        return [], True
    for message in getattr(state, "messages", None) or []:
        if isinstance(message, dict) and str(message.get("id", "")).startswith(
            SESSION_START_ID_PREFIX
        ):
            return [], True
    payload = {
        **_base_payload(ctx, state, EVENT_SESSION_START),
        "source": _SESSION_SOURCE,
    }
    results = await _run_event(
        ctx, EVENT_SESSION_START, _SESSION_SOURCE, payload
    )
    texts = [r.additional_context for r in results if r.additional_context]
    if not texts:
        return [], True
    from gc_core.message_policy.tool_usage_notice.policy import (
        wrap_thread_memory_technical_user_message,
    )

    return [
        wrap_thread_memory_technical_user_message(
            "\n\n".join(texts), message_id_suffix="plugin_session_start"
        )
    ], True


def _is_subagent(state: Any) -> bool:
    return getattr(state, "parent_state_id", None) is not None


def _blocked_output(result: HookResult, tool_name: str) -> str:
    return json.dumps(
        {
            "ok": False,
            "error": BLOCKED_ERROR,
            "plugin": result.plugin,
            "tool": tool_name,
            "message": result.reason or "Blocked by plugin hook",
        },
        ensure_ascii=False,
    )


def _claude_tool_names(tool_name: str) -> tuple[str, ...]:
    """Все имена инструмента в Claude Code (``Edit``, ``MultiEdit``) или своё."""
    from gc_core.plugins.agents import TOOL_ALIASES

    names = tuple(c for c, n in TOOL_ALIASES.items() if n == tool_name)
    return names or (tool_name,)


def _claude_tool_name(tool_name: str) -> str:
    """Первое имя в Claude Code (``Bash``) — его видит хук в ``tool_name``."""
    return _claude_tool_names(tool_name)[0]


def _match_names(tool_name: str) -> tuple[str, ...]:
    """Имена для matcher: имя агента и все алиасы Claude Code."""
    return (tool_name, *_claude_tool_names(tool_name))


def _tool_input(arguments: str) -> dict[str, Any]:
    try:
        data = json.loads(arguments) if arguments else {}
    except ValueError:
        return {"raw": arguments}
    return data if isinstance(data, dict) else {"raw": arguments}


async def apre_tool_call(
    ctx: Any, state: Any, tool_name: str, arguments: str
) -> PreToolDecision:
    """Хуки ``PreToolUse``: запретить вызов или поменять аргументы."""
    if not active_plugins(ctx):
        return PreToolDecision(arguments=arguments)
    payload = {
        **_base_payload(ctx, state, EVENT_PRE_TOOL_USE),
        "tool_name": _claude_tool_name(tool_name),
        "tool_input": _tool_input(arguments),
    }
    current = arguments
    for result in await _run_event(
        ctx,
        EVENT_PRE_TOOL_USE,
        _match_names(tool_name),
        payload,
    ):
        if result.blocked:
            return PreToolDecision(
                arguments=arguments,
                blocked_output=_blocked_output(result, tool_name),
            )
        if result.updated_input is not None:
            current = json.dumps(result.updated_input, ensure_ascii=False)
            payload["tool_input"] = result.updated_input
    return PreToolDecision(arguments=current)


def _is_failed_output(output: Any) -> bool:
    """Вывод инструмента — JSON с ``ok: false`` (формат ядра для ошибок)."""
    if not isinstance(output, str):
        return False
    try:
        data = json.loads(output)
    except ValueError:
        return False
    return isinstance(data, dict) and data.get("ok") is False


async def apost_tool_call(
    ctx: Any, state: Any, tool_name: str, arguments: str, output: Any
) -> None:
    """``PostToolUse`` / ``PostToolUseFailure``: только наблюдение."""
    if not active_plugins(ctx):
        return
    event = (
        EVENT_POST_TOOL_USE_FAILURE
        if _is_failed_output(output)
        else EVENT_POST_TOOL_USE
    )
    payload = {
        **_base_payload(ctx, state, event),
        "tool_name": _claude_tool_name(tool_name),
        "tool_input": _tool_input(arguments),
        "tool_response": output if isinstance(output, str) else str(output),
    }
    await _run_event(ctx, event, _match_names(tool_name), payload)


def _technical(text: str, suffix: str) -> dict[str, Any]:
    from gc_core.message_policy.tool_usage_notice.policy import (
        wrap_thread_memory_technical_user_message,
    )

    return wrap_thread_memory_technical_user_message(
        text, message_id_suffix=suffix
    )


def _user_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("text")
        )
    return ""


async def auser_prompt_messages(
    state: Any, ctx: Any, messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """``UserPromptSubmit``: контекст плагинов к сообщению пользователя.

    Запрос уже записан в историю, поэтому запретить его хук не может: ответ
    ``decision: block`` превращается в сообщение с причиной для модели.
    Служебные (технические) сообщения хук не запускают.
    """
    if _is_subagent(state) or not messages or not active_plugins(ctx):
        return []
    last = messages[-1]
    if not isinstance(last, dict) or last.get("role") != "user":
        return []
    from gc_core.message_policy.user_message_policy import (
        is_technical_user_message_text,
    )

    prompt = _user_text(last)
    if not prompt or is_technical_user_message_text(prompt):
        return []
    payload = {
        **_base_payload(ctx, state, EVENT_USER_PROMPT_SUBMIT),
        "prompt": prompt,
    }
    texts: list[str] = []
    command_text = command_prompt_text(active_plugins(ctx), prompt)
    if command_text:
        texts.append(command_text)
    for result in await _run_event(
        ctx, EVENT_USER_PROMPT_SUBMIT, None, payload
    ):
        if result.additional_context:
            texts.append(result.additional_context)
        if result.blocked:
            texts.append(
                f"[plugin {result.plugin}] {result.reason or 'blocked'}"
            )
    if not texts:
        return []
    return [_technical("\n\n".join(texts), "plugin_user_prompt")]


async def astop_decision(
    state: Any, ctx: Any, last_message: str | None, used: int
) -> tuple[list[dict[str, Any]], int]:
    """``Stop``: хук может не пустить ход к завершению.

    Возвращает ``(сообщения для продолжения, новый счётчик продолжений)``;
    сообщения пусты, если завершать можно. Счётчик ведёт вызывающий цикл,
    число продолжений ограничено ``MAX_STOP_CONTINUATIONS``, чтобы плагин не
    зациклил агента; в хук уходит ``stop_hook_active``. Субагентам хук не
    запускается.
    """
    if _is_subagent(state) or not active_plugins(ctx):
        return [], used
    payload = {
        **_base_payload(ctx, state, EVENT_STOP),
        "stop_hook_active": used > 0,
        "last_assistant_message": last_message or "",
    }
    reasons: list[str] = []
    for result in await _run_event(ctx, EVENT_STOP, None, payload):
        if result.blocked:
            reasons.append(
                f"[plugin {result.plugin}] {result.reason or 'continue'}"
            )
    if not reasons:
        return [], 0
    if used >= MAX_STOP_CONTINUATIONS:
        logger.warning("plugin Stop hooks exceeded %s continuations", used)
        return [], 0
    return [_technical("\n\n".join(reasons), "plugin_stop")], used + 1


def plugin_mcp_servers(paths: list[str]) -> dict[str, dict[str, Any]]:
    """Серверы ``.mcp.json`` включённых плагинов под именами ``<плагин>_<имя>``."""
    import re

    def safe(text: str) -> str:
        return re.sub(r"[^0-9A-Za-z_-]+", "_", text).strip("_") or "x"

    out: dict[str, dict[str, Any]] = {}
    for plugin in load_plugins(list(paths)):
        for name, spec in plugin.mcp_servers.items():
            out[f"{safe(plugin.name)}_{safe(name)}"] = spec
    return out
