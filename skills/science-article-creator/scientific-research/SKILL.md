---
id: scientific-research
name: Scientific Research
description: "Multi-source ACADEMIC research through science-mcp: optionally sharpens vague intent using 10 cognitive lenses (problem-first, abstraction ladder, tension hunting, cross-pollination, what-changed, failure analysis, simplicity, stakeholder rotation, composition/decomposition, explain-to-someone), then queries literature_search + preprint_search + open_access_search across OpenAlex/PubMed/arXiv/bioRxiv/medRxiv/Semantic Scholar/CORE, dedups, normalizes every result via work_lookup, returns a confidence-rated list of papers with DOI/PMID/arXiv IDs. Use when the user asks to investigate a scientific topic, find peer-reviewed papers, build a literature overview, locate preprints. Do NOT use for non-academic web research — use `topic-research` instead."
version: 0.1.0
---

# Skill: Scientific Research

## Language Strategy (MANDATORY)

- This SKILL.md is in English (project convention).
- **The deliverable MUST be written in Russian.** Headings, section names, narrative — all Russian. Even when the user's request includes English technical terms (CRISPR, mRNA, GLP-1), the output is Russian Markdown.
- Source titles, journal names, author lists, DOI / PMID / arXiv identifiers stay verbatim in their original language. Abstracts can be quoted in the source language.
- If the user explicitly asks for English output, switch — otherwise default to Russian regardless of the input language.

## When to use

- Triggers: "find papers / preprints on topic X", "investigate scientific topic Y", "literature review", "what's the current state of literature on…", mentions of DOI / PMID / arXiv / journal / peer-reviewed sources.
- Do NOT use for non-academic web research → `topic-research`. Do NOT use to draft a full article → `scientific-article-creator`.

## What it does

Collects real, existing scientific works on a topic from six or more sources via science-mcp, normalizes their identifiers (DOI / PMID / arXiv), sorts by relevance and citation count, and tags each entry with a confidence level.

## Inputs

- Topic or research question from the user (often vague).
- Optional constraints: years, source types (preprints only, OA only), desired number of results.

## Algorithm

### Phase 1 — sharpen the query (skip if intent is already precise)

Run the request through one to three of the lenses below to convert a fuzzy ask into two to four concrete search queries:

1. **Problem-First vs Solution-First** — what hurts, or what tool are we looking for?
2. **Abstraction Ladder** — survey of a field vs a specific technique?
3. **Tension Hunting** — is there a contested point or two competing schools?
4. **Cross-Pollination** — do adjacent fields solve a similar problem?
5. **What Changed?** — what is new in the last 1–3 years?
6. **Failure Analysis** — where do known approaches break?
7. **Simplicity Test** — what is the simplest possible phrasing of the question?
8. **Stakeholder Rotation** — a clinician, an ML engineer, and a biologist would each phrase the query differently.
9. **Composition / Decomposition** — split the topic into 2–3 sub-questions.
10. **Explain-It-to-Someone** — restate as "explain it to a schoolkid".

Lenses are a tool, not a mandatory step. If the request already names key terms, years, and a source — skip Phase 1.

### Phase 2 — search

1. Call `explain_sources` once at the start of the session (if not called yet) to record which sources are available (API keys).
2. For each formulated query:
   - `literature_search` — broad pass (journals + aggregators).
   - `preprint_search` — when "fresh, pre-peer-review" matters (arXiv by default, add bioRxiv / medRxiv for biomedical topics).
   - `open_access_search` — when the user needs full text.
3. Deduplicate by DOI / arXiv ID.
4. For the top-N (10–30) call `work_lookup` to obtain a normalized `WorkRecord` (unless the entry is already normalized).

### Phase 3 — synthesis

- Sorting: by `citation_count` for established topics; by date for hot topics (last 12 months).
- Grouping: foundational / methodological / applied / contested.
- For each work — confidence rating (high / medium / low) based on: present in ≥2 databases, DOI resolves cleanly, `citation_count` > 5 (for works older than a year).

## MCP tools (science-mcp capabilities)

- `literature_search(query: str, year_from?: int, year_to?: int, limit?: int)` — main search. `year_from` / `year_to` are accepted here.
- `preprint_search(query: str, sources?: ["arxiv","biorxiv","medrxiv"], limit?: int)` — preprints. Note: this tool does **not** accept `year_from` / `year_to`; filter by year in post-processing or use `literature_search`.
- `open_access_search(query: str, limit?: int)` — OA-only.
- `work_lookup(identifier: str)` — single-record normalization.
- `explain_sources()` — pre-flight availability check.

## Output format

**Delivery: always include the annotated list in your chat reply** — even when you also save an artifact to disk. The chat message must contain the compact grouped list (papers + gaps note), not only a path or "saved to file". You may optionally save the same content as a `*_literature_review.md` file when useful, but the inline chat list is mandatory. This skill produces a short annotated source list, not a full review or article. No cover page in the chat version.

Russian Markdown with this skeleton:

```markdown
# Обзор источников: <тема>

## Сформулированные запросы
1. ...
2. ...

## Найденные работы (N)

### Фундаментальные
1. **<Title>** — Authors, Year. [https://doi.org/<doi>](https://doi.org/<doi>) · [arXiv:<id>](https://arxiv.org/abs/<id>). Cited: <N>. Confidence: high.
   <краткая аннотация 1–2 строки>

Concrete worked example of ONE entry (copy this exact shape, substituting real values from MCP):

> 1. **Telomere Biology and Human Phenotype** — Turner et al., 2019. [https://doi.org/10.3390/cells8010073](https://doi.org/10.3390/cells8010073) · Cited: 373. Confidence: high.
>    Обзор роли теломер в старении человека.

### Методические / применённые / спорные
...

## Гэпы и наблюдения
- ...

## Источники (technical)
- OpenAlex: <queries>, <records>
- PubMed: ...
- arXiv: ...
```

### Linking rule (MANDATORY)

Every identifier in the output **must be a clickable Markdown link** whose link text is the full URL. Never output a bare id. This overrides any habit of writing `DOI: <number>`.

For each paper, contrast:

- ❌ WRONG (bare, not clickable): `DOI: 10.3390/cells8010073`
- ✅ RIGHT (clickable, link text = URL): `[https://doi.org/10.3390/cells8010073](https://doi.org/10.3390/cells8010073)`

URL patterns to build the link:

- DOI → `https://doi.org/<doi>`
- arXiv → `https://arxiv.org/abs/<id>`
- PMID → `https://pubmed.ncbi.nlm.nih.gov/<pmid>`

Build the link by substituting the real id from the MCP response into BOTH the brackets and the parentheses. Include only the identifiers a paper actually has. Use **only** identifiers returned by MCP (`work_lookup` / search responses); never fabricate one. If a record has no resolvable id, say so explicitly and lower its confidence rating.

## What NOT to do

- Do not invent titles / DOIs / authors — every record must come from a real MCP response.
- Do not substitute web search for MCP — this is an academic skill; the source of truth is science-mcp.
- Do not write a full review or article in the output — return the annotated source list only. Article writing is the job of `scientific-article-creator`.
- Do not treat a saved file as sufficient delivery — always include the annotated list inline in the chat response too, even if you also wrote an artifact.

## Examples

**Request:** "Find the top-10 preprints from 2024 on CRISPR in hereditary myopathies."
→ Query is precise; skip Phase 1. `preprint_search(query="CRISPR hereditary myopathy", sources=["biorxiv","medrxiv"], limit=20)` → post-filter to 2024 → dedup → pick top-10 → `work_lookup` for each → output.

**Request:** "I want to make sense of microbiome and metabolic syndrome."
→ Vague; Phase 1 (Decomposition + Tension Hunting): 3 sub-queries (gut microbiome composition in MetS / SCFA mechanism / FMT clinical trials). For each — `literature_search`. Dedup → top-15 → grouping.

## Limits and risks

- arXiv: 1 request per 3 seconds. For >20 preprints, warn the user about the expected wait.
- Semantic Scholar without an API key returns 429 immediately; the source is skipped. `explain_sources` shows status.
- `ncbi_fetch` full text is capped at 50 KB — for full-text analysis use `open_access_search` (CORE) instead.
