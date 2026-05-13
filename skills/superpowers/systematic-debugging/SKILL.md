---
id: systematic-debugging
name: Systematic Debugging
description: 4-phase root-cause process for any non-trivial bug, test failure, or unexpected behavior. Use BEFORE proposing fixes. Prevents shotgun changes that mask symptoms.
version: 1.0.0
---

# Systematic Debugging

Stop guessing. Find the root cause, not a symptom. Four phases, in order.

## Phase 1 — Reproduce

- Get a deterministic reproduction. If the bug is "sometimes", you don't have
  a reproduction yet — find the trigger.
- Capture the EXACT input that triggers the bug, the EXACT command/sequence
  that exercises it, and the EXACT observed output.
- Format the repro as `steps -> observed -> expected`. Save to `memory_files`
  so it survives summarization.
- If you can't reproduce — STOP. Don't propose fixes for bugs you can't
  trigger. Ask the user for more context.

## Phase 2 — Isolate

- Bisect: narrow the failing path to the smallest set of code/data involved.
- Strip away anything that doesn't matter. Confirm the bug still reproduces.
- Use `execute_command "git log -10 <file>"` or `git bisect` to find the
  introducing commit if regression.
- Output of this phase: a minimal repro the user could read in 30 seconds.

## Phase 3 — Understand

- Read the relevant code via `read_file`. Trace data flow from input to point
  of failure.
- For each unexpected behavior, ask: "What is the proximate cause of THIS
  observed state?" Walk up the chain.
- Stop at the boundary you can change. Don't blame "the framework" / "the OS"
  unless you've actually traced through and the boundary is real.
- See `skills_load_reference("systematic-debugging", "root-cause-tracing")`
  for the chain-of-cause technique.

## Phase 4 — Fix at root cause

- The fix lands at the level you identified as root, not at the symptom level.
- Symptom-level fix: "catch the exception and return default". Hides the bug.
- Root-cause fix: "the input shape was wrong because earlier validation
  was missing — add it at the entry point".
- Add a regression test that fails on the old code, passes on the new one.
  This belongs to the `superpowers:test-driven-development` cycle.

## Hard rules

- **No shotgun fixes.** Don't change 5 things and hope one works. If your fix
  is to "try X" without knowing why X helps — go back to Phase 3.
- **No symptom suppression.** Catching exceptions to "make the error go away"
  is anti-debugging. Read `defense-in-depth` reference for how to validate at
  boundaries without burying root causes.
- **One hypothesis at a time.** Test it via the repro before moving to the next.
- **Two failed attempts → stop and re-plan.** If your fix made things worse or
  didn't help, you're missing context. Re-read the code, refine the hypothesis.

## References

- `skills_load_reference("systematic-debugging", "root-cause-tracing")` —
  chain-of-cause technique.
- `skills_load_reference("systematic-debugging", "defense-in-depth")` — where
  to validate inputs without masking bugs.
- `skills_load_reference("systematic-debugging", "condition-based-waiting")` —
  fixing flaky async/timing bugs without sleep-based hacks.

## Next step

After the fix is in and the regression test is GREEN:
- `skills_load("superpowers:verification-before-completion")` before claiming
  the bug is fixed.
