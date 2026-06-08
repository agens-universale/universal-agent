[Назад к README](../readme.md)

# Swarm-режим (только Linux)

> ⚠️ **Только Linux.** Swarm-режим требует `bubblewrap` (`bwrap`) для изоляции и `timeout` из GNU coreutils для ограничения времени выполнения. На macOS/Windows эти инструменты недоступны.

Swarm — серверный режим, в котором REST-обёртка форкает CLI в **изолированную bubblewrap-песочницу** на каждый `POST /swarm/run`. Каждая задача:

- работает в собственных mount/pid/user/uts/ipc/cgroup namespaces (network — общий с хостом, см. AR-8 ниже);
- видит только writable `/work` и read-only биндинги системных директорий + venv с `gc-cli`;
- получает свои `meta.json`, `stdout.log`, `stderr.log`, `completion.json` в `threads_dir`;
- ограничена по времени, памяти, диску и CPU.

Это даёт три практических свойства поверх обычного `serve`-режима:

1. **Изоляция** — sworm не видит `~/.ssh`, `~/.aws`, `/etc/shadow`, домашнюю директорию пользователя и т.п. Полезно для многопользовательских запусков.
2. **Идемпотентность** — повторный `POST` с тем же `task_id` возвращает 409, а не запускает второй процесс.
3. **Ресурсные лимиты** — таймаут (GNU `timeout`), память/CPU (`psutil`), диск (`du -sb`).

## Содержание

- [Системные требования](#системные-требования)
- [Минимальная установка](#минимальная-установка)
- [Конфигурация (config_server.yml)](#конфигурация-config_serveryml)
- [Pipeline одного sworm-запроса](#pipeline-одного-sworm-запроса)
- [Эндпоинты](#эндпоинты)
- [Файлы и куда смотреть](#файлы-и-куда-смотреть)
- [Архитектурные решения (AR-1..AR-9)](#архитектурные-решения)
- [Известные ограничения](#известные-ограничения)
- [Troubleshooting](#troubleshooting)

## Системные требования

| Зависимость | Обязательность | Ubuntu/Debian | Fedora/RHEL | Alpine |
|---|---|---|---|---|
| `bubblewrap` (`bwrap`) | обязательно | `bubblewrap` | `bubblewrap` | `bubblewrap` |
| `timeout` (GNU coreutils) | обязательно | `coreutils` | `coreutils` | `coreutils` |
| `gc-cli` в `PATH` | обязательно | — | — | — |
| `psutil` | опционально (для memory/cpu enforcement) | `python3-psutil` | `python3-psutil` | `py3-psutil` |
| `jq` | опционально (для shim'а `gc-cli-swarm`) | `jq` | `jq` | `jq` |

Также ядро должно разрешать unprivileged user namespaces — на Ubuntu ≥18.04 это дефолт (`kernel.unprivileged_userns_clone=1`). Проверка:

```bash
sysctl kernel.unprivileged_userns_clone   # должно вернуть 1
```

## Минимальная установка

```bash
# 1. Системные зависимости
sudo apt-get install -y bubblewrap coreutils jq

# 2. gc-cli в отдельный venv (важно: НЕ uv-venv, нужен системный python3 —
#    он подхватится через ro-bind /usr внутри bwrap)
python3 -m venv /opt/gc-cli/.venv
/opt/gc-cli/.venv/bin/pip install -e .
/opt/gc-cli/.venv/bin/pip install psutil    # для memory/cpu enforcement

# 3. config_server.yml с включённым swarm (см. ниже)
# 4. Запуск сервера
/opt/gc-cli/.venv/bin/gc-cli serve --config /etc/gc-cli/config_server.yml
```

## Конфигурация (config_server.yml)

Секция `swarm:` в `config_server.yml`:

```yaml
swarm:
  enabled: true                           # обязательно для активации ручек

  # Корни для артефактов. Дефолты:
  #   threads_dir       = <state>/projects/<project-slug>/swarm/threads
  #   sandbox_root_dir  = <state>/projects/<project-slug>/swarm/sandboxes
  # Можно переопределить абсолютным путём:
  threads_dir: "/var/lib/gc-cli/swarm/threads"
  sandbox_root_dir: "/var/lib/gc-cli/swarm/sandboxes"

  # Сколько одновременных sworm'ов разрешено. При превышении → 503.
  max_concurrent: 10

  # TTL для janitor'а: старше этого — каталог удаляется.
  # Учитываются как finalized sworm'ы, так и orphan sandbox'ы.
  ttl_seconds: 86400

  limits:
    timeout_seconds: 1800                 # дедлайн на sworm; после — SIGTERM → 30s grace → SIGKILL
    memory_mb: 1024                       # enforced через psutil (если установлен)
    disk_mb: 2048                         # enforced через du -sb на sandbox
    cpu_percent: 50                       # enforced через psutil (если установлен)

  # Чем запускать CLI внутри песочницы. По дефолту "gc-cli";
  # на bare-Linux часто используется shim "gc-cli-swarm", читающий
  # /work/payload.json через jq (workaround AR-5 — пока gc-cli не умеет --payload-file).
  cli_command: "gc-cli"

  # Какой sandbox-tool. На данный момент поддерживается только bubblewrap.
  sandbox_tool: "bubblewrap"

  # Путь к wrapper-скрипту (по умолчанию scripts/run-cli-in-sandbox.sh в репо).
  wrapper_path: "/opt/gc-cli/scripts/run-cli-in-sandbox.sh"

  # Venv c gc-cli и зависимостями. Биндится --ro-bind внутрь песочницы.
  # ПРИНИМАЕТСЯ ТОЛЬКО АБСОЛЮТНЫЙ путь; пустое или относительное игнорируется.
  cli_venv_root: "/opt/gc-cli/.venv"

  # Доп. директория с зависимостями MCP (например ~/.local/share/gc/node для playwright).
  # ПРИНИМАЕТСЯ ТОЛЬКО АБСОЛЮТНЫЙ путь.
  agent_dep_dir: "/opt/gc-cli/mcp-deps"

  # Доп. ro-bind'ы внутрь sandbox (skills, prompts, статика и т.п.). Только абсолютные.
  extra_mounts:
    - "/opt/gc-cli/prompts"
    - "/opt/gc-cli/skills"

  # Опц. пути к agent.md и skills_data_dir, которые материализуются в sandbox.
  # Относительные резолвятся от корня репозитория сервера.
  agentmd_path: "swarm_agent.md"
  skills_dir: "swarm_skills"
```

Дефолты (если поле опущено): `enabled=false`, `max_concurrent=10`, `ttl_seconds=86400`, `limits={memory_mb=1024, disk_mb=2048, cpu_percent=50, timeout_seconds=1800}`, `cli_command="gc-cli"`, `sandbox_tool="bubblewrap"`.

**Важно:** `cli_venv_root`, `agent_dep_dir` и каждый элемент `extra_mounts` принимают **только абсолютные пути**. Относительные значения молча игнорируются (см. `swarm/config.py::_abs_or_empty`).

## Pipeline одного sworm-запроса

```
client                 server                   wrapper.sh             bwrap + gc-cli
  │                       │                          │                       │
  │  POST /swarm/run ────►│                          │                       │
  │                       ├─ idempotency check       │                       │
  │                       ├─ materialize config      │                       │
  │                       │  → sandbox/.gc/config.yml│                       │
  │                       ├─ Popen(start_new_session)┼──────────────────────►│
  │  200 started ◄────────┤                          │                       │
  │                       │                          ├─ payload.json          │
  │                       │                          ├─ assemble BWRAP_CMD    │
  │                       │                          ├─ timeout ${T} bwrap ──►│ gc-cli
  │                       │                          │                       │ ⇣
  │                       │                          │                       │ LLM + MCP
  │                       │                          │                       │ (через --share-net)
  │                       │                          │                       │ ⇣
  │                       │                          │◄── exit_code ─────────┤
  │                       │                          ├─ write completion.json│
  │                       │ monitor (60s tick)       │                       │
  │  GET /swarm/status ──►├─ check_and_finalize ◄────┘                       │
  │  {status: completed} ◄┤                                                  │
```

**Этапы по шагам:**

1. **HTTP-вход.** `POST /swarm/run` приходит в `server.py:swarm_run`. Проверяется `swarm.enabled` (иначе 503), парсится JSON, нормализуются `task_id` и `system`, формируется `swarm_id = "<system>_<task_id>"`.
2. **Идемпотентность (AR-3).** Под глобальным `_SWARM_RUN_LOCK` читается `meta.json`:
   - exist & `status ∈ {queued, running}` → **409 `swarm_id_already_active`**;
   - exist & `status` final → **409 `swarm_id_already_finalized`**;
   - `reuse_strategy != "reject"` → **501 `not_implemented`** (MVP поддерживает только `reject`).
3. **Защита от перегрузки.** Если `monitor.active_count >= max_concurrent` → **503 `too_many_concurrent_swarm_processes`**.
4. **Materialize config (AR-1).** В `runner.start_swarm_process` создаётся `sandbox_root/<swarm_id>/`, конфиг собирается из 5 слоёв (deep_merge в порядке применения):
   1. **Base** — `host_config_path` (по дефолту resolve через XDG / пакетный default).
   2. **Project overlay** — `<cwd>/.gc/config.yml` относительно CWD сервера (симметрично с обычным CLI'ем).
   3. **SWARM_* env vars** — `SWARM_AGENTMD_PATH` / `SWARM_THREADS_DIR` / `SWARM_SKILLS_DIR` → `paths.*`.
   4. **`agent_overrides`** из `config_server.yml` — flat-формат авто-оборачивается в `agent:` (для совместимости с webhook'ом), raw-формат с секциями применяется как есть.
   5. **Swarm-specific overrides** — всегда последние и всегда побеждают: `paths.threads_data_path=/work/.gc/threads`, `paths.logs_dump_dir=/work/.gc/logs`, `agent.use_server_subagents=False`. Это означает, что никакой `agent_overrides` не может сломать sandbox-изоляцию.

   Итог пишется атомарно в `sandbox/.gc/config.yml`.
5. **Spawn wrapper.** `Popen([wrapper_path], env=..., start_new_session=True, stdout=DEVNULL, stderr=DEVNULL)`. `start_new_session` критичен: благодаря ему PID становится главой новой process group, и `os.killpg(pid, SIGTERM)` укладывает всё дерево (wrapper → bwrap → CLI → дочерние tool-процессы).
6. **wrapper.sh.** Записывает `payload.json` в `${SWARM_SANDBOX_PATH}/payload.json`, динамически собирает `--ro-bind` для `/lib`, `/lib64`, `/bin`, `/sbin`, `/etc`, `cli_venv_root`, `agent_dep_dir`, каждого `extra_mounts`, плюс `--bind ${SANDBOX_PATH} /work --chdir /work --unshare-all --share-net --setenv HOME /work`.
7. **bwrap + CLI.** Команда оборачивается в `timeout --kill-after=30 --signal=TERM ${TIMEOUT_SEC}`, stdout/stderr пишутся в `threads_dir/<swarm_id>/{stdout,stderr}.log`. CLI читает `/work/.gc/config.yml` и `/work/payload.json`, работает с LLM/MCP через расшаренный network namespace.
8. **Completion.** После выхода CLI wrapper маппит exit_code → `error_type` (`0`=success, `124/137`=timeout, `125-127`=wrapper_setup_error, остальное=cli_error) и атомарно пишет `completion.json`.
9. **Финализация.** Фоновый `SwarmMonitor` каждые 60 секунд (или lazy в момент `GET /swarm/status`) зовёт `lifecycle.check_and_finalize`: проверяет deadline, ресурсные лимиты, читает `completion.json`, обновляет `meta.json` и снимает sworm с `active`-списка.
10. **Cleanup.** Janitor-тред раз в час сканирует `threads_dir`/`sandbox_root_dir`, удаляет каталоги старше `ttl_seconds` (включая orphan sandbox'ы).

## Эндпоинты

### `POST /swarm/run`

Запуск задачи (idempotent по `task_id`).

**Request body:**

```json
{
  "task_id": "abc123",                    // опц.; если нет — UUID4
  "system": "generic",                    // опц.; дефолт из defaults.system
  "query": "...",                         // основной запрос для агента
  "reuse_strategy": "reject"              // MVP: только "reject"
}
```

**Ответы:**
- `200` — `{"status": "started", "swarm_id", "system", "task_id"}`.
- `400` — `invalid JSON body` / `JSON body must be an object`.
- `409` — `swarm_id_already_active` (sworm запущен, ещё не завершён) или `swarm_id_already_finalized` (уже завершён).
- `501` — `reuse_strategy_<x>_not_supported` (всё кроме `reject`).
- `503` — `swarm_disabled` (фича выключена в конфиге) или `too_many_concurrent_swarm_processes`.

### `GET /swarm/status/{system}/{task_id}`

Текущий статус задачи. Вызывает `check_and_finalize` с `heavy_checks=False` (без `du`/`psutil`-проверок — быстро для HTTP).

**Ответ:** `{"status", "system", "task_id", "meta": {swarm_meta: {...}}}`.
`status` ∈ `{queued, running, completed, failed, timeout, unknown_failed}`. Финальные статусы — `{completed, failed, timeout, unknown_failed}` (см. `lifecycle.FINAL_STATUSES`).

Если `meta.json` отсутствует — `404`.

### `GET /swarm/logs/{system}/{task_id}`

Возвращает `{meta, messages, stdout, stderr}`. `messages` читается из `messages.jsonl` (канал E — лежит внутри `sandbox/.gc/threads/<id>/`), с фоллбэком в `threads_dir/<id>/`. `stdout`/`stderr` — построчно из `stdout.log`/`stderr.log`.

Если ничего не найдено — `404`.

## Файлы и куда смотреть

После запуска одной задачи на диске появляется такое:

```
${threads_dir}/<system>_<task_id>/
├── meta.json              ← главный snapshot: status, exit_code, pid, deadline_at, finished_at
├── completion.json        ← как wrapper.sh завершился (exit_code, error_type)
├── stdout.log             ← stdout CLI (полный trace)
└── stderr.log             ← stderr CLI (python tracebacks, MCP errors)

${sandbox_root_dir}/<system>_<task_id>/   ← это /work внутри bwrap
├── payload.json           ← вход (что было в POST body)
└── .gc/
    ├── config.yml         ← materialized runtime config (после deep_merge)
    ├── threads/<id>/
    │   └── messages.jsonl ← канал E: все сообщения диалога
    └── logs/              ← внутренние логи CLI (storage dump и т.п.)
```

Быстрый диагностический рецепт:

```bash
SID=$(ls -t ${threads_dir} | head -1)
jq . ${threads_dir}/$SID/meta.json
cat   ${threads_dir}/$SID/completion.json
tail -50 ${threads_dir}/$SID/stderr.log
jq -r .message ${sandbox_root_dir}/$SID/.gc/threads/*/messages.jsonl | head
```

Проверка изоляции на живом sworm'е (пока работает):

```bash
WRAPPER_PID=$(jq -r .swarm_meta.pid ${threads_dir}/$SID/meta.json)
# Найти bwrap-descendant — у него другой mnt-namespace:
for p in /proc/[0-9]*; do
  if [[ "$(readlink $p/ns/mnt 2>/dev/null)" != "$(readlink /proc/$WRAPPER_PID/ns/mnt)" ]]; then
    echo "bwrap PID: $(basename $p)"; break
  fi
done
# Mount-tree внутри sandbox:
awk '{print $5}' /proc/<bwrap_pid>/mountinfo | sort -u
```

## Архитектурные решения

| AR | Решение | Зачем |
|---|---|---|
| AR-1 | Per-request materialized config в sandbox | Изоляция мутаций; race-free относительно server config |
| AR-3 | Idempotency через `swarm_id = system + task_id`, MVP `reuse_strategy: reject` | Защита от случайных дублей и пропуска `task_id` |
| AR-4 | Запрет per-request config overrides из payload'а | Клиент не может расширить себе перимтр через body |
| AR-5 | Shim `gc-cli-swarm` читает `payload.json` через `jq` | Пока в `gc-cli` нет нативного `--payload-file` |
| AR-6 | Read-only bind на venv, RW только `/work` | Минимум attack surface |
| AR-7 | `agent_dep_dir` для node/npm-зависимостей MCP | Изолировать тяжёлые зависимости |
| AR-8 | `--share-net` (network namespace общий с хостом) | LLM/MCP TCP-доступ; **известное ослабление изоляции** |
| AR-9 | `start_new_session=True` + `os.killpg` по PGID | Гарантированное снятие всего дерева процессов |

## Известные ограничения

- **Только Linux.** macOS/Windows — нет bubblewrap.
- **Network namespace общий с хостом** (AR-8). Sworm видит `127.0.0.1` хоста, может ходить в его сервисы. Это сознательный trade-off для доступа к LLM. Если нужна полная сетевая изоляция — wrapper надо менять.
- **`reuse_strategy: continue`/`fresh`** — не реализованы (501). Только `reject`.
- **Docker MCP через stdio** (`docker exec`) — не поддерживается, потребовался бы mount `/var/run/docker.sock`. Используйте TCP/HTTP MCP.
- **Per-request config overrides из payload** запрещены (AR-4). Конфиг полностью определяется сервером.
- **Bind `/lib`, `/lib64`, `/etc`** делается динамически (только если директория существует на хосте). На Arch/Alpine это уже symlink на `/usr/lib` — wrapper это учитывает.

## Troubleshooting

| Симптом | Причина | Лечение |
|---|---|---|
| `503 swarm_disabled` | `swarm.enabled=false` или импорт `swarm.*` не прошёл | Проверь конфиг; смотри в логах `swarm disabled due to import error: ...` |
| `503 too_many_concurrent_swarm_processes` | Лимит `max_concurrent` | Подожди или увеличь лимит |
| `409 swarm_id_already_active` | Дубль `task_id` для живого sworm'а | Подожди или используй другой `task_id` |
| `409 swarm_id_already_finalized` | Дубль `task_id` для завершённого | `task_id` одноразовые — генерируй новый |
| Sworm падает за 5-10 секунд с `cli_error` | CLI не видит LLM-бэкенда | `curl http://127.0.0.1:<port>/v1/models` на хосте — проверь что бэкенд жив |
| `bwrap: setting up uid map` | Unprivileged user namespaces выключены | `sudo sysctl -w kernel.unprivileged_userns_clone=1`; при AppArmor — `sudo aa-disable /etc/apparmor.d/unprivileged_userns` |
| `FileNotFoundError: /work/.gc/prompts/...` | Относительные пути в `config.yml` | Замени на абсолютные (резолвятся от `/work/.gc/` внутри bwrap) |
| `MCP tools/list ... 400 Bad Request` | Неправильный transport | Для публичных MCP-серверов (playwright и т.п.) → `transport: stream_sse` или `stream_http`, не legacy `sse`/`http` |
| `messages.jsonl` пуст | CLI не успел финализировать поток (короткая задача) | Смотри `stdout.log` — там полный дамп |
| `meta.status = unknown_failed`, `error_type = process_crash` | wrapper упал до записи `completion.json` | Смотри `stderr.log` — там traceback |
| Накопилось много `threads/`, `sandboxes/` | TTL janitor не успевает или `ttl_seconds` большой | Снизь `ttl_seconds` или запусти `scripts/cleanup_swarm_threads.py` вручную |
