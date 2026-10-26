"""Состояние зрения пересекает границу модельной операции в обе стороны.

В develop запрос шёл с живым State; через ModelRequest (local и Temporal)
терялись ``vision_live_ids`` (``fs_view_image`` прикладывал снимок повторно)
и потолок картинок после 413 (каждый шаг снова начинал с полного набора).
"""

import json
from types import SimpleNamespace

from gc_core.execution.contracts import ModelResult
from gc_core.execution.model import (
    make_model_request,
    model_state,
    model_vision,
)
from gc_core.execution.reducers import apply_model_result
from gc_core.usage.account import UsageAccount
from gc_core.vision.rejects import ImageRequestLimit
from gc_core.vision.route import VisionRouteKey


def _limit():
    key = VisionRouteKey("http://h/v1/chat/completions", "m", "fp")
    return ImageRequestLimit((key, "turn-1", 2, 0, frozenset({"a", "b"})), 1)


def test_request_limit_round_trips_through_model_request():
    operations = SimpleNamespace(next_operation_id=lambda kind: kind)
    state = SimpleNamespace(vision_request_limit=_limit(), messages=[])
    request = make_model_request(operations, state, [], consumer="main")
    json.dumps(request.state)  # граница durable: только данные
    assert model_state(request).vision_request_limit == _limit()


def test_model_result_vision_updates_live_state():
    worker_state = SimpleNamespace(
        vision_live_ids=frozenset({"a"}), vision_request_limit=_limit()
    )
    result = ModelResult(vision=model_vision(worker_state))
    json.dumps(result.vision)
    state = SimpleNamespace(
        usage=UsageAccount(),
        vision_live_ids=frozenset(),
        vision_request_limit=None,
        last_finish_reason=None,
        last_main_prompt_tokens=None,
        last_main_prompt_message_count=None,
    )
    apply_model_result(state, result, main=False)
    assert state.vision_live_ids == frozenset({"a"})
    assert state.vision_request_limit == _limit()
