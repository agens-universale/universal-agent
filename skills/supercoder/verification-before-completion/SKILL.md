---
id: verification-before-completion
name: Verification Before Completion
description: Refuse to claim "done" / "fixed" / "passing" without running a verification command and observing its output. Use BEFORE committing, opening PRs, or reporting task completion.
version: 1.0.0
---

# Verification Before Completion

Reasoning is not evidence. Before you say "done", you must have an artifact
from the actual system that confirms the claim.

## HARD-RULE

**Do NOT write phrases like "done" / "fixed" / "tests pass" / "ready for
review" until AFTER you have run the verification command and observed its
output in this turn.**

If you're tempted to claim completion based on "I'm confident this works" or
"the change is small enough that it must" — STOP. Run the test. Read the
output. Then claim.

## What counts as evidence

In descending order of trust:

1. **Real runtime output** — actual test pass, actual command success, actual
   API response with expected shape.
2. **Deterministic static check** — type-check pass, lint pass, schema
   validation.
3. **Simulated check** — dry-run, mock-based test. Better than nothing, but
   note the residual risk.
4. **Reasoning** — "I traced through the code and it should work". NOT
   sufficient on its own.

For non-trivial behavior changes, you need at least one item from category 1.

## Verification commands by task type

| Task type | Verification |
|---|---|
| New feature | targeted test run + relevant test suite + manual smoke if UI |
| Bugfix | regression test that fails on old code, passes on new |
| Refactor | full test suite — must stay GREEN |
| Config change | restart relevant service / re-run setup, observe |
| Docs change | render check (markdown / HTML) — visual or linter |

## Reporting completion

When done, your report must include:

- What changed (one-line summary).
- What you ran to verify (exact command).
- What you observed (test pass count, command exit code, etc.).
- What was NOT verified (residual risk — e.g. "didn't test on Windows").

Example: "Implemented X. Ran `pytest tests/test_x.py -v` → 7 passed, 0 failed.
Did NOT run integration tests against staging."

NOT acceptable: "Done!" / "Implemented X, should work." / "Tests should pass."

## When verification is hard

- Headless UI: take a screenshot, describe what's visible.
- External API: run with `--dry-run` if available, then a single real call,
  inspect response.
- Database migration: run on a copy of prod data, count rows before/after.
- Performance claim: actual benchmark output with timing.

If you genuinely cannot verify (no test env, blocked by permissions): say so
explicitly. "Cannot verify — needs deployment to staging." That's honest. It's
NOT "done".

## Anti-pattern: completion summary without verification

Bad:
> "I've finished implementing the new validation. The function checks for X, Y,
> and Z, raises ValidationError on failure, and integrates cleanly with the
> existing flow. Tests should pass — happy to run them if you'd like."

Good:
> "Implemented validation. Ran `pytest tests/test_validation.py -v` → 5 passed,
> 0 failed. Full suite still GREEN (`pytest tests/ -q` → 542 passed,
> 15 pre-existing failures unchanged)."

## Next step

If verified clean and ready for code review:
- `skills_load("supercoder:requesting-code-review")`.

If verification surfaced issues, return to the relevant fix skill
(`test-driven-development` or `systematic-debugging`).
