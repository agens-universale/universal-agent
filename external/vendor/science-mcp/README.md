# science-mcp

MCP-сервер для унифицированного поиска научной литературы. Оборачивает 10
публичных API (OpenAlex, CrossRef, PubMed/PMC, arXiv, Semantic Scholar, DBLP,
CORE, OpenCitations, bioRxiv/medRxiv) в 7 нормализованных tools.

## Установка

Из корня родительского репозитория (`background-agent/`):

```bash
python recommended_mcp/install.py
```

Или напрямую:

```bash
pip install /Users/gkave/SberAI/background-agent/external/science-mcp
```

После установки entry-point `science-mcp` доступен в `$PATH` виртуального
окружения.

## Запуск daemon

science-mcp работает как долгоживущий процесс — это нужно, чтобы in-memory
троттлинг переживал между вызовами агента. По умолчанию: HTTP на
`127.0.0.1:8765`, endpoint `/mcp`.

```bash
# Foreground (транспорт по умолчанию — http)
science-mcp --transport http

# Кастомный bind
science-mcp --transport http --host 0.0.0.0 --port 9000

# Background
nohup science-mcp --transport http > /tmp/science-mcp.log 2>&1 &
```

Stdio оставлен как legacy fallback для разовой отладки (процесс спавнится на
каждый вызов агента, без shared-throttler):

```bash
science-mcp --transport stdio
```

## Конфигурация

Основной источник — YAML-файл. Env переменные остаются как fallback (удобно в
systemd/docker, где править файлы неудобно). Порядок lookup-а для каждого
поля: **YAML > env > встроенный default**.

### YAML (рекомендуется)

Скопируй tracked-шаблон и впиши свои значения:

```bash
cp science-mcp.example.yaml science-mcp.yaml
$EDITOR science-mcp.yaml
science-mcp --transport http --config ./science-mcp.yaml
```

Если `--config` не передан, daemon сам ищет `./science-mcp.yaml`, потом
`~/.config/science-mcp.yaml`. Реальный `science-mcp.yaml` — в `.gitignore`,
коммитим только `.example`-шаблон.

Доступные ключи (полный аннотированный пример — в `science-mcp.example.yaml`):

| Ключ | По умолчанию | Назначение |
|---|---|---|
| `mailto` | `team@company.example` | OpenAlex polite-pool + CrossRef UA |
| `host` | `127.0.0.1` | Bind-адрес daemon |
| `port` | `8765` | Порт daemon |
| `ncbi_api_key` | `null` | Поднимает лимит NCBI с 3/s до 10/s |
| `semantic_scholar_api_key` | `null` | Без ключа S2 моментально отдаёт 429 |
| `core_api_key` | `null` | Стабильный доступ к CORE |
| `cache_dir` | `~/.cache/science-mcp` | Папка disk JSON cache |
| `cache_enabled` | `true` | `false` отключает disk cache |
| `http_timeout_sec` | `15` | Per-request timeout, секунды |
| `queue_timeout_sec` | `120` | Макс. ожидание в очереди троттлера, секунды |

Неизвестные ключи в YAML — падают с понятным `ValueError` (typo guard).

### Env переменные (fallback)

Полезны для docker/systemd. Каждому YAML-ключу соответствует env с префиксом
`SCIENCE_MCP_` (или каноничные `NCBI_API_KEY` / `SEMANTIC_SCHOLAR_API_KEY` /
`CORE_API_KEY` для ключей API):

| Переменная | Соответствует |
|---|---|
| `SCIENCE_MCP_MAILTO` | `mailto` |
| `SCIENCE_MCP_HOST` | `host` |
| `SCIENCE_MCP_PORT` | `port` |
| `NCBI_API_KEY` | `ncbi_api_key` |
| `SEMANTIC_SCHOLAR_API_KEY` | `semantic_scholar_api_key` |
| `CORE_API_KEY` | `core_api_key` |
| `SCIENCE_MCP_CACHE_DIR` | `cache_dir` |
| `SCIENCE_MCP_CACHE` | `cache_enabled` (`0` → false) |
| `SCIENCE_MCP_HTTP_TIMEOUT` | `http_timeout_sec` |
| `SCIENCE_MCP_QUEUE_TIMEOUT` | `queue_timeout_sec` |

## Подключение к агенту

Скопируй блок из `recommended_mcp/science/agent_config.snippet.yml` (в
родительском репозитории) в секцию `mcp-servers:` своего agent `config.yml`.
Дефолтное подключение: `transport: stream_http`,
`url: http://127.0.0.1:8765/mcp`.

## Архитектура

Слоистая: `utilities → adapters → tools → server`. Никаких reverse-imports.

- `utils` — `cache.py`, `throttle.py`, `http_client.py`, `errors.py`,
  `models.py`, `normalize.py`, `config.py`. Технический слой, бизнес-логики
  нет.
- `adapters/` — по одному модулю на провайдера (10 шт.). Знают про конкретный
  upstream API + нормализуют ответ.
- `tools/` — 7 MCP-tools (`literature_search`, `work_lookup`, `ncbi_fetch`,
  `citations_by_doi`, `preprint_search`, `open_access_search`,
  `explain_sources`). Оркестрация, выбор источников, dedup.
- `server.py` — регистрация tools в FastMCP, транспорт.

## Операционные ограничения (из live-probes 2026-05-18)

- Semantic Scholar без API-key моментально отдаёт 429 — адаптер raise-ит `unauthorized`.
- CrossRef `filter=member:246` отдаёт не только препринты, но и журналы CSHL — `cshl_preprints` доп. фильтрует по DOI-префиксу `10.1101/YYYY.MM.DD.NNNNNN`.
- PMC `efetch` возвращает full-text XML до 236KB — `ncbi_fetch` по умолчанию работает в `summary`; режим `fetch` capped на 50KB.
- OpenCitations `citations` возвращает ~5MB для популярных DOI — disk cache TTL 24h обязателен.
- arXiv держит лимит 1 req / 3s; `preprint_search` изолирован от `literature_search`.
- CORE endpoint требует trailing slash: `/v3/search/works/?q=...`.
- OpenCitations endpoint переехал: `https://api.opencitations.net/index/v1/...`.
- `stream_stdio` транспорт в `gc-cli-pub` whitelist-ит унаследованный env до `HOME/LOGNAME/PATH/SHELL/TERM/USER` — нужно явно перечислять все `SCIENCE_MCP_*` и provider-ключи в `env:` агентского config. HTTP-транспорт это обходит — daemon наследует свой собственный shell env.
