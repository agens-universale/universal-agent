# recommended_mcp/

Каталог рекомендованных MCP-интеграций для background-agent.

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

Для `install.type: wheel` — `pip install` в активный venv.
Для `install.type: docker` — `docker compose build`.

### 3. Запустить долгоживущие MCP

```bash
python recommended_mcp/run.py
```

MCP с блоком `run:` поднимаются через `docker compose up -d`.
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
- Wheel-файлы хранятся в git как бинарные артефакты (~50-200KB каждый).
- Docker-образы собираются локально из `Dockerfile`; ни один MCP не пулится из
  docker registry. Нужен установленный Docker + docker compose v2.
