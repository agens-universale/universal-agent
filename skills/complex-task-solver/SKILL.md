---
id: "complex-task-solver"
name: "complex-task-solver"
description: "Strict orchestration protocol for high-impact, multi-step tasks with external side effects, blockers, irreversible actions, or cross-tool workflows. Use when the task requires planning gates, critic loops, approval before side effects, and recovery from failed attempts. Do not use when a specialized skill directly covers the task, such as SWE implementation, architecture design, topic research, article creation, article checking, or project overview."
version: "0.4.0"
---

# Skill: Complex Task Solver — Universal Strict Orchestration

## Overview
Universal strict protocol for complex tasks in any domain: research, browser flows, OS actions, form filling, bookings, data handling, and software work.
The goal is reliable execution quality under uncertainty through mandatory self-correction loops and explicit safety gates.

## Use When
- The task is multi-step and cross-tool (browser + files + shell + APIs).
- Requirements are evolving and need iterative re-planning.
- The workflow has decision points, blockers, or external dependencies.
- The output is not just code and may include forms, operations, analysis, or submissions.

## Do Not Use When
- The task is trivial or one-step.
- A specialized project skill is a better fit (for example, architecture-only design or pure SWE delivery).
- No meaningful execution decisions are required.

## Language Strategy
- Final user-facing content must be in Russian.
- Research may use any language, but conclusions and decisions are reported in Russian.
- Use stable Russian technical terminology; keep common English terms only when useful.

## Roles
- `Planner`: decomposes goal into executable steps with checkpoints.
- `Actor`: performs the current step using available tools.
- `Critic`: validates relevance, correctness, and user-intent alignment.
- `SpecialAgent`: optional specialist for narrow high-uncertainty subtasks.
- `Orchestrator`: controls order, gates, retries, and stop/replan conditions.

Strict role separation is mandatory: `Critic` never executes side-effect actions.

## Mandatory Strict Workflow
Every non-trivial task follows the same strict process:
1. Planner-Critic pass on plan.
2. Step-Check before each action.
3. Actor execution.
4. Actor-Critic validation loop until pass.
5. Explicit approval gate before irreversible action.

## Universal Workflow

### Phase 1: Framing, Constraints, and Plan
1. Define objective, constraints, and success criteria.
2. Split task into atomic steps with expected outputs and rollback notes where possible.
3. Mark irreversible/externally visible steps that require explicit approval.
4. Run mandatory Planner-Critic review:
   - Plan item coverage.
   - Dependency correctness.
   - Feasibility and missing prerequisites.
   - Risk and safety adequacy.
5. Revise plan until `Planner-Critic: Pass`.

### Phase 2: Sequential Step Execution Loop
For each step:
1. Run Step-Check (mandatory):
   - Is this still the right step given latest state?
   - Are inputs, permissions, and dependencies valid?
   - Has a blocker appeared that requires replan?
2. Route to `Actor` or `SpecialAgent`.
3. Execute one focused action set.
4. Run Actor-Critic audit with structured findings:
   - `[Element] -> [Issue] -> [Evidence] -> [Action Required] -> [Status]`
5. Revise and re-check until `Pass`.
6. Move to next step only after current step passes.

### Phase 3: Final Validation
1. Confirm result against original user intent.
2. Confirm all planned success criteria are met or explicitly blocked.
3. List evidence, unresolved assumptions, and residual risks.
4. Return concise outcome and next safe action.

## Safety Gates for Irreversible Actions
Before payment/submit/publish/final send/destructive command:
- Present a compact preflight summary:
  - target system/page
  - exact action
  - critical parameters
  - expected irreversible effect
- Request explicit user confirmation.
- If confirmation is missing, stop at ready-to-confirm checkpoint.

## Fact Checking and Evidence
Mandatory:
- Verify all non-trivial factual claims with live sources/tools when feasible.
- Validate operational outcomes using direct tool evidence (not only reasoning).
- Distinguish:
  - observed evidence (tool/runtime result),
  - inferred reasoning,
  - unverified assumptions.

## Output Contract
Use concise structure unless user asks otherwise:

```markdown
# Task Outcome: <Title>

## Goal and Scope
- Goal:
- Scope boundaries:

## Execution Log
1. <Step> — Status: pass | blocked | needs confirmation
2. <Step> — Status: ...

## Result
- What is completed:
- What is pending:

## Evidence and Risks
- Evidence:
- Assumptions:
- Residual risks:

## Next Action
- Recommended immediate next step:
```

## Escalation and Recovery
- If 2 attempts fail on one step, trigger micro-replan.
- If blocker requires manual user action (login/captcha/2FA/approval), stop and hand control to user.
- If context drifts materially, re-run planning for remaining steps.

## Anti-Patterns
- Skipping Planner-Critic or Step-Check because the task "looks easy".
- Proceeding with irreversible actions without explicit confirmation.
- Hiding uncertainty instead of stating assumptions.
- Continuing retries without new evidence.
