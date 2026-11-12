"""AnswerFlow — общее правило показа ответов ленты и /copy."""

from __future__ import annotations

import subprocess
import sys

from gc_core.tools.turn.turn_close import TurnClose

from gc_tui.services.answer_flow import AnswerFlow


def _close(body):
    return TurnClose("", body, "", "", "")


def test_import_is_light():
    code = (
        "import sys, gc_tui.services.answer_flow;"
        "print('gc_core.tools.turn.turn_close' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True
    )
    assert out.stdout.strip() == "False", out.stderr


def test_repeat_in_turn_hidden_new_turn_shown():
    flow = AnswerFlow()
    assert flow.agent("X").show is True
    assert flow.agent("X").show is False
    flow.new_turn()
    assert flow.agent("X").show is True


def test_attempt_dropped_by_repeating_prose():
    flow = AnswerFlow()
    flow.finish(_close("отчёт"), tracked=True)
    verdict = flow.agent("отчёт")
    assert (verdict.show, verdict.drop_attempt) == (True, True)
    assert flow.attempt is None


def test_final_repeating_intermediate_is_promoted_once():
    flow = AnswerFlow()
    flow.agent("X", intermediate=True)
    verdict = flow.agent("X")
    assert (verdict.show, verdict.final_of_intermediate) == (False, True)
    assert flow.agent("X").final_of_intermediate is False


def test_report_repeating_prose_loses_body():
    flow = AnswerFlow()
    flow.agent("X")
    assert flow.finish(_close("X"), tracked=True).body == ""
    assert flow.report_of_intermediate is False


def test_empty_report_after_empty_intermediate_not_promoted():
    flow = AnswerFlow()
    flow.agent("", intermediate=True)
    flow.finish(_close(""), tracked=True)
    assert flow.report_of_intermediate is False
