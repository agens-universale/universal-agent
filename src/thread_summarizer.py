import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List

from thread_store import message_text_for_summary
from user_message_policy import is_technical_user_message_text
from utils.connect_backend import (
    create_call_headers,
    request_backend_chat_completions,
)

_INVALID_SUMMARY_PREFIXES = (
    "[tool_call ",
    "[assistant]",
    "[user]",
    "<dialog_summary>",
)
_FALLBACK_SUMMARY = (
    "Automatic summarization produced no valid output. "
    "Continue from the latest confirmed user requirements and implementation state."
)
SUGGESTED_MEMORY_HEADER = "## Suggested memory additions"
_MIN_SUMMARY_CHARS = 200
_MIN_SUMMARY_LINES = 3
_REQUIRED_SECTION_MARKERS = (
    "TASK_AND_CONTEXT",
    "GOALS",
    "PLAN_AND_PROGRESS",
    "NEXT_ACTIONS",
    "SPEC_AND_CONSTRAINTS",
    "RISKS_AND_OPEN_QUESTIONS",
    "IMPORTANT_ARTIFACTS",
)
@dataclass
class MemoryAdditionsResult:
    extracted: str
    request_messages: List[Dict[str, str]]
    raw_response: str


def _default_prompt_path() -> str:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, "..", "prompts", "chat_completions_summary_prompt.md")


def _load_prompt_template(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fd:
        prompt = fd.read().strip()
    if not prompt:
        raise ValueError(f"Summary prompt file is empty: {path}")
    return prompt


def collect_user_messages_from_history(messages: List[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    for msg in messages:
        if msg.get("role") != "user":
            continue
        text = message_text_for_summary(msg).strip()
        if not text:
            continue
        if is_technical_user_message_text(text):
            continue
        if text not in out:
            out.append(text)
    return out


def normalize_user_messages_log(messages: List[str] | None) -> List[str]:
    out: List[str] = []
    if not isinstance(messages, list):
        return out
    for item in messages:
        text = str(item).strip()
        if not text:
            continue
        if is_technical_user_message_text(text):
            continue
        if text not in out:
            out.append(text)
    return out


def _build_history_text(
    messages: List[Dict[str, Any]],
    max_chars: int = 200_000,
    max_message_chars: int = 0,
) -> str:
    parts: List[str] = []
    total = 0
    for idx, msg in enumerate(messages, start=1):
        text = _message_text_limited(msg, max_message_chars).strip()
        if not text:
            continue
        row = f"{idx}. {text}"
        total += len(row) + 1
        if total > max_chars:
            break
        parts.append(row)
    return "\n".join(parts)


def _message_text_limited(message: Dict[str, Any], max_message_chars: int) -> str:
    text = message_text_for_summary(message)
    if max_message_chars > 0:
        return _truncate_text(text, max_message_chars)
    return text


def _trim_history_to_max_dialogue_chars(
    messages: List[Dict[str, Any]],
    *,
    max_dialogue_chars: int,
    max_message_chars: int,
) -> List[Dict[str, Any]]:
    if not messages:
        return []
    if max_dialogue_chars <= 0:
        return messages
    total = 0
    start_idx = len(messages)
    for i in range(len(messages) - 1, -1, -1):
        total += len(_message_text_limited(messages[i], max_message_chars))
        if total > max_dialogue_chars:
            start_idx = i + 1
            break
        start_idx = i
    if start_idx >= len(messages):
        return messages[-1:]
    return messages[start_idx:]


def _trim_original_user_messages_to_max_chars(
    messages: List[str], max_chars: int
) -> List[str]:
    if not messages:
        return []
    if max_chars <= 0:
        return messages
    out: List[str] = []
    used = 0
    for msg in reversed(messages):
        msg_len = len(msg)
        if out and used + msg_len > max_chars:
            break
        out.insert(0, msg)
        used += msg_len
    return out


def _group_into_turns(messages: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    def _is_tool_call(msg: Dict[str, Any]) -> bool:
        return msg.get("type") in {"function_call", "tool_call"}

    def _is_tool_call_output(msg: Dict[str, Any]) -> bool:
        return msg.get("type") in {"function_call_output", "tool_call_output"}

    if not messages:
        return []
    turns: List[List[Dict[str, Any]]] = []
    consumed: set[int] = set()
    fc_to_fco: Dict[int, int] = {}

    for i, msg in enumerate(messages):
        if not _is_tool_call(msg):
            continue
        call_id = msg.get("call_id")
        for j, msg2 in enumerate(messages):
            if j in consumed:
                continue
            if _is_tool_call_output(msg2) and msg2.get("call_id") == call_id:
                fc_to_fco[i] = j
                consumed.add(j)
                break

    i = 0
    consumed.clear()
    while i < len(messages):
        if i in consumed:
            i += 1
            continue
        msg = messages[i]
        msg_type = msg.get("type", "message")
        role = msg.get("role")

        if _is_tool_call(msg):
            if i in fc_to_fco:
                j = fc_to_fco[i]
                turns.append([msg, messages[j]])
                consumed.add(j)
            else:
                turns.append([msg])
            i += 1
            continue

        if _is_tool_call_output(msg):
            turns.append([msg])
            i += 1
            continue

        if role == "user":
            turn: List[Dict[str, Any]] = [msg]
            j = i + 1
            while j < len(messages):
                nxt = messages[j]
                if j in consumed:
                    j += 1
                    continue
                nxt_type = nxt.get("type", "message")
                nxt_role = nxt.get("role")
                if nxt_role == "assistant" and nxt_type == "message":
                    turn.append(nxt)
                    consumed.add(j)
                    j += 1
                    break
                if nxt_role == "user" or nxt_type in {
                    "function_call",
                    "tool_call",
                    "function_call_output",
                    "tool_call_output",
                }:
                    break
                j += 1
            turns.append(turn)
            i = j
            continue

        turns.append([msg])
        i += 1

    return turns


def _split_turns_into_char_batches(
    turns: List[List[Dict[str, Any]]],
    *,
    batch_chars_limit: int,
    max_message_chars: int = 0,
) -> List[List[List[Dict[str, Any]]]]:
    if batch_chars_limit <= 0 or not turns:
        return [turns] if turns else []
    batches: List[List[List[Dict[str, Any]]]] = []
    current: List[List[Dict[str, Any]]] = []
    current_chars = 0
    for turn in turns:
        turn_chars = sum(len(_message_text_limited(msg, max_message_chars)) for msg in turn)
        if current and current_chars + turn_chars > batch_chars_limit:
            batches.append(current)
            current = []
            current_chars = 0
        current.append(turn)
        current_chars += turn_chars
        if current_chars >= batch_chars_limit:
            batches.append(current)
            current = []
            current_chars = 0
    if current:
        batches.append(current)
    return batches


def _truncate_text(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + " ... [TEXT CUTTED]"


def _format_last_dialog_steps(
    messages: List[Dict[str, Any]],
    max_steps: int = 5,
    message_max_chars: int = 5000,
    steps_offset: int = 0,
) -> str:
    turns = _group_into_turns(messages)
    tail = turns[-max_steps:] if len(turns) > max_steps else turns
    start_idx = steps_offset + (len(turns) - len(tail)) + 1
    parts: List[str] = []
    for idx, turn in enumerate(tail, start=start_idx):
        rendered: List[str] = []
        for msg in turn:
            text = message_text_for_summary(msg).strip()
            if text:
                rendered.append(_truncate_text(text, message_max_chars))
        text = "\n".join(rendered).strip()
        if not text:
            continue
        parts.append(f"<turn {idx}>\n{text}\n</turn {idx}>")
    return "\n\n".join(parts)


def build_dialog_summary_content(
    summary_text: str,
    last_dialog_steps: str,
    current_memory: str = "",
) -> str:
    lines: List[str] = ["<summary>"]
    lines.append(summary_text.strip())
    lines.append("</summary>")
    lines.append("")
    lines.append("<current_memory>")
    lines.append((current_memory or "").strip())
    lines.append("</current_memory>")
    lines.append("")
    lines.append("<last_dialog_steps>")
    if last_dialog_steps.strip():
        lines.append(last_dialog_steps.strip())
    lines.append("</last_dialog_steps>")
    lines.append("")
    lines.append(
        "<instruction>Use current_memory above as the up-to-date thread_notes state. "
        "You do not need to call thread_notes/read for this summarized context; append new durable facts to thread_notes when needed.</instruction>"
    )
    return "\n".join(lines)


def _is_valid_summary_text(text: str) -> bool:
    ok, _ = _validate_summary_text(text)
    return ok


def _validate_summary_text(text: str) -> tuple[bool, str]:
    s = (text or "").strip()
    if not s:
        return False, "empty"
    if s.startswith(_INVALID_SUMMARY_PREFIXES):
        return False, "invalid_prefix"
    if len(s) < _MIN_SUMMARY_CHARS:
        return False, "too_short"
    non_empty_lines = [line for line in s.splitlines() if line.strip()]
    if len(non_empty_lines) < _MIN_SUMMARY_LINES:
        return False, "too_few_lines"
    section_hits = 0
    upper_s = s.upper()
    for marker in _REQUIRED_SECTION_MARKERS:
        if marker in upper_s:
            section_hits += 1
    if section_hits < 3:
        return False, "missing_structure"
    return True, "ok"


def _extract_add_to_memory(block: str) -> str:
    match = re.search(r"<add_to_memory>\s*(.*?)\s*</add_to_memory>", block, re.DOTALL | re.IGNORECASE)
    if not match:
        return ""
    return match.group(1).strip()


def _shorten_for_log(text: str, max_chars: int = 500) -> str:
    s = (text or "").strip()
    if len(s) <= max_chars:
        return s
    return f"{s[:max_chars]} ... [TRUNCATED {len(s) - max_chars} chars]"


def _log_llm_prompt_and_result(
    *,
    logger: Any,
    label: str,
    messages: List[Dict[str, str]],
    output_text: str,
) -> None:
    if logger is None:
        return
    system_part = "\n\n".join(
        str(m.get("content", "")) for m in messages if m.get("role") == "system"
    )
    user_part = "\n\n".join(
        str(m.get("content", "")) for m in messages if m.get("role") == "user"
    )
    logger.info(
        "summary_llm_prompt label=%s system=\n%s\n\nuser=\n%s",
        label,
        system_part,
        user_part,
    )
    logger.info("summary_llm_result label=%s output=\n%s", label, output_text or "")


def _request_chat_completion_text(
    *,
    ctx: Any,
    state: Any,
    messages: List[Dict[str, str]],
    max_tokens: int,
    request_label: str = "chat_completion",
) -> str:
    cfg = getattr(ctx, "config_dict", {}) or {}
    payload = {
        "model": getattr(state, "default_model", None) or cfg.get("model"),
        "messages": messages,
        "tools": [],
        "temperature": 0.01,
        "top_p": 0.8,
        "top_k": 20,
        "min_p": 0.0,
        "max_tokens": max_tokens,
        "repetition_penalty": 1.1,
        "separate_reasoning": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    headers = create_call_headers(state, ctx)
    res = request_backend_chat_completions(
        payload,
        headers,
        url=cfg["completions_url"],
        verify=cfg.get("use_ssl", True),
        use_retry=cfg.get("use_retry", False),
        logger=getattr(ctx, "logger", None),
    )
    text = (
        res.get("choices", [{}])[0].get("message", {}).get("content")
        if isinstance(res, dict)
        else ""
    )
    return (text or "").strip()


def suggest_memory_additions(
    *,
    ctx: Any,
    state: Any,
    history_text: str,
    summary_text: str,
    current_memory: str,
) -> MemoryAdditionsResult:
    system = (
        "You are helping enrich long-term session memory for a technical dialogue. "
        "You receive: (1) full dialog history, (2) summary, (3) current memory. "
        "Return EXACTLY one XML tag and nothing else: <add_to_memory>...</add_to_memory>. "
        "Inside the tag, include ONLY durable information that should persist across turns: "
        "stable facts, confirmed decisions, constraints, accepted requirements, key assumptions, "
        "important environment/project details, and explicit user preferences/rules. "
        "Also include meaningful progress state, active plans/next steps, and working hypotheses when they are useful "
        "for continuity and are not already captured in curent_summary/current memory. "
        "Do NOT include transient chatter, noisy tool execution logs, or duplicate/paraphrased content already present "
        "in current_summary/current memory. "
        "Prefer concise bullet points in Markdown. "
        "If there is nothing new worth storing, return an empty tag: <add_to_memory></add_to_memory>."
    )
    user_prompt = (
        "<dialog_history>\n"
        f"{history_text}\n"
        "</dialog_history>\n\n"
        "<current_summary>\n"
        f"{summary_text}\n"
        "</current_summary>\n\n"
        "<current_memory>\n"
        f"{current_memory}\n"
        "</current_memory>\n"
    )
    request_messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_prompt},
    ]
    block = _request_chat_completion_text(
        ctx=ctx,
        state=state,
        messages=request_messages,
        max_tokens=1200,
        request_label="memory_additions",
    )
    logger = getattr(ctx, "logger", None)
    _log_llm_prompt_and_result(
        logger=logger,
        label="memory_additions",
        messages=request_messages,
        output_text=block,
    )
    if logger is not None:
        logger.info(
            "memory_additions_raw_response chars=%s preview=%s",
            len(block or ""),
            _shorten_for_log(block or "", max_chars=500),
        )
    extracted = _extract_add_to_memory(block)
    if logger is not None:
        logger.info(
            "memory_additions_extracted chars=%s preview=%s",
            len(extracted or ""),
            _shorten_for_log(extracted or "", max_chars=500),
        )
    return MemoryAdditionsResult(
        extracted=extracted,
        request_messages=request_messages,
        raw_response=block,
    )


def _compress_summary_text(
    *,
    ctx: Any,
    state: Any,
    summary_text: str,
    max_chars: int,
    max_tokens: int,
) -> str:
    if not summary_text or len(summary_text) <= max_chars:
        return summary_text
    system = (
        "You are a summarization assistant. Compress the given summary while preserving "
        "all critical facts, constraints, decisions, and technical references. "
        "Output only compressed summary text."
    )
    request_messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": summary_text},
    ]
    text = _request_chat_completion_text(
        ctx=ctx,
        state=state,
        messages=request_messages,
        max_tokens=max_tokens,
        request_label="summary_compress",
    )
    _log_llm_prompt_and_result(
        logger=getattr(ctx, "logger", None),
        label="summary_compress",
        messages=request_messages,
        output_text=text,
    )
    text = (text or "").strip()
    if not text:
        return summary_text[:max_chars]
    if len(text) > max_chars:
        return _truncate_text(text, max_chars)
    return text


def _repair_summary_text(
    *,
    ctx: Any,
    state: Any,
    history_text: str,
    user_requests_text: str,
    invalid_summary: str,
    reason: str,
    max_tokens: int,
) -> str:
    system = (
        "You are fixing an invalid technical summary for a software-engineering dialogue. "
        "You receive the original user requests log, full dialogue history, and an invalid summary draft. "
        "Produce a corrected summary that is concise, complete, structured, and actionable."
    )
    user = (
        f"Previous summary is invalid. Reason: {reason}.\n\n"
        "<user_requests_log>\n"
        f"{user_requests_text}\n"
        "</user_requests_log>\n\n"
        "<dialog_history>\n"
        f"{history_text}\n"
        "</dialog_history>\n\n"
        "Rewrite summary using the real context above. It must be high-quality, concise, and structured. "
        "Use section headers: TASK_AND_CONTEXT, GOALS, DECISIONS_AND_RATIONALE, "
        "PLAN_AND_PROGRESS, NEXT_ACTIONS, SPEC_AND_CONSTRAINTS, RISKS_AND_OPEN_QUESTIONS, IMPORTANT_ARTIFACTS.\n\n"
        "<invalid_summary>\n"
        f"{invalid_summary}\n"
        "</invalid_summary>"
    )
    request_messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    out = _request_chat_completion_text(
        ctx=ctx,
        state=state,
        messages=request_messages,
        max_tokens=max_tokens,
        request_label="summary_repair",
    )
    _log_llm_prompt_and_result(
        logger=getattr(ctx, "logger", None),
        label="summary_repair",
        messages=request_messages,
        output_text=out,
    )
    return out


def summarize_dialog_via_chat_completions(
    *,
    ctx: Any,
    state: Any,
    full_history_messages: List[Dict[str, Any]],
    current_memory: str = "",
    user_messages_log: List[str] | None = None,
    max_tokens: int = 2500,
    steps_offset: int = 0,
) -> Dict[str, Any]:
    cfg = getattr(ctx, "config_dict", {}) or {}
    logger = getattr(ctx, "logger", None)
    trace: Dict[str, Any] = {
        "step1_batches": [],
        "step2_memory_update": {
            "executed": False,
            "input": {},
            "output": "",
        },
        "final_output": {},
    }
    if not full_history_messages:
        if logger is not None:
            logger.info("summary_empty_history: no messages to summarize")
        summary_content = build_dialog_summary_content(
            summary_text="",
            last_dialog_steps="",
            current_memory=current_memory,
        )
        return {
            "raw_summary": "",
            "summary_message_content": summary_content,
            "user_messages_log": [],
            "memory_additions": "",
            "last_dialog_steps": "",
            "num_batches": 0,
            "trace": trace,
        }
    summary_params = cfg.get("summary_params") if isinstance(cfg.get("summary_params"), dict) else {}
    try:
        last_steps_count = int(summary_params.get("last_dialog_steps_count", 5))
    except (TypeError, ValueError):
        last_steps_count = 5
    try:
        last_step_message_max_chars = int(
            summary_params.get("last_dialog_step_message_max_chars", 5000)
        )
    except (TypeError, ValueError):
        last_step_message_max_chars = 5000
    if last_steps_count <= 0:
        last_steps_count = 5
    if last_step_message_max_chars <= 0:
        last_step_message_max_chars = 5000
    try:
        max_message_chars = int(summary_params.get("max_message_chars", 50000))
    except (TypeError, ValueError):
        max_message_chars = 50000
    try:
        max_dialogue_chars = int(summary_params.get("max_dialogue_chars", 1000000))
    except (TypeError, ValueError):
        max_dialogue_chars = 1000000
    if max_message_chars <= 0:
        max_message_chars = 50000
    if max_dialogue_chars <= 0:
        max_dialogue_chars = 1000000
    try:
        max_accumulated_summary_chars = int(
            summary_params.get("max_accumulated_summary_chars", 20000)
        )
    except (TypeError, ValueError):
        max_accumulated_summary_chars = 20000
    if max_accumulated_summary_chars <= 0:
        max_accumulated_summary_chars = 20000
    try:
        original_user_messages_max_chars = int(
            summary_params.get("original_user_messages_max_chars", 20000)
        )
    except (TypeError, ValueError):
        original_user_messages_max_chars = 20000
    if original_user_messages_max_chars <= 0:
        original_user_messages_max_chars = 20000
    # Размер батча для инкрементальной суммаризации.
    # По умолчанию нормализуется в config.py как int(trigger * 1.4).
    try:
        batch_chars_limit = int(
            summary_params.get("incremental_batch_chars", 0)
        )
    except (TypeError, ValueError):
        batch_chars_limit = 0
    if batch_chars_limit <= 0:
        batch_chars_limit = 10**12
    prompt_path = summary_params.get("chat_completions_summary_prompt_path") or _default_prompt_path()
    system_prompt = _load_prompt_template(str(prompt_path))

    history_for_summary = _trim_history_to_max_dialogue_chars(
        full_history_messages,
        max_dialogue_chars=max_dialogue_chars,
        max_message_chars=max_message_chars,
    )
    user_requests = normalize_user_messages_log(user_messages_log)
    user_requests = _trim_original_user_messages_to_max_chars(
        user_requests, original_user_messages_max_chars
    )
    user_requests_text = (
        "\n".join(f"- {x}" for x in user_requests) if user_requests else "- not found"
    )
    batches = _split_turns_into_char_batches(
        _group_into_turns(history_for_summary),
        batch_chars_limit=batch_chars_limit,
        max_message_chars=max_message_chars,
    )
    if not batches:
        batches = [_group_into_turns(history_for_summary)]
    if logger is not None:
        logger.info(
            "summary_start messages_total=%s messages_after_trim=%s batches=%s trigger_prompt=%s",
            len(full_history_messages),
            len(history_for_summary),
            len(batches),
            prompt_path,
        )
    accumulated_summary = ""
    for batch_index, batch_turns in enumerate(batches, start=1):
        batch: List[Dict[str, Any]] = [m for turn in batch_turns for m in turn]
        history_text = _build_history_text(
            batch, max_message_chars=max_message_chars
        )
        if logger is not None:
            logger.info(
                "summary_batch_start batch=%s/%s batch_messages=%s batch_chars=%s",
                batch_index,
                len(batches),
                len(batch),
                len(history_text),
            )
        if batch_index == 1:
            user_prompt = (
                "<user_requests_log>\n"
                f"{user_requests_text}\n"
                "</user_requests_log>\n\n"
                "<dialog_history>\n"
                f"{history_text}\n"
                "</dialog_history>\n\n"
                "Produce the structured summary now."
            )
        else:
            user_prompt = (
                "<user_requests_log>\n"
                f"{user_requests_text}\n"
                "</user_requests_log>\n\n"
                "<previous_summary>\n"
                f"{accumulated_summary}\n"
                "</previous_summary>\n\n"
                "<dialog_history_batch>\n"
                f"{history_text}\n"
                "</dialog_history_batch>\n\n"
                "Update previous_summary using this batch. "
                "Keep all important facts/decisions/constraints from both."
            )
        request_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        trace_batch: Dict[str, Any] = {
            "batch_index": batch_index,
            "batch_total": len(batches),
            "input_messages": request_messages,
            "input_system_prompt": system_prompt,
            "input_user_prompt": user_prompt,
            "output_summary_text": "",
        }
        if logger is not None:
            logger.info(
                "summary_batch_request_full_messages batch=%s/%s messages_full=\n%s",
                batch_index,
                len(batches),
                request_messages,
            )
        text = _request_chat_completion_text(
            ctx=ctx,
            state=state,
            messages=request_messages,
            max_tokens=max_tokens,
            request_label=f"summary_batch_{batch_index}",
        )
        _log_llm_prompt_and_result(
            logger=logger,
            label=f"summary_batch_{batch_index}",
            messages=request_messages,
            output_text=text,
        )
        trace_batch["output_summary_text"] = text
        if logger is not None:
            logger.info(
                "summary_batch_result batch=%s/%s summary_text=\n%s",
                batch_index,
                len(batches),
                text,
            )
        is_valid, reason = _validate_summary_text(text)
        if not is_valid:
            if logger is not None:
                logger.warning(
                    "summary_batch_invalid batch=%s/%s reason=%s text_chars=%s text_preview=%s",
                    batch_index,
                    len(batches),
                    reason,
                    len(text or ""),
                    _shorten_for_log(text or "", max_chars=500),
                )
                logger.info(
                    "summary_batch_repair_start batch=%s/%s reason=%s history_chars=%s user_requests_chars=%s invalid_summary_chars=%s",
                    batch_index,
                    len(batches),
                    reason,
                    len(history_text),
                    len(user_requests_text),
                    len(text or ""),
                )
            repaired = _repair_summary_text(
                ctx=ctx,
                state=state,
                history_text=history_text,
                user_requests_text=user_requests_text,
                invalid_summary=text,
                reason=reason,
                max_tokens=max_tokens,
            )
            repaired_valid, _ = _validate_summary_text(repaired)
            text = repaired if repaired_valid else (accumulated_summary or _FALLBACK_SUMMARY)
            trace_batch["repair"] = {
                "reason": reason,
                "repaired_text": repaired,
                "repaired_valid": repaired_valid,
                "final_text_after_repair": text,
            }
            if logger is not None:
                logger.info(
                    "summary_batch_repair_result batch=%s/%s repaired_valid=%s repaired_chars=%s final_chars=%s final_preview=%s",
                    batch_index,
                    len(batches),
                    repaired_valid,
                    len(repaired or ""),
                    len(text or ""),
                    _shorten_for_log(text or "", max_chars=500),
                )
        accumulated_summary = text
        trace["step1_batches"].append(trace_batch)
        if len(accumulated_summary) > max_accumulated_summary_chars:
            if logger is not None:
                logger.info(
                    "summary_batch_compress batch=%s/%s before_chars=%s max_chars=%s",
                    batch_index,
                    len(batches),
                    len(accumulated_summary),
                    max_accumulated_summary_chars,
                )
            accumulated_summary = _compress_summary_text(
                ctx=ctx,
                state=state,
                summary_text=accumulated_summary,
                max_chars=max_accumulated_summary_chars,
                max_tokens=max_tokens,
            )
    text = accumulated_summary or _FALLBACK_SUMMARY

    history_text = _build_history_text(
        history_for_summary, max_message_chars=max_message_chars
    )
    # Как и в референсе: второй такт апдейта памяти делаем только
    # когда суммаризация прошла в один батч.
    memory_additions = ""
    if len(batches) <= 1:
        trace["step2_memory_update"] = {
            "executed": True,
            "input": {
                "history_text": history_text,
                "summary_text": text,
                "current_memory": current_memory,
            },
            "output": "",
            "llm": {},
        }
        memory_result = suggest_memory_additions(
            ctx=ctx,
            state=state,
            history_text=history_text,
            summary_text=text,
            current_memory=current_memory,
        )
        if isinstance(memory_result, str):
            memory_result = MemoryAdditionsResult(
                extracted=memory_result,
                request_messages=[],
                raw_response="",
            )
        memory_additions = memory_result.extracted
        trace["step2_memory_update"]["output"] = memory_additions
        trace["step2_memory_update"]["llm"] = {
            "request_messages": memory_result.request_messages,
            "raw_response": memory_result.raw_response,
            "extracted": memory_result.extracted,
        }
    else:
        trace["step2_memory_update"] = {
            "executed": False,
            "input": {
                "history_text": history_text,
                "summary_text": text,
                "current_memory": current_memory,
                "reason": "skipped_for_multi_batch",
            },
            "output": "",
        }
    if logger is not None:
        logger.info(
            "summary_finish batches=%s raw_summary_chars=%s memory_additions_chars=%s",
            len(batches),
            len(text),
            len(memory_additions or ""),
        )
        logger.info("summary_final_raw_text=\n%s", text)
    summary_text = text
    if memory_additions:
        summary_text = (
            f"{text}\n\n{SUGGESTED_MEMORY_HEADER}\n"
            f"<add_to_memory>\n{memory_additions}\n</add_to_memory>"
        )
    if logger is not None:
        logger.info("summary_final_content_text=\n%s", summary_text)

    last_dialog_steps = _format_last_dialog_steps(
        history_for_summary,
        max_steps=last_steps_count,
        message_max_chars=min(last_step_message_max_chars, max_message_chars),
        steps_offset=max(0, int(steps_offset or 0)),
    )
    summary_content = build_dialog_summary_content(
        summary_text=summary_text,
        last_dialog_steps=last_dialog_steps,
        current_memory=current_memory,
    )
    trace["final_output"] = {
        "raw_summary": text,
        "memory_additions": memory_additions,
        "summary_message_content": summary_content,
    }
    return {
        "raw_summary": text,
        "summary_message_content": summary_content,
        "user_messages_log": user_requests,
        "memory_additions": memory_additions,
        "last_dialog_steps": last_dialog_steps,
        "num_batches": len(batches),
        "trace": trace,
    }

