"""Project captured UAG events into a Harbor ATIF trajectory."""

import json
import threading
from pathlib import Path
from stat import S_IMODE
from tempfile import TemporaryDirectory
from typing import Any

from gc_core.execution.message_keys import has_tool_call_identity
from gc_core.execution.progress import parse_tool_result_status
from gc_core.execution.run_trace import (
    BackendResponse,
    CommittedMessages,
    DiscardedResponse,
    ModelResponse,
    TraceEvent,
)
from gc_core.usage.account import CATEGORY_MAIN
from gc_core.usage.normalize import (
    reported_reasoning_tokens,
    reported_token_counts,
)
from gc_core.utils.atomic_io import atomic_replace
from harbor.models.trajectories import (
    FinalMetrics,
    Metrics,
    Observation,
    ObservationResult,
    Step,
    ToolCall,
    Trajectory,
)
from harbor.utils.trajectory_utils import format_trajectory_json


def stringify_content(content: Any) -> str:
    """Flatten text content from canonical messages."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(part.get("text") or part.get("content") or "")
            else:
                parts.append(str(part))
        return "\n".join(str(part) for part in parts if part)
    return str(content)


def _tool_call(message: dict[str, Any]) -> ToolCall:
    raw = message.get("arguments")
    try:
        arguments = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        arguments = None
    if isinstance(arguments, dict):
        extra = None
    else:
        arguments = {}
        extra = {"raw_arguments": raw} if raw is not None else None
    return ToolCall(
        tool_call_id=message["call_id"],
        function_name=message["name"],
        arguments=arguments,
        extra=extra,
    )


def _step_metrics(usage: Any) -> Metrics | None:
    prompt, completion, cached = reported_token_counts(usage)
    reasoning = reported_reasoning_tokens(usage)
    if all(count is None for count in (prompt, completion, cached, reasoning)):
        return None
    return Metrics(
        prompt_tokens=prompt,
        completion_tokens=completion,
        cached_tokens=cached,
        extra={"reasoning_tokens": reasoning}
        if reasoning is not None
        else None,
    )


def _model_step(
    event: ModelResponse, step_id: int, *, timestamp: str | None = None
) -> Step:
    replies = [m for m in event.messages if m.get("role") == "assistant"]
    text = "\n".join(
        part for m in replies if (part := stringify_content(m.get("content")))
    )
    reasoning = "\n".join(
        item
        for m in replies
        if isinstance(item := m.get("reasoning_content"), str)
    )
    calls = [
        _tool_call(m)
        for m in event.messages
        if m.get("type") == "function_call" and has_tool_call_identity(m)
    ]
    return Step(
        step_id=step_id,
        source="agent",
        timestamp=timestamp or event.started_at.isoformat(),
        model_name=event.model_name,
        message=text,
        reasoning_content=reasoning or None,
        tool_calls=calls or None,
        metrics=_step_metrics(event.usage),
        llm_call_count=1,
        extra={
            "response_id": event.response_id,
            "usage_category": CATEGORY_MAIN,
        },
    )


class TrajectoryRecorder:
    """Collect one trial's steps in the worker and freeze them for finalization."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frozen = False
        self._steps: list[Step] = []
        self._message_ids: set[str] = set()
        self._calls: dict[str, Step] = {}
        self._responses: dict[str, Step] = {}
        self._pending_model_step: tuple[dict[str, Any], int] | None = None

    def observe(self, event: TraceEvent) -> None:
        with self._lock:
            if self._frozen:
                return
            if isinstance(event, BackendResponse):
                self._record_backend_step(event)
            elif isinstance(event, ModelResponse):
                self._record_model_step(event)
            elif isinstance(event, CommittedMessages):
                timestamp = event.timestamp.isoformat()
                for message in event.messages:
                    self._commit(message, timestamp)
            elif isinstance(event, DiscardedResponse):
                self._discard(event.response_id)

    def _record_backend_step(self, event: BackendResponse) -> None:
        extra = {"usage_category": event.usage_category}
        if response_id := event.payload.get("id"):
            extra["response_id"] = str(response_id)
        if event.is_subagent:
            extra["agent_scope"] = "subagent"
            if event.subagent_id is not None:
                extra["subagent_id"] = event.subagent_id
        index = len(self._steps)
        self._steps.append(
            Step(
                step_id=index + 1,
                source="agent",
                timestamp=event.started_at.isoformat(),
                model_name=event.model_name,
                message="",
                metrics=_step_metrics(event.payload.get("usage")),
                llm_call_count=1,
                extra=extra,
            )
        )
        # The root loop awaits its model calls in order. Keep the latest
        # candidate by identity because providers can reuse response IDs.
        if event.usage_category == CATEGORY_MAIN and not event.is_subagent:
            self._pending_model_step = (event.payload, index)

    def _record_model_step(self, event: ModelResponse) -> None:
        pending, self._pending_model_step = self._pending_model_step, None
        if pending is not None and pending[0] is event.backend_payload:
            index = pending[1]
            backend_step = self._steps[index]
            step = _model_step(
                event, backend_step.step_id, timestamp=backend_step.timestamp
            )
            self._steps[index] = step
        else:
            step = _model_step(event, len(self._steps) + 1)
            self._steps.append(step)
        self._responses[event.response_id] = step
        for call in step.tool_calls or []:
            self._calls[call.tool_call_id] = step

    def _commit(self, message: dict[str, Any], timestamp: str) -> None:
        if message.get("type") == "function_call_output":
            self._attach_result(message)
            return
        role = message.get("role")
        if role not in ("system", "user"):
            return
        message_id = message.get("id")
        if message_id in self._message_ids:
            return
        self._steps.append(
            Step(
                step_id=len(self._steps) + 1,
                source=role,
                message=stringify_content(message.get("content")),
                timestamp=timestamp,
            )
        )
        if message_id:
            self._message_ids.add(message_id)

    def _attach_result(self, message: dict[str, Any]) -> None:
        call_id = message["call_id"]
        step = self._calls.get(call_id)
        if step is None:
            return
        output = message.get("output")
        content = stringify_content(output)
        ok, _ = parse_tool_result_status(output)
        extra = {"is_error": not ok} if ok is not None else None
        if step.observation is None:
            step.observation = Observation(results=[])
        for result in step.observation.results:
            if result.source_call_id == call_id:
                result.content = content
                result.extra = extra
                return
        step.observation.results.append(
            ObservationResult(
                source_call_id=call_id, content=content, extra=extra
            )
        )

    def _discard(self, response_id: str) -> None:
        step = self._responses.get(response_id)
        if step is None:
            return
        step.extra = {**(step.extra or {}), "discarded_by_routing": True}
        for call in step.tool_calls or []:
            if self._calls.get(call.tool_call_id) is step:
                del self._calls[call.tool_call_id]

    def freeze(self) -> list[Step]:
        with self._lock:
            self._frozen = True
            self._pending_model_step = None
            return list(self._steps)


def _sum_metric(steps: list[Step], field: str) -> int | None:
    values = [
        value
        for step in steps
        if step.metrics is not None
        if (value := getattr(step.metrics, field)) is not None
    ]
    return sum(values) if values else None


def _total_reasoning_tokens(steps: list[Step]) -> int | None:
    counts = []
    for step in steps:
        if not step.llm_call_count:
            continue
        metrics_extra = step.metrics.extra if step.metrics else None
        count = (metrics_extra or {}).get("reasoning_tokens")
        if count is None:
            return None
        counts.append(count)
    return sum(counts) if counts else None


def final_metrics(steps: list[Step]) -> FinalMetrics:
    reasoning = _total_reasoning_tokens(steps)
    return FinalMetrics(
        total_steps=len(steps),
        total_prompt_tokens=_sum_metric(steps, "prompt_tokens"),
        total_completion_tokens=_sum_metric(steps, "completion_tokens"),
        total_cached_tokens=_sum_metric(steps, "cached_tokens"),
        extra={"reasoning_tokens": reasoning}
        if reasoning is not None
        else None,
    )


def trial_usage_extra(
    steps: list[Step], usage_totals: dict[str, Any]
) -> dict[str, Any]:
    """Expose the trial usage snapshot alongside the sums for published steps."""
    atif_model_calls = sum(step.llm_call_count or 0 for step in steps)
    model_calls = usage_totals["calls"]
    return {
        "model_calls": model_calls,
        "atif_model_calls": atif_model_calls,
        "atif_model_calls_match": atif_model_calls == model_calls,
        "call_statistics": {
            "model_calls_without_usage": usage_totals["missing_usage_calls"],
        },
        "trial_usage": {
            "prompt_tokens": usage_totals["input"],
            "completion_tokens": usage_totals["output"],
            "cached_tokens": usage_totals["cached"],
        },
    }


def write_trajectory(path: Path, trajectory: Trajectory) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = format_trajectory_json(trajectory.to_json_dict())
    # Stage privately on the same filesystem while letting the umask set the file mode.
    with TemporaryDirectory(prefix=".atif-", dir=path.parent) as staging_dir:
        temporary = Path(staging_dir) / path.name
        temporary.write_text(payload, encoding="utf-8")
        try:
            previous_mode = S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            pass
        else:
            temporary.chmod(previous_mode)
        atomic_replace(temporary, path)
