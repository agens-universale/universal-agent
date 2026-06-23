# Interactive turn-handoff — finish-gate judge fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Научить finish-gate-судью принимать легитимную «передачу хода пользователю» как валидное завершение хода, чтобы интерактивные диалоги (викторина и т.п.) не зацикливались.

**Architecture:** Чисто промптовая правка — вставить одну секцию `## Interactive turn-handoff (Axis A clarification)` в общий системный промпт судьи `prompts/chat_completions_intent_guard_prompt.md` (между `## Scope` и `## Trace digest — how to read it`). Промпт грузит `_load_guard_prompt()` в `intent_guard/final_report_consistency.py`; его используют оба потребителя (прозо-судья и настоящий finish-gate) — одна правка чинит оба. Код и конфиг не трогаем.

**Tech Stack:** Python ≥3.10, pytest (conftest кладёт `src/` в `sys.path`), абсолютные импорты от `src/`.

## Global Constraints

- Правка только ассета `prompts/chat_completions_intent_guard_prompt.md`. Код (`intent_guard/*`, `processing_responses.py`) и конфиг НЕ трогать.
- Вставляемый блок — **дословно** из спеки; ничего из существующего промпта (Scope, Axis A/B, матрица решений, схема вывода) не удалять и не переписывать.
- Точка вставки — строго между секцией `## Scope` и `## Trace digest — how to read it`.
- Абсолютные импорты от `src/` (`from intent_guard.final_report_consistency import ...`).
- Все тесты — pytest; прямые читаемые `assert`.
- Коммит-сообщения — лаконичные, по-русски, без имён коллег и без AI-атрибуции.

---

### Task 1: Вставить правило turn-handoff в промпт судьи + тест загружаемости

**Files:**
- Modify: `prompts/chat_completions_intent_guard_prompt.md` (вставка между `## Scope` и `## Trace digest — how to read it`)
- Test: `tests/test_intent_guard_final_report_consistency.py` (добавить функцию-тест в конец файла)

**Interfaces:**
- Consumes: `intent_guard.final_report_consistency._load_guard_prompt() -> str` — читает и возвращает текст промпта судьи (существует, без изменений сигнатуры).
- Produces: ничего нового для других задач (план из одной задачи).

- [ ] **Step 1: Написать падающий тест**

Добавить в конец `tests/test_intent_guard_final_report_consistency.py` (после существующего класса; модульная pytest-функция собирается наравне с unittest-классами):

```python
def test_guard_prompt_contains_turn_handoff_rule():
    """Промпт судьи содержит правило turn-handoff (доставка ассета + резолв
    bundled_root для editable и wheel)."""
    from intent_guard.final_report_consistency import _load_guard_prompt

    prompt = _load_guard_prompt()
    assert "## Interactive turn-handoff (Axis A clarification)" in prompt
    assert "hands the turn back to the user" in prompt
    assert "set `trace_ok=true`" in prompt
    assert "near-repeat of an earlier one" in prompt
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `uv run pytest tests/test_intent_guard_final_report_consistency.py::test_guard_prompt_contains_turn_handoff_rule -q`
Expected: FAIL — маркеры отсутствуют (секции в промпте пока нет; `assert "## Interactive turn-handoff (Axis A clarification)" in prompt` падает).

- [ ] **Step 3: Вставить секцию в промпт**

В `prompts/chat_completions_intent_guard_prompt.md` найти точную пару строк (конец `## Scope` → заголовок `## Trace digest`):

`old_string`:
```text
- Work beyond intent (extra files/tools) is **not** an Axis A gap if intent is already structurally met — handle on Axis B (`fix_report`), not `continue_work`.

## Trace digest — how to read it
```

`new_string`:
```text
- Work beyond intent (extra files/tools) is **not** an Axis A gap if intent is already structurally met — handle on Axis B (`fix_report`), not `continue_work`.

## Interactive turn-handoff (Axis A clarification)

Some open turns are **conversational**: their correct completion is a message that hands the turn back to the user, awaiting input the task genuinely needs next (an answer, choice, or clarification only the user can give). For these, that responsive message **is** the turn's required work — on **Axis A** treat the requirement as met by the message itself and set `trace_ok=true`, without demanding a separate tool/action. **Axis B is unchanged:** any claim of an action the visible trace does not show is still a `report_mismatches[]` contradiction.

Keep it **narrow** — a normal Axis A gap (not a hand-off) when the agent could proceed on its own, the question is vacuous or a near-repeat of an earlier one (stalling), or intent required concrete actions (write / run / fetch / …) absent or failed in the trace.

## Trace digest — how to read it
```

(Это вставка одной секции; строка-якорь `## Trace digest — how to read it` остаётся, перед ней появляется новый блок. Существующий текст не меняется.)

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

Run: `uv run pytest tests/test_intent_guard_final_report_consistency.py::test_guard_prompt_contains_turn_handoff_rule -q`
Expected: PASS.

- [ ] **Step 5: Регресс — прогон intent_guard / finish-gate тестов**

Run: `uv run pytest tests/test_intent_guard_final_report_consistency.py tests/test_finish_gate_guard.py tests/test_finish_gate_two_axis.py tests/test_finish_gate_iteration_limit.py tests/test_finish_gate_output_patch.py tests/test_text_answer_judge.py tests/test_intent_guard_validation_parse.py tests/test_bundled_prompts_resolved.py -q`
Expected: PASS (новый тест зелёный, существующие без регрессий). Если какой-то тест ассертит точный объём/хэш промпта — это ожидаемое следствие добавления секции: обновить ассерт под новый текст (не откатывать вставку).

- [ ] **Step 6: Commit**

```bash
git add prompts/chat_completions_intent_guard_prompt.md tests/test_intent_guard_final_report_consistency.py
git commit -m "fix(intent-guard): судья принимает легитимную передачу хода пользователю (anti-loop)"
```

---

## Self-Review

**1. Spec coverage:**
- «Правка #1: вставить секцию между Scope и Trace digest, дословно, ничего не удаляя» → Task 1, Step 3 (точные `old_string`/`new_string`). ✓
- «Код intent_guard/processing_responses и конфиг не трогать» → Global Constraints + файлы задачи (только промпт + тест). ✓
- «Тест на загружаемость: `_load_guard_prompt()` содержит маркеры» → Task 1, Step 1 (маркеры: `Interactive turn-handoff`, `hands the turn back to the user`, `trace_ok=true`, `near-repeat`). ✓
- «Прогон существующих intent_guard/finish-gate тестов на регресс» → Task 1, Step 5. ✓
- Охват режимов (всё chat_completions; прозо-приём TUI-gated; без TUI-only ветки) — следствие правки общего промпта; отдельной задачи не требует (ничего больше не меняем). ✓
- #2 — вне scope (спека) → в плане отсутствует намеренно. ✓

**2. Placeholder scan:** Плейсхолдеров нет — тест и вставка приведены дословно, команды и ожидаемые результаты конкретны. ✓

**3. Type consistency:** Используется существующая `_load_guard_prompt() -> str` без изменения сигнатуры; новых типов/функций задача не вводит. ✓
