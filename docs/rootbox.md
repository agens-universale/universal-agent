# Security policy: `command_moderation`, `write_guard`, `read_guard`, `url_policy`, `protected_paths`

Клиент поддерживает пять **независимых** слоёв безопасности на одном уровне внутри `security:`.

| Слой | Что блокирует | Тулы под защитой | Error type |
|---|---|---|---|
| `command_moderation.mode` | опасные команды/код через LLM-судью; в `deny` fail-CLOSED если judge недоступен, в `ask` — запрос пользователю | `execute_command`, `run_python_code` | `unsafe_command_blocked`, `unsafe_command_cancelled_by_user` |
| `write_guard` | запись вне `launch_root` + `extra_paths` | `write_to_file`, `append_to_file`, `search_and_replace`, write-fragments в `execute_command`, `download_file` | `write_guard_violation` |
| `read_guard` | чтение вне `allowed_read_roots`, в `denied_*`, по запрещённым паттернам/расширениям | `read_file`, `list_files`, `search_files` | `read_guard_violation` |
| `url_policy` | URL по схеме или `url_blacklist` (включая SSRF-подсети) | `fetch_url`, `simple_fetch_url`, `download_file` | `url_validation_failed`, `url_blocked_by_policy` |
| `protected_paths` | запись/чтение `.gc` и инструкций; детерминированный command-guard на shell-команды | все write-тулы + `execute_command`, `run_python_code` | `protected_path_denied` |

Слои независимы: `read_guard` блокирует чтение в любом `command_moderation.mode` (даже `allow`); `url_policy` не зависит от `mode`; `write_guard` независим от `read_guard`. **Secure-by-default: все пять слоёв включены по умолчанию.** `enabled: true` стоит и в pydantic-дефолтах (`config_modules/security.py`), и в поставляемом `config.default.yml`. Чтобы слой выключить, нужен явный `enabled: false` — обратное утверждение («safe-by-default-OFF») в старых редакциях этого документа было неверным.

`os_backend: sandbox` — не шестой слой `security:`, а другая граница (YAML — [os_backends.md](os_backends.md)): файловые тулы по-прежнему держат `write_guard`/`read_guard`/`protected_paths`, а shell-команды модели (`execute_command`, `run_python_code`, фон) выполняются в bwrap / sandbox-exec с записью только в `launch_root` (плюс временные каталоги — иначе не работает `tempfile`). Это закрывает то, что `command_moderation` ловит лишь эвристикой: произвольная команда не запишет за корень даже при `mode: allow`. Граница именно на запись: чтение (включая `$HOME`) и сеть (`sandbox.network`, по умолчанию включена) у команды такие же, как при `os_backend: local`; окружение процесса агента в команду не попадает — только allowlist (`PATH`, `HOME`, локаль, прокси, CA-сертификаты), как у `run_python_code`. Жёсткие ссылки наружу bwrap режет сам (`EXDEV`: корень и остальное — разные bind-монтирования). MCP с `execute_on: os_backend` при `sandbox` идёт той же обёрткой (carrier `python3` и stdio-сервер под bwrap / sandbox-exec); `download_file` остаётся — файлы и так хостовые.

## `security.protected_paths` — floor-защита `.gc` (ON по умолчанию)

«Пол» безопасности: работает поверх всех остальных слоёв и не отключается вместе с `write_guard`/`read_guard`/`command_moderation`.

```yaml
security:
  protected_paths:
    enabled: true   # дефолт; явно задавать не нужно
    extra: []       # дополнительные пути, приравниваемые к operational
```

### Что защищает

Четыре класса путей с разным решением:

| Класс | Примеры | Решение |
|---|---|---|
| `instructions` | `AGENTS.md`, `CLAUDE.md`, инструктивные файлы | `ask` — запрос подтверждения у пользователя |
| `media` | копии вложенных изображений `<threads_data_path>/images/`, свой карман процесса `…/images/_staging/<pid>/`, картинки из ответов MCP-тулов `<gc_cache>/mcp-media/` | чтение — без вопроса; запись тулами — `deny`; команда — `ask` |
| `operational` | весь `.gc/` проекта, `~/.local/state/gc/` (config/state/cache), карманы других процессов `…/images/_staging/<другой pid>/` | `deny` — блок без возможности подтвердить |
| `skills` | `.gc/skills/`, `~/.local/share/gc/skills/` | `allow` — запись разрешена (скиллы можно редактировать) |

`media` проверяется раньше `operational`: по умолчанию его корни лежат внутри `<gc_state>` и
`<gc_cache>`. Карманы других процессов (`_staging/<другой pid>`) — вставки чужих сессий до
отправки — в `media` не входят и считаются `operational`, где бы ни лежал корень хранилища
(в том числе свой `threads_data_path` вне `<gc_state>` и временный корень SDK): чтение
закрыто полом, запись и команды — `deny`; разрешение корня хранилища в периметре
`read_guard` на них не распространяется. Класс считается по пути как написан: `..`
нормализуется, симлинки — нет (периметр `read_guard` их разворачивает и видит путь вне
проекта). Корни считает `security/media_roots.py` по конфигу запуска; вызывающие без конфига
(судья команд, diff-TUI) класса не видят — для них эти пути остаются `operational`.

### Детерминированный command-guard (tripwire)

Перед тем как команда уйдёт в subprocess, `execute_command` и `run_python_code` проходят через `command_guard` — детерминированный сканер токенов команды. Он ловит прямые обращения к protected-путям в тексте команды (например, `rm -rf .gc`) и блокирует их **до** запуска процесса. Это закрывает сценарий, при котором агент сам себе стирал `.gc` через `execute_command` (баг TSKBCKGRNDGNT-2).

Обфускацию (`$(printf '.%s' gc)`, base64, переменные окружения) tripwire не ловит — второй рубеж там LLM-судья (`command_moderation`).

Список protected-путей передаётся в prompt судьи как `never_touch_paths`; если судья недоступен — fail-closed (команда блокируется).

### Long-memory backup

Проектная долгосрочная память (`.gc/GIGACODE_memory.md`) автоматически резервируется в `<gc_state>/projects/<slug>/GIGACODE_memory_backup.md`. Этот путь лежит **вне дерева проекта**, поэтому при вайпе `.gc/` бэкап сохраняется. При следующем обращении к памяти `storage.read()` / `storage.stat()` автоматически восстанавливает основной файл из бэкапа.

## Базовый пример конфига

```yaml
security:
  command_moderation:
    enabled: true                      # дефолт; false полностью отключает судью
    mode: ask                          # allow | ask | deny
    allowed_command_examples: []
  write_guard:
    enabled: true                      # дефолт
    mode: ask
    extra_paths: []
  read_guard:
    enabled: true                      # дефолт
    mode: ask                          # allow | ask | deny
    allowed_read_roots: []             # пусто → [launch_root]; ["*"] → allow-by-default
    allowed_read_paths: []             # exception-override над denied_*
    denied_read_paths: []
    denied_file_patterns: []           # fnmatch по basename
    denied_extensions: []              # точное совпадение расширения
  url_policy:
    enabled: true                      # дефолт
    allowed_schemes: ["http", "https"]
    url_blacklist: []                  # exact host, glob host (*.example), или URL glob
```

## `command_moderation.mode` — судья команд/кода

Управляет только `execute_command` и `run_python_code`:

- `allow` — выполнять без judge (документированная дыра — judge просто отключён);
- `ask` — `safe` выполнять сразу; `potentially_dangerous` или недоступный judge → запрос подтверждения пользователя;
- `deny` — безопасное выполнять, опасное → блок с `unsafe_command_blocked`.

**Недоступный judge**: в `deny` — fail-CLOSED, блок (`reason: "Judge is unavailable; fail-closed policy blocks execution"`). В `ask` — запрос подтверждения (`reason: "Judge is unavailable; user confirmation is required."`). В `deny` это закрывает класс атак через python escape: `print(open('/etc/passwd').read())` блокируется даже если `read_guard` декорирует только `read_file`.

`allowed_command_examples` — positive examples для LLM judge (ориентир безопасного профиля проекта, не строгий allowlist).

**Запрет на обход.** Любой отказ уходит агенту с полем `error.guidance`: судья
оценивает конкретную команду, а не намерение, и модель охотно переписывает
заблокированное в эквивалент (`rm -rf X` → `rm X/* && rmdir X`). Поле несёт явный
запрет повторять команду и запускать переписанный/раздробленный вариант.
Формулировка одна на весь периметр — константа `NO_WORKAROUND_NOTICE`
(`packages/core/src/gc_core/security/command_moderation.py`), её же переиспользует детерминированный
command-guard защищённых путей.

Это **смягчение, а не граница**: инструкция адресована модели и ничего не
принуждает. Настоящей границей остаётся песочница — см. `docs/swarm.md`.

## `write_guard` — write-периметр

Включён по умолчанию; выключается явным `enabled: false`. Ограничивает write-операции директорией `launch_root` + `extra_paths`. Относительные пути в `extra_paths` разрешаются от каталога `config.yml`.

```yaml
security:
  write_guard:
    enabled: true
    extra_paths:
      - "./build"                      # относительно config.yml
      - "/tmp/agent-scratch"           # абсолютный
      - "~/Downloads/agent-out"        # ~ резолвится
```

`extra_paths` расширяет право ЗАПИСИ, но не отслеживание правок: файл вне
`launch_root` показывает дифф в ленте и не попадает на экран изменений,
где бы он ни лежал.

## `read_guard` — read-периметр

Симметричен `write_guard`, но для чтения.

**Дефолт в shipped `config.yml` / `config.default.yml`** (защита системных секретов; в pydantic-дефолтах кода списки пустые):

```yaml
security:
  read_guard:
    enabled: true
    mode: deny
    allowed_read_roots: []             # = [launch_root]
    allowed_read_paths:
      - "~/.local/share/gc/skills"    # user-layer skills
      - "<launch_root>/.gc/skills"    # project-layer skills (внутри denied .gc)
    denied_read_paths:
      - "~/.ssh"
      - "~/.aws"
      - "~/.config/gcloud"
      - "~/.gnupg"
      - "/etc/ssl/private"
      - "/proc"
      - "/sys"
      - "~/.local/state/gc"            # state cli (треды, auth-токены)
      - "<launch_root>/.gc"            # project-local state
    denied_file_patterns:
      - ".env"
      - ".env.*"
      - "*id_rsa*"
      - "*id_ed25519*"
      - "*.pem"
      - "*credentials*.json"
      - "*.sqlite"
    denied_extensions:
      - ".key"
      - ".pfx"
      - ".jks"
```

**Альтернативный «открытый» режим** (для диагностики всей машины):

```yaml
security:
  read_guard:
    enabled: true
    mode: deny
    allowed_read_roots: ["*"]          # allow-by-default; denied_* по-прежнему режут
```

Особенности:

- Плейсхолдеры `~`, `%USERPROFILE%`, `<launch_root>` резолвятся в путях `denied_*` / `allowed_*`.
- Symlink защита: путь нормализуется через `realpath` до проверки.
- Скиллы (`<launch_root>/.gc/skills/`, `~/.local/share/gc/skills/`) — через `allowed_read_paths`; bundled system skills — через `_resolve_skills_root` в `extra_allowed_paths`.
- `fs_view_image` читает пути класса `media` (хранилище картинок тредов, каталог MCP-медиа) **с хоста** агента, а не через среду исполнения: в Harbor остальные пути он читает из контейнера, как `fs_read_file`, а картинки хранилища и скриншоты MCP лежат у агента. Каталог вывода Playwright MCP (`<gc_cache>/playwright-mcp`) в `media` не входит — скриншот смотрят по пути из блока картинки результата MCP.

## `url_policy` — schema + SSRF-blacklist

Проверяется для **исходного** URL в `fetch_url`/`simple_fetch_url`/`download_file` до сетевого запроса. Redirect targets валидируются per-hop в `simple_fetch_url` и `download_file`.

`mode` (как у write/read_guard):

| mode | Поведение при матче blacklist / disallowed scheme |
|---|---|
| `deny` (default) | блок c `url_blocked_by_policy` / `url_validation_failed` |
| `ask` | Prompt: «Разрешить запрос к URL вне policy? [y/N]:». Confirm → пропустить + audit `out_of_perimeter`; decline → `url_blocked_by_policy_cancelled_by_user`. Auto-decline если stdin не tty. |
| `allow` | Пропустить + audit `risk_flags: ["out_of_perimeter"]` |

```yaml
security:
  url_policy:
    enabled: true
    mode: deny                        # allow | ask | deny
    allowed_schemes: ["http", "https"]
    url_blacklist:
      - "169.254.*"                    # link-local + AWS/GCP IMDS 169.254.169.254
      - "127.*"                        # loopback
      - "10.*"                         # RFC1918
      - "192.168.*"
      - "172.1[6-9].*"                 # RFC1918 172.16-19
      - "172.2[0-9].*"                 # 172.20-29
      - "172.3[0-1].*"                 # 172.30-31
      - "0.0.0.0"
      - "localhost"
      - "*.internal.example"           # glob на suffix
```

Матчинг через `fnmatch` по host и по полному URL (case-insensitive).

## `security.download` — каталог и лимит для `download_file`

Не enforcement-слой: только настройки builtin-тула `download_file` (без `enabled` / `mode`). Вкл/выкл тула — через `agent.stop_tools`.

```yaml
security:
  download:
    download_dir: downloads       # default; относительный → от launch_root
    max_bytes: 52428800           # default 50 MiB; макс. размер тела HTTP-ответа
```

| Поле | Поведение |
|---|---|
| `download_dir` | Каталог сохранения. Относительный путь резолвится от `launch_root` (не от каталога `config.yml`). Абсолютный / `~/…` — после `expanduser` / `expandvars`. Пустой → `invalid_download_dir`. |
| `max_bytes` | Лимит размера тела ответа при чтении; превышение → `file_too_large`, файл не создаётся. |

Запись в `download_dir` дополнительно проходит `write_guard` (если `enabled: true`): абсолютный каталог вне периметра нужно добавить в `write_guard.extra_paths`.

### SSRF-blacklist

> В shipped `config.yml` / `demo_config.yml` уже задан базовый SSRF-blacklist
> (metadata, loopback/link-local, RFC1918). В pydantic-дефолтах кода список
> пустой (`[]`) — для тестов и bench-конфигов. Списки в security мерджатся
> конкатенацией с dedup: per-project `.gc/config.yml` может только **расширить**
> blacklist, не сбросить global.

Набор закрывает три класса internal-таргетов, к которым агента нельзя
пускать через `fetch_url` / `simple_fetch_url`:

| Группа | Что закрывает | Зачем |
|---|---|---|
| **Cloud metadata** | `169.254.169.254`, `metadata.google.internal` (+поддомены), `metadata.azure.com` (+поддомены) | IMDS облачных провайдеров отдаёт креды/токены инстанса. Классический SSRF-pivot: агента просят сходить на metadata-endpoint и слить IAM-роль. |
| **Loopback / link-local** | `localhost`, `127.*`, `::1`, `0.0.0.0`, `169.254.*` | Сервисы, слушающие только на localhost (админки, debug-порты, незащищённые локальные БД), и весь link-local блок. |
| **RFC1918 private** | `10.*`, `172.16–31.*`, `192.168.*` | Соседи по внутренней сети (internal API, БД, k8s-сервисы), которые не должны быть доступны агенту. |

```yaml
security:
  url_policy:
    enabled: true
    mode: deny
    allowed_schemes: ["http", "https"]
    url_blacklist:
      # --- Cloud metadata endpoints ---
      - "169.254.169.254"              # AWS / GCP / OpenStack IMDS
      - "metadata.google.internal"     # GCP metadata (apex)
      - "*.metadata.google.internal"   # GCP metadata (поддомены)
      - "metadata.azure.com"           # Azure metadata (apex)
      - "*.metadata.azure.com"         # Azure metadata (поддомены)
      # --- Loopback / link-local ---
      - "localhost"
      - "127.*"                        # весь loopback-блок 127.0.0.0/8
      - "::1"                          # IPv6 loopback
      - "0.0.0.0"
      - "169.254.*"                    # link-local (включая IMDS-подсеть)
      # --- RFC1918 private networks ---
      - "10.*"                         # 10.0.0.0/8
      - "192.168.*"                    # 192.168.0.0/16
      - "172.1[6-9].*"                 # 172.16-19 (часть 172.16.0.0/12)
      - "172.2[0-9].*"                 # 172.20-29
      - "172.3[0-1].*"                 # 172.30-31
```

## CLI override'ы

Флаги есть не у всех секций. Полноценно переключаются из CLI только
`write_guard` и `read_guard` — у них флаг меняет и `enabled`, и режим:

```bash
# Полностью выключить write-периметр (опасно):
gc-cli -q "..." --no-write-guard

# Полностью выключить read_guard:
gc-cli -q "..." --no-read-guard
# или
gc-cli -q "..." --read-guard false

# Поменять режим read_guard без правки config:
gc-cli -q "..." --read-guard-mode ask

# Включить write_guard явно:
gc-cli -q "..." --write-guard true
```

**`--command-moderation` — исключение: он меняет только `mode`, но не
`enabled`.** Если в конфиге стоит `security.command_moderation.enabled: false`,
флаг молча ни на что не влияет — судья не запускается ни в каком режиме, и в
логах не будет ни одного события:

```bash
# Работает, только если enabled уже true:
gc-cli -q "..." --command-moderation deny

# Включить сам слой можно generic-override'ом (заменяет секцию целиком,
# поэтому allowed/denied_command_examples из конфига теряются):
gc-cli -q "..." --command_moderation "{enabled: true, mode: deny}"
```

Секции `protected_paths` и `url_policy` из CLI не переключаются вовсе.

Приоритет: **CLI override → `config.yml` → дефолт в коде**.

## Формат ошибок (JSON envelope тулов)

Все блокировки возвращают envelope с `ok: false`, `status: "blocked"` (или специфичным) и структурированной `error`. Примеры:

```json
// read_guard блокировал /etc/hosts вне allowed_read_roots
{
  "tool": "read_file",
  "ok": false,
  "status": "blocked",
  "error": {
    "type": "read_guard_violation",
    "path": "/etc/hosts",
    "operation": "read",
    "reason": "path is outside allowed_read_roots: ['/work/orchestrator/scratchpad/lab']",
    "matched_rule": "allowed_read_roots",
    "mode": "deny"
  }
}

// url_policy блокировал SSRF к IMDS
{
  "tool": "fetch_url",
  "ok": false,
  "error": {
    "type": "url_blocked_by_policy",
    "url": "http://169.254.169.254/latest/meta-data",
    "reason": "URL host or URL matched blacklist rule.",
    "matched_rule": "169.254.*"
  }
}

// command_moderation fail-closed (judge offline, mode=deny)
// (no command/code preview — model already has full args in the tool call)
{
  "tool": "run_python_code",
  "ok": false,
  "error": {
    "type": "unsafe_command_blocked",
    "mode": "deny",
    "reason": "Judge is unavailable; fail-closed policy blocks execution.",
    "guidance": "Do NOT retry the same command, and do NOT run a rewritten, split-up or otherwise equivalent command that reaches the same result …",
    "risk_flags": []
  }
}

// write_guard блокировал write outside launch_root
{
  "tool": "write_to_file",
  "ok": false,
  "error": {
    "type": "write_guard_violation",
    "launch_root": "/work/lab",
    "allowed_write_roots": ["/work/lab"],
    "path": "/etc/passwd",
    "operation": "write"
  }
}
```

Все policy-решения дополнительно пишутся в run logger как `security_policy_decision` (без полного payload команды/кода).

## Полный сценарий: hardened-конфиг для продакшен-агента

```yaml
security:
  command_moderation:
    mode: deny
    allowed_command_examples:
      - "ls -la"
      - "git status"
      - "pytest tests/"
  write_guard:
    enabled: true
    extra_paths:
      - "./build"
  read_guard:
    enabled: true
    mode: deny
    denied_read_paths:
      - "~/.ssh"
      - "~/.aws"
      - "~/.config/gcloud"
      - "/proc"
      - "/sys"
      - "<launch_root>/.gc"
    allowed_read_paths:
      - "<launch_root>/.gc/skills"
    denied_file_patterns:
      - ".env"
      - ".env.*"
      - "*.pem"
      - "*id_rsa*"
      - "*credentials*.json"
    denied_extensions:
      - ".key"
  url_policy:
    enabled: true
    allowed_schemes: ["https"]
    url_blacklist:
      - "169.254.*"
      - "127.*"
      - "10.*"
      - "192.168.*"
      - "172.1[6-9].*"
      - "172.2[0-9].*"
      - "172.3[0-1].*"
      - "localhost"
```

Этот конфиг закрывает: SSRF к internal IP, чтение системных секретов, write вне проекта, любые опасные shell-команды и python escape через `run_python_code`.
