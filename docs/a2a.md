[Назад к README](../readme.md)

# A2A-фасад (`gc-a2a`)

Отдельный процесс `gc-a2a`. Другой агент открывает этого агента по
[Agent2Agent Protocol 1.0.0](https://a2a-protocol.org/v1.0.0/specification/)
и ставит задачу. Нормативный источник моделей — proto `lf.a2a.v1`
(`spec/a2a.proto` релиза 1.0.0). Петля тулов остаётся внутри ядра: пир присылает
текст и получает либо ответ, либо вопрос.

Ход такой. Клиент шлёт `SendMessage` и держит HTTP-запрос, пока задача не дойдёт
до ответа или до вопроса. Ответ лежит в артефакте задачи со состоянием
`TASK_STATE_COMPLETED`. Вопрос — в `status.message` со состоянием
`TASK_STATE_INPUT_REQUIRED`; следующее сообщение с тем же `taskId` продолжает
ту же задачу. Потока событий нет.

Это не `gc-chat` и не `gc-acp`. Для HTTP-чата — [chat.md](chat.md), для IDE —
[acp.md](acp.md).

Примеры в разделах 6 и 8 опубликованной спецификации местами ещё на именах 0.3
(`message/send`, поле `security`, состояния строчными буквами). Этот фасад
следует разделу 5.5 и proto: поля JSON в camelCase, значения перечислений —
имена ProtoJSON (`TASK_STATE_COMPLETED`, `ROLE_USER`).

## Срез операций

Один биндинг: JSON-RPC 2.0 по HTTP. В карточке одна запись
`supportedInterfaces`: `protocolBinding` = `JSONRPC`, `protocolVersion` = `1.0`.
gRPC и HTTP+JSON фасад не объявляет.

| Операция | Метод | Поведение |
|---|---|---|
| Карточка | `GET /.well-known/agent-card.json` | публичное описание агента, без JSON-RPC |
| Поставить задачу или ответить на вопрос | `SendMessage` | ждёт конца хода; в `result` всегда `task` |
| Прочитать задачу | `GetTask` | текущее состояние, история, артефакты |
| Список задач | `ListTasks` | курсор, фильтр по контексту и состоянию |
| Отменить ход | `CancelTask` | доводит живой ход до `TASK_STATE_CANCELED` |

`SendMessage` может вернуть `task` или голый `message`. Фасад всегда возвращает
`task`: у хода есть состояние, и ответ отделён от вопроса.

Методы вне среза разбираются и отклоняются кодом возможности. Ход при этом
не начинается.

| Метод | Код | Имя |
|---|---|---|
| `SendStreamingMessage`, `SubscribeToTask`, `GetExtendedAgentCard` | `-32004` | `UnsupportedOperationError` |
| `CreateTaskPushNotificationConfig`, `GetTaskPushNotificationConfig`, `ListTaskPushNotificationConfigs`, `DeleteTaskPushNotificationConfig` | `-32003` | `PushNotificationNotSupportedError` |
| любое другое имя | `-32601` | `MethodNotFoundError` |

Поле `configuration.taskPushNotificationConfig` в `SendMessage` даёт `-32003`
до создания задачи. Заголовок `A2A-Extensions` фасад игнорирует: обязательных
расширений в карточке нет.

## Транспорт

Слушает `127.0.0.1:8091`. Карточка указывает абсолютный URL интерфейса,
например `http://127.0.0.1:8091/rpc`. Вызов метода — `POST` на этот URL,
`Content-Type: application/json`.

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "SendMessage",
  "params": {}
}
```

Каждый такой `POST` несёт заголовок `A2A-Version: 1.0`. Пустое или отсутствующее
значение спецификация велит читать как `0.3`. Фасад говорит только на `1.0`,
поэтому пустой заголовок и любая другая пара `Major.Minor` дают
`VersionNotSupportedError` (`-32009`). Допустима и query-форма `A2A-Version=1.0`;
при обоих источниках берётся заголовок.

Разобранный JSON-RPC-запрос отвечает HTTP 200, ошибка протокола лежит в `error`.
Тело не JSON — HTTP 400 и код `-32700`. Объект без `jsonrpc`, `method` или `id` —
код `-32600`.

Непустой `a2a.auth_token` требует `Authorization: Bearer <token>` на `/rpc`.
Карточка при этом остаётся открытой и объявляет схему. Чужая или пустая
авторизация — HTTP 401 и JSON-RPC код `-32000`, сообщение `Unauthorized`.
Пустой токен на loopback — сервер без авторизации, в карточке нет
`securitySchemes`. Не-loopback (`0.0.0.0`, LAN и т.п.) при пустом токене —
ошибка старта до bind.

Поле `tenant` в параметрах фасад не принимает: в интерфейсе карточки его нет.
Непустое значение — `-32602`.

## Карточка

`GET /.well-known/agent-card.json` отдаёт JSON карточки. Авторизация на этот
путь не распространяется.

```json
{
  "name": "GigaCode",
  "description": "Универсальный агент GigaCode",
  "supportedInterfaces": [
    {
      "url": "http://127.0.0.1:8091/rpc",
      "protocolBinding": "JSONRPC",
      "protocolVersion": "1.0"
    }
  ],
  "version": "0.4.0",
  "capabilities": {
    "streaming": false,
    "pushNotifications": false,
    "extendedAgentCard": false
  },
  "defaultInputModes": ["text/plain"],
  "defaultOutputModes": ["text/plain"],
  "skills": [
    {
      "id": "agent",
      "name": "Агент",
      "description": "Принимает текстовую задачу и возвращает текстовый ответ или уточняющий вопрос.",
      "tags": ["agent"]
    }
  ]
}
```

При заданном токене к карточке добавляются схема и требование:

```json
{
  "securitySchemes": {
    "bearer": {
      "httpAuthSecurityScheme": { "scheme": "Bearer" }
    }
  },
  "securityRequirements": [
    { "schemes": { "bearer": {} } }
  ]
}
```

`name`, `description` и текст скилла читаются из секции `a2a`. Подписи карточки
нет.

## Сообщение и части

Вход `SendMessage.params`:

| Поле | Обязательность | Смысл в фасаде |
|---|---|---|
| `message` | да | реплика клиента |
| `message.messageId` | да | идемпотентность: повтор того же id с теми же `taskId` и `contextId` возвращает уже созданную задачу и второй ход не запускает. Другие `taskId` или `contextId` — `-32602`, чужая задача не отдаётся |
| `message.role` | да | только `ROLE_USER` |
| `message.parts` | да | одна или несколько частей, все с полем `text` |
| `message.contextId` | нет | продолжение диалога; сервер подставляет свой, если поля нет |
| `message.taskId` | нет | продолжение задачи, которая ждёт вопрос |
| `configuration.returnImmediately` | нет | `false` по умолчанию: ждать ответа или вопроса |
| `configuration.historyLength` | нет | сколько последних сообщений задачи положить в ответ |
| `configuration.acceptedOutputModes` | нет | если список задан и в нём нет `text/plain` — `-32005` |
| `metadata` | нет | фасад не читает |

Часть обязана содержать ровно одно из `text`, `raw`, `url`, `data`. Фасад
принимает только `text`. `mediaType` пустой или `text/plain`. Иное содержимое
или иной media type — `ContentTypeNotSupportedError` (`-32005`), задача не
создаётся.

`message.metadata.cwd` — абсолютный путь корня нового диалога. Без него берётся
`a2a.launch_root`. На продолжении диалога чужой `cwd` отклоняется: корень
задан первым ходом.

## Диалог, задача, ход

`contextId` — диалог, один тред ядра с системным id `a2a`. `taskId` — один ход
этого диалога. Оба идентификатора выдаёт сервер; клиентский id новой задачи
не принимается.

Правила `SendMessage`:

- нет `contextId` и нет `taskId` — новый диалог и новая задача;
- есть `contextId`, нет `taskId` — новая задача в этом диалоге, если все его
  задачи в терминальном состоянии;
- есть `taskId` — ход продолжается, только если задача в
  `TASK_STATE_INPUT_REQUIRED`; `contextId`, если он передан, обязан совпасть
  с задачей, иначе `-32602`;
- неизвестный `contextId` — `-32602`, новый диалог с этим id не создаётся;
- неизвестный `taskId` — `TaskNotFoundError` (`-32001`);
- `taskId` задачи в `COMPLETED`, `FAILED`, `CANCELED` или `REJECTED` —
  `UnsupportedOperationError` (`-32004`);
- в диалоге уже идёт ход (`TASK_STATE_WORKING`) — `-32004`, второй ход не
  ставится в очередь;
- диалог ждёт ответа на вопрос, а новое сообщение пришло без этого `taskId` —
  `-32602`.

Один диалог — один живой агент и один ход за раз. Разные диалоги идут
параллельно: у каждого свой агент и свой тред. Две задачи сразу возможны
как два `contextId`. Две задачи в одном `contextId` одновременно невозможны:
второй `SendMessage` получает `-32004`.

33-й диалог не отбивается. После создания агента фасад закрывает самых давних
простаивающих, пока живых не станет 32, как у Responses. Ход в
`TASK_STATE_WORKING` не закрывается. Если все 32 сейчас в ходе, новый
запрос всё равно стартует, и живых становится больше 32, пока кто-то не
освободится. Закрытый простаивающий диалог продолжается из файла треда.

`returnImmediately: false` (и отсутствие поля) держит запрос, пока состояние не
станет терминальным или `TASK_STATE_INPUT_REQUIRED`. `returnImmediately: true`
сразу возвращает задачу в `TASK_STATE_WORKING`; клиент дальше читает её через
`GetTask`. Отмены и вопроса это не отменяет.

Состояния, которые фасад записывает:

| Состояние | Когда |
|---|---|
| `TASK_STATE_WORKING` | ход выполняется |
| `TASK_STATE_INPUT_REQUIRED` | ход остановился на `turn_ask_human`; текст вопроса в `status.message` |
| `TASK_STATE_COMPLETED` | ход закончился ответом; текст в артефакте |
| `TASK_STATE_FAILED` | ход упал или остановился по лимиту шагов, токенов или вызовов инструментов; пояснение в `status.message`, артефакта нет |
| `TASK_STATE_CANCELED` | ход отменён через `CancelTask` |

`TASK_STATE_REJECTED`, `TASK_STATE_AUTH_REQUIRED` и `TASK_STATE_UNSPECIFIED`
фасад не выставляет. Отказ принять сообщение — ошибка JSON-RPC до появления
задачи. `status.timestamp` — UTC, ISO 8601 с суффиксом `Z`.

Вопрос — сообщение `role: ROLE_AGENT` с текстовой частью. Оно же попадает в
`history`. Ответ в историю сообщением не копируется: спецификация относит
результат к артефактам. У артефакта один `artifactId` на задачу, часть `text`,
`mediaType` = `text/plain`. Трейс тулов, мысли и промежуточные реплики в задачу
не входят.

`historyLength` в `SendMessage`, `GetTask` и `ListTasks`: поле отсутствует —
вся история этой задачи; `0` — поле `history` в ответе опущено; положительное —
не больше стольких последних сообщений.

## Пример: ответ

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "SendMessage",
  "params": {
    "message": {
      "messageId": "0b1c2d3e-4f50-4617-8901-23456789abcd",
      "role": "ROLE_USER",
      "parts": [{ "text": "Сколько файлов в src?" }]
    }
  }
}
```

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "task": {
      "id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
      "contextId": "2f1e0d3c-5b4a-4987-8c6d-112233445566",
      "status": {
        "state": "TASK_STATE_COMPLETED",
        "timestamp": "2026-10-12T15:04:05.000Z"
      },
      "artifacts": [
        {
          "artifactId": "a1b2c3d4-e5f6-4789-a012-3456789abcde",
          "parts": [{ "text": "В src 42 файла.", "mediaType": "text/plain" }]
        }
      ],
      "history": [
        {
          "messageId": "0b1c2d3e-4f50-4617-8901-23456789abcd",
          "role": "ROLE_USER",
          "parts": [{ "text": "Сколько файлов в src?" }]
        }
      ]
    }
  }
}
```

Следующая реплика того же диалога — новый `messageId`, тот же `contextId`,
без `taskId`. Это новая задача на том же треде.

## Пример: вопрос

Пока задача в `TASK_STATE_INPUT_REQUIRED`, агент ждёт текст и ход не крутит.
Продолжение — новое сообщение с тем же `taskId`:

```json
{
  "message": {
    "messageId": "11111111-2222-4333-8444-555555555555",
    "taskId": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
    "contextId": "2f1e0d3c-5b4a-4987-8c6d-112233445566",
    "role": "ROLE_USER",
    "parts": [{ "text": "Только *.py" }]
  }
}
```

Ответ на этот запрос снова либо `TASK_STATE_COMPLETED` с артефактом, либо
новый `TASK_STATE_INPUT_REQUIRED`.

## GetTask, ListTasks, CancelTask

`GetTask`: обязателен `id`. Неизвестный id — `-32001`.

`ListTasks` возвращает задачи этого процесса, видимые без разделения по
клиентам. Сортировка — по `status.timestamp`, сначала свежие. `pageSize`
по умолчанию 50, допустимый диапазон 1…100. `nextPageToken` есть всегда;
пустая строка означает последнюю страницу. `pageSize` и `totalSize` в ответе
обязательны. `includeArtifacts: false` или отсутствие поля — у задач в списке
нет ключа `artifacts`. Фильтры: `contextId`, `status` (имя состояния ProtoJSON),
`statusTimestampAfter`, `historyLength`.

`CancelTask`: обязателен `id`. Задача в `WORKING` или `INPUT_REQUIRED`
переводится в `TASK_STATE_CANCELED`; для идущего хода запрос ждёт, пока
прогон после `stop()` действительно закончится. До этого диалог занят, и
следующий `SendMessage` в тот же `contextId` не стартует второй прогон.
Уже терминальная задача — `TaskNotCancelableError` (`-32002`). Повторная
отмена уже отменённой задачи возвращает её же: задачи фасад не вычищает сразу
после отмены. Неизвестный id — `-32001`.

## Ошибки JSON-RPC

| Код | Когда |
|---|---|
| `-32700` | тело не JSON |
| `-32600` | это не запрос JSON-RPC 2.0 |
| `-32601` | метод вне таблицы среза и вне таблицы отказа по возможности |
| `-32602` | кривые параметры: нет `messageId`, чужой `role`, пустые части, битый `contextId`, несовпадение `contextId` и `taskId`, `pageSize` вне 1…100 |
| `-32603` | сбой хода, который не удалось записать как `TASK_STATE_FAILED` |
| `-32000` | нет или неверный Bearer |
| `-32001` | нет задачи |
| `-32002` | отмена терминальной задачи |
| `-32003` | push-конфиг или push-метод |
| `-32004` | стрим, подписка, extended card, сообщение в терминальную задачу, второй ход диалога |
| `-32005` | часть не `text` или выход не `text/plain` |
| `-32009` | `A2A-Version` не `1.0` |

В `error.data` один объект с `@type` = `type.googleapis.com/google.rpc.ErrorInfo`,
полем `reason` (имя ошибки из таблицы спецификации, для `-32602` —
`INVALID_PARAMS`) и `domain` = `a2a-protocol.org`.

## Канал

Секцию `a2a` читает только этот процесс. `os_backend` обязан быть `local`:
рабочий каталог хода — путь на машине процесса. Иной бэкенд завершает процесс
до bind.

| Ключ | Дефолт | Смысл |
|---|---|---|
| `a2a.host` | `127.0.0.1` | адрес bind |
| `a2a.port` | `8091` | порт |
| `a2a.public_url` | `null` | URL в карточке; пусто — из `host`:`port`. Нужен при bind на `0.0.0.0` |
| `a2a.auth_token` | `""` | пусто — без Bearer |
| `a2a.launch_root` | `null` | cwd нового диалога, если в сообщении нет абсолютного `metadata.cwd` |
| `a2a.name` | `GigaCode` | `name` карточки |
| `a2a.description` | краткое описание агента | `description` карточки и скилла |
| `a2a.text_answer_mode` | `allow` | проза без tool-call |
| `a2a.finish_gate_mode` | `off` | судья на `turn_finish` |
| `a2a.todo_reminder_steps` | `0` | напоминания `session_todo` |

`turn_ask_human` остаётся в схеме: его текст становится вопросом задачи.
`turn_intermediate_reply` снимается — прогресс пиру не отдаётся.
Подтверждение write/command guard в терминал сервера не уходит: порт
отвечает отказом, ход не зависает в `WORKING` на `input()`.

Остановка процесса останавливает идущие ходы сразу, не дожидаясь, пока
блокирующий `SendMessage` сам закроет соединение. Ожидание закрытия
соединений ограничено.

Клиент, которым этот агент сам ходит к чужим карточкам, в фасад не входит.

POST `/rpc` принимается только с `Content-Type: application/json` (параметр
`charset` допустим). Иной тип — `415` и `ContentTypeNotSupportedError`: так
браузер не отправит запуск агента без preflight. Заголовок `Origin`, если он
есть, обязан совпасть с origin bind (`http://` + `a2a.host` + `:` + `a2a.port`);
чужой origin — `403`. На loopback-бинде (`127.0.0.1`, `localhost`) заголовок
`Host` тоже только loopback: чужой домен, перерезолвленный в `127.0.0.1`,
получает `400`. У `::1` проверка `Host` выключена. Клиент без `Origin`
(curl, SDK) эти правила не задевают.
