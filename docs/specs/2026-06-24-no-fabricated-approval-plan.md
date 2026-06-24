# No fabricated approval — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Не дать агенту действовать по согласию, которого пользователь не давал — через две промпт-слой правки (anti-fabrication в bounce-сообщении + тюнинг turn-handoff-правила судьи).

**Architecture:** Две независимые правки. (1) В `src/message_policy/no_tool_call_bounce.py` добавить клаузу анти-фабрикации в обе формы сообщения, вынеся её текст в один общий модульный констант (DRY). (2) В `prompts/chat_completions_intent_guard_prompt.md` дописать одну фразу в конец секции `## Interactive turn-handoff`. Код `processing_responses`/`intent_guard`, системный промпт и конфиг — не трогаем.

**Tech Stack:** Python ≥3.10, pytest (conftest кладёт `src/` в `sys.path`), абсолютные импорты от `src/`.

## Global Constraints

- Правки только в двух файлах: `src/message_policy/no_tool_call_bounce.py` (Задача 1) и `prompts/chat_completions_intent_guard_prompt.md` (Задача 2), плюс их тесты.
- НЕ трогать: `prompts/chat_completions_system_prompt.md`, код `src/processing_responses.py` и `src/intent_guard/*`, любой конфиг.
- `turn_ask_human` НЕ мандатим — клауза тул-агностична («ask via the user-input tool **if one is available**, otherwise finalize as blocked»).
- Тексты клауз — **дословно** из спеки `docs/specs/2026-06-24-no-fabricated-approval-design.md` (Компонент 1 / Компонент 2).
- Не удалять и не переписывать существующий текст сообщений/промпта — только добавлять.
- Абсолютные импорты от `src/`. Тесты pytest, прямые `assert`. Запуск — `uv run pytest`.
- Коммит-сообщения: лаконично, по-русски, без имён коллег и без AI-атрибуции.

---

### Task 1: Anti-fabrication клауза в bounce-сообщении (Компонент 1)

**Files:**
- Modify: `src/message_policy/no_tool_call_bounce.py`
- Test: `tests/test_no_tool_call_bounce.py`

**Interfaces:**
- Consumes: существующие `NO_TOOL_CALL_BOUNCE_MESSAGE` (module-level str) и `format_no_tool_call_bounce(assistant_text: str) -> str`.
- Produces: новый module-level констант `_NO_FABRICATION_CLAUSE: str`, дописанный в обе формы сообщения. Сигнатуры функций не меняются.

- [ ] **Step 1: Написать падающий тест**

Дописать в конец `tests/test_no_tool_call_bounce.py` (имена `NO_TOOL_CALL_BOUNCE_MESSAGE` и `format_no_tool_call_bounce` уже импортированы в шапке файла):

```python
def test_bounce_messages_forbid_acting_on_unreceived_reply():
    """Обе формы bounce запрещают действовать по неполученному ответу пользователя."""
    for msg in (
        NO_TOOL_CALL_BOUNCE_MESSAGE,
        format_no_tool_call_bounce("Одобряешь? Напиши 'ок' — начну делать."),
    ):
        assert "their reply has NOT arrived" in msg
        assert "Do not assume, invent, or act on" in msg
        assert "not standing approval" in msg
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `uv run pytest tests/test_no_tool_call_bounce.py::test_bounce_messages_forbid_acting_on_unreceived_reply -q`
Expected: FAIL — маркеров пока нет (`assert "their reply has NOT arrived" in msg` падает).

- [ ] **Step 3: Добавить общий констант клаузы**

В `src/message_policy/no_tool_call_bounce.py` сразу ПЕРЕД определением `NO_TOOL_CALL_BOUNCE_MESSAGE` (после импортов) добавить:

```python
# Anti-fabrication: append to every no-tool-call bounce. The agent must never
# act on a user reply it did not receive (spec 2026-06-24-no-fabricated-approval).
_NO_FABRICATION_CLAUSE = (
    "\n\nIf your prose asked the user a question, requested approval, or awaited "
    "their decision, their reply has NOT arrived — it is not in this conversation. "
    "Do not assume, invent, or act on a user response that has not actually arrived. "
    "If you need their input to proceed, wait for it: ask via the user-input tool if "
    "one is available, otherwise finalize the turn as blocked / needing input. "
    "A prior user choice is not standing approval for a later action."
)
```

- [ ] **Step 4: Дописать клаузу к константе-сообщению**

В том же файле, в определении `NO_TOOL_CALL_BOUNCE_MESSAGE`, заменить хвост:

```python
    "  - If you are not finished, call the tool for your next step — the tool that "
    "asks the user / waits for input (one precise question), or an action tool "
    "(read, search, write, run, etc.)."
)
```

на:

```python
    "  - If you are not finished, call the tool for your next step — the tool that "
    "asks the user / waits for input (one precise question), or an action tool "
    "(read, search, write, run, etc.)."
    + _NO_FABRICATION_CLAUSE
)
```

- [ ] **Step 5: Дописать клаузу к функции**

В функции `format_no_tool_call_bounce`, в `return (...)`, заменить хвост:

```python
        "Reuse the relevant part of the block above VERBATIM where it is the "
        "content the user must react to — do not paraphrase or summarize it."
    )
```

на:

```python
        "Reuse the relevant part of the block above VERBATIM where it is the "
        "content the user must react to — do not paraphrase or summarize it."
        + _NO_FABRICATION_CLAUSE
    )
```

- [ ] **Step 6: Запустить тест — убедиться, что проходит**

Run: `uv run pytest tests/test_no_tool_call_bounce.py::test_bounce_messages_forbid_acting_on_unreceived_reply -q`
Expected: PASS.

- [ ] **Step 7: Регресс по файлу**

Run: `uv run pytest tests/test_no_tool_call_bounce.py -q`
Expected: PASS (новый тест + существующие). Если существующий тест ассертит точный/полный текст сообщения и теперь падает из-за добавленной клаузы — обновить его ожидание под новый текст (НЕ откатывать клаузу) и отметить в отчёте.

- [ ] **Step 8: Commit**

```bash
git add src/message_policy/no_tool_call_bounce.py tests/test_no_tool_call_bounce.py
git commit -m "fix(message-policy): bounce запрещает действовать по неполученному ответу пользователя"
```

---

### Task 2: Тюнинг turn-handoff-правила судьи — approval перед действием (Компонент 2)

**Files:**
- Modify: `prompts/chat_completions_intent_guard_prompt.md`
- Test: `tests/test_intent_guard_final_report_consistency.py`

**Interfaces:**
- Consumes: `intent_guard.final_report_consistency._load_guard_prompt() -> str` (без изменений сигнатуры).
- Produces: ничего для других задач (задачи независимы).

- [ ] **Step 1: Написать падающий тест**

Дописать в конец `tests/test_intent_guard_final_report_consistency.py` (модульная pytest-функция собирается наравне с unittest-классами; рядом уже есть аналогичный `test_guard_prompt_contains_turn_handoff_rule`):

```python
def test_guard_prompt_accepts_confirmation_before_action():
    """Судья знает: запрос одобрения перед действием — легитимный hand-off, не stalling."""
    from intent_guard.final_report_consistency import _load_guard_prompt

    prompt = _load_guard_prompt()
    assert "explicit confirmation or approval before a consequential" in prompt
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `uv run pytest tests/test_intent_guard_final_report_consistency.py::test_guard_prompt_accepts_confirmation_before_action -q`
Expected: FAIL — фразы в промпте пока нет.

- [ ] **Step 3: Вставить фразу в промпт**

В `prompts/chat_completions_intent_guard_prompt.md` найти точную пару (конец секции turn-handoff → заголовок Trace digest):

`old_string`:
```text
Keep it **narrow** — a normal Axis A gap (not a hand-off) when the agent could proceed on its own, the question is vacuous or a near-repeat of an earlier one (stalling), or intent required concrete actions (write / run / fetch / …) absent or failed in the trace.

## Trace digest — how to read it
```

`new_string`:
```text
Keep it **narrow** — a normal Axis A gap (not a hand-off) when the agent could proceed on its own, the question is vacuous or a near-repeat of an earlier one (stalling), or intent required concrete actions (write / run / fetch / …) absent or failed in the trace.

A request for explicit confirmation or approval **before** a consequential, irreversible, or plan-committing action is itself a legitimate hand-off — not stalling — even if the user already made a related choice earlier.

## Trace digest — how to read it
```

(Вставка одной строки-абзаца перед `## Trace digest`; существующий текст не меняется.)

- [ ] **Step 4: Запустить тест — убедиться, что проходит**

Run: `uv run pytest tests/test_intent_guard_final_report_consistency.py::test_guard_prompt_accepts_confirmation_before_action -q`
Expected: PASS.

- [ ] **Step 5: Регресс — intent_guard / finish-gate**

Run: `uv run pytest tests/test_intent_guard_final_report_consistency.py tests/test_finish_gate_guard.py tests/test_finish_gate_two_axis.py tests/test_text_answer_judge.py tests/test_bundled_prompts_resolved.py -q`
Expected: PASS (новый тест зелёный, существующие без регрессий). Если тест ассертит точный объём/хэш промпта — обновить ассерт под новый текст (НЕ откатывать вставку).

- [ ] **Step 6: Commit**

```bash
git add prompts/chat_completions_intent_guard_prompt.md tests/test_intent_guard_final_report_consistency.py
git commit -m "fix(intent-guard): запрос одобрения перед действием — легитимный hand-off, не stalling"
```

---

## Self-Review

**1. Spec coverage:**
- Компонент 1 (клауза в обе формы bounce, DRY-констант, дословный текст) → Task 1, Steps 3–5. ✓
- Компонент 1 тест (маркеры `their reply has NOT arrived`, `Do not assume, invent, or act on`, `not standing approval`) → Task 1, Step 1. ✓
- Компонент 2 (одна фраза в конец turn-handoff, дословно, без хвоста про standing approval) → Task 2, Step 3. ✓
- Компонент 2 тест (маркер `explicit confirmation or approval before a consequential`) → Task 2, Step 1. ✓
- «Системный промпт / код / конфиг не трогать», «turn_ask_human не мандатим» → Global Constraints + файлы задач. ✓
- Регресс (bounce / intent_guard / finish-gate) → Task 1 Step 7, Task 2 Step 5. ✓
- Поведенческая (живой реплей 31a0a127) — ручная по спеке, не часть детерминированного плана. ✓

**2. Placeholder scan:** Плейсхолдеров нет — весь код и тексты приведены дословно, команды и ожидаемые результаты конкретны. ✓

**3. Type consistency:** `_NO_FABRICATION_CLAUSE: str` определён до использования и применён к обеим формам; `_load_guard_prompt() -> str` используется без изменений сигнатуры. Имена тестов уникальны и не конфликтуют с существующими. ✓
