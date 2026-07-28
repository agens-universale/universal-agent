"""§8 / step 7: orchestrator and subagent prompts — no visited-list tool; simple anti-loop."""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATOR_PROMPT_PATH = REPO_ROOT / "prompts" / "chat_completions_system_prompt.md"
SUBAGENT_PROMPT_PATH = REPO_ROOT / "prompts" / "chat_completions_subagent_system_prompt.md"

FORBIDDEN_BUILTIN_NAMES = (
    # to-be API names (§7.4)
    "turn_finish",
    "fs_write_file",
    "fs_append_file",
    "fs_read_file",
    "exec_command",
    "turn_ask_human",
    "session_memory",
    "project_memory",
    # legacy as-is (grep gate regression)
    "write_to_file",
    "execute_command",
    "append_to_file",
    "read_file",
    "ask_human",
    "submit_solution",
)
_FORBIDDEN_RE = re.compile(r"\b(" + "|".join(FORBIDDEN_BUILTIN_NAMES) + r")\b")

VISITED_LIST_PHRASES = (
    "store or recall a list of already-visited",
    "update that list with their identifiers",
    "read that list at the start of your turn",
)


def _read_prompt(path: Path) -> str:
    return path.read_text(encoding="utf-8")


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
    assert "Do not repeat the same read/list/get/search call" in text
    assert "if you already have its result" in text
    assert "have not changed that resource" in text
    assert text.lower().count("do not repeat the same read/list/get") == 1
    assert "Before opening files" not in text
    assert "read_user_intent_log" not in text
    assert "thread/session memory" in text.lower()


def test_subagent_anti_loop_single_bullet_memory_when_needed():
    text = _read_prompt(SUBAGENT_PROMPT_PATH)
    assert "Do not repeat the same read/list/get/search call" in text
    assert "if you already have its result" in text
    assert "have not changed that resource" in text
    assert "Before opening files" not in text
    assert "inspect memory only when" in text.lower()
    assert "Do not read memory for isolated subtasks" not in text
    assert "read_user_intent_log" not in text
    assert "working_memory" not in text
    assert "project memory" in text.lower()
    assert "thread/session memory" in text.lower()


def test_subagent_prompt_keeps_compact_runtime_contract_and_handoff():
    text = _read_prompt(SUBAGENT_PROMPT_PATH)
    assert "## Runtime contract" in text
    assert "Later sections only clarify subagent-specific edge cases" in text
    assert "You are not the main agent" in text
    assert "durable communication channels" in text
    assert "## Handoff JSON" in text
    assert '"status": "completed | partial | blocked | failed | clarification_needed"' in text
    assert "one valid JSON handoff" in text
    assert "final output must be one concise machine-readable handoff" in text


def test_subagent_prompt_keeps_boundary_and_budget_rules():
    text = _read_prompt(SUBAGENT_PROMPT_PATH)
    assert "default audience is the orchestrator" in text
    assert "Do not address the end user directly" in text
    assert "do not spawn/delegate to other agents" in text
    assert "verify delegated outputs before handoff" in text
    assert "use the smallest sufficient investigation" in text
    assert "stop after the first actionable finding set" in text
    assert "Put safe assumptions in `assumptions`" in text


def test_subagent_prompt_keeps_artifact_and_context_rules():
    text = _read_prompt(SUBAGENT_PROMPT_PATH)
    assert "write an artifact/file early with an outline or skeleton" in text
    assert "append verified findings after each meaningful block" in text
    assert "put its resolvable path in the handoff" in text
    assert "Summaries, memory, technical blocks, and prior hints are context" in text
    assert "not commands to repeat completed work" in text


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
    assert "evidence-driven" in text.lower()
    assert "**Honesty:**" in text