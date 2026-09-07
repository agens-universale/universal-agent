---
id: executing-plans
name: Executing Implementation Plans
description: Run a written implementation plan task by task with self-review checkpoints between groups of tasks. Use when a plan file exists and is approved.
version: 1.0.0
---

# Executing Implementation Plans

You have a plan file (e.g. `docs/supercoder/plans/<date>-<feature>-plan.md`).
Each task in it is a single commit. Your job is to execute them mechanically
and verify each completes before moving to the next.

## Process

1. **Read the plan once.** Note the total task count. Record your position
   in durable storage that survives context summarization (e.g.
   `task_pointer: 3` and `tasks_remaining: 11`) so progress is not lost.

2. **For each task, in order:**
   a. Load that task's text into context (re-read if needed).
   b. Execute its steps in sequence. Each step is one action.
   c. After test steps, verify the expected outcome by running the test
      (e.g. `pytest tests/test_x.py::test_y -v`).
   d. After implementation steps, re-run the test to confirm GREEN.
   e. Commit when the task's last step says so.
   f. Update that durable position record with the new task_pointer.

3. **Checkpoint every 3-5 tasks.** Pause and run the full project test
   suite (or the relevant subset) to catch regressions early. Report a
   short status to the user. Don't ask permission to continue unless
   something failed — the user already approved by approving the plan.

4. **If a task fails** (test won't pass, code won't compile, command errors):
   - Read the failure carefully. Don't retry the same thing twice.
   - If the problem is a typo / wrong import / minor adjustment — fix and continue.
   - If the problem reveals the plan is wrong (e.g. assumed a function exists
     that doesn't) — STOP, report to user, do NOT skip ahead. The plan needs
     a fix, not a workaround.

## Hard rules

- **One task at a time.** Don't batch multiple task commits.
- **TDD discipline.** When a task says "run test, see it fail" — actually run
  it. Don't skip to implementation because "you know it would fail".
- **Don't drift from plan.** If you notice the plan could be improved while
  executing, NOTE it (in durable storage that survives summarization), do
  not modify in-flight. The
  plan is the contract.
- **Commit messages from the plan, verbatim.** Don't paraphrase.
- **No silent expansion of scope.** If the plan touches files A and B and you
  see a related issue in C, do NOT fix C in the same commit. Report it
  separately or note for a follow-up.

## Self-review per task

Before marking a task done:

- Did all listed steps complete with the expected outcome?
- Did the commit include only the files the plan listed?
- Are there any TODO / FIXME / debug-prints I accidentally left in?

## Next step

After the last task is committed:

- Load `skills_load("supercoder:verification-before-completion")` for the
  final sanity check before claiming done.
- Then `skills_load("supercoder:requesting-code-review")` if the user is
  ready to hand off for review.
