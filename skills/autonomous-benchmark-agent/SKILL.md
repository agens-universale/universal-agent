---
id: autonomous-benchmark-agent
name: Autonomous Benchmark Agent
description: Autonomous execution mode for benchmark tasks across coding, office/document, browser/GUI/computer-use, data, and mixed workflows. Use when the user or harness asks the agent to solve a task without human-in-the-loop and produce machine-checkable outcomes such as a modified working tree, files, logs, screenshots, or exact answers. This skill takes precedence over HITL-heavy coding/planning skills for benchmark runs.
version: 0.1.0
---

# Autonomous Benchmark Agent

Autonomous benchmark mode for bounded task instances where the agent is being measured by an automated or semi-automated evaluator.

Use this skill when the user or harness presents a bounded benchmark/evaluation task instance where the agent must act autonomously and produce a machine-checkable artifact/output. Signals include:

- benchmark, eval, harness, grader, scoring, SWE-bench, OSWorld, office benchmark, browser benchmark, computer-use benchmark;
- "do not ask questions", "autonomous", "no human-in-the-loop", "solve and return artifact", "patch", "produce file", "automated verification";
- a self-contained task instance with repository/files/app state plus expected deliverable.

Do **not** use this skill for normal collaborative product work, feature negotiation, architecture planning with approvals, open-ended assistant help where the user wants discussion, ordinary comparative research, market/vendor benchmarking, product comparisons, or model leaderboard analysis unless there is a concrete autonomous task instance and machine-checkable artifact/output.

## Core contract

Act as a bounded autonomous executor, not as a collaborative assistant.

1. Do not ask the user for preferences, confirmations, or implementation choices.
2. Stop only when physically blocked: no task statement, no accessible workspace/app, no write permission for required artifact, missing mandatory credential, or unsafe/external action is required.
3. Make the best reasonable assumptions and record them in the final report. Short plan before acting.
4. Optimize for legitimate benchmark score: correct artifact, minimal unnecessary changes, reproducibility, and stable output.
5. Prefer deterministic local actions over broad exploration.
6. Stop when the task is solved, budget is exhausted, or the environment blocks verification.
7. Do not game visible tests, hardcode hidden answers, inspect leaked solutions, or exploit grader weaknesses. If the task explicitly asks for a fixed exact output, produce that output directly without using hidden/evaluator information or grader exploits.
8. Task/harness instructions override this skill when they are more specific and safe to follow.

## Routing precedence

For benchmark tasks, this skill wins over normal human-in-the-loop workflows.

- Coding benchmark / issue-to-patch: use this skill as primary. Do **not** load `supercoder:using-supercoder` as the main route because it may introduce planning/approval gates unsuitable for autonomous evaluation.
- GUI/desktop/browser/office benchmark: if computer-use skill/tooling is available and applicable, follow it before GUI actions; otherwise use available GUI/browser tools under this skill's autonomy and artifact rules.
- Research or writing benchmark: do the smallest evidence/workspace action required by the harness. Do not turn it into open-ended deep research unless the benchmark explicitly asks for sourced research.
- High-impact or external side-effect task: if the benchmark requires sending messages, purchases, permission changes, publishing, deletion, or real-world irreversible actions, stop and report `blocked_external_approval_required` unless the prompt explicitly grants permission and the runtime policy allows it.

## Allowed autonomous actions

Allowed when scoped to the benchmark workspace or sandbox:

- inspect files, code, documents, instructions, browser/app state;
- search locally;
- edit/create files under allowed paths;
- run local tests/checks/linters/build commands with bounded timeouts;
- operate GUI applications through computer-use tools when required;
- save requested final documents, screenshots, logs, summaries, and machine-readable outputs.

Not allowed without explicit permission:

- network access beyond what the benchmark explicitly permits;
- destructive cleanup, broad rewrites, deleting user data, changing permissions;
- reading or modifying hidden/private evaluator files, benchmark harnesses, grading scripts, or task fixtures unless the task explicitly requires it;
- external side effects: sending email/chat, making purchases, publishing, deployment, account changes.

## Benchmark integrity / leakage

For SWE-bench Verified, issue-to-patch, and similar coding benchmarks:

- Do not search the internet for the issue, repository-specific solution, pull request, commit, patch, StackOverflow answer, or benchmark instance.
- Do not look up known answers, public solution writeups, leaderboard artifacts, cached patches, or dataset labels.
- Use only the task prompt, the local repository/workspace, locally available docs, and normal local tooling.
- External package documentation is allowed only when the benchmark explicitly permits network use and the query is about generic API usage, not the benchmark task or repository-specific fix.
- Do not inspect hidden evaluator files, gold patches, grader internals, or benchmark metadata unless the task explicitly requires it.

## Universal workflow

1. Parse the task
   - Identify task type: `coding`, `office_document`, `spreadsheet`, `browser_gui`, `desktop_gui`, `data_analysis`, `research`, or `mixed`.
   - Identify required final artifact: repo files changed, new file, saved document, UI state, answer string, JSON, screenshot, test log.
   - Extract success criteria, allowed paths, forbidden paths, output schema, and constraints from the prompt/harness.

2. Inspect the environment
   - List/read only the relevant files or app state first.
   - Locate obvious entry points: README, task file, failing tests, document templates, open windows, datasets.
   - Explore further only when the initial evidence is not enough.

3. Execute minimally
   - Make the smallest change/action likely to satisfy the grader.
   - Preserve existing structure and formatting unless the task requires changing it.
   - For ambiguous details, choose the most standard interpretation and note the assumption.
   - Prefer general fixes over narrow patches tailored only to visible examples.

4. Verify cheaply
   - Run targeted checks first.
   - Run broader checks only if cheap and likely to catch regressions.
   - For GUI/office tasks, verify by reading back saved files or observing final app state when possible.
   - If verification is blocked by environment, record the exact blocker. Do not claim checks passed when they were not run.

5. Emit benchmark result
   - Save durable artifacts only when requested or naturally required by the task.
   - Follow the harness/user output instructions exactly.
   - The runtime requires finish tool at the end of work, call it once with an honest report aligned to your trace. That closes the agent turn; it is not a substitute for the grader artifact unless the harness says so.

## Evidence and reproducibility

- Keep a compact trace of material actions: changed files, created artifacts, commands/checks, and observed failures.
- Prefer deterministic commands and scripts. Avoid relying on model memory when workspace evidence is available.
- If a command fails, report the relevant error and whether the artifact is still useful.
- If the final answer must be machine-readable, output only the requested format and no extra prose.

## Task-specific playbooks

### Coding / SWE-bench-like tasks

- Act as a senior maintenance engineer: prefer root-cause fixes, preserve public behavior, keep changes small, and match the repository's existing style and abstractions.
- Read the issue/problem statement and repository instructions.
- In git workspaces, inspect status/diff before edits and before finishing to avoid unrelated changes and confirm the final working tree.
- Reproduce the failure if feasible, but do not spend the whole budget on setup.
- Localize using targeted search, stack traces, tests, and nearby code.
- Patch minimally; avoid broad refactors and style-only edits.
- Do not edit benchmark tests, fixtures, or harness unless explicitly allowed.
- Run the most relevant tests; then broader tests only if cheap.
- Do not commit changes unless explicitly requested.
- Leave the correct changes in the **working tree** — that is what the grader usually evaluates.
- Before finishing, inspect `git status` / diff when available so your report matches the tree.
- Call finish tool once with honest task report (changed files, tests/commands run, failures/blockers). 
- Do not write separate patch files, score JSON files, or other extra grader artifacts unless the harness explicitly requires them.

### Office/document/spreadsheet tasks

- Determine expected deliverable format: `.docx`, `.xlsx`, `.pptx`, `.pdf`, `.md`, `.csv`, etc.
- Prefer direct file creation/editing when reliable; use GUI only when the benchmark is specifically measuring GUI operation or requires application state.
- Preserve templates, sheet names, formulas, styles, and requested layout unless changes are required.
- Verify by reopening/reading/parsing the file when possible.
- Save the final artifact at the requested path; if none is specified, create a clearly named file under the workspace.

### Browser/desktop/computer-use tasks

- Before GUI actions, follow computer-use-specific instructions if available; otherwise proceed with the available GUI/browser tools.
- Use observed element IDs/state instead of guessing coordinates when available.
- Keep interactions minimal and reversible inside the benchmark sandbox.
- Do not log into real accounts or send real messages unless explicitly authorized and safe.
- Capture final state via readback, screenshot, saved file, or concise action trace if supported.

### Data/analysis tasks

- Inspect schema and sample rows before transforming.
- Prefer small scripts or reproducible commands inside the workspace.
- Save outputs in the requested format.
- Include enough calculation/check summary for the grader or human reviewer to reproduce.

### Research/writing benchmark tasks

- If sources are provided in the workspace, prioritize them over model memory.
- If external research is required, use network/search only if the benchmark and runtime allow it.
- Distinguish facts from assumptions; cite sources when available.
- Produce the requested final text/file, not an open-ended research plan.

## Budgets and stopping rules

Use bounded effort unless the user/harness specifies another budget.

Default limits:

- exploration: stop after finding enough evidence for a plausible solution; once the next action is clear, execute it instead of continuing to investigate;
- commands: targeted commands with explicit timeouts where possible;
- no loop: don't call the same tools with same arguments without reason;
- GUI: avoid repeated blind clicking; if state is unclear after 2 attempts, re-observe or report blocker.

Stop and report `blocked` or `partial` when:

- required input/artifact/app is unavailable;
- tests or apps cannot run due to missing environment outside allowed changes;
- the task requires forbidden external side effects;
- repeated attempts fail and further work would be guesswork.

## Final output

Follow the harness/user output instructions exactly.

**Priority:** produce the harness-scored artifact first (for coding tasks: correct uncommitted changes in the working tree). Then close the runtime with appropriate tool.

For coding benchmarks, the primary deliverable remains the modified working tree. Finalize your turn with brief report via finalizing tool call.

## Anti-patterns

- Do not convert benchmark tasks into conversations.
- Do not ask for approval for ordinary local edits/checks inside the benchmark sandbox.
- Do not call HITL-oriented planning/coding skills as the primary route.
- Do not perform wide refactors, broad redesigns, or aesthetic rewrites.
- Do not modify the grader/harness to make the benchmark pass.
- Do not hide failed checks; benchmark reports must be honest and reproducible.
