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
- Call (or delegate to `scientific-research`) — `literature_search` with 2–4 topical queries, collect 15–30 candidates.
- Add `preprint_search` for hot subareas.
- Run every selected work through `work_lookup` to ensure a normalized `WorkRecord`.
- Pick 8–15 key works for inclusion in the text.

### Step 2.5 — read the full text of the core papers (MANDATORY)

A review synthesizes methods and results, which live in the article BODY, not the
abstract. For the 2–5 papers the review is actually built on (the central works — not
all 8–15), read their full text BEFORE drafting. This is a standard step, not optional.
Use the chain in "Reading full text" below, ONE path per paper:
- arXiv / CORE / bioRxiv-medRxiv → the PDF chain (download the PDF, then convert it to
  text with the office-document reader). PDF is ~100% coverage and the proven path; do
  not rely on arXiv HTML (only exists for some recent papers).
- PubMed/PMC with a `pmcid` → fetch the PMC HTML page with the URL-fetch tool.
Do NOT read the same paper through two paths. If a body cannot be fetched (paywall, no
OA), use its abstract and lower that work's confidence — never fabricate body content.

### Step 3 — draft with inline citations

- Write sections per the plan. Every claim that contains a number, name, year, specific mechanism, method, or conclusion MUST carry an inline citation `[N]` referencing an entry in the bibliography block.
- "Common knowledge" claims (e.g. "DNA is made of four nucleotides") may stand without a citation.
- **Grounding guard:** before writing a claim with a citation, confirm the source body you read at Step 2.5 actually supports it. If the body does not support it, do not write it — soften the wording or mark `[CITATION NEEDED]`. Never attribute causality ("causes", "enables") or superiority ("outperforms") to a source whose body only shows correlation / association. (Lightweight grounding check; the full claim-type audit is the job of `scientific-article-checker`.)
- If no supporting paper can be found, write `[CITATION NEEDED]` with a short note "searched X, Y — not found".

### Step 4 — bibliography block

End the article with a `## Литература` section, numbered. Entry format:

```
[N] Authors. Title. Journal/Venue, Year. DOI:<…> | PMID:<…> | arXiv:<…>. URL: <…>
```

Every identifier comes from an MCP response — never from model memory.

### Step 5 — citation strength check

For critical claims (central hypothesis, headline numbers) call `citations_by_doi(doi=…, count_only=true)`. If a work older than 2 years has 0 citations, mark it as "weak evidence" inline or seek additional support.

### Step 6 — final pass

- Read the draft, remove duplicates, polish Russian prose.
- Verify no `[CITATION NEEDED]` remains without an explicit note.
- Save to `tmp/science-smoke-en/<task>/article.md` or to the directory specified by the user.

## MCP tools

- `literature_search`, `preprint_search` — search.
- `work_lookup(identifier)` — normalization before adding to the bibliography.
- `citations_by_doi(doi, count_only=true)` — citation-weight check.
- `open_access_search` — when full text is needed for deeper extraction.

## Reading full text

science-mcp returns metadata + abstract + a link, **not the article body**. When a
claim needs the actual full text (methods, results, exact numbers) and the source is
arXiv / a CORE `downloadUrl` / a bioRxiv-medRxiv PDF, read the body with this two-tool
chain (both tools are already in your registry):

> Tools below are named by **function**, not by exact registry id (ids carry server
> prefixes and can be renamed/absent). Match them by capability in your registry:
> - **file-download tool** — HTTP GET of a document (PDF/DOCX) to the downloads dir;
> - **office-document reader** — turns a PDF/DOCX on disk into markdown (the markitdown-
>   based reader; NOT the plain-text file reader, which returns "binary data" for a PDF);
> - **URL-fetch tool** — HTTP GET of a web page → readable text;
> - **stored-output pager** — reads a large stored tool result by `output_ref` + `offset`/`limit`.

1. Build the PDF URL.
   - arXiv: `https://arxiv.org/pdf/<arxiv_id>` (e.g. `2210.11416` → `https://arxiv.org/pdf/2210.11416`).
   - CORE: use the record's `downloadUrl` as-is.
2. Pick a filename: the id with a `.pdf` extension. The filename may contain only
   `A-Za-z0-9._-` — if an arXiv id contains a slash (old style, e.g. `math/0309136`),
   replace the slash with `_` → `math_0309136.pdf`.
3. Download it with the **file-download tool** (PDF URL + filename) → it returns an
   absolute `path`.
4. Convert it to text with the **office-document reader** — give it only the file path
   (it takes no `offset`/`limit`). It returns the full text prefixed with
   `[document: format=pdf, total_chars=…]`. When the body is large the runtime stores it
   and hands back an `output_ref`; page through it with the **stored-output pager** (by
   `output_ref` + `offset`/`limit`), not by passing `offset`/`limit` to the office-document
   reader itself. You need not consume all of it.

Rules:
- Read full text for the CORE papers by default (Step 2.5). For the remaining peripheral
  works, abstract + metadata are enough — do not download every paper (one PDF is
  hundreds of KB and inflates context). Read exactly ONE path per paper; never fetch the
  same body twice.
- Never invent a PDF URL for a paper science-mcp did not return.
- If the download fails (paywall, 403, non-PDF) or the office-document reader returns
  `PATH_NOT_ALLOWED` / `parse_failed`, fall back to the abstract and lower the claim's
  confidence — do not fabricate body content.
- PubMed / CrossRef / DBLP records have no open PDF here; do not attempt the chain for them.

### PMC full text (HTML, no download)

If a PubMed/PMC record has a `pmcid` (e.g. `PMC13175772`), its open-access body is
readable directly as HTML — no PDF download needed. Fetch it with the **URL-fetch tool**
on `https://www.ncbi.nlm.nih.gov/pmc/articles/<pmcid>/` (the `pmcid` value already
includes the `PMC` prefix); the URL-fetch tool pages with `offset`/`limit` if the page is
long. If a record has no `pmcid`, there is no free full text on NCBI — use the abstract
and lower the claim's confidence; do not attempt a PDF download for it.

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
