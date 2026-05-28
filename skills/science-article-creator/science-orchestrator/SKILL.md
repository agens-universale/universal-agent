---
id: science-orchestrator
name: Science Orchestrator
description: "Two-loop orchestration of a full SCIENTIFIC research lifecycle. Inner loop: hypothesis → choose sources → run / synthesize a step → record → learn (minutes to days). Outer loop: periodically step back, ask 'what do these results mean?', synthesize patterns, decide deepen / broaden / pivot / conclude. Routes to inner skills as needed: scientific-research (find papers), research-artifact-compiler (structure results), scientific-article-creator (final write-up), scientific-article-checker (self-review). Use when the user requests a multi-step scientific investigation from a research question to a final report. Different from `complex-task-solver` (generic orchestration with side effects) — this is research-specific with evidence-grade gates and a synthesis loop."
version: 0.1.0
---

# Skill: Science Orchestrator

## Language Strategy

- This SKILL.md is in English (project convention).
- Working files (`master_plan.md`, `iterations/*/plan.md`, `iterations/*/observations.md`) are written in **Russian** so the user can review them in their preferred language. Tool names and identifiers stay in English.
- The final deliverable (`final_report.md` produced via `scientific-article-creator`) is in **Russian Markdown** unless the user asks otherwise.

## When to use

- Triggers: "run a full mini-investigation", "from a hypothesis to a final report", "figure out topic X and produce a conclusion", "investigate and write a report", "plan and run a scientific inquiry".
- Do NOT use for one-off paper search → `scientific-research`. Do NOT use for an upfront article request → `scientific-article-creator`. Do NOT use for a generic multi-step task without scientific context → `complex-task-solver`.

## What it does

Manages a long-running research session with two control loops: a fast inner loop for iteration and a periodic outer loop for synthesis. The output is an artifact plus a final report with verified references.

## Role boundary (MANDATORY)

The orchestrator **coordinates**, it does not execute. It is forbidden from doing by hand what inner skills cover:

- Literature search → **only** via `skills_load("science-article-creator:scientific-research")`, not directly through `science-literature_search` / `science-preprint_search`.
- Structuring results → **only** via `skills_load("science-article-creator:research-artifact-compiler")`.
- Writing the report → **only** via `skills_load("science-article-creator:scientific-article-creator")`.
- Self-review of the final document → **only** via `skills_load("science-article-creator:scientific-article-checker")`.

The orchestrator may call `science-*` MCP tools directly **only** in two cases:
1. Pre-flight `science-explain_sources` once at the start of the session.
2. A single `science-work_lookup` to resolve one identifier the user named in the prompt (not an identifier from search results — that belongs to inner skills).

If the log shows the orchestrator running many direct searches or lookups, it is not delegating. That is an execution failure, not "efficiency". Restart with an explicit reminder about delegation.

## Inputs

- Topic / research question.
- Optional: target format of the report (review / short conclusion / decision), time horizon (1–3 iterations / more).

## Algorithm

### Bootstrap

1. State the central research question (if vague — delegate to `scientific-research` to apply the lenses).
2. Formulate 2–4 falsifiable hypotheses.
3. Create the working directory `tmp/science-smoke-en/<session-id>/` with an `iterations/` subdirectory.

### Inner loop (repeat)

One pass = one iteration in `iterations/<NN>/`:

1. **Pick** — pick the hypothesis or sub-question for this iteration.
2. **Lock protocol** — write `iterations/<NN>/plan.md` describing exactly what we are doing and what we expect BEFORE acting. This is the git-style proof against post-hoc rationalization.
3. **Execute** — only via delegation (see "Role boundary"):
   - Search → `skills_load("science-article-creator:scientific-research")` with the iteration's sub-task.
   - Full-text reading of found works → inside `scientific-research` or `scientific-article-checker`, not the orchestrator.
   - Identifier resolution from search results — handled by `scientific-research` (it already calls `work_lookup` for `WorkRecord` normalization).
4. **Measure** — write `iterations/<NN>/observations.md`: what was found, what confirms / refutes hypotheses, what gaps remain.
5. **Record** — add the relevant claims / evidence to the rolling artifact via `research-artifact-compiler` in incremental mode.

After every 2–3 iterations, switch to the outer loop.

### Outer loop (synthesis)

1. **Step back** — read all `iterations/*/observations.md`.
2. **Pattern recognition** — what patterns are visible, which hypotheses got stronger, which were rejected, what was surprising.
3. **Decision** — pick one:
   - **Deepen** — dive into one sub-area, schedule a new iteration on it.
   - **Broaden** — extend the inquiry into adjacent fields.
   - **Pivot** — the current branch yields no answer; reformulate the hypotheses.
   - **Conclude** — enough data; move to the final report.
4. **Update plan** — write the decision into `master_plan.md` with a rationale.

### Final stage

When the outer loop returns `Conclude`:

1. Lock the artifact via `research-artifact-compiler` (final assembly across all iterations).
2. Delegate to `scientific-article-creator` for the final text built on top of the artifact.
3. Optional — `scientific-article-checker` for a self-review of the generated report.
4. Place the finished file at `<session-dir>/final_report.md`.

## MCP tools

The orchestrator itself calls only:
- `science-explain_sources` — pre-flight, once per session.
- `science-work_lookup` — point-resolve when the user named an identifier in the prompt (not from search results).

Everything else (`literature_search`, `preprint_search`, `open_access_search`, `citations_by_doi`, `ncbi_fetch`) is **delegated** to inner skills that already know how and when to invoke them.

## Output format

Files inside `<session-dir>/`:

```
master_plan.md          # план + outer-loop decisions with rationale
iterations/
  01/plan.md
  01/observations.md
  02/plan.md
  ...
artifact/               # final research artifact
final_report.md         # final article / report (Russian)
review.md               # optional, from scientific-article-checker
```

## What NOT to do

- Do not collapse inner and outer loop into one phase — the outer loop runs only after ≥2 inner iterations.
- Do not rewrite prior `observations.md` retroactively. If a mistake was made, add a new entry marking the correction.
- Do not invent gates ("data is sufficient") without an explicit criterion in `master_plan.md`.
- Do not duplicate inner-skill function — delegate via `skills_load`. A direct call to `science-literature_search`, `science-preprint_search`, or `science-citations_by_doi` from the orchestrator is a bug, not a feature.
- Do not write the final report yourself. The final text is produced by `scientific-article-creator` from the assembled artifact.

## Examples

**Request:** "Run a mini-investigation: effect of tirzepatide on cardiovascular risk. 2 iterations, final report."
→ Bootstrap: research_question + 3 hypotheses (MACE reduction / HF hospitalization reduction / mortality reduction).
→ Inner #1: HF hypothesis — delegate to `scientific-research` for `literature_search "tirzepatide heart failure"`; the inner skill handles `work_lookup` of the key RCTs.
→ Inner #2: MACE hypothesis — delegate again for `literature_search "tirzepatide cardiovascular outcomes"` plus `citations_by_doi` on the main RCT.
→ Outer: MACE evidence is thin (1 ongoing trial), HF evidence is positive. Decision: `Conclude`, note insufficient MACE data.
→ Final stage: artifact + `final_report.md` via `scientific-article-creator`.

**Request:** "Sort out microbiome and metabolic syndrome; iterate until you have a clear answer."
→ The inner loop cycles until the outer loop returns `Conclude`, typically 3–5 iterations.

## Limits

- arXiv rate limit (1 req / 3 s) plus a missing Semantic Scholar API key can stretch iterations.
- For long sessions, run science-mcp as an HTTP daemon (`127.0.0.1:8766/mcp` in this project) — stdio loses throttler state between calls.
- If the outer loop cannot reach `Conclude` in more than 5 iterations, stop, surface this to the user explicitly, and ask them to refine the question.
