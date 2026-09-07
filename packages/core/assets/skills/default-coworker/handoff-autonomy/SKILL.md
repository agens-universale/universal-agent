---
id: handoff-autonomy
name: Coworker Handoff and Autonomy
description: Method skill for autonomy boundaries, approval gates, handoffs, and continuity. Use when the agent may coordinate tools, delegate, run longer work, or approach external/irreversible actions.
version: 0.1.0
---

# Coworker Handoff and Autonomy

Boundaries and continuity for work that outlasts one turn. Standalone method — does not
assume any particular task mode is active.

## Hard rules

**External or irreversible actions require explicit confirmation with exact action summary.**

**Do not store secrets, raw logs, unnecessary personal data, or hidden reasoning in memory.**

**State material doubts, contradictions, or safer reframings before executing under autonomy.**

## Autonomy ladder

- **L0** answer only — no tools except required output/finalization.
- **L1** inspect/recommend — read/search/check; no modifications.
- **L2** prepare artifact — draft/report/plan in allowed workspace.
- **L3** local execution — edit/run/check within allowed paths.
- **L4** orchestrate — skills/subagents/multiple tools with validation.
- **L5** external/irreversible — send/deploy/pay/delete/permission/public/commitment; ask approval.

Choose the lowest sufficient level. If uncertain, go one level lower.

## Approval prompt

When approval is needed, include: exact action, target/system/person, expected effect,
risks/irreversibility, what happens if approved.

## Handoff protocol

Preserve compact state: goal, success criteria, constraints, completed steps + evidence,
open blockers, next action, artifact paths. Bulky detail → files; durable facts → memory only
when appropriate and non-sensitive.

## Delegation protocol

Delegate only when complexity or parallelism justifies it. Give: task title, deliverable,
inputs, constraints, success criteria, required verification. Validate outputs before
reporting as final.

## Anti-patterns

- Vague approval requests.
- Executing past an approval boundary.
- Persisting sensitive data for convenience.
- Delegating to appear sophisticated.
- Hiding critique in handoff.
- Improvising multi-step recovery, planning gates, critic loops, or approval choreography
  inside coworker modes when a dedicated high-impact orchestration skill would fit better.

## Next step

Apply the chosen autonomy level to the active task. If the workflow needs recovery,
planning gates, critic loops, or approval choreography across many steps — stop and
return to routing; do not imitate full orchestration here. For approval or continuity
only (without that scope), continue with this skill.
