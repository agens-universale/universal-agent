# Дизайн: стриминг `chat_completions` → живой эфемерный вывод в TUI

- **Дата:** 2026-06-15
- **Ветка:** `feat/tui-stream-chat-completions`
- **Статус:** design / на ревью
- **Режим API:** только `backend.api_mode = chat_completions`

## 1. Проблема

В режиме `chat_completions` клиент берёт ответ бэкенда **одним блоком** (`requests.post` без `stream`, затем `r.json()` — см. `utils/connect_backend.py::request_backend_chat_completions`). TUI «тейлит» тред пофайлово на тике `_poll_active` (250 мс) и показывает новые строки только **по завершении шага** (assistant-message / function_call / function_call_output записаны в стор).

На быстрых моделях (MiniMax, ~секунды на шаг) строки появляются часто — есть ощущение «tail». На медленной reasoning-модели `gigacode_agent_v1_rc` один шаг может занимать десятки–сотни секунд (`<think>`-блоки + длинные генерации), и всё это время в TUI **ничего не растёт**: видна только «зелёная плашка»/спиннер. Пользователь воспринимает это как «сообщение не уходит / зависло».

Подтверждено по сессии `2742a25f`: ответы модели — длинные `<think>` + проза, паузы между шагами 45–167 с.

## 2. Цель и не-цели

**Цель:** показывать живой вывод модели во время генерации шага в TUI, чтобы не выглядело зависшим, не меняя поведение агентного цикла и сохранив единый источник истины состояния.

**Не-цели (YAGNI):**
- Живой показ дельт в server/swarm (нет интерактивного холста).
- Живой показ в `gc-cli` stdout (отложено; решение — только TUI).
- Полная отрисовка `<think>` целиком (показываем компактно).
- Перенос `chat_completions` headless-режимов на стриминг в этой итерации (только флипом ключа позже).

## 3. Принцип (соответствие CLAUDE.md)

Стриминг разделён на два независимых слоя, что снимает конфликт с правилом «поведение системы единообразно между режимами; различаться могут только побочные эффекты»:

- **Слой 1 — HTTP-стриминг (транспорт).** `stream=True` + пересборка SSE в **тот же финальный объект**, что и сегодня. Цикл агента, история, тулы — идентичны во всех режимах. Бонус: длинные ответы не упираются в HTTP-таймаут.
- **Слой 2 — живой показ дельт (презентация).** Различается по режимам — это разрешённый «побочный эффект»: TUI рисует эфемерный блок; server/swarm/CLI получают `on_delta=None` и ничего не рисуют.

`State` (доменный SoT) не трогаем: канал дельт — это транспорт+презентация, в `states.py` ничего не кладём. Потребитель потока для персиста — по-прежнему тред-стор.

## 4. Решения (зафиксировано в brainstorming)

| Вопрос | Решение |
|---|---|
| UX живого вывода | **Вариант C** — эфемерный «in-progress» блок, по завершении заменяется канонной строкой из треда (как Claude Code; без per-token примирения). |
| HTTP-стриминг — раскатка | **Поэтапно**: сейчас стримим только при `on_delta` (TUI); server/swarm/CLI — прежний блокирующий путь, перевод **флипом ключа** после обкатки. |
| Живой показ | **Только TUI.** |
| Ключ конфига | **`backend.stream`** (kill-switch, default on). |
| `<think>` | Компактно: «думаю…» + последняя строка рассуждения (tail). Текст ответа стримим полностью. |

## 5. Как это делают по best-practice (опорная модель)

- API отдаёт SSE **чанками** (несколько токенов/слово на событие), не посимвольно. OpenAI-совместимый: `data: {"choices":[{"delta":{"content":"…"}}]}` … `data: [DONE]`.
- Reasoning идёт отдельным каналом (`delta.reasoning_content` или инлайн-теги `<think>`); показывается как «думает…», не как ответ.
- Рендер — **троттлинг** (перерисовка раз в ~50–100 мс), не на каждый токен. У нас естественный троттл — существующий тик `_poll_active` (250 мс).
- По завершении — единая отрисовка финального, канонического сообщения.

## 6. Архитектура

### 6.1 Компоненты и слои

1. **Транспорт — SSE-ридер** (`utils/connect_backend.py`).
   `request_backend_chat_completions(...)` получает опциональный `on_delta: Callable[[str, str], None] | None` (`kind ∈ {"answer", "think"}`, `text` — фрагмент).
   - Стрим-путь включается, только когда `backend.stream` **и** `on_delta is not None` → `stream=True`, парсинг SSE, на каждый чанк `on_delta(kind, text)`, **сборка финального объекта, идентичного `r.json()`-форме** (`choices[0].message.content`, `tool_calls`, `finish_reason`, `usage`), возврат как сегодня.
   - Иначе (нет потребителя или ключ off) → текущий блокирующий путь (`r.json()`), без изменений. Так headless-режимы этой итерации идут блокирующим путём даже при `stream: true` (нет `on_delta`).
   - Чистый транспорт: `on_delta` — инъекция, без бизнес-логики.

2. **Пересборка ответа из дельт** (вынести в чистую функцию, напр. `utils/chat_completions_stream.py::assemble_streamed_completion`).
   - Конкатенация `delta.content` → `message.content`.
   - Слияние `delta.tool_calls` по `index`: накопление `function.name` и **склейка фрагментов `function.arguments`** (частичная JSON-строка); `id`/`type` фиксируются по первому фрагменту.
   - `finish_reason` — из последнего значимого чанка; `usage` — из финального чанка (учесть `stream_options.include_usage`, если требуется бэкендом).
   - Распознавание reasoning: `delta.reasoning_content` → `kind="think"`; инлайн `<think>…</think>` в `content` → выделять как `think`, остальное как `answer`.
   - Возвращает объект, побайтово совместимый с non-stream shape для downstream.

3. **StreamSink** (новый презентационный объект `tui/services/stream_sink.py`, по образцу `tui/services/mcp_progress.py`).
   - Thread-safe буфер текущего шага: `phase ∈ {think, answer}`, накопленный `think_tail` (последняя строка), накопленный `answer_text`, счётчик/таймстемп.
   - Воркер-поток агента пишет (`push(kind, text)`); UI-поток дренит на тике.
   - `reset()` — сброс при старте нового шага / ретрае / stop.

4. **Проводка через RunContext** (`run_context.py`).
   - Добавить `on_stream_delta: Optional[Callable[[str, str], None]] = None` (презентационный колбэк — ровно по образцу существующего `on_progress`; **не** в `State`).
   - `processing_responses.py` (ветка chat_completions) пробрасывает `ctx.on_stream_delta` в `request_backend_chat_completions(..., on_delta=ctx.on_stream_delta)`.
   - `app.py` цепляет `on_stream_delta = StreamSink.push` (thread-safe).

5. **Рендер** (`tui/screens/main.py`).
   - На тике `_poll_active`: если run активен и в sink есть in-progress контент — смонтировать/обновить **эфемерный** in-progress-виджет (think-tail + растущий answer).
   - Когда канонная assistant-строка шага приходит из треда обычным poll — эфемерный блок **гасится** (замена на отрисованный финал).
   - Новый таймер не вводим — переиспользуем 250 мс тик (троттл по best-practice).

### 6.2 Поток данных

```
SGLang/gigacode SSE
  → request_backend_chat_completions(on_delta)         [транспорт]
      ├─ assemble_streamed_completion(...) → финал == non-stream shape  → агент-цикл (без изменений)
      └─ on_delta(kind, text) → ctx.on_stream_delta → StreamSink.push   [презентация, thread-safe]
  → [250 мс] _poll_active дренит StreamSink → эфемерный блок
  → шаг завершён → assistant-строка в треде → poll рисует канон + гасит блок
```

### 6.3 Правило примирения

Единственный источник истины состояния — тред-стор (без изменений). Эфемерный блок — **одноразовое представление** текущего шага; гасится при:
- появлении финальной строки этого шага в треде, **или**
- stop/cancel/конце run.

## 7. Краевые случаи

| Случай | Поведение |
|---|---|
| Tool-call без текста | Блок показывает только «думаю…»; финальные tool-строки — как сейчас. |
| `no_tool_call_bounce` (проза без тула) | Прозу видно вживую; затем ложится assistant-строка + technical-bounce (как сейчас). Спец-логики нет. |
| stop/cancel mid-stream | `StreamSink.reset()`; `discard_writes_after_stop` и так не даёт партиалу осесть в треде; блок снимается. |
| retry/backoff (`request_backend_policy.py`) | На ретрае `StreamSink.reset()` — партиал шага сброшен, блок начинается заново. Политика backoff/понижения max_tokens сохраняется. |
| Суммаризация mid-run | Не задето (отдельные backend-вызовы; sink по шагам). |
| HTTP 4xx/5xx | **Сохранить** текущее логирование тела ответа (`response_body`) — критично для диагностики (ловили им 401/404). При стриминге ошибку статуса перехватывать до начала чтения тела стрима. |
| `usage` отсутствует в стриме | Если downstream зависит от `usage` — запросить `stream_options.include_usage` либо мягко деградировать; зафиксировать в тестах. |

## 8. Риски (слой 1, HTTP-стриминг)

- **Сборка tool_calls** — главный риск: `arguments` приходят фрагментами по `index`, склейка должна быть точной; ошибка = битый tool-call. Митигируется тестами на реальном payload, включая мульти-тул и фрагментированные `arguments`.
- **`usage`/`finish_reason`** — место в стриме разнится по бэкендам (SGLang/gigacode/RouterAI). Покрыть тестом на реальных сэмплах.
- **Потеря диагностики ошибок** — сохранить `response_body`-логирование.
- **Взаимодействие с ретрай-политикой** — стрим рвётся иначе; политика должна остаться рабочей.

Поэтапная раскатка (TUI-only сейчас) ограничивает blast radius: баг стриминга виден только в интерактивном TUI, прод-режимы на проверенном блокирующем пути до флипа ключа.

## 9. Конфиг

```yaml
backend:
  stream: true   # default on; off → сегодняшний блокирующий путь
```

- Поле в `BackendConfig` (`config_modules/backend.py`), проброс в `config_dict` (`config.py`).
- Семантика этой итерации: стримим, когда `backend.stream` **и** задан `on_delta` (т.е. из TUI). Headless получают `on_delta=None` → блокирующий путь, даже при `stream: true`, до отдельного флипа на следующей итерации.

## 10. Тестирование (TDD, реальные данные)

- **Шаг 0:** снять реальный SSE-сэмпл с SGLang/gigacode (`curl`) — парсер/сборку пишем на нём (правило real-payload-first), не на гипотетических моках.
- **Unit — `assemble_streamed_completion`:** на реальной последовательности `data:`-строк (incl. `reasoning_content`, инлайн-`<think>`, фрагментированные `tool_calls.arguments`, `[DONE]`) собранный финал **равен** non-stream `r.json()`-форме; `on_delta` получает корректные `(kind, text)`.
- **Unit — StreamSink:** thread-safety, drain-семантика, `reset`.
- **Unit — регресс:** `on_delta=None` и `stream` off → путь не изменился (тот же запрос/ответ).
- **TUI:** эфемерный блок монтируется на дельтах и гасится при появлении канонной строки (целевой прогон; снапшоты флапают в полном прогоне — гонять точечно).

## 11. Затрагиваемые файлы (ориентир)

- `src/utils/connect_backend.py` — `on_delta`, ветвление stream/non-stream.
- `src/utils/chat_completions_stream.py` — **новый**: парсинг SSE + `assemble_streamed_completion`.
- `src/config_modules/backend.py`, `src/config.py` — ключ `backend.stream`.
- `src/run_context.py` — `on_stream_delta`.
- `src/processing_responses.py` — проброс `ctx.on_stream_delta` в chat_completions-ветку.
- `src/tui/services/stream_sink.py` — **новый**: StreamSink.
- `src/tui/app.py` — цепляет sink на `on_stream_delta`.
- `src/tui/screens/main.py` — рендер эфемерного блока + гашение на канонной строке.
- Тесты: `tests/test_chat_completions_stream*.py`, `tests/tui/test_stream_*`.

## 12. Орг

- Ветка: `feat/tui-stream-chat-completions` (от master).
- Спека: этот файл (`docs/tui/specs/`).
- План реализации: `docs/tui/plans/` (после одобрения спеки).
