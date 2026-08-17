---
id: "project-investigation"
name: "project-investigation"
description: "Investigates a specific behavior, subsystem, flow, symbol, integration, or change surface in an existing codebase without modifying files. Use when the user asks to understand how something works, trace where behavior is implemented, locate relevant files, explain control/data flow, or identify likely change points. Do not use for broad project overview, software architecture design, or implementing code changes."
version: "0.1.0"
---

# Project Investigation

## Purpose
Use this skill for targeted, read-only investigation of an existing codebase.
The goal is to answer a specific code question with evidence from files, symbols, configuration, tests, and runtime entrypoints.

Default outcome: a concise investigation report explaining how the relevant part works, where it lives, what evidence supports the conclusion, and what remains uncertain.

## Use When
- The user asks how a specific feature, command, API, integration, state flow, or subsystem works.
- The user asks where behavior is implemented or which files are relevant.
- The user asks to trace a code path, data flow, lifecycle, dependency, or configuration path.
- The user asks what would likely need to change, but has not asked to implement the change.

Do not use this skill for broad repository onboarding, direct implementation, architecture option design, article writing, or external topic research.

## Input Contract
Before investigating, establish:
- Investigation question: the behavior, subsystem, symbol, route, command, or user flow to understand.
- Scope boundary: in-scope modules, platforms, languages, or packages when known.
- Desired output: explanation, file map, flow trace, change-surface estimate, or risk/gap assessment.
- Constraints: read-only mode, no code edits, no broad refactors, no speculative redesign.

If the question is broad, narrow it to 1 primary investigation question and up to 3 sub-questions. If narrowing would change the user's intent, ask a focused clarification.

## Investigation Strategy
- Start from the most reliable anchors available: user-provided files, exact symbols, routes, commands, tests, configs, logs, or error text.
- If no anchor is provided, discover likely anchors through file names, dependency/build files, entrypoints, routing/config files, tests, and semantic search.
- Prefer targeted search and selective reading over broad file dumps.
- Build and revise hypotheses as evidence appears. Do not keep reading randomly after the likely path is found.
- Treat tests, configuration, and call sites as first-class evidence, not secondary details.

## Workflow

### 1) Frame the Question
- Restate the investigation target in one sentence.
- Identify expected evidence types: entrypoints, definitions, call sites, config, tests, docs, or runtime output.
- Define what would count as a sufficient answer.

Exit criteria:
- The target is specific enough to search for.
- The expected output is clear.

### 2) Find Anchors
Search for:
- exact names from the request: symbols, routes, commands, env vars, config keys, filenames, UI labels, error messages;
- likely entrypoints: CLI commands, API routes, app initialization, job handlers, event consumers;
- tests that describe the behavior;
- docs or comments only as supporting evidence.

Exit criteria:
- At least one concrete anchor is found, or the report clearly explains that no anchor was found and what was searched.

### 3) Trace the Flow
Follow the code path from entrypoint to outcome:
- caller -> callee chain;
- data transformations and state changes;
- configuration or dependency injection;
- external integrations, persistence, network calls, queues, subprocesses, or side effects;
- error handling and edge cases relevant to the question.

Keep the trace bounded. Stop following branches that do not affect the investigation question.

### 4) Cross-Check
Validate the trace against independent evidence when available:
- tests or fixtures;
- configuration defaults;
- call sites from another layer;
- runtime logs or command output if safe and read-only;
- docs only if they match the code.

Mark confidence separately:
- high: code path is directly traced and corroborated by tests/config/call sites.
- medium: main path is traced, but tests or some branches are missing.
- low: evidence is indirect, generated, stale, or incomplete.

### 5) Produce the Report
Explain the answer using file and symbol references close to the claims they support.
Separate confirmed facts from assumptions and open questions.

If the user asked what would need to change, identify likely change points and risks, but do not produce an implementation plan unless requested.

## Output Template
Use this structure unless the user asks for another format:

```markdown
# Project Investigation: <Target>

## Question
- Primary question:
- Scope:

## Short Answer
<Concise answer in 2-5 sentences.>

## Evidence Map
- `<file/path>` / `<symbol>`: why it matters
- `<file/path>` / `<symbol>`: why it matters

## Flow Trace
1. `<entrypoint>` -> what happens
2. `<next symbol>` -> what happens
3. `<outcome>` -> what happens

## Behavior Details
- Confirmed facts:
- Edge cases / error paths:
- Config, tests, or integrations:

## Change Surface (optional)
- Likely files/symbols to change:
- Risks:
- Tests to inspect or add:

## Confidence and Gaps
- Confidence: high | medium | low
- Assumptions:
- Unresolved questions:
- Suggested next reads:
```

## Quality Gates
Before finalizing, verify:
- The investigation question is answered or explicitly blocked.
- Claims about behavior are tied to concrete files, symbols, tests, configs, or command evidence.
- The flow trace starts from a real anchor and ends at the relevant outcome.
- Assumptions and gaps are clearly labeled.
- The report does not drift into implementation, architecture redesign, or broad project overview.
- Suggested change points are marked as optional and evidence-based.

## Anti-Patterns
- Listing files without explaining behavior.
- Reading many files without a search hypothesis.
- Treating directory names or comments as proof when code evidence is available.
- Turning a read-only investigation into code changes.
- Producing an implementation plan when the user only asked how something works.
- Ignoring tests, fixtures, or configuration that could confirm or contradict the trace.
