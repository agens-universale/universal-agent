# Harbor-адаптеры

Адаптеры для запуска gc-cli под харнессом [Harbor](https://www.harborframework.com).

## `gc_cli_external.py` — External-агент

Запускает gc-cli как **External**-агент Harbor: цикл агента крутится **в раннере** (вне
контейнера задачи), а файловые/shell-тулы gc-cli работают **внутри контейнера задачи**
через `BaseEnvironment` Harbor (`os_backend: harbor` в конфиге → `ctx.os_backend` =
`HarborBackend`, собирается в `create_run_context`). **В контейнер задачи ничего не устанавливается** —
это и есть смысл External-паттерна (важно для честности замеров: установка чего-либо
в контейнер может неявно изменить условия задачи).

```
Runner / main-контейнер (хост-сайд)            task-контейнер (поднят через DinD)
  GcCliExternal(BaseAgent)                       [ничего не ставится]
    run() → AgentExecutor (gc-cli loop)
              └─ базовые тулы gc-cli (единственные, без копий)
                   └─ ctx.os_backend (HarborBackend)
                        ──exec/upload/download──►  ФС/shell контейнера
```

## Бэкенд (`src/tools/backends/`)

Физический I/O тулов вынесен за интерфейс `FsBackend` (`protocol.py`) — паттерн
DeepAgents: **тул определён один раз** (валидация, форматы ошибок, envelope — в базовом
туле), а «где живут байты / где бежит команда» решает подставленная реализация:

- `local.py` — `LocalFsBackend`: хост (`os`/`open`/`subprocess`), дефолт;
- `harbor.py` — `HarborBackend`: контейнер задачи через `BaseEnvironment`
  (`is_sandbox = True`; exec-timeout из `agent.exec_timeout_sec`, в bench — 600s).

Резолв: `create_run_context` собирает `ctx.os_backend` через `tools/backends/factory.py`
(`os_backend: local` по умолчанию, `harbor` — при запуске через адаптер). Тулы читают
`self._ctx.os_backend`. Отдельного harbor-пака тулов **нет** — контейнерный режим и
хост-режим гоняют один и тот же код тулов.

Бэкенд затрагивает **только тулы с ФС/shell** (`fs_read_file`, `fs_write_file`,
`fs_append_file`, `fs_apply_file_edits`, `fs_search_and_replace`, `fs_list_files`, `fs_find_files_by_name`,
`fs_find_in_files`, `fs_file_info`, `fs_save_base64_file`, `exec_command`, `exec_python`).
Остальные тулы (think, web, память, subagents, скиллы, MCP) хост не трогают — остаются
без изменений.

`HarborBackend` использует нативные ручки `BaseEnvironment` где они есть
(`exec`, `upload_file`, `is_file`/`is_dir`); для read/stat/sha256/walk/listdir
нативных ручек у Harbor нет — там shell через `exec` (GNU coreutils, как в Linux-докере).

Корректность проверяется **дифференциальными oracle-тестами** (`tests/test_harbor_*.py`):
один и тот же ввод через базовый тул с реальной ФС (`LocalFsBackend`) и с in-memory
`FakeBackend` должен дать **идентичный envelope и результат**.

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
# ОБЯЗАТЕЛЬНО для локальных запусков: хардкод-дефолта URL нет. Источники:
# api_base от раннера → env GC_CLI_BACKEND_URL → gc_cli_config.completions_url
# из профиля; без всех трёх адаптер падает с понятной ошибкой (fail-fast).
export GC_CLI_BACKEND_URL=http://127.0.0.1:<port>/v1/chat/completions   # бэкенд на хосте
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
    chat_completion_kwargs:             # сэмплинг агента
      temperature: 1.0
      top_p: 0.95
      top_k: 40
    auxiliary_llm_params:               # summary / finish-gate / judges (отдельно от агента)
      max_tokens: 24576
      # Schema default — {}. Vendor-ключи (enable_thinking) только если бэкенд их знает;
      # иначе OpenAI/Azure → 400. Когда у агента thinking вкл — гасите явно здесь.
      completion_kwargs:
        chat_template_kwargs:
          enable_thinking: false
    summary_params:                     # суммаризация (по умолчанию ВЫКЛ)
      chat_completions_summary_trigger_chars: 200000
```

- `api_base` (URL Polar) и `session_id` раннер впрыскивает **конструктором** (как Terminus) —
  в профиль не пишутся.
- `completions_url`/`model` runtime-авторитетны (из `api_base`/`model`) — профильный override
  их не перебивает.
- **Два профиля сэмплинга:** `chat_completion_kwargs` — tool-цикл агента (thinking обычно вкл);
  `auxiliary_llm_params.completion_kwargs` — summary / finish-gate / judges (thinking обычно выкл).
  В harbor пишите **плоский** `auxiliary_llm_params`, не `chat_completions_params.auxiliary_llm`.

## Замечания

- **Бэкенд-URL хост-сайд** (`127.0.0.1`), не `host.docker.internal` — цикл бежит на хосте,
  не в контейнере.
- **Model id сохраняет vendor-path** (`MiniMaxAI/MiniMax-M2.7`); срезается только префикс
  провайдера `openai/`.
- Security-декораторы (`@enforce_write_guard`/`@enforce_read_guard`/`@moderate_command_execution`)
  остаются на тулах, но адаптер **явно пинит их OFF** в `_build_config()`
  (`read_guard`/`write_guard`/`url_policy` → `enabled: false`, `command_moderation` → `allow`) —
  независимо от дефолтов релиза gc-cli. Причины: (1) контейнер сам песочница; (2) гарды
  резолвят пути по ФС **раннера** (`launch_root` = cwd раннера) — контейнерный `/app` для них
  всегда «вне периметра», включённый гард блокирует 100% файловых операций; (3) режим `ask`
  на non-tty раннере авто-отклоняет → `ask` ≡ `deny`. Пин защищён тестом
  (`test_security_guards_pinned_off_for_benches`); отдельный профиль может явно включить
  гард через `gc_cli_config` (deep-merge поверх дефолтов).
- При `os_backend: harbor` из реестра выбрасывается `download_file` (пишет на
  хост-ФС — в контейнер-режиме файл лёг бы мимо контейнера). Маршрутизация I/O в
  контейнер идёт через `ctx.os_backend`.
