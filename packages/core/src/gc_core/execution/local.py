"""Local implementation of the same model port used by durable runners."""

import asyncio
import time

from gc_core.execution.contracts import (
    ModelOperationCancelled,
    ModelOperationError,
    ModelRequest,
    ModelResult,
)
from gc_core.execution.model import model_state, model_vision


class LocalOperations:
    def __init__(self, ctx):
        self.ctx = ctx
        self._sequence = 0

    def next_operation_id(self, kind: str) -> str:
        self._sequence += 1
        return f"{kind}-{self._sequence}"

    def monotonic(self) -> float:
        return time.monotonic()

    async def model(self, request: ModelRequest) -> ModelResult:
        from gc_core.processing_responses import (
            _arequest_backend_canonical_local,
        )
        from gc_core.utils.llm_chat_client import (
            _arequest_chat_completion_outcome_local,
        )

        state = model_state(request)
        result = ModelResult()
        try:
            if request.consumer == "main":
                (
                    result.messages,
                    result.response_id,
                    result.response_metadata,
                ) = await _arequest_backend_canonical_local(
                    request.messages,
                    state,
                    self.ctx,
                    set_previous_response_id=request.set_previous_response_id,
                    mute_answer=request.mute_answer,
                )
                # State.set_resp_headers_metadata consumes only x-target. Raw
                # transport headers (cookies, credentials) must not cross the
                # serializable model boundary into durable history.
                result.response_metadata = {
                    k: v
                    for k, v in result.response_metadata.items()
                    if k == "x-target"
                }
            else:
                outcome = await _arequest_chat_completion_outcome_local(
                    ctx=self.ctx,
                    state=state,
                    messages=request.messages,
                    max_tokens=request.max_tokens,
                    consumer=request.consumer,
                    request_label=request.request_label,
                    payload_overrides=request.payload_overrides,
                )
                result.text = outcome.text
                result.finish_reason = outcome.finish_reason
        except asyncio.CancelledError as exc:
            raise ModelOperationCancelled(exc, result) from exc
        except Exception as exc:
            raise ModelOperationError(exc, result) from exc
        finally:
            result.usage = state.usage.to_meta()
            if request.consumer == "main":
                result.finish_reason = state.last_finish_reason
                result.prompt_tokens = state.last_main_prompt_tokens
                result.prompt_message_count = (
                    state.last_main_prompt_message_count
                )
                result.vision = model_vision(state)
        return result
