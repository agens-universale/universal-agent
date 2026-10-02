"""Project memory simple tool — controller layer.

The agent sees only three actions: read / append / write. Path, lock, atomicity,
and compaction are entirely under the hood. Errors carry concrete actionable
messages without leaking implementation details.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from gc_core.config_modules.env_vars import gc_paths, project_slug
from gc_core.config_modules.project_memory import ProjectMemoryConfig
from gc_core.constants.tool_names import PROJECT_MEMORY_TOOL_NAME
from gc_core.loop_detector.constants import (
    GENERIC_PROFILE,
    loop_warn_on_repeat,
)
from gc_core.message_policy.project_memory_notice.policy import (
    touch_project_memory_baseline,
)
from gc_core.project_memory import compaction, storage
from gc_core.project_memory.operations import (
    ProjectMemoryReply,
    ProjectMemoryRequest,
)
from gc_core.project_memory.status import fill_percent
from gc_core.project_memory.storage import ProjectMemoryError
from gc_core.settings import settings_of
from gc_core.tools.simple_tool_base import AbstractSimpleTool

_BACKUP_FILE_NAME = "GIGACODE_memory_backup.md"


def backup_path(*, cwd: str | None = None) -> Path:
    """Return the backup path for long memory: <gc_state>/projects/<slug>/GIGACODE_memory_backup.md.

    The backup lives outside the project tree so it survives `rm -rf .gc`.
    """
    slug = project_slug(cwd)
    return gc_paths()["state"] / "projects" / slug / _BACKUP_FILE_NAME


_FILE_NAME = "project_memory.md"

_READ_EMPTY_HINT = "long memory is empty. Use action=write to seed it with important facts you want to remember across sessions."
_AUTO_COMPACTION_HINT = "Memory was compacted to fit the limit. Call action=read if you want to see what was kept."
_HIGH_FILL_THRESHOLD_PERCENT = 70
_HIGH_FILL_HINT = (
    f"Project memory is over {_HIGH_FILL_THRESHOLD_PERCENT}% full. Consider action=write to reorganize it: "
    "remove obsolete details, deduplicate, and restructure into concise sections. "
    "If filesystem or artifact tools are available, move bulky or reference material "
    "outward and keep only a short index with paths here."
)


@loop_warn_on_repeat(**GENERIC_PROFILE)
class ProjectMemory(AbstractSimpleTool):
    tool_name = PROJECT_MEMORY_TOOL_NAME

    def __init__(
        self, ctx: Any = None, state: Any = None, **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self._ctx = ctx
        self._state = state
        gc_dir: Optional[Path] = getattr(ctx, "project_gc_dir", None)
        self._path: Optional[Path] = (
            (Path(gc_dir) / _FILE_NAME) if gc_dir else None
        )
        # Backup path: mirrors primary outside the project tree so it survives rm -rf .gc.
        # cwd is the parent of .gc (project root).
        if (
            gc_dir is not None
            and getattr(ctx, "project_memory_io", None) is None
        ):
            cwd = str(Path(gc_dir).parent)
            self._backup: Optional[Path] = backup_path(cwd=cwd)
        else:
            self._backup = None
        limits = (
            settings_of(ctx).project_memory
            if ctx is not None
            else ProjectMemoryConfig()
        )
        self._max_chars = limits.max_chars
        self._max_append_chars = limits.max_append_chars
        self._baseline_updated = False

    @classmethod
    def tool_description(
        cls, config_dict: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
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
    def prompt_description(
        cls, config_dict: Optional[Dict[str, Any]] = None
    ) -> str:
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

    def _current_length(self) -> int:
        if self._path is None:
            return 0
        info = storage.stat(self._path, backup=self._backup)
        return info.chars if info.exists else 0

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

    def _err(self, *, action: str, error_type: str, message: str) -> str:
        current_length = self._current_length()
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
            )
        )
        if finished.output is None:
            raise ValueError("Project memory finish returned no output")
        return finished.output

    async def _acall_io(
        self, request: ProjectMemoryRequest
    ) -> ProjectMemoryReply:
        port = getattr(self._ctx, "project_memory_io", None)
        if port is None:
            return self.execute_io(request)
        reply = await port(request)
        if reply.schema_version != 1:
            raise ValueError("Unsupported project memory reply")
        if reply.baseline_updated and self._state is not None:
            self._state.project_memory_content_hash = reply.baseline_hash
        return reply

    def execute_io(self, request: ProjectMemoryRequest) -> ProjectMemoryReply:
        """Worker/local file handler. Never calls a model or changes model policy."""
        if request.schema_version != 1:
            raise ValueError("Unsupported project memory request")
        self._baseline_updated = False
        combined = ""
        output = self._reject_unusable(request.action)
        if output is None:
            output, combined = self._dispatch_io(request)
        return ProjectMemoryReply(
            output=output,
            combined=combined,
            baseline_updated=self._baseline_updated,
            baseline_hash=getattr(
                self._state, "project_memory_content_hash", None
            ),
        )

    def _dispatch_io(
        self, request: ProjectMemoryRequest
    ) -> tuple[Optional[str], str]:
        if request.phase == "finish":
            if request.action != "append" or request.compaction is None:
                raise ValueError("Invalid project memory compaction result")
            return self._append_finish(
                request.combined, request.compaction
            ), ""
        if request.phase != "prepare":
            raise ValueError("Unknown project memory phase")
        if request.action == "append":
            return self._append_prepare(request.content)
        return self._dispatch_sync(request.action, request.content), ""

    def _reject_unusable(self, action: str) -> Optional[str]:
        """Отказ, если тулу негде работать. Общий для обоих входов."""
        if self._path is None:
            return self._err(
                action=action,
                error_type="not_configured",
                message="project_memory path is not available in this context.",
            )
        return None

    def _dispatch_sync(self, action: str, content: str) -> str:
        """Ветки без компакции: они одинаковы для синхронного и async входа."""
        if action == "read":
            return self._handle_read()
        if action == "write":
            return self._handle_write(content)
        return self._err(
            action=action,
            error_type="invalid_action",
            message=f"unknown action: {action!r}. expected one of read|append|write.",
        )

    def _sync_baseline_after_mutation(self) -> None:
        if self._ctx is not None and self._state is not None:
            touch_project_memory_baseline(self._state, self._ctx)
            self._baseline_updated = True

    def _handle_read(self) -> str:
        info = storage.stat(self._path, backup=self._backup)
        text = storage.read(self._path, backup=self._backup)
        is_empty = (not info.exists) or (not text.strip())
        self._sync_baseline_after_mutation()
        return self._ok(
            action="read",
            length=info.chars if info.exists else 0,
            lines=info.lines if info.exists else 0,
            content=text if not is_empty else "",
            hint=_READ_EMPTY_HINT if is_empty else None,
        )

    def _append_prepare(self, content: str) -> tuple[Optional[str], str]:
        """``(готовый ответ, текст для компакции)`` — ровно одно из двух значимо.

        Всё, что не зависит от способа дождаться компакции: валидация, чтение
        текущего содержимого и простая дозапись, если всё влезает. Вынесено,
        чтобы у двух входов не разошлись правила — расходились бы молча.
        """
        delta = content if isinstance(content, str) else ""
        delta_stripped = delta.strip()
        if not delta_stripped:
            return self._err(
                action="append",
                error_type="empty_content",
                message="content must be non-empty for append.",
            ), ""
        if len(delta_stripped) > self._max_append_chars:
            return self._err(
                action="append",
                error_type="input_too_long",
                message=(
                    f"delta is {len(delta_stripped)} chars "
                    f"(> {self._max_append_chars}). "
                    "Rewrite more concisely (store facts, not raw text) and retry."
                ),
            ), ""
        existing = storage.read(self._path, backup=self._backup)
        existing_stripped = existing.strip()
        combined = (
            f"{existing_stripped}\n\n{delta_stripped}"
            if existing_stripped
            else delta_stripped
        )
        if len(combined) <= self._max_chars:
            try:
                info = storage.append(
                    self._path,
                    delta_stripped,
                    max_chars=self._max_chars,
                    backup=self._backup,
                )
            except ProjectMemoryError as e:
                return self._err(
                    action="append", error_type=e.code, message=e.message
                ), ""
            self._sync_baseline_after_mutation()
            return self._ok(
                action="append", length=info.chars, lines=info.lines
            ), ""
        # Overflow path: compact existing + delta together; LLM decides what to keep.
        return None, combined

    def _append_finish(self, combined: str, result: Any) -> str:
        """Разобрать итог компакции и записать результат."""
        if not result.ok:
            return self._err(
                action="append",
                error_type="compaction_failed",
                message=result.error_message or "compaction failed",
            )
        if len(result.text) > self._max_chars:
            return self._err(
                action="append",
                error_type="limit_exceeded",
                message="content too large even after compaction.",
            )
        try:
            info = storage.write(
                self._path,
                result.text,
                max_chars=self._max_chars,
                backup=self._backup,
            )
        except ProjectMemoryError as e:
            return self._err(
                action="append", error_type=e.code, message=e.message
            )
        self._sync_baseline_after_mutation()
        return self._ok(
            action="append",
            length=info.chars,
            lines=info.lines,
            compaction_block={
                "performed": True,
                "before_chars": len(combined),
                "after_chars": len(result.text),
            },
            hint=_AUTO_COMPACTION_HINT,
        )

    def _handle_write(self, content: str) -> str:
        text = content if isinstance(content, str) else ""
        text_stripped = text.strip()
        if not text_stripped:
            return self._err(
                action="write",
                error_type="empty_content",
                message="content must be non-empty for write.",
            )
        if len(text_stripped) > self._max_chars:
            return self._err(
                action="write",
                error_type="input_too_long",
                message=(
                    f"content is {len(text_stripped)} chars (> {self._max_chars}). Rewrite more concisely and retry."
                ),
            )
        try:
            info = storage.write(
                self._path,
                text_stripped,
                max_chars=self._max_chars,
                backup=self._backup,
            )
        except ProjectMemoryError as e:
            return self._err(
                action="write", error_type=e.code, message=e.message
            )
        self._sync_baseline_after_mutation()
        return self._ok(action="write", length=info.chars, lines=info.lines)


__all__ = ["ProjectMemory"]
