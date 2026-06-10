[Назад к README](../readme.md)

# Тесты

Один раз в корне репо: `uv sync`. Этого достаточно для pytest.

`uv tool install -e .` и `gc-mcp-install` — для **запуска агента**, к тестам не относятся. MCP в `~/.local/share/gc/...` и dev-окружение для тестов — разные venv, так и задумано.

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
