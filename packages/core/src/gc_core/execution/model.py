"""Construct model requests from explicit data, without copying live State."""

from copy import deepcopy
from types import SimpleNamespace

from gc_core.execution.contracts import (
    ModelOperationCancelled,
    ModelOperationError,
    ModelRequest,
)
from gc_core.execution.model_errors import restore_model_failure
from gc_core.execution.reducers import apply_model_result
from gc_core.usage.account import UsageAccount

MODEL_STATE_FIELDS = (
    "state_id",
    "thread_id",
    "parent_state_id",
    "call_id",
    "x_target",
    "messages",
    "tools",
    "default_model",
    "call_metadata",
    "previous_response_id",
    "turn_level",
    "last_main_prompt_tokens",
    "last_main_prompt_message_count",
    "last_finish_reason",
    # Картинки: ключ хода для проб зрения и область успешного потолка картинок
    # (``processing_responses._image_request_rejects``). Пробы и проекция идут
    # в обработчике модели, где есть хранилище картинок.
    "vision_probe_turn",
    "user_turn_seq",
    "dialog_steps_offset",
)


def make_model_request(operations, state, messages, *, consumer, **kwargs):
    return ModelRequest(
        operation_id=operations.next_operation_id("model"),
        consumer=consumer,
        messages=deepcopy(messages),
        state={
            name: deepcopy(getattr(state, name, None))
            for name in MODEL_STATE_FIELDS
        },
        **kwargs,
    )


async def arequest_model(operations, state, messages, *, consumer, **kwargs):
    """Dispatch one model operation and apply its explicit result once.

    Both the main loop and auxiliary callers use this boundary. Provider
    routing, fallback and response conversion remain in the local handlers.
    """
    request = make_model_request(
        operations, state, messages, consumer=consumer, **kwargs
    )
    try:
        result = await operations.model(request)
    except (ModelOperationError, ModelOperationCancelled) as failure:
        apply_model_result(state, failure.result, main=consumer == "main")
        raise failure.cause from None
    if result.context_overflow and result.failure is None:
        # Durable transports return this classified failure as data. Restore
        # the same error recognised by the native compaction/retry policy.
        # Old histories carry no updated main counters with this flag.
        apply_model_result(state, result, main=False)
        raise RuntimeError("Model context window exceeded")
    apply_model_result(state, result, main=consumer == "main")
    if result.failure is not None:
        raise restore_model_failure(result.failure)
    return result


def model_state(request: ModelRequest):
    if request.schema_version != 1:
        raise ValueError("Unsupported ModelRequest schema_version")
    if set(request.state) - set(MODEL_STATE_FIELDS):
        raise ValueError("Unexpected model state fields")
    return SimpleNamespace(**deepcopy(request.state), usage=UsageAccount())
