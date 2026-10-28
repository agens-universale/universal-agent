"""Project memory simple tool — controller layer.

The agent sees only three actions: read / append / write. Path, lock, atomicity,
and compaction are entirely under the hood. Errors carry concrete actionable
messages without leaking implementation details.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from gc_core.agent_store import (
    MemoryConflict,
    agent_store_of,
    project_key_of,
)
from gc_core.agent_store import ProjectMemory as StoredMemory
from gc_core.config_modules.project_memory import ProjectMemoryConfig
from gc_core.constants.tool_names import PROJECT_MEMORY_TOOL_NAME
from gc_core.execution.ports import port as runtime_port
from gc_core.loop_detector.constants import (
    GENERIC_PROFILE,
    loop_warn_on_repeat,
)
from gc_core.message_policy.project_memory_notice.policy import (
    touch_project_memory_baseline,
)
from gc_core.project_memory import compaction
from gc_core.project_memory.operations import (
    ProjectMemoryReply,
    ProjectMemoryRequest,
)
from gc_core.project_memory.status import fill_percent
from gc_core.settings import settings_of
from gc_core.tools.simple_tool_base import AbstractSimpleTool
from gc_core.utils.user_text import unreadable_reason

_READ_EMPTY_HINT = "long memory is empty. Use action=write to seed it with important facts you want to remember across sessions."
_AUTO_COMPACTION_HINT = "Memory was compacted to fit the limit. Call action=read if you want to see what was kept."
_CONFLICT_MESSAGE = (
    "project memory was changed concurrently by another session. "
    "Call action=read and retry."
)
_HIGH_FILL_THRESHOLD_PERCENT = 70
_HIGH_FILL_HINT = (
    f"Project memory is over {_HIGH_FILL_THRESHOLD_PERCENT}% full. Consider action=write to reorganize it: "
    "remove obsolete details, deduplicate, and restructure into concise sections. "
    "If filesystem or artifact tools are available, move bulky or reference material "
    "outward and keep only a short index with paths here."
)


@loop_warn_on_repeat(**GENERIC_PROFILE)
class ProjectMemory(AbstractSimpleTool):
    # Тело исполняется в ядре (оркестрация или чистое состояние), не на
    # воркере (execution.tool_effects.uses_leaf_effect).
    effect_in_core = True
    tool_name = PROJECT_MEMORY_TOOL_NAME

    def __init__(
        self, ctx: Any = None, state: Any = None, **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self._ctx = ctx
        self._state = state
        # Память — в хранилище агента (у файлового — <проект>/.gc/
        # project_memory.md с бэкапом вне дерева проекта).
        self._project = project_key_of(ctx) if ctx is not None else None
        self._store = agent_store_of(ctx) if ctx is not None else None
        limits = (
            settings_of(ctx).project_memory
            if ctx is not None
            else ProjectMemoryConfig()
        )
        self._max_chars = limits.max_chars
        self._max_append_chars = limits.max_append_chars
        self._baseline_updated = False

    @classmethod
    def tool_description(cls, settings: Any = None) -> Dict[str, Any]:
        return {
            "type": "function",
            "name": "project_memory",
            "description": (
                "Project/long memory: durable facts for FUTURE sessions ('remember this rule', conventions, "
                "artifact-first preferences, stable decisions). Not for turn-by-turn chat progress — use thread memory. "
                "Survives restarts. Actions: read, append (short delta), write (full replace / full overwrite; rare). "
                "Reports fill percent. "
                "Append verified bullets only; no secrets, chain-of-thought, or guesses as facts."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["read", "append", "write"],
                        "description": (
                            "read = return the current memory content; "
                            "append = add ONLY the new delta at end; "
                            "write = fully replace the memory (full overwrite; rare)."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "Markdown text for append/write. "
                            "Required for every action; ignored for read. "
                            "For append: ONLY the new delta. "
                            "For write: the FULL updated markdown (full overwrite). "
                            'For read: pass empty string "".'
                        ),
                    },
                },
                "required": ["action", "content"],
                "additionalProperties": False,
            },
            "strict": True,
        }

    @classmethod
    def prompt_description(cls, settings: Any = None) -> str:
        return """Tool name: project_memory
Tool description: Persistent project-scoped markdown memory. Survives CLI restarts.
Use read when Project Info reports long memory and conventions or durable facts may matter.
Append short factual deltas; write (full overwrite) only for rare full replacements.

What to put in (any task domain):
- Purpose/scope, standing requirements, conventions, stable decisions, preferences.
- Verified reference facts, names, dates, constraints, gotchas. Short bullets.

What NOT to put in:
- Long brainstorming or step-by-step reasoning.
- Guesses presented as facts.
- Transient turn-only state or ongoing dialogue (use thread memory for that).

Input schema: {"type":"object","properties":{"action":{"type":"string","enum":["read","append","write"]},"content":{"type":"string"}},"required":["action"],"additionalProperties":false}

Return format (JSON string):
{
  "tool":"project_memory",
  "ok": true|false,
  "action": "read|append|write",
  "length": number,
  "lines": number,
  "max_chars": number,
  "fill_percent": number,
  "max_append_chars": number,
  "error": null|{"type":"...","message":"..."},
  "content": "..." (only for read),
  "compaction": {"performed": bool, "before_chars"?: number, "after_chars"?: number},
  "hint": "..." (when relevant; includes reorganize nudge above 70% fill)
}
"""

    def _usage_fields(self, length: int) -> Dict[str, int]:
        return {
            "max_chars": self._max_chars,
            "fill_percent": fill_percent(length, self._max_chars),
            "max_append_chars": self._max_append_chars,
        }

    async def _amemory(self) -> Optional[StoredMemory]:
        """Память для ответа модели: не читается — как пустая (как раньше)."""
        try:
            return await self._store.read_memory(self._project)
        except (OSError, UnicodeDecodeError):
            return None

    async def _alength(self) -> int:
        if self._project is None:
            return 0
        memory = await self._amemory()
        return len(memory.text) if memory is not None else 0

    def _high_fill_hint(self, length: int) -> Optional[str]:
        if (
            fill_percent(length, self._max_chars)
            > _HIGH_FILL_THRESHOLD_PERCENT
        ):
            return _HIGH_FILL_HINT
        return None

    def _resolve_hint(
        self, length: int, hint: Optional[str] = None
    ) -> Optional[str]:
        hints = [part for part in (hint, self._high_fill_hint(length)) if part]
        return "\n\n".join(hints) if hints else None

    def _ok(
        self,
        *,
        action: str,
        length: int,
        lines: int,
        content: Optional[str] = None,
        compaction_block: Optional[Dict[str, Any]] = None,
        hint: Optional[str] = None,
    ) -> str:
        payload: Dict[str, Any] = {
            "tool": ProjectMemory.registered_tool_name(),
            "ok": True,
            "action": action,
            "length": length,
            "lines": lines,
            "error": None,
            **self._usage_fields(length),
        }
        if content is not None:
            payload["content"] = content
        payload["compaction"] = compaction_block or {"performed": False}
        resolved_hint = self._resolve_hint(length, hint)
        if resolved_hint is not None:
            payload["hint"] = resolved_hint
        return json.dumps(payload, ensure_ascii=False)

    async def _aerr(
        self, *, action: str, error_type: str, message: str
    ) -> str:
        current_length = await self._alength()
        payload: Dict[str, Any] = {
            "tool": ProjectMemory.registered_tool_name(),
            "ok": False,
            "action": action,
            "length": current_length,
            "error": {"type": error_type, "message": message},
            **self._usage_fields(current_length),
        }
        resolved_hint = self._resolve_hint(current_length)
        if resolved_hint is not None:
            payload["hint"] = resolved_hint
        return json.dumps(payload, ensure_ascii=False)

    async def acall(
        self, action: str, content: str = "", **kwargs: Any
    ) -> str:
        """Execute file phases separately from the optional compaction model call."""
        prepared = await self._acall_io(
            ProjectMemoryRequest("prepare", action, content)
        )
        if prepared.output is not None:
            return prepared.output
        result = await compaction.acompact_text(
            prepared.combined, self._ctx, self._state
        )
        finished = await self._acall_io(
            ProjectMemoryRequest(
                "finish",
                action,
                combined=prepared.combined,
                compaction=result,
                base_version=prepared.base_version,
            )
        )
        if finished.output is None:
            raise ValueError("Project memory finish returned no output")
        return finished.output

    async def _acall_io(
        self, request: ProjectMemoryRequest
    ) -> ProjectMemoryReply:
        port = runtime_port(self._ctx, "project_memory_io")
        if port is None:
            return await self.aexecute_io(request)
        reply = await port(request)
        if reply.schema_version != 1:
            raise ValueError("Unsupported project memory reply")
        if reply.baseline_updated and self._state is not None:
            self._state.project_memory_content_hash = reply.baseline_hash
        return reply

    async def aexecute_io(
        self, request: ProjectMemoryRequest
    ) -> ProjectMemoryReply:
        """Worker/local memory handler. Never calls a model or changes model policy."""
        if request.schema_version != 1:
            raise ValueError("Unsupported project memory request")
        self._baseline_updated = False
        combined, base_version = "", None
        output = await self._areject_unusable(request.action)
        if output is None:
            # Удалённую память возвращает из копии только сам тул — как в
            # develop; статус и отпечаток памяти её не трогают.
            if self._project is not None:
                await self._store.restore_memory(self._project)
            output, combined, base_version = await self._adispatch_io(request)
        return ProjectMemoryReply(
            output=output,
            combined=combined,
            base_version=base_version,
            baseline_updated=self._baseline_updated,
            baseline_hash=getattr(
                self._state, "project_memory_content_hash", None
            ),
        )

    async def _adispatch_io(
        self, request: ProjectMemoryRequest
    ) -> tuple[Optional[str], str, Optional[str]]:
        if request.phase == "finish":
            if request.action != "append" or request.compaction is None:
                raise ValueError("Invalid project memory compaction result")
            output = await self._aappend_finish(
                request.combined, request.compaction, request.base_version
            )
            return output, "", None
        if request.phase != "prepare":
            raise ValueError("Unknown project memory phase")
        if request.action == "append":
            return await self._aappend_prepare(request.content)
        return (
            await self._adispatch_simple(request.action, request.content),
            "",
            None,
        )

    async def _areject_unusable(self, action: str) -> Optional[str]:
        """Отказ, если тулу негде работать. Общий для обоих входов."""
        if self._project is None:
            return await self._aerr(
                action=action,
                error_type="not_configured",
                message="project_memory path is not available in this context.",
            )
        return None

    async def _adispatch_simple(self, action: str, content: str) -> str:
        """Ветки без компакции."""
        if action == "read":
            return await self._ahandle_read()
        if action == "write":
            return await self._ahandle_write(content)
        return await self._aerr(
            action=action,
            error_type="invalid_action",
            message=f"unknown action: {action!r}. expected one of read|append|write.",
        )

    def _sync_baseline(self, version: Optional[str]) -> None:
        """Базовая версия подсказки «память изменилась» — та, что тул
        только что прочитал или записал."""
        if self._ctx is not None and self._state is not None:
            touch_project_memory_baseline(
                self._state, self._ctx, get_hash_fn=lambda _ctx: version
            )
            self._baseline_updated = True

    async def _ahandle_read(self) -> str:
        memory = await self._amemory()
        text = memory.text if memory is not None else ""
        is_empty = not text.strip()
        self._sync_baseline(memory.version if memory is not None else None)
        return self._ok(
            action="read",
            length=len(text),
            lines=_count_lines(text),
            content=text if not is_empty else "",
            hint=_READ_EMPTY_HINT if is_empty else None,
        )

    async def _aappend_prepare(
        self, content: str
    ) -> tuple[Optional[str], str, Optional[str]]:
        """``(готовый ответ, текст для компакции, его версия)`` — либо ответ,
        либо текст для компакции.

        Валидация, чтение текущего содержимого и простая дозапись, если всё
        влезает. Чужая запись между чтением и записью — повтор по свежему
        тексту, один раз.
        """
        delta = content if isinstance(content, str) else ""
        delta_stripped = delta.strip()
        if not delta_stripped:
            return (
                await self._aerr(
                    action="append",
                    error_type="empty_content",
                    message="content must be non-empty for append.",
                ),
                "",
                None,
            )
        if len(delta_stripped) > self._max_append_chars:
            return (
                await self._aerr(
                    action="append",
                    error_type="input_too_long",
                    message=(
                        f"delta is {len(delta_stripped)} chars "
                        f"(> {self._max_append_chars}). "
                        "Rewrite more concisely (store facts, not raw text) and retry."
                    ),
                ),
                "",
                None,
            )
        for _attempt in range(2):
            # Дописывание — это read-modify-write: непрочитанное «существующее»
            # ушло бы в память ПУСТЫМ, то есть стёрло бы её. Нечитаемая память
            # — отказ, а не "".
            try:
                memory = await self._store.read_memory(self._project)
            except (OSError, UnicodeDecodeError) as exc:
                return (
                    await self._aerr(
                        action="append",
                        error_type="io_error",
                        message=unreadable_reason(
                            self._store.memory_label(self._project), exc
                        ),
                    ),
                    "",
                    None,
                )
            existing = (memory.text if memory is not None else "").strip()
            version = memory.version if memory is not None else None
            combined = (
                f"{existing}\n\n{delta_stripped}"
                if existing
                else delta_stripped
            )
            if len(combined) > self._max_chars:
                # Переполнение: сжать существующее вместе с дельтой (решает LLM).
                return None, combined, version
            try:
                written = await self._store.write_memory(
                    self._project, combined, expected_version=version
                )
            except MemoryConflict:
                continue
            self._sync_baseline(written.version)
            return (
                self._ok(
                    action="append",
                    length=len(combined),
                    lines=_count_lines(combined),
                ),
                "",
                None,
            )
        return (
            await self._aerr(
                action="append",
                error_type="conflict",
                message=_CONFLICT_MESSAGE,
            ),
            "",
            None,
        )

    async def _aappend_finish(
        self, combined: str, result: Any, base_version: Optional[str]
    ) -> str:
        """Разобрать итог компакции и записать его поверх версии prepare."""
        if not result.ok:
            return await self._aerr(
                action="append",
                error_type="compaction_failed",
                message=result.error_message or "compaction failed",
            )
        if len(result.text) > self._max_chars:
            return await self._aerr(
                action="append",
                error_type="limit_exceeded",
                message="content too large even after compaction.",
            )
        text = result.text.strip()
        if not text:
            return await self._aerr(
                action="append",
                error_type="empty_content",
                message="content must be non-empty for write.",
            )
        try:
            written = await self._store.write_memory(
                self._project, text, expected_version=base_version
            )
        except MemoryConflict:
            # Сжатие считалось по тексту, который уже заменили: повторять его
            # по свежему — новый вызов модели; пусть решит агент.
            return await self._aerr(
                action="append",
                error_type="conflict",
                message=_CONFLICT_MESSAGE,
            )
        self._sync_baseline(written.version)
        return self._ok(
            action="append",
            length=len(text),
            lines=_count_lines(text),
            compaction_block={
                "performed": True,
                "before_chars": len(combined),
                "after_chars": len(result.text),
            },
            hint=_AUTO_COMPACTION_HINT,
        )

    async def _ahandle_write(self, content: str) -> str:
        text = content if isinstance(content, str) else ""
        text_stripped = text.strip()
        if not text_stripped:
            return await self._aerr(
                action="write",
                error_type="empty_content",
                message="content must be non-empty for write.",
            )
        if len(text_stripped) > self._max_chars:
            return await self._aerr(
                action="write",
                error_type="input_too_long",
                message=(
                    f"content is {len(text_stripped)} chars (> {self._max_chars}). Rewrite more concisely and retry."
                ),
            )
        for _attempt in range(2):
            # Полная перезапись: правка «поверх свежего» — тот же текст.
            memory = await self._amemory()
            try:
                written = await self._store.write_memory(
                    self._project,
                    text_stripped,
                    expected_version=memory.version if memory else None,
                )
            except MemoryConflict:
                continue
            self._sync_baseline(written.version)
            return self._ok(
                action="write",
                length=len(text_stripped),
                lines=_count_lines(text_stripped),
            )
        return await self._aerr(
            action="write", error_type="conflict", message=_CONFLICT_MESSAGE
        )


def _count_lines(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


__all__ = ["ProjectMemory"]
