---
id: writing-plans
name: Writing Implementation Plans
description: Produce a concrete TDD-style implementation plan from an approved design. Tasks are bite-sized (2-5 min each), with exact file paths and actual code in every step. Use AFTER design is approved, BEFORE execution.
version: 1.0.0
---

# Writing Implementation Plans

Turn an approved design into a step-by-step plan that another engineer (or a
subagent) can execute mechanically. Quality of the plan determines quality of
the execution — invest in concrete code, exact paths, exact commands.

## Plan structure

Every plan starts with:

- **Goal** (one sentence).
- **Architecture** (2-3 sentences on the approach).
- **Tech Stack** (key libraries/tools).
- **File map** (created / modified, with one-line purpose each).
- **Tasks** (numbered, each producing a single commit).

## Task structure

Each task touches a small, coherent slice of the file map. Format:

```
### Task N: <Component Name>

**Files:**
- Create: `exact/path/file.py`
- Modify: `exact/path/existing.py`

- [ ] Step 1: Write failing test  (show the test code)
- [ ] Step 2: Run test, verify it fails  (exact pytest command + expected output)
- [ ] Step 3: Implement  (show the implementation code)
- [ ] Step 4: Run test, verify it passes
- [ ] Step 5: Commit  (exact `git commit -m "..."` command)
```

Each step is one action, 2-5 minutes of work.

## Hard rules

- **No placeholders.** "TBD" / "implement later" / "fill in details" / "handle
  edge cases" / "similar to Task N" — these are plan failures. Every step
  must show the actual content.
- **Show code, not descriptions.** Steps that change code MUST include the
  actual code block (test code, implementation code, command). A reader should
  be able to execute the step without re-reading the design.
- **TDD by default.** Test step BEFORE implementation step, with explicit
  run-to-fail verification.
- **Exact paths and commands.** No `<path>` or `<test_command>` placeholders.
- **One commit per task.** Don't bundle unrelated changes.
- **Self-contained tasks.** Each task should make sense if read alone. If
  context from another task is needed, repeat it (don't say "see Task 3").

## Self-review after writing

Look at the plan with fresh eyes. Verify:

1. **Spec coverage.** Each requirement in the design has at least one task.
2. **Placeholder scan.** Search for the red-flag phrases above and fix.
3. **Type consistency.** A function called `clearLayers()` in Task 3 and
   `clearFullLayers()` in Task 7 is a bug — fix it.
4. **Scope check.** Each task touches files that make sense as one commit.
   If a task touches 8 unrelated files, split it.

Fix issues inline. No need to re-review.

## Output

Plan saved to `docs/superpowers/plans/YYYY-MM-DD-<feature-name>-plan.md`.

Commit the plan file before starting execution (the design+plan commit makes
the spec traceable in git history).

## Next step

After the user reviews and approves the plan, load
`skills_load("superpowers:executing-plans")` to run it task by task. Do NOT
start executing in the same turn that wrote the plan — let the user review
the plan file first.

If the plan is small and the user explicitly says "just do it", you can
proceed inline without the separate executing-plans skill — but the default
is to pause for review.
