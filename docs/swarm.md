[Назад к README](../readme.md)

# Swarm-режим (только Linux)

> ⚠️ **Только Linux.** Swarm-режим требует `bubblewrap` (`bwrap`) для изоляции и `timeout` из GNU coreutils для ограничения времени выполнения. На macOS/Windows эти инструменты недоступны.

Swarm — поверхность `swarm` сервера `gc-serve` ([server.md](server.md)): те же ручки, что у вебхука, на своём порту (8092), но каждая задача (`POST /events/webhook`) исполняется `gc-cli` в **изолированной bubblewrap-песочнице**. Каждая задача:

- работает в собственных mount/pid/user/uts/ipc/cgroup namespaces (network — общий с хостом, см. AR-8 ниже);
- видит только writable `/work` (+ эфемерный `/tmp` через tmpfs) и read-only биндинги системных директорий + venv с `gc-cli`;
- получает свои `meta.json`, `stdout.log`, `stderr.log`, `completion.json` в `swarm_threads_dir`;
- ограничена по времени, памяти, диску и CPU.

Это даёт три практических свойства поверх вебхука:

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
| `jq` | опционально (для диагностических рецептов ниже) | `jq` | `jq` | `jq` |

Также ядро должно разрешать unprivileged user namespaces — на Ubuntu ≥18.04 это дефолт (`kernel.unprivileged_userns_clone=1`). Проверка:

```bash
sysctl kernel.unprivileged_userns_clone   # должно вернуть 1
```

## Минимальная установка

```bash
# 1. Системные зависимости. bubblewrap — сама песочница, coreutils — `timeout`,
#    которым обёртка ограничивает время жизни sworm'а.
sudo apt-get install -y bubblewrap coreutils

# 2. Профиль сервера в отдельный venv. Ставит ТОЛЬКО uv: манифест пинит
#    gc-core==0.3.0 / gc-cli==0.3.0 / gc-mcp==0.3.0, а те резолвятся через
#    [tool.uv.sources] workspace = true и на PyPI не публикуются. Обычный pip
#    этой секции не понимает и падает
#    `No matching distribution found for gc-cli==0.3.0` — см. usage.md,
#    «Через pip — не работает».
python3 -m venv /opt/gc-cli/.venv
uv pip install --python /opt/gc-cli/.venv/bin/python \
  "gc-server @ git+https://gitverse.ru/gigacode-ai/universal-agent#subdirectory=packages/server"
# Ставить что-либо отдельно не нужно: gc-cli (его и запускает песочница),
# psutil для memory/cpu enforcement и uvicorn объявлены зависимостями gc-server.

# venv можно собрать и через `uv venv` — обёртка резолвит символическую ссылку
# .venv/bin/python и добавляет --ro-bind на корень интерпретатора, поэтому
# uv-managed python внутри песочницы виден. Системный python3 просто короче:
# он уже лежит под /usr, а /usr обёртка биндит всегда.

# 3. config_server.yml с секцией swarm (см. ниже)
# 4. Запуск поверхности swarm (или server.apis: [swarm] и просто gc-serve).
#    Путь к конфигу — флагом или переменной SERVER_CONFIG_PATH.
/opt/gc-cli/.venv/bin/gc-serve swarm \
  --server-config /etc/gc-cli/config_server.yml
```

## Конфигурация (config_server.yml)

Секция `swarm:` в `config_server.yml`:

```yaml
swarm:
  port: 8092                              # своя поверхность: gc-serve swarm
  # host, auth_token — по умолчанию из секции server; agent_overrides —
  # общие из config_server.yml, затем свои.

  # Корни для артефактов. Принимают абсолютный путь (как есть) ИЛИ относительный
  # (резолвится от каталога САМОГО config_server.yml — то есть от того места, где
  #  вы этот путь и видите); если опустить — дефолт:
  #   swarm_threads_dir = <state>/projects/<project-slug>/swarm/threads
  #   sandbox_root_dir  = <state>/projects/<project-slug>/swarm/sandboxes
  # (в отличие от cli_venv_root/agent_dep_dir/extra_mounts — те только абсолютные).
  # swarm_threads_dir — процессный каталог sworm'а: meta/completion/stdout/stderr.
  # (старое имя поля `threads_dir` ещё читается для совместимости.)
  swarm_threads_dir: "/var/lib/gc-cli/swarm/threads"
  sandbox_root_dir: "/var/lib/gc-cli/swarm/sandboxes"

  # Куда на хосте сохранять агентские .gc/logs из песочницы при финализации.
  # Это делает сам swarm (lifecycle.check_and_finalize), на хост-стороне —
  # поэтому логи агента сохраняются даже если bwrap снят / убит по таймауту / упал.
  # Абсолютный путь (как есть) / относительный (от каталога config_server.yml, см. выше);
  # пусто → дефолт swarm_threads_dir/<swarm_id>/agent_logs/. При задании — <swarm_logs_dir>/<swarm_id>/.
  swarm_logs_dir: "/var/lib/gc-cli/swarm/agent_logs"

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

  # Чем запускать CLI внутри песочницы. Дефолт "gc-cli". Запрос доставляется
  # нативно через `-q "<payload-json>"` (см. AR-5), отдельный shim не нужен.
  cli_command: "gc-cli"

  # Какой sandbox-tool. На данный момент поддерживается только bubblewrap.
  sandbox_tool: "bubblewrap"

  # Путь к wrapper-скрипту. Дефолт — run-cli-in-sandbox.sh ВНУТРИ пакета
  # (gc_server/swarm/, едет в колесе как package data, поэтому работает и в
  # установленном профиле). Относительный путь ищется рядом с config_server.yml,
  # затем в каталоге пакета; не нашёлся нигде — WARNING и пакетная обёртка,
  # чтобы копия шаблона с develop не роняла каждый запуск задачи.
  wrapper_path: "/opt/gc-cli/gc_server/swarm/run-cli-in-sandbox.sh"

  # Venv c gc-cli и зависимостями. Биндится --ro-bind внутрь песочницы.
  # ПРИНИМАЕТСЯ ТОЛЬКО АБСОЛЮТНЫЙ путь; пустое или относительное игнорируется.
  # Если не задать — берётся venv, в котором работает сам сервер: `gc-cli`
  # приезжает зависимостью gc-server и лежит именно там. Наружу, в PATH, uv
  # выносит только gc-serve — команды зависимостей он не экспортирует, поэтому
  # прежний PATH-fallback в установке через `uv tool install` команду не находил
  # и КАЖДЫЙ sworm падал с exit 127. Задавать вручную нужно, только если CLI
  # живёт в отдельном venv.
  cli_venv_root: "/opt/gc-cli/.venv"

  # Доп. директория с зависимостями MCP (например ~/.local/share/gc/node для playwright).
  # ПРИНИМАЕТСЯ ТОЛЬКО АБСОЛЮТНЫЙ путь.
  agent_dep_dir: "/opt/gc-cli/mcp-deps"

  # Доп. ro-bind'ы внутрь sandbox (skills, prompts, статика и т.п.). Только абсолютные.
  extra_mounts:
    - "/opt/gc-cli/prompts"
    - "/opt/gc-cli/skills"

  # Опц. пути к agent.md и skills_data_dir, которые материализуются в sandbox.
  # У них РАЗНЫЕ якоря, и это не случайность: agentmd_path целится в файл ВНУТРИ
  # дистрибутива, поэтому относительный путь резолвится от корня bundled-ассетов
  # (дефолтный prompts/server_agent.md там и лежит). skills_dir — пользовательский
  # контент, его относительный путь резолвится от каталога config_server.yml, а
  # дефолт лежит в <gc-data>/swarm_skills и переживает `uv tool upgrade`.
  # Если каталога скиллов нет, sworm идёт БЕЗ них; в лог об этом пишется только
  # когда путь задан явно.
  # Отдельного swarm-промпта нет: по умолчанию используется тот же
  # prompts/server_agent.md, что и в webhook-режиме (вход — JSON-payload).
  agentmd_path: "prompts/server_agent.md"
  skills_dir: "/var/lib/gc-cli/swarm/skills"
```

Дефолты (если поле опущено): `port=8092`, `max_concurrent=10`, `ttl_seconds=86400`, `limits={memory_mb=1024, disk_mb=2048, cpu_percent=50, timeout_seconds=1800}`, `cli_command="gc-cli"`, `sandbox_tool="bubblewrap"`.

**Важно:** `cli_venv_root`, `agent_dep_dir` и каждый элемент `extra_mounts` принимают **только абсолютные пути**. Относительные значения молча игнорируются (см. `swarm/config.py::_abs_or_empty`).

**Версия CLI.** `gc-cli` в `cli_venv_root` (или в `PATH`) должен быть той же версии, что сервер: обёртка передаёт ему флаги текущей версии (например, `--external-request`), и CLI старее сервера отвергнет незнакомый флаг — каждый swarm-ран завершится с кодом 2 (`cli_error`). Незаданный `cli_venv_root` берёт venv самого сервера, где версии совпадают сами.

## Pipeline одного sworm-запроса

```
client                 server                   wrapper.sh             bwrap + gc-cli
  │                       │                          │                       │
  │  POST /events/webhook►│                          │                       │
  │                       ├─ idempotency check       │                       │
  │                       ├─ materialize config      │                       │
  │                       │  → sandbox/.gc/config.yml│                       │
  │                       ├─ Popen(start_new_session)┼──────────────────────►│
  │  200 started ◄────────┤                          │                       │
  │                       │                          ├─ assemble BWRAP_CMD    │
  │                       │                          │   + -q "<payload-json>"│
  │                       │                          ├─ timeout ${T} bwrap ──►│ gc-cli
  │                       │                          │                       │ ⇣
  │                       │                          │                       │ LLM + MCP
  │                       │                          │                       │ (через --share-net)
  │                       │                          │                       │ ⇣
  │                       │                          │◄── exit_code ─────────┤
  │                       │                          ├─ write completion.json│
  │                       │ monitor (60s tick)       │                       │
  │  GET /runs/… ────────►├─ check_and_finalize ◄────┘                       │
  │  {status: completed} ◄┤                                                  │
```

**Этапы по шагам:**

1. **HTTP-вход.** `POST /events/webhook` приходит в `SwarmSurface.start_task`. Парсится JSON, нормализуются `task_id` и `system` (как у вебхука: `system` по умолчанию — `webhook.default_system`, занятые другими поверхностями — `400`), формируется `swarm_id = "swarm_<system>_<task_id>"` (префикс `swarm_` — чтобы каталоги отличались от обычных тредов вебхука).
2. **Одна задача — одна песочница (AR-3).** Под замком поверхности читается `meta.json`:
   - exist & `status ∈ {queued, running}` → **409 `task is running`**
     (`queued` на диск больше не пишется: `runner.start_swarm_process` сохраняет meta ровно один раз, уже после успешного `Popen`, сразу в `running`; в проверке имя оставлено ради meta, написанных прежними версиями);
   - exist & `status` final → **409 `task already finished — use a new task_id`**: песочница одноразовая, сообщения в идущую или завершённую задачу нет.
3. **Защита от перегрузки.** Если `monitor.active_count >= max_concurrent` → **503 `too many concurrent swarm processes, retry later`**.
4. **Materialize config (AR-1).** В `runner.start_swarm_process` создаётся `sandbox_root/<swarm_id>/`, конфиг собирается из 5 слоёв (deep_merge в порядке применения):
   1. **Base** — `host_config_path` (по дефолту resolve через XDG / пакетный default).
   2. **Project overlay** — `<cwd>/.gc/config.yml` относительно CWD сервера (симметрично с обычным CLI'ем).
   3. **SWARM_* env vars** — `SWARM_AGENTMD_PATH` / `SWARM_THREADS_DIR` / `SWARM_SKILLS_DIR` → `paths.*`.
   4. **`agent_overrides`** из `config_server.yml` — пути через точку (`agent.max_steps`) раскладываются во вложенные секции, секции (`agent:`, `paths:`…) применяются как есть. Плоских имён (`max_steps`) больше нет: неизвестное имя — ошибка с подсказкой пути, как у webhook'а.
   5. **Swarm-specific overrides** — всегда последние и всегда побеждают: `paths.threads_data_path=/work/.gc/threads`, `paths.logs_dump_dir=/work/.gc/logs`, `agent.use_server_subagents=False`. Это означает, что никакой `agent_overrides` не может сломать sandbox-изоляцию.

   Итог пишется атомарно в `sandbox/.gc/config.yml`.
5. **Spawn wrapper.** `Popen([wrapper_path], env=..., start_new_session=True, stdout=DEVNULL, stderr=DEVNULL)`. `start_new_session` критичен: благодаря ему PID становится главой новой process group, и `os.killpg(pid, SIGTERM)` укладывает всё дерево (wrapper → bwrap → CLI → дочерние tool-процессы).
6. **wrapper.sh.** Динамически собирает `--ro-bind` для `/lib`, `/lib64`, `/bin`, `/sbin`, `/etc`, `cli_venv_root`, `agent_dep_dir`, каждого `extra_mounts`, плюс `--bind ${SANDBOX_PATH} /work --chdir /work --unshare-all --share-net --setenv HOME /work --tmpfs /tmp`. `--tmpfs /tmp` даёт эфемерный writable `/tmp` внутри песочницы (нужен тулзам, пишущим во временные файлы — например playwright-mcp создаёт `/tmp/playwright-artifacts-*`); исчезает вместе с песочницей, изоляцию не ослабляет. CLI запускается как `gc-cli --config /work/.gc/config.yml --external-request -q "${SWARM_PAYLOAD_JSON}"` — запрос передаётся прямо в `-q` (payload как JSON-строка, симметрично webhook-режиму, где `json.dumps(payload)` уходит user-сообщением). `--external-request` объявляет канал так же, как webhook: человека нет, а слова задачи для судьи действий — запрос внешней системы (см. «Режимы разрешений на сервере» в `server.md`). Файл `payload.json` не пишется. Если задан внешний трейс, обёртка дополнительно добавляет `--trace-id "${SWARM_TRACE_ID}"` — только при непустом значении, иначе командная строка не меняется. Само поле `trace_id` из payload снято ещё в хендлере `POST /events/webhook`, чтобы не уехать в промпт модели текстом задачи.
7. **bwrap + CLI.** Команда оборачивается в `timeout --kill-after=30 --signal=TERM ${TIMEOUT_SEC}`, stdout/stderr пишутся в `swarm_threads_dir/<swarm_id>/{stdout,stderr}.log`. CLI читает `/work/.gc/config.yml`, получает запрос из `-q`, работает с LLM/MCP через расшаренный network namespace.
8. **Completion.** После выхода CLI wrapper маппит exit_code → `error_type` (`0`=success, `124/137`=timeout, `125-127`=wrapper_setup_error, остальное=cli_error) и атомарно пишет `completion.json`.
9. **Финализация.** Фоновый `SwarmMonitor` каждые 60 секунд (или lazy в момент `GET /runs/{system}/{task_id}`) зовёт `lifecycle.check_and_finalize`: проверяет deadline, ресурсные лимиты, читает `completion.json`, обновляет `meta.json` и снимает sworm с `active`-списка.
10. **Cleanup.** Janitor-тред (интервал = `ttl_seconds/10`, зажат в 60–3600с) чистит `swarm_threads_dir` и `sandbox_root_dir` **независимо** (расцеплено, как обычная чистка тредов по возрасту): `threads/<id>` удаляется по timestamp из `meta.json`, `sandboxes/<id>` — по собственному `mtime`; всё старше `ttl_seconds` сносится. Агентские логи (`swarm_logs_dir` или дефолт `swarm_threads_dir/<id>/agent_logs`) уходят вместе со своим каталогом.

## Тред задачи в общей базе (`swarm.storage`)

По умолчанию тред задачи — в файлах песочницы (`/work/.gc/threads`), а
сообщение в идущую задачу — `409`. С явной секцией `swarm.storage` в
config_server.yml тред ведётся в общей сетевой базе:

```yaml
swarm:
  storage:
    type: postgres                       # postgres | mysql | redis | s3
    dsn: "${FILE:/run/secrets/swarm_pg_dsn}"
```

- Песочница получает в материализованном `config.yml` секцию `storage.threads`:
  подключение и прочие секреты — подстановками `${ENV:GC_SWARM_DB_<ПАРАМЕТР>}`,
  значения — окружением процесса песочницы; на её диске секрета нет.
  `gc-cli` в песочнице ведёт тред задачи (`-t <task_id> --system <system>`).
- `/logs` swarm-порта читает тред из хранилища (только треды задач swarm —
  тред вебхука с тем же `(system, task_id)` он не отдаёт), а `POST` в идущую
  задачу кладёт сообщение во входящий поток треда и отвечает `queued`: его
  забирает опрос ядра в песочнице.
- Треды сервера должны лежать в той же базе (`storage.threads` config.yml —
  сетевая база), иначе секция игнорируется с предупреждением. SQLite хоста и
  своя фабрика песочнице недоступны.
- **Учётная запись — отдельная, с урезанными правами.** Агент в песочнице
  с shell может прочитать своё окружение, а значит и подключение. Дайте
  песочницам роль только на таблицы тредов (Postgres: своя роль на схему
  хранилища), не учётные данные сервера. Остальное окружение сервера песочница
  наследует, как и раньше: секреты сервера держите в `${FILE:…}`, а не в
  переменных окружения.
- Восстановление вебхука такие треды не берёт (тред помечен задачей swarm):
  жизнь песочницы ведёт монитор swarm.

## Эндпоинты

Ручки и формы ответа — как у вебхука ([server.md](server.md#вебхук)); токен —
`swarm.auth_token` (по умолчанию `server.auth_token`).

### `POST /events/webhook`

Запуск задачи.

```json
{
  "task_id": "abc123",                    // опц.; если нет — UUID4
  "system": "generic",                    // опц.; дефолт — webhook.default_system
  "query": "..."                          // весь JSON уходит агенту как запрос
}
```

**Ответы:**
- `200` — `{"status": "started", "payload", "system", "task_id"}`; с
  `swarm.storage` и идущей задачей — `{"status": "queued", …}`.
- `400` — `invalid JSON body` / `JSON body must be an object` / занятый `system` /
  невалидный `trace_id`.
- `409` — `task is running` или `task already finished — use a new task_id`.
- `503` — `too many concurrent swarm processes, retry later`.

### `GET /runs/{system}/{task_id}`

Статус задачи — общая форма статуса прогона вебхука: `status` ∈ `{running,
completed, failed}`, `started_at`, `finished_at`, `error` (для `failed`). Поля
песочницы — в `swarm` (`status` песочницы, `pid`, `deadline_at`, `exit_code`,
`error_type`…): там различаются `timeout` и `unknown_failed`, которые в общей
форме — `failed`. Зовёт `check_and_finalize` с `heavy_checks=False` (без
`du`/`psutil`-проверок — быстро для HTTP).

Если `meta.json` отсутствует — `404`.

### `GET /logs/{system}/{task_id}`

`{status: "ok", system, task_id, meta, messages, stdout, stderr}`. `messages`
читается из `messages.jsonl` (канал E — лежит внутри
`sandbox/.gc/threads/<id>/`), с фоллбэком в `swarm_threads_dir/<id>/`.
`stdout`/`stderr` — построчно из `stdout.log`/`stderr.log`. Ничего не найдено —
`404`.

### `GET /logs/all`

Задачи swarm (свежие первыми) со ссылкой `logs_url` на логи каждой.

## Файлы и куда смотреть

После запуска одной задачи на диске появляется такое:

```
${swarm_threads_dir}/swarm_<system>_<task_id>/
├── meta.json              ← главный snapshot: status, exit_code, pid, deadline_at, finished_at
├── completion.json        ← как wrapper.sh завершился (exit_code, error_type)
├── stdout.log             ← stdout CLI (полный trace; здесь же реальный диалог агента)
├── stderr.log             ← stderr CLI (python tracebacks, MCP errors)
└── agent_logs/            ← .gc/logs агента (дефолт; если задан swarm_logs_dir — туда)

${sandbox_root_dir}/swarm_<system>_<task_id>/   ← это /work внутри bwrap
└── .gc/
    ├── config.yml         ← materialized runtime config (после deep_merge)
    ├── threads/<id>/
    │   └── messages.jsonl ← канал E (обычно ПУСТО: swarm запускает CLI без -t)
    └── logs/              ← внутренние логи CLI (копируются в agent_logs при финализации)
```

> Каталоги — с префиксом `swarm_` (отличие от тредов вебхука). `threads`-каталог
> (`swarm_threads_dir`) и `sandbox` чистятся janitor'ом **независимо** по своему возрасту.

Быстрый диагностический рецепт:

```bash
SID=$(ls -t ${swarm_threads_dir} | head -1)
jq . ${swarm_threads_dir}/$SID/meta.json
cat   ${swarm_threads_dir}/$SID/completion.json
tail -50 ${swarm_threads_dir}/$SID/stderr.log
jq -r .message ${sandbox_root_dir}/$SID/.gc/threads/*/messages.jsonl | head
```

Проверка изоляции на живом sworm'е (пока работает):

```bash
WRAPPER_PID=$(jq -r .swarm_meta.pid ${swarm_threads_dir}/$SID/meta.json)
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
| AR-3 | Одна песочница на `swarm_id = swarm_<system>_<task_id>`; повтор — 409 | Защита от случайных дублей и пропуска `task_id` |
| AR-4 | Запрет per-request config overrides из payload'а | Клиент не может расширить себе перимтр через body |
| AR-5 | Запрос доставляется нативно через `-q "<payload-json>"` (payload JSON, как webhook). Исключение — `trace_id`: снимается из payload и передаётся вне него, через `SWARM_TRACE_ID` → `--trace-id`, иначе трейс попал бы в промпт модели | Без файла/шима; раньше был workaround `gc-cli-swarm`+`jq`, теперь не нужен |
| AR-6 | Read-only bind на venv, RW только `/work` | Минимум attack surface |
| AR-7 | `agent_dep_dir` для node/npm-зависимостей MCP | Изолировать тяжёлые зависимости |
| AR-8 | `--share-net` (network namespace общий с хостом) | LLM/MCP TCP-доступ; **известное ослабление изоляции** |
| AR-9 | `start_new_session=True` + `os.killpg` по PGID | Гарантированное снятие всего дерева процессов |

## Известные ограничения

- **Только Linux.** macOS/Windows — нет bubblewrap.
- **Network namespace общий с хостом** (AR-8). Sworm видит `127.0.0.1` хоста, может ходить в его сервисы. Это сознательный trade-off для доступа к LLM. Если нужна полная сетевая изоляция — wrapper надо менять.
- **Продолжения завершённой задачи нет**: песочница одноразовая, повтор `task_id` после конца — 409. В идущую задачу сообщение доходит только с `swarm.storage` (см. ниже), иначе — 409.
- **Тред песочницы — в её файлах**, если не задан `swarm.storage`; учётные данные хранилища сервера в песочницу не передаются никогда.
- **Docker MCP через stdio** (`docker exec`) — не поддерживается, потребовался бы mount `/var/run/docker.sock`. Используйте TCP/HTTP MCP.
- **Per-request config overrides из payload** запрещены (AR-4). Конфиг полностью определяется сервером.
- **Bind `/lib`, `/lib64`, `/etc`** делается динамически (только если директория существует на хосте). На Arch/Alpine это уже symlink на `/usr/lib` — wrapper это учитывает.

## Troubleshooting

| Симптом | Причина | Лечение |
|---|---|---|
| `404` на `/events/webhook` порта 8092 | Поверхность swarm не поднята | `gc-serve swarm` или `server.apis: [swarm]` |
| `503 too many concurrent swarm processes` | Лимит `max_concurrent` | Подожди или увеличь лимит |
| `409 task is running` | Дубль `task_id` для живой песочницы | Подожди или используй другой `task_id` |
| `409 task already finished — use a new task_id` | Дубль `task_id` для завершённой | `task_id` одноразовые — генерируй новый |
| Sworm падает за 5-10 секунд с `cli_error` | CLI не видит LLM-бэкенда | `curl http://127.0.0.1:<port>/v1/models` на хосте — проверь что бэкенд жив |
| `bwrap: setting up uid map` | Unprivileged user namespaces выключены | `sudo sysctl -w kernel.unprivileged_userns_clone=1`; при AppArmor — `sudo aa-disable /etc/apparmor.d/unprivileged_userns` |
| `FileNotFoundError: /work/.gc/prompts/...` | Относительные пути в `config.yml` | Замени на абсолютные (резолвятся от `/work/.gc/` внутри bwrap) |
| `MCP tools/list ... 400 Bad Request` | Неправильный transport | Для публичных MCP-серверов (playwright и т.п.) → `transport: stream_sse` или `stream_http`, не legacy `sse`/`http` |
| `messages.jsonl` пуст | CLI не успел финализировать поток (короткая задача) | Смотри `stdout.log` — там полный дамп |
| `meta.status = unknown_failed`, `error_type = process_crash` | wrapper упал до записи `completion.json` | Смотри `stderr.log` — там traceback |
| Накопилось много `threads/`, `sandboxes/` | TTL janitor не успевает или `ttl_seconds` большой | Снизь `ttl_seconds` или запусти `python -m gc_server.swarm.cleanup` вручную |
| Playwright MCP: `ENOENT mkdtemp /tmp/playwright-artifacts-*` | В песочнице нет writable `/tmp` | Уже решено: wrapper монтирует `--tmpfs /tmp`. Если своя сборка wrapper'а — добавь его |
| Playwright MCP: `chrome-for-testing not installed` / `Executable doesn't exist` | Браузер не найден внутри песочницы | Установить Chromium (`npx playwright install --with-deps chromium`) + системные либы (`libatk-1.0.so.0`, `libasound2` …); забиндить кэш Chromium через `extra_mounts` или `agent_dep_dir`; запускать MCP с `--browser chromium`; убедиться, что `PLAYWRIGHT_BROWSERS_PATH` доходит до stdio-процесса MCP |
