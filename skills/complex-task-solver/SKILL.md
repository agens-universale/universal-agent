---
id: "complex-task-solver"
name: "Complex Task Solver"
description: "Instructions for agents on solving complex multi-step tasks with planning, iterative actor-critic loops, fact-checking, and intent alignment"
version: "0.1.0"
---

# Skill: Complex Task Solving — Planning, Iterative Refinement & Intent-Aligned Delivery

## Overview
A general-purpose multi-agent workflow for solving complex, open-ended tasks. The skill orchestrates `Planner`, `Actor`, `Critic`, and `SpecialAgent` sub-agents through: (1) structured planning, (2) per-item actor-critic refinement loops with mandatory fact-checking, (3) final artifact assembly, and (4) intent-alignment verification. It guarantees factual correctness, traceable reasoning, and output that matches user intent.

## Language Strategy (Critical)
- **Target Language**: All final user-facing content MUST be in **Russian**.
- **Intermediate Artifacts**: Plans, drafts, critic reports, and reasoning traces MAY be multilingual for research efficiency, but must be synthesized into Russian before user delivery.
- **Research Phase**: Agents are encouraged to search sources in any language (for coding and science search in English). All synthesized conclusions must be written directly in Russian — no "write in English then translate" workflow.
- **Terminology**: Use established Russian technical terms. For neologisms or widely used English terms, keep the English term with a brief Russian explanation on first use.

## Agent Roles & Responsibilities

| Role | Responsibility | Language Constraint |
|------|---------------|---------------------|
| `Planner` | Decomposes the task into atomic, verifiable steps; flags items requiring `SpecialAgent` | Plan and step descriptions in **Russian** |
| `Actor` | Executes the assigned step: research, coding, analysis, drafting | Output artifacts in **Russian** |
| `Critic` | Fact-checks every claim using live search; validates logic, completeness, and intent alignment | Reports and feedback in **Russian** |
| `SpecialAgent` | Instantiated on demand for niche/complex subtasks (e.g., `CodeReviewer`, `DataAnalyst`); receives an auto-generated role prompt | Role-specific output in **Russian** |
| `Orchestrator` *(implicit)* | Manages workflow gates, artifact versioning, role routing, and loop control | Internal coordination (language-agnostic) |

## Workflow Pipeline

### Phase 1: Planning & Task Decomposition
1. `Planner` analyzes user intent, constraints, and success criteria. Use web search and artifact inspection if needed.
2. `Planner` outputs a structured plan:
   - Ordered list of atomic, testable steps
   - Expected artifact type per step (code, text, data, decision)
   - Flags for steps requiring `SpecialAgent`
   - Explicit success criteria for final artifact
3. `Orchestrator` validates plan completeness before proceeding.

### Phase 2: Per-Step Execution Loop (Actor-Critic)
*Executed strictly sequentially for each plan item:*

**Hard gating rules for `Orchestrator`**:
- Process steps one by one: `step_n` must reach `Critic: Pass` before `step_n+1` starts.
- No batch execution of Actors without interleaved Critic validation.
- `Critic` MUST use live internet search to fact-check every non-trivial claim.

1. **Assignment**: `Orchestrator` routes step to:
   - `Actor` (standard complexity), or
   - `SpecialAgent` (complex/niche; auto-generates a system prompt from the role description)
2. **Execution**: The assigned agent produces a versioned artifact (`step_01_v1.md`, `script_v1.py`, etc.) with an explicit reasoning trace.
3. **Audit**: `Critic` reviews and outputs a structured report:
    [Claim/Artifact Element] → [Issue Type: factual/logical/intent] → [Evidence/Source] → [Action Required] → [Status: Pass/Minor/Critical]
4. **Revision**: `Actor` updates artifact, explicitly addressing every Minor/Critical item with change log.
5. **Verification**: `Critic` re-checks revised artifact. Loop repeats until all items are `Pass`.
6. **Gate**: `Orchestrator` enqueues next step only after current step achieves `Pass`.

### Phase 3: Final Artifact Assembly
1. A `SpecialAgent` instantiated as `Integrator` ingests all approved step artifacts.
2. `Integrator` unifies terminology, resolves cross-references, removes redundancy, and outputs a cohesive final artifact in Markdown (or other required format).
3. The artifact includes an execution summary, a decision log, and a traceability map (step → artifact section).

### Phase 4: Intent-Aligned Final Verification
1. `Critic` performs final review against **original user intent**:
- Does the artifact solve the stated problem?
- Are all success criteria from Phase 1 met?
- Are there unsupported claims, logical gaps, or format violations?
2. If `Critic` returns any `Critical` or unresolved `Minor` items:
- Route back to `Actor`/`Integrator` for targeted revision
- Re-enter Phase 4 after fixes
3. If all items `Pass`: `Critic` signs off with `Final Approval` stamp.

## Output Specifications
- **Format**: As required by task (Markdown, JSON, code, etc.); if unspecified, default to Markdown.
- **Citation Style**: Inline citations for every factual claim: `[Source Title](URL)`
- **Reference List**: `## References` section with title, author/org, date, URL.
- **Traceability**: All artifacts are versioned (`_v1`, `_v2`); critic reports are linked to revisions.
- **Quality Gates**:
  - Zero unsourced factual statements
  - All critic feedback explicitly resolved and traceable
  - Final artifact passes intent-alignment check
  - Ready for delivery without further intervention

## Execution Constraints
1. **Strict Role Separation**: No role bypassing; `Critic` never edits artifacts directly.
2. **Mandatory Fact-Checking**: `Critic` MUST use live search/knowledge tools for verification — no reliance on internal knowledge alone.
3. **Intent Anchoring**: User's original request and success criteria are immutable reference points; any scope change requires explicit user confirmation.
4. **Artifact Versioning**: All outputs are traceable via semantic versioning (`step_01_v1.md` → `step_01_v2_revised.md`).
5. **Critic Report Format**: Structured as `[Element] → [Issue] → [Evidence] → [Action] → [Status]`.
6. **No Deferred QA**: The per-step critic loop is mandatory; the final critic pass does not replace iterative validation.
7. **Final Output Language**: Always Russian for user-facing content; no omission or simplification during synthesis.

## Expected Final Artifact Format (Example: Markdown Report)
```markdown
# [Task Title / Solution Summary]

## Executive Summary
[Brief Russian-language overview of solution and outcome]

## Execution Plan
1. [Step 1 description] — Status: Pass
2. [Step 2 description] — Status: Pass
...

## Solution Details
### [Section corresponding to Step N]
Content with inline citations: ...[Source Title](https://example.com/source1)

## Decision Log & Reasoning Trace
- [Key decision point] → [Rationale] → [Supporting evidence]

## References
- [Source Title](https://example.com/source1) — Author, Year
- [Source Title](https://example.com/source2) — Organization, Date

## Final Approval
✅ Critic Sign-off: Intent-aligned, fact-checked, ready for delivery.

## Failure & Recovery Protocol
- If Critic detects irreconcilable factual conflict: pause workflow, flag for user clarification.
- If Actor cannot complete step after 2 revision cycles: escalate to SpecialAgent.
- If the final artifact fails the intent check twice: trigger Planner re-evaluation of task decomposition.
- Orchestrator can ask Planner to change the plan during task solving if necessary.
