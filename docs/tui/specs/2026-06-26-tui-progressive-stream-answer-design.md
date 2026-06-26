# Дизайн: прогрессивный показ ответа агента в TUI (стрим как у харнесов)

Дата: 2026-06-26
Статус: дизайн (ожидает ревью)

## Проблема

В `chat_completions`-режиме TUI инфраструктура стрима уже есть, но ответ агента
не показывается «как у других харнесов» (Claude Code, Hermes): прогрессивный показ
выключен по умолчанию, а когда включён — рисуется **серым эфемерным** блоком, который
на финале **скачком** подменяется канонической markdown-строкой.

Причины (из текущего кода):
1. Выключено дефолтом — флаг `show_stream_answer: false`.
2. Серый (`$deep-dim`) plain-текст ≠ финальный вид → видимый скачок цвета/стиля.
3. Два пересекающихся булевых флага (`show_stream_answer`, `show_reasoning_line`).

Цель: прогрессивный ответ **по умолчанию**, в нормальном виде, плавно дорастающий до
финала; навести порядок во флагах. Чисто, без оверинжиниринга.

## Что НЕ трогаем (контекст реализации)

Конвейер стрима уже корректен и остаётся как есть:
- Транспорт: `src/utils/chat_completions_stream.py::assemble_streamed_completion`
  (эмитит `on_delta(kind∈{"think","answer"}, text)`).
- Подключение: `src/processing_responses.py:219-231` (`on_delta` только при `stream:true`).
- Кросс-тред буфер: `src/tui/services/stream_sink.py::StreamSink` (think/answer,
  счётчик `progress_units`, тайминг `reasoning_state`) — питает индикатор **независимо**
  от показа текста (поэтому `minimal` осмыслен).
- `ThinkingIndicator` (счётчик токенов + «думаю…»/«✓ за N»).

Правка локализована в **слое рендера TUI + конфиге**.

## Решения

### 1. Флаг-модель (clean break, без миграции)

`TuiConfig` (`src/config_modules/tui.py`): убрать `show_reasoning_line` и
`show_stream_answer`; ввести один enum:

```python
stream_display: Literal["minimal", "answer", "answer_reasoning"] = "answer"
```

Линейная эскалация детализации:

| значение | показывает |
| --- | --- |
| `minimal` | живой индикатор + счётчик токенов; без текста стрима |
| `answer` (**дефолт**) | + прогрессивный ответ, дорастающий до финала |
| `answer_reasoning` | + однострочный «глазок» рассуждения `⠇ …` |

- Флэттенинг `src/config.py:196-198`: `tui_show_reasoning_line`/`tui_show_stream_answer`
  → один `tui_stream_display`.
- Старые флаги — это только текущая ветка, в проде их нет. Миграция/deprecated-алиасы
  **не делаем**; `extra="forbid"` остаётся.
- **Обязательно** пронести новый флаг (вместо двух старых) в:
  - `config.default.yml:48-49` (ставится при установке),
  - `demo_config.yml:59,81-83` (+ комментарий о применимости только в `chat_completions`),
  - `docs/CHANGES-vs-master.md:62-63` (таблица фич — заменить две строки одной).

### 2. Рендер V1 — «живой prose, форматирование на финале»

Чтобы не нарушать ответственность слоёв, V1 раскладывается на три части:

- **`render_stream_answer(text: str) -> Text`** — в `src/tui/services/markdown_render.py`
  (модуль-владелец рендера). Построчно: inline-разметка (`**bold**`, `` `code` ``) через
  **тот же модуль** (`_apply_inline_styles` — без кросс-модульного импорта приватного) +
  line-стили heading/bullet; fenced ```` ``` ````-строки и таблицы — **plain** (без рамок;
  мерцания на незакрытом фенсе/половине таблицы нет). Цвет — INK. На финале каноническая
  строка доезжает до боксов/сетки штатным блок-пайплайном.
- **`<think>`-strip остаётся в `format_stream_block(answer) -> str`** (`stream_sink.py`,
  уже существует) — это его концерн; poll берёт `clean = format_stream_block(...)` и
  передаёт в `render_stream_answer(clean)`.
- **`select_stream_render(mode, clean_answer, reasoning_line) -> tuple[kind, thinking]`** —
  чистая функция-решение в `stream_sink.py`, `kind ∈ {"answer","reasoning","none"}`. Без
  зависимости от markdown_render (возвращает только вид) → тестируется изолированно.

`src/tui/widgets/stream_block.py`: `set_text` принимает renderable (`str | Text`) — для
answer `Text`, для reasoning-глазка `str` (Static.update принимает оба). Стили
(`src/tui/styles.tcss`): `.stream-block` — `$deep-dim` → **`$ink`** (база честная; answer и
так несёт явный INK в спанах); `.stream-block-think` — без изменений (для reasoning-строки).

### 3. Ветка показа в `_poll_active` (`src/tui/screens/main.py:1503-1535`)

Заменить два булевых гейта решением `select_stream_render` по `tui_stream_display`:

```python
mode = _cfg.get("tui_stream_display", "answer")
clean = format_stream_block(answer=answer)         # <think> вырезан
reasoning = format_reasoning_line(think=think)
kind, thinking = select_stream_render(mode, clean, reasoning)
renderable = (render_stream_answer(clean) if kind == "answer"
              else reasoning if kind == "reasoning" else None)
# mount/update StreamBlock при renderable, иначе clear(reset_sink=False)
```

Семантика: `minimal` — `none` (sink жив, блок не показываем); `answer` — только ответ;
`answer_reasoning` — reasoning-глазок до появления текста ответа, затем ответ.

### 4. Бесшовный шов «стрим → финал» — нового кода НЕ требует

Шов уже реализован: `_render_event` на монтаже **любой** канонической строки вызывает
`self._clear_stream_block()` ([main.py:804](../../src/tui/screens/main.py#L804)) — то есть в
тот же тик, когда из `tail` монтируется каноническая строка ответа, эфемерный `StreamBlock`
снимается (mount-then-clear по построению, без «дыры» и без 1-тик дубля). Покрыто тестом
`test_stream_block_mounts_then_clears_on_canonical_row`.

Для прозы переход INK→INK незаметен; код/таблица «защёлкиваются» в рамку на финале (граница
V1). Полная бесшовность для всех типов контента — свойство true-parity (Direction A). V1 здесь
допускает доводку на живых прогонах, но в рамках V1 отдельного шов-кода не пишем.

## Тесты (TDD; TUI → `uv run pytest`)

- `tests/tui/test_tui_config_stream_display.py` (new): дефолт `answer`; принимает три
  значения; старые ключи отвергаются (`extra="forbid"`); флэттенинг даёт
  `tui_stream_display`.
- `tests/tui/test_render_stream_answer.py` (new): `render_stream_answer` — `**bold**`/
  `` `code` `` дают стилизованные спаны; heading/bullet — line-стиль; fenced-строка остаётся
  plain (нет рамки); пустой текст → пустой `Text`. (`<think>`-strip покрыт существующим
  `format_stream_block`.)
- `tests/tui/test_select_stream_render.py` (new): `select_stream_render` — `minimal`→`none`;
  непустой clean→`answer` (даже если есть reasoning); пустой clean при `answer`→`none`; пустой
  clean при `answer_reasoning` с reasoning→`reasoning`.

## Варианты роста (не сейчас)

Зафиксированы как named-направления, если V1 на живых прогонах окажется недостаточным
(сначала оцениваем V1, потом решаем):

- **V2 — полный живой рендер блок-дерева** (рамки code-block/таблицы формируются по мере
  стрима через текущий `markdown_render`-пайплайн). Максимальная точность, но в Textual
  тяжело: ремоунт дерева виджетов 4 раза/сек + обработка незакрытого фенса/половины таблицы.
- **True-parity — унификация рендереров (как Claude Code).** Даёт настоящую бесшовность
  для **всех** типов контента (включая код/таблицы), в отличие от «V2 в лоб». Источник
  бесшовности в Claude Code: (1) рендерер (Ink) диффит вывод терминала и красит только
  изменившиеся ячейки → перерендер всего markdown на чанк дёшев; (2) стрим и финал идут через
  **один** markdown-рендерер → шва нет по построению. Ниже — конкретный чистый путь под наш
  стек (Direction A); это отдельная крупная задача, разрабатываем позже отдельным циклом
  spec→plan.

  **Direction A — «один renderable, один виджет» (рекомендуемый чистый путь):**
  Шов исчезает не сглаживанием, а по построению — стрим и финал строит одна функция.
  1. **Одна чистая функция** в `markdown_render.py`:
     `render_answer(text: str) -> RenderableType` — реюзит `render_block`, собирает
     `rich.console.Group`: prose/heading/bullet → `Text` (`render_block_to_text`),
     таблица → `rich.Table` (`build_markdown_table`, уже rich), code-block → `rich.Panel`/
     `Syntax`. **Толерантна к незаконченному markdown**: открытый ```` ``` ```` →
     code-блок из имеющегося; половина таблицы (нет строки-разделителя) → пока plain.
     Чистая, без I/O → тестируется как `text → структура`.
  2. **Один host-виджет** `AnswerBlock(Static)`: `set_text(text)` → `update(render_answer(text))`.
     Static держит `Group`; Textual-компоситор диффит строки вывода (не дерево виджетов) →
     дёшево, без мерцания (аналог reconciler'а Ink).
  3. **Стрим** = `answer_block.set_text(accumulated)` каждый poll-тик.
  4. **Финал = визуальный no-op:** живой `AnswerBlock` **становится** канонической строкой
     (перестаём обновлять — он уже содержит `render_answer(final)`). Ни подмены, ни
     перемонтажа, ни смены цвета — для любого контента.

  **Чем чисто:** единый источник истины «как выглядит ответ» (`render_answer`) — для стрима,
  финала и, при желании, preview/export; нет reconciler'а, нет seam-логики, нет двойного
  представления (сейчас их два: Static-стрим vs блок-дерево-финал); меньше виджетов в трейсе.

  **Цена/риски:** финал переезжает с TCSS-классов (`.code-block`/`.table-block`) на rich-стили
  (`Panel`/`Syntax`/`border_style`) — таблицы уже на rich (почти даром), code-block надо
  переразразить и **сверить вид 1:1** → нужны **snapshot-тесты** на регресс устоявшегося вида;
  определить правила толерантности к частичному markdown (открытый фенс / половина таблицы);
  проверить скролл/выделение одного многоблочного Static.

  **Альтернатива (Direction B) — отвергнута как менее чистая:** «живое блок-дерево» — стрим
  монтирует те же виджеты, что и финал, реконсайля их по росту. Плюс: сохраняется текущий
  TCSS-вид. Минус: нужен мини-reconciler по списку блоков (mount/unmount/update минимального
  набора) + гашение «дёрганья» хвостового блока, меняющего тип (para→code при открытии фенса).
  Сложнее и грязнее, чем один renderable.

V1 — осознанный размен: бесшовность для прозы (≈90% ответов) малой ценой; код/таблицы
«защёлкиваются» в рамку на финале — единственное место расхождения с Claude Code.

## Вне scope

- Транспорт/`StreamSink`/`ThinkingIndicator` — не трогаем.
- `responses`-режим — стрим-показ к нему не относится (фича только `chat_completions`).
- Поведение агента, finish-gate, суммаризация — не трогаем.
