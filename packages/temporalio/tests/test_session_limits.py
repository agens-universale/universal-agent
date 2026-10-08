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
    fitted = _fit_resume(resume)
    # Ответ сессии — ответ последнего хода: в resume одной копией.
    assert fitted.answer is None and fitted.answer_in_outcome
    assert fitted.turn_outcomes == resume.turn_outcomes
    assert fitted.undelivered == resume.undelivered


def test_answer_and_formatted_answer_of_600kb_each_survive_whole():
    """Ответ и форматированный ответ по ~600 КБ: вместе с копией ответа
    сессии не помещались, и ответ резался до 256 КБ."""
    answer, formatted = "а" * 100_000, "б" * 100_000
    resume = SessionResume(
        answer=answer,
        formatted_answer=formatted,
        turns=1,
        turn_outcomes={
            "1": {
                "answer": answer,
                "formatted_answer": formatted,
                "post_error": None,
            }
        },
    )
    fitted = _fit_resume(resume)
    last = fitted.turn_outcomes["1"]
    assert last["answer"] == answer and last["formatted_answer"] == formatted
    assert "answer_truncated" not in last
    assert payload_size(asdict(fitted)) <= RESUME_BUDGET


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


def test_latest_answer_survives_continue_as_new_whole():
    """CAN идёт сразу после хода: ответ ~700 КБ (кириллица) должен дойти до
    wait/ask целиком, а не урезанным до 256 КБ."""
    answer = "о" * 120_000
    resume = SessionResume(
        answer=answer,
        turns=2,
        turn_outcomes={
            "1": {"answer": answer, "post_error": None},
            "2": {"answer": answer, "post_error": None},
        },
    )
    fitted = _fit_resume(resume)
    assert fitted.turn_outcomes["2"]["answer"] == answer
    assert "answer_truncated" not in fitted.turn_outcomes["2"]
    assert fitted.turn_outcomes["1"]["answer_truncated"] is True
    assert payload_size(asdict(fitted)) <= RESUME_BUDGET


async def test_truncated_answer_is_an_error_not_a_silent_cut():
    from gc_core_temporalio.client import AnswerTruncated

    class _Done(_Handle):
        async def query(self, _query, message_id):
            return {
                "message_id": message_id,
                "state": "done",
                "turn": 3,
                "answer": "начало",
                "answer_truncated": True,
            }

    session = _session(_Done([]))

    async def sent(text, *, message_id=None):
        return "m"

    session.send = sent
    with pytest.raises(AnswerTruncated) as error:
        await session.ask("вопрос")
    assert error.value.answer == "начало"


def test_oversized_auxiliary_request_is_cut_in_the_middle():
    """Сводка при лимите шагов: трасса 2.7 МБ одной строкой не роняет ход —
    середина укорачивается, начало и конец трассы остаются."""
    from gc_core_temporalio.operations import (
        AUX_MESSAGES_BYTES,
        _fit_auxiliary_messages,
    )

    trace = "НАЧАЛО " + "т" * 450_000 + " КОНЕЦ"
    messages = [
        {"role": "system", "content": "You are a trace summarizer."},
        {"role": "user", "content": trace},
    ]
    assert payload_size(messages) > AUX_MESSAGES_BYTES
    fitted = _fit_auxiliary_messages(messages)
    assert payload_size(fitted) <= AUX_MESSAGES_BYTES
    text = fitted[1]["content"]
    assert text.startswith("НАЧАЛО") and text.endswith("КОНЕЦ")
    assert "пропущено" in text
    assert fitted[0] == messages[0]
    small = [{"role": "user", "content": "коротко"}]
    assert _fit_auxiliary_messages(small) is small
