[Назад к README](../readme.md)

# `os_backend` — справочник конфигов

Куда уходят файловые и shell-тулы агента. Схема полей — в [configuration.md](configuration.md#секция-os_backend--где-живут-файлы-и-команды-тулов); поведение `sandbox` — в [rootbox.md](rootbox.md). Здесь — готовые блоки для копирования в `config.yml` или `<repo>/.gc/config.yml`.

`kind`: `local` (дефолт) | `harbor` (только Harbor-адаптер) | `docker` | `ssh` | `exec` | `sandbox`.

Голая строка эквивалентна объекту с `kind`:

```yaml
os_backend: docker
# то же, что:
os_backend:
  kind: docker
```

Общие ключи (имеют смысл у remote-kind; у `sandbox` `cwd` не используется):

| Ключ | Смысл |
|---|---|
| `cwd` | стартовый каталог на той стороне; `null` → `pwd` при сборке контекста |
| `env` | переменные для каждой команды модели |
| `fetch_url_execute_on` | `agent` (дефолт) — `exec_fetch_url` с хоста; `os_backend` — GET через `python3` среды исполнения. Для `local`/`sandbox` игнорируется |
| `provision_skills` | `true` (дефолт): для `docker`/`ssh`/`exec` копировать скиллы в `<launch_root>/.gc/skills_runtime/` |

Правило разделения: хост — конфиг, AGENTS.md, треды, MCP с `execute_on: agent`; среда исполнения — `fs_*`, `exec_*`, фон, скиллы, MCP с `execute_on: os_backend`. У remote-kind (`docker`/`ssh`/`exec`) тул `download_file` снят.

Удалённой стороне нужны GNU `stat`/`find`, `sh`, `cat`, `base64`, `sha256sum`; `python3` — для `run_python_code`, MCP `execute_on: os_backend` и `fetch_url_execute_on: os_backend`; `bash` — для `run_in_background`.

---

## `local`

Дефолт. Файлы и команды на машине агента.

```yaml
os_backend:
  kind: local
```

Или просто не задавать секцию.

---

## `sandbox`

Локальная изоляция **записи** shell-команд модели (`execute_command`, `run_python_code`, фон) через `bwrap` (Linux) или `sandbox-exec` (macOS). Без бинарника старт падает — тихого отката в `local` нет. Windows не поддерживается.

Файловые тулы остаются на хосте (с `write_guard` / `read_guard` / `protected_paths`). `download_file` работает. Подробности границы — [rootbox.md](rootbox.md).

```yaml
os_backend:
  kind: sandbox
  sandbox:
    workdir: null        # null — текущий launch_root
    network: true        # false — без сети у shell-команд
    extra_ro: []         # доп. read-only пути (Linux/bwrap)
```

Отдельный каталог на папку запуска (общий для запусков и тредов):

```yaml
os_backend:
  kind: sandbox
  sandbox:
    workdir: auto        # <GC_STATE_DIR>/sandboxes/<slug>/
    network: true
    extra_ro:
      - /usr/share/zoneinfo
```

Чистка: `python -m gc_server.swarm.cleanup … --core-sandboxes-dir ~/.local/state/gc/sandboxes`.

---

## `docker`

`docker exec -i [-u USER] NAME`. Контейнер должен уже работать.

```yaml
os_backend:
  kind: docker
  cwd: null
  env: {}
  docker:
    name: my-box         # обязательно: имя или ID контейнера
    user: null           # опционально: docker exec -u
    binary: docker       # podman — сюда же
  provision_skills: true
  fetch_url_execute_on: agent
```

Podman:

```yaml
os_backend:
  kind: docker
  docker:
    name: my-box
    binary: podman
```

---

## `ssh`

`ssh -T -o BatchMode=yes …`. Пароль не поддерживается: ключ или ssh-agent.

```yaml
os_backend:
  kind: ssh
  cwd: null
  env: {}
  ssh:
    host: dev.example    # обязательно
    port: 22
    user: me             # null → как в ~/.ssh/config
    key_path: ~/.ssh/id_ed25519   # null → ssh-agent / IdentityFile
    options: []          # доп. -o …
    control_master: true # один TCP на ран; на Windows выключается
  provision_skills: true
  fetch_url_execute_on: agent
```

Качать URL только с той стороны (сеть есть у удалённой машины, не у хоста агента):

```yaml
os_backend:
  kind: ssh
  ssh:
    host: dev.example
    user: me
  fetch_url_execute_on: os_backend
```

---

## `exec`

Произвольный argv-префикс перед командой (kubectl, containerd, свой wrapper).

```yaml
os_backend:
  kind: exec
  cwd: null
  env: {}
  exec:
    argv_prefix:         # обязательно, непустой
      - kubectl
      - exec
      - -i
      - -n
      - my-ns
      - my-pod
      - --
  provision_skills: true
  fetch_url_execute_on: agent
```

Другой пример:

```yaml
os_backend:
  kind: exec
  exec:
    argv_prefix: ["container", "exec", "-i", "my-box"]
```

`agent.harbor_exec_init` действует и для `docker` / `ssh` / `exec` (профиль перед командой модели).

---

## `harbor`

Только из Harbor-адаптера бенчей. Вручную в обычном `config.yml` не ставить — адаптер выставляет сам. См. [integrations/harbor/README.md](../integrations/harbor/README.md).

```yaml
os_backend: harbor
```

---

## Куда класть

- Глобально: `~/.config/gc/config.yml` (или путь из `--config`).
- На проект: `<repo>/.gc/config.yml` — deep-merge поверх базы (см. [configuration.md](configuration.md#project-local-config-опционально)).

Короткий комментарий-шпаргалка также в `packages/core/assets/config.default.yml`.
