"""Stage 2: session tools declare interaction roles in API descriptions."""

from tools.session.answer_question import AnswerQuestion
from tools.session.ask_human import AskHuman
from tools.session.submit_solution import SubmitSolution
from tools.session.working_memory import WorkingMemory
from tools.session.long_memory import LongMemory
from tools.fs.read_file import ReadFile
from tools.session.stored_outputs.read_stored_tool_output import ReadStoredToolOutput
from tools.session.read_user_intent_log import ReadUserIntentLog


def test_answer_question_intermediate_non_final():
    desc = AnswerQuestion.tool_description()["description"].lower()
    assert "intermediate" in desc or "non-final" in desc
    assert "not complete" in desc
    assert "final" in desc
    assert "multi-step" in desc or "multi-step" in desc


def test_ask_human_requests_input_non_final():
    desc = AskHuman.tool_description()["description"].lower()
    assert "non-final" in desc
    assert "confirmation" in desc or "choice" in desc
    assert "turn-finalizing" in desc or "final" in desc
    assert len(desc) < 950


def test_submit_solution_finalize_turn():
    desc = SubmitSolution.tool_description()["description"].lower()
    assert "finalize" in desc
    assert "exactly once" in desc
    assert "intermediate" in desc
    assert "ask-user" in desc or "ask the user" in desc


def test_working_memory_thread_not_project():
    desc = WorkingMemory.tool_description()["description"].lower()
    assert "thread" in desc or "session" in desc or "dialogue" in desc
    assert "forever" in desc or "project-wide" in desc


def test_long_memory_project_future():
    desc = LongMemory.tool_description()["description"].lower()
    assert "future" in desc or "future sessions" in desc
    assert "thread" in desc


def test_read_file_pagination_lines_and_chars():
    flat = " ".join(ReadFile.tool_description()["description"].lower().split())
    assert "line" in flat
    assert "char_offset" in flat
    assert "char_limit" in flat


def test_read_stored_tool_output_pagination_chars():
    flat = " ".join(
        ReadStoredToolOutput.tool_description()["description"].lower().split()
    )
    assert "character" in flat


def test_read_user_intent_log_pagination_records():
    spec = ReadUserIntentLog.tool_description()
    flat = " ".join(spec["description"].lower().split())
    assert "record" in flat
    offset_desc = spec["parameters"]["properties"]["offset"]["description"].lower()
    assert "record" in offset_desc
