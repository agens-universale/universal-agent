"""Transport-only LLM call for working_memory update step after summary."""

import os
from dataclasses import dataclass
from typing import Any, Dict, List

from summarize.llm import request_summary_text
from summarize.working_memory_gateway import (
    MEMORY_DELTAS_FORMAT_REPAIR_REQUEST_LABEL,
    build_memory_deltas_format_repair_messages,
)
from utils.llm_chat_client import (
    log_llm_prompt_and_result,
    shorten_for_log,
)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_BASE_DIR, "..", ".."))
DEFAULT_MEMORY_UPDATE_PROMPT_PATH = os.path.abspath(
    os.path.join(_PROJECT_ROOT, "prompts", "chat_completions_memory_update_prompt.md")
)


@dataclass
class MemoryUpdateLlmResult:
    """Raw LLM transport result for memory patch step (no parsing here)."""

    request_messages: List[Dict[str, str]]
    raw_response: str


def request_memory_update(
    *,
    ctx: Any,
    state: Any,
    history_text: str,
    summary_text: str,
    persistent_state_block: str,
) -> MemoryUpdateLlmResult:
    cfg = getattr(ctx, "config_dict", {}) or {}
    summary_params = cfg.get("summary_params") if isinstance(cfg.get("summary_params"), dict) else {}
    prompt_path_raw = str(
        summary_params.get("chat_completions_memory_update_prompt_path")
        or DEFAULT_MEMORY_UPDATE_PROMPT_PATH
    ).strip()
    if not prompt_path_raw:
        raise ValueError(
            "summary_params.chat_completions_memory_update_prompt_path is empty"
        )
    prompt_path = (
        prompt_path_raw
        if os.path.isabs(prompt_path_raw)
        else os.path.abspath(os.path.join(_PROJECT_ROOT, prompt_path_raw))
    )
    if not os.path.exists(prompt_path):
        raise FileNotFoundError(
            f"Memory update prompt file not found: {prompt_path}"
        )
    try:
        with open(prompt_path, "r", encoding="utf-8") as fd:
            system = fd.read().strip()
    except OSError as prompt_exc:
        raise OSError(
            f"Failed to read memory update prompt file: {prompt_path}"
        ) from prompt_exc
    if not system:
        raise ValueError(f"Memory update prompt file is empty: {prompt_path}")

    logger = getattr(ctx, "logger", None)
    user_prompt = (
        "<dialog_history>\n"
        f"{history_text}\n"
        "</dialog_history>\n\n"
        "<current_summary>\n"
        f"{summary_text}\n"
        "</current_summary>\n\n"
        f"{persistent_state_block.strip()}\n"
    )
    request_messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_prompt},
    ]
    request_label = "working_memory_update_after_summary"
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
            "working_memory_update_after_summary_raw chars=%s preview=%s",
            len(raw_llm_response or ""),
            shorten_for_log(raw_llm_response or "", max_chars=500),
        )
    return MemoryUpdateLlmResult(
        request_messages=request_messages,
        raw_response=raw_llm_response,
    )


def request_memory_deltas_format_repair(
    *,
    ctx: Any,
    state: Any,
    raw_output: str,
    max_tokens: int | None = None,
) -> str:
    """LLM transport: переформатировать сырой ответ memory-step в register-теги."""
    request_messages = build_memory_deltas_format_repair_messages(raw_output)
    return request_summary_text(
        ctx=ctx,
        state=state,
        messages=request_messages,
        request_label=MEMORY_DELTAS_FORMAT_REPAIR_REQUEST_LABEL,
        max_tokens=max_tokens,
    )
