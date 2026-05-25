---
id: "project-overview"
name: "project-overview"
description: "Creates a deep, evidence-backed map of an unfamiliar codebase: structure, stack, entrypoints, major components, tests, tooling, operational shape, coverage, confidence, and suggested exploration path. Use when the user asks to understand a repository, onboard to a project, inspect project structure, or get a broad overview before changes. Do not use for targeted behavior tracing, implementation, or deep architecture decisions."
version: "0.4.0"
---

# Project Overview

## Purpose
Use this skill to build an evidence-backed map of an unfamiliar repository.
The goal is to help a reader understand what the project is, how it is organized, how it likely runs, how quality is maintained, and where to read next.

Default outcome: a useful project map that is deep enough to support onboarding or future work. Keep the final answer concise, but do not keep the exploration shallow.

## Use When
- The user asks to understand a repository or codebase.
- The user asks for onboarding context before making changes.
- The user asks for project structure, stack, entrypoints, tests, tooling, or CI overview.
- The user asks for a deeper project map, but not a targeted trace of one behavior.

Do not use this skill for targeted behavior tracing, implementation, architecture option design, code review, or external topic research.

## Input Contract
Before exploring, establish:
- Project root: use the current working directory unless the user names another path.
- Goal: onboarding, broad map, pre-change orientation, handoff summary, or deep project overview.
- Scope boundaries: whole repository, one package, one service, or representative sampling for large repos.
- Constraints: read-only exploration; do not modify files or run project code.

## Exploration Rules
- Prefer read-only inspection: file listing, targeted file reads, search, dependency/build metadata, documentation, tests, and git metadata.
- Do not run project code, migrations, tests, build scripts, custom helper scripts, or commands with side effects.
- Safe read-only commands or tools may be used when available, but do not depend on specific tool names.
- Avoid full tree dumps. Summarize structure by purpose, not by exhaustive listing.
- For large repos, sample representative areas and clearly state coverage limits.
- Tie important conclusions to concrete evidence: files, directories, config files, entrypoints, tests, or docs.
- Distinguish confirmed facts from likely inferences based on naming or layout.

## Overview Depth
Always inspect deeply enough to produce a reliable project map, not just a directory summary.

Cover:
- top-level structure and main source directories;
- dependency/build/config files;
- entrypoints and run commands;
- major components/modules;
- tests, linting, formatting, typing, CI/CD;
- docs that explain architecture or operations;
- package/service boundaries and ownership clues;
- runtime shape: CLI, web server, workers, jobs, background tasks, libraries, generated assets;
- configuration flow: env files, settings modules, feature flags, deployment configs;
- integration boundaries: databases, APIs, queues, subprocesses, external services;
- quality model: unit/integration/e2e split, fixtures, test utilities, CI stages;
- operational model: logging, metrics, error handling, deployment/runtime files when visible;
- representative modules in each major subsystem.

Depth does not mean reading every file. For large repositories, use representative sampling, state what was sampled, and report coverage and confidence.

## Workflow

### 1) Repository Shape
- Identify top-level directories and classify them by role: source, tests, docs, scripts, infra, generated/build artifacts, examples, assets.
- Detect monorepo, multi-service, library, CLI, web app, backend/frontend split, plugin system, or package collection.
- Note generated/vendor/build directories and avoid treating them as primary source.

Exit criteria:
- The repository's broad shape is described without dumping the full tree.

### 2) Stack and Dependencies
- Identify languages from extensions and source roots.
- Use dependency/build files to confirm stack and tooling.
- Look for framework markers, package scripts, build config, runtime config, lockfiles, and formatter/linter/type-checker config.

Exit criteria:
- Main languages, frameworks, dependency managers, and build/test tooling are identified or explicitly unknown.

### 3) Entrypoints and Runtime Shape
- Find how the project starts: CLI commands, application bootstrap, web server, workers, jobs, library exports, notebooks, or package entrypoints.
- Use manifests, scripts, main modules, route/app initialization, and docs as evidence.
- Explain what each confirmed or likely entrypoint is for.

Exit criteria:
- Entrypoints are listed as confirmed, likely, or not found.

### 4) Components and Boundaries
- Map major source directories or packages to their responsibilities.
- Identify cross-cutting concerns: configuration, logging, persistence, API clients, state management, auth, background work, serialization, validation.
- For deeper overviews, inspect representative files from each major component to avoid relying only on directory names.

Exit criteria:
- Key components and boundaries are explained with concrete evidence.

### 5) Tests, Quality, and CI
- Identify test directories, test frameworks, fixtures, and test layering when visible.
- Identify linting, formatting, static typing, code generation, pre-commit hooks, and CI/CD.
- Note quality gaps if tests or automation are missing or unclear.

Exit criteria:
- The project quality model is summarized, including known gaps.

### 6) Coverage and Confidence
- Record what was inspected and what was not.
- Mark uncertain claims as assumptions.
- If the repo is large, explain which parts were sampled and why.
- Recommend the next files or subsystems to inspect for deeper understanding.

Exit criteria:
- The reader can judge how complete the overview is.

## Output Template
Use this structure unless the user requests another format:

```markdown
# Project Overview: <Repository or Package>

## High-Level Summary
<1-3 paragraphs: what the project is, primary stack, broad runtime shape, and repository type.>

## Repository Shape
- Structure:
- Main source areas:
- Docs/assets/infra:
- Monorepo or package boundaries:

## Stack and Tooling
- Languages/frameworks:
- Dependency/build files:
- Run/build/test commands:
- Formatting/linting/typing:

## Entrypoints and Runtime
- Confirmed entrypoints:
- Likely entrypoints:
- Runtime components:

## Major Components
- `<path>`: responsibility and evidence
- `<path>`: responsibility and evidence

## Tests, Quality, and CI
- Test structure:
- Test frameworks/fixtures:
- CI/CD or automation:
- Quality gaps:

## Coverage and Confidence
- Inspected:
- Not inspected:
- Confidence: high | medium | low
- Assumptions:

## Suggested Exploration Path
1. `<path>` - why read next
2. `<path>` - why read next
3. `<path>` - why read next
```

## Quality Gates
Before finalizing, verify:
- The project root and scope are clear.
- Main stack and dependency/build files are identified.
- Entrypoints are confirmed, likely, or explicitly not found.
- Major components are described by responsibility, not just listed.
- Tests, tooling, and CI/CD are covered or marked as missing/unknown.
- Important claims are tied to concrete repository evidence.
- Coverage, assumptions, and confidence are stated.
- The answer does not drift into targeted behavior tracing, implementation, or architecture redesign.

## Anti-Patterns
- Dumping a file tree instead of explaining the system.
- Reading many files without a hypothesis about project structure.
- Treating names, comments, or docs as proof when code/config evidence is available.
- Ignoring tests, tooling, CI, or operational files.
- Overstating confidence for a large repo after shallow sampling.
- Turning a project overview into a feature investigation or implementation plan.
