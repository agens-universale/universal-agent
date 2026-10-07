# gc-core-temporalio

Опциональный Temporal runtime для Universal Agent. Native entrypoints запускают
настоящие `AgentExecutor` и `ThreadSession` из `gc-core`; локальный режим остаётся
по умолчанию. Native поддерживает Chat Completions и Responses через общий
модельный порт, `DurableFileThreadStore` по умолчанию и явно выбранный
`SQLiteThreadStore`; своё хранилище проверяется durable-конформансом
(`gc_testkit.durable_thread_store_conformance`).

- [Native quickstart, конфигурация и управление](../../docs/temporal-configuration.md#native-quickstart)
- [Пример worker.json](examples/worker.json) — `${ENV:...}`, без секретов.
- [Архитектура, проверки и ограничения](../../docs/temporal-architecture.md)
- [Размещение worker и восстановление](../../docs/temporal.md)

`gc-temporal-worker` / `gc-temporal` используют Temporal workflow; TUI,
`gc-cli` и gc-server работают только локально.

Сейчас native worker обслуживает workflow/model/resource queues одним процессом
для одного workspace. Его cwd должен совпадать с `profiles.<name>.workspace`;
worker проверяет это и не вызывает `os.chdir`. Отдельные очереди не означают
поддержку раздельных model/resource процессов. Своей базы у воркера нет
(результаты activities хранит история Temporal); каталог тредов (`state_dir`)
размещайте вне workspace. Потерянные MCP/background владельцы не
пересоздаются автоматически; неопределённые эффекты требуют подтверждённого
результата и явного операторского решения.
