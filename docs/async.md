[Назад к README](../readme.md)

# Асинхронное ядро: два API

Ядро `gc-core` — асинхронное внутри. У каждой операции одна реализация,
асинхронная; синхронные точки входа сохранены и **не депрекейтятся**.

## Какой API выбирать

| Ваше приложение | Что звать |
|---|---|
| Скрипт, CLI, синхронный код | `AgentExecutor.run(...)` — как раньше |
| FastAPI, aiohttp, Textual, любой код с живым event loop | `gc_core.aio.AsyncAgent` или `AgentExecutor.arun(...)` |

Правило одно: **если у вас есть работающий event loop — используйте
асинхронный вход**. Синхронный, позванный изнутри loop, поднимет
`SyncApiInsideEventLoopError` — молча заблокировать чужой loop он не имеет
права.

## Асинхронный агент

```python
from gc_core.aio import AsyncAgent
from gc_core.config import load_config

config = load_config("~/.gc/config.yml")

async with await AsyncAgent.create(config) as agent:
    answer = await agent.run("почини падающие тесты", thread_id="t1")
```

Живые дельты ответа:

```python
async with await AsyncAgent.create(config) as agent:
    async for kind, text in agent.stream("объясни этот модуль", thread_id="t1"):
        if kind == "answer":
            print(text, end="", flush=True)
        elif kind == "think":
            ...          # рассуждение модели, если провайдер его отдаёт
        elif kind == "reset":
            ...          # начать вывод заново: пошла повторная попытка запроса
```

Управление ходом снаружи:

```python
agent.send({"role": "user", "content": "стоп, сначала линтер"})   # подкинуть в идущий ход
agent.stop()                                                      # кооперативная остановка
```

Полный рабочий пример — [`packages/core/examples/async_embed.py`](../packages/core/examples/async_embed.py).

## Без фасада

Фасад ничего не прячет: то же самое доступно напрямую.

```python
from gc_core.agent_executor import AgentExecutor
from gc_core.run_context import acreate_run_context

ctx = await acreate_run_context(config, on_stream_delta=my_sink)
answer = await AgentExecutor(ctx).arun(["привет"], thread_id="t1", system="sdk")
```

## Таблица соответствия

| Синхронно | Асинхронно |
|---|---|
| `create_run_context` | `acreate_run_context` |
| `AgentExecutor.run` | `AgentExecutor.arun` |
| `processing_responses.run_agent` | `arun_agent` |
| `tools.apply_tool` | `tools.aapply_tool` |
| `connect_backend.request_backend_chat_completions` | `arequest_backend_chat_completions` |
| `request_backend_policy.request_chat_completions_with_policy` | `arequest_chat_completions_with_policy` |
| `utils.llm_chat_client.request_chat_completion_text` | `arequest_chat_completion_text` |
| `summarize.llm.request_summary_text` | `arequest_summary_text` |
| `backends.local.LocalFsBackend.exec` | `aexec` (и `aexec_argv`, `arun_python`) |

Синхронные имена — тонкие обёртки: они гоняют ту же реализацию на приватном
event loop в фоновом потоке (`gc_core.utils.portal`). Второй реализации нет
нигде — это и есть причина, по которой поведение двух API совпадает.

## Свои тулы и свои бэкенды

**Синхронные тулы работают без правок и будут работать дальше.** Диспетчер
смотрит на `__call__`: корутину ждёт, обычную функцию уводит в поток, так что
loop она не задержит.

Если тул умеет асинхронно — объявите `acall`, и диспетчер позовёт его без
потока:

```python
class MyTool:
    def __call__(self, x: str) -> str:      # синхронный путь (тесты, apply_tool)
        return run_coro_blocking(self.acall(x=x))

    async def acall(self, x: str) -> str:   # быстрый путь для цикла агента
        ...
```

**Свои `FsBackend` тоже работают без правок.** Асинхронным сделан только
запуск процессов — единственная операция, которая держит поток минутами;
бэкенд без `aexec` автоматически заворачивается в `SyncExecAdapter`.

## Порты приложения

Ядро — библиотека, и ресурсы приложения ей не принадлежат:

```python
ctx.ask_human = my_ask          # спросить пользователя: функция или корутина
ctx.on_stream_delta = my_sink   # живые дельты ответа
ctx.on_progress = my_progress   # прогресс подключения MCP при сборке контекста
```

`ask_human` не задан → тул `turn_ask_human` читает stdin, как раньше в CLI.

### Логи

Отдельной абстракции нет и не нужно: `ctx.logger` — обычный
`logging.Logger`, а лучшего контракта, чем стандартная библиотека, не
придумать. Нужен только способ подключиться:

```python
agent = await AsyncAgent.create(cfg, log_handlers=[MyHandler()])
```

Хендлер **добавляется** к файловому выводу ядра, а не заменяет его; отключить
файл по-прежнему можно через `save_logs: false`.

Цеплять хендлер к `ctx.logger` вручную ПОСЛЕ сборки тоже можно, но раньше это
был единственный способ, и об этом никто не знал: ядро делает
`logger.handlers.clear()` в начале сборки и стирает всё, подключённое заранее.

Если приложение настроило `logging` по-своему и хочет видеть жизнь агента у
себя, нужен ещё флаг — иначе оно не увидит ничего:

```yaml
logging:
  propagate: true     # дефолт false: ран пишет в свой файл и не засоряет чужие хендлеры
```

Осторожно с `ctx.logging_path` и `ctx.summary_trace_path`: это пути к **файлам**,
и при `save_logs: false` они пусты.

### Кэш MCP-дискавери

Дискавери — самая дорогая часть старта (866–1405 мс против 2.4–6.9 мс на всё
остальное). Один агент платит её один раз, но приложение, поднимающее агентов
пачками, заплатит за каждого. Кэшем владеет вызывающий:

```python
cache = ToolsCache(config_dict)
a1 = await AsyncAgent.create(cfg, tools_cache=cache)
a2 = await AsyncAgent.create(cfg, tools_cache=cache)   # уже бесплатно
```

Кэш достаётся и субагентам через `ctx` — иначе каждый спавн стоил бы секунду.
Процессным синглтоном он намеренно не сделан: скрытое время жизни, протечка
между арендаторами сервера с разными наборами серверов и переживание между
тестами. Разным наборам — разные кэши, это решает владелец.

## Приёмка

Три критерия, которые сторожит
[`test_acceptance_non_blocking.py`](../packages/core/tests/test_acceptance_non_blocking.py):

1. **Изоляция.** Агент собирается там, где установлен только `gc-core`;
   `gc_cli`, `gc_tui`, `gc_server` в `sys.modules` не попадают.
2. **Неблокирование.** Пока агент работает (сеть + subprocess), соседняя
   корутина тикает с шагом 10 мс, и максимальный разрыв между тиками < 150 мс.
3. **Параллельность.** Два агента через `asyncio.gather` укладываются
   быстрее, чем в 1.6× одиночного прогона.

Проверить на своей установке:

```bash
uv venv /tmp/gc-core-only && source /tmp/gc-core-only/bin/activate
uv pip install "gc-core @ ./packages/core"
python -c "import gc_core.aio, sys; print('gc_tui' in sys.modules)"   # False

cd packages/core && python -m examples.async_embed ~/.gc/config.yml \
    "посчитай 2+2" "назови столицу Франции"
```

## Что осталось синхронным намеренно

- **Компакция контекста и intent-scope.** Подсистема большая, срабатывает раз
  в N шагов. Из цикла зовётся через `asyncio.to_thread`: loop свободен, а её
  собственные LLM-вызовы всё равно уходят на портальный loop асинхронно.
- **TUI** работает через синхронный `run` в своём daemon-потоке. Перевод на
  нативный asyncio возможен (Textual и так асинхронный), но 324 теста пакета
  завязаны на потоковую модель раннера — отдельная задача. Спрашивает
  пользователя TUI через порт `ctx.ask_human`, а не через `input()`: диспетчер
  уводит синхронные тулы в поток, где потоко-локальная подмена stdin не
  действует.

## HTTP-тулы: две точки входа

`exec_fetch_url`, `exec_simple_fetch_url` и `exec_download_file` переведены на
`httpx` и имеют обе точки входа — ровно ту форму, которую этот документ
предписывает сторонним тулам выше:

- `acall` — сеть через `await`, разбор HTML через `asyncio.to_thread`
  (trafilatura/lxml — чистый CPU, на большой странице это секунды);
- `__call__` — синхронный шим поверх `acall`, чтобы `apply_tool` и прямые
  вызовы из тестов продолжали работать.

Прокси эти тулы берут из окружения (`HTTPS_PROXY`/`HTTP_PROXY`/`NO_PROXY`)
через `resolve_proxies`, а не через `trust_env`: включать `trust_env` нельзя —
вместе с прокси вернулось бы чтение `~/.netrc`, а URL сюда приходят от модели.

`requests` из зависимостей ядра **ушёл** — весь HTTP на httpx. Переезд был не
про асинхронность (у httpx есть синхронный `Client`), а про одну зависимость
вместо двух на один и тот же протокол.

Две тонкости, которые всплыли по дороге:

* `NO_PROXY` httpx применяет только сам, при `trust_env=True` — а он нам не
  подходит, потому что вместе с прокси включает чтение `~/.netrc`. Матчинг
  вынесен в `utils/no_proxy.py`; его эквивалентность
  `requests.utils.should_bypass_proxies` закреплена дифференциальным тестом на
  221 паре «URL × список».
* Ловушка с netrc, ради которой заводилась `NO_NETRC_SESSION`, у httpx
  отсутствует структурно: он **не читает** netrc сам, `NetRCAuth` нужно
  передавать явно. Имя переменной оставлено — на неё ссылаются вызывающие, —
  а `trust_env=False` по-прежнему значим: он отключает env-прокси и CA из
  окружения, которые мы передаём явно.

Откат сторожит `test_no_requests_dependency.py`: он проверяет и манифест, и
код (через AST, чтобы не спотыкаться о докстроки, где объяснено, как было).
