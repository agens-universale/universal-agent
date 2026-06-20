"""Tests for _extract_markdown helper in cli.py."""
import json
import sys

# arg_parser.py parses sys.argv at import — pin argv before importing cli.
_ORIG_ARGV = sys.argv
sys.argv = ["pytest-cli-import", "-q", "dummy"]
try:
    from cli import _extract_markdown
finally:
    sys.argv = _ORIG_ARGV


# ---------- fixtures ----------------------------------------------------------

MARKDOWN_CONTENT = "# Project\n\nThis is the AGENTS.md content.\n\n## Commands\n\n- run tests"


def _finish_args(task_report: str = "", handoff: str = "") -> str:
    """Simulates finish_arguments JSON string returned by run_agent."""
    return json.dumps({"solution_type": "done", "task_report": task_report, "handoff": handoff})


# ---------- tests -------------------------------------------------------------


def test_extract_from_task_report():
    """Markdown in task_report field of finish_arguments JSON -> returned as-is."""
    result = _finish_args(task_report=MARKDOWN_CONTENT)
    assert _extract_markdown(result) == MARKDOWN_CONTENT


def test_extract_fenced_markdown_with_lang():
    """task_report wrapped in ```markdown\n...\n``` -> unfenced content returned."""
    fenced = "```markdown\n" + MARKDOWN_CONTENT + "\n```"
    result = _finish_args(task_report=fenced)
    assert _extract_markdown(result) == MARKDOWN_CONTENT


def test_extract_plain_fenced_markdown():
    """task_report wrapped in plain ```\n...\n``` -> unfenced content returned."""
    fenced = "```\n" + MARKDOWN_CONTENT + "\n```"
    result = _finish_args(task_report=fenced)
    assert _extract_markdown(result) == MARKDOWN_CONTENT


def test_empty_result_returns_empty_string():
    """None input -> empty string."""
    assert _extract_markdown(None) == ""


def test_empty_string_returns_empty_string():
    """Empty string input -> empty string."""
    assert _extract_markdown("") == ""


def test_fallback_to_handoff_when_task_report_empty():
    """When task_report is empty but handoff has content, return handoff."""
    result = _finish_args(task_report="", handoff=MARKDOWN_CONTENT)
    assert _extract_markdown(result) == MARKDOWN_CONTENT


def test_plain_text_response_returned_as_is():
    """Plain text (text_response mode) returned directly (no JSON wrapping)."""
    assert _extract_markdown(MARKDOWN_CONTENT) == MARKDOWN_CONTENT


def test_plain_text_fenced_at_top_level():
    """Plain text wrapped in fenced block (not inside JSON) -> unfenced."""
    fenced = "```markdown\n" + MARKDOWN_CONTENT + "\n```"
    assert _extract_markdown(fenced) == MARKDOWN_CONTENT


def test_error_string_returns_empty():
    """Step-limit error string -> empty string (no useful markdown)."""
    error = "Agent executed 50 steps and doesn't finish his work because of step limits."
    assert _extract_markdown(error) == ""
