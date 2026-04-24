# browser_playwright — Microsoft @playwright/mcp

Интеграция официального `@playwright/mcp` (Microsoft) в background-agent.
Браузерная автоматизация через Playwright: навигация, клики, ввод, скриншоты,
скрейпинг.

Доставка — **через npx**, без Docker и без локальной сборки. Node.js
ставится автоматически (см. ниже).

## Требования

- **Node.js 18+** — ставится автоматически `install.py`, см. раздел «Установка».
- **Chromium** — качается Playwright'ом автоматически на этапе install.

## Установка

Из корня background-agent:

```bash
python recommended_mcp/install.py
```

Что произойдёт:

1. Если `npx` уже в PATH — шаг пропускается.
2. Иначе ставится Node.js штатным пакетным менеджером платформы:
   - **macOS**  — `brew install node` (требует Homebrew).
   - **Windows** — `winget install OpenJS.NodeJS.LTS`.
   - **Linux**  — первый найденный из `apt-get`, `dnf`, `pacman`, `apk`
     (с `sudo`, запросит пароль).

   Если ни один способ не сработал — install.py печатает ссылку
   https://nodejs.org/ и завершает работу с ошибкой. Поставь Node.js руками
   и повторно запусти `install.py`.
3. `npx -y @playwright/mcp@latest --help` — прогрев npm-кэша.
4. `npx -y playwright@latest install chromium` — пред-скачивание браузера.

## Запуск

Отдельного шага `run.py` нет — агент сам спавнит процесс `npx` через stdio
при первом tool_call и держит его живым весь цикл работы (флаг
`persistent_session: true`). По завершении агент убивает процесс и все его
дочерние (Chromium) через process-group сигналы.

## Подключение к агенту

Скопируй содержимое `agent_config.snippet.yml` в секцию `mcp-servers:` своего
`config.yml` агента.

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
