# Harbor-адаптеры

Адаптеры для запуска gc-cli под харнессом [Harbor](https://www.harborframework.com).

## `gc_cli_external.py` — External-агент

Запускает gc-cli как **External**-агент Harbor: цикл агента крутится **в раннере** (вне
контейнера задачи), а файловые/shell-тулы gc-cli работают **внутри контейнера задачи**
через `BaseEnvironment` Harbor (пак `use_harbor_tools` + `HarborBackend`). **В контейнер
задачи ничего не устанавливается** — это и есть смысл External-паттерна (важно для
честности замеров: установка чего-либо в контейнер может неявно изменить условия задачи).

```
Runner / main-контейнер (хост-сайд)            task-контейнер (поднят через DinD)
  GcCliExternal(BaseAgent)                       [ничего не ставится]
    run() → AgentExecutor (gc-cli loop)
              └─ HarborToolPack
                   └─ HarborBackend  ──exec/upload/download──►  ФС/shell контейнера
```

## Тул-пак (`src/tools/harbor/`)

11 тулов-обёрток — **тонкие подклассы базовых тулов gc-cli**. Каждый переопределяет
**только `__call__`** для маршрутизации I/O через `HarborBackend`; вся логика, валидация
и формат ответа **наследуются** от базы → поведение **1:1 с обычными тулами**.

Меняются **только тулы с ФС/shell** (`read_file`, `write_to_file`, `append_to_file`,
`apply_file_edits`, `search_and_replace`, `list_files`, `search_files`, `file_info`,
`save_base64_file`, `exec_command`, `run_python_code`). Остальные тулы (think, web,
память, subagents, скиллы, MCP) хост не трогают — остаются без изменений.

`HarborBackend` использует нативные ручки `BaseEnvironment` где они есть
(`exec`, `upload_file`/`download_file`, `is_file`/`is_dir`); для read/stat/sha256/walk
нативных ручек у Harbor нет — там shell через `exec` (GNU coreutils, как в Linux-докере).

Корректность проверяется **дифференциальными oracle-тестами** (`tests/test_harbor_*.py`):
один и тот же ввод через базовый тул (реальная ФС) и шим (in-memory backend) должны дать
**идентичный envelope и результат**.

## Запись траекторий

Адаптер сам пишет `self.logs_dir/trajectory.json` в формате **ATIF-v1.7** (как Terminus):
шаги агента, вызовы тулов (свёрнуты в `observation`), `final_metrics`. Harbor синкает в
`agent/`-папку результатов.

## Установка (один раз)

gc-cli должен импортироваться в окружении раннера Harbor:

```bash
uv pip install --python "$(dirname "$(readlink -f "$(command -v harbor)")")/python" -e .
```

(или любое окружение, где запускается CLI `harbor`).

## Запуск

Запускать из каталога `harbor/`, чтобы `adapters.gc_cli_external` резолвился из cwd:

```bash
cd harbor
export GC_CLI_BACKEND_URL=http://127.0.0.1:8014/v1/chat/completions   # бэкенд на хосте
harbor run \
  --path <task-dir> \
  --agent-import-path adapters.gc_cli_external:GcCliExternal \
  --model openai/MiniMaxAI/MiniMax-M2.7 \
  --jobs-dir results/run
```

Локально, если образ задачи только в приватном реестре — добавь `--force-build`
(соберёт образ из `environment/Dockerfile` задачи, минуя реестр).

> Import path — `adapters.gc_cli_external`, **не** `harbor.adapters...`: верхнеуровневое
> имя `harbor` занято самим фреймворком, его нельзя затенять. Запуск из каталога `harbor/`
> держит `adapters` на пути, а фреймворк импортится из своего venv.

## Конфигурация из профиля (без правки кода)

Любой ключ `config_dict` gc-cli задаётся из harbor-профиля через
`agent_kwargs.gc_cli_config` (deep-merge поверх дефолтов адаптера, **плоские** ключи):

```yaml
import_path: adapters.gc_cli_external:GcCliExternal
model: openai/MiniMaxAI/MiniMax-M2.7
agent_kwargs:
  gc_cli_config:
    max_steps: 300                      # дефолт 200
    no_tool_call_max_count: 3           # дефолт 3 (0 = обрыв с первой ошибки тула)
    chat_completion_kwargs:             # сэмплинг
      temperature: 1.0
      top_p: 0.95
      top_k: 40
    summary_params:                     # суммаризация (по умолчанию ВЫКЛ)
      chat_completions_summary_trigger_chars: 200000
```

- `api_base` (URL Polar) и `session_id` раннер впрыскивает **конструктором** (как Terminus) —
  в профиль не пишутся.
- `completions_url`/`model` runtime-авторитетны (из `api_base`/`model`) — профильный override
  их не перебивает.

## Замечания

- **Бэкенд-URL хост-сайд** (`127.0.0.1`), не `host.docker.internal` — цикл бежит на хосте,
  не в контейнере.
- **Model id сохраняет vendor-path** (`MiniMaxAI/MiniMax-M2.7`); срезается только префикс
  провайдера `openai/`.
- Security-декораторы (`@enforce_write_guard`/`@enforce_read_guard`/`@moderate_command_execution`)
  в harbor-шимах **сняты** — контейнер сам песочница.
- Тул-пак включается флагом `use_harbor_tools` (в `_build_config` адаптера / через
  `build_tools_data`).
