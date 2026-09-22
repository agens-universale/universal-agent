"""Long memory compaction: text-in, text-out LLM orchestration.

No file I/O. Caller (the tool) decides what to do with the compressed text.
Used only from the append-overflow path in the project_memory tool.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from gc_core.bundled_assets import bundled_root
from gc_core.config_modules.project_memory import (
    DEFAULT_PROJECT_MEMORY_MAX_APPEND_CHARS,
    project_memory_from_config_dict,
)
from gc_core.summarize.llm import arequest_auxiliary_completion_text

SOFT_APPEND_INPUT_LIMIT_CHARS: int = DEFAULT_PROJECT_MEMORY_MAX_APPEND_CHARS

# bundled_root() — единый резолвер ассетов (editable → repo, wheel → site-packages).
_COMPACTION_PROMPT_PATH = bundled_root() / "prompts" / "project_memory_compaction_prompt.md"


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


async def acompact_text(text: str, ctx: Any, state: Any = None) -> CompactionResult:
    """Сжать `text` бэкендным LLM по промпту компакции long_memory.

    Основная и единственная реализация; синхронный ``compact_text`` — шим над ней.
    Вызов уходит на асинхронный лист ``arequest_auxiliary_completion_text``, то
    есть без потока из пула: компакция может тянуться десятки секунд, и держать
    на ней воркер незачем.

    Возвращает CompactionResult — никогда не бросает. При любой неудаче файловое
    состояние вызывающего может остаться как есть. ``ok=True`` означает, что
    бэкенд вернул непустую сжатую строку; длину против MAX_CHARS вызывающий
    проверяет сам.

    Возвращает ok=false с error_type:
      - "backend_unsupported" — режим конфига не chat_completions.
      - "empty_response"      — бэкенд вернул пустое/пробельное.
      - "backend_error"       — исключение на вызове бэкенда (ловится здесь).
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
    max_tokens = project_memory_from_config_dict(cfg).compaction_max_tokens
    try:
        compressed = await arequest_auxiliary_completion_text(
            ctx=ctx,
            state=state,
            messages=messages,
            max_tokens=max_tokens,
            request_label="project_memory_compaction",
            consumer="memory_compaction",
        )
    except Exception as exc:
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


