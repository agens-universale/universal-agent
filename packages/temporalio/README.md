# gc-core-temporalio

Агент ядра `gc-core` в [Temporal](https://temporal.io): ход переживает падение
процесса, запрос к модели повторяется при сбое провайдера, ожидание человека не
ограничено по времени. Агент тот же, что локально (`AsyncAgent`), — Temporal
меняет только то, где исполняются модель, инструменты и хранилище.

- Воркер: нативные `Client`/`Worker` из `temporalio` с `AgentPlugin` — или
  `gc-temporal-worker --config worker.json` ([пример](examples/worker.json)).
- Приложение: `AsyncAgent.create(settings, durable=TemporalDurability(...))`,
  `AgentSession` или `gc-temporal`.

Документация — [docs/temporal.md](../../docs/temporal.md).
