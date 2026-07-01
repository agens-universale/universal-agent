# Context gauge — normalize to hard threshold — Design

**Дата:** 2026-06-24
**Статус:** согласовано, готово к плану
**Ветка:** feat/agent-instructions-and-gc-guard

## Цель

Убрать визуальный рассинхрон: в длинном ходу бар «контекст NN%» прибит к 100% на минуты, а сжатие («сжимаю контекст…») стартует заметно позже. Шкала должна мерить заполненность до точки, где система **обязана** сжать.

## Проблема

`_context_meter` ([src/tui/screens/main.py:222](src/tui/screens/main.py#L222)) нормирует гейдж на **turn-end** порог:
```python
threshold = parse_summary_turn_end_trigger_chars(cfg) or parse_summary_trigger_chars(cfg)
```
Числа (конфиг по умолчанию): `summary_trigger=200000`, `turn_end=120000` (= 200000 × 0.6).

Сжатие срабатывает в трёх местах (`run_summary_coordinator.py`):
- `pre_run_compact_if_needed` / `pre_backend_compaction_hook` — по **summary_trigger (200k)**; `pre_backend` — это и есть мид-турновое сжатие после tool-output;
- `turn_end_compact_if_needed` — по **turn_end (120k)**, но **только когда `turn_finish` принят** (конец хода).

В длинном незавершённом ходу: объём пересекает 120k → `min(100, 100·chars/120000)` = **100%**, бар залипает; turn-end-сжатие не идёт (ход не закончен); реальное мид-турновое сжатие ждёт **200k**. Отсюда «100% давно, сжатия нет».

### Корень

Гейдж нормирован на нижний (120k) порог, а сжатие внутри хода — по верхнему (200k). Коммит `90f6a260` поставил знаменатель 120k на **ошибочной посылке** (его комментарий: «turn-end срабатывает после каждого tool-output» — неверно: после tool-output жмёт `pre_backend` по 200k; turn-end 120k только на `turn_finish`). Тем самым `90f6a260` молча отклонился от исходной спеки ([2026-06-23-session-context-status-ux-design.md](2026-06-23-session-context-status-ux-design.md)), где `context_threshold = chat_completions_summary_trigger_chars`.

## Решение (вариант A — нормировать на жёсткий потолок)

Поменять порядок в `or` — жёсткий потолок primary, turn-end fallback:
```python
threshold = parse_summary_trigger_chars(cfg) or parse_summary_turn_end_trigger_chars(cfg)
```
И поправить комментарий над строкой: убрать ошибочное «turn-end после каждого tool-output», написать корректную причину (меряем до точки форс-сжатия = `summary_trigger`; turn-end — проактивная уборка между ходами, её показывает нижняя плашка «сжимаю…»).

### Почему swap, а не «убрать turn_end»

- **Норма** (`summary=200k, turn_end=120k`): `200000 or 120000` = **200000** → бар растёт 0→100%, «сжимаю…» (мид-турн) у ~100%. Баг уходит.
- **Edge** (`summary=0` — мид-турн/pre-run сжатие выключено, `turn_end>0`): `0 or 50000` = **50000** → меряем до единственного реального порога. Без fallback гейдж бы скрылся — поэтому fallback нужен (не сирота).
- **Оба 0:** `0 or 0` = 0 → скрыт (как сейчас).

### Принятый trade-off

На конце хода при контексте 120–200k «сжимаю…» (turn-end-сжатие) появится, когда бар показывает 60–100%. Это честно (контекст реально на столько), а действие объясняет нижняя плашка. Сознательно выбираем это вместо ложного 100% на 120k (то, что пытался показать `90f6a260`).

Существующий `tests/tui/test_context_meter.py` покрывает **только** чистый `render_context_meter`; выбор порога в `_context_meter` (то, что правим) не покрыт — добавляем screen-level тест (через `run_test`, как `test_processing_indicator.py`). Техника устойчива к внутренностям `calc_history_chars_for_trigger`: задаём пороги так, чтобы выбор знаменателя давал заведомо разный %.

1. **Гейдж на жёсткий порог (ключевой RED→GREEN):** `summary_params: {chat_completions_summary_trigger_chars: 10_000_000, chat_completions_summary_turn_end_trigger_chars: 1}`, активная задача с одним маленьким сообщением → бар показывает **~0%** (от summary 10M). На старом коде (turn_end=1 primary) было бы 100% → тест ловит регресс.
2. **Fallback:** `trigger: 0, turn_end: 10_000_000`, маленькое сообщение → `_context_meter` не None и показывает % (использует turn_end, иначе знаменатель 0 → скрыт).
3. **Регресс:** прогон `tests/tui/test_context_meter.py` + screen/снапшот-тесты шапки; снапшоты при сдвиге %/цвета регенерировать осознанно.

## Слои и ответственность

- Правка — только `src/tui/screens/main.py::_context_meter` (выбор порога + комментарий).
- `render_context_meter` (`src/tui/services/context_meter.py`) — чистая презентация, не трогаем.
- `run_summary_coordinator` / summary-pipeline / пороги / ratio 0.6 — **не трогаем**.
- Системный промпт, конфиг — не трогаем.

## Вне scope

- Изменение порогов сжатия или ratio 0.6 (pipeline).
- Визуальная метка проактивного turn-end-порога на баре (вариант C) — отклонён в пользу простоты.
- Дефолты конфига (`90f6a260` уже синхронизировал trigger=200000).
