---
id: research-artifact-compiler
name: Research Artifact Compiler
description: "Compiles free-form research input (notes, conversation excerpts, experiment logs, PDF excerpts, source lists) into a STRUCTURED research artifact with four layers: Logic (claims with falsifiability criteria, formal concepts, related work), Physical (configs, env stubs), Exploration (research DAG in YAML), Evidence (numerical results with provenance). Every claim is bound to one or more experiment IDs (E01, E02, …); evidence layer holds the raw numbers — never duplicated inside experiment plans. Citations resolved through science-mcp work_lookup. Intended as an intermediate format consumed by `scientific-article-creator` and `scientific-article-checker`. Use when the user has raw scientific material to organize before writing or reviewing. Do NOT use for casual note-taking — only for material with claims and evidence."
version: 0.1.0
---

# Skill: Research Artifact Compiler

## Когда использовать

- Триггеры: «собери из этих заметок research artifact», «структурируй мои результаты», «преобразуй PDF в claims+evidence», «подготовь материал для статьи».
- НЕ использовать: казуальные заметки без научного содержания; уже структурированный артефакт (просто использовать как вход).

## Что делает

Преобразует разрозненный материал в каноничную директорию с 4 слоями, где каждое утверждение явно привязано к эксперименту или источнику.

## Входные данные

- Сырой текст / путь к файлу / набор файлов (заметки, логи, выдержки из статей).
- Опционально — топик / гипотеза, к которой материал относится.

## Алгоритм

### Шаг 1 — извлечение

Сканировать вход, отметить:
- Утверждения (claims) — предложения с фактическим содержанием.
- Эксперименты / наблюдения / измерения — описания методики и того, что измеряли.
- Численные результаты с указанием источника.
- Внешние ссылки (DOI/PMID/arXiv) — собирать отдельно.

### Шаг 2 — нормализация цитат

Каждый встретившийся идентификатор пропустить через `science-work_lookup`. Сохранить `WorkRecord`. Не-резолвящиеся пометить и сообщить пользователю.

### Шаг 3 — формирование 4 слоёв

Создать директорию `<output_dir>/artifact/`:

```
artifact/
├── PAPER.md              # одно-страничный конспект: что исследуем и почему
├── logic/
│   ├── claims.md         # список утверждений, каждый с criteria и evidence-refs
│   ├── concepts.md       # формальные определения / переменные
│   └── related_work.md   # из нормализованных WorkRecord
├── physical/
│   ├── hyperparams.yaml  # если применимо
│   └── environment.md    # окружение / setup
├── exploration_tree.yaml # research DAG (≥8 узлов)
└── evidence/
    ├── experiments.md    # списки экспериментов E01, E02, ... — план + методика, БЕЗ точных чисел
    ├── results.md        # точные численные результаты с привязкой к E01, E02, ...
    └── sources.json      # массив WorkRecord'ов (с DOI/PMID/arXiv)
```

### Шаг 4 — claims-evidence binding

Каждый claim в `logic/claims.md` имеет:
- ID (C01, C02, …).
- Текст утверждения.
- `evidence_refs: [E01, E03]` — на какие эксперименты опирается.
- `criteria` — при каком наблюдении claim считается опровергнутым.
- Если evidence-ref'ов нет — статус `unsupported` (заметно красным/тегом).

Правило: формулировка claim'а ограничена тем, что доказывает evidence. Валидационная метрика → claim про валидационную метрику. Не «метод улучшает X в проде», если в evidence только тестовый набор.

### Шаг 5 — exploration_tree.yaml

Минимум 8 узлов вида:

```yaml
nodes:
  - id: N01
    type: question | hypothesis | experiment | dead_end | pivot | decision | observation | conclusion
    title: "..."
    parent: null | <id>
    notes: "..."
edges:
  - from: N01
    to: N02
    relation: "leads_to | rejects | refines | branches"
```

Включать тупики и пивоты честно, не только успешный путь.

### Шаг 6 — валидация

Прогнать самопроверку:
- Каждый claim либо имеет ≥1 `evidence_refs`, либо помечен `unsupported`.
- Каждый experiment ID в claim существует в `experiments.md`.
- В `experiments.md` нет точных чисел (только дизайн); точные числа в `results.md`.
- exploration_tree ≥8 узлов.
- Все `sources.json` записи имеют canonical id.

Отчёт о проверке вывести пользователю.

## MCP-тулы

- `science-work_lookup(identifier)` — нормализация цитат.
- `science-citations_by_doi(doi, count_only=true)` — опционально, для оценки веса источника в `related_work.md`.

## Формат вывода

Структура директории выше + краткое summary в чат:
```
artifact compiled at <path>
- claims: <N> (supported: <M>, unsupported: <K>)
- experiments: <N>
- exploration_tree nodes: <N>
- sources resolved: <N> / <total>
```

## Что НЕ делать

- НЕ выдумывать experiment'ы / результаты, которых нет во входе. Если данных нет — отметить gap и попросить пользователя дополнить.
- НЕ копировать точные числа в experiment plans — только в evidence/results.md.
- НЕ писать готовую статью — артефакт это вход для `scientific-article-creator`.

## Примеры

**Запрос:** «Из этих заметок (3 КБ) собери artifact».
→ Шаги 1–6. Если из текста извлечено 5 claims и только 2 experiment'а — exploration_tree всё равно ≥8 узлов (включая вопросы и пивоты, не только эксперименты).

**Запрос:** «У меня есть только список 10 статей, никаких своих экспериментов».
→ Артефакт в режиме «обзор»: `claims.md` отражает claim'ы из чужих статей, `evidence_refs` указывают на номера в `sources.json` через E01, E02 как «external evidence», exploration_tree описывает структуру области.

## Ограничения

- Если вход требует full-text работ, а они доступны только в PubMed — `ncbi_fetch` режет на 50 КБ. Использовать суммаризацию или `open_access_search`.
- Для очень больших входов (>50 КБ) лучше делать в несколько проходов и склеивать.
