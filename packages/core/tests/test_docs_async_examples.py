"""Примеры в docs/async.md обязаны быть валидным Python.

Документация к SDK — тоже интерфейс: пример, который не компилируется,
обнаруживается пользователем, а не нами.
"""

import ast
import re

from gc_testkit.paths import REPO

DOC = REPO / "docs" / "async.md"


def test_doc_exists():
    assert DOC.is_file(), "docs/async.md не найден"


def test_all_python_snippets_parse():
    blocks = re.findall(r"```python\n(.*?)```", DOC.read_text(encoding="utf-8"), re.S)
    assert blocks, "в docs/async.md нет ни одного python-примера"
    for i, block in enumerate(blocks, 1):
        try:
            ast.parse(block)
        except SyntaxError as exc:
            raise AssertionError(f"пример №{i} не парсится: {exc}") from exc


def test_doc_names_the_public_entries():
    text = DOC.read_text(encoding="utf-8")
    for name in ("AsyncAgent", "AgentExecutor.arun", "acreate_run_context"):
        assert name in text, f"в документации не назван {name}"


def test_entry_table_matches_reality():
    """Таблица имён не должна врать.

    Разъехавшаяся таблица хуже её отсутствия: читатель поверит и позовёт имя,
    которого нет. Проверяем оба направления — асинхронное имя существует, а
    синхронного двойника рядом с ним НЕТ: именно его возвращение и было бы
    откатом ветки.
    """
    import importlib

    # `create_run_context` — ЕДИНСТВЕННОЕ уцелевшее синхронное имя, и уцелело
    # не по недосмотру: это не шим поверх корутины, а вторая нативная сборка
    # (`abuild_tools_data`), которой пользуется TUI. Разделить её нельзя,
    # пока MCP-сессии привязаны к loop'у, на котором заведены: TUI строит
    # контекст и гоняет ход разными `asyncio.run`.
    entries = [
        ("gc_core.processing_responses", "arun_agent"),
        ("gc_core.tools", "aapply_tool"),
        ("gc_core.utils.connect_backend", "arequest_backend_chat_completions"),
        ("gc_core.request_backend_policy", "arequest_chat_completions_with_policy"),
        ("gc_core.utils.llm_chat_client", "arequest_chat_completion_text"),
        ("gc_core.summarize.llm", "arequest_summary_text"),
    ]
    text = DOC.read_text(encoding="utf-8")
    assert "acreate_run_context" in text, "acreate_run_context не назван в таблице"
    for module_name, async_name in entries:
        module = importlib.import_module(module_name)
        assert hasattr(module, async_name), f"{module_name}.{async_name} исчез"
        assert not hasattr(module, async_name[1:]), f"вернулся синхронный {module_name}.{async_name[1:]}"
        assert async_name in text, f"{async_name} не назван в таблице"

    from gc_core.agent_executor import AgentExecutor

    assert hasattr(AgentExecutor, "arun")
    assert not hasattr(AgentExecutor, "run"), "у AgentExecutor вернулся синхронный run"
