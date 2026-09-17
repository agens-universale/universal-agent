from __future__ import annotations

from gc_core.intent_guard.validation_result import ValidationResult


def _bullet_block(title: str, items: list[str]) -> list[str]:
    cleaned = [str(x).strip() for x in items if str(x).strip()]
    if not cleaned:
        return []
    return [title, *[f"- {x}" for x in cleaned]]


# Agent-facing when continue-like reject and handoff itself is a user question (D5).
QUESTION_CONTINUE_HINT = (
    "You did not close the request — you asked the user a question instead. "
    "If you cannot proceed without that answer, ask via an available clarification tool "
    "when one is present; otherwise finish the turn with the question as the outcome. "
    "If the question is not blocking, stop asking, decide or act yourself from the current "
    "intent, and continue the work. Ask only when the answer blocks execution."
)


def build_finish_gate_feedback(result: ValidationResult) -> str:
    """
    Reject finish-gate text → `message` field in the finish-turn tool output JSON.

    Do not use raw `feedback_for_agent` from the validator LLM JSON (quality_gate §7.3).
    Tool-agnostic: no builtin tool names in agent-facing copy (§7.4). English only.
    """
    action = result.action
    lines: list[str] = [f"Finish gate: {action}."]

    if action == "accept":
        return lines[0]

    if action == "fix_report":
        lines.append(
            "The trace already closes the request; fix the finish-turn tool arguments "
            "without repeating steps that already succeeded in the trace."
        )
        lines.extend(
            _bullet_block("Report does not match trace or intent:", result.report_mismatches)
        )
        if not result.report_mismatches:
            lines.extend(_bullet_block("Notes (conflicts):", result.conflicts))
        lines.append(
            "Align solution_type, task_report, handoff, and comment with what is visible in the trace."
        )
        return "\n".join(lines)

    if action == "continue_work":
        if result.is_question:
            lines.append(QUESTION_CONTINUE_HINT)
            lines.extend(_bullet_block("Trace gaps:", result.trace_gaps))
            if not result.trace_gaps:
                lines.extend(_bullet_block("Additionally:", result.missing_requirements))
            return "\n".join(lines)
        lines.append(
            "The trace does not yet support current_user_requests; more work is required."
        )
        lines.extend(_bullet_block("Trace gaps:", result.trace_gaps))
        if not result.trace_gaps:
            lines.extend(_bullet_block("Additionally:", result.missing_requirements))
        lines.append(
            "Take the missing step (use the appropriate tool), then finish the turn again "
            "with an honest report via the finish-turn tool."
        )
        return "\n".join(lines)

    if action == "fix_report_and_continue":
        if result.is_question:
            lines.append(QUESTION_CONTINUE_HINT)
            lines.extend(_bullet_block("Trace gaps:", result.trace_gaps))
            if not result.trace_gaps:
                lines.extend(_bullet_block("Additionally:", result.missing_requirements))
            lines.extend(_bullet_block("Report mismatches:", result.report_mismatches))
            if not result.report_mismatches:
                lines.extend(_bullet_block("Notes (conflicts):", result.conflicts))
            return "\n".join(lines)
        lines.append(
            "Close trace gaps first, then align the finish-turn report with the trace."
        )
        lines.extend(_bullet_block("Trace gaps:", result.trace_gaps))
        if not result.trace_gaps:
            lines.extend(_bullet_block("Additionally:", result.missing_requirements))
        lines.extend(_bullet_block("Report mismatches:", result.report_mismatches))
        if not result.report_mismatches:
            lines.extend(_bullet_block("Notes (conflicts):", result.conflicts))
        lines.append("Do not repeat steps that already appear successful in the trace.")
        return "\n".join(lines)

    lines.append(
        f"Unknown action={action!r}; fix the finish-turn report or continue working."
    )
    return "\n".join(lines)


TEXT_ANSWER_REJECT_TOOL_HINT = (
    "Prefer finalizing this turn with a proper tool call over plain prose: if the work "
    "is genuinely done, use the designated finalization tool to deliver the answer; if it "
    "is not, call the appropriate tool for the next step. A structured tool handoff is "
    "preferred here."
)


def build_text_answer_reject_feedback(result: ValidationResult) -> str:
    """Judge rejected a prose text-answer as the turn's final answer.
    Agent-facing, tool-agnostic (§7.4). English only."""
    lines = [
        "Your text reply was not accepted as this turn's final answer: it does not "
        "adequately close the user's request. Continue the current turn; do not treat "
        "it as complete.",
        "",
        build_finish_gate_feedback(result),
    ]
    if not result.is_question:
        lines.extend(["", TEXT_ANSWER_REJECT_TOOL_HINT])
    return "\n".join(lines)
