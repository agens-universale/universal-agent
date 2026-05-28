---
id: scientific-article-checker
name: Scientific Article Checker
description: "Verifies an existing SCIENTIFIC article along 6 epistemic dimensions (Evidence Relevance, Falsifiability, Scope Calibration, Argument Coherence, Exploration Integrity, Methodological Rigor) AND resolves every cited DOI/PMID/arXiv identifier through science-mcp (work_lookup) — unverifiable citations are flagged. Optionally weighs citations via citations_by_doi and fetches full-text via ncbi_fetch/open_access_search to confirm cited claims. Outputs a severity-ranked findings report with revision suggestions. Use when the user provides a scientific article / draft and asks to verify, fact-check, review, or audit it. Do NOT use for casual article fact-checking without DOI references — use `articles:check-article` instead."
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

Runs a two-track check: (1) every citation actually exists and matches what the article claims about it; (2) the article's methodology holds up across six epistemic dimensions.

## Inputs

- Markdown / plain text of the scientific article (file path or content).
- Optional: focus (citations only / methodology only / both).

## Algorithm

### Step 1 — citation extraction

- Find every identifier in the text: DOI (`10.\d+/...`), PMID (`PMID:\s*\d+`), arXiv ID (`arXiv:\s*\d+\.\d+`), plus inline `[N]` references resolved through the bibliography.
- Build a map: `claim_in_text → identifier`.

### Step 2 — MCP resolution

For each citation:
1. `science-work_lookup(identifier)` — try to resolve via 6 sources.
2. If it does not resolve → finding **"Unresolved Citation"** (severity: high).
3. If it resolves but the title / authors materially disagree with how the article cites it → finding **"Citation Mismatch"** (severity: medium).
4. `science-citations_by_doi(doi, count_only=true)` — if a key claim relies on a work with 0–2 citations and age >2 years → **"Weak Evidence"** (severity: low–medium).
5. For critical claims: `science-ncbi_fetch(pmid, mode="fetch")` or `science-open_access_search` to confirm the cited work actually contains the asserted result.

### Step 3 — 6 epistemic dimensions

Score each dimension 1 (poor) to 5 (excellent) with concrete findings.

1. **Evidence Relevance** — does the cited experiment actually support the claim, or is it merely "thematically close"?
2. **Falsifiability** — is the claim phrased so it could be refuted; are explicit criteria stated?
3. **Scope Calibration** — does the strength of the claim match the strength of the evidence (no "improves by 30%" when p=0.05 in a single small cohort)?
4. **Argument Coherence** — is there a logical arc observation → gap → insight → solution → evidence, or does the text fall apart?
5. **Exploration Integrity** — are alternatives and dead-ends acknowledged, or does the paper read as if the authors marched in a straight line?
6. **Methodological Rigor** — baselines, ablation / controls, statistical testing, reproducibility discussion?

### Step 4 — severity ranking

Consolidate every finding into one list ranked by severity (high / medium / low). High = blocks publication; medium = requires revision; low = nice-to-have.

### Step 5 — recommendations

For each high / medium finding, give a concrete action: "drop the claim", "replace source with …", "rephrase as…", "add a statistical test".

## MCP tools

- `science-work_lookup(identifier)` — main tool, called for every citation.
- `science-citations_by_doi(doi, count_only=true)` — citation-weight check.
- `science-ncbi_fetch(pmid, mode="fetch")` — PubMed full text (≤50 KB).
- `science-open_access_search(query)` — fallback for full text outside PubMed.

## Output format

A file `<input>.review.md` (or `level2_report.md`):

```markdown
# Эпистемическая проверка: <название статьи>

## Сводка
- Общий балл: <среднее по 6 измерениям>
- Грейд: Strong Accept / Accept / Weak Accept / Borderline / Reject
- Найдено критических проблем: <N high>; средних: <M medium>; пожеланий: <K low>.

## Findings (по severity)

### High (N)
- **[Citation #3]** Unresolved Citation: `10.1234/fake` не резолвится через `work_lookup`. Action: проверить DOI или удалить утверждение.
- **[Section: Results]** Scope Overreach: «метод улучшает выживаемость на 50%», тогда как в источнике [4] только проксиметрика без клинических исходов. Action: переформулировать как «улучшает суррогатный маркер X».

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
- [1] DOI:10.x/y → resolved, title match: yes
- [2] PMID:12345 → resolved, title match: yes
- [3] DOI:10.1234/fake → **UNRESOLVED**
- ...
```

## What NOT to do

- Do not rewrite the article — that is the job of `scientific-article-creator`.
- Do not invent replacement sources — only flag that the current source does not support the claim.
- Do not treat human-readable URLs without a canonical identifier as a successful resolve — flag them as "no canonical ID, manual review needed".

## Examples

**Request:** "Check mRNA_cancer_vaccines_review.md."
→ Step 1: extract DOI / PMID. Step 2: each through `work_lookup`. Step 3: 6 dimensions. Step 4: rank. Step 5: recommendations. Output: `mRNA_cancer_vaccines_review.review.md`.

**Request:** "Only check the citations; do not touch the methodology."
→ Steps 1–2 + Step 4 (Unresolved / Mismatch findings only), no 6 dimensions.

## Limits

- `ncbi_fetch` truncates full text at 50 KB — detailed claim-to-source comparison may need several calls or a CORE fallback.
- If the article exceeds the context window, process it section by section.
