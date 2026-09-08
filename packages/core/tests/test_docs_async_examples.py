"""Примеры в docs/async.md обязаны быть валидным Python.

Документация к SDK — тоже интерфейс: пример, который не компилируется,
обнаруживается пользователем, а не нами.
"""
import ast
from gc_testkit import as_coro
import re
from pathlib import Path

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


def test_doc_names_both_apis():
    text = DOC.read_text(encoding="utf-8")
    for name in ("AsyncAgent", "AgentExecutor.arun", "SyncApiInsideEventLoopError"):
        assert name in text, f"в документации не назван {name}"


def test_mapping_table_matches_reality():
    """Таблица соответствия sync↔async не должна врать.

    Разъехавшаяся таблица хуже её отсутствия: читатель поверит и позовёт имя,
    которого нет.
    """
    import importlib

    pairs = [
        ("gc_core.run_context", "create_run_context", "acreate_run_context"),
        ("gc_core.processing_responses", "run_agent", "arun_agent"),
        # `apply_tool` больше нет: у тулов форма одна, и синхронный
        # диспетчер стал мёртвым по построению — звать было некого.
        ("gc_core.utils.connect_backend",
         "request_backend_chat_completions", "arequest_backend_chat_completions"),
        ("gc_core.request_backend_policy",
         "arequest_chat_completions_with_policy", "arequest_chat_completions_with_policy"),
        ("gc_core.utils.llm_chat_client",
         "request_chat_completion_text", "arequest_chat_completion_text"),
        ("gc_core.summarize.llm", "request_summary_text", "arequest_summary_text"),
    ]
    text = DOC.read_text(encoding="utf-8")
    for module_name, sync_name, async_name in pairs:
        module = importlib.import_module(module_name)
        assert hasattr(module, sync_name), f"{module_name}.{sync_name} исчез"
        assert hasattr(module, async_name), f"{module_name}.{async_name} исчез"
        assert async_name in text, f"{async_name} не назван в таблице"

    from gc_core.agent_executor import AgentExecutor

    assert hasattr(AgentExecutor, "run") and hasattr(AgentExecutor, "arun")
