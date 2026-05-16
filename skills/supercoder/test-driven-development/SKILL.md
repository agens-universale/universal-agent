---
id: test-driven-development
name: Test-Driven Development
description: RED-GREEN-REFACTOR cycle for any feature or bugfix that changes behavior. Use BEFORE writing implementation code. Loads alongside writing-plans/executing-plans for typical feature work.
version: 1.0.0
---

# Test-Driven Development

Three steps per behavior change: write a failing test (RED), make it pass with
minimal code (GREEN), then refactor without breaking the test (REFACTOR).

## Why RED first

A test that's never been RED has never proven the production code is what made
it pass. "I wrote the test after the code and it passed" → you might be testing
the wrong thing, or the test might pass on broken code too. The RED step is
non-negotiable evidence.

## Cycle

### 1. RED — write a failing test

- Write the test that would pass IF the feature/fix were complete.
- Run it via `pytest tests/path/test_x.py::test_name -v`.
- Verify it actually fails, and fails for the expected reason (e.g.
  `AttributeError: function not defined`, not an unrelated import error).
- If the test passes immediately — STOP. Either the feature already works
  (you didn't need this change), or your test isn't exercising what you think.
  See `skills_load_reference("test-driven-development", "testing-anti-patterns")`.

### 2. GREEN — minimal code to pass

- Implement the SMALLEST change that makes the test pass.
- Run the test, verify it passes.
- Do NOT add code beyond what the test requires. "I know I'll need this later"
  is a YAGNI antipattern — write that test when you need it, not now.
- Run the full test file (not just the new test) to check for regressions.

### 3. REFACTOR — improve without changing behavior

- Now you can clean up: extract helpers, rename variables, simplify branches.
- After each refactor, re-run the test. Stay GREEN.
- If a refactor breaks the test — your refactor changed behavior. Either it's
  a bug in the refactor, or your test was over-specified.

## Hard rules

- **Test BEFORE code.** No exceptions for "obvious" features.
- **Run-to-fail is mandatory.** Skipping the RED verification = skipping the
  whole methodology.
- **One behavior per test.** Don't bundle assertions across unrelated cases.
- **Real evidence over reasoning.** "I think it works" is not a substitute for
  "I ran the test and it's GREEN".

## When NOT to use TDD

- Pure exploration / spike code that won't ship.
- Print-statement debugging (use `supercoder:systematic-debugging` instead).
- Schema-only changes (config tweaks, dependency bumps).
- Documentation / typo fixes.

## Reference

For common mistakes that defeat TDD:

```
skills_load_reference("test-driven-development", "testing-anti-patterns")
```

## Next step

After feature/fix is GREEN and committed:
- `skills_load("supercoder:verification-before-completion")` before claiming done.
