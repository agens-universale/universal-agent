---
id: scientific-article-creator
name: Scientific Article Creator
description: "Authors a Russian Markdown SCIENTIFIC article / literature review with VERIFIED citations. Structure: contribution → main figure idea → abstract → intro → methods → results/synthesis → related work → limitations. Every claim is backed by a paper resolved through science-mcp (literature_search + work_lookup); the bibliography block contains normalized entries with DOI/PMID/arXiv IDs. Never cites from model memory — unverifiable claims are tagged `[CITATION NEEDED]` instead. Use when the user asks to write a scientific article, paper, review, or topical synthesis for publication or formal use. Do NOT use for casual articles without citation-verification requirement — use `articles:article-creator` instead. Do NOT use to verify an existing article — use `scientific-article-checker`."
version: 0.1.0
---

# Skill: Scientific Article Creator

## Language Strategy (MANDATORY)

- This SKILL.md is in English (project convention).
- **The article MUST be written in Russian.** Title, abstract, every section heading, every sentence of the narrative — all Russian. Even when the topic is named with English technical terms (CRISPR, mRNA, GLP-1, Alzheimer), the article itself is Russian Markdown.
- Source paper titles, journal names, author lists, and DOI / PMID / arXiv identifiers stay verbatim in their original language inside the bibliography block.
- If the user explicitly asks for English output, switch — otherwise default to Russian regardless of the input language.

## When to use

- Triggers: "write a scientific review / article on topic X", "literature review for publication", "paper-style review", "turn this into a scientific article".
- Do NOT use for casual articles without source verification → `articles:article-creator`. Do NOT use to verify an existing article → `scientific-article-checker`. Do NOT use for an unstructured idea sketch without citations → `scientific-research`.

## What it does

Produces a 1500–5000-word scientific text in Russian where every non-trivial claim is backed by a real paper with a verified identifier (DOI / PMID / arXiv).

## Inputs

- Topic, target word count, optional audience (specialists / general).
- Optional: a research artifact from `research-artifact-compiler` or a sources list from `scientific-research`.

## Algorithm

### Step 1 — article skeleton

1. State one central contribution / thesis in one sentence.
2. Describe "Figure 1" in one paragraph — what the headline illustration would be (even if not drawn).
3. Sketch section headings: abstract, introduction, methods / approach, results / synthesis, related work, limitations, conclusion.

### Step 2 — literature collection

If no sources list is provided on input:
- Call (or delegate to `scientific-research`) — `science-literature_search` with 2–4 topical queries, collect 15–30 candidates.
- Add `science-preprint_search` for hot subareas.
- Run every selected work through `science-work_lookup` to ensure a normalized `WorkRecord`.
- Pick 8–15 key works for inclusion in the text.

### Step 3 — draft with inline citations

- Write sections per the plan. Every claim that contains a number, name, year, specific mechanism, method, or conclusion MUST carry an inline citation `[N]` referencing an entry in the bibliography block.
- "Common knowledge" claims (e.g. "DNA is made of four nucleotides") may stand without a citation.
- If no supporting paper can be found, write `[CITATION NEEDED]` with a short note "searched X, Y — not found".

### Step 4 — bibliography block

End the article with a `## Литература` section, numbered. Entry format:

```
[N] Authors. Title. Journal/Venue, Year. DOI:<…> | PMID:<…> | arXiv:<…>. URL: <…>
```

Every identifier comes from an MCP response — never from model memory.

### Step 5 — citation strength check

For critical claims (central hypothesis, headline numbers) call `science-citations_by_doi(doi=…, count_only=true)`. If a work older than 2 years has 0 citations, mark it as "weak evidence" inline or seek additional support.

### Step 6 — final pass

- Read the draft, remove duplicates, polish Russian prose.
- Verify no `[CITATION NEEDED]` remains without an explicit note.
- Save to `tmp/science-smoke-en/<task>/article.md` or to the directory specified by the user.

## MCP tools

- `science-literature_search`, `science-preprint_search` — search.
- `science-work_lookup(identifier)` — normalization before adding to the bibliography.
- `science-citations_by_doi(doi, count_only=true)` — citation-weight check.
- `science-open_access_search` — when full text is needed for deeper extraction.

## Output format

A finished Markdown file:

```markdown
# <Заголовок статьи>

> Contribution: <одно предложение>

## Abstract
<150–250 слов>

## Введение
<2–4 абзаца, заканчивающихся чёткой постановкой>

## Методы / подход
...

## Результаты / синтез
...

## Связанные работы
...

## Ограничения
...

## Заключение
...

## Литература

[1] ...
[2] ...
```

## What NOT to do

- Do not generate BibTeX or citations from model memory.
- Do not leave `[CITATION NEEDED]` without an explanatory note.
- Do not use web search as a citation source — only science-mcp.
- Do not duplicate the function of `scientific-article-checker` — verification runs as a separate step on request.

## Examples

**Request:** "Write a 3000-word review on the link between gut microbiome and metabolic syndrome."
→ Step 1: contribution = "synthesis of evidence for the causal microbiome → MetS link via SCFA and endotoxemia". Figure 1 = pathway diagram. Steps 2–6 as planned. Bibliography ≈ 12–15 works, every entry carrying DOI / PMID.

**Request:** "Turn this artifact into an article."
→ Sources are already normalized by the compiler. Skip Step 2, jump to Step 3.

## Limits

- Full-text deep dives often require `open_access_search` (CORE) or PMC; PubMed XML is capped at 50 KB.
- A long review (>4000 words) with >20 sources may need a second search pass — that is expected.
