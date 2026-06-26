# Дизайн: событийная перерисовка стрима ответа (по приходу дельты)

Дата: 2026-06-26
Статус: дизайн (ожидает ревью)

## Проблема

Сейчас стрим-виджет освежается общим poll-тиком (`set_interval(0.25, _poll_active)`),
т.е. контент ответа перерисовывается с фиксированной частотой 4 fps независимо от темпа
генерации — ощущается порционно («блочно»), не как непрерывный поток у Claude Code/Ink.
Поднять частоту самого poll'а — грубо: разгоняется ВСЯ тяжёлая часть (файловый I/O хвоста
треда, статус, заголовок), и таймер тикает вхолостую между токенами.

Цель: контент стрима освежать **по приходу дельты** (как Ink), коалесцируя до разумного
потолка, не разгоняя тяжёлый poll и не тикая вхолостую.

## Решение и граница

**Poll — структура, событие — контент.**
- Структурный lifecycle стрим-виджета (монтаж / снятие / смена kind answer↔reasoning)
  остаётся на poll'е; poll возвращается на **250 мс** (триальную правку 0.4 откатываем) и
  служит backstop'ом финального/хвостового кадра.
- Контент уже смонтированного виджета освежается **событийно** — по приходу дельты,
  коалесцируя «не чаще раза в `STREAM_REFRESH_MS`».

## Архитектура и компоненты

### 1. `StreamPump` — обёртка дельт (новый, чистый/тестируемый)
`src/tui/services/stream_pump.py`:

```python
STREAM_REFRESH_MS = 80   # потолок коалесценса событийной перерисовки стрима

class StreamPump:
    def __init__(self, sink, signal, *, throttle_s, clock=time.monotonic):
        # sink: StreamSink; signal: no-arg колбэк, безопасный из worker-потока
        ...
    def on_delta(self, kind, text) -> None:
        self._sink.push(kind, text)            # как сейчас — буфер не теряем
        now = self._clock()
        if now - self._last >= self._throttle_s:
            self._last = now
            self._signal()                      # leading-edge коалесценс
```

Троттл-состояние живёт **только на worker-потоке** (колбэк дельты вызывается из агент-потока) →
без локов; `sink` уже thread-safe. `signal` оборачивается в try/except в проводке (поздний
сигнал при teardown не должен ронять worker).

### 2. Проводка — `src/tui/app.py:104-107`
`on_stream_delta` теперь идёт через pump, а `signal` маршалит в UI-поток тем же мостом, что
confirm-баннер ([app.py:125](../../src/tui/app.py#L125)):

```python
pump = StreamPump(
    self._stream_sink,
    signal=lambda: self.call_from_thread(self._refresh_stream_content),
    throttle_s=STREAM_REFRESH_MS / 1000,
)
ctx = create_run_context(cfg, ..., on_stream_delta=pump.on_delta)
self._stream_pump = pump   # держим ссылку (per-run)
```

`self._refresh_stream_content` — делегат на активный `MainScreen` (no-op, если экрана/виджета
нет). `call_from_thread` блокирует worker на ~мс ≤~12/сек — приемлемо, прецедент.

### 3. `MainScreen._refresh_stream_content()` (UI-поток) — контент уже смонтированного
Освежает контент **существующего** стрим-виджета из снапшота sink; **не** монтирует/снимает/
переключает (это poll):

```python
def _refresh_stream_content(self) -> None:
    sink = getattr(self.app, "_stream_sink", None)
    if sink is None:
        return
    following = self._trace_is_at_bottom()
    think, answer = sink.snapshot()
    if self._stream_answer_row is not None:
        self._stream_answer_row.set_content(render_prose(format_stream_block(answer=answer)))
    elif self._stream_block is not None:
        self._stream_block.set_text(format_reasoning_line(think=think), thinking=True)
    else:
        return
    if following:
        self.query_one("#trace", VerticalScroll).scroll_end(animate=False)
```

Poll-ветка `_poll_active` после своего mount/switch вызывает **тот же** `_refresh_stream_content`
для контента — устранение дублирования (poll владеет структурой + kind-решением, контент —
общий метод). Слоты `_stream_*` мутируются и читаются только на UI-потоке (poll + событие
через `call_from_thread`) → без гонок.

### 4. Backstop (без потери финала)
Троттл leading-edge → последняя дельта пачки может не дать сигнала; **poll (250 мс)
гарантирует финальный кадр** в пределах 250 мс, плюс на финале каноническая строка заменяет
стрим (`_render_event` → `_clear_stream_block`). Между токенами событий нет → вхолостую
ничего не перерисовывается.

## Регресс-поверхности (что НЕ сломать)

- **Откат триала:** `main.py` poll вернуть `0.4 → 0.25`.
- **Существующие стрим-тесты** (`test_stream_block_render.py`, `test_stream_autoscroll.py`)
  зелёные: poll по-прежнему делает mount/switch/clear/autoscroll; событийный путь аддитивен.
  Рефактор poll-ветки (вынос контента в `_refresh_stream_content`) сохраняет поведение.
- **Автоскролл (#2):** stick-to-bottom внутри `_refresh_stream_content` (capture `following`
  до апдейта), идемпотентен с poll-путём.
- **CLI/server/swarm:** `on_stream_delta` подключается ТОЛЬКО в TUI `app.py` → pump TUI-only;
  не-TUI режимы не затронуты.
- **Teardown:** поздний `signal()` после закрытия loop'а — try/except в проводке, worker не
  падает.

## Тесты (TUI → `uv run pytest`)

- `tests/tui/test_stream_pump.py` (new, чистый — фейк clock + recorder signal):
  `sink.push` зовётся на каждой дельте; в окне < T → один сигнал; дельты, раздвинутые > T →
  несколько; не чаще раза в T; первый вызов сигналит (last инициализирован так, что
  leading-edge срабатывает).
- `tests/tui/test_refresh_stream_content.py` (new, pilot): смонтированный `_stream_answer_row`
  обновляет контент при прямом вызове `_refresh_stream_content`; при отсутствии виджета —
  no-op (без исключений).
- Регресс: `test_stream_block_render.py`, `test_stream_autoscroll.py` — без изменений зелёные.

## Вне scope
- Конфиг-флаг частоты (решено — константа `STREAM_REFRESH_MS`).
- Non-блокирующий мост (решено — `call_from_thread`, прецедент).
- Reasoning-«глазок»/poll-структура/поведение агента — не меняем.
