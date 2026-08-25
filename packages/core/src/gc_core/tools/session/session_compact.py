"""Agent-initiated dialog compaction (same pipeline as threshold-triggered summary)."""

from __future__ import annotations

import json
from typing import Any

from gc_core.constants.tool_names import SESSION_COMPACT_TOOL_NAME
from gc_core.summarize.compaction import arun_summary_compaction, without_system_messages
from gc_core.summarize.summary_logging import SummaryCompactionLog, log_compaction_failed, logger_from_ctx
from gc_core.tools.simple_tool_base import AbstractSimpleTool
from gc_core.utils.aio_compat import run_coro_blocking

TRIGGER_REASON = "session_compact"




class SessionCompact(AbstractSimpleTool):
    tool_name = SESSION_COMPACT_TOOL_NAME

    def __init__(self, ctx: Any = None, state: Any = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._ctx = ctx
        self._state = state

    @staticmethod
    def _result(
        *,
        ok: bool,
        message: str | None,
        error: dict | None,
        extra: dict | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "tool": SessionCompact.registered_tool_name(),
            "ok": ok,
            "error": error,
            "message": message,
        }
        if extra:
            payload.update(extra)
        return json.dumps(payload, ensure_ascii=False)

    @classmethod
    def tool_description(cls) -> dict[str, Any]:
        return {
            "type": "function",
            "name": SESSION_COMPACT_TOOL_NAME,
            "description": (
                "Replace the active step-by-step trace with a fresh baseline summary for the same user turn. "
                "When to use: after a major phase or before a new sub-goal "
                "(also when the trace is long or cluttered). "
                "This is trace hygiene, not durable memory and not a full log or archive. "
                "After success, the pre-compaction detailed trace is no longer sent to the model; "
                "the new trace starts with a structured dialog summary that embeds updated "
                "session memory, last_dialog_steps (recent pre-compaction messages), and the current open "
                "user intent—continue from that baseline only. "
                "Before calling, persist important verified facts or artifact paths in session_memory or a file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": (
                            "Short note why compaction is needed now; "
                            "be specific: major phase done, new sub-goal, noisy trace, etc.)."
                        ),
                    },
                },
                "required": ["reason"],
                "additionalProperties": False,
            },
            "strict": True,
        }

    @classmethod
    def prompt_description(cls) -> str:
        return """Tool name: session_compact
Tool description: Replace the active step-by-step trace with a baseline summary for the same user turn—not durable memory and not a full log. When to use: after a major phase or before a new sub-goal (also when the trace is noisy or long). After success, the pre-compaction detailed trace is no longer sent to the model; the new trace is a structured dialog summary with updated thread memory registers, <last_dialog_steps> (recent pre-compaction messages), and current_user_intent for the open turn. Before calling, persist important verified facts or artifact paths in session_memory or a file.
Input schema: {"type":"object","properties":{"reason":{"type":"string","description":"why compact now"}},"required":["reason"],"additionalProperties":false}

Return format (JSON string):
{"tool":"session_compact","ok":true,"error":null|{"type":"...","message":"..."},"message":"...","compacted":true}
"""

    def __call__(self, *args: Any, **kwargs: Any) -> str:
        """Синхронный двойник: прямые вызовы ``apply_tool`` и тесты.

        Реализация одна — ``acall``; здесь только способ её дождаться.
        Без этого синхронный ``apply_tool`` вернул бы строку «tool is async»,
        и агентское сжатие молча перестало бы работать через публичную
        синхронную точку SDK.
        """
        return run_coro_blocking(self.acall(*args, **kwargs))

    async def acall(self, reason: str, **kwargs: Any) -> str:
        """Основная реализация: сжать трассу текущего хода по просьбе агента.

        Диспетчер тулов видит ``acall`` и зовёт его напрямую, без
        ``asyncio.to_thread``: цепочка суммаризации асинхронна до самого
        сетевого листа, поэтому поток пула на десятки секунд больше не
        занимается.
        """
        if not isinstance(reason, str):
            return self._result(
                ok=False,
                message=None,
                error={"type": "invalid_argument", "field": "reason"},
            )
        note = reason.strip()
        if not note:
            return self._result(
                ok=False,
                message=None,
                error={
                    "type": "invalid_argument",
                    "field": "reason",
                    "message": "reason must be a non-empty string.",
                },
            )

        state = self._state
        if state is None:
            return self._result(
                ok=False,
                message=None,
                error={"type": "no_state", "message": "Agent state is not available."},
            )

        backend_mode = str(getattr(state, "backend_api_mode", "") or "").strip().lower()
        if backend_mode != "chat_completions":
            return self._result(
                ok=False,
                message=None,
                error={
                    "type": "unsupported_backend",
                    "message": "session_compact is only available in chat_completions mode.",
                },
            )

        dialog_messages = without_system_messages(getattr(state, "messages", []) or [])
        if not dialog_messages:
            return self._result(
                ok=False,
                message=None,
                error={
                    "type": "nothing_to_compact",
                    "message": "No non-system messages in the trace to summarize.",
                },
            )

        ctx = self._ctx
        on_summary_applied = getattr(ctx, "on_summary_applied", None) if ctx else None
        is_cli_mode = bool(getattr(ctx, "is_cli_mode", False)) if ctx else False
        logger = logger_from_ctx(ctx)

        try:
            await arun_summary_compaction(
                ctx=ctx,
                state=state,
                trigger_reason=TRIGGER_REASON,
                is_cli_mode=is_cli_mode,
                on_summary_applied=on_summary_applied,
                trigger_threshold_chars=None,
            )
        except Exception as exc:
            log_compaction_failed(
                logger,
                SummaryCompactionLog(
                    trigger_reason=TRIGGER_REASON,
                    thread_id=getattr(state, "thread_id", None),
                    is_cli_mode=is_cli_mode,
                ),
                exc,
            )
            return self._result(
                ok=False,
                message=None,
                error={
                    "type": "compaction_failed",
                    "message": str(exc) or "summary compaction failed",
                },
            )

        state.session_compact_applied = True
        msg = f"Compaction succeeded. Reason: {note}"
        return self._result(
            ok=True,
            message=msg,
            error=None,
            extra={"compacted": True},
        )


__all__ = ["SessionCompact", "TRIGGER_REASON"]
