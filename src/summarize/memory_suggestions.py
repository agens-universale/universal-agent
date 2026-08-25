"""Transport-only LLM calls for working_memory patch steps after summary."""

import os
from dataclasses import dataclass
from typing import Any, Dict, List

from summarize.llm import request_summary_text
from summarize.prompt_loading import (
    load_memory_patch_replace_prompt,
)
from summarize.working_memory_gateway import (
    MEMORY_DELTAS_FORMAT_REPAIR_REQUEST_LABEL,
    build_memory_deltas_format_repair_messages,
)
from bundled_assets import bundled_root

# bundled_root() — единый резолвер ассетов (editable → repo, wheel → site-packages).
_PROJECT_ROOT = str(bundled_root())
DEFAULT_MEMORY_PATCH_REPLACE_PROMPT_PATH = os.path.abspath(
    os.path.join(_PROJECT_ROOT, "prompts", "chat_completions_memory_patch_replace.md")
)


@dataclass
class MemoryPatchLlmResult:
    """LLM transport result for a memory patch step (no parsing here).

    ``raw_response`` is already ``for_model``-projected (aux choke in
    ``request_summary_text``); dirty text is logged there, not here.
    """

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
    # Dirty logged inside request_summary_text / aux choke; return is projected.
    projected = request_summary_text(
        ctx=ctx,
        state=state,
        messages=request_messages,
        request_label=request_label,
        consumer="summary",
        logger=logger,
    )
    return MemoryPatchLlmResult(
        request_messages=request_messages,
        raw_response=projected,
    )


def _is_subagent_state(state: Any) -> bool:
    """Child agent State has parent_state_id; main orchestrator does not."""
    return getattr(state, "parent_state_id", None) is not None


def request_patch_replace(
    *,
    ctx: Any,
    state: Any,
    history_text: str,
    summary_text: str,
    persistent_state_block: str = "",
) -> MemoryPatchLlmResult:
    """LLM step: full replace snapshots for all working_memory registers."""
    prompt_path = _resolve_prompt_path(
        ctx,
        "chat_completions_memory_patch_replace_prompt_path",
        DEFAULT_MEMORY_PATCH_REPLACE_PROMPT_PATH,
    )
    # Soft-only: subagent addendum lives in system prompt only (no user duplicate).
    system = load_memory_patch_replace_prompt(
        prompt_path, subagent=_is_subagent_state(state)
    )
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
        consumer="summary",
        max_tokens=max_tokens,
    )
