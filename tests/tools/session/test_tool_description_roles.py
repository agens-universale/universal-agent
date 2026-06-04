"""Stage 2: session tools declare interaction roles in API descriptions."""

from tools.turn.intermediate_reply import IntermediateReply
from tools.turn.ask_human import AskHuman
from tools.turn.submit_solution import SubmitSolution
from tools.session.working_memory import WorkingMemory
from tools.project.project_memory import ProjectMemory
from tools.fs.read_file import ReadFile
from tools.session.stored_outputs.read_stored_tool_output import ReadStoredToolOutput
from tools.session.read_user_intent_log import ReadUserIntentLog


def test_intermediate_reply_non_final():
    desc = IntermediateReply.tool_description()["description"].lower()
    assert desc.startswith("finalizes turn: no")
    assert "intermediate" in desc
    assert "not complete" in desc
    assert "does not wait" in desc
    assert "turn_finish" not in desc
    assert "ask_human" not in desc


def test_ask_human_requests_input_non_final():
    desc = AskHuman.tool_description()["description"].lower()
    assert desc.startswith("finalizes turn: no")
    assert "wait" in desc
    assert "confirmation" in desc or "choice" in desc
    assert "one-way" in desc
    assert "turn_finish" not in desc
    assert "intermediate_reply" not in desc
    assert len(desc) < 950


def test_submit_solution_finalize_turn():
    desc = SubmitSolution.tool_description()["description"].lower()
    assert desc.startswith("finalizes turn: yes")
    assert "exactly once" in desc
    assert "mid-task" in desc or "progress" in desc
    assert "intermediate_reply" not in desc
    assert "ask_human" not in desc


def test_working_memory_thread_not_project():
    desc = WorkingMemory.tool_description()["description"].lower()
    assert "thread" in desc or "session" in desc or "dialogue" in desc
    assert "forever" in desc or "project-wide" in desc


def test_working_memory_register_hygiene_in_description():
    desc = WorkingMemory.tool_description()["description"].lower()
    prompt = WorkingMemory.prompt_description().lower()
    for text in (desc, prompt):
        assert "approach" in text and "exceed" in text
        assert "rewrite" in text
        assert "do not wait" in text
        assert "hygiene" in text
        assert "deduplicate" in text or "deduplication" in text
    assert "do not append on every trivial step" in prompt
    assert "definitions above" in desc or "per the definitions above" in desc


def test_project_memory_project_future():
    desc = ProjectMemory.tool_description()["description"].lower()
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
    assert "eof" in flat
    prompt = ReadStoredToolOutput.prompt_description().lower()
    assert "truncated=false" in prompt
    assert "next_offset" in prompt and "total_chars" in prompt


def test_read_user_intent_log_pagination_records():
    spec = ReadUserIntentLog.tool_description()
    flat = " ".join(spec["description"].lower().split())
    assert "record" in flat
    offset_desc = spec["parameters"]["properties"]["offset"]["description"].lower()
    assert "record" in offset_desc
