---
id: using-default-coworker
name: Using Default Coworker
description: Entry point for broad non-trivial “digital coworker” tasks. Use when the user delegates an outcome, asks the agent to take work on, coordinate research/execution, assist personally, or run a background business task. Classifies the request, chooses autonomy/evidence/deliverable level, and routes to narrower coworker or specialized skills. Do not use for simple direct answers.
version: 0.1.0
---

# Using Default Coworker

Default router for broad non-trivial work — not an always-on context blob.

**DO NOT load for simple direct answers, one-line explanations, or tiny edits where the core prompt is enough.**

## Epistemic stance

Be a truth-seeking colleague, not a flattering assistant: critique weak premises,
separate facts from assumptions, prefer verifiable evidence over vibes or authority.

## Process

1. Classify the task (answer / research / execution / assistant / background ops /
   software / GUI / high-impact orchestration).
2. Choose autonomy level (L0–L5; L5 = external/irreversible → explicit approval).
3. Choose deliverable/evidence standard (short answer / artifact / cited research /
   implementation+verification / handoff report).
4. Load **one** primary skill — inner coworker mode or external specialized pack.
   Your very next `skills_load` after classification is this primary skill only.
5. Cross-cutting method skills (`deliverables-evidence`, `handoff-autonomy`) — **never
   preload at routing time**. Load them later, in a separate `skills_load`, only when
   the **active** task hits a trigger in the table below (not “just in case”).
6. If nothing fits, proceed with the core prompt only.

## Routing — inner coworker modes

| User intent (signals) | Primary `skills_load` |
|---|---|
| Ambiguous analysis, comparison, recommendation (coworker triage — not deep specialist research) | `default-coworker:research` |
| Personal planning, drafts, prioritization, triage | `default-coworker:personal-assistant` |
| Ongoing monitoring, follow-up, business process support | `default-coworker:background-business` |

## Routing — cross-cutting methods (lazy — mid-task, not at routing)

Load on a **later** turn when the trigger is real. Do not bundle with the primary
`skills_load` at the start.

| Situation | `skills_load` |
|---|---|
| Substantive output but unclear format or verification depth | `default-coworker:deliverables-evidence` |
| External/irreversible/high-impact actions, delegation, long continuity | `default-coworker:handoff-autonomy` |

## Routing — external specialized packs

| User intent (signals) | Consider `skills_load` |
|---|---|
| Scientific literature, DOI, PMID, arXiv, peer-reviewed papers | `science-article-creator:using-science-article-creator` |
| General domain, market, vendor, regulatory, web research | `topic-research` |
| Coding feature, bugfix, refactor, design, implementation plan | `supercoder:using-supercoder` |
| Read-only codebase explanation, map, or flow trace | `codebase:using-codebase` |
| Russian article or long-form writing | `articles:using-articles` |
| GUI/desktop/browser/office app control | `computer-use` (before any GUI tools) |
| High-impact multi-step workflow with recovery, planning gates, or critic loops | `complex-task-solver` |

## Routing notes

- **Research — specialist-first:** clear scientific literature → `science-article-creator`;
  deep domain/web brief → `topic-research`. `default-coworker:research` only for
  ambiguous triage, lightweight analysis, or workspace-local framing.
- **Orchestration:** high-impact multi-step with recovery/planning/critic → primary
  `complex-task-solver`. `handoff-autonomy` lazy for L5/continuity only — not a substitute.

## Pack rules

- **One process skill at a time.** Finish the current phase before loading another.
  Primary first; cross-cutting only when the live task hits its trigger.
- **No cross-cutting preload.** Two `skills_load` calls at routing (primary +
  handoff/deliverables) is an anti-pattern — the second waits until needed.
- **Bootstrap is the only router.** Inner skills do not chain to each other; if the
  mode was wrong, stop and re-classify here.
- **External packs win when they clearly match.** Do not wrap specialized work in
  coworker process for its own sake.
- **Hard-gates are not optional.** When a loaded skill says “do NOT until Y”, obey it.

## When NOT to use this pack

- Pure read-only explanations with no delegated outcome.
- One-step trivial commands.
- Questions where the core prompt already suffices.

## Next step

Your very next action after classification: `skills_load` the selected **primary**
skill only. Cross-cutting methods come later if triggers fire. If L5 or an approval
boundary applies, stop and ask before side effects (you may then `skills_load`
`handoff-autonomy` for the full protocol).
