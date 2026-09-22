from __future__ import annotations

from gc_core.utils.aio_compat import await_callback
import json
import logging
from typing import Any, Callable, Dict, List, Optional

from gc_core.intent_guard.agent_logging import log_finish_gate_output_patched
from gc_core.intent_guard.guard import FinishGateDecision
from gc_core.tools.turn.submit_solution import SubmitSolution

_logger = logging.getLogger(__name__)


def get_function_call_output_text(messages: List[Dict], call_id: str) -> Optional[str]:
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if message.get("type") != "function_call_output":
            continue
        if message.get("call_id") != call_id:
            continue
        output = message.get("output")
        return output if isinstance(output, str) else None
    return None


def patch_function_call_output(
    messages: List[Dict],
    *,
    call_id: str,
    new_output: str,
) -> Optional[Dict]:
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if message.get("type") != "function_call_output":
            continue
        if message.get("call_id") != call_id:
            continue
        message["output"] = new_output
        return message
    return None


async def apply_finish_gate_to_turn_finish_output_in_state(
    *,
    state: Any,
    finish_call_id: str,
    decision: FinishGateDecision,
    logger: Any | None = None,
    persist_messages_callback: Optional[Callable[[List[Dict]], None]] = None,
) -> bool:
    """
    Merge finish-gate decision into the persisted finish-turn tool output envelope.
    """
    log = logger if logger is not None else _logger
    raw_output = get_function_call_output_text(state.messages, finish_call_id)
    if not isinstance(raw_output, str) or not raw_output.strip():
        log.warning(
            "finish_gate finish_turn_output_patch_skipped call_id=%s reason=no_output",
            finish_call_id,
        )
        return False

    patched_output = SubmitSolution.apply_finish_gate_decision(raw_output, decision)
    updated_message = patch_function_call_output(
        state.messages,
        call_id=finish_call_id,
        new_output=patched_output,
    )
    if updated_message is None:
        log.warning(
            "finish_gate finish_turn_output_patch_skipped call_id=%s reason=message_not_found",
            finish_call_id,
        )
        return False

    output_ok: bool | None = None
    output_status: str | None = None
    try:
        payload = json.loads(patched_output)
        if isinstance(payload, dict):
            output_ok = payload.get("ok") if isinstance(payload.get("ok"), bool) else None
            status_raw = payload.get("status")
            output_status = status_raw if isinstance(status_raw, str) else None
    except json.JSONDecodeError:
        pass

    log_finish_gate_output_patched(
        log,
        call_id=finish_call_id,
        outcome=decision.outcome,
        output_ok=output_ok,
        output_status=output_status,
    )

    if persist_messages_callback is not None:
        try:
            await await_callback(persist_messages_callback([updated_message]))
        except Exception as exc:
            log.warning(
                "finish_gate finish_turn_output_persist_warning call_id=%s error=%s",
                finish_call_id,
                exc,
                exc_info=True,
            )
    return True
