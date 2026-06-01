"""§8 / step 7: orchestrator and subagent prompts — no visited-list tool; simple anti-loop."""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATOR_PROMPT_PATH = REPO_ROOT / "prompts" / "chat_completions_system_prompt.md"
SUBAGENT_PROMPT_PATH = REPO_ROOT / "prompts" / "chat_completions_subagent_system_prompt.md"

FORBIDDEN_BUILTIN_NAMES = (
    "submit_solution",
    "write_to_file",
    "execute_command",
    "append_to_file",
)
_FORBIDDEN_RE = re.compile(r"\b(" + "|".join(FORBIDDEN_BUILTIN_NAMES) + r")\b")

VISITED_LIST_PHRASES = (
    "store or recall a list of already-visited",
    "update that list with their identifiers",
    "read that list at the start of your turn",
)


def _read_prompt(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _anti_loop_section(text: str, heading: str, until: str) -> str:
    return text.split(heading, 1)[1].split(until, 1)[0]


def test_orchestrator_prompt_has_no_hardcoded_builtin_tool_names():
    text = _read_prompt(ORCHESTRATOR_PROMPT_PATH)
    offenders = []
    for i, line in enumerate(text.splitlines(), 1):
        m = _FORBIDDEN_RE.search(line)
        if m:
            offenders.append(f"L{i}: {m.group(1)} :: {line.strip()}")
    assert not offenders, "hardcoded builtin tool name(s):\n" + "\n".join(offenders)


def test_prompts_do_not_reference_visited_list_store_tool():
    for path in (ORCHESTRATOR_PROMPT_PATH, SUBAGENT_PROMPT_PATH):
        text = _read_prompt(path)
        for phrase in VISITED_LIST_PHRASES:
            assert phrase not in text, f"{path.name}: found {phrase!r}"


def test_orchestrator_anti_loop_single_bullet_own_changes_only():
    text = _read_prompt(ORCHESTRATOR_PROMPT_PATH)
    section = _anti_loop_section(text, "**Anti-loop (reads):**", "**Command execution:**")
    assert "do not repeat the same read/list/get" in section
    assert "last_dialog_steps" in section
    assert "your tools" in section
    assert text.count("do not repeat the same read/list/get") == 1
    assert "Before opening files" not in text
    assert "read_user_intent_log" not in text
    assert "thread-memory tool" in text.lower() or "thread/session memory" in text.lower()


def test_subagent_anti_loop_single_bullet_memory_at_run_start():
    text = _read_prompt(SUBAGENT_PROMPT_PATH)
    section = _anti_loop_section(text, "### Anti-loop (reads)", "### Completion evidence")
    assert section.count("\n- ") == 1
    assert "your tools" in section
    assert "Before opening files" not in text
    assert "At run start" in text
    assert "read_user_intent_log" not in text
    assert "working_memory" not in text
    assert "project/long memory" in text.lower() or "project-scoped long memory" in text.lower()
    assert "thread/session memory" in text.lower()


def test_subagent_prompt_has_no_hardcoded_builtin_tool_names():
    text = _read_prompt(SUBAGENT_PROMPT_PATH)
    offenders = []
    for i, line in enumerate(text.splitlines(), 1):
        m = _FORBIDDEN_RE.search(line)
        if m:
            offenders.append(f"L{i}: {m.group(1)} :: {line.strip()}")
    assert not offenders, "hardcoded builtin tool name(s):\n" + "\n".join(offenders)


def test_subagent_prompt_no_zero_sycophancy_block():
    text = _read_prompt(SUBAGENT_PROMPT_PATH)
    assert "ZERO SYCOPHANCY" not in text
    assert "### Integrity and rigor" in text
