# browser_playwright — Microsoft @playwright/mcp

Интеграция официального `@playwright/mcp` (Microsoft) в background-agent.
Браузерная автоматизация через Playwright: навигация, клики, ввод, скриншоты,
скрейпинг.

Доставка — **через npx**, без Docker и без локальной сборки. Node.js
ставится автоматически в изолированную папку (`~/.agent/node/v20.18.0/` по
умолчанию, см. ниже).

## Требования

- **Node.js 20.18.0** — пин под совместимость с `@playwright/mcp@0.0.70`.
  Ставится автоматически `install.py`, **системная Node не используется**
  и не трогается.
- **Chromium** — качается Playwright'ом автоматически на этапе install.

## Установка

Из корня background-agent:

```bash
python recommended_mcp/install.py
```

Что произойдёт:

1. Проверяется `<agent_dep_dir>/node/v20.18.0/bin/node --version` (на Windows
   `…/node.exe`). Если совпадает с пином — скип. `agent_dep_dir` берётся из
   top-level ключа `agent_dep_dir` в `recommended_mcp/config.yml`, дефолт
   `~/.agent`.
2. Иначе скачивается архив Node под текущую платформу/архитектуру с
   `nodejs.org/dist/v20.18.0/`:
   - macOS arm64/x64 → `node-v20.18.0-darwin-{arch}.tar.gz`
   - Linux x64/arm64 → `node-v20.18.0-linux-{arch}.tar.xz`
   - Windows x64    → `node-v20.18.0-win-x64.zip`

   SHA256 сверяется с `SHASUMS256.txt` оттуда же. Распаковка идёт в
   `<agent_dep_dir>/node/v20.18.0/`.
3. `npm install -g @playwright/mcp@0.0.70` — прогрев npm-кэша уже на нашей
   Node.
4. `npx -y playwright install --with-deps chromium` — пред-скачивание браузера.

В конце `install.py` печатает абсолютный путь до `npx` для текущей машины —
он понадобится в следующем шаге.

## Запуск

Отдельного шага `run.py` нет — агент сам спавнит процесс `npx` через stdio
при первом tool_call и держит его живым весь цикл работы (флаг
`persistent_session: true`). По завершении агент убивает процесс и все его
дочерние (Chromium) через process-group сигналы.

## Подключение к агенту

Скопируй блок из `agent_config.snippet.yml` в секцию `mcp-servers:` своего
`config.yml`. В `command:` подойдёт `~/.agent/node/v20.18.0/bin/npx` —
`~` раскрывает `_resolve_command()` (`src/tools/connect_mcp/transport.py`).
Абсолютный путь, который печатает `install.py` в конце вывода, тоже
работает. Пример:

```yaml
mcp-servers:
  browser_playwright:
    transport: stream_stdio
    command: ~/.agent/node/v20.18.0/bin/npx
    args: ["-y", "@playwright/mcp@0.0.70"]
    persistent_session: true
    persistent_session_close_tool: browser_close
```

## Альтернатива: запуск в Docker

Если на хосте не хочется ставить Node.js (или нужна воспроизводимая среда
с фиксированной версией Chromium) — `browser_playwright` можно поднять в
контейнере. В папке уже лежат `Dockerfile` (node:22-bookworm-slim +
`@playwright/mcp@0.0.70` + chromium с deps) и `docker-compose.yml`.

Требования: **Docker + docker compose v2** (`docker compose version`).

### 1. Переключи install/run-блоки в `recommended_mcp/config.yml`

Закомментируй npx-вариант и раскомментируй docker-вариант:

```yaml
- name: browser_playwright
  path: ./browser_playwright
  install:
    type: docker
  run:
    type: docker-compose
    detach: true
```

### 2. Собери образ и подними контейнер

```bash
python recommended_mcp/install.py   # docker compose build
python recommended_mcp/run.py       # docker compose up -d
```

Сервер слушает `http://localhost:8931`, `--headless --isolated`,
`--output-dir /tmp/playwright-mcp` внутри контейнера.

Остановка:

```bash
docker compose -f recommended_mcp/browser_playwright/docker-compose.yml down
```

### 3. Поменяй snippet в `config.yml` агента на HTTP/SSE

При docker-режиме stdio не работает — агент общается с контейнером по
сети. Замени блок `browser_playwright` в своём `mcp-servers:` на:

```yaml
browser_playwright:
  transport: stream_sse
  url: http://localhost:8931
  persistent_session: true
  server-name: playwright-browser
  description: Browser automation via Playwright.
```

### Скриншоты в Docker-режиме

Sandbox контейнера разрешает запись только в `/tmp/playwright-mcp` и `/app`.
Абсолютные хостовые пути (вроде `/Users/...`) `@playwright/mcp` отклонит с
`File access denied`. Агент это обрабатывает автоматически: base64 из
`content[].type=image` декодируется в `api.py` и пишется на хост в
`./mcp_media/` (см. раздел ниже) — Docker-sandbox на это не влияет, потому
что запись делает python-процесс агента, а не контейнер.

Если хочется дополнительно пробрасывать `--output-dir` контейнера наружу —
добавь volume в `docker-compose.yml`:

```yaml
volumes:
  - ../../mcp_media:/tmp/playwright-mcp
```

## Скриншоты и Docker-sandbox

`@playwright/mcp` всегда возвращает PNG base64 в tool_result рядом с путём в
`--output-dir`. Это сотни тысяч токенов на вызов и пробивает context window.

В `src/tools/connect_mcp/api.py` есть `_rewrite_media_blocks`: агент
декодирует base64 → сохраняет файл в `./mcp_media/` (переопределяется env
`MCP_MEDIA_SAVE_DIR`) → заменяет `data` на `path`. Это решает и проблему
контекста, и ограничения sandbox'а Docker-варианта сервера.

## Обновление версии

`@latest` в `install.commands` тянет свежую версию при каждом install.py.
Если нужна фиксированная — поменяй на `@playwright/mcp@0.0.70` (или нужную)
и там, и в `agent_config.snippet.yml`.
