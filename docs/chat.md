[Назад к README](../readme.md)

# HTTP-фасады: `gc-serve chat`

Поверхность `chat` сервера `gc-serve` ([server.md](server.md)). Внешнее
приложение (или встроенная страница чата) открывает ход агента по HTTP и читает
стрим. Петля тулов остаётся внутри ядра: клиент присылает текст и не исполняет
тулы сам.

На одном порту два пути — клиент выбирает контракт:

| Путь | Контракт | Диалог |
|---|---|---|
| `POST /v1/responses` | близок к OpenAI Responses API | продолжение через `previous_response_id`, тред `system="responses"` |
| `POST /v1/chat/completions` | OpenAI-совместимый Chat Completions | клиент каждый раз шлёт `messages`; тред хода эфемерный; трейс внутри `<think>` |

У вебхука другой контракт: он отвечает `started` и просит опрашивать статус.
Здесь соединение живёт, пока жив ход.

Для IDE по Agent Client Protocol — [acp.md](acp.md) (`gc-acp`), не эта поверхность.

## Установка и запуск

Поверхность — часть пакета `gc-server` (FastAPI, uvicorn); полный `gc-ua` его
включает.

```bash
uv tool install -e packages/server
# без клона:
uv tool install "gc-server @ git+https://gitverse.ru/gigacode-ai/universal-agent#subdirectory=packages/server"
```

В корпоративной сети к `uv tool install` добавляйте `--system-certs`.

```bash
gc-serve chat --config ./config.yml
gc-serve chat --config ./config.yml --profile <имя> --server-config ./config_server.yml
gc-serve chat a2a webhook        # вместе с другими поверхностями, одним процессом
```

Без `--config` поиск конфига тот же, что у остальных команд. Конфиг не читается —
сообщение в stderr, код выхода 1, порт не открывается.

`os_backend` обязан быть `local`. `harbor` и неизвестный бэкенд — отказ старта
до bind: рабочий каталог хода — путь на машине этого процесса.

По умолчанию слушает `127.0.0.1:8090`. Логи — в stderr. Пустой токен даёт на
старте строку: поверхность без авторизации. Там же перечисляются ручки.

Страница чата: `http://127.0.0.1:8090/` (`GET /`).

## Секция `chat` в config_server.yml

Аннотированный образец — секция `chat` в
[`demo_config_server.yml`](../demo_config_server.yml). Без секции — умолчания.

| Ключ | Дефолт | Смысл |
|---|---|---|
| `chat.host` | `server.host` (`127.0.0.1`) | Адрес bind |
| `chat.port` | `8090` | Порт |
| `chat.auth_token` | `server.auth_token` | Пусто — без Bearer. Иначе каждый `/v1/` несёт `Authorization: Bearer <token>` |
| `chat.cors_origins` | `[]` | Пусто — CORS выключен. Непусто — только эти origin. `"*"` из списка выбрасывается |
| `chat.launch_root` | `null` | Корень ходов: cwd нового хода без абсолютного `metadata.cwd`; `metadata.cwd` — только внутри, иначе 400; пусто — каталог процесса |
| `chat.text_answer_mode` | `allow` | `deny` / `judge` / `allow` |
| `chat.finish_gate_mode` | `off` | `off` / `on` / `smart` (в YAML — в кавычках) |
| `chat.todo_reminder_steps` | `0` | Напоминания `session_todo`; `0` — выкл |
| `chat.agent_md_path` | — | Роль агента этой поверхности (слой L0) |
| `chat.agent_overrides` | `{}` | Правки настроек агента поверх общих `agent_overrides` |

Секция `chat_api` в config.yml больше не читается: `gc-cli` и TUI покажут
уведомление, а `gc-serve chat` при ней не стартует — иначе её `auth_token`
пропал бы молча. Перенесите ключи в секцию `chat` config_server.yml.

После загрузки поверхность перекрывает `text_answer_mode` и судью `turn_finish`
значениями из секции. `turn_ask_human` и `turn_intermediate_reply` снимаются из
схемы: вопрос и прогресс — прозой ответа. У каждого HTTP-пути свой файл special
instructions; свой `paths.chat_completions_special_instructions_path` на этой
поверхности не читается.

Защита от CSRF / DNS rebinding действует всегда (в т.ч. проверка `Host` на loopback).
Токен её не заменяет. Полная таблица ключей, cwd и ошибок — в
[responses.md](responses.md#конфиг).

Треды Responses и Chat Completions лежат в хранилище из `storage` config.yml
([server.md](server.md#хранилище-storage-в-configyml)).

## Responses API

Один запрос — один ход. Продолжение диалога — `previous_response_id` прошлого хода.

```bash
curl -s http://127.0.0.1:8090/v1/responses \
  -H 'Content-Type: application/json' \
  -d '{"model":"agent","input":"привет","stream":false}'
```

Продолжение диалога, ход которого ещё идёт, — `409` (на этом инстансе —
«turn already running», на другом — по аренде треда в общем хранилище:
«turn already running on another instance»).

`model` в ответ копируется как строка; в роутер моделей фасад её не передаёт —
модель хода из конфига агента. `stream: true` — SSE до терминального события.

Полный контракт запроса, объекта ответа, cancel, индекса, диалогов и артефактов —
[responses.md](responses.md).

## Chat Completions API

`POST /v1/chat/completions` — тот же порт и та же секция `chat` (host, port,
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
  тексту — один текст, `comment` — `заметка`. Текст более ранних ответов
  модели хода уходит в трейс `<think>` на каждом новом запросе к модели, а
  финальная проза после `</think>` всегда несёт конец хода по правилу (текст
  и/или отчёт): она единственная память хода, которую клиент присылает
  обратно. Поэтому отчёт, равный прозе, уже ушедшей в `<think>`, всё равно
  виден в ответе (правило — docs/tui.md, «Конец хода»);
- продолжение диалога — снова полный `messages` со стороны клиента, не
  `previous_response_id`;
- список диалогов `/v1/dialogs` показывает только треды Responses.

`GET /v1/models` отдаёт модель из конфига агента (и зарезервированные id для
прямого прокси, если они настроены — см. код `gc_server.chat.model_route`).

## Режимы разрешений

Поверхность `chat` канал о себе не объявляет: есть ли человек, определяет
автопроверка ядра. Она снимает `turn_ask_human`, поэтому автопроверка человека не находит, и
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
| HTTP-стрим хода для своего UI / интеграции | `gc-serve chat` (этот документ) |
| Агент как пир A2A | `gc-serve a2a` — [a2a.md](a2a.md) |
| Вебхук + опрос статуса / swarm | `gc-serve webhook` / `gc-serve swarm` — [server.md](server.md) |

## Пакет

| | |
|---|---|
| Дистрибутив | `gc-server` (`packages/server`), модуль `gc_server.chat` |
| Зависит от | `gc-core`, `gc-stores`, FastAPI, uvicorn |
| Команда | `gc-serve chat` → `gc_server.cli:main` |
| В мета `gc-ua` | да |

Раскладка дистрибутивов — [packaging.md](packaging.md).
