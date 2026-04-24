# office-reader — mcp-office-reader

Интеграция `mcp-office-reader` в background-agent. Читает офисные документы
(pdf, docx, xls, xlsx, pptx, csv, html, msg, legacy .doc) и возвращает
содержимое в формате markdown.

Основной путь — **Docker**: образ включает LibreOffice для `.doc`, это
проще, чем ставить LibreOffice в систему.

## Требования

- **Docker** + **docker compose v2** (`docker compose version`).
- Свободный порт `8080` на `127.0.0.1`.

## Установка

Из корня background-agent:

```bash
python recommended_mcp/install.py
```

Что произойдёт: `docker compose build` соберёт образ из `Dockerfile`.
Первый билд занимает 5-15 минут (ставится LibreOffice). Повторные —
секунды за счёт кэша слоёв.

## Запуск

```bash
python recommended_mcp/run.py
```

Что произойдёт: `docker compose up -d` — контейнер поднимется в фоне,
слушает `127.0.0.1:8080`.

Проверка:

```bash
curl -s http://127.0.0.1:8080/mcp -X POST -H "Content-Type: application/json" \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

Остановка:

```bash
docker compose -f recommended_mcp/office-reader/docker-compose.yml down
```

## Подключение к агенту

Скопируй содержимое `agent_config.snippet.yml` в секцию `mcp-servers:` своего
`config.yml` агента.

## Видимость файлов — важное ограничение

Из Docker-контейнера MCP видит **только** содержимое `/workspace/`, в который
смонтирован **корень проекта background-agent** (`../..` из этой папки),
read-only.

Это значит:

- Файлы **внутри** проекта агента (`./reports/`, `./threads/...`) — читаются
  нормально.
- Файлы **вне** проекта (`D:/docs/*.pdf`, `~/Downloads/*.docx`) — MCP **не
  видит**. Варианты:
  - Добавить bind-mount в `docker-compose.yml`:

    ```yaml
    volumes:
      - ../..:/workspace:ro
      - D:/docs:/workspace/docs:ro   # пример своей папки
    ```

  - Или использовать stdio-альтернативу (см. ниже) — без контейнера, без
    ограничений видимости.

## Альтернативный путь: stdio вместо Docker

`mcp-office-reader` умеет работать и через stdio — без контейнера, как
Python-пакет. В этом режиме MCP видит файлы напрямую через файловую
систему хоста. Требует отдельной установки LibreOffice для `.doc`.

Секция config.yml агента при stdio:

```yaml
office-reader:
  transport: stdio
  command: python
  args: ["-m", "mcp_office_reader", "--transport", "stdio", "--allow-any"]
```

Этот путь в `recommended_mcp/` не автоматизирован — `install.py` / `run.py`
ориентированы на docker-путь как основной.

## Обновление версии

1. В репо `mcp-office-reader`: `uv build` → свежий `.whl`.
2. Удали старый wheel из `dist/`, положи новый.
3. `python recommended_mcp/install.py` — пересоберёт образ с новой версией.
4. `docker compose down` + `python recommended_mcp/run.py` — перезапуск.
5. Коммит: `recommended_mcp/office-reader: bump mcp-office-reader to X.Y.Z`.
