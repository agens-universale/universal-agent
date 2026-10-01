"""Bind the shared loop to the explicitly supported deployment profile.

This module and the workflow import no tool implementations or network clients.
A branch that needs a later integration step fails instead of falling back to I/O.
"""

import asyncio
import inspect
import json
from types import SimpleNamespace
from typing import Any, Dict, List

from gc_core.config_modules.gc import gc_force_collect_enabled
from gc_core.config_modules.strip_tags_projection import (
    projection_tags_from_config,
)
from gc_core.constants.tool_names import (
    ASK_HUMAN_TOOL_NAME,
    FINISH_TOOL_NAME,
    INTERMEDIATE_REPLY_TOOL_NAME,
    SESSION_COMPACT_TOOL_NAME,
    SESSION_TODO_TOOL_NAME,
    TURN_FINALIZING_TOOLS,
)
from gc_core.execution.arguments_error import WrongFunctionArgumentsException
from gc_core.execution.constants import (
    _MAX_CONTEXT_OVERFLOW_RETRIES,
    _SESSION_COMPACT_RESUME_USER_TEXT,
    _TRUNCATED_ARGUMENTS_HINT,
)
from gc_core.execution.contracts import ExecutionFailure, ToolInvocation
from gc_core.execution.finish_reason import (
    FINISH_KIND_LENGTH,
    classify_finish_kind,
    finish_blocks_final,
    finish_prevents_text_final,
    is_context_overflow_error,
)
from gc_core.execution.message_keys import filter_messages_not_in_state
from gc_core.execution.model import arequest_model
from gc_core.execution.output_wrapping import maybe_store_and_wrap_tool_output
from gc_core.execution.policy import ExecutionPolicy
from gc_core.execution.preserved_batch import build_preserved_batch
from gc_core.execution.progress import (
    brief_args_summary,
    emit_progress,
    extract_intermediate_prose,
    parse_tool_result_status,
    todo_items_from_arguments,
)
from gc_core.execution.reducers import (
    apply_state_delta,
    tool_state_view,
)
from gc_core.execution.tool_feedback import (
    is_internal_tool_error_output,
    is_unknown_tool_output,
)
from gc_core.intent_guard.guard import FinishGateGuard
from gc_core.intent_guard.text_answer_judge import ashould_return_text_answer
from gc_core.intent_guard.turn_finish_emit import (
    apply_finish_gate_to_turn_finish_output_in_state,
    get_function_call_output_text,
)
from gc_core.intent_scope.final_answer import amaybe_record_final_answer_intent
from gc_core.intent_scope.hook import amaybe_update_intent_scope_from_tool
from gc_core.loop_detector.detector import clear as clear_loop_detector
from gc_core.loop_detector.detector import flush as flush_loop_detector
from gc_core.loop_detector.detector import observe as observe_loop_detector
from gc_core.message_policy.no_tool_call_bounce import (
    NO_TOOL_CALL_BOUNCE_MESSAGE,
    extract_last_assistant_text,
    format_no_tool_call_bounce,
)
from gc_core.message_policy.stop_persist import is_persist_discarded
from gc_core.message_policy.user_message_policy import (
    TECHNICAL_TYPE_ERROR_MESSAGE,
    TECHNICAL_TYPE_TECHNICAL,
    build_invalid_tool_call_feedback_message,
    wrap_technical_user_content,
)
from gc_core.message_policy.user_stop import (
    not_started_due_to_user_stop_output,
)
from gc_core.summarize.limit_trace_summary import abuild_limit_envelope
from gc_core.turn_boundary import begin_turn
from gc_core.usage.account import UsageAccount
from gc_core.utils.aio_compat import await_callback
from gc_core.utils.convertations import sanitize_messages_for_chat_completions
from gc_core.utils.format_messages import format_message_responses
from gc_core.utils.generate_message_id import (
    generate_msg_id_sha256 as generate_message_id_func,
)
from gc_core.utils.logging_module import human_console_enabled


def unsupported(*_args, **_kwargs):
    raise ExecutionFailure(
        "This operation is outside the Temporal execution profile"
    )


async def no_child_trace(ctx, **_kwargs):
    if getattr(ctx, "parent_call_id", None):
        unsupported()


async def request_backend(
    messages, state, ctx, *, set_previous_response_id=True
):
    result = await arequest_model(
        ctx.operations,
        state,
        messages,
        consumer="main",
        set_previous_response_id=set_previous_response_id,
    )
    return result.messages, result.response_id or "", result.response_metadata


def build_bindings(logger):
    bindings = SimpleNamespace()
    policy = ExecutionPolicy(bindings)
    bindings.ASK_HUMAN_TOOL_NAME = ASK_HUMAN_TOOL_NAME
    bindings.Any = Any
    bindings.Dict = Dict
    bindings.ExecutionFailure = ExecutionFailure
    bindings.FINISH_KIND_LENGTH = FINISH_KIND_LENGTH
    bindings.FINISH_TOOL_NAME = FINISH_TOOL_NAME
    bindings.FinishGateGuard = FinishGateGuard
    bindings.INTERMEDIATE_REPLY_TOOL_NAME = INTERMEDIATE_REPLY_TOOL_NAME
    bindings.List = List
    bindings.NO_TOOL_CALL_BOUNCE_MESSAGE = NO_TOOL_CALL_BOUNCE_MESSAGE
    bindings.SESSION_COMPACT_TOOL_NAME = SESSION_COMPACT_TOOL_NAME
    bindings.SESSION_TODO_TOOL_NAME = SESSION_TODO_TOOL_NAME
    bindings.TECHNICAL_TYPE_ERROR_MESSAGE = TECHNICAL_TYPE_ERROR_MESSAGE
    bindings.TECHNICAL_TYPE_TECHNICAL = TECHNICAL_TYPE_TECHNICAL
    bindings.TURN_FINALIZING_TOOLS = TURN_FINALIZING_TOOLS
    bindings.ToolInvocation = ToolInvocation
    bindings.UsageAccount = UsageAccount
    bindings.WrongFunctionArgumentsException = WrongFunctionArgumentsException
    bindings._MAX_CONTEXT_OVERFLOW_RETRIES = _MAX_CONTEXT_OVERFLOW_RETRIES
    bindings._SESSION_COMPACT_RESUME_USER_TEXT = (
        _SESSION_COMPACT_RESUME_USER_TEXT
    )
    bindings._TRUNCATED_ARGUMENTS_HINT = _TRUNCATED_ARGUMENTS_HINT
    bindings._aask_backend_and_record = policy._aask_backend_and_record
    bindings._abefore_tool_call = policy._abefore_tool_call
    bindings._acall_blocking_hook = policy._acall_blocking_hook
    bindings._aemit_intermediate_reply = policy._aemit_intermediate_reply
    bindings._aexecute_tool_call = policy._aexecute_tool_call
    bindings._afinalize_tool_batch = policy._afinalize_tool_batch
    bindings._afinish_tool_call = policy._afinish_tool_call
    bindings._after_ask_human_skip_output = policy._after_ask_human_skip_output
    bindings._apply_session_compact_to_batch = (
        policy._apply_session_compact_to_batch
    )
    bindings._attach_truncation_hint = policy._attach_truncation_hint
    bindings._batch_has_pending_function_calls = (
        policy._batch_has_pending_function_calls
    )
    bindings._closing_finish_after_batch = policy._closing_finish_after_batch
    bindings._commit_user_stop_pairing = policy._commit_user_stop_pairing
    bindings._extra_finish_skip_outputs = policy._extra_finish_skip_outputs
    bindings._first_finish_call = policy._first_finish_call
    bindings._function_call_output_message = (
        policy._function_call_output_message
    )
    bindings._get_backend_api_mode = policy._get_backend_api_mode
    bindings._is_previous_response_cache_miss = (
        policy._is_previous_response_cache_miss
    )
    bindings._make_technical_feedback_user_message = (
        policy._make_technical_feedback_user_message
    )
    bindings._maybe_persist = policy._maybe_persist
    bindings._mode_requires_backend_call = policy._mode_requires_backend_call
    bindings._not_started_outputs = policy._not_started_outputs
    bindings._output_for_call = policy._output_for_call
    bindings._pickup_pending_as_new_turn = policy._pickup_pending_as_new_turn
    bindings._split_batch_new_around_finish = (
        policy._split_batch_new_around_finish
    )
    bindings._track_repeat_errors = policy._track_repeat_errors
    bindings._without_tool_call_pairs = policy._without_tool_call_pairs
    bindings.abuild_limit_envelope = abuild_limit_envelope
    bindings.amaybe_record_final_answer_intent = (
        amaybe_record_final_answer_intent
    )
    bindings.amaybe_update_intent_scope_from_tool = (
        amaybe_update_intent_scope_from_tool
    )
    bindings.apply_finish_gate_to_turn_finish_output_in_state = (
        apply_finish_gate_to_turn_finish_output_in_state
    )
    bindings.apply_state_delta = apply_state_delta
    bindings.aprocess_tool_calls = policy.aprocess_tool_calls
    bindings.aprocess_tool_outputs = policy.aprocess_tool_outputs
    bindings.aprocess_user_ask = policy.aprocess_user_ask
    bindings.ashould_return_text_answer = ashould_return_text_answer
    bindings.asyncio = asyncio
    bindings.await_callback = await_callback
    bindings.brief_args_summary = brief_args_summary
    bindings.build_invalid_tool_call_feedback_message = (
        build_invalid_tool_call_feedback_message
    )
    bindings.check_mode = policy.check_mode
    bindings.classify_finish_kind = classify_finish_kind
    bindings.clear_loop_detector = clear_loop_detector
    bindings.emit_progress = emit_progress
    bindings.extract_intermediate_prose = extract_intermediate_prose
    bindings.extract_last_assistant_text = extract_last_assistant_text
    bindings.filter_messages_not_in_state = filter_messages_not_in_state
    bindings.finish_blocks_final = finish_blocks_final
    bindings.finish_prevents_text_final = finish_prevents_text_final
    bindings.flush_loop_detector = flush_loop_detector
    bindings.format_message_responses = format_message_responses
    bindings.format_no_tool_call_bounce = format_no_tool_call_bounce
    bindings.gc_force_collect_enabled = gc_force_collect_enabled
    bindings.generate_message_id_func = generate_message_id_func
    bindings.get_function_call_output_text = get_function_call_output_text
    bindings.group_messages = policy.group_messages
    bindings.human_console_enabled = human_console_enabled
    bindings.inspect = inspect
    bindings.is_context_overflow_error = is_context_overflow_error
    bindings.is_internal_tool_error_output = is_internal_tool_error_output
    bindings.is_persist_discarded = is_persist_discarded
    bindings.is_unknown_tool_output = is_unknown_tool_output
    bindings.json = json
    bindings.maybe_store_and_wrap_tool_output = (
        maybe_store_and_wrap_tool_output
    )
    bindings.not_started_due_to_user_stop_output = (
        not_started_due_to_user_stop_output
    )
    bindings.observe_loop_detector = observe_loop_detector
    bindings.parse_tool_result_status = parse_tool_result_status
    bindings.partition_work_then_finish = policy.partition_work_then_finish
    bindings.projection_tags_from_config = projection_tags_from_config
    bindings.sanitize_messages_for_chat_completions = (
        sanitize_messages_for_chat_completions
    )
    bindings.todo_items_from_arguments = todo_items_from_arguments
    bindings.tool_state_view = tool_state_view
    bindings.wrap_technical_user_content = wrap_technical_user_content

    bindings._fallback_logger = logger
    bindings._arequest_backend_canonical = request_backend
    bindings.aapply_tool = unsupported
    bindings.apersist_subagent_trace = no_child_trace
    bindings.begin_turn_main_trace = begin_turn
    bindings.build_preserved_batch = build_preserved_batch
    bindings.apply_tool_usage_notice_before_llm = unsupported
    bindings.arun_agent = unsupported
    bindings.time = SimpleNamespace(monotonic=unsupported)
    bindings.gc = SimpleNamespace(collect=unsupported)
    return bindings
