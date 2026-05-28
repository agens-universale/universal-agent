---
id: science-orchestrator
name: Science Orchestrator
description: "Two-loop orchestration of a full SCIENTIFIC research lifecycle. Inner loop: hypothesis → choose sources → run / synthesize a step → record → learn (minutes to days). Outer loop: periodically step back, ask 'what do these results mean?', synthesize patterns, decide deepen / broaden / pivot / conclude. Routes to inner skills as needed: scientific-research (find papers), research-artifact-compiler (structure results), scientific-article-creator (final write-up), scientific-article-checker (self-review). Use when the user requests a multi-step scientific investigation from a research question to a final report. Different from `complex-task-solver` (generic orchestration with side effects) — this is research-specific with evidence-grade gates and a synthesis loop."
version: 0.1.0
---

# Skill: Science Orchestrator

## Когда использовать

- Триггеры: «проведи полное мини-исследование», «от гипотезы до отчёта», «разберись с темой X и сделай вывод», «исследуй и напиши отчёт», «спланируй и проведи научное расследование».
- НЕ использовать: разовый поиск статей → `scientific-research`; готовый запрос на статью → `scientific-article-creator`; общая многошаговая задача без научного контекста → `complex-task-solver`.

## Что делает

Управляет длинной исследовательской сессией с двумя контурами: быстрая итерация (inner) и периодический синтез (outer). На выходе — артефакт + финальный отчёт со ссылками.

## Граница ответственности (MANDATORY)

Orchestrator **координирует**, а не исполняет. Запрещено делать своими руками то, что закрыто inner-скиллами:

- Поиск литературы → **только** через `skills_load("science-article-creator:scientific-research")`, не напрямую через `science-literature_search` / `science-preprint_search`.
- Структурирование результатов → **только** через `skills_load("science-article-creator:research-artifact-compiler")`.
- Написание отчёта → **только** через `skills_load("science-article-creator:scientific-article-creator")`.
- Self-review итогового документа → **только** через `skills_load("science-article-creator:scientific-article-checker")`.

Прямые вызовы `science-*` MCP-тулов orchestrator'ом допустимы **только** в двух случаях:
1. Pre-flight `science-explain_sources` один раз в начале сессии.
2. Точечный `science-work_lookup`, когда нужно резолвить один identifier, упомянутый пользователем в постановке (не результат поиска — это работа inner-скиллов).

Если по логу видно, что orchestrator делает много прямых поисков и резолвов — он не делегирует. Это failure исполнения, а не «эффективность». Перезапустить с явным напоминанием о делегировании.

## Входные данные

- Тема / research question.
- Опционально: целевой формат отчёта (обзор / краткое заключение / решение), временной горизонт (1–3 итерации / больше).

## Алгоритм

### Bootstrap

1. Сформулировать центральный research question (если расплывчат — делегировать в `scientific-research` для применения линз).
2. Сформулировать 2–4 фальсифицируемые гипотезы.
3. Создать рабочую папку `tmp/science-smoke/<session-id>/` с подкаталогом `iterations/`.

### Inner loop (повторять)

Один проход = одна итерация в `iterations/<NN>/`:

1. **Pick** — выбрать гипотезу или подвопрос для итерации.
2. **Lock protocol** — записать в `iterations/<NN>/plan.md` что именно делаем и какой результат ожидаем ДО действия. Это git-style proof против rationalization задним числом.
3. **Execute** — действия только через делегирование (см. «Граница ответственности» выше):
   - Поиск → `skills_load("science-article-creator:scientific-research")` с подзадачей итерации.
   - Полнотекстовый разбор найденных работ → внутри `scientific-research` или `scientific-article-checker`, не из orchestrator'а.
   - Резолв identifier'ов из результатов поиска — работа `scientific-research` (он уже делает `work_lookup` для нормализации в `WorkRecord`).
4. **Measure** — записать в `iterations/<NN>/observations.md`: что нашли, что подтверждает / опровергает гипотезы, какие пробелы.
5. **Record** — добавить relevant claims/evidence в общий накапливаемый артефакт через `research-artifact-compiler` (incremental режим).

После каждой 2–3 итераций — выходить в outer loop.

### Outer loop (синтез)

1. **Step back** — прочитать `iterations/*/observations.md` целиком.
2. **Pattern recognition** — какие закономерности видны, какие гипотезы укрепились, какие отброшены, что неожиданно.
3. **Decision** — выбрать одно:
   - **Deepen** — углубиться в одну подобласть, новая итерация по ней.
   - **Broaden** — расширить запрос на смежные области.
   - **Pivot** — текущая ветка не даёт ответа, переформулировать гипотезы.
   - **Conclude** — данных достаточно, переходим к финальному отчёту.
4. **Update plan** — записать решение в `master_plan.md` с обоснованием.

### Финал

Когда outer loop сказал `Conclude`:

1. Зафиксировать артефакт через `research-artifact-compiler` (финальная сборка из всех iterations).
2. Делегировать в `scientific-article-creator` — финальный текст на основе артефакта.
3. Опционально — `scientific-article-checker` для самопроверки сгенерированного отчёта.
4. Положить готовый MD в `<session-dir>/final_report.md`.

## MCP-тулы

Orchestrator сам вызывает только:
- `science-explain_sources` — pre-flight один раз в начале сессии.
- `science-work_lookup` — точечно, если пользователь упомянул identifier в постановке (не из результатов поиска).

Всё остальное (`literature_search`, `preprint_search`, `open_access_search`, `citations_by_doi`, `ncbi_fetch`) — **через делегирование** в inner-скиллы, которые уже знают, как и когда их вызывать.

## Формат вывода

Файлы в `<session-dir>/`:
```
master_plan.md          # план + outer-loop решения с обоснованием
iterations/
  01/plan.md
  01/observations.md
  02/plan.md
  ...
artifact/               # финальный research artifact
final_report.md         # итоговая статья / отчёт
review.md               # опционально, от scientific-article-checker
```

## Что НЕ делать

- НЕ совмещать inner и outer loop в одну фазу — outer loop работает только после ≥2 inner итераций.
- НЕ переписывать prior observations.md ретроактивно. Если ошиблись — новая запись с пометкой.
- НЕ выдумывать gates («достаточно данных») без явного критерия в master_plan.
- НЕ повторять функционал inner skills — делегировать через `skills_load`. Прямой вызов `science-literature_search`, `science-preprint_search`, `science-citations_by_doi` из orchestrator'а — bug, не feature.
- НЕ писать финальный отчёт самостоятельно. Финал — это `scientific-article-creator` на основе собранного артефакта.

## Примеры

**Запрос:** «Проведи мини-исследование: эффективность тирзепатида в снижении сердечно-сосудистых рисков. 2 итерации, финальный отчёт».
→ Bootstrap: research_question + 3 гипотезы (снижение MACE / снижение HF hospitalizations / снижение mortality).
→ Inner #1: гипотеза HF — `literature_search "tirzepatide heart failure"` + `work_lookup` ключевых RCT.
→ Inner #2: гипотеза MACE — `literature_search "tirzepatide cardiovascular outcomes"` + `citations_by_doi` главного RCT.
→ Outer: данных по MACE мало (1 ongoing trial), по HF — есть положительные. Decision: `Conclude`, отметить что MACE-данных недостаточно.
→ Финал: artifact + `final_report.md` через `scientific-article-creator`.

**Запрос:** «Разберись с микробиомом и метаболическим синдромом, итерируй пока не получишь чёткий ответ».
→ Inner loop циклится до выхода outer-loop `Conclude`, обычно 3–5 итераций.

## Ограничения

- arXiv rate limit (1 req/3s) + Semantic Scholar без API-key могут удлинить итерации.
- Для длинных сессий запускать science-mcp как HTTP daemon (`127.0.0.1:8765/mcp`) — stdio теряет throttler-state между вызовами.
- Если outer loop не может выйти на `Conclude` за >5 итераций — остановиться, явно сообщить пользователю и попросить переформулировать вопрос.
