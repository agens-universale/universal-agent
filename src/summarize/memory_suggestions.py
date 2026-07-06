"""Transport-only LLM calls for working_memory patch steps after summary."""

import os
from dataclasses import dataclass
from typing import Any, Dict, List

from summarize.llm import request_summary_text
from summarize.prompt_loading import (
    load_memory_patch_append_prompt,
    load_memory_patch_replace_prompt,
)
from summarize.working_memory_gateway import (
    MEMORY_DELTAS_FORMAT_REPAIR_REQUEST_LABEL,
    build_memory_deltas_format_repair_messages,
)
from utils.llm_chat_client import (
    log_llm_prompt_and_result,
    shorten_for_log,
)
from bundled_assets import bundled_root

# bundled_root() — единый резолвер ассетов (editable → repo, wheel → site-packages).
_PROJECT_ROOT = str(bundled_root())
DEFAULT_MEMORY_PATCH_APPEND_PROMPT_PATH = os.path.abspath(
    os.path.join(_PROJECT_ROOT, "prompts", "chat_completions_memory_patch_append.md")
)
DEFAULT_MEMORY_PATCH_REPLACE_PROMPT_PATH = os.path.abspath(
    os.path.join(_PROJECT_ROOT, "prompts", "chat_completions_memory_patch_replace.md")
)


@dataclass
class MemoryPatchLlmResult:
    """Raw LLM transport result for a memory patch step (no parsing here)."""

    request_messages: List[Dict[str, str]]
    raw_response: str


def _resolve_prompt_path(ctx: Any, config_key: str, default_path: str) -> str:
    cfg = getattr(ctx, "config_dict", {}) or {}
    summary_params = cfg.get("summary_params") if isinstance(cfg.get("summary_params"), dict) else {}
    prompt_path_raw = str(summary_params.get(config_key) or default_path).strip()
    if not prompt_path_raw:
        raise ValueError(f"summary_params.{config_key} is empty")
    if os.path.isabs(prompt_path_raw):
        return prompt_path_raw
    return os.path.abspath(os.path.join(_PROJECT_ROOT, prompt_path_raw))


def _request_patch(
    *,
    ctx: Any,
    state: Any,
    system_prompt: str,
    user_prompt: str,
    request_label: str,
) -> MemoryPatchLlmResult:
    logger = getattr(ctx, "logger", None)
    request_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    raw_llm_response = request_summary_text(
        ctx=ctx,
        state=state,
        messages=request_messages,
        request_label=request_label,
    )
    log_llm_prompt_and_result(
        logger=logger,
        label=request_label,
        messages=request_messages,
        output_text=raw_llm_response,
    )
    if logger is not None:
        logger.info(
            "%s_raw chars=%s preview=%s",
            request_label,
            len(raw_llm_response or ""),
            shorten_for_log(raw_llm_response or "", max_chars=500),
        )
    return MemoryPatchLlmResult(
        request_messages=request_messages,
        raw_response=raw_llm_response,
    )


def request_patch_append(
    *,
    ctx: Any,
    state: Any,
    history_text: str,
    summary_text: str,
    persistent_state_block: str,
) -> MemoryPatchLlmResult:
    """Legacy optional prompt (not used by default summarization pipeline)."""
    prompt_path = _resolve_prompt_path(
        ctx,
        "chat_completions_memory_patch_append_prompt_path",
        DEFAULT_MEMORY_PATCH_APPEND_PROMPT_PATH,
    )
    system = load_memory_patch_append_prompt(prompt_path)
    user_prompt = (
        "<dialog_history>\n"
        f"{history_text}\n"
        "</dialog_history>\n\n"
        "<current_summary>\n"
        f"{summary_text}\n"
        "</current_summary>\n\n"
        f"{persistent_state_block.strip()}\n"
    )
    return _request_patch(
        ctx=ctx,
        state=state,
        system_prompt=system,
        user_prompt=user_prompt,
        request_label="working_memory_patch_append_after_summary",
    )


def request_patch_replace(
    *,
    ctx: Any,
    state: Any,
    history_text: str,
    summary_text: str,
    persistent_state_block: str = "",
) -> MemoryPatchLlmResult:
    """LLM step 2: full replace snapshots for all working_memory registers."""
    prompt_path = _resolve_prompt_path(
        ctx,
        "chat_completions_memory_patch_replace_prompt_path",
        DEFAULT_MEMORY_PATCH_REPLACE_PROMPT_PATH,
    )
    system = load_memory_patch_replace_prompt(prompt_path)
    user_prompt = (
        "<dialog_history>\n"
        f"{history_text}\n"
        "</dialog_history>\n\n"
        "<current_summary>\n"
        f"{summary_text}\n"
        "</current_summary>\n\n"
        f"{str(persistent_state_block or '').strip()}\n"
    )
    return _request_patch(
        ctx=ctx,
        state=state,
        system_prompt=system,
        user_prompt=user_prompt,
        request_label="working_memory_patch_replace_after_summary",
    )


def request_memory_deltas_format_repair(
    *,
    ctx: Any,
    state: Any,
    raw_output: str,
    max_tokens: int | None = None,
    full_replace: bool = False,
) -> str:
    """LLM transport: переформатировать сырой ответ memory-step в register-теги."""
    request_messages = build_memory_deltas_format_repair_messages(
        raw_output, full_replace=full_replace
    )
    return request_summary_text(
        ctx=ctx,
        state=state,
        messages=request_messages,
        request_label=MEMORY_DELTAS_FORMAT_REPAIR_REQUEST_LABEL,
        max_tokens=max_tokens,
    )
