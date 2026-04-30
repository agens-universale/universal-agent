---
id: "swe-task-solver"
name: "swe-task-solver"
description: "Solves software engineering delivery tasks with risk-based planning, implementation, review, and verification. Use for features, bugfixes, behavior-changing refactors, test fixes, and implementation-ready code changes. Use exploration mode only when code work is likely and requirements or risks need clarification before delivery. Do not use for standalone codebase explanation, pure research, architecture ADRs, article writing, or project overview."
version: "0.9.1"
---

# Skill: SWE Task Solver

## Purpose
This skill defines a portable software-delivery protocol that can run with different tools, agent names, and handoff schemas. It is intentionally independent of any specific runtime implementation.

The protocol should help solve the task, not become the task. Use the lightest flow that is sufficient for the risk, and escalate strictness when evidence calls for it.

Map protocol roles to whatever the current runtime provides:
- **planner**: turns unclear or non-trivial work into an implementation-ready plan.
- **implementer**: makes scoped code changes.
- **reviewer**: checks correctness, scope, risk, and verification evidence.
- **specialist**: optional domain expert for narrow high-risk subtasks.
- **finalizer**: ends the turn using the runtime's local completion mechanism.

## Use When
- Implementing a feature, bugfix, or behavior-changing refactor.
- Changing one or more files with an observable success condition.
- Investigating implementation requirements before coding when code delivery is the likely next step.
- Applying quality gates before completion, not only after the fact.

Do not use this skill for simple explanations, read-only code lookup, or one-step commands with no delivery decision.

## Input Contract
Before coding, establish enough of:
- **Goal:** desired business or technical outcome.
- **Context:** relevant files, modules, system area, or observed failure.
- **Constraints:** compatibility, performance, security, data, runtime, or style limits.
- **Non-goals:** explicitly out-of-scope work.
- **Success criteria:** observable completion conditions.

If details are missing:
- Ask up to 3 focused questions only for blocking gaps.
- Use explicit assumptions for non-blocking gaps.
- Do not invent requirements that can affect behavior, public interfaces, data, security, compatibility, or rollback.
- Unknowns block implementation only when they can materially affect behavior, API, persisted data, security, compatibility, or rollback.

## Task State
Maintain a compact `task_state` throughout the work, especially for `standard`, `high-risk`, and long-running tasks.

Keep it short, but update it when facts change:
- selected mode and escalation triggers,
- goal and non-goals,
- acceptance criteria,
- current plan,
- changed files or intended change boundary,
- validation evidence,
- open risks, blockers, or evidence gaps.

Before review and closeout, compare the current result against `task_state` so original acceptance criteria and scope are not lost.

## Mode Selection
Select exactly one mode before implementation:
- `quick`: small localized work with low failure impact.
- `standard`: normal development flow and the default mode.
- `high-risk`: strict flow for sensitive, broad, or hard-to-rollback changes.
- `exploration`: analysis-only flow when coding would be premature.

Default to `standard`.

Escalation rules:
- Use `quick` only when all quick criteria are proven.
- If unsure between adjacent modes, choose the stricter one: `quick -> standard -> high-risk`.
- Missing information raises strictness only when it can materially affect behavior, API, data, security, compatibility, or rollback.
- A mode can be downgraded only with evidence, not optimism.

### Quick
Use only when all are true:
- The change is localized and mechanically simple.
- No public behavior/API, persisted data, security, concurrency, compatibility, error semantics, or integration boundary changes.
- The expected failure impact is low and easily reversible.
- A targeted check can validate the change.

Flow:
1. State a one-paragraph mini-plan.
2. Implement the scoped change.
3. Run a targeted check.
4. Run the quick review checklist.

### Standard
Use for ordinary features, bugfixes, and refactors when no high-risk trigger applies.

Flow:
1. Inspect relevant code before designing the change.
2. Produce a compact plan with scope, target files, acceptance criteria, and validation checks.
3. Implement the smallest scoped change.
4. Run available tests, lint, static checks, or focused runtime checks.
5. Review against the plan, project conventions, and verification evidence.

### High-Risk
Use when any trigger is present:
- Security, auth, permissions, secrets, privacy, or compliance.
- Persistence, schema changes, migrations, data loss, or user-data impact.
- Public API/CLI behavior, compatibility, or persisted contracts.
- Concurrency, caching, transactions, retries, idempotency, or distributed behavior.
- Broad integration, cross-module ownership boundary, or large refactor.
- Performance-critical path or resource scaling concern.
- Production incident, flaky bug, nondeterministic behavior, or hard-to-reproduce failure.
- External integration with hard-to-replay side effects.
- Two failed implementation attempts.
- Critical or major review finding.

Flow:
1. Create an explicit plan with rollback or mitigation notes when applicable.
2. Reproduce or characterize the risk before fixing when possible.
3. Implement one focused change set.
4. Run positive, negative/failure-path, invariant, and smoke/end-to-end checks as available.
5. Use independent or adversarial review if the runtime allows it; otherwise perform an explicit adversarial self-review.
6. Iterate at most 2 times before re-planning unless new evidence shows clear progress.

### Exploration
Use when requirements, architecture, or solution shape are not implementation-ready.

Do not implement code. Report:
- candidate approaches,
- trade-offs and risks,
- recommended implementation path,
- readiness signal: ready for implementation vs. needs user/design decision.

If exploration makes implementation ready, re-select the mode before coding.

## Universal Engineering Rules
- Keep changes within the user's requested scope.
- Prefer existing project patterns, helpers, and abstractions.
- Do not mix functional changes with unrelated cleanup.
- Do not silently add fallbacks/defaults not requested by the spec or user.
- Do not duplicate logic when reusable project code exists.
- Protect unrelated dirty work; never revert changes you did not make unless explicitly requested.
- If a plan no longer matches reality, pause for a micro-replan instead of stacking ad hoc fixes.

## Bugfix Discipline
For bugfixes:
- State the root-cause hypothesis before fixing.
- Reproduce or collect concrete evidence when practical.
- Record a compact repro contract: `steps -> observed -> expected`.
- Tie the fix to the hypothesis.
- If two attempts fail, stop and re-plan.

No bugfix should close with only "seems fixed" reasoning when a targeted repro or regression check is practical.

## Testing and Evidence
Behavior changes require regression evidence. RED/GREEN is preferred when practical, but equivalent evidence is acceptable when it is more realistic for the task.

Choose validation by mode:
- `quick`: targeted check + scope/invariant check.
- `standard`: targeted functional check + regression check when behavior changed + scope/invariant check.
- `high-risk`: positive check + negative/failure-path check + invariant check + smoke/end-to-end check when available.
- `exploration`: decision-quality evidence only; no implementation validation.

Evidence priority:
`real runtime/test evidence > deterministic static/contract evidence > simulated checks > reasoning-only evidence`.

Reasoning-only or simulated evidence must be reported as residual risk, not as equivalent to real validation.

## Evidence Gaps
Use one common evidence-gap report instead of special-case exception protocols.

When expected planning, review, or validation cannot be completed, state:
- what was not done,
- why it could not be done now,
- what evidence was used instead,
- residual risk,
- the concrete follow-up check or decision needed.

Do not claim complete success if a major required check is missing without this evidence-gap report.

## Review
Review strictness follows mode:
- `quick`: run the quick review checklist; do not replace it with a vague "looks good".
- `standard`: structured review pass against plan, acceptance criteria, conventions, and tests.
- `high-risk`: independent or adversarial review when possible.

Quick review checklist:
- Does the diff match the mini-plan and stay within scope?
- Are acceptance criteria satisfied?
- Did the targeted check actually exercise the changed behavior or artifact?
- Are error paths, null/empty inputs, or boundary cases relevant to this change?
- Did unrelated files, formatting, generated output, or cleanup leak into the diff?
- Is there any reason to escalate to `standard` before closing?

Prioritize findings by user impact:
- `critical`: must fix or stop.
- `major`: fix or request explicit user acceptance.
- `minor`: fix if cheap and in scope; otherwise report.
- `info`: optional context only.

Critical/major findings cannot be silently accepted by the agent. Fix them, or stop with a clear user decision point.

## Delegation
When delegating to another tool, agent, or role, pass only dense task context:
- objective,
- selected mode and escalation triggers,
- constraints and non-goals,
- relevant files/modules,
- acceptance criteria,
- required evidence.

Use specialists only for narrow questions where domain-specific uncertainty materially affects correctness or risk.

## Closeout
Before final response, ensure:
1. Acceptance criteria are satisfied or explicitly blocked.
2. The selected mode's flow was followed.
3. Validation evidence is present, or evidence gaps are reported.
4. Bugfixes include root-cause evidence when practical.
5. No unresolved critical/major review findings remain unless explicitly accepted by the user.
6. Scope stayed clean: no unrelated files or refactors leaked in.
7. Final result was checked against `task_state`.

Final response should be concise:
- what changed,
- what was validated,
- what risks or evidence gaps remain,
- recommended next step only if useful.
