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
3. **Execute** — действия:
   - Поиск: вызывать `science-literature_search` / `preprint_search` / `open_access_search` (или делегировать в `scientific-research`).
   - Чтение: `science-ncbi_fetch` / `open_access_search` для нужных полных текстов.
   - Резолв: `science-work_lookup` для каждого найденного DOI/PMID/arXiv.
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

Все 7 science-mcp + делегирование в inner skills через `skills_load`:
- `science-explain_sources` — pre-flight.
- `science-literature_search`, `science-preprint_search`, `science-open_access_search` — поиск.
- `science-work_lookup` — нормализация идентификаторов.
- `science-citations_by_doi` — вес источников.
- `science-ncbi_fetch` — полнотекст PubMed.

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
- НЕ повторять функционал inner skills — делегировать через skills_load.

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
