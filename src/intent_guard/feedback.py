from __future__ import annotations

from intent_guard.validation_result import ValidationResult


def _bullet_block(title: str, items: list[str]) -> list[str]:
    cleaned = [str(x).strip() for x in items if str(x).strip()]
    if not cleaned:
        return []
    return [title, *[f"- {x}" for x in cleaned]]


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


def build_finish_gate_reject_technical_user_content(feedback: str) -> str:
    """Technical user message after finish gate reject — continue the open turn."""
    body = str(feedback or "").strip()
    lines = [
        "Finish gate rejected this turn: the handoff does not match the user's intent.",
        "Continue working on the current turn; do not treat the turn as complete.",
    ]
    if body:
        lines.extend(["", "Details:", body])
    return "\n".join(lines)
