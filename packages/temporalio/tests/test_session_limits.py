"""Состояние сессии в пределах записи истории; ожидание ответа не вечное."""

import asyncio
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from gc_core_temporalio.client import AgentSession, SessionStuck
from gc_core_temporalio.contracts import SessionResume
from gc_core_temporalio.sizes import (
    HistoryLimitExceeded,
    check_history_size,
    payload_size,
)
from gc_core_temporalio.workflow import (
    ANSWER_KEEP_BYTES,
    RESUME_BUDGET,
    _fit_resume,
)
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

#: ~1.2 МБ в истории Temporal.
BIG = "ж" * 200_000


def test_resume_with_big_answers_fits_the_budget():
    """8 ходов с ответами по 1.2 МБ и отброшенные большие сообщения: вход
    Continue-As-New был бы > 10 МБ — вечный сбой задачи. Теперь в пределах,
    последний ответ укорочен с пометкой, ID отброшенных сохранены."""
    resume = SessionResume(
        answer=BIG,
        formatted_answer=BIG,
        turns=8,
        turn_outcomes={
            str(turn): {
                "answer": BIG,
                "terminal_outcome": None,
                "formatted_answer": None,
                "post_error": None,
            }
            for turn in range(1, 9)
        },
        undelivered=[
            {"message_id": f"u{i}", "content": BIG} for i in range(3)
        ],
        human_replies={f"h{i}": BIG for i in range(3)},
    )
    assert payload_size(asdict(resume)) > 10 * RESUME_BUDGET
    fitted = _fit_resume(resume)
    assert payload_size(asdict(fitted)) <= RESUME_BUDGET
    last = fitted.turn_outcomes["8"]
    assert last["answer_truncated"] is True
    assert last["answer"] == BIG[: len(last["answer"])]
    assert payload_size(last["answer"]) <= ANSWER_KEEP_BYTES
    assert [value["message_id"] for value in fitted.undelivered] == [
        "u0",
        "u1",
        "u2",
    ]
    assert payload_size(fitted.answer) <= ANSWER_KEEP_BYTES


def test_small_resume_is_unchanged():
    resume = SessionResume(
        answer="ok",
        turn_outcomes={"1": {"answer": "ok", "post_error": None}},
        undelivered=[{"message_id": "u", "content": "текст"}],
    )
    assert _fit_resume(resume) == resume


def test_history_size_error_names_the_size():
    with pytest.raises(HistoryLimitExceeded, match=r"\d+ байт"):
        check_history_size("Вход", {"text": BIG + BIG})


class _Handle:
    """Сессия без сервера: ответы query и describe по сценарию."""

    id = "session"

    def __init__(self, states, *, attempt=0, status=None):
        self.states = list(states)
        self.attempt = attempt
        self.status = status or WorkflowExecutionStatus.RUNNING
        self.queries = self.describes = 0

    async def query(self, _query, message_id):
        self.queries += 1
        state = self.states.pop(0) if self.states else "pending"
        if state == "error":
            raise RPCError("task failed", RPCStatusCode.UNKNOWN, b"")
        return {"message_id": message_id, "state": state}

    async def describe(self):
        self.describes += 1
        task = SimpleNamespace(attempt=self.attempt)
        return SimpleNamespace(
            status=self.status,
            raw_description=SimpleNamespace(pending_workflow_task=task),
        )


def _session(handle):
    client = SimpleNamespace(get_workflow_handle=lambda _id: handle)
    return AgentSession(client, agent="a", thread_id="t", task_queue="q")


async def test_wait_stops_when_workflow_task_keeps_failing():
    handle = _Handle(["error"] * 50, attempt=3)
    with pytest.raises(SessionStuck, match="попытка 3"):
        await asyncio.wait_for(
            _session(handle).wait("m", poll=0.001, max_poll=0.001), 5
        )


async def test_wait_times_out_and_mostly_queries_only():
    handle = _Handle([])
    with pytest.raises(TimeoutError):
        await _session(handle).wait("m", poll=0.01, max_poll=0.01, timeout=0.3)
    assert handle.queries > 10
    # describe — на первом шаге и раз в несколько опросов, не на каждом.
    assert handle.describes <= handle.queries // 4 + 1


async def test_wait_returns_done_state():
    handle = _Handle(["pending", "done"])
    reply = await _session(handle).wait("m", poll=0.001)
    assert reply["delivered"] is True


def test_summary_trace_goes_in_parts_below_the_record_limit():
    """Трасса сжатия длинного треда (батчи суммаризатора по ~600 КБ и
    огромный итог): части меньше страницы, батчи целы, ключи сохранены."""
    from gc_core_temporalio.operations import TRACE_PART_BYTES, trace_parts

    batch = {"input": "ш" * 100_000, "output": "итог"}
    trace = {
        "trigger": "limit",
        "step1_batches": [batch] * 6,
        "final_output": {"text": "ф" * 1_000_000},
    }
    parts = trace_parts(trace, TRACE_PART_BYTES)
    assert all(payload_size(part) <= TRACE_PART_BYTES for part in parts)
    batches = [b for part in parts for b in part.get("step1_batches", [])]
    assert len(batches) == 6
    assert batches[0]["output"] == "итог"
    assert any("trigger" in part for part in parts)
    final = next(part for part in parts if "final_output" in part)
    assert "обрезано" in final["final_output"]["text"]
    assert trace_parts({"small": 1}, TRACE_PART_BYTES) == [{"small": 1}]
