# Harbor-адаптеры

Адаптеры для запуска gc-cli под харнессом [Harbor](https://www.harborframework.com).

Штатные четыре профиля — текстовые: просмотр изображений исключён из набора
инструментов, а `backend.vision` в конфигурации адаптера по умолчанию равен `false`.
Для отдельного эксперимента со зрением задайте `backend.vision: true` в
`agent_kwargs.gc_cli_config` и уберите `fs_view_image` из `agent.stop_tools` своего
профиля.

## `gc_cli_external.py` — External-агент

Запускает gc-cli как **External**-агент Harbor: цикл агента крутится **в раннере** (вне
контейнера задачи), а файловые/shell-тулы gc-cli работают **внутри контейнера задачи**
через `BaseEnvironment` Harbor (`os_backend: harbor` в конфиге → `ctx.os_backend` =
`HarborBackend`, собирается в `acreate_run_context`). **В контейнер задачи ничего не устанавливается** —
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

Резолв: `acreate_run_context` собирает `ctx.os_backend` через `tools/backends/factory.py`
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
# api_base от раннера → env GC_CLI_BACKEND_URL → gc_cli_config backend.completions_url
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

Любая настройка gc-cli задаётся из harbor-профиля через `agent_kwargs.gc_cli_config`
поверх дефолтов адаптера: заданное значение заменяет дефолт по своему пути, остальные
поля секции остаются; словари-значения (`chat_completion_kwargs`) и списки (`stop_tools`)
заменяются целиком, а не сливаются. Ключи — вложенные секции, как в `config.yml`
(`agent: {max_steps: …}`), или пути через точку (`agent.max_steps`). Прежние
плоские имена (`max_steps`, `summary_params`, `mcp-servers`) не принимаются: трайл падает на
сборке, а сообщение подсказывает путь (`неизвестная настройка max_steps (возможно,
agent.max_steps)`). Итог валидируется схемой `AppConfig`: опечатка в ключе или негодное
значение роняет трайл на сборке, а не теряется молча. `os_backend`
— не настройка, а рантайм (`RuntimeEnv`), адаптер ставит `harbor` сам:

```yaml
import_path: adapters.gc_cli_external:GcCliExternal
model: openai/MiniMaxAI/MiniMax-M2.7
agent_kwargs:
  gc_cli_config:
    agent.max_steps: 300                # дефолт схемы: 1000
    agent.no_tool_call_max_count: 3     # дефолт схемы: 5 (0 = обрыв с первой ошибки тула)
    backend.chat_completion_kwargs:     # сэмплинг агента
      temperature: 1.0
      top_p: 0.95
      top_k: 40
    chat_completions.auxiliary_llm:     # summary / finish-gate / judges (отдельно от агента)
      max_tokens: 24576
      # Schema default — {}. Vendor-ключи (enable_thinking) только если бэкенд их знает;
      # иначе OpenAI/Azure → 400. Когда у агента thinking вкл — гасите явно здесь.
      completion_kwargs:
        chat_template_kwargs:
          enable_thinking: false
    summary:                            # суммаризация (по умолчанию ВЫКЛ)
      chat_completions_summary_trigger_chars: 200000
```

- `api_base` (URL Polar) и `session_id` раннер впрыскивает **конструктором** (как Terminus) —
  в профиль не пишутся.
- `backend.completions_url`/`agent.model` runtime-авторитетны (из `api_base`/`model`) —
  профильный override их не перебивает.
- **Два профиля сэмплинга:** `backend.chat_completion_kwargs` — tool-цикл агента (thinking
  обычно вкл); `chat_completions.auxiliary_llm.completion_kwargs` — summary / finish-gate /
  judges (thinking обычно выкл).

## Workspace-write profile

[workspace_write.job.yaml](profiles/workspace_write.job.yaml) is a native Harbor
job config. Run it from the repository root with Python 3.12 and the Harbor
version in `uv.lock` (0.22.0 at the time of writing). Set `agents[0].model_name`
in the YAML to the model served by the endpoint.

```bash
uv sync --locked --python 3.12
export GC_CLI_BACKEND_URL=http://127.0.0.1:8000/v1/chat/completions
PYTHONPATH="$PWD/integrations/harbor" uv run --locked --python 3.12 harbor run \
  -c integrations/harbor/profiles/workspace_write.job.yaml \
  --path "$TASK_DIR" --jobs-dir "$JOBS_DIR"
```

For Fleet, use the YAML's `import_path` and copy `kwargs.gc_cli_config` into
`agent_kwargs.gc_cli_config`. The single setting `os_backend: harbor-workspace`
selects Harbor with the workspace-write policy. Other Harbor jobs default to
`os_backend: harbor`. Build the runner with a revision that includes the new
selector. The model and routing configuration can be supplied independently.

- The task image needs Bash, GNU coreutils and a user-namespace build of
  bubblewrap at `/usr/bin/bwrap` with `--disable-userns` support. The Linux
  container policy must permit the required namespaces and a private procfs
  mount. Harbor's effective workdir must be a writable project directory.
  The container needs a writable `/tmp` directory that resolves to itself.
  Startup probes the actual mounts and aborts the trial on failure.
- Shell, Python, and tool-provided argv execute inside bubblewrap in the trial
  container. Its root filesystem is visible read-only. The project is writable,
  with read-only mounts for top-level `.git`, `.gc`, `.agents`, `.codex` and
  `AGENTS.md`, and for the existing canonical paths declared in
  `security.protected_paths.extra`. Empty directories reserve missing top-level
  paths during the agent phase. Cleanup removes only the directories created
  by the profile before the verifier starts. Commands and file tools share the
  writable `/tmp` of the trial. Files there survive subsequent calls and are
  visible to a verifier in the same container. Commands have a private PID namespace,
  read-only procfs and a minimal `/dev`. Detached background jobs are unavailable.
  On agent timeout, the adapter allows up to 30 seconds for the worker to stop
  and release the reservations. A command or cleanup whose completion cannot be
  confirmed fails the single-step trial before Harbor 0.22 starts the verifier.
- Built-in file tools retain their UAG policy checks. In the External agent,
  the write decorator checks the built-in path classes and the command
  tripwire also checks `security.protected_paths.extra` on literal paths.
  Both routes enforce the configured extra paths as read-only mounts.
  The adapter defaults disable `read_guard`, `write_guard` and `url_policy`,
  with `command_moderation.mode: allow`. File writes stage the payload in a
  per-operation project directory and publish it under the same bubblewrap
  mounts. Built-in reads use the task filesystem via Harbor.
- The profile uses the container PATH, including Harbor's agent-phase environment.
  `exec_command` sources `/etc/profile` and the image user's `.bashrc`
  interactively inside bubblewrap, then runs the command with the resulting
  exported environment. The executable, its interpreter and its libraries must
  be visible. Invoke a project virtualenv
  as `./.venv/bin/python`, or add its bin directory to PATH. Profile scripts
  that require writing to a read-only location need a writable cache or tmp
  directory. The adapter applies the image PATH and dynamic loader variables
  inside model command launches. For images that use `BASH_ENV`, source the
  initializer via `agent.harbor_exec_init` to run it inside the workspace
  sandbox. Task images and their startup environment are trusted inputs.
  Read-only mounts still allow reads and access to visible service sockets,
  so trial images must follow the benchmark's mount policy.
- Bubblewrap shares the task's network namespace. In Harbor 0.22, task phase
  policy such as `[agent].network_mode = "no-network"` governs access from the
  container. The Docker egress sidecar controls traffic to outside destinations
  while allowing local traffic in that namespace. The example removes
  `exec_fetch_url`, which otherwise defaults to runner-side HTTP. Optional MCP
  servers and the model endpoint have their own execution locations and
  connectivity.

The local smoke run used Harbor 0.22.0, Docker Engine 28.4.0 and a Debian 13
ARM64 task image with bubblewrap 0.12.0. Default Docker seccomp prevented user
namespace creation. Dropping seccomp alone exposed a second failure mounting
procfs. The disposable smoke task used this `environment/docker-compose.yaml`
override with no host mounts or additional capabilities for `main`.

```yaml
services:
  main:
    security_opt:
      - seccomp=unconfined
      - systempaths=unconfined
```

The job profile leaves the runtime's controls in place. Run the startup probe
and workspace checks for each benchmark image and container policy before
using it for a comparison.

## Замечания

- **Бэкенд-URL хост-сайд** (`127.0.0.1`), не `host.docker.internal` — цикл бежит на хосте,
  не в контейнере.
- **Model id сохраняет vendor-path** (`MiniMaxAI/MiniMax-M2.7`); срезается только префикс
  провайдера `openai/`.
- Security-декораторы (`@enforce_write_guard`/`@enforce_read_guard`/`@moderate_command_execution`)
  остаются на тулах, но адаптер **явно пинит их OFF** в `_build_settings()`
  (`read_guard`/`write_guard`/`url_policy` → `enabled: false`, `command_moderation` → `allow`) —
  независимо от дефолтов релиза gc-cli. Причины: (1) контейнер сам песочница; (2) гарды
  резолвят пути по ФС **раннера** (`launch_root` = cwd раннера) — контейнерный `/app` для них
  всегда «вне периметра», включённый гард блокирует 100% файловых операций; (3) режим `ask`
  на non-tty раннере авто-отклоняет → `ask` ≡ `deny`. Пин защищён тестом
  (`test_security_guards_pinned_off_for_benches`); отдельный профиль может явно включить
  гард через `gc_cli_config` (поверх дефолтов, замена по пути).
- При `os_backend: harbor` из реестра выбрасывается `download_file` (пишет на
  хост-ФС — в контейнер-режиме файл лёг бы мимо контейнера). Маршрутизация I/O в
  контейнер идёт через `ctx.os_backend`.
