---
id: scientific-article-checker
name: Scientific Article Checker
description: "Verifies an existing SCIENTIFIC article along 6 epistemic dimensions (Evidence Relevance, Falsifiability, Scope Calibration, Argument Coherence, Exploration Integrity, Methodological Rigor) AND resolves every cited DOI/PMID/arXiv identifier through science-mcp (work_lookup) — unverifiable citations are flagged. Optionally weighs citations via citations_by_doi and fetches full-text via ncbi_fetch/open_access_search to confirm cited claims. Outputs a severity-ranked findings report with revision suggestions. Use when the user provides a scientific article / draft and asks to verify, fact-check, review, or audit it. Do NOT use for casual article fact-checking without DOI references — use `articles:check-article` instead."
version: 0.1.0
---

# Skill: Scientific Article Checker

## Когда использовать

- Триггеры: «проверь эту научную статью», «отрецензируй обзор», «fact-check с проверкой DOI», «оцени методологию статьи», файл со ссылками DOI/PMID/arXiv в нём.
- НЕ использовать: бытовая проверка статьи без научных ссылок → `articles:check-article`; создание статьи с нуля → `scientific-article-creator`.

## Что делает

Проводит двойную проверку: (1) каждая цитата существует и действительно говорит то, что заявлено в статье; (2) методология статьи выдерживает 6 эпистемических измерений.

## Входные данные

- Markdown / текст научной статьи (путь к файлу или содержимое).
- Опционально: фокус (только методология / только цитаты / всё).

## Алгоритм

### Шаг 1 — извлечение цитат

- Найти в тексте все идентификаторы: DOI (`10.\d+/...`), PMID (`PMID:\s*\d+`), arXiv ID (`arXiv:\s*\d+\.\d+`), плюс инлайн-формы `[N]` с расшифровкой в bibliography.
- Сохранить карту: claim_in_text → identifier.

### Шаг 2 — резолв через MCP

Для каждой ссылки:
1. `science-work_lookup(identifier)` — пытается резолвить через 6 источников.
2. Если не резолвится — finding **«Unresolved Citation»** (severity: high).
3. Если резолвится, но заголовок / авторы заметно расходятся с тем, как процитировано в тексте — finding **«Citation Mismatch»** (severity: medium).
4. `science-citations_by_doi(doi, count_only=true)` — если ключевое утверждение опирается на работу с 0–2 цитатами при возрасте >2 лет → **«Weak Evidence»** (severity: low–medium).
5. Для критических утверждений — `science-ncbi_fetch(pmid, mode="fetch")` или `science-open_access_search` для подтверждения, что цитируемая работа действительно содержит заявленный результат.

### Шаг 3 — 6 эпистемических измерений

Каждое измерение оценить от 1 (плохо) до 5 (отлично), привести конкретные находки.

1. **Evidence Relevance** — действительно ли цитируемый эксперимент подтверждает claim, или это «тематически близкая» работа.
2. **Falsifiability** — сформулирован ли claim так, что его можно было бы опровергнуть; есть ли заявленные критерии.
3. **Scope Calibration** — соответствует ли сила утверждения силе доказательств (нет ли «улучшает на 30%» при p=0.05 в одной малой когорте).
4. **Argument Coherence** — есть ли логическая цепь наблюдение → пробел → инсайт → решение → доказательство, или текст разваливается.
5. **Exploration Integrity** — упомянуты ли альтернативы и тупики, или статья выглядит так, будто авторы шли по прямой.
6. **Methodological Rigor** — есть ли baseline, ablation/control, статистическая проверка, обсуждение воспроизводимости.

### Шаг 4 — severity ranking

Все finding'и собрать в общий список с severity (high / medium / low). High = блокер публикации; medium = требует ревизии; low = пожелание.

### Шаг 5 — рекомендации

Для каждой high/medium находки — конкретная рекомендация: «удалить утверждение», «заменить источник на …», «переформулировать как…», «добавить статистический тест».

## MCP-тулы

- `science-work_lookup(identifier)` — основной, для каждой цитаты.
- `science-citations_by_doi(doi, count_only=true)` — оценка веса.
- `science-ncbi_fetch(pmid, mode="fetch")` — full-text PubMed (≤50 КБ).
- `science-open_access_search(query)` — fallback для full-text вне PubMed.

## Формат вывода

Файл `<input>.review.md` (или `level2_report.md`):

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

## Что НЕ делать

- НЕ переписывать статью — это задача `scientific-article-creator`.
- НЕ выдумывать заменяющие источники — только показывать, что текущий источник не подходит.
- НЕ резолвить «человекочитаемые» ссылки без identifier'а как успех — если в статье ссылка только URL'ом, отметить как «no canonical ID, manual review needed».

## Примеры

**Запрос:** «Проверь mRNA_cancer_vaccines_review.md».
→ Шаг 1: извлечь DOI/PMID. Шаг 2: каждый прогнать через `work_lookup`. Шаг 3: 6 измерений. Шаг 4: ранжировать. Шаг 5: рекомендации. Вывод: `mRNA_cancer_vaccines_review.review.md`.

**Запрос:** «Проверь только цитаты, методологию не трогай».
→ Шаги 1–2 + Шаг 4 (только Unresolved/Mismatch findings), без 6 измерений.

## Ограничения

- `ncbi_fetch` обрезает full-text до 50 КБ — для подробной сверки claim'а с источником может потребоваться несколько вызовов или fallback в CORE.
- Если статья сильно превышает контекст — обрабатывать секциями.
