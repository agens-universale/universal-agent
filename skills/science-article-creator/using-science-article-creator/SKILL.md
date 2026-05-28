---
id: using-science-article-creator
name: Using Science Article Creator
description: "Entry point for SCIENTIFIC literature work through science-mcp — search papers/preprints in arXiv/bioRxiv/medRxiv/OpenAlex/PubMed/Semantic Scholar, build topical reviews with VERIFIED citations (DOI/PMID/arXiv resolved through MCP, never from model memory), fact-check existing scientific articles along epistemic dimensions, structure raw research material into a research artifact, orchestrate multi-step investigations. Use when the user asks for scientific research, an academic article, a literature review, a paper survey, fact-checking with citation verification, or mentions DOI/PMID/arXiv IDs / journal papers / peer-reviewed sources. Do NOT use for everyday article writing without citation verification — use the `articles` pack. Do NOT use for non-academic web research — use `topic-research`."
version: 0.1.0
---

# Using Science Article Creator

This pack handles scientific literature work on top of the `science-mcp` server (tools: `literature_search`, `work_lookup`, `preprint_search`, `ncbi_fetch`, `citations_by_doi`, `open_access_search`, `explain_sources`).

## Language Strategy

- This SKILL.md and the bodies of all inner skills are in English (project convention).
- User-facing deliverables this pack produces — articles, reviews, fact-check reports, research artifacts, final orchestrator reports — are in **Russian Markdown** unless the user explicitly asks for another language.
- Source papers can be in any language; quotes and titles remain verbatim.

## Pre-flight check

Before the first MCP call in a session, invoke `explain_sources` once. Surface to the user any missing API keys (Semantic Scholar, CORE) and the resulting degradation — those sources will be skipped or rate-limited.

## Pick the inner skill by what you must deliver

| Deliverable | skills_load |
|---|---|
| Formulate a hypothesis or research question from a vague interest | `science-article-creator:scientific-research` |
| Find papers / preprints on a scientific topic | `science-article-creator:scientific-research` |
| Write a Russian Markdown scientific article / review with verified citations | `science-article-creator:scientific-article-creator` |
| Verify an existing scientific article along epistemic dimensions and resolve every cited DOI/PMID/arXiv | `science-article-creator:scientific-article-checker` |
| Compile raw research input (notes, logs, PDF excerpts) into a structured research artifact | `science-article-creator:research-artifact-compiler` |
| Run a full multi-step investigation from hypothesis to final report | `science-article-creator:science-orchestrator` |

## When NOT to use this pack

- Generic article writing with no requirement to cite peer-reviewed sources → use `articles` pack.
- Open-web research, market analysis, vendor comparison, regulatory review → use `topic-research`.
- One-off questions answered from model knowledge → answer directly, no skill needed.

## Common patterns

- **Review on a topic**: `scientific-research` → optional `research-artifact-compiler` → `scientific-article-creator`.
- **Verify someone's article**: `scientific-article-checker` directly.
- **Long investigation**: `science-orchestrator` (it routes to the other inner skills as needed).
