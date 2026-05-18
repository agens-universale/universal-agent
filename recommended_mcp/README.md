# recommended_mcp/

Каталог рекомендованных MCP-интеграций для background-agent.

Здесь лежат «рецепты» сборки и подключения MCP — то, что нужно агенту, чтобы
поднять интеграцию: snippet для `config.yml` и (опционально) сборочные
артефакты. Сами исходники наших MCP живут в [`../external/`](../external/).

## Что доступно

| MCP | Источник | Установка |
|---|---|---|
| `computer` | `external/computer-mcp/` (исходники в monorepo) | `pip install` из source |
| `office` | `external/office-mcp/` (исходники в monorepo) | `pip install` из source |

При расширении сюда же подключаются публичные MCP — npx-based (`@playwright/mcp`),
PyPI-пакеты, docker-образы из реестра. Список — в `config.yml`.

## Как пользоваться

### 1. Активируй venv агента

```bash
# Linux / macOS
source .venv/bin/activate

# Windows (PowerShell)
.venv\Scripts\Activate.ps1
```

### 2. Установить MCP

```bash
python recommended_mcp/install.py
```

Для каждого `install.type` — свой шаг:

| `install.type` | Что делает |
|---|---|
| `source` | `pip install <path>` из `external/<name>/` (PEP 517 build из исходников) |
| `wheel` | `pip install` локального wheel из `<name>/dist/*.whl` |
| `docker` | `docker compose -f <name>/docker-compose.yml build` (локальный билд образа) |
| `npx` | bootstrap Node.js (если нет) + прогрев `npm install -g` / `npx` команд |

Для итеративной разработки наших MCP (`source`-тип):

```bash
python recommended_mcp/install.py --editable
```

В этом режиме `pip install -e <path>` — изменения в `external/<name>/`
подхватываются на следующем запуске MCP без переустановки. Для `wheel/docker/npx`
флаг игнорируется.

### 3. Запустить долгоживущие MCP

```bash
python recommended_mcp/run.py
```

MCP с блоком `run:` (например `docker-compose`) поднимаются как daemon.
MCP без блока `run:` пропускаются — агент спавнит их по stdio сам.

### 4. Подключить MCP к агенту

Скопируй содержимое `<mcp>/agent_config.snippet.yml` в секцию `mcp-servers:`
своего `config.yml` агента. `install.py` **не редактирует** `config.yml` агента
автоматически — чтобы не повредить твои кастомные правки.

## Как пропустить MCP

Закомментируй его блок в `config.yml`.

## Как добавить новый MCP

1. Создай подпапку `recommended_mcp/<name>/`.
2. Положи туда установочные артефакты:
   - для wheel-пакета: `dist/<package>-<version>-py3-none-any.whl`
   - для docker-сервера: `Dockerfile` + `docker-compose.yml`
3. Добавь `agent_config.snippet.yml` — блок для `mcp-servers:` в config.yml агента.
4. Добавь `README.md` — требования, ограничения, альтернативы.
5. Допиши блок в `recommended_mcp/config.yml`:

```yaml
mcps:
  - name: <name>
    path: ./<name>
    install:
      type: wheel | docker
      platforms: [win32, linux, darwin]   # опционально, whitelist
    run:                                  # опционально, только для долгоживущих
      type: docker-compose
      detach: true
```

6. Если `install.type` / `run.type` — новый, расширь switch в `install.py` /
   `run.py` под свой тип.

## Ограничения

- `install.py` ставит пакеты в **активный Python** (`sys.executable`). Если venv
  не активирован — установка пойдёт в system-python, чего обычно не хочется.
- При `type: source` без бампа `version` в `pyproject.toml` pip не перетрёт
  уже стоящую копию. При итеративной разработке используй `--editable`.
- Docker-образы по умолчанию собираются локально из `Dockerfile`; для pull'а
  из реестра нужен соответствующий `install.type` (на момент написания —
  не реализован, реализовать по необходимости).
