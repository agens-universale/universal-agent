"""Tests for all_builtin_tool_names in registry."""

import pytest
from gc_testkit.plugin import ensure_real_tools_package


@pytest.fixture(autouse=True)
def _ensure_real_tools_module():
    """Keep the real module identity shared by tool calls and teardown."""
    ensure_real_tools_package()


def test_returns_prefilter_names_ignoring_stop_tools():
    """stop_tools НЕ влияет на состав имён (проверка против полного набора)."""
    from gc_core.tools.registry import all_builtin_tool_names
    from gc_testkit.settings import make_settings

    # stop_tools НЕ влияет на состав имён (проверка против полного набора)
    names_no_stop = all_builtin_tool_names(make_settings())
    names_with_stop = all_builtin_tool_names(
        make_settings({"agent.stop_tools": ["fs_write_file"]})
    )
    assert names_no_stop == names_with_stop
    assert "fs_write_file" in names_no_stop
    assert "turn_ask_human" in names_no_stop


def test_every_artifact_tool_name_exists_in_the_registry():
    """Набор артефактного пола описывает реальные тулы — встроенные и тулы
    скиллов: переименование тула, не доехавшее до набора, ловится здесь, а не
    молчаливо выключает пол."""
    from gc_core.constants.tool_names import ARTIFACT_TOOL_NAMES
    from gc_core.tools.registry import all_builtin_tool_names
    from gc_core.tools.skills.skills_tools import get_skills_tools_data
    from gc_testkit.settings import make_settings

    names = all_builtin_tool_names(make_settings()) | set(
        get_skills_tools_data(None)
    )
    assert names >= ARTIFACT_TOOL_NAMES
    assert {
        "fs_write_file",
        "fs_append_file",
        "fs_apply_file_edits",
        "fs_search_and_replace",
        "fs_save_base64_file",
        "exec_command",
        "exec_python",
        "download_file",
        "skills_run_script",
    } == ARTIFACT_TOOL_NAMES


def test_file_write_and_exec_sets_have_the_exact_composition():
    from gc_core.constants.tool_names import (
        EXEC_TOOL_NAMES,
        FILE_WRITE_TOOL_NAMES,
    )

    assert {
        "fs_write_file",
        "fs_append_file",
        "fs_apply_file_edits",
        "fs_search_and_replace",
        "fs_save_base64_file",
        "download_file",
    } == FILE_WRITE_TOOL_NAMES
    assert {
        "exec_command",
        "exec_python",
        "skills_run_script",
    } == EXEC_TOOL_NAMES
    assert isinstance(FILE_WRITE_TOOL_NAMES, frozenset) and isinstance(
        EXEC_TOOL_NAMES, frozenset
    )


def test_artifact_set_is_the_union_of_file_write_and_exec():
    from gc_core.constants.tool_names import (
        ARTIFACT_TOOL_NAMES,
        EXEC_TOOL_NAMES,
        FILE_WRITE_TOOL_NAMES,
    )

    assert ARTIFACT_TOOL_NAMES == FILE_WRITE_TOOL_NAMES | EXEC_TOOL_NAMES
    assert set() == FILE_WRITE_TOOL_NAMES & EXEC_TOOL_NAMES
    assert isinstance(ARTIFACT_TOOL_NAMES, frozenset)


def test_important_set_has_the_exact_composition_and_excludes_exec():
    """Запуск команд в список важных не входит (спека two_level §1 п.6, вариант A):
    слабая модель сама гоняет тесты."""
    from gc_core.constants.tool_names import (
        EXEC_TOOL_NAMES,
        FILE_WRITE_TOOL_NAMES,
        IMPORTANT_TOOL_NAMES,
    )

    assert {
        "fs_write_file",
        "fs_append_file",
        "fs_apply_file_edits",
        "fs_search_and_replace",
        "fs_save_base64_file",
        "download_file",
        "session_memory",
        "project_memory",
        "session_compact",
        "turn_finish",
        "turn_ask_human",
        "turn_propose_plan",
    } == IMPORTANT_TOOL_NAMES
    assert FILE_WRITE_TOOL_NAMES <= IMPORTANT_TOOL_NAMES
    assert set() == IMPORTANT_TOOL_NAMES & EXEC_TOOL_NAMES
    assert isinstance(IMPORTANT_TOOL_NAMES, frozenset)


def test_every_important_and_exec_tool_name_exists_in_the_registry():
    """Переименование тула, не доехавшее до набора, ловится здесь. Реестр собран
    под `chat_completions`: `session_compact` регистрируется только там
    (`tools/registry.py::_session_registry`), и автовыбор работает только там же."""
    from gc_core.constants.tool_names import (
        EXEC_TOOL_NAMES,
        IMPORTANT_TOOL_NAMES,
    )
    from gc_core.tools.registry import all_builtin_tool_names
    from gc_core.tools.skills.skills_tools import get_skills_tools_data
    from gc_testkit.settings import make_settings

    names = all_builtin_tool_names(
        make_settings({"backend.api_mode": "chat_completions"})
    ) | set(get_skills_tools_data(None))
    assert names >= IMPORTANT_TOOL_NAMES
    assert names >= EXEC_TOOL_NAMES
