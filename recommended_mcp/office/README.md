# office — office-mcp

Интеграция `office-mcp` в background-agent. Читает офисные документы
(pdf, docx, xls, xlsx, pptx, csv, html, msg) и возвращает содержимое в формате
markdown. Все форматы парсятся в Python через `markitdown` — внешних бинарных
зависимостей (LibreOffice и т.п.) нет.

Исходники — в `external/office-mcp/`. Пакет ставится `install.py` через
`pip install ./external/office-mcp` (PEP 517 build из исходников).

## Требования

- **Python 3.10+.**
- **Активный venv агента** перед запуском `install.py`.

Кросс-платформенно: работает на Windows / Linux / macOS. Пакет — pure Python,
зависимости (`markitdown` и его парсеры) тоже без нативного кода.

## Установка

Из корня background-agent:

```bash
python recommended_mcp/install.py
```

Для разработки:

```bash
python recommended_mcp/install.py --editable
```

После установки в PATH активного venv появится команда `office-mcp`.

## Подключение к агенту

Скопируй содержимое `agent_config.snippet.yml` в секцию `mcp-servers:` твоего
`config.yml` агента.

## Запуск

Отдельного запуска не требует — агент сам спавнит `office-mcp` по stdio
при первом вызове `read_file` / `read_document`. `run.py` этот MCP пропускает.

## Тулы

| Тул | Назначение |
|---|---|
| `read_file` | Читает файл с файловой системы хоста по абсолютному пути → markdown. |
| `read_document` | Читает файл, переданный как base64 (`filename` + `content_base64`) → markdown. |

## Видимость файлов

В snippet передаётся флаг `--allow-any` — отключает whitelist путей, MCP видит
любые файлы, к которым у процесса агента есть доступ на чтение. Если нужно
ограничить — замени `--allow-any` на `--allowed-dirs DIR1 DIR2 ...` в `args:`.

## Флаги CLI

Можно дописать в `args:` сниппета — все флаги из `office-mcp --help` доступны.
Полезное:

| Флаг | По умолчанию | Описание |
|---|---|---|
| `--allowed-dirs` | cwd | Каталоги, из которых `read_file` может читать. |
| `--allow-any` | выкл | Отключить whitelist путей. |
| `--max-file-size` | `50MB` | Макс. размер входного файла. |
| `--max-output-chars` | `1000000` | Макс. длина markdown-ответа до обрезки. |
| `--timeout-seconds` | `60` | Таймаут обработки одного документа. |
| `--log-level` | `INFO` | Уровень логирования. |

## Обновление

Правки идут в `external/office-mcp/`. После правок:

- В обычном режиме — пере-запустить `python recommended_mcp/install.py`
  (с бампом `version` в `external/office-mcp/pyproject.toml`, иначе pip не
  перетрёт уже стоящую копию).
- В editable-режиме — изменения подхватываются на следующем запуске MCP без
  переустановки.
