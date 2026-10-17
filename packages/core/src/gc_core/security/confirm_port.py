from __future__ import annotations

import inspect
import sys
from functools import partial
from types import SimpleNamespace
from typing import Any, Callable

from gc_core.constants.tool_names import ASK_HUMAN_TOOL_NAME
from gc_core.execution.human import HumanRequest, HumanRequestInterrupted
from gc_core.execution.ports import port
from gc_core.message_policy.permission_denials import (
    declined_error,
    timed_out_error,
    unattended_error,
)
from gc_core.security.call_scope import current_call
from gc_core.security.permission_store import remembers_similar
from gc_core.settings import runtime_of, settings_of

CONFIRM_ALLOW_REMEMBER = (
    "allow_remember"  # Y — exec + session True + few-shot allowed
)
CONFIRM_ALLOW_ONCE = "allow_once"  # O — разрешить на этот вызов тула
CONFIRM_DENY_REMEMBER = (
    "deny_remember"  # N — block + session False + few-shot denied
)
CONFIRM_DENY_ONCE = "deny_once"  # T — запретить на этот вызов тула
# Таймаут диалога: отказ без записи. Человек «запретить разово» не выбирал —
# тот же ключ спрашивается снова при следующей проверке, даже в этом вызове.
CONFIRM_TIMED_OUT = "timed_out"
#: Исход вопроса, не давшего «да», — для текста отказа (`unconfirmed_error`):
#: человек ответил «нет».
CONFIRM_OUTCOME_DECLINED = "declined"
#: Ответа нет: таймаут, вопрос отменён или клиент не умеет отвечать.
CONFIRM_OUTCOME_TIMED_OUT = "timed_out"
_VALID_CHOICES = {
    CONFIRM_ALLOW_REMEMBER,
    CONFIRM_ALLOW_ONCE,
    CONFIRM_DENY_REMEMBER,
    CONFIRM_DENY_ONCE,
}

# Canon key sets — same physical key on QWERTY and ЙЦУКЕН (TUI event.key + CLI input).
# Н ≠ Latin N; Т on ЙЦУКЕН is physical N, not Latin T.
CONFIRM_KEY_Y = frozenset({"y", "Y", "н", "Н"})  # allow_remember
CONFIRM_KEY_O = frozenset({"o", "O", "щ", "Щ"})  # allow_once
CONFIRM_KEY_N = frozenset({"n", "N", "т", "Т"})  # deny_remember
CONFIRM_KEY_T = frozenset({"t", "T", "е", "Е"})  # deny_once

# Human-facing one-liner (CLI prompt / docs); mirrors banner Y/O/N/T labels.
CONFIRM_CHOICE_HINT = "[Y/Н] и подобные в сессии  [O/Щ] разово  [N/Т] запрет+ в сессии  [T/Е] запрет разово"
# То же для вопросов НЕ про команду (запись, чтение, адрес, MCP, скрипт):
# «подобных» там нет — запоминается ровно это действие, и судье команд ответ
# подсказкой не уходит.
CONFIRM_CHOICE_HINT_PLAIN = "[Y/Н] разрешить в сессии  [O/Щ] разово  [N/Т] запретить в сессии  [T/Е] запрет разово"


def confirm_choice_hint(decision_key: str | None, mode: Any = "edit") -> str:
    """Строка-подсказка ответов под вид вопроса (консольный запрос).

    «И подобные» — ровно там, где ответ действительно уйдёт судье команд
    (`remembers_similar`)."""
    if remembers_similar(decision_key, mode):
        return CONFIRM_CHOICE_HINT
    return CONFIRM_CHOICE_HINT_PLAIN


def call_with_optional(
    fn: Callable[..., Any], *args: Any, **optional: Any
) -> Any:
    """Позвать ``fn``, отдав ему только те необязательные именованные
    параметры, которые он принимает.

    Показ вопроса проходит несколько звеньев (порт → раннер → приложение →
    экран), и у каждого есть упрощённые реализации с короткой сигнатурой.
    Новый необязательный параметр не должен ломать ни одну из них.
    """
    try:
        parameters = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return fn(*args)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters):
        return fn(*args, **optional)
    names = {
        p.name
        for p in parameters
        if p.kind
        in (
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    }
    return fn(*args, **{k: v for k, v in optional.items() if k in names})


# Backward-compat aliases (optional one release): old tui_confirm returns
# once|session|deny — маппим в новые 4-choice ids. CLI input() aliases removed.
_ALIAS_MAP = {
    "once": CONFIRM_ALLOW_ONCE,
    "session": CONFIRM_ALLOW_REMEMBER,
    "deny": CONFIRM_DENY_REMEMBER,
}

# Old constants kept as aliases for external callers (one release).
CONFIRM_ONCE = CONFIRM_ALLOW_ONCE
CONFIRM_SESSION = CONFIRM_ALLOW_REMEMBER
CONFIRM_DENY = CONFIRM_DENY_REMEMBER


def normalize_confirm_key(answer: str | None) -> str:
    """Map a single key / CLI answer to a 4-way choice id (TUI + CLI).

    Accepts Latin/Cyrillic same-key glyphs in either case. Multi-char words
    (yes/no/once) are not accepted — same as TUI single-key handlers.
    Unknown / empty → deny_remember (explicit-safe).
    """
    if answer is None:
        return CONFIRM_DENY_REMEMBER
    token = str(answer).strip()
    if not token:
        return CONFIRM_DENY_REMEMBER
    # Single key only (TUI parity). Full-width / accidental paste → deny_remember.
    if len(token) != 1:
        return CONFIRM_DENY_REMEMBER
    if token in CONFIRM_KEY_Y:
        return CONFIRM_ALLOW_REMEMBER
    if token in CONFIRM_KEY_O:
        return CONFIRM_ALLOW_ONCE
    if token in CONFIRM_KEY_N:
        return CONFIRM_DENY_REMEMBER
    if token in CONFIRM_KEY_T:
        return CONFIRM_DENY_ONCE
    return CONFIRM_DENY_REMEMBER


def coerce_confirm_choice(choice: Any) -> str:
    """Normalize UI/port return value to a valid 4-way id."""
    if choice == CONFIRM_TIMED_OUT:
        return CONFIRM_TIMED_OUT
    if choice in _VALID_CHOICES:
        return choice  # type: ignore[return-value]
    if isinstance(choice, str) and choice in _ALIAS_MAP:
        return _ALIAS_MAP[choice]
    return CONFIRM_DENY_REMEMBER


def _ask_human_enabled(ctx: Any) -> bool:
    stop = settings_of(ctx).agent.stop_tools or []
    return ASK_HUMAN_TOOL_NAME not in stop


def confirm_available(ctx: Any) -> bool:
    """Можно ли спросить человека.

    Объявление канала (`RuntimeEnv.human_channel`) сильнее догадок: ACP —
    человек в IDE, сервер и A2A — никого. Не объявлено — автопроверка:
    порт рантайма, TUI, терминал при включённом `turn_ask_human` (`gc-cli`).
    """
    declared = runtime_of(ctx).human_channel
    if declared is not None:
        return declared
    if callable(port(ctx, "human_request")):
        return True
    if getattr(ctx, "is_tui", False):
        return True
    if not _ask_human_enabled(ctx):
        return False
    stdin = getattr(sys, "stdin", None)
    return bool(stdin and stdin.isatty())


def unconfirmed_error(ctx: Any, **fields: Any) -> dict:
    """Тело отказа после вопроса, на который не ответили «да».

    Причин три, и человеку с моделью нужно знать, какая: спросить было
    некого (канал без человека — вопрос стал отказом), ответа не пришло
    вовремя (таймаут, отмена, клиент не умеет отвечать) — или человек
    отказал. Исход вопроса лежит в привязке вызова (`CallScope`).
    """
    if not confirm_available(ctx):
        return unattended_error(**fields)
    scope = current_call()
    if getattr(scope, "confirm_outcome", None) == CONFIRM_OUTCOME_TIMED_OUT:
        return timed_out_error(**fields)
    return declined_error(**fields)


def _via_name(ctx: Any) -> str | None:
    name = getattr(ctx, "subagent_tool_name", None)
    return name if isinstance(name, str) and name.strip() else None


def _ask_choice(
    ctx: Any,
    *,
    title: str,
    question: str,
    fields: list,
    decision_key: str | None = None,
) -> str:
    request_port = getattr(ctx, "confirm_request", None)
    if callable(request_port):
        return coerce_confirm_choice(
            request_port(
                HumanRequest(
                    kind="confirm",
                    title=title,
                    question=question,
                    fields=[[str(key), str(value)] for key, value in fields],
                    decision_key=decision_key,
                )
            )
        )
    # Confirm недоступен / AFK-like: block without teaching (deny_once).
    # Explicit invalid UI choice → deny_remember (fail-closed + few-shot).
    if not confirm_available(ctx):
        return CONFIRM_DENY_ONCE

    # Lazy: security → tools на module load ломает import graph (paths/diff_tui).
    # Nested: path_policy → confirm_port cycle if imported at module top.
    from gc_core.execution.progress import emit_progress
    from gc_core.security.path_policy import prompt_user_confirmation_choice

    via = _via_name(ctx)
    # confirm-порт не знает steps child — для progress достаточно нулей
    emit_state = SimpleNamespace(steps=0, max_steps=0)
    emit_progress(
        ctx,
        emit_state,
        "confirm",
        prose=question,
        tool_name=via,
        args_summary=title,
    )
    try:
        if getattr(ctx, "is_tui", False):
            tui_confirm = getattr(ctx, "tui_confirm", None)
            if not callable(tui_confirm):
                # Нет рабочего present → deny_once (не учить few-shot), не CLI input() за Textual.
                return CONFIRM_DENY_ONCE
            choice = call_with_optional(
                partial(
                    tui_confirm, title=title, question=question, fields=fields
                ),
                via=via,
                decision_key=decision_key,
            )
            result = coerce_confirm_choice(choice)
        else:
            # Non-TUI fallback (headless CLI / scripts). Product path is TUI;
            # mode=ask is not the intended CLI UX (allow|deny policy instead),
            # but if confirm is reached, key matrix matches TUI.
            from gc_core.security.permission_mode import resolve_mode

            result = coerce_confirm_choice(
                prompt_user_confirmation_choice(
                    title=title,
                    fields=fields,
                    question=question,
                    decision_key=decision_key,
                    permission_mode=resolve_mode(ctx).value,
                )
            )
    finally:
        emit_progress(
            ctx,
            emit_state,
            "confirm_done",
            tool_name=via,
            args_summary=title,
        )
    return result


def _note_outcome(outcome: str | None) -> None:
    """Исход вопроса — в привязку вызова: по нему `unconfirmed_error`
    выбирает текст отказа. Вне диспетчера привязки нет, и текст не нужен."""
    scope = current_call()
    if scope is not None:
        scope.confirm_outcome = outcome


def _outcome_of(choice: Any) -> str | None:
    """Исход ответа на вопрос; «да» — None."""
    if choice == CONFIRM_TIMED_OUT:
        return CONFIRM_OUTCOME_TIMED_OUT
    if coerce_confirm_choice(choice) in (
        CONFIRM_ALLOW_REMEMBER,
        CONFIRM_ALLOW_ONCE,
    ):
        return None
    return CONFIRM_OUTCOME_DECLINED


def _remembered(store: Any, decision_key: str | None) -> bool | None:
    """Ответ, уже данный на этот ключ (в сессии или в этом вызове тула);
    исход у него тот же, что у самого ответа."""
    if store is None or decision_key is None:
        return None
    cached = store.decision_for(decision_key)
    if cached is not None:
        _note_outcome(None if cached else CONFIRM_OUTCOME_DECLINED)
    return cached


def request_confirm(
    *,
    title: str,
    question: str,
    fields: list[tuple[str, object]],
    ctx: Any,
    decision_key: str | None = None,
) -> bool:
    # Lazy: security → tools на module load ломает import graph.
    from gc_core.tools.connect_mcp.abandonable import current_call_abandoned

    store = getattr(ctx, "permission_store", None)
    cached = _remembered(store, decision_key)
    if cached is not None:
        return cached
    if current_call_abandoned():
        _note_outcome(CONFIRM_OUTCOME_TIMED_OUT)
        return False
    choice = _ask_choice(
        ctx,
        title=title,
        question=question,
        fields=fields,
        decision_key=decision_key,
    )
    if current_call_abandoned():
        # Вопрос бросили (Ctrl+C), пока человек думал: ответ, если он и пришёл,
        # набран уже для ДРУГОГО вопроса. Записать его — значит выдать
        # разрешение на путь, в котором человек только что отказал.
        _note_outcome(CONFIRM_OUTCOME_TIMED_OUT)
        return False
    _note_outcome(_outcome_of(choice))
    return apply_confirm_choice(choice, store=store, decision_key=decision_key)


def apply_confirm_choice(
    choice: Any, *, store: Any, decision_key: str | None
) -> bool:
    """Apply the native four-choice policy after a completed human reply."""
    if choice == CONFIRM_TIMED_OUT:
        # Голый таймаут не выбор человека: ничего не пишем — разовый ответ
        # заглушил бы повтор вопроса до конца вызова тула.
        return False
    choice = coerce_confirm_choice(choice)
    allow = choice in (CONFIRM_ALLOW_REMEMBER, CONFIRM_ALLOW_ONCE)
    if store is not None and decision_key is not None:
        # 2×2 record matrix: (allow, session) per choice.
        #   Y=allow_remember → (True, True)
        #   O=allow_once     → (True, False)
        #   N=deny_remember  → (False, True)
        #   T=deny_once      → (False, False)
        session = choice in (CONFIRM_ALLOW_REMEMBER, CONFIRM_DENY_REMEMBER)
        store.record(decision_key, allow, session=session)
    return allow


async def aconfirm_via_human_port(request: HumanRequest, *, ctx: Any) -> bool:
    """Ответ человека через async-порт рантайма (``ctx.human_request``).

    Это не async-двойник `request_confirm`: тот ждёт локальный UI (TUI-баннер
    или ``input()``) блокирующе в рабочем потоке, а здесь ожидание — durable
    порт Temporal. Общий у них только редьюсер `apply_confirm_choice`.
    Отмена / ``HumanRequestInterrupted`` разрешения не записывают.
    """
    if request.kind != "confirm" or request.schema_version != 1:
        raise ValueError("Unsupported confirmation request")
    port = getattr(ctx, "human_request", None)
    if not callable(port):
        raise ValueError("ctx.human_request port is required")
    store = getattr(ctx, "permission_store", None)
    cached = _remembered(store, request.decision_key)
    if cached is not None:
        return cached
    try:
        choice = await port(request)
    except HumanRequestInterrupted:
        _note_outcome(CONFIRM_OUTCOME_TIMED_OUT)
        return False
    _note_outcome(_outcome_of(choice))
    return apply_confirm_choice(
        choice,
        store=store,
        decision_key=request.decision_key,
    )


def confirmation_step(
    *,
    title: str,
    question: str,
    fields: list[tuple[str, object]],
    ctx: Any,
    decision_key: str | None = None,
    local_request: Callable[..., bool] | None = None,
    local_wait: Callable[[], bool] | None = None,
) -> Any:
    """One native guard request with local UI and durable async drivers."""
    from gc_core.security.dual_wrapper import AwaitOnCaller, OffLoop

    request = HumanRequest(
        kind="confirm",
        title=title,
        question=question,
        fields=[[str(key), str(value)] for key, value in fields],
        decision_key=decision_key,
    )
    if callable(port(ctx, "human_request")):
        return AwaitOnCaller(lambda: aconfirm_via_human_port(request, ctx=ctx))
    wait = local_wait or partial(
        local_request or request_confirm,
        title=title,
        question=question,
        fields=fields,
        ctx=ctx,
        decision_key=decision_key,
    )
    return OffLoop(wait, on_interrupt=False)
