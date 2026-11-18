"""Контекстные хуки плагинов для цикла агента (спека §7).

Цикл зовёт ``acontext_hook_messages`` в двух местах: на сообщении
пользователя (``user_prompt=True``, событие ``UserPromptSubmit``) и перед
каждым запросом к модели (``user_prompt=False``, ``SessionStart``). Хуки
только добавляют технические сообщения; дописывает их в историю цикл.
Запускаются хуки только у доверенных плагинов снимка (``trusted``) и никогда
у субагентов.

«SessionStart уже запускали» определяется по текущей истории: у каждого
плагина свой маркер (id его технического сообщения). После сжатия маркеров
нет, и хук срабатывает снова с ``source="compact"``, в том числе посреди
хода. Хук без вывода маркера не оставляет: такой плагин отмечается в
``State.plugin_hooks_ran_turn`` (сбрасывает ``turn_boundary.begin_turn``),
повтор — раз на ход.

Переезд на middleware (§7.3): ``SessionStart`` → ``before_model``,
``UserPromptSubmit`` → ``before_commit``, оба через ``Command.append``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
from typing import Any

from gc_core.agent_edits.ctx import RUN_MODE_CLI
from gc_core.message_format.serialization import (
    history_has_dialog_summary_message,
)
from gc_core.message_policy.tool_usage_notice.policy import (
    wrap_thread_memory_technical_user_message,
)
from gc_core.message_policy.user_message_policy import (
    last_real_user_text,
    wrap_request_context_message,
)
from gc_core.plugins.hook_runner import HookResult, run_hook
from gc_core.plugins.manifest import (
    EVENT_SESSION_START,
    EVENT_USER_PROMPT_SUBMIT,
    Plugin,
)
from gc_core.runtime_notices import push_runtime_notice
from gc_core.settings import runtime_of, settings_of
from gc_core.startup_notices import Notice

logger = logging.getLogger(__name__)

# Суффикс id сообщения ``SessionStart``:
# ``m_plugin_session_start_<плагин>_<хеш имени>_…``.
_SESSION_START_SUFFIX = "plugin_session_start"
_SOURCE_STARTUP = "startup"
_SOURCE_COMPACT = "compact"
_HOOK_FAILED = "plugin_hook_failed"
# Уже показанные сбои (плагин, событие): уведомление — раз за процесс.
_notified: set[tuple[str, str]] = set()


async def acontext_hook_messages(
    state: Any, ctx: Any, messages: list[dict], *, user_prompt: bool
) -> list[dict]:
    """Технические сообщения контекстных хуков; ``[]`` — добавить нечего."""
    if getattr(state, "parent_state_id", None) is not None:
        return []
    plugins = _trusted_plugins(ctx)
    if not plugins:
        return []
    if user_prompt:
        return await _auser_prompt(state, ctx, plugins, messages)
    return await _asession_start(state, ctx, plugins)


def _trusted_plugins(ctx: Any) -> tuple[Plugin, ...]:
    snapshot = runtime_of(ctx).plugins
    return tuple(
        p for p in snapshot.plugins if p.hooks and p.name in snapshot.trusted
    )


async def _auser_prompt(
    state: Any, ctx: Any, plugins: tuple[Plugin, ...], messages: list[dict]
) -> list[dict]:
    """``UserPromptSubmit``: запрос уже в истории, поэтому ``block`` и код 2
    не запрещают его, а дают модели сообщение с причиной. Технические
    сообщения хук не запускают."""
    prompt = last_real_user_text(messages) if messages else ""
    if not prompt:
        return []
    payload = {
        **_base_payload(ctx, state, EVENT_USER_PROMPT_SUBMIT),
        "prompt": prompt,
    }
    texts: list[str] = []
    for plugin in plugins:
        for result in await _arun(
            ctx, plugin, EVENT_USER_PROMPT_SUBMIT, None, payload
        ):
            texts.extend(_prompt_texts(ctx, result))
    if not texts:
        return []
    return [
        wrap_request_context_message(
            "\n\n".join(texts), kind="plugin_user_prompt"
        )
    ]


def _prompt_texts(ctx: Any, result: HookResult) -> list[str]:
    if result.failed:
        _report_failure(ctx, result.plugin, EVENT_USER_PROMPT_SUBMIT)
        return []
    texts = [result.additional_context] if result.additional_context else []
    if result.blocked:
        texts.append(f"[plugin {result.plugin}] {result.reason or 'blocked'}")
    return texts


async def _asession_start(
    state: Any, ctx: Any, plugins: tuple[Plugin, ...]
) -> list[dict]:
    history = [
        m
        for m in getattr(state, "messages", None) or []
        if isinstance(m, dict)
    ]
    source = (
        _SOURCE_COMPACT
        if history_has_dialog_summary_message(history)
        else _SOURCE_STARTUP
    )
    payload = {
        **_base_payload(ctx, state, EVENT_SESSION_START),
        "source": source,
    }
    ran = state.plugin_hooks_ran_turn
    out: list[dict] = []
    for plugin in plugins:
        if plugin.name in ran or _has_marker(history, plugin.name):
            continue
        results = await _arun(
            ctx, plugin, EVENT_SESSION_START, source, payload
        )
        texts = _session_texts(ctx, results)
        if texts:
            out.append(_technical("\n\n".join(texts), _marker(plugin.name)))
        else:
            ran.add(plugin.name)
    return out


def _session_texts(ctx: Any, results: list[HookResult]) -> list[str]:
    """Контекст ``SessionStart``; код 2 (``blocked``) здесь — сбой хука."""
    texts: list[str] = []
    for result in results:
        if result.failed or result.blocked:
            _report_failure(ctx, result.plugin, EVENT_SESSION_START)
        elif result.additional_context:
            texts.append(result.additional_context)
    return texts


def _safe(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z_-]+", "_", name).strip("_") or "x"


def _marker(plugin: str) -> str:
    """Суффикс id сообщения ``SessionStart`` плагина (маркер в истории).

    Читаемая часть теряет заменённые знаки (``my.plugin`` и ``my_plugin``,
    кириллица целиком), поэтому за ней — короткий хеш точного имени."""
    digest = hashlib.sha256(plugin.encode("utf-8")).hexdigest()[:8]
    return f"{_SESSION_START_SUFFIX}_{_safe(plugin)}_{digest}"


def _has_marker(history: list[dict], plugin: str) -> bool:
    pattern = re.compile(re.escape(f"m_{_marker(plugin)}") + r"_[0-9a-f]{32}")
    return any(pattern.fullmatch(str(m.get("id", ""))) for m in history)


async def _arun(
    ctx: Any,
    plugin: Plugin,
    event: str,
    value: str | None,
    payload: dict[str, Any],
) -> list[HookResult]:
    timeout = settings_of(ctx).plugins.hook_timeout_sec
    return [
        await run_hook(
            plugin,
            hook,
            payload,
            cwd=payload["cwd"],
            default_timeout_sec=timeout,
        )
        for hook in plugin.hooks_for(event, value)
    ]


def _cwd(ctx: Any) -> str:
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


def _technical(text: str, suffix: str) -> dict[str, Any]:
    return wrap_thread_memory_technical_user_message(
        text, message_id_suffix=suffix
    )


def _report_failure(ctx: Any, plugin: str, event: str) -> None:
    """Сбой хука: всегда в лог; пользователю — раз на плагин и событие за
    процесс: в TUI уведомление рантайма, в CLI строка в stderr. У ACP и
    сервера канала уведомлений нет — только лог."""
    logger.warning("plugin %s: %s hook failed", plugin, event)
    if (plugin, event) in _notified:
        return
    _notified.add((plugin, event))
    message = f"Хук {event} плагина {plugin} не сработал — подробности в логе."
    runtime = runtime_of(ctx)
    if getattr(ctx, "is_tui", False) or runtime.is_tui:
        push_runtime_notice(
            Notice("warning", _HOOK_FAILED, message, (plugin,))
        )
    elif runtime.run_mode == RUN_MODE_CLI:
        sys.stderr.write(message + "\n")
