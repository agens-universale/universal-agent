# completion_report template for skill-creator self-check Step 5

The agent loads this asset through
`skills_read_asset(skill_id='skill-creator', slug='completion_report_template')`
in Step 5 of the self-check flow. Fill in **all 5 sections** below and
save the result to `<workspace>/completion_report.md`.

If a section genuinely has nothing to report, write `Нет.` and move on.
**Do NOT write empty reports** such as “всё хорошо, готов к
распространению”. Half a page of template prose without specifics
is useless.

---

## Template structure

```markdown
# Completion Report — <skill-id>

**Финальная версия:** v0.1.X
**Дата завершения:** YYYY-MM-DD
**Итераций self-check:** N (text-review × 1 + hybrid review × 1
[+ final run × 2 если был fix])

## Что сделано

<1-2 предложения: что скилл делает, ключевые фичи.>

## Эволюция версий

- **v0.1.0** (draft): начальная версия с базовым алгоритмом.
- **v0.1.1** (после text-review): конкретные правки структурного
  плана (1-2 строки на фикс). Например:
  - добавил syllable validation шаг;
  - убрал штампы из примеров;
  - переписал триггеры явно.
- **v0.1.2** (после hybrid review + final run): конкретные правки
  по output-quality. Если фиксов после hybrid не потребовалось —
  пиши «фиксов не потребовалось, outputs Step 2 финальные».

## Accepted limitations

<Вещи которые НЕ идеальны, но сознательно оставлены — это известные
ограничения модели или scope-decision. Например:
"Точный подсчёт русских слогов — модель часто ошибается на 1; не
фиксим в SKILL.md, полагаемся на алгоритм пересчёта в выходе."
Если ничего такого нет — пиши «Нет.».>

## Открытые вопросы для будущих итераций

<Что можно улучшить, но не блокер сейчас. TODO-лист для будущих
сессий. Например:
- Добавить поддержку английского как опции.
- Расширить anti-cliché правило примерами before/after.
Если ничего — пиши «Нет.».>

## Подтверждение готовности

- [x] Все артефакты в `_dev/` на месте.
- [x] Final run выполнен на финальной версии (если был fix в Step 3),
      ИЛИ outputs из Step 2 финальные (если фиксов после hybrid не было).
- [x] Outputs соответствуют shipped SKILL.md.
```

---

## Filling rules (important)

1. **Эволюция версий** — do NOT fall back on generic wording. Take
   the specific list of fixes from the session context (subagent review
   reports via task_report, or your own files if saved in
   `text_review_report.md` / `self_check_report.md`).
   Be specific (“добавил syllable validation шаг”, “убрал
   штамп `капли стучат` из примеров”), not “исправил пробелы”.

2. **Accepted limitations** — this section is critical for future
   **improve-existing** work. If the model does something poorly and you
   accept it as a known limitation rather than fixing it in SKILL.md,
   you must record it. Otherwise, the next session will waste an iteration
   on the same problem.

3. **Открытые вопросы** — keep it brief, without explanations. Use a
   TODO list for future iterations. If the content is fine, honestly write `Нет.`.

4. **Do NOT write empty reports** such as “всё хорошо, готов к распространению”.
   If a section genuinely has nothing to report, write `Нет.` and move on.
   Half a page of template prose without specifics is useless.
