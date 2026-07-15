"""Long memory compaction: text-in, text-out LLM orchestration.

No file I/O. Caller (the tool) decides what to do with the compressed text.
Used only from the append-overflow path in the project_memory tool.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from bundled_assets import bundled_root
from summarize.llm import request_auxiliary_completion_text

from config_modules.project_memory import DEFAULT_PROJECT_MEMORY_MAX_APPEND_CHARS

SOFT_APPEND_INPUT_LIMIT_CHARS: int = DEFAULT_PROJECT_MEMORY_MAX_APPEND_CHARS

# bundled_root() — единый резолвер ассетов (editable → repo, wheel → site-packages).
_COMPACTION_PROMPT_PATH = bundled_root() / "prompts" / "project_memory_compaction_prompt.md"

_COMPACTION_MAX_TOKENS: int = 4_000  # roughly enough for compressed project_memory.md.


@dataclass(frozen=True)
class CompactionResult:
    ok: bool
    text: str
    error_type: Optional[str]
    error_message: Optional[str]


_FAILURE_BACKEND_UNSUPPORTED = CompactionResult(
    ok=False,
    text="",
    error_type="backend_unsupported",
    error_message=(
        "long memory compaction requires backend_api_mode=chat_completions; "
        "current backend mode does not support text compaction."
    ),
)


def _load_prompt() -> str:
    return _COMPACTION_PROMPT_PATH.read_text(encoding="utf-8")


def compact_text(text: str, ctx: Any, state: Any = None) -> CompactionResult:
    """Compress `text` via backend LLM using the long_memory compaction prompt.

    Returns CompactionResult — never raises. On any failure, the caller's file
    state can stay as-is. `ok=True` means a non-empty compressed string was
    returned by the backend; the caller still validates length against MAX_CHARS.

    Returns ok=false with error_type:
      - "backend_unsupported" — config mode is not chat_completions.
      - "empty_response"      — backend returned empty/whitespace.
      - "backend_error"       — exception during backend call (caught here).
    """
    cfg = getattr(ctx, "config_dict", None) or {}
    mode = str(cfg.get("backend_api_mode", "responses")).strip().lower()
    if mode != "chat_completions":
        return _FAILURE_BACKEND_UNSUPPORTED

    system = _load_prompt()
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": text},
    ]
    try:
        compressed = request_auxiliary_completion_text(
            ctx=ctx,
            state=state,
            messages=messages,
            max_tokens=_COMPACTION_MAX_TOKENS,
            request_label="project_memory_compaction",
        )
    except Exception as exc:  # noqa: BLE001 — caller wants explicit ok=false, not exception.
        return CompactionResult(
            ok=False, text="", error_type="backend_error", error_message=str(exc)
        )

    if not compressed:
        return CompactionResult(
            ok=False,
            text="",
            error_type="empty_response",
            error_message="backend returned empty compaction response.",
        )
    return CompactionResult(
        ok=True, text=compressed, error_type=None, error_message=None
    )
