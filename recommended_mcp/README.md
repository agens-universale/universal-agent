# recommended_mcp/

Каталог рекомендованных MCP-интеграций для background-agent.

Здесь лежат «рецепты» сборки и подключения MCP — то, что нужно агенту, чтобы
поднять интеграцию: snippet для `config.yml` и (опционально) сборочные
артефакты. Сами исходники наших MCP живут в [`../external/vendor/`](../external/).

## Что доступно

| MCP | Источник | Установка | Платформа |
|---|---|---|---|
| `computer` | `external/vendor/computer-mcp/` (git-подкаталог monorepo) | `pip install` из git-подкаталога (`repo_url`) | **только Windows** (`platforms: [win32]`) |
| `office` | `external/vendor/office-mcp/` (git-подкаталог monorepo) | `pip install` из git-подкаталога (`repo_url`) | Linux, macOS, Windows |
| `science` | `external/vendor/science-mcp/` (git-подкаталог monorepo) | `pip install` из git-подкаталога (`repo_url`) + HTTP-daemon | Linux, macOS, Windows |
| `duckduckgo_search` | `external/vendor/ddgs-mcp/` (своя обёртка над `ddgs`, git-подкаталог monorepo + бандлится в wheel) | `pip install` из source (локально / из бандла в method-2) | Linux, macOS, Windows |
| `browser_playwright` | [@playwright/mcp](https://www.npmjs.com/package/@playwright/mcp) | **Docker** (`docker compose build` + `run.py`) или npx (Node в `gc_data`) | Linux, macOS, Windows |

По умолчанию в `config.yml` для `browser_playwright` включён **npx** (stdio, без Docker).
Для Docker раскомментируй `install.type: docker` + `run.type: docker-compose` в манифесте
и подключи агент по `stream_sse` — см. [`browser_playwright/README.md`](browser_playwright/README.md).

Полный список и комментарии — в `config.yml`.

## Как пользоваться

### 1. Установить MCP (основной путь)

После `uv tool install -e .` доступна команда `gc-mcp` с подкомандами `install`,
`list`, `remove`. MCP ставятся в **отдельный venv** агента
(`<gc_data>/recommended_mcp/venv`, Linux: `~/.local/share/gc/recommended_mcp/venv`).
Окружение `gc-cli` не затрагивается.

#### `gc-mcp install [name]` — установить + авто-подключить

```bash
# поставить ОДИН MCP по имени (pакет + авто-wire в ~/.gc/config.local.yml)
gc-mcp install duckduckgo_search

# поставить ВСЕ MCP из манифеста + авто-wire всех
gc-mcp install
```

**Ручное копирование `agent_config.snippet.yml` в `config.yml` больше не нужно.**
`gc-mcp install` автоматически записывает конфиг каждого MCP в машино-управляемый
файл `~/.gc/config.local.yml`, который агент мёржит поверх основного `config.yml`.
Ваш `config.yml` не трогается.

`gc-mcp-install` — back-compat алиас; `gc-mcp-install` (без имени) равнозначен
`gc-mcp install` (ставит и wire'ит всё).

Манифест (по умолчанию, без `--config`):

1. `mcp-meta.recommended-mcp-manifest` в config агента
2. `<gc_config>/recommended_mcp/config.local.yml` (Linux: `~/.config/gc/recommended_mcp/`)
3. встроенный `recommended_mcp/config.yml` из пакета

#### `gc-mcp list` — состояние MCP

```bash
gc-mcp list
```

Показывает таблицу: доступные MCP из манифеста, для каждого —
установлен ли пакет в venv (`INSTALLED`) и подключён ли к агенту через
`config.local.yml` (`WIRED`).

#### `gc-mcp remove <name>` — отключить + деинсталл

```bash
# убрать из конфига + деинсталл пакета из venv
gc-mcp remove science

# только un-wire из конфига (пакет в venv остаётся)
gc-mcp remove science --keep-package
```

#### Ручной путь (фолбэк / для кастомных манифестов)

Если нужен кастомный манифест — передай его через `--config`:

```bash
# gc-mcp-install — алиас gc-mcp install; ДЕЛАЕТ авто-wire в config.local.yml
gc-mcp-install --config recommended_mcp/config.local.yml

# скрипт напрямую — авто-wire НЕ делает (только установка пакетов)
cp recommended_mcp/config.local.example.yml recommended_mcp/config.local.yml   # первый раз
python3 recommended_mcp/install.py --config recommended_mcp/config.local.yml
```

При запуске `python3 recommended_mcp/install.py` (скрипт напрямую) авто-wire не выполняется —
нужно вручную скопировать `<mcp>/agent_config.snippet.yml`
в секцию `mcp-servers:` своего `config.yml` — см. раздел «4. Подключить MCP к агенту» ниже.

Для каждого `install.type` — свой шаг:

| `install.type` | Что делает |
|---|---|
| `source` | при наличии локальных исходников `external/<name>/` (клон) — `pip install <path>`; иначе (method-2, без клона) — `pip install "git+<repo_url>[@repo_ref]#subdirectory=external/<name>"` из git-подкаталога репо (нужен `git` в PATH + сеть). |
| `wheel` | `pip install` локального wheel из `<name>/dist/*.whl` |
| `pypi` | `pip install <package>` из PyPI (пин версии в `install.package`) |
| `docker` | `docker compose -f <name>/docker-compose.yml build` (локальный билд образа) |
| `npx` | bootstrap Node.js (если нет) + прогрев `npm install -g` / `npx` команд |

Для итеративной разработки наших MCP (`source`-тип) при наличии локального клона:

```bash
python recommended_mcp/install.py --editable
```

В этом режиме `pip install -e <path>` — изменения в `external/<name>/`
подхватываются на следующем запуске MCP без переустановки. Для `wheel/pypi/docker/npx`
флаг игнорируется.

#### Рецепт `install.type: wheel` (vendored .whl)

Когда MCP поставляется **готовым wheel** (без исходников в `external/` и без PyPI):

```
recommended_mcp/my_mcp/
  dist/my-mcp-1.0.0-py3-none-any.whl   # ровно один файл
  agent_config.snippet.yml
  README.md
```

Блок в манифесте:

```yaml
  - name: my_mcp
    path: ./my_mcp
    install:
      type: wheel
```

`gc-mcp-install` сделает `pip install` этого wheel в `recommended_mcp/venv`.
В `agent_config.snippet.yml` укажи `command:` из entry point пакета (как в
`pip show -f` / `--help` после установки). Для публичных пакетов с PyPI проще
`install.type: pypi` — см. `duckduckgo_search`.

### 3. Запустить долгоживущие MCP

**gc-ua** поднимает недостающие HTTP/SSE-daemon'ы **по умолчанию** (для серверов
из `mcp-servers`, пересечение с манифестом); отключить: `gc-ua --no-auto-mcp`.
**gc-cli** — только с флагом **`-a` / `--auto-mcp`**. cwd не важен; гасятся только
процессы, поднятые этим запуском:

```bash
gc-ua
gc-cli -a -q "найди статьи про ..."
```

Ручной запуск (отдельный терминал, Ctrl+C гасит поднятое в этом сеансе):

```bash
python recommended_mcp/run.py
```

| `run.type` | Поведение |
|---|---|
| `command` | Daemon-команда (`science-mcp --transport http`). PID-файл в `gc_paths()['data']/recommended_mcp/pids/`. Повторный запуск пропускает уже живые процессы. `run.py` **ждёт Ctrl+C** и гасит только то, что сам поднял в этом сеансе. |
| `docker-compose` | `docker compose up [-d]` (как раньше). |

Остановить daemon'ы из манифеста вручную:

```bash
python recommended_mcp/run.py --stop
python recommended_mcp/run.py --config recommended_mcp/config.local.yml --stop
```

MCP без блока `run:` пропускаются — агент спавнит их по stdio сам.

### 4. Подключить MCP к агенту (ручной путь)

> При использовании `gc-mcp install` этот шаг **не нужен** — авто-wire делает его автоматически
> через `~/.gc/config.local.yml`.

Если ты использовал скрипт напрямую (`python3 recommended_mcp/install.py`):
скопируй содержимое `<mcp>/agent_config.snippet.yml` в секцию `mcp-servers:`
своего `config.yml` агента. `install.py` **не редактирует** `config.yml` агента
автоматически — чтобы не повредить твои кастомные правки.
(`gc-mcp-install` и `gc-mcp install` авто-wire делают — этот шаг не нужен.)

## Как пропустить MCP

- **Предпочтительно:** скопируй `config.local.example.yml` → `config.local.yml`,
  оставь только нужные блоки, ставь через `--config recommended_mcp/config.local.yml`.
- **Или:** закомментируй лишние блоки в `config.yml`.

`computer` на Linux и так пропускается (`platforms: [win32]`).

### science + duckduckgo (типичный Linux-набор)

```bash
gc-mcp install science
gc-mcp install duckduckgo_search
gc-cli -a -q "..."
```

Дальше:

1. Подключение в `~/.gc/config.local.yml` выполнено автоматически шагом выше.
2. **science** — HTTP-daemon: поднимает `gc-cli -a` (или вручную `python recommended_mcp/run.py`).
3. **duckduckgo_search** и **office** — stdio, агент спавнит сам после install.

## Как добавить новый MCP

1. Создай подпапку `recommended_mcp/<name>/`.
2. Положи туда установочные артефакты (по типу):
   - для wheel-пакета: `dist/<package>-<version>-py3-none-any.whl`
   - для PyPI-пакета: достаточно snippet + README (wheel в репо не нужен)
   - для docker-сервера: `Dockerfile` + `docker-compose.yml`
3. Добавь `agent_config.snippet.yml` — блок для `mcp-servers:` в config.yml агента.
4. Добавь `README.md` — требования, ограничения, альтернативы.
5. Допиши блок в `recommended_mcp/config.yml`:

```yaml
mcps:
  - name: <name>
    path: ./<name>
    install:
      type: wheel | pypi | docker
      package: some-mcp==1.2.3            # для type: pypi
      platforms: [win32, linux, darwin]   # опционально, whitelist
    run:                                  # опционально, только для долгоживущих
      type: docker-compose
      detach: true
```

6. Если `install.type` / `run.type` — новый, расширь switch в `install.py` /
   `run.py` под свой тип.

## Ограничения

- MCP venv: `<gc_data>/recommended_mcp/venv/`. Агент резолвит `office-mcp` и др. оттуда
  автоматически (PATH prepend при spawn). Рядом: `pids/`, `runtime.json`.
- При `type: source` без бампа `version` в `pyproject.toml` pip не перетрёт
  уже стоящую копию. При итеративной разработке используй `--editable`.
- Docker-образы по умолчанию собираются локально из `Dockerfile`; для pull'а
  из реестра нужен соответствующий `install.type` (на момент написания —
  не реализован, реализовать по необходимости).
