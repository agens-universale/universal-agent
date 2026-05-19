---
id: using-supercoder
name: Using Supercoder
description: Entry point of the Supercoder methodology pack. Load FIRST when starting non-trivial coding work (feature, bugfix, refactor, design, planning) or hitting a tricky bug. Maps user intent to the right inner skill from this pack — see the body for the trigger table.
version: 1.0.0
---

# Using Supercoder

You are operating inside the **supercoder methodology pack**. This pack
contains process skills that guide non-trivial coding work: brainstorming,
planning, TDD, debugging, code review.

## CRITICAL — these skills are mandatory, not suggestions

If a skill in this pack applies to the user's task, you MUST load and follow
it. Skipping the methodology because the task "looks simple" is a defaulting
antipattern — it leads to unexamined assumptions, undefined success criteria,
and rework. Trust the process.

## When to use which skill

| User intent (signals) | Skill to load via skills_load |
|---|---|
| New feature, refactor, change of behavior, "let's build X" | `supercoder:brainstorming` |
| Design approved, need a concrete TDD-style plan | `supercoder:writing-plans` |
| Plan exists, ready to execute task by task | `supercoder:executing-plans` |
| Writing tests, implementing a feature | `supercoder:test-driven-development` |
| Bug, test failure, "X doesn't work", flaky output | `supercoder:systematic-debugging` |
| About to claim "done" / commit / open PR | `supercoder:verification-before-completion` |
| About to ask for code review | `supercoder:requesting-code-review` |
| Just received review feedback, need to respond | `supercoder:receiving-code-review` |
| Creating a new project skill | `supercoder:writing-skills` |

## How to proceed

1. **Identify** the matching skill by intent (table above).
2. **Load it immediately — hard-gate.** Your very next tool call MUST be
   `skills_load("supercoder:<skill-id>")`. Do NOT ask the user questions,
   do NOT explore files, do NOT call any other tool before this load. The
   inner skill defines when and how to do those things; without it loaded,
   you are improvising outside the methodology.
3. **Follow the loaded skill exactly.** Do not improvise around its hard-gates.

If no row in the intent table matches the user's request, jump to "When NOT
to use this pack" below — and answer/act directly without methodology.

## Hard rules for this pack

- **One process skill at a time.** Don't load two methodology skills in parallel
  — finish the current phase before moving on.
- **Hard-gates are not optional.** When a skill says "do NOT do X until Y",
  obey it. Hard-gates exist because earlier sessions failed without them.
- **Cross-skill chaining is text-based.** Each skill ends with an explicit
  "next step" — load that skill when the current phase is done. There is no
  runtime engine; the agent's discipline is the engine.
- **Domain-specific skills (skills_list) come AFTER methodology.** If the user
  needs an article checker, a SBerTrack analyst, or another domain skill, load
  it in addition to (not instead of) the relevant process skill from this pack.

## When NOT to use this pack

- Pure read-only requests ("explain this function", "what does X do") — answer
  directly, no methodology needed.
- One-step trivial commands ("rename foo to bar in file X") — just do it.
- Questions about the agent itself, the config, or non-code tasks — out of scope
  for this pack.
