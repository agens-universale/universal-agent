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
        self.accepted = []
        self.events = []
        self.systems = []

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
        self.systems.append(kwargs.get("system"))
        self.runs.append(messages)
        self.events.append(("run", kwargs.get("account_run", True)))
        return f"ответ {len(self.runs)}"

    async def aclose_run_accounting(self):
        self.events.append(("close",))

    async def aexecute_accepted_plan(
        self,
        mode,
        thread_id,
        *,
        extra_text="",
        verbose=None,
        account_run=True,
        system=None,
    ):
        self.systems.append(system)
        self.accepted.append((mode, thread_id, extra_text, account_run))
        self.events.append(("accepted", account_run))
        self.status = "approved"
        return f"исполнено в {mode}"


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


def test_approval_hands_the_choice_to_the_approval_sequence(answers):
    answers.append("3")
    ex = _Executor()
    assert _approve(ex) == "исполнено в manual"
    assert ex.accepted == [("manual", "t1", "", True)]
    # режим и одобрение ставит последовательность ядра, не CLI
    assert (ex.modes, ex.transitions, ex.runs) == ([], [], [])


def test_rework_sends_feedback_in_plan_mode_and_asks_again(answers):
    answers.extend(["4", "добавь тесты", "1"])
    ex = _Executor()
    assert _approve(ex) == "исполнено в edit"
    assert ex.modes == ["plan"]
    assert ex.rework == ["t1"]
    assert ex.runs[0] == ["добавь тесты"]
    assert ex.accepted == [("edit", "t1", "", True)]


def test_any_other_answer_leaves_the_plan_as_the_result(answers):
    answers.append("n")
    ex = _Executor()
    assert _approve(ex) == _PLAN
    assert (ex.modes, ex.accepted, ex.runs) == ([], [], [])


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
    assert _approve(ex) == "исполнено в auto"
    assert ex.accepted == [("auto", "t1", "", True)]


def test_deferred_accounting_is_handed_to_the_approval_sequence(answers):
    """С `--format` учёт закрывает вызывающий после перефраза. Первый прогон
    закрывает последовательность ядра (до смены режима), продолжение
    остаётся открытым вызывающему."""
    answers.append("1")
    ex = _Executor()
    run_coro(
        plan_approval.arun_plan_approval(
            ex, _PLAN, thread_id="t1", verbose=False, account_run=False
        )
    )
    assert ex.accepted == [("edit", "t1", "", False)]
    assert ex.events == [("accepted", False)]


def test_ordinary_run_keeps_its_own_accounting(answers):
    answers.append("1")
    ex = _Executor()
    _approve(ex)
    assert ex.events == [("accepted", True)]


def test_without_a_terminal_the_plan_waits_with_a_hint(
    answers, monkeypatch, capsys
):
    monkeypatch.setattr(
        plan_approval.sys, "stdin", SimpleNamespace(isatty=lambda: False)
    )
    assert _approve(_Executor()) == _PLAN
    err = capsys.readouterr().err
    assert "сессия осталась в plan" in err
    assert "gc-cli -t t1 --approve-plan edit (или auto, manual)" in err


def test_without_a_session_the_hint_asks_for_one_even_without_a_terminal(
    answers, monkeypatch, capsys
):
    monkeypatch.setattr(
        plan_approval.sys, "stdin", SimpleNamespace(isatty=lambda: False)
    )
    assert _approve(_Executor(), thread_id=None) == _PLAN
    err = capsys.readouterr().err
    assert "только для сессии" in err
    assert "--approve-plan" not in err


def test_plan_answer_is_printed_as_plan_text_not_json(capsys):
    from unittest.mock import MagicMock

    from gc_cli import cli
    from gc_testkit.settings import make_settings

    cli._print_agent_response(
        _PLAN,
        make_settings({"logging.console_trace": True}),
        SimpleNamespace(logger=MagicMock()),
    )
    assert capsys.readouterr().out.strip() == "1. шаг"


def test_approval_keeps_the_system_of_the_thread(answers):
    """Сессия другой системы (``--system``): доработка и исполнение плана
    идут по тому же треду, а не по треду ``cli`` с тем же id."""
    answers.extend(["4", "добавь тесты", "1"])
    ex = _Executor()
    run_coro(
        plan_approval.arun_plan_approval(
            ex, _PLAN, thread_id="t1", verbose=False, system="g"
        )
    )
    assert ex.systems == ["g", "g"]
