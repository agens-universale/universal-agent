---
id: brainstorming
name: Brainstorming Ideas Into Designs
description: Socratic refinement of an idea into an approved design before any code is written. Use BEFORE starting work on a feature, refactor, or behavior change — required gate for non-trivial creative tasks.
version: 1.0.0
---

# Brainstorming Ideas Into Designs

Turn a vague request into a concrete, approved design through collaborative
dialogue. Surface assumptions early. Avoid building the wrong thing.

## HARD-GATE

**Do NOT write code, scaffold files, or take implementation actions until the
user has approved a design.** This applies to every project, including ones
that look "simple". Simple-looking projects are where unexamined assumptions
do the most damage.

If you're tempted to skip this gate because the task is "too small" — that's a
red flag. The design can be short (a few sentences for trivial features), but
you MUST present it and get approval.

## Process

1. **Explore project context** — current files, existing patterns, recent
   commits in the relevant area. Use `read_file`, `list_files`,
   `execute_command "git log --oneline -20"` as needed.

2. **Clarifying questions, one at a time** — prefer multiple choice when
   possible. Don't overwhelm with a numbered list of 6 questions. Cover:
   - Purpose: what's the underlying goal?
   - Constraints: what must NOT break? what's out of scope?
   - Success criteria: how will the user know it works?

3. **Propose 2-3 approaches** — with tradeoffs. State your recommendation
   first, then alternatives. Don't pick silently among several valid options.

4. **Present the design in sections** — architecture, components, data flow,
   error handling, testing. Scale each to its complexity (a few sentences for
   straightforward parts, up to ~200 words for nuanced parts). Ask after each
   section whether it looks right.

5. **Get approval** — explicit "ok" / "yes" / "approved" before moving to
   implementation. Hedged responses ("looks good but maybe...") are NOT
   approval — clarify and re-present the affected section.

## Anti-Pattern: "This Is Too Simple"

If you catch yourself thinking any of these, STOP and brainstorm anyway:

- "Just a todo list, doesn't need a design"
- "Single utility function, obvious what it does"
- "Config change, what could go wrong"
- "User clearly knows what they want, just build it"

Every project goes through this skill. The design can be short, but it MUST
be presented and approved.

## Output

- **Trivial task** (single function, one-screen change): a short design
  summary in the chat is enough.
- **Multi-step task** (multiple files, non-obvious sequencing): a design doc
  MUST be saved to
  `docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md` and committed before
  moving on.

For design docs, structure:
- Problem / goals
- Solution (one-paragraph summary)
- Architecture / data model
- Open questions / out-of-scope

## Next step

After the user approves the design:

- **Trivial task** (single function, one-screen change): proceed directly with
  `superpowers:test-driven-development` for the implementation. No separate
  plan needed.

- **Multi-step task** (multiple files, non-obvious sequencing) —
  **Hard-gate before `writing-plans`:** spec file MUST exist on disk and be
  acknowledged by the user first. Your next tool calls, in order:

  1. `write_to_file` (or `append_to_file`) — save the validated design to
     `docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md` with the sections
     described in "Output" above.
  2. `ask_human` — show the user the saved path and ask them to review and
     confirm: e.g. "Design saved to <path>. Review it and reply 'ok' to
     proceed to the plan, or describe changes."
  3. ONLY after the user confirms: `skills_load("superpowers:writing-plans")`.

  Do NOT call `skills_load("superpowers:writing-plans")` before the spec
  file exists on disk AND the user has acknowledged it. Skipping this
  hard-gate means the plan is generated from informal in-chat design —
  exactly the unexamined-assumption antipattern this skill exists to
  prevent.

Do NOT start writing implementation code in this same turn after approval.
Switch to the plan/TDD skill and let its discipline drive the next steps.
