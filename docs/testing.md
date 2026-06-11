[Назад к README](../readme.md)

# Тесты

Один раз в корне репо: `uv sync`. Подтягивает dev/test-зависимости (`textual`, `rich` — в основных deps; `office-mcp` из `external/office-mcp` — в test-группе).

`uv tool install -e .` и `gc-mcp-install` — для **запуска агента** (`gc-cli` / `gc-ua`), не для pytest. Тесты гоняйте через `uv run pytest`, а не из venv `uv tool`.

```bash
# gc-cli
uv run pytest

# office-mcp (свой venv через --directory)
uv run --directory external/office-mcp pytest

# science-mcp
uv run --directory external/science-mcp pytest

# computer-mcp (на Linux)
uv run --directory external/computer-mcp pytest -m "not windows"
```

Папка или файл: `uv run pytest tests/security/ -q`
