---
id: scientific-article-checker
name: Scientific Article Checker
description: "Verifies an existing SCIENTIFIC article along 6 epistemic dimensions (Evidence Relevance, Falsifiability, Scope Calibration, Argument Coherence, Exploration Integrity, Methodological Rigor) AND resolves every cited DOI/PMID/arXiv identifier through science-mcp (work_lookup) — unverifiable citations are flagged. Weighs citations via citations_by_doi and runs claim-audit — fetching source text via ncbi_fetch/open_access_search and judging whether each cited source actually supports the claim. Outputs a severity-ranked findings report with revision suggestions. Use when the user provides a scientific article / draft and asks to verify, fact-check, review, or audit it. Do NOT use for casual article fact-checking without DOI references — use `articles:check-article` instead."
version: 0.1.0
---

# Skill: Scientific Article Checker

## Language Strategy (MANDATORY)

- This SKILL.md is in English (project convention).
- **The review report MUST be written in Russian.** Title, summary, findings text, dimension comments, recommendations — all Russian. Even when the article being reviewed is in English or contains English technical terms, the review report itself is Russian Markdown.
- Quoted titles, journals, DOI / PMID / arXiv identifiers and direct quotes from the article stay verbatim in their original language.
- If the user explicitly asks for English output, switch — otherwise default to Russian regardless of the input language.

## When to use

- Triggers: "check this scientific article", "review this paper", "fact-check with DOI verification", "assess the methodology of this article", a file with DOI / PMID / arXiv identifiers in its citations.
- Do NOT use for casual article fact-checking without scientific references → `articles:check-article`. Do NOT use to write an article from scratch → `scientific-article-creator`.

## What it does

Runs a two-track check: (1) every citation actually exists and matches what the article claims about it; (2) the article's methodology holds up across six epistemic dimensions. **The audited article is read-only** — this skill produces a review report and never edits the article itself (see the read-only contract under "What NOT to do").

## Inputs

- Markdown / plain text of the scientific article (file path or content).
- Optional: focus (citations only / methodology only / both).

## Algorithm

### Step 1 — citation extraction

- Find every identifier in the text: DOI (`10.\d+/...`), PMID (`PMID:\s*\d+`), arXiv ID (`arXiv:\s*\d+\.\d+`), plus inline `[N]` references resolved through the bibliography.
- Build a map: `claim_in_text → identifier`.

### Step 2 — MCP resolution

For each citation:
1. `work_lookup(identifier)` — try to resolve via 6 sources.
2. If it does not resolve → finding **"Unresolved Citation"** (severity: high).
3. If it resolves but the title / authors materially disagree with how the article cites it → finding **"Citation Mismatch"** (severity: medium).
4. `citations_by_doi(doi, count_only=true)` — if a key claim relies on a work with 0–2 citations and age >2 years → **"Weak Evidence"** (severity: low–medium).

### Step 2.5 — claim-audit (faithfulness of cited sources)

For each citation that resolved in Step 2 AND backs a substantive claim
(a number, an effect size, a mechanism, a clinical outcome):

1. **Locate the claim** the citation is attached to in the article text.
2. **Fetch the source BODY** (not just the abstract — the type-aware design check below
   needs the methods/results section), in priority order. Tools are named by **function**,
   not by exact registry id (ids carry server prefixes / can be renamed): the
   **file-download tool**, the **office-document reader** (PDF/DOCX → markdown — not the
   plain-text file reader), the **URL-fetch tool**, the **stored-output pager**.
   - arXiv / CORE / bioRxiv-medRxiv → download the PDF with the **file-download tool**,
     then convert it with the **office-document reader** (give it only the file path — it
     takes no `offset`/`limit`; page a large body with the **stored-output pager** by
     `output_ref` + `offset`/`limit`);
   - PubMed/PMC with a `pmcid` → fetch `https://www.ncbi.nlm.nih.gov/pmc/articles/<pmcid>/`
     with the **URL-fetch tool** (HTML);
   - fallback → `ncbi_fetch(pmid=..., mode="fetch")` (≤50 KB) or
     `open_access_search(query="<title or DOI>")` (abstract-level only);
   - if no body can be fetched → `unverifiable-no-fulltext` (NOT the same as
     unsupported).
3. **Judge support** — compare the article's claim against the fetched text:
   - **supported** — the source states the claim (or a stronger version).
   - **partial** — the source is related but weaker / narrower / a different
     population than the article asserts.
   - **unsupported** — the source does not contain the claim, or contradicts it.
   - **unverifiable-no-fulltext** — no text could be fetched (paywall, no PMID,
     >50 KB). This is NOT the same as unsupported — say so explicitly.
4. **Type-aware design check** — classify the claim by its wording and verify the
   fetched body contains evidence of the REQUIRED design:
   - Causal ("causes", "leads to", "enables") → an isolating **ablation**;
   - Improvement ("outperforms", "better", "−N%") → a **baseline** comparison;
   - Generalization ("robust", "across", "generalizes") → **heterogeneous** test conditions;
   - Descriptive ("accounts for", "distribution") → a **representative** sample;
   - Scoping ("when", "under conditions") → explicitly **declared bounds**.
   Mismatch (e.g. a causal claim with correlation-only evidence, or an improvement
   claim with no baseline) → finding **"Claim/Evidence-design Mismatch"**, severity
   **medium** (low for descriptive / scoping).
5. **Emit findings:**
   - `unsupported` → severity **high** ("Claim Not Supported by Source").
   - `partial` → severity **medium** ("Claim Overstates Source").
   - `unverifiable-no-fulltext` → severity **low** ("Citation Not Audited").

Audit only substantive claims — skip background / common-knowledge statements.
For a large article, audit at least every claim carrying a headline number or
the central thesis; note in the report if some were skipped for length. The
50 KB `ncbi_fetch` cap and section-by-section handling are already covered in
the existing `## Limits` section — rely on them, do not restate.

### Step 3 — 6 epistemic dimensions

Score each dimension 1 (poor) to 5 (excellent) with concrete findings.

1. **Evidence Relevance** — does the cited experiment actually support the claim (not merely "thematically close"), and does its design match the claim TYPE (causal→ablation, improvement→baseline, generalization→heterogeneous conditions — see the type-aware check in Step 2.5)?
2. **Falsifiability** — is the claim phrased so it could be refuted; are explicit criteria stated?
3. **Scope Calibration** — does the strength of the claim match the strength of the evidence (no "improves by 30%" when p=0.05 in a single small cohort)?
4. **Argument Coherence** — is there a logical arc observation → gap → insight → solution → evidence, or does the text fall apart?
5. **Exploration Integrity** — are alternatives and dead-ends acknowledged, or does the paper read as if the authors marched in a straight line?
6. **Methodological Rigor** — baselines, ablation / controls, statistical testing, reproducibility discussion?

### Step 4 — severity ranking

Consolidate every finding into one list ranked by severity (high / medium / low). High = blocks publication; medium = requires revision; low = nice-to-have.

**Evidence-span rule (MANDATORY):** every finding about text that IS present in the article or a fetched source must quote the exact verbatim substring that triggered it (from the article and/or the source body). If you cannot quote it verbatim, do not emit the finding. Findings about something ABSENT (a missing baseline, no declared bounds) may omit the quote.

### Step 5 — recommendations

For each high / medium finding, give a concrete action **as text in the report**: "drop the claim", "replace source with …", "rephrase as…", "add a statistical test". You only WRITE the recommendation into the review report — you do NOT apply it to the audited article. Fixing the article is a separate `scientific-article-creator` pass, run only if the user explicitly asks.

## MCP tools

- `work_lookup(identifier)` — main tool, called for every citation.
- `citations_by_doi(doi, count_only=true)` — citation-weight check.
- `ncbi_fetch(pmid, mode="fetch")` — PubMed/PMC full text (≤50 KB); used in Step 2.5 to audit claim faithfulness.
- `open_access_search(query)` — OA full-text fallback outside PubMed; used in Step 2.5 when there is no PMID or the text exceeds 50 KB.

## Output format

A file `<input>.review.md` (or `level2_report.md`):

```markdown
# Эпистемическая проверка: <название статьи>

## Сводка
- Общий балл: <среднее по 6 измерениям>
- Грейд: Strong Accept / Accept / Weak Accept / Borderline / Reject
- Найдено критических проблем: <N high>; средних: <M medium>; пожеланий: <K low>.
- Claim-audit: <S> supported / <P> partial / <NS> unsupported / <NA> not audited.

## Findings (по severity)

### High (N)
- **[Citation #3]** Unresolved Citation: `10.1234/fake` не резолвится через `work_lookup`. Action: проверить DOI или удалить утверждение.
- **[Section: Results]** Scope Overreach: «метод улучшает выживаемость на 50%», тогда как в источнике [4] только проксиметрика без клинических исходов. Action: переформулировать как «улучшает суррогатный маркер X».
- **[Claim → Citation #4]** Claim Not Supported by Source: статья утверждает «снижение смертности на 40%», источник [4] (`ncbi_fetch`) сообщает только о суррогатной конечной точке без данных по смертности. Action: удалить утверждение или заменить источник.

### Medium (M)
- ...

### Low (K)
- ...

## Оценки по 6 измерениям

| Dimension | Score (1–5) | Комментарий |
|---|---|---|
| Evidence Relevance | 3 | ... |
| Falsifiability | 4 | ... |
| Scope Calibration | 2 | overreach в Results |
| Argument Coherence | 4 | ... |
| Exploration Integrity | 3 | альтернатив не обсуждали |
| Methodological Rigor | 4 | baseline есть, ablation нет |

## Карта цитат
- [1] DOI:10.x/y → resolved, title match: yes, support: supported
- [2] PMID:12345 → resolved, title match: yes, support: partial
- [3] DOI:10.1234/fake → **UNRESOLVED** (support: n/a)
- [4] PMID:67890 → resolved, support: **unsupported**
- ...
```

## What NOT to do

- **Read-only contract (HARD):** the audited article is read-only. NEVER modify it — no file-writing, search-and-replace, or apply-edits tool on the article path. Your only artifact is the review report file (`*.review.md`); rewriting the article or applying fixes is a separate `scientific-article-creator` pass, run only on explicit user request. Even if the SAME session just wrote the article, switch to report-only here — emit findings + textual recommendations, do not touch the file.
- Do not invent replacement sources — only flag that the current source does not support the claim.
- Do not treat human-readable URLs without a canonical identifier as a successful resolve — flag them as "no canonical ID, manual review needed".
- Do not mark a claim `unsupported` when the full text simply could not be fetched — use `unverifiable-no-fulltext`.
- Do not audit background or common-knowledge sentences — only substantive claims (numbers, effect sizes, mechanisms, outcomes).

## Examples

**Request:** "Check mRNA_cancer_vaccines_review.md."
→ Step 1: extract DOI / PMID. Step 2: each through `work_lookup`. Step 3: 6 dimensions. Step 4: rank. Step 5: recommendations. Output: `mRNA_cancer_vaccines_review.review.md`.

**Request:** "Only check the citations; do not touch the methodology."
→ Steps 1–2 + Step 4 (Unresolved / Mismatch findings only), no 6 dimensions.

**Request:** "Check this review, and verify each source actually supports its claim."
→ Steps 1–2 resolve citations. Step 2.5 fetches each cited source via `ncbi_fetch` / `open_access_search` and judges support. An article claim of "reduces mortality by 80%" backed by a source that only reports a surrogate marker → High finding "Claim Not Supported by Source".

## Limits

- `ncbi_fetch` truncates full text at 50 KB — detailed claim-to-source comparison may need several calls or a CORE fallback.
- If the article exceeds the context window, process it section by section.
