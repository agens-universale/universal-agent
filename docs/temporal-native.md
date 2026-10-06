# Нативный Temporal runtime

Нативный путь запускает настоящий `AgentExecutor` и `ThreadSession` из `gc-core`.
Это opt-in entrypoint; локальный режим остаётся по умолчанию.

Полный [native quickstart](temporal-configuration.md#native-quickstart) содержит
пример с `${ENV:...}`, установку optional extra и команды для отдельного workspace.

## Размещение worker

Текущий native worker обслуживает workflow, model и resource queues одним
процессом. Он привязан к одной рабочей области: запустите процесс из каталога,
указанного в `profiles.<name>.workspace`. Worker проверяет совпадение каталогов
и не вызывает `os.chdir`. Для другого workspace запустите другой процесс с
отдельной resource queue. Не подключайте несколько процессов к одной очереди
ресурсов, владеющей живыми MCP-сессиями или фоновыми процессами.

Конфигурация worker — JSON. `core` содержит обычный структурированный `AppConfig`,
без отдельного сокращённого набора agent options:

```json
{
  "address": "localhost:7233",
  "namespace": "default",
  "workflow_queue": "ua-native-workflow",
  "resource_queue": "ua-native-resource-project",
  "model_queue": "ua-native-model",
  "worker_id": "project-host",
  "state_dir": "/absolute/state",
  "thread_stores": {
    "files": {"kind": "file", "path": "/absolute/state/threads"}
  },
  "profiles": {
    "project": {
      "workspace": "/absolute/project",
      "store_id": "files",
      "core": {
        "backend": {
          "api_mode": "chat_completions",
          "completions_url": "http://localhost:8000/v1/chat/completions"
        },
        "agent": {"model": "configured-model"}
      }
    }
  }
}
```

История и метаданные по умолчанию хранятся в `FileThreadStore`. Явный
`{"kind": "sqlite", "path": "/absolute/state/threads.sqlite"}` выбирает
существующий SQLite backend для конкретного store. Файловый store в Temporal —
`DurableFileThreadStore`: те же файлы плюс служебная `.store/` (квитанции,
журнал прерванной записи, владелец). Своей базы у воркера нет: результаты
activities хранит история Temporal.

Workflow ссылается на профиль только по имени; конфигурацию каждая activity
берёт из текущего процесса воркера. Смена настроек или токена — перезапуск
воркера, живые сессии продолжают работу. Владелец контекста хода закреплён за
профилем и store.

## Запуск и управление

Из workspace профиля, указав абсолютный путь к checkout Universal Agent:

```sh
uv run --project /absolute/universal-agent --extra temporal \
  gc-temporal-native-worker --config /absolute/native-worker.json
```

Либо из другого каталога с установленным пакетом используйте
`gc-temporal-native-worker --config /absolute/native-worker.json` напрямую.
Фактический cwd процесса должен совпадать с workspace профиля.

Запуск клиента:

```sh
uv run --project /absolute/universal-agent --extra temporal \
  gc-temporal-native start --profile project \
  --system cli --thread example --store files \
  --resource-queue ua-native-resource-project \
  --prompt 'Прочитай README и опиши устройство проекта'
```

Команда печатает workflow ID. `status ID`, `result ID`, `stop ID` запрашивают
состояние, результат и остановку. `answer ID --request-id REQUEST --answer TEXT`
отвечает на запрос человека. Для сессии, ожидающей следующие сообщения, при
старте передайте `--keep-alive`; затем используйте
`send ID --message-id UNIQUE_ID --text TEXT` и `close ID`.

Обычные файловые и model операции восстанавливают контекст из закреплённого
профиля после перезапуска worker. MCP discovery выполняется внутри задачи
владельца ресурсов; фоновые команды получают такого владельца лениво. После
перезапуска worker'а живые ресурсы потеряны: модель получает `ResourceOwnerLost`
ошибкой инструмента, эффект не повторяется, следующий ход открывает нового
владельца. Подробности — [архитектура, раздел 8](temporal-architecture.md#8-ошибки-повторы-и-неизвестный-исход).
