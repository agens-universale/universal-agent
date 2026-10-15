"""Консольное одобрение плана: выбор, продолжение, доработка, выход."""

import json
import sys
from types import SimpleNamespace

import pytest

# `import cli` парсит sys.argv при импорте (--query обязателен).
sys.argv = ["gc-cli", "-q", "x"]

from gc_cli import plan_approval  # noqa: E402
from gc_testkit import run_coro  # noqa: E402

_PLAN = json.dumps(
    {"plan": "1. шаг", "status": "proposed"}, ensure_ascii=False
)


class _Executor:
    def __init__(self, outcome="plan_proposed", status="proposed"):
        self.last_terminal_outcome = outcome
        self.status = status
        self.modes, self.transitions, self.rework, self.runs = [], [], [], []
        self.events = []

    def plan_snapshot(self, thread_id):
        return {"text": "1. шаг", "status": self.status} if thread_id else None

    def set_permission_mode(self, mode):
        self.modes.append(mode)

    def accept_plan(self, mode, thread_id):
        self.transitions.append((mode, thread_id))
        self.status = "approved"

    def arm_plan_rework(self, thread_id):
        self.rework.append(thread_id)

    async def arun(self, messages, **kwargs):
        self.runs.append(messages)
        self.events.append(("run", kwargs.get("account_run", True)))
        return f"ответ {len(self.runs)}"

    async def aclose_run_accounting(self):
        self.events.append(("close",))


@pytest.fixture
def answers(monkeypatch):
    queue: list[str] = []
    monkeypatch.setattr(
        plan_approval.sys, "stdin", SimpleNamespace(isatty=lambda: True)
    )
    monkeypatch.setattr("builtins.input", lambda: queue.pop(0))
    return queue


def _approve(executor, thread_id="t1"):
    return run_coro(
        plan_approval.arun_plan_approval(
            executor, _PLAN, thread_id=thread_id, verbose=False
        )
    )


def test_approval_switches_mode_and_continues(answers):
    answers.append("3")
    ex = _Executor()
    assert _approve(ex) == "ответ 1"
    assert ex.modes == ["manual"]
    assert ex.transitions == [("manual", "t1")]
    assert "approved" in ex.runs[0][0] and "<technical" in ex.runs[0][0]


def test_rework_sends_feedback_in_plan_mode_and_asks_again(answers):
    answers.extend(["4", "добавь тесты", "1"])
    ex = _Executor()
    assert _approve(ex) == "ответ 2"
    assert ex.modes == ["plan", "edit"]
    assert ex.rework == ["t1"]
    assert ex.runs[0] == ["добавь тесты"]


def test_any_other_answer_leaves_the_plan_as_the_result(answers):
    answers.append("n")
    ex = _Executor()
    assert _approve(ex) == _PLAN
    assert (ex.modes, ex.transitions, ex.runs) == ([], [], [])


def test_nothing_is_asked_without_a_terminal_a_session_or_a_fresh_plan(
    answers, monkeypatch
):
    assert (
        _approve(_Executor(outcome=None)) == _PLAN
    )  # прогон кончился не планом
    assert _approve(_Executor(), thread_id=None) == _PLAN  # одобрять не к чему
    monkeypatch.setattr(
        plan_approval.sys, "stdin", SimpleNamespace(isatty=lambda: False)
    )
    assert _approve(_Executor()) == _PLAN  # человека нет
    assert answers == []


def test_second_answer_approves_the_plan_for_auto(answers):
    answers.append("2")
    ex = _Executor()
    assert _approve(ex) == "ответ 1"
    assert ex.modes == ["auto"]
    assert ex.transitions == [("auto", "t1")]


def test_deferred_accounting_closes_the_first_run_before_continuing(answers):
    """С `--format` учёт закрывает вызывающий после перефраза. Продолжение
    после одобрения — новый прогон: первый надо закрыть до него (иначе его
    мета и отчёт usage теряются), а продолжение оставить открытым."""
    answers.append("1")
    ex = _Executor()
    run_coro(
        plan_approval.arun_plan_approval(
            ex, _PLAN, thread_id="t1", verbose=False, account_run=False
        )
    )
    assert ex.events == [("close",), ("run", False)]


def test_ordinary_run_keeps_its_own_accounting(answers):
    answers.append("1")
    ex = _Executor()
    _approve(ex)
    assert ex.events == [("run", True)]


def test_deferred_accounting_closes_the_first_run_before_switching_the_mode(
    answers,
):
    """Учёт первого прогона пишет его мету: режим и план там — как на момент
    его конца, без смены режима от одобрения."""
    answers.append("1")
    ex = _Executor()
    original = ex.set_permission_mode

    def set_mode(mode):
        ex.events.append(("mode", mode))
        original(mode)

    ex.set_permission_mode = set_mode
    run_coro(
        plan_approval.arun_plan_approval(
            ex, _PLAN, thread_id="t1", verbose=False, account_run=False
        )
    )
    assert ex.events[:2] == [("close",), ("mode", "edit")]
