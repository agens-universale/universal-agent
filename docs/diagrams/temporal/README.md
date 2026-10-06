# Изображения архитектуры агента и Temporal

Версия от 06.10.2026: раздел 0 показывает штатного агента без Temporal,
схемы 01–07 — фактическую нативную интеграцию.

Исходники — девять блоков Mermaid в [карте архитектуры](../../temporal-architecture.md):
два в разделе 0 об агенте без Temporal, затем по одному в разделах 1–7.
PNG предназначены для вставки в Outline, SVG — для
масштабирования. В SVG не встраиваются шрифты; используется Arial / sans-serif.

Рендер проверен с `@mermaid-js/mermaid-cli@12.0.0`. Пример из корня репозитория:

```bash
mkdir -p /tmp/ua-temporal-diagrams
npx --yes @mermaid-js/mermaid-cli@12.0.0 -i docs/temporal-architecture.md -o /tmp/ua-temporal-diagrams/diagram.svg -c docs/diagrams/temporal/theme.json --no-font-embed -j 2
npx --yes @mermaid-js/mermaid-cli@12.0.0 -i docs/temporal-architecture.md -o /tmp/ua-temporal-diagrams/diagram.png -c docs/diagrams/temporal/theme.json --scale 2 -j 2
```

CLI использует headless Chromium/Puppeteer. Системный Chrome можно указать через
`--puppeteerConfigFile` с `executablePath`. Имена после рендера:

| Результат CLI | Файл в этом каталоге | Содержание |
|---|---|---|
| `diagram-1` | `00-agent-components` | Компоненты агента без Temporal |
| `diagram-2` | `00-agent-turn` | Один ход агента без Temporal |
| `diagram-3` | `01-architecture` | Процессы, очереди и хранилища |
| `diagram-4` | `02-dispatch` | Порты операций: local и Temporal |
| `diagram-5` | `03-turn` | Один ход в Temporal |
| `diagram-6` | `04-guards` | Guards, подтверждение и эффект |
| `diagram-7` | `05-mcp` | MCP и фоновые процессы |
| `diagram-8` | `06-child` | Субагент в child workflow |
| `diagram-9` | `07-persistence` | Тред, история Temporal и память воркера |

При обновлении нужно заменить обе версии каждой изменившейся схемы и визуально
проверить переносы текста и стрелки. Существующие имена `01`–`07` сохранены,
чтобы ссылки на схемы Temporal продолжали работать.
