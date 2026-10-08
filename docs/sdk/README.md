[Назад к README](../../readme.md)

# SDK: агент в своём приложении

Пакет `gc-core` — это агент как библиотека: тот же цикл, инструменты, MCP,
субагенты и сжатие контекста, что у CLI и TUI, но запускает его ваш код.
Интерфейс асинхронный: `gc_core.aio.AsyncAgent`.

| Страница | Про что |
|---|---|
| эта | быстрый старт, настройки, треды, картинки, вопрос человеку, логи |
| [Хранилище тредов](thread-store.md) | где лежит история: файлы, своя база, S3; как написать своё |
| [Контракт хранилища тредов](thread-store-contract.md) | подробно для авторов хранилищ: методы, свойства, проверки |
| [Хранилище агента](agent-store.md) | промпты, AGENTS.md, скиллы и субагенты из своего источника |
| [Temporal](../temporal.md) | тот же агент в durable-исполнении |
| [Асинхронное ядро](../async.md) | как устроено ядро: потоки, порты, свои инструменты и бэкенды |

## Быстрый старт

```python
import asyncio

from gc_core.aio import AsyncAgent
from gc_core.config import load_settings


async def main():
    settings = load_settings("~/.gc/config.yml")
    async with await AsyncAgent.create(settings) as agent:
        answer = await agent.run("почини падающие тесты", thread_id="t1")
        print(answer)


asyncio.run(main())
```

`run` возвращает ответ хода. `async with` закрывает агента: сетевой пул и
MCP-сессии, которые он поднял.

## Настройки

`AsyncAgent.create` принимает типизированные настройки, словарь — нет:

- `load_settings(path)` — из файлов, с профилями, слоями и подстановкой
  переменных окружения и файлов-секретов; каждое значение помнит, откуда пришло;
- `default_settings(**{"agent.model": "…"})` (`gc_core.settings`) — без файла,
  от умолчаний.

Настройки неизменяемы. Правка даёт новый проверенный объект:

```python
from gc_core.settings import default_settings, override_settings

settings = default_settings(**{"agent.model": "my-model", "agent.max_steps": 20})
strict = override_settings(settings, {"security.command_moderation.mode": "deny"})
loaded = load_settings("config.yml").with_overrides({"agent.max_steps": 5})
```

Неизвестный ключ или негодное значение — `ValidationError` сразу.
Все ключи — [Конфигурация](../configuration.md).

## Треды

```python
await agent.run("какой у нас бюджет?", thread_id="crm-42")
await agent.run("а если урезать на 10%?", thread_id="crm-42")   # продолжение
await agent.run("…", thread_id="crm-42", system="crm")            # своё пространство
```

- `thread_id` — тред: история, память сессии, счётчики токенов. Без него
  каждый ход — с чистого листа, ничего не сохраняется.
- `system` — пространство тредов приложения (по умолчанию `"sdk"`): два
  приложения в одном хранилище не видят треды друг друга.
- Где лежит история — решает хранилище тредов: по умолчанию файлы в
  `paths.threads_data_path`, своё — `AsyncAgent.create(settings,
  thread_store=…)`. Подробно — [Хранилище тредов](thread-store.md).

## Живой вывод, сообщения посреди хода, остановка

```python
async for kind, text in agent.stream("объясни этот модуль", thread_id="t1"):
    if kind == "answer":
        print(text, end="", flush=True)    # "think" — рассуждение, "reset" — начать заново
print(agent.last_answer)                     # итоговый ответ хода

agent.send({"role": "user", "content": "стоп, сначала линтер"})   # в идущий ход
agent.stop()                                                       # кооперативно
```

## Картинки

```python
from gc_core.attachments.model import ORIGIN_USER, chip_text, user_label
from gc_core.attachments.store import ImageStore, amake_attachment

store = ImageStore.for_config(settings)
shot = await amake_attachment(store, "screen.png", origin=ORIGIN_USER, label=user_label(1))
answer = await agent.run(
    f"{chip_text(1)} почему кнопка съехала?", thread_id="t1", attachments=[[shot]]
)
```

Картинку ужимают и кладут во временный карман процесса; в тред её копирует
ход. Хранилище тредов с файлами треда держит картинки у себя, а не на диске.

## Вопрос человеку

Без своего обработчика инструмент `turn_ask_human` читает stdin — это поведение
CLI. Приложение ставит свой порт: функцию или корутину, которая получает вопрос
и возвращает ответ (строку или `HumanAnswer` с картинками из
`gc_core.tools.turn.ask_human`):

```python
async def ask(question: str) -> str:
    return await my_ui.ask_user(question)

agent.executor._ctx.ask_human = ask
```

Так подключают свой вопрос встроенные фронтенды (ACP, chat). Под Temporal
вопроса человеку по умолчанию нет — см. [Temporal](../temporal.md).

## Логи

```python
agent = await AsyncAgent.create(settings, log_handlers=[MyHandler()])
```

Лог прогона — обычный `logging.Logger`. Обработчик приложения добавляется к
файлу прогона; без файла — `logging.save_logs: false`, в корневой `logging` —
`logging.propagate: true`. Секреты в записях маскируются при
`logging.redact_secrets.enabled: true`.

## Что ещё можно передать в `create`

| Параметр | Зачем |
|---|---|
| `thread_store` | своё хранилище тредов — [подробно](thread-store.md) |
| `agent_store` | своё хранилище агента: промпты, AGENTS.md, скиллы, субагенты — [подробно](agent-store.md) |
| `log_handlers` | свои обработчики лога прогона |
| `tools_cache`, `os_backend` | общий кэш MCP-дискавери для пачки агентов, свой бэкенд исполнения — [Асинхронное ядро](../async.md) |
| `on_stream_delta` | второй слушатель живых дельт (метрика, лог) |
| `durable` | ходы исполняет Temporal — [Temporal](../temporal.md) |

Рабочий пример — [`packages/core/examples/async_embed.py`](../../packages/core/examples/async_embed.py).
