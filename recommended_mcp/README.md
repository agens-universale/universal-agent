# recommended_mcp/

Каталог рекомендованных MCP-интеграций для background-agent.

Здесь лежат «рецепты» сборки и подключения MCP — то, что нужно агенту, чтобы
поднять интеграцию: snippet для `config.yml` и (опционально) сборочные
артефакты. Сами исходники наших MCP живут в [`../external/`](../external/).

## Что доступно

| MCP | Источник | Установка | Платформа |
|---|---|---|---|
| `computer` | `external/computer-mcp/` (исходники в monorepo) | `pip install` из source | **только Windows** (`platforms: [win32]`) |
| `office` | `external/office-mcp/` (исходники в monorepo) | `pip install` из source | Linux, macOS, Windows |
| `science` | `external/science-mcp/` (исходники в monorepo) | `pip install` из source + HTTP-daemon | Linux, macOS, Windows |
| `duckduckgo_search` | [ddgs-mcp](https://pypi.org/project/ddgs-mcp/) (PyPI) | `pip install` из PyPI | Linux, macOS, Windows |
| `browser_playwright` | [@playwright/mcp](https://www.npmjs.com/package/@playwright/mcp) | **Docker** (`docker compose build` + `run.py`) или npx (Node в `gc_data`) | Linux, macOS, Windows |

По умолчанию в `config.yml` для `browser_playwright` включён **npx** (stdio, без Docker).
Для Docker раскомментируй `install.type: docker` + `run.type: docker-compose` в манифесте
и подключи агент по `stream_sse` — см. [`browser_playwright/README.md`](browser_playwright/README.md).

Полный список и комментарии — в `config.yml`.

## Как пользоваться

### 1. Установить MCP (одна команда)

После `uv tool install -e .` MCP ставятся в **отдельный venv** агента
(`<gc_data>/recommended_mcp/venv`, Linux: `~/.local/share/gc/recommended_mcp/venv`).
Окружение `gc-cli` не
затрагивается. `gc-cli` может работать из любой папки (в т.ч. временной).

```bash
# вариант A: команда из PATH (после uv tool install)
gc-mcp-install --config recommended_mcp/config.local.yml

# вариант B: из репозитория
cp recommended_mcp/config.local.example.yml recommended_mcp/config.local.yml   # первый раз
python3 recommended_mcp/install.py --config recommended_mcp/config.local.yml
```

Манифест для install и `gc-cli -a` (по умолчанию, без `--config`):

1. `mcp-meta.recommended-mcp-manifest` в config агента
2. `<gc_config>/recommended_mcp/config.local.yml` (Linux: `~/.config/gc/recommended_mcp/`)
3. встроенный `recommended_mcp/config.yml` из пакета

Полный набор из встроенного `config.yml`:

```bash
gc-mcp-install
```

Тот же `--config` работает и для `run.py`.

Для каждого `install.type` — свой шаг:

| `install.type` | Что делает |
|---|---|
| `source` | `pip install <path>` из `external/<name>/` (PEP 517 build из исходников) |
| `wheel` | `pip install` локального wheel из `<name>/dist/*.whl` |
| `pypi` | `pip install <package>` из PyPI (пин версии в `install.package`) |
| `docker` | `docker compose -f <name>/docker-compose.yml build` (локальный билд образа) |
| `npx` | bootstrap Node.js (если нет) + прогрев `npm install -g` / `npx` команд |

Для итеративной разработки наших MCP (`source`-тип):

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

**gc-cli** с флагом **`-a` / `--auto-mcp`** поднимает недостающие HTTP/SSE-daemon'ы
перед агентом (cwd не важен) и гасит только те, что поднял в этом запуске:

```bash
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

### 4. Подключить MCP к агенту

Скопируй содержимое `<mcp>/agent_config.snippet.yml` в секцию `mcp-servers:`
своего `config.yml` агента. `install.py` **не редактирует** `config.yml` агента
автоматически — чтобы не повредить твои кастомные правки.

## Как пропустить MCP

- **Предпочтительно:** скопируй `config.local.example.yml` → `config.local.yml`,
  оставь только нужные блоки, ставь через `--config recommended_mcp/config.local.yml`.
- **Или:** закомментируй лишние блоки в `config.yml`.

`computer` на Linux и так пропускается (`platforms: [win32]`).

### science + duckduckgo (типичный Linux-набор)

```bash
gc-mcp-install --config recommended_mcp/config.local.yml
gc-cli -a --config demo_config_openai.yml -q "..."
```

Дальше:

1. Snippet'ы уже есть в `demo_config_openai.yml` / `config.yml` (секция `mcp-servers:`).
2. **science** — HTTP-daemon: поднимает `gc-cli -a` (или вручную `python recommended_mcp/run.py --config …`).
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
