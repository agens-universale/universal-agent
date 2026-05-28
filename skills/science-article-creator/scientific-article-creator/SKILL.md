---
id: scientific-article-creator
name: Scientific Article Creator
description: "Authors a Russian Markdown SCIENTIFIC article / literature review with VERIFIED citations. Structure: contribution → main figure idea → abstract → intro → methods → results/synthesis → related work → limitations. Every claim is backed by a paper resolved through science-mcp (literature_search + work_lookup); the bibliography block contains normalized entries with DOI/PMID/arXiv IDs. Never cites from model memory — unverifiable claims are tagged `[CITATION NEEDED]` instead. Use when the user asks to write a scientific article, paper, review, or topical synthesis for publication or formal use. Do NOT use for casual articles without citation-verification requirement — use `articles:article-creator` instead. Do NOT use to verify an existing article — use `scientific-article-checker`."
version: 0.1.0
---

# Skill: Scientific Article Creator

## Когда использовать

- Триггеры: «напиши научный обзор / статью по теме X», «литературный обзор для публикации», «paper-style review», «оформи это в научную статью».
- НЕ использовать: казуальная статья без проверки источников → `articles:article-creator`; проверка готовой статьи → `scientific-article-checker`; черновик идеи без цитат → `scientific-research`.

## Что делает

Создаёт научный текст 1500–5000 слов на русском, в котором каждое нетривиальное утверждение подкреплено реальной статьёй с проверенным идентификатором.

## Входные данные

- Тема, объём, опционально аудитория (специалисты / широкая).
- Опционально: research artifact от `research-artifact-compiler` или список источников от `scientific-research`.

## Алгоритм

### Шаг 1 — план статьи (skeleton)

1. Сформулировать одну центральную contribution / тезис в одну фразу.
2. Описать «Figure 1» одним абзацем — какая ключевая иллюстрация была бы (даже если не рисуем).
3. Набросать заголовки секций: abstract, introduction, methods/approach, results/synthesis, related work, limitations, conclusion.

### Шаг 2 — сбор литературы

Если на входе нет готового списка:
- Вызвать (или делегировать в `scientific-research`) — `science-literature_search` по 2–4 запросам по теме, собрать 15–30 кандидатов.
- Дополнить `science-preprint_search` для горячих направлений.
- Каждую отобранную работу пропустить через `science-work_lookup` — гарантировать нормализованный `WorkRecord`.
- Отобрать 8–15 ключевых работ для использования в тексте.

### Шаг 3 — draft с инлайн-цитатами

- Писать секции по плану. Каждое утверждение, содержащее цифры, имена, годы, конкретный механизм, метод или вывод — обязано иметь цитату вида `[N]`, где N — индекс в bibliography-блоке.
- Утверждения «общего знания» (например, «ДНК состоит из четырёх нуклеотидов») допустимы без цитаты.
- Если по теме найти подтверждение не удалось — `[CITATION NEEDED]` с короткой пометкой «искал X, Y — не нашёл».

### Шаг 4 — bibliography-блок

В конце статьи — секция `## Литература` с пронумерованным списком. Формат записи:

```
[N] Authors. Title. Journal/Venue, Year. DOI:<…> | PMID:<…> | arXiv:<…>. URL: <…>
```

Все идентификаторы — из ответов MCP, никогда не из памяти модели.

### Шаг 5 — citation strength check

Для критичных утверждений (центральная гипотеза, ключевые цифры) — вызвать `science-citations_by_doi(doi=…, count_only=True)`. Если работа имеет 0 цитат при возрасте >2 лет — пометить как «weak evidence» в тексте или искать дополнительное подтверждение.

### Шаг 6 — финальный проход

- Прочитать draft, убрать дубли, гладкая русская проза.
- Проверить, что нет `[CITATION NEEDED]` без явного комментария.
- Сохранить в `tmp/science-smoke/<task>/article.md` или в указанную пользователем директорию.

## MCP-тулы

- `science-literature_search`, `science-preprint_search` — поиск.
- `science-work_lookup(identifier)` — нормализация перед добавлением в bib.
- `science-citations_by_doi(doi, count_only=true)` — оценка веса.
- `science-open_access_search` — если нужен full-text для глубокого вытаскивания.

## Формат вывода

Готовый Markdown-файл:

```markdown
# <Заголовок статьи>

> Contribution: <одно предложение>

## Abstract
<150–250 слов>

## Введение
<2–4 абзаца, заканчивая четкой постановкой>

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

## Что НЕ делать

- НЕ генерировать BibTeX / ссылки из памяти модели.
- НЕ оставлять `[CITATION NEEDED]` без пояснения.
- НЕ использовать веб-поиск как источник цитат — только science-mcp.
- НЕ дублировать функционал `scientific-article-checker` — проверка делается отдельным шагом по запросу.

## Примеры

**Запрос:** «Напиши обзор на 3000 слов про связь микробиома кишечника и метаболического синдрома».
→ Шаг 1: contribution = «синтез доказательств причинной связи микробиом → MetS через SCFA и эндотоксемию». Figure 1 = диаграмма путей. Шаги 2–6 по плану. Bibliography ≈ 12–15 работ, все с DOI/PMID.

**Запрос:** «Из этого артефакта сделай статью».
→ Источники уже нормализованы compiler'ом. Пропустить шаг 2, идти сразу к 3.

## Ограничения

- Запросы на полный full-text часто требуют `open_access_search` (CORE) или PMC; PubMed XML режется на 50 КБ.
- Длинный обзор (>4000 слов) с >20 источниками может потребовать пары прогонов поиска — это нормально.
