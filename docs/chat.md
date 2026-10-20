[Назад к README](../readme.md)

# HTTP-фасады (`gc-chat`)

Отдельный процесс `gc-chat`. Внешнее приложение (или встроенная страница чата)
открывает ход агента по HTTP и читает стрим. Петля тулов остаётся внутри ядра:
клиент присылает текст и не исполняет тулы сам.

На одном порту два пути — клиент выбирает контракт:

| Путь | Контракт | Диалог |
|---|---|---|
| `POST /v1/responses` | близок к OpenAI Responses API | продолжение через `previous_response_id`, тред `system="responses"` |
| `POST /v1/chat/completions` | OpenAI-совместимый Chat Completions | клиент каждый раз шлёт `messages`; тред хода эфемерный; трейс внутри `<think>` |

Это не `gc-serve`. У вебхука другой контракт: он отвечает `started` и просит
опрашивать статус. Здесь соединение живёт, пока жив ход. Флага у `gc-cli` и
маршрутов в `gc-serve` нет.

Для IDE по Agent Client Protocol — [acp.md](acp.md) (`gc-acp`), не этот процесс.

## Установка и запуск

Зависимости FastAPI и uvicorn живут в дистрибутиве `gc-chat`. Базовая установка
CLI их не ставит. Полный `gc-ua` команду `gc-chat` тоже объявляет.

```bash
uv tool install -e packages/chat
# без клона:
uv tool install "gc-chat @ git+https://gitverse.ru/gigacode-ai/universal-agent#subdirectory=packages/chat"
```

В корпоративной сети к `uv tool install` добавляйте `--system-certs`.

```bash
gc-chat --config ./config.yml
gc-chat --config ./config.yml --profile <имя>
```

Без `--config` поиск конфига тот же, что у остальных команд. Конфиг не читается —
сообщение в stderr, код выхода 1, порт не открывается.

`os_backend` обязан быть `local`. `harbor` и неизвестный бэкенд завершают процесс
с кодом 1 до bind: рабочий каталог хода — путь на машине этого процесса.

По умолчанию слушает `127.0.0.1:8090`. Логи — в stderr. Пустой `chat_api.auth_token`
даёт на старте строку: сервер без авторизации. Там же процесс перечисляет ручки.

Страница чата: `http://127.0.0.1:8090/` (`GET /`).

## Конфиг `chat_api`

Секция — часть общей схемы `config.yml`. Аннотированный образец —
[`demo_config_chat.yml`](../demo_config_chat.yml): скопируйте блок в свой конфиг
агента. В шаблон CLI и в `demo_config.yml` секция не входит; без неё схема
подставляет дефолты, и процесс стартует.

Ключи читает только `gc-chat`. `gc-cli` и TUI их не применяют.

| Ключ | Дефолт | Смысл |
|---|---|---|
| `chat_api.host` | `127.0.0.1` | Адрес bind |
| `chat_api.port` | `8090` | Порт |
| `chat_api.auth_token` | `""` | Пусто — без Bearer. Иначе каждый `/v1/` несёт `Authorization: Bearer <token>` |
| `chat_api.cors_origins` | `[]` | Пусто — CORS выключен. Непусто — только эти origin. `"*"` из списка выбрасывается |
| `chat_api.launch_root` | `null` | cwd нового хода, если в запросе нет абсолютного `metadata.cwd` |
| `chat_api.text_answer_mode` | `allow` | `deny` / `judge` / `allow` |
| `chat_api.finish_gate_mode` | `off` | `off` / `on` / `smart` (в YAML — в кавычках) |
| `chat_api.todo_reminder_steps` | `0` | Напоминания `session_todo`; `0` — выкл |

После загрузки фасад перекрывает `text_answer_mode` и судью `turn_finish` значениями
из секции. `turn_ask_human` и `turn_intermediate_reply` снимаются из схемы: вопрос
и прогресс — прозой ответа. У каждого HTTP-пути свой файл special instructions;
свой `paths.chat_completions_special_instructions_path` на этом процессе не читается.

Защита от CSRF / DNS rebinding действует всегда (в т.ч. проверка `Host` на loopback).
Токен её не заменяет. Полная таблица ключей, cwd и ошибок — в
[responses.md](responses.md#конфиг).

## Responses API

Один запрос — один ход. Продолжение диалога — `previous_response_id` прошлого хода.

```bash
curl -s http://127.0.0.1:8090/v1/responses \
  -H 'Content-Type: application/json' \
  -d '{"model":"agent","input":"привет","stream":false}'
```

`model` в ответ копируется как строка; в роутер моделей фасад её не передаёт —
модель хода из конфига агента. `stream: true` — SSE до терминального события.

Полный контракт запроса, объекта ответа, cancel, индекса, диалогов и артефактов —
[responses.md](responses.md).

## Chat Completions API

`POST /v1/chat/completions` — тот же порт и та же секция `chat_api` (host, port,
токен, CORS, режимы хода).

Отличия от Responses:

- клиент каждый раз присылает массив `messages`; история до последнего user
  сеется во временный тред и после хода удаляется;
- трейс (мысли, карточки тулов) уходит внутри блока `<think>…</think>`, затем
  финальная проза; если ход закрыт `turn_finish`, то и текст ответа модели, и
  запись `* **итог:**` / `* **заметка:**` (метка своей строкой, под ней через
  пустую строку отчёт и `comment` отдельными абзацами с отступом в два
  пробела, как у docs/acp.md): текст и другой отчёт — запись
  `итог`; текста нет — отчёт сам ответ, `comment` — `заметка`; отчёт равен
  тексту — один текст, `comment` — `заметка`. Отчёт, равный тексту, уже
  отданному раньше в этом же ответе, не повторяется (правило — docs/tui.md,
  «Конец хода»);
- продолжение диалога — снова полный `messages` со стороны клиента, не
  `previous_response_id`;
- список диалогов `/v1/dialogs` показывает только треды Responses.

`GET /v1/models` отдаёт модель из конфига агента (и зарезервированные id для
прямого прокси, если они настроены — см. код `gc_chat.model_route`).

## Режимы разрешений

`gc-chat` канал о себе не объявляет: есть ли человек, определяет автопроверка ядра.
`gc-chat` снимает `turn_ask_human`, поэтому автопроверка человека не находит, и
любой вопрос режима разрешений или защиты заканчивается отказом «спросить некого» (`manual`, пауза судьи в `auto`, запись в
каталог `.git`, скиллы и настройки инструментов). Слова пользователя для судьи
действий в `auto` — свои, а не «запрос внешней системы»: клиент HTTP-фасада — сам
пользователь. Подробности — [configuration.md](configuration.md#securitypermissions-режимы-разрешений).

## Когда что выбирать

| Задача | Команда |
|---|---|
| Чат в терминале | `gc-ua` |
| Разовый запрос / скрипт | `gc-cli` |
| IDE (VS Code ACP) | `gc-acp` — [acp.md](acp.md) |
| HTTP-стрим хода для своего UI / интеграции | `gc-chat` (этот документ) |
| Вебхук + опрос статуса / swarm | `gc-serve` — [server.md](server.md) |

## Пакет

| | |
|---|---|
| Дистрибутив | `gc-chat` (`packages/chat`) |
| Зависит от | `gc-core`, FastAPI, uvicorn |
| Команда | `gc-chat` → `gc_chat.main:main` |
| В мета `gc-ua` | да |

Раскладка дистрибутивов — [packaging.md](packaging.md).
