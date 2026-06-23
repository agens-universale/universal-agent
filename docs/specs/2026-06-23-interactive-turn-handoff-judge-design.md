# Interactive turn-handoff — finish-gate judge fix — Design

**Дата:** 2026-06-23
**Статус:** согласовано, готово к плану
**Ветка:** feat/agent-instructions-and-gc-guard

## Цель

Устранить зацикливание агента в интерактивных диалогах (пример: сессия `8c576f7b`,
игра «Кто хочет стать миллионером»), когда корректное завершение хода — это
сообщение/вопрос, передающий ход пользователю. Сейчас finish-gate-судья бракует
такой ход как преждевременный → bounce → петля.

Правка — **одна, чисто промптовая**: добавить в системный промпт судьи правило
о легитимной «передаче хода пользователю». Кода не трогаем.

---

## Проблема (анализ сессии 8c576f7b)

Тред 8c576f7b, 24 записи, один ход после ответа игрока зациклился (записи 17–24):

```
[17] turn_intermediate_reply (вопрос игроку) → ok
[19] assistant <think>+проза «жду ответа», БЕЗ tool-call → bounce «no tool call»
[22] turn_intermediate_reply (тот же вопрос) → ok
[24] assistant <think>+проза, БЕЗ tool-call → bounce → …
```

Петля крутится до `max_steps`, повторяя тот же вопрос.

### Почему существующий adaptive turn-contract не спас

Мы ранее добавили «adaptive turn-contract»: приём прозы как ответа хода + LLM-судья
(`text_answer_mode`, переиспользует finish-gate). В этой сессии он **был включён и
сработал**, но «не туда»:

- TUI ставит `text_answer_mode = "judge"` (`config_modules/tui.py:14`, `tui/app.py:99`).
- `strict_turn_finish_validation = True` (`config_modules/agent.py:26`) + backend
  `chat_completions` → `FinishGateGuard.enabled = True` (`intent_guard/guard.py:85`).
- Значит прозо-судья реально вызывался на прозу (записи 19, 24) и **забраковал** её.

Корень: прозо-судья переиспользует **finish-gate-судью**
(`intent_guard/text_answer_judge.py:30` → `validate_final_report_vs_user_intent`).
Его вопрос — *«закрывает ли текст интент пользователя (задача выполнена)?»*. В
интерактивной игре ход «вот вопрос, твой ход» **не закрывает** интент «сыграть всю
игру» → судья по своей task-completion-логике говорит «не доделал» → bounce. Судья
честно делает свою работу, но у него **неправильный вопрос** для пошагового
диалога: он умеет «задача завершена?», а нужно ещё «это легитимная передача хода
пользователю?».

То есть фича работает для one-shot («2+2» → «4» → accept), но ломается на любом
интерактиве (викторина, уточняющий вопрос, выбор из вариантов, разговорный
туда-сюда), где ход штатно уступает ход пользователю в середине задачи.

---

## Решение (#1) — правило turn-handoff в промпте судьи

### Где

Единственное место — системный промпт `prompts/chat_completions_intent_guard_prompt.md`
(грузится `_load_guard_prompt()` в `intent_guard/final_report_consistency.py:50`). Его
используют **оба** потребителя через `validate_final_report_vs_user_intent`:
прозо-судья и настоящий finish-gate. Одна правка чинит оба (DRY).

Промпт намеренно игнорирует имена тулов («Ignore any tool registry names; only the
JSON fields matter») → правило формулируем **семантически** (контент уступает ход),
без привязки к `turn_intermediate_reply`/`turn_finish`.

### Что (точная вставка)

Новая секция между `## Scope` и `## Trace digest — how to read it`. **Ничего не
удаляется и не переписывается — только вставляется** (Axis A/B, матрица, схема
вывода не трогаются):

```text
## Interactive turn-handoff (Axis A clarification)

Some open turns are **conversational**: their correct completion is a message that hands the turn back to the user, awaiting input the task genuinely needs next (an answer, choice, or clarification only the user can give). For these, that responsive message **is** the turn's required work — on **Axis A** treat the requirement as met by the message itself and set `trace_ok=true`, without demanding a separate tool/action. **Axis B is unchanged:** any claim of an action the visible trace does not show is still a `report_mismatches[]` contradiction.

Keep it **narrow** — a normal Axis A gap (not a hand-off) when the agent could proceed on its own, the question is vacuous or a near-repeat of an earlier one (stalling), or intent required concrete actions (write / run / fetch / …) absent or failed in the trace.
```

### Почему так (точки точности)

- **Через `trace_ok`, не новый `action`** — правило говорит, как выставить `trace_ok`
  для интерактивного интента; дальше штатная матрица решений сама даёт `accept` (при
  `report_ok=true`). Схема вывода и матрица не меняются — правка чисто аддитивная.
- **Axis B нетронут** — если проза/отчёт врёт про выполненное действие, это
  по-прежнему `report_mismatches[]` → не примем. Защита от вранья сохранена.
- **«input the task genuinely needs next»** — якорь легитимности: yield валиден,
  только если продолжение объективно требует ввода пользователя (или задача по
  природе интерактивна — видно из intent + trace_digest).
- **Гард «near-repeat of an earlier one (stalling)»** — прямо бьёт по нашему кейсу
  (повтор того же вопроса = стойло, остаётся gap); не даёт правилу стать лазейкой
  для зацикливания.
- **Без имён тулов** — судья классифицирует по смыслу (intent + содержимое
  хэндоффа), как и весь остальной промпт.

### Механика после правки

Модель задаёт вопрос игроку (прозой ИЛИ через `turn_finish`) → судья распознаёт
легитимный turn-handoff → `trace_ok=true` → матрица → `accept` → ход штатно
закрывается `[completed]` → игрок отвечает новым ходом. **Петли нет.**

---

## Охват режимов

Промпт общий, поэтому эффект делится по **уже существующему** гейтингу:

- **Приём прозы как ответа** — остаётся **TUI-only** (ключ `text_answer_mode=judge`
  ставит только TUI; CLI/server/swarm → strict → прозу бунсят, судью для прозы не
  зовут). Правка их прозо-поведение не меняет.
- **Оценка `turn_finish`-хэндоффа finish-gate'ом** — во **всех** `chat_completions`
  режимах (CLI/server/swarm/TUI). Это корректно: «легитимная передача хода = валидное
  завершение» — универсальная семантика, не TUI-фишка; интерактивный CLI-агент тоже
  вправе штатно уступить ход. В headless (server/swarm) правило само не срабатывает —
  там интент не интерактивен, и гард «мог продолжить сам → gap» остаётся.

**TUI-only ветку сознательно НЕ делаем:** она потребовала бы отдельный/условный
промпт (дублирование в поддержке) и нарушила бы принцип единообразия поведения между
режимами (CLAUDE.md). Реальный риск уже локализован: единственная «новая» поблажка —
приём сырой прозы — и так только TUI. Правка finish-gate — это уточнение семантики
судьи, корректное везде.

Вне `chat_completions` (responses-режим) судьи нет — правка неактивна по построению.

---

## Тестирование

Изменение **чисто промптовое** (решение принимает LLM-судья), поэтому
детерминированная проверка тонкая:

1. **Загружаемость/доставка:** `_load_guard_prompt()` возвращает текст, содержащий
   секцию `## Interactive turn-handoff` (ключевые маркеры: `hands the turn back to
   the user`, `trace_ok=true`, `near-repeat`). Гарантирует, что правило в файле и
   корректно резолвится через `bundled_root()` (editable и wheel).
2. **Поведенческая валидация (ручная/живая, не CI):** реплей зацикленной сессии
   8c576f7b в TUI на той же модели — проза-вопрос викторины должна приниматься, ход
   закрываться `[completed]`, петли быть не должно. Плюс негативная проверка: агент,
   который «спрашивает вместо работы» при actionable-интенте, по-прежнему получает
   bounce (гард сработал).

Прогон существующих `intent_guard`/finish-gate тестов на отсутствие регрессий
(промпт-зависимые ассерты, если есть).

---

## Слои и ответственность

- Правка — только ассет `prompts/chat_completions_intent_guard_prompt.md`. Код
  (`intent_guard/*`, `processing_responses.py`) не трогаем.
- Конфиг не меняется (sync в demo/default не требуется).

## Вне scope

- **Предохранитель #2 (circuit-breaker от остаточных петель)** — отложен как
  возможное развитие. #1 чинит причину (петля не возникает). Если на практике
  всплывут остаточные циклы (судью крутит слабая модель и он продолжает ошибаться) —
  добавить точечно: второй, кумулятивный на ход счётчик no-tool-call бунсов (не
  сбрасывается tool-call'ами), при превышении `no_tool_call_turn_max` (≈8) →
  force-accept последнего видимого сообщения как ответа хода. Сейчас есть грубый
  backstop `max_steps`.
- Изменение самой матрицы решений / осей судьи, кода `text_answer_mode`,
  TUI-only-ветки промпта.
