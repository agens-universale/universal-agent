---
id: scientific-research
name: Scientific Research
description: "Multi-source ACADEMIC research through science-mcp: optionally sharpens vague intent using 10 cognitive lenses (problem-first, abstraction ladder, tension hunting, cross-pollination, what-changed, failure analysis, simplicity, stakeholder rotation, composition/decomposition, explain-to-someone), then queries literature_search + preprint_search + open_access_search across OpenAlex/PubMed/arXiv/bioRxiv/medRxiv/Semantic Scholar/CORE, dedups, normalizes every result via work_lookup, returns a confidence-rated list of papers with DOI/PMID/arXiv IDs. Use when the user asks to investigate a scientific topic, find peer-reviewed papers, build a literature overview, locate preprints. Do NOT use for non-academic web research — use `topic-research` instead."
version: 0.1.0
---

# Skill: Scientific Research

## Когда использовать

- Триггеры: «найди статьи / препринты по теме X», «исследовать научную тему Y», «литературный обзор», «что сейчас в литературе по…», упоминания DOI / PMID / arXiv / журналов / peer-reviewed.
- НЕ использовать: общий веб-ресерч без научной литературы → `topic-research`; написание готовой статьи → `scientific-article-creator`.

## Что делает

Собирает по теме набор реально существующих научных работ из 6+ источников, нормализует идентификаторы (DOI/PMID/arXiv), сортирует по релевантности и цитируемости, маркирует уверенность.

## Входные данные

- Тема или вопрос пользователя (часто расплывчатый).
- Опциональные ограничения: годы, типы источников (только препринты, только OA), желаемое число результатов.

## Алгоритм

### Фаза 1 — формализация запроса (если интент расплывчатый)

Прогнать через 1–3 линзы из списка ниже, чтобы получить 2–4 чётких поисковых запроса вместо одного нечёткого:

1. **Problem-First vs Solution-First** — что именно болит, или какой инструмент ищем?
2. **Abstraction Ladder** — нужен общий обзор области или конкретная техника?
3. **Tension Hunting** — есть ли спорный аспект, противоречие двух школ?
4. **Cross-Pollination** — какие соседние области решают похожую задачу?
5. **What Changed?** — что нового появилось за последние 1–3 года?
6. **Failure Analysis** — где известные подходы ломаются?
7. **Simplicity Test** — какая самая простая формулировка вопроса?
8. **Stakeholder Rotation** — клиницист, ML-инженер, биолог дадут разные query.
9. **Composition / Decomposition** — разбить тему на 2–3 подвопроса.
10. **Explain-It-to-Someone** — переформулировать как «объясни школьнику».

Линзы — инструмент, не обязательный шаг. Если запрос уже точный (упомянуты ключевые термины, годы, источник) — пропустить фазу 1.

### Фаза 2 — поиск

1. Вызвать `science-explain_sources` один раз в начале сессии (если ещё не вызывался) — зафиксировать какие источники доступны (API ключи).
2. Для каждого сформулированного запроса:
   - `science-literature_search` — общий проход (журналы + agg).
   - `science-preprint_search` — если важно «свежее, до peer-review» (arXiv по умолчанию, +bioRxiv/medRxiv для биомед).
   - `science-open_access_search` — если пользователю важен полный текст.
3. Дедуплицировать по DOI / arXiv ID.
4. Для топ-N (10–30) сделать `science-work_lookup` чтобы получить нормализованный `WorkRecord` (если ещё не нормализован).

### Фаза 3 — синтез

- Сортировка: по citation_count для устоявшихся тем; по date для горячих тем (последние 12 месяцев).
- Группировка: «фундаментальные / методические / применённые / спорные».
- Для каждой работы — оценка уверенности (high / medium / low) на основе: источник найден в ≥2 базах, корректно разрешается DOI, citation_count > 5 (для статей старше года).

## MCP-тулы

- `science-literature_search(query: str, year_from?: int, year_to?: int, limit?: int)` — основной поиск.
- `science-preprint_search(query: str, sources?: ["arxiv","biorxiv","medrxiv"], limit?: int)` — препринты.
- `science-open_access_search(query: str, limit?: int)` — OA-only.
- `science-work_lookup(identifier: str)` — нормализация одной записи.
- `science-explain_sources()` — pre-flight доступности.

## Формат вывода

Markdown со структурой:

```markdown
# Обзор источников: <тема>

## Сформулированные запросы
1. ...
2. ...

## Найденные работы (N)

### Фундаментальные
1. **<Title>** — Authors, Year. DOI:<…> | PMID:<…> | arXiv:<…>. Cited: <N>. Confidence: high.
   <короткая аннотация 1–2 строки>

### Методические / применённые / спорные
...

## Гэпы и наблюдения
- ...
- ...

## Источники (technical)
- OpenAlex: <запросы>, <число записей>
- PubMed: ...
- arXiv: ...
```

## Что НЕ делать

- НЕ изобретать названия / DOI / авторов — каждая запись приходит из реального ответа MCP.
- НЕ использовать веб-поиск как замену MCP — это академический скилл, источник истины — science-mcp.
- НЕ писать обзор / статью в выводе — отдать только список с аннотациями. Текстовая статья — задача `scientific-article-creator`.

## Примеры

**Запрос:** «Найди топ-10 препринтов 2024 года про CRISPR при наследственных миопатиях».
→ Запрос точный; фазу 1 пропускаем. `preprint_search(query="CRISPR hereditary myopathy", sources=["biorxiv","medrxiv"], year_from=2024, limit=15)` → дедуп → отбор топ-10 → `work_lookup` каждой → вывод.

**Запрос:** «Хочу разобраться с микробиомом и метаболическим синдромом».
→ Расплывчато; фаза 1 (Decomposition + Tension): 3 подзапроса (gut microbiome composition in MetS / SCFA mechanism / FMT clinical trials). Для каждого — `literature_search`. Дедуп → топ-15 → группировка.

## Ограничения и риски

- arXiv: 1 запрос в 3 секунды. При выборке >20 препринтов предупредить пользователя об ожидании.
- Semantic Scholar без API-ключа = 429; источник будет пропущен. `explain_sources` покажет статус.
- `ncbi_fetch` полный текст режется до 50 КБ — для полнотекстового анализа использовать `open_access_search` (CORE).
