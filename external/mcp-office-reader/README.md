# mcp-office-reader

MCP-сервер для чтения офисных документов и возврата содержимого в формате markdown. Поддерживает транспорты streamable-http и stdio.

## Поддерживаемые форматы

| Расширение | Движок |
|------------|--------|
| `.pdf` | markitdown (pdfminer) |
| `.docx` | markitdown (mammoth) |
| `.xlsx` | markitdown (openpyxl) |
| `.xls` | markitdown (xlrd) |
| `.pptx` | markitdown (python-pptx) |
| `.csv` | markitdown |
| `.html` | markitdown (beautifulsoup4) |
| `.msg` | markitdown |

Обработка построена на [markitdown](https://github.com/microsoft/markitdown) (Microsoft) — библиотеке, которая конвертирует документы в markdown через специализированные Python-парсеры (pdfminer, mammoth, openpyxl и др.). Все форматы обрабатываются в памяти; внешних бинарных зависимостей нет.

## Установка

```bash
pip install -e .
# или
uv sync
```

## Локальный запуск

streamable-http (транспорт по умолчанию):

```bash
python -m mcp_office_reader --port 8080
```

stdio:

```bash
python -m mcp_office_reader --transport stdio
```

По умолчанию `read_file` может читать только из текущей рабочей директории. Используйте `--allowed-dirs` для указания разрешённых каталогов или `--allow-any` чтобы отключить whitelist.

## Конфигурация клиента

Streamable-http (отдельный локальный процесс — запускается командой `python -m mcp_office_reader --port 8080`):

```yaml
mcp-servers:
  office-reader:
    transport: stream_http
    url: http://localhost:8080/mcp
    description: Read office documents (pdf, docx, xls, xlsx, pptx, csv, html, msg) as markdown
    server-name: office-reader
```

stdio (встроенный, агент сам запускает MCP-сервер):

```yaml
mcp-servers:
  office-reader:
    transport: stdio
    command: python
    args: ["-m", "mcp_office_reader", "--transport", "stdio", "--allow-any"]
    description: Read office documents (pdf, docx, xls, xlsx, pptx, csv, html, msg) as markdown
    server-name: office-reader
```

## Инструменты (tools)

### `read_file`

Читает офисный документ из файловой системы сервера и возвращает markdown.

| Параметр | Тип | Описание |
|----------|-----|----------|
| `path` | `string` | Абсолютный путь к файлу на сервере |

### `read_document`
 
Читает офисный документ, переданный в виде base64-строки, и возвращает markdown.

| Параметр | Тип | Описание |
|----------|-----|----------|
| `filename` | `string` | Имя файла с расширением (например `report.docx`) |
| `content_base64` | `string` | Содержимое файла в base64 |

## Создание документов (запись)

Эти инструменты создают новые `.docx` и `.xlsx` файлы из структурированных входных данных. Редактирование существующих документов, изображения и диаграммы не поддерживаются. Формулы в xlsx — поддерживаются (см. тип колонки `formula` ниже).

Поддерживаемый markdown для `.docx`: заголовки, абзацы, инлайн **bold** / *italic* / `code` / ссылки, маркированные и нумерованные списки (вкладываемые), GFM-таблицы, цитаты (blockquote), fenced code blocks, горизонтальные разделители. Изображения в MVP не поддерживаются.

### `create_docx_file`

Генерирует `.docx` из markdown и записывает его на файловую систему сервера. Пишет только в пути внутри `--output-allowed-dirs` (по умолчанию совпадают с `--allowed-dirs`). Возвращает `{path, bytes_written}`.

| Параметр | Тип | Описание |
|----------|-----|----------|
| `path` | `string` | Абсолютный путь, куда записать файл |
| `markdown` | `string` | Содержимое документа в markdown |
| `overwrite` | `bool` | Перезаписывать ли существующий файл (по умолчанию `false`) |

### `create_docx`

Генерирует `.docx` из markdown и возвращает содержимое в base64. Используется, когда у вызывающей стороны нет доступа к файловой системе сервера. Размер ограничен `--max-output-base64-size`. Возвращает `{filename, content_base64, raw_bytes}`.

| Параметр | Тип | Описание |
|----------|-----|----------|
| `filename` | `string` | Имя файла с расширением `.docx` (возвращается в ответе, на диск не пишется) |
| `markdown` | `string` | Содержимое документа в markdown |

### `create_xlsx_file`

Генерирует `.xlsx` из структурированной спецификации `workbook` и записывает на диск. Пишет только в пути внутри `--output-allowed-dirs`. Возвращает `{path, bytes_written}`.

| Параметр | Тип | Описание |
|----------|-----|----------|
| `path` | `string` | Абсолютный путь, куда записать файл |
| `workbook` | `object` | Спецификация книги (см. формат ниже) |
| `overwrite` | `bool` | Перезаписывать ли существующий файл (по умолчанию `false`) |

### `create_xlsx`

Генерирует `.xlsx` из `workbook` и возвращает содержимое в base64. Размер ограничен `--max-output-base64-size`. Возвращает `{filename, content_base64, raw_bytes}`.

| Параметр | Тип | Описание |
|----------|-----|----------|
| `filename` | `string` | Имя файла с расширением `.xlsx` (возвращается в ответе, на диск не пишется) |
| `workbook` | `object` | Спецификация книги (см. формат ниже) |

### Формат `workbook`

Типы колонок: `text`, `number`, `integer`, `date`, `datetime`, `bool`, `currency`, `percent`, `formula`. Даты передаются как ISO-8601 строки. Колонка `formula` принимает строки, начинающиеся с `=` (включая cross-sheet ссылки `=Sheet1!A1`); вычисление значений выполняется самим Excel/LibreOffice при открытии файла.

Компактный пример:

```json
{
  "sheets": [
    {
      "name": "Sales",
      "columns": [
        {"name": "Product", "type": "text", "width": 20},
        {"name": "Price",   "type": "currency", "format": "#,##0.00 ₽", "width": 14}
      ],
      "rows": [
        ["Apple", 99.5],
        ["Bread", 45.0]
      ],
      "merges": ["A1:B1"],
      "cell_styles": [
        {"range": "B2:B3", "bg": "FFF2CC", "bold": true}
      ]
    }
  ]
}
```

Диапазоны в `merges` и `cell_styles` используют A1-нотацию и привязаны к итоговой сетке: строка 1 — автоматически сгенерированный жирный заголовок, данные начинаются со строки 2.

## Аналитика (xlsx/csv через pandas)

Четыре read-side инструмента поверх `pandas` для агентов, которые не имеют собственного Python-runtime. Принимают `.xlsx` / `.xls` / `.csv` на вход, не модифицируют исходный файл. Результат либо возвращается inline (с лимитом строк), либо записывается в новый xlsx через `output: {path: ...}`.

### Когда что использовать

| Сценарий | Tool |
|---|---|
| «Что в этом файле?» — узнать колонки и типы перед запросом | `inspect_xlsx` |
| «Покажи строки по условию» (фильтр + select + sort + top-N) | `query_xlsx` |
| «Сколько X в каждой группе» (sum/count/mean ...) | `aggregate_xlsx` |
| «Матрица status × team» (cross-tab) | `pivot_xlsx` |

### `inspect_xlsx`

Возвращает schema + sample листа: имена и типы колонок, n_rows, head из 3 строк. Не пишет, размер ответа фиксирован (~10-50KB), не зависит от объёма файла.

| Параметр | Тип | Описание |
|---|---|---|
| `path` | `string` | Абсолютный путь |
| `sheet` | `string \| null` | Имя листа (null = первый) |

### `query_xlsx`

| Параметр | Тип | Описание |
|---|---|---|
| `path` | `string` | Абсолютный путь |
| `sheet` | `string \| null` | Имя листа |
| `filter` | `[FilterClause]` | См. ниже; AND-связка |
| `select` | `[string]` | Подмножество колонок (default = все) |
| `sort` | `[{col, order}]` | `order: "asc" \| "desc"` |
| `limit` | `int` | Макс. строк |
| `output` | `OutputSpec` | См. ниже |

### `aggregate_xlsx`

| Параметр | Тип | Описание |
|---|---|---|
| `path`, `sheet`, `filter`, `sort`, `limit`, `output` | — | Как у `query_xlsx` |
| `groupby` | `[string]` | Колонки для группировки. Пустой массив = totals по всему датасету |
| `agg` | `{col: AggFunc}` или `{col: [AggFunc, ...]}` | См. ниже |
| `having` | `[FilterClause]` | Фильтр по результатам агрегации |

Имена результат-колонок: `<col>_<func>` (`hours_sum`, `id_count`).

### `pivot_xlsx`

| Параметр | Тип | Описание |
|---|---|---|
| `path`, `sheet`, `filter`, `sort`, `limit`, `output` | — | Как у `query_xlsx` |
| `index` | `string \| [string]` | Колонки для строк pivot |
| `columns` | `string \| [string]` | Колонки для столбцов pivot |
| `values` | `string` | Колонка для агрегирования |
| `aggfunc` | `AggFunc` | По умолчанию `sum` |
| `fill_value` | `any` | Значение для пустых ячеек pivot |

Имена результат-колонок: `<values>_<column_value>` (например `id_open`, `id_closed`).

### `FilterClause`

Простое выражение:
```json
{"col": "status", "op": "==", "value": "open"}
```

Top-level массив = AND. OR — через явный объект:
```json
[{"or": [
  {"col": "status", "op": "==", "value": "open"},
  {"col": "status", "op": "==", "value": "review"}
]}]
```

Операторы (whitelist): `==, !=, >, >=, <, <=, in, not_in, contains, startswith, endswith, is_null, not_null, between`.

- `in`/`not_in`: `value` — массив
- `between`: `value` — `[lo, hi]` (inclusive)
- `is_null`/`not_null`: `value` опускается
- `contains`/`startswith`/`endswith`: только для текстовых колонок

### `AggFunc`

`sum`, `mean`, `count`, `min`, `max`, `median`, `std`, `first`, `last`, `nunique`. Численные функции (`sum`, `mean`, `median`, `std`) на text-колонках падают с `incompatible_dtype`.

### `OutputSpec`

| Значение | Поведение |
|---|---|
| `null` или `"data"` (default) | Вернуть rows в JSON-ответе. Если строк > `--max-query-rows` → `output_too_large` |
| `{"path": "/abs/...", "sheet": "Result", "overwrite": false}` | Записать xlsx через тот же пайплайн, что `create_xlsx_file` |

### Примеры

«Сводка по статусам»:
```json
{
  "tool": "aggregate_xlsx",
  "path": "/data/sprint.xlsx",
  "groupby": ["status"],
  "agg": {"hours": "sum", "id": "count"}
}
```

«Топ-10 разработчиков по часам в открытых задачах»:
```json
{
  "tool": "aggregate_xlsx",
  "path": "/data/sprint.xlsx",
  "filter": [{"col": "status", "op": "==", "value": "open"}],
  "groupby": ["assignee"],
  "agg": {"hours": "sum"},
  "sort": [{"col": "hours_sum", "order": "desc"}],
  "limit": 10
}
```

«Матрица status × team в виде xlsx»:
```json
{
  "tool": "pivot_xlsx",
  "path": "/data/sprint.xlsx",
  "index": "team",
  "columns": "status",
  "values": "id",
  "aggfunc": "count",
  "fill_value": 0,
  "output": {"path": "/data/cross.xlsx", "sheet": "Matrix"}
}
```

### ⚠️ Важно: формулы в исходном файле

Если входной xlsx содержит формулы (`=SUM(...)`, `=Sheet1!A1`), pandas читает их **закешированные** значения. Если файл был открыт хотя бы раз в Excel/LibreOffice и сохранён — значения есть. Если файл сгенерирован программно (например, через `create_xlsx_file`) и не открывался — значения отсутствуют, в результате будут `None`. **Для чисел из формул убедись, что файл был один раз открыт в Excel/LibreOffice.** Recalc-tool намеренно не входит в текущий scope.

## Флаги CLI

| Флаг | По умолчанию | Описание |
|------|--------------|----------|
| `--transport` | `http` | Транспорт: `http` (streamable-http) или `stdio` |
| `--host` | `127.0.0.1` | Адрес привязки (только streamable-http) |
| `--port` | `8080` | Порт (только streamable-http) |
| `--allowed-dirs` | cwd | Каталоги, из которых `read_file` может читать |
| `--allow-any` | выкл | Отключить whitelist путей |
| `--max-file-size` | `50MB` | Макс. размер входного файла (поддерживает суффиксы MB/KB/B) |
| `--max-output-chars` | `1000000` | Макс. длина вывода до обрезки |
| `--timeout-seconds` | `60` | Таймаут обработки документа |
| `--max-output-base64-size` | `20MB` | Макс. размер base64-ответа для `create_docx` / `create_xlsx` |
| `--output-allowed-dirs` | =`--allowed-dirs` | Каталоги, куда `create_*_file` могут писать |
| `--max-query-rows` | `10000` | Макс. строк inline-ответа для аналитических tools (для большего — `output: {path}`) |
| `--log-level` | `INFO` | Уровень логирования |

## Ошибки

Сервер возвращает человекочитаемые сообщения об ошибках следующих категорий:

| Категория | Когда возникает |
|-----------|-----------------|
| `file_not_found` | Файл не найден по указанному пути |
| `path_not_allowed` | Путь за пределами `--allowed-dirs` |
| `unsupported_format` | Расширение файла не в списке поддерживаемых |
| `file_too_large` | Файл превышает `--max-file-size` |
| `parse_failed` | Документ не удалось разобрать (повреждён / защищён паролем) |
| `timeout` | Обработка превысила `--timeout-seconds` |
| `invalid_base64` | `content_base64` не является валидным base64 |
| `permission_denied` | Нет прав на чтение файла (на уровне ОС) |
| `invalid_input` | Некорректный `workbook` или markdown для `create_*` (ошибка валидации схемы) |
| `file_exists` | Целевой путь `create_*_file` уже существует, а `overwrite=false` |
| `output_too_large` | Сгенерированный `.docx` / `.xlsx` превышает `--max-output-base64-size`, либо аналитический результат превышает `--max-query-rows` |
| `write_failed` | Не удалось записать файл на диск (I/O-ошибка, нет прав и т.п.) |
| `column_not_found` | В spec аналитического tool указана колонка, которой нет в DataFrame |
| `unsupported_aggregation` | Указанная agg-функция вне whitelist (`sum/mean/count/min/max/median/std/first/last/nunique`) |
| `incompatible_dtype` | Численная агрегация (например `sum`) на text-колонке |

## Разработка

```bash
pip install -e ".[dev]"
# или
uv sync --extra dev

pytest -v
ruff check .
```