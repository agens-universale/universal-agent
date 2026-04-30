---
id: "architecture-design-research"
name: "architecture-design-research"
description: "Designs software architecture for ambiguous engineering problems by framing constraints, comparing architecture options, evaluating trade-offs, validating hard constraints, and producing ADR-style decisions plus implementation handoff. Use when the user asks to design a system, subsystem, API, workflow, compare software architectures, or plan software before coding. Do not use for general topic research or direct implementation."
version: "0.0.2"
---

# Architecture Design Research

## Purpose
Use this skill for open-ended architecture work where requirements are incomplete, several designs are plausible, and decision quality matters more than immediate coding.

Default outcome: a decision-ready architecture artifact and a clean handoff for implementation roles.

## Use When
- The user asks to design a new subsystem, platform capability, interface, or workflow.
- The task requires comparing architecture options, not just implementing a known solution.
- The user asks about long-term effects: scalability, operability, evolution cost, lock-in, maintainability.
- Requirements are ambiguous and must be framed before coding.

Do not use this skill for simple bugfixes, localized refactors, or one-step implementation tasks.

## Workflow

### 1) Problem Framing
Create a compact framing section before proposing solutions:
- Objective and business/technical outcome.
- Constraints: runtime, performance, security, team capacity, deadlines, compatibility.
- Non-goals and scope boundaries.
- Quality attributes (ordered): e.g., reliability, latency, operability, extensibility, cost.
- Unknowns and assumptions.

If blocking ambiguity remains, ask focused questions. If not blocking, continue with explicit assumptions.
Exit criteria:
- Top 3-5 constraints are explicit and testable.
- Quality attributes are prioritized with rationale.
- Blocking unknowns are either resolved or converted to explicit assumptions.

### 2) Baseline Discovery
Inspect relevant code and conventions:
- Existing modules, interfaces, and lifecycle patterns that must be reused.
- Existing operational model: logging, metrics, state management, cancellation semantics.
- Integration boundaries and ownership.

Record what is reused vs what is new.
Evidence requirement:
- Cite at least 2-3 concrete reuse anchors (modules, interfaces, or patterns) from the repository.
Exit criteria:
- Reuse/new split is documented.
- Ownership and integration boundaries are clear.

### 3) Alternatives Generation
Produce 2-4 viable architecture options:
- Option name and core idea.
- Main components and interaction model.
- Data/control flow and state model.
- Migration path from current system.
- Failure modes and operational complexity.

At least one option should be conservative (low migration risk) and one should be future-leaning.
Exit criteria:
- At least 2 real options are viable under stated constraints.
- Options differ in mechanism, not just naming.

### 4) Trade-off Evaluation
Evaluate each option on explicit criteria with short rationale:
- Correctness and failure containment.
- Scalability and performance characteristics.
- Operability (debuggability, observability, incident response).
- Evolution cost (how hard future changes become).
- Delivery risk and implementation effort.

Use a normalized matrix when scoring:
- Score each criterion from 1-5 (5 = best).
- Assign criterion weights that sum to 100.
- Compute total as `sum(score * weight) / 100`.
- Explain non-obvious judgments.
Minimum criteria set:
- correctness/failure containment
- scalability/performance
- operability
- evolution cost
- delivery risk/effort
Exit criteria:
- Matrix and rationale are reproducible by another reviewer.
- Winner is justified by both score and qualitative caveats.

### 4.5) Constraints Validation
Before selecting a winner, validate each option against hard constraints:
- Security/compliance constraints.
- SLA/SLO or performance boundaries.
- Team/time capacity and delivery deadlines.
- Compatibility and migration constraints.

Mark each constraint as: pass, risk, or fail. Do not select an option with unresolved hard-fail constraints.

### 5) Future Effects Analysis
Forecast downstream effects across time horizons:
- Near term (0-3 months): delivery speed, migration friction, immediate risk.
- Mid term (3-12 months): coupling, testability, ownership load, feature velocity.
- Long term (12+ months): architectural debt, replacement cost, lock-in exposure.

Explicitly call out second-order effects and irreversible decisions.
Exit criteria:
- At least 1 second-order effect per time horizon.
- Irreversible decisions are flagged with consequences.

### 6) Decision and ADR
Produce an ADR-style decision:
- Decision: selected option.
- Why selected: key trade-offs.
- Why rejected: strongest rejected alternatives and rejection reasons.
- Guardrails/invariants that must hold during implementation.
- Triggers to revisit decision (clear conditions).
- Constraint compliance summary for the selected option.
- Decision log for assumptions/open questions: owner, validation deadline, fallback if invalid.

### 7) Implementation Handoff
Prepare a handoff for `code_planner` / `coder` / `reviewer`:
- File/module impact map.
- Incremental rollout plan (small reversible steps).
- Test and verification plan (positive, negative, regression, migration checks).
- Risks and mitigations.
- Explicit out-of-scope list.
- Rollback plan: trigger, blast radius, data compatibility constraints, go/no-go observability signals.
- NFR budgets: target bounds (e.g., latency p95/p99, error rate, cost ceiling).

## Output Template
Use this structure unless user requests another format:

```markdown
# Architecture Proposal: <Topic>

## 1. Problem Framing
- Objective:
- Constraints:
- Quality attributes (priority order):
- Non-goals:
- Assumptions:

## 2. Existing Context
- Reuse candidates:
- Current limitations:
- Integration boundaries:

## 3. Options
### Option A — <name>
- Design:
- Pros:
- Cons:
- Failure modes:
- Migration:

### Option B — <name>
- Design:
- Pros:
- Cons:
- Failure modes:
- Migration:

## 4. Trade-off Matrix
- Criteria:
- Option scores/rationale:
- Key trade-offs:

## 5. Future Effects
- 0-3 months:
- 3-12 months:
- 12+ months:

## 6. Decision (ADR)
- Chosen option:
- Decision drivers:
- Rejected alternatives:
- Guardrails:
- Revisit triggers:
- Constraint compliance summary:
- Assumptions/open questions log (owner, due date, fallback):

## 7. Delivery Handoff
- Phase plan:
- Validation plan:
- Risks and mitigations:
- Out of scope:
- Rollback plan (trigger, blast radius, data compatibility, signals):
- NFR budgets (latency/reliability/cost):
```

## Quality Gates
Before finalizing, verify:
- At least 2 real options were evaluated.
- Trade-offs include both technical and operational consequences.
- Future effects are explicit, not generic.
- Decision has revisit triggers.
- Handoff is implementation-ready and scoped.
- Assumptions and residual risks are clearly listed.
- Hard constraints are explicitly validated (pass/risk/fail).
- Rollback and reversibility are documented.
- NFR budgets are concrete, not qualitative.

## Anti-Patterns
- Jumping to a single preferred design without alternatives.
- Mixing architecture proposal with broad unrequested implementation.
- Ignoring migration/rollback and operability.
- Over-optimizing for ideal future while violating near-term constraints.
- Vague statements like "scales well" without mechanism or boundary conditions.

## Routing Boundaries
- Use this skill for architecture decision quality, not implementation delivery.
- Stop at a decision-ready architecture artifact and implementation handoff unless the user explicitly asks to proceed into coding.
- For one-page stakeholder communication, include an executive summary with chosen option, top drivers, key risks, and phased timeline.
