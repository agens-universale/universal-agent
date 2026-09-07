---
id: "check-article"
name: "check-article"
description: "Fact-checks and revises an existing article through claim verification, revision files, rewrite passes, and final merge while preserving useful original content. Use when the user provides an article or draft and asks to verify, correct, improve, fact-check, or revise it. Do not use to create a new article from scratch."
version: "0.1.0"
---

# Skill: Article Fact-Checking and Iterative Revision

## Overview
This skill implements a rigorous, multi‑agent pipeline for verifying and improving any article (technical, journalistic, scientific, etc.). The workflow uses:
- **FactChecker** – validates every factual claim, code snippet, data point, and technical detail against original source artifacts and live web search.
- **RevisionWriter** – produces a structured change log („diff“) based on fact‑check findings.
- **ArticleWriter** – rewrites the article by applying a revision file, creating a new version.
- **Merger** – performs a final, conservative merge between the original article and the last revised version, recovering any valuable content that was lost during multiple rewrite passes.
- **Orchestrator** – controls the sequential gates and versioning.

The process runs two full fact‑check → revision → rewrite cycles, followed by a context‑recovery merge. All intermediate artifacts are preserved and traceable.

## Language Strategy (Critical)
- **Target Language**: All final user‑facing content (the final merged article) MUST be in **Russian**.
- **Intermediate Artifacts**: Fact‑check reports, revision files, and draft articles MAY be in Russian or English, depending on the original article’s language. However, all explanatory notes, comments, and change descriptions MUST be in Russian.
- **Research Phase**: Agents may search sources in any language (English, Russian, etc.). Synthesized conclusions must be written directly in Russian — no translation from English unless the original article itself is in English.
- **Terminology**: Use established Russian technical terms. For English terms without a standard Russian equivalent, keep the English term with a brief Russian explanation on first use.

## Agent Roles & Responsibilities

| Role | Responsibility | Output Language |
|------|---------------|----------------|
| `Orchestrator` | Manages the sequential pipeline, gates, versioning, and fallback logic. | Internal (language‑agnostic) |
| `FactChecker` | Scans the article for every factual claim, assertion, code example, data point, specification, and reference. Uses live web search and provided source artifacts to verify each one. Produces a structured fact‑check report. | Russian (report) |
| `RevisionWriter` | Consumes a fact‑check report and generates a revision file – a precise list of required changes (add, delete, modify, move, or flag for human review). | Russian (revision file) |
| `ArticleWriter` | Takes an existing article (or draft) and a revision file. Applies all changes to produce a new, corrected article version. | Same as input article (preserves original language, but all newly added text must be in Russian unless the original is English) |
| `Merger` | Compares the original article, the final revised article, and the two revision files. Extracts any useful content from the original that is missing in the final article AND does not contradict any verified fact from the revision files. Produces the merged final article. | Same as original article language (with all final output in Russian if original was Russian) |

## Workflow Pipeline

The Orchestrator executes the following **strictly sequential** phases. Each phase must complete successfully before the next begins.

### Phase 0: Initialisation
1. `Orchestrator` receives the **original article** (as a text block or file path) and any **source artifacts** (specifications, data files, reference documents, etc.).
2. `Orchestrator` creates a working directory with versioned subfolders: `v1/`, `v2/`, `v3/`, `merge/`.
3. `Orchestrator` writes the original article as `original_article.md` (or `.txt`).

### Phase 1: First Fact‑Check & Revision (v1)
1. **FactChecker** reads `original_article.md` and all source artifacts.
   - For each claim (fact, number, date, technical parameter, code output, citation, etc.), the FactChecker performs a live web search or inspects the provided artifacts.
   - If a claim is verified, the FactChecker marks it as `[VERIFIED]` with a source citation.
   - If a claim is partially true or false, the FactChecker marks it as `[DISPUTED]` or `[FALSE]` and provides the correct information with evidence.
   - If a claim cannot be verified due to missing sources, the FactChecker marks it as `[UNVERIFIABLE]`.
   - The FactChecker also notes style, clarity, or logical issues (e.g., contradictions, missing steps).
2. `FactChecker` outputs a **fact‑check report** `v1/factcheck_report.md` in the following format:

```markdown
# Фактчекинг: версия 1
## Статья: [название/источник]
## Дата проверки: [дата]

| # | Местоположение (строка/раздел) | Утверждение / факт | Статус | Доказательство / источник | Действие для правок |
|---|--------------------------------|-------------------|--------|---------------------------|---------------------|
| 1 | Раздел "Введение", параграф 2 | "Скорость света в вакууме составляет 299 792 458 м/с" | VERIFIED | [NIST Reference](https://...) | Без изменений |
| 2 | Раздел "Пример кода", строка 12 | "Функция `foo()` возвращает 42" | FALSE | [Документация Python](https://...) → возвращает None | Исправить на "возвращает None" |
| ... | ... | ... | ... | ... | ... |

## Дополнительные замечания по стилю и логике
- ...
```

3. **RevisionWriter** reads `v1/factcheck_report.md` and generates `v1/revision_file.md`. This file must contain **only actionable change instructions**. Format:

```markdown
# Файл правок: версия 1
## Базовый документ: original_article.md
## Изменения:

### DELETE
- [Точное местоположение / цитата] → причина (например, ложное утверждение)

### MODIFY
- [Оригинальный текст] → [Новый текст] → причина (например, фактическая ошибка)

### ADD
- [Где добавить: после строки X / в раздел Y] → [Добавляемый текст] → причина (например, отсутствует важное уточнение)

### MOVE
- [Что переместить] → [Откуда] → [Куда] → причина

### FLAG_FOR_HUMAN
- [Текст / утверждение] → причина неоднозначности (требуется авторское решение)
```

4. `Orchestrator` validates that the revision file contains no conflicting instructions. If conflicts exist, it asks `RevisionWriter` to resolve them.

### Phase 2: First Rewrite (v2)
1. `ArticleWriter` reads `original_article.md` and `v1/revision_file.md`.
2. `ArticleWriter` applies every instruction (DELETE, MODIFY, ADD, MOVE, FLAG_FOR_HUMAN – for flagged items it adds a visible comment like `[!] НЕОБХОДИМО УТОЧНЕНИЕ: ...` but does not change the text).
3. `ArticleWriter` outputs `v2/article_v2.md`.
4. `Orchestrator` verifies that the new article is syntactically valid (Markdown structure preserved) and that all changes from the revision file are reflected.

### Phase 3: Second Fact‑Check & Revision (v2 → v3)
**Repeat Phase 1 and Phase 2 logic, but now using `v2/article_v2.md` as the base document.**

1. `FactChecker` performs a **full fact‑check** on `v2/article_v2.md` (including previously verified claims – re‑checking is mandatory because the rewriting may have introduced new errors).
2. Output: `v2/factcheck_report_v2.md`.
3. `RevisionWriter` generates `v2/revision_file_v2.md` based on the second fact‑check.
4. `ArticleWriter` applies `v2/revision_file_v2.md` to `v2/article_v2.md` and produces `v3/article_v3.md`.

> **Note**: The second cycle may uncover issues that were missed in the first cycle or introduced during the first rewrite. This is expected.

### Phase 4: Context‑Recovery Merge (Final)
The double rewrite may have removed valuable content from the original article that was factually correct and stylistically useful, but was dropped because the revision files focused only on errors. The Merger restores that content safely.

1. `Merger` reads four artifacts:
   - `original_article.md`
   - `v3/article_v3.md` (the final revised article)
   - `v1/revision_file.md`
   - `v2/revision_file_v2.md`
2. `Merger` builds a **kept‑from‑original** set:
   - Start with all content from `original_article.md`.
   - For each DELETE or MODIFY instruction in both revision files, mark the affected original spans as „superseded“ or „removed“.
   - The remaining unmarked original content is „preserved original knowledge“.
3. `Merger` compares `v3/article_v3.md` with the preserved original knowledge:
   - If a piece of preserved original knowledge is **missing** in `v3/article_v3.md` AND it does **not contradict** any fact‑checked statement in the revision files (i.e., no VERIFIED/FALSE entry that would invalidate it), then it is added back.
   - If a piece of preserved original knowledge contradicts a verified fact from the revision files, it is **not** added back, and the Merger logs the conflict.
4. `Merger` produces `final_article.md` with the following structure:
   - The base is `v3/article_v3.md`.
   - Restored content is inserted at appropriate locations (the Merger decides sensible placement, e.g., near related paragraphs).
   - A **footnote or comment** marks each restored block: `[Восстановлено из оригинальной статьи: причина восстановления]`.
5. `Merger` also outputs a **merge report** `merge/merge_report.md` detailing:
   - What was restored and why.
   - What was intentionally not restored (with conflict explanation).
   - Any remaining flags for human review.

### Phase 5: Final Quality Gate
1. `Orchestrator` invokes `FactChecker` one last time on `final_article.md` (light check – only high‑level contradictions, no need to re‑verify every single fact unless a major inconsistency is found).
2. If the light check passes (no CRITICAL issues), the `Orchestrator` outputs `final_article.md` as the deliverable.
3. If any CRITICAL issue remains, the `Orchestrator` may either:
   - Reject the merge and ask `Merger` to adjust, or
   - Fall back to `v3/article_v3.md` as final and report the unresolved merge conflicts to the user.

## Output Specifications
- **Final deliverable**: `final_article.md` (or `.txt` / `.docx` if specified). Language: Russian for all user‑facing content.
- **Intermediate artifacts** (all must be preserved and versioned):
  - `v1/factcheck_report.md`
  - `v1/revision_file.md`
  - `v2/article_v2.md`
  - `v2/factcheck_report_v2.md`
  - `v2/revision_file_v2.md`
  - `v3/article_v3.md`
  - `merge/merge_report.md`
- **Citation style**: Every `[VERIFIED]` entry in fact‑check reports must include an inline source: `[Источник](URL)`. For offline artifacts, cite the file name and section.
- **Quality gates**:
  - Zero unverified claims after Phase 1 and Phase 3 (every claim has a status: VERIFIED / FALSE / DISPUTED / UNVERIFIABLE). UNVERIFIABLE claims must be flagged for human review.
  - Revision files must be syntactically correct and contain no contradictions.
  - The merge phase must not introduce unmarked changes.

## Execution Constraints
1. **Strict role separation**: Only `FactChecker` may judge factuality; only `ArticleWriter` may modify the article text; only `Merger` may perform the final context restoration.
2. **Mandatory live search**: `FactChecker` MUST use web search tools for any claim that is not trivially verifiable from provided artifacts. Internal knowledge alone is insufficient.
3. **Two complete cycles**: The pipeline cannot skip the second fact‑check/rewrite cycle. The double iteration is essential for catching errors introduced in the first rewrite.
4. **Artifact versioning**: All files must follow the naming scheme `article_vX.md`, `factcheck_report_vX.md`, `revision_file_vX.md`. No overwriting.
5. **Merge conflict transparency**: Any content from the original that is deliberately not restored must be documented in `merge/merge_report.md`.
6. **Final output language**: The final article must be in Russian unless the user explicitly requests another language. If the original article is in English, the rewritten versions may stay in English, but all fact‑check reports, revision files, and merge reports must be in Russian.

## Expected Final Artifact Format (Example)
```markdown
# [Название финальной статьи]

> [Примечание: блоки, восстановленные из оригинальной статьи, отмечены сносками.]

## Введение
... (текст из v3/article_v3.md)

## Детали
... (текст из v3)

## Дополнительный контекст (восстановлено из оригинальной статьи)
[Восстановлено из оригинальной статьи: этот абзац содержал правильное описание протокола, не противоречащее проверенным фактам, и был утерян при двойной перезаписи.]
... (восстановленный текст)

## Заключение
...

## Сводка правок и слияния
См. `merge/merge_report.md`
```

## Failure & Recovery Protocol
- **If FactChecker cannot verify a critical claim after 3 search attempts**: mark as `[UNVERIFIABLE]` and raise a flag. The RevisionWriter must add a `FLAG_FOR_HUMAN` instruction. The pipeline continues, but the final article will contain `[!] НЕ ПРОВЕРЕНО` placeholders.
- **If ArticleWriter fails to apply a revision file cleanly** (e.g., instruction refers to missing text): pause pipeline, ask RevisionWriter to regenerate with precise locations, then retry.
- **If Merger detects a conflict that affects more than 30% of the original article**: instead of automatic merging, produce a side‑by‑side diff and request human guidance.
- **If the second fact‑check finds more than 10 new critical errors**: consider restarting from Phase 1 with a more conservative revision file (i.e., only minimal changes). Orchestrator may ask the user for permission to restart.
- **Orchestrator may skip the merge phase** if `v3/article_v3.md` already contains all relevant content from the original (verified by comparing word‑overlap > 95%). In that case, `v3/article_v3.md` becomes the final article.
