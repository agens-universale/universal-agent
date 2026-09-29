[Назад к README](../readme.md)

# HTTP-фасад Responses (`gc-responses`)

Отдельный процесс. Внешнее приложение открывает ход агента через `POST /v1/responses` и читает стрим прозы, рассуждения и коротких карточек тулов. Конверт близок к OpenAI Responses API. Петля тулов остаётся внутри ядра: клиент присылает текст и не исполняет тулы сам.

Это не `gc-serve`. У вебхука другой контракт: он отвечает `started` и просит опрашивать статус. Здесь соединение живёт, пока жив ход. Флага у `gc-cli` и маршрутов в `gc-serve` нет.

Один запрос — один ход. Продолжение диалога — `previous_response_id` прошлого хода, тот же тред (`system="responses"`).

## Установка и запуск

Зависимости FastAPI и uvicorn живут в дистрибутиве `gc-responses`. Базовая установка CLI их не ставит.

```bash
uv tool install -e packages/responses
# без клонирования репозитория:
uv tool install "gc-responses @ git+https://gitverse.ru/gigacode-ai/universal-agent#subdirectory=packages/responses"
```

В корпоративной сети к `uv tool install` добавляйте `--system-certs`.

```bash
gc-responses --config ./config.yml
gc-responses --config ./config.yml --profile <имя>
```

Без `--config` поиск конфига тот же, что у остальных команд: явный путь, затем `config.yml` в каталоге Config, затем пакетный шаблон. Конфиг не читается — сообщение в stderr, код выхода 1, порт не открывается.

`os_backend` обязан быть `local`. `harbor` и неизвестный бэкенд завершают процесс с кодом 1 до bind.

По умолчанию слушает `127.0.0.1:8090`. Логи — в stderr. Тело ответа в stderr не пишется. Пустой `responses.auth_token` даёт на старте одну строку: сервер без авторизации.

## Конфиг

Секция `responses` — часть общей схемы `config.yml`. Аннотированный образец — [`demo_config_responses.yml`](../demo_config_responses.yml) в корне репозитория: скопируйте блок в свой конфиг агента. В шаблон CLI (`config.default.yml`) и в справочник `demo_config.yml` секция не входит. Без неё схема подставляет дефолты, и процесс стартует.

Ключи читает только этот процесс. gc-cli и TUI их не применяют.

| Ключ | Дефолт | Смысл |
|---|---|---|
| `responses.host` | `127.0.0.1` | Адрес bind. Пустая строка отклоняется при загрузке. |
| `responses.port` | `8090` | Порт, 1–65535. |
| `responses.auth_token` | `""` | Пусто — заголовок не проверяется. Иначе каждый запрос к `/v1/` несёт `Authorization: Bearer <token>`, иначе 401 `invalid_api_key`. |
| `responses.cors_origins` | `[]` | Пусто — `Access-Control-Allow-Origin` не ставится. Иначе эти origin, методы `GET`, `POST`, `OPTIONS`, заголовок `Authorization`. `"*"` из списка выбрасывается. |
| `responses.launch_root` | `null` | Рабочий каталог нового хода, если в запросе нет абсолютного `metadata.cwd`. Относительный путь не используется: тогда берётся каталог процесса. |
| `responses.text_answer_mode` | `allow` | `deny`, `judge` или `allow`. После загрузки пишется в runtime-ключ `text_answer_mode`. |
| `responses.finish_gate_mode` | `off` | `off`, `on` или `smart`. Пишется в `strict_turn_finish_validation` (`true`, когда режим не `off`) и `finish_gate_smart` (`true` только для `smart`). |
| `responses.todo_reminder_steps` | `0` | Напоминания про `session_todo`. `0` — не напоминать. Сам тул остаётся. Значение пишется в `todo_params.reminder_steps` этого процесса. |

Значения `off` и `on` в yaml пишите в кавычках: без них YAML читает их как boolean, и загрузка конфига падает.

`agent.strict_turn_finish_validation` и уже лежащий в конфиге `text_answer_mode` на этом процессе не действуют: фасад перекрывает оба ключа после загрузки. Дефолт канала — проза без тула принимается, судья на `turn_finish` не зовётся. `session_todo` остаётся в схеме. Напоминания о нём этот процесс ставит в `0`, если в секции не задано другое число.

После загрузки режим дописывает в `stop_tools` имена `turn_ask_human` и `turn_intermediate_reply`: ни вопроса человеку, ни промежуточной реплики в схеме нет. Прогресс пишется прозой. В системный промпт подставляется ролевая вставка режима (`chat_completions_responses_special_instructions.md`): вопрос пользователю — проза ответа, следующий запрос — ответ человека. Свой `paths.chat_completions_special_instructions_path` на этом процессе не читается. Порт `ask_human` остаётся и сразу возвращает пустую строку, чтобы случайный вызов не читал stdin.

Неизвестный ключ внутри `responses` загрузка отклоняет (`extra=forbid`).

Рабочий каталог хода, по порядку:

1. абсолютный `metadata.cwd` нового запроса;
2. иначе абсолютный `responses.launch_root`;
3. иначе каталог процесса.

На продолжении присланный `metadata.cwd` должен совпасть с каталогом, записанным у прошлого ответа. Чужой путь — 400, ход не стартует.

## Запрос

`POST /v1/responses`. Тело — объект. Обязательны непустые `model` и `input`.

`model` в объект ответа копируется как присланная строка. В роутер моделей фасад её не передаёт: модель хода — та, что в конфиге агента.

`stream: true` — ответ `text/event-stream`, поток закрывается на терминальном событии. Нет поля или `false` — процесс держит запрос до конца хода и отдаёт один объект Response.

`input` — строка или массив. Из массива в ход берётся только последний элемент, история перед ним игнорируется. Текст последнего элемента: сама строка; либо объект `role: user` (с `type: message` или без type) и `content` строкой; либо `content` — массив, и склеиваются через перевод строки только части `type: input_text`. Картинки, файлы и прочие части этого сообщения пропускаются. Последний элемент `assistant`, `function_call` или `function_call_output` текста не даёт. Пустой текст — 400, ход не стартует.

Без `previous_response_id` открывается новый тред. С ним ход продолжается на том же `thread_id`. Пока предыдущий ход этого треда не записал терминальный статус, второй `POST` — 409, новый ход не стартует. Клиент шлёт тот же текст ещё раз, когда первый ход закончился.

`instructions`, `tools`, `tool_choice`, `temperature`, `max_output_tokens`, `store`, `truncation` принимаются и на ход не влияют. `store: false` индекс всё равно пишет.

Ошибки запроса — JSON `{ "error": { "message", "type": "invalid_request_error", "code" } }`:

| HTTP | `code` | Когда |
|---|---|---|
| 400 | `invalid_request` | тело не объект, нет `model` или `input`, у последнего элемента нет текста, чужой `metadata.cwd` на открытом треде |
| 401 | `invalid_api_key` | токен задан, заголовок не сошёлся |
| 404 | `not_found` | нет такого ответа |
| 409 | `conflict` | у этого треда ещё идёт ход |
| 500 | `server_error` | ход без стрима упал исключением. В теле при этом объект Response со `status: failed`, не конверт ошибки |

## Объект ответа

Поля: `id` (`resp_` и uuid), `object: "response"`, `created_at`, `status`, `model`, `previous_response_id`, `output`, `error`, `incomplete_details`, `metadata`, `usage` (`null`).

`status`: `completed` — обычный конец хода, в том числе `tool_errors`; `incomplete` — `token_limit`, `step_limit` или `tool_call_limit`, и тогда `incomplete_details.reason` равен этому исходу; `cancelled` — `POST …/cancel`, обрыв соединения или остановка хода; `failed` — исключение, `error.message` — его текст.

Элементы `output`: `message` (проза), `reasoning` (сплошная серия мыслей), `gc_tool_call`. Клиентского `function_call` фасад не пишет.

Карточка тула — имя, короткий заголовок и статус (`in_progress`, затем `completed` или `failed`). Сырых аргументов и тела результата в событии нет. `turn_finish`, `turn_ask_human`, `turn_intermediate_reply` и `session_think` наружу не выходят.

`GET /v1/models` отдаёт один элемент: `id` — модель из конфига агента.

`GET /v1/responses/{id}` читает сохранённый объект, в том числе `in_progress`, и работает после перезапуска процесса. Нет файла — 404.

`POST /v1/responses/{id}/cancel` останавливает идущий ход, статус `cancelled`. Повтор после терминального статуса возвращает сохранённый объект и агента не трогает.

Индекс лежит рядом с каталогом тредов: `responses-index/<response_id>.json`. История хода — в обычном jsonl треда, `system="responses"`.

## Чего в этом релизе нет

Веб-UI, Conversations API, `background: true`, картинки и аудио во входе, клиентские `tools`, которые UI исполняет сам, `DELETE /v1/responses/{id}`, смена модели полем `model`, песочница Harbor. Вопрос человеку в канал отдельным событием не выходит: модель пишет его прозой и завершает ход.
