"""Категории тулов: по ним режим разрешений решает, спрашивать ли человека.

Сторож: тул без категории попадает в «неизвестные» и в любом режиме
спрашивает подтверждение (а без человека — запрещается). Новый тул обязан
объявить категорию там же, где он регистрируется.
"""

import inspect

from gc_testkit.settings import make_settings

from gc_core.security.permission_mode import (
    CAT_EDIT,
    CAT_EXEC,
    CAT_MCP,
    CAT_NETWORK,
    CAT_READ,
    CAT_SKILL,
    CAT_SKILL_EXEC,
    CAT_STATE,
    CAT_SUBAGENT,
    CAT_TURN,
)
from gc_core.tools.registry import (
    all_builtin_tool_names,
    get_builtin_tools_data,
)

_KNOWN = {
    CAT_EDIT, CAT_EXEC, CAT_MCP, CAT_NETWORK, CAT_READ, CAT_SKILL,
    CAT_SKILL_EXEC, CAT_STATE, CAT_SUBAGENT, CAT_TURN,
}  # fmt: skip


def _settings():
    return make_settings({"backend.api_mode": "chat_completions"})


def _skills_tools():
    from gc_core.tools.skills.registry import SkillsRegistry
    from gc_core.tools.skills.skills_tools import get_skills_tools_data

    return get_skills_tools_data(SkillsRegistry.__new__(SkillsRegistry))


def _missing(data):
    return sorted(
        name
        for name, entry in data.items()
        if entry.get("category") not in _KNOWN
    )


def test_every_builtin_tool_has_category():
    data = get_builtin_tools_data(_settings())
    assert _missing(data) == [], "встроенные тулы без категории"
    assert set(data) == all_builtin_tool_names(_settings())


def test_known_category_assignments():
    data = get_builtin_tools_data(_settings())
    expected = {
        "fs_write_file": CAT_EDIT,
        "fs_append_file": CAT_EDIT,
        "fs_apply_file_edits": CAT_EDIT,
        "fs_search_and_replace": CAT_EDIT,
        "fs_save_base64_file": CAT_EDIT,
        # двухприродный (сеть + запись) — по сильнейшему эффекту
        "download_file": CAT_EDIT,
        "fs_read_file": CAT_READ,
        "fs_file_info": CAT_READ,
        "fs_list_files": CAT_READ,
        "fs_find_files_by_name": CAT_READ,
        "fs_find_in_files": CAT_READ,
        "fs_view_image": CAT_READ,
        "exec_calculator": CAT_READ,
        "exec_sleep": CAT_READ,
        "session_think": CAT_READ,
        "session_read_stored_tool_output": CAT_READ,
        "session_read_user_intent_log": CAT_READ,
        "exec_command": CAT_EXEC,
        "exec_python": CAT_EXEC,
        "exec_fetch_url": CAT_NETWORK,
        "turn_finish": CAT_TURN,
        "turn_propose_plan": CAT_TURN,
        "turn_ask_human": CAT_TURN,
        "turn_intermediate_reply": CAT_TURN,
        "session_memory": CAT_STATE,
        "session_todo": CAT_STATE,
        "session_compact": CAT_STATE,
        # путь задаёт рантайм, агент управляет только содержимым
        "project_memory": CAT_STATE,
    }
    actual = {name: data[name]["category"] for name in expected}
    assert actual == expected
    assert sorted(set(data) - set(expected)) == [], (
        "тул без записи в этом тесте: занесите его категорию осознанно"
    )


def test_every_skill_tool_has_category():
    assert _missing(_skills_tools()) == [], "скилл-тулы без категории"


def test_skills_run_script_is_execution_not_plain_skill():
    """skills_run_script запускает произвольный код мимо модерации команд."""
    data = _skills_tools()
    assert data["skills_run_script"]["category"] == CAT_SKILL_EXEC
    for name in sorted(set(data) - {"skills_run_script"}):
        assert data[name]["category"] == CAT_SKILL, name


def test_skills_run_script_declares_what_identifies_a_call():
    """Без decision_args ключ решения был бы «имя тула», и одно «разрешить»
    открывало бы исполнение любого скрипта до конца сессии."""
    entry = _skills_tools()["skills_run_script"]
    assert tuple(entry["decision_args"]) == ("skill_id", "script_name")


def test_every_subagent_tool_has_category():
    from gc_core.tools.subagents.registry import get_subagents_tools_data

    data = get_subagents_tools_data(_settings())
    assert data, "реестр субагентов пуст — тест ничего не проверил"
    assert {entry.get("category") for entry in data.values()} == {CAT_SUBAGENT}


def test_mcp_tool_entry_carries_category_and_server():
    """Категория и сервер ставятся там, где запись MCP-тула собирается: по ним
    строится ключ решения `mcp:<server>:<tool>`."""
    from gc_core.tools.connect_mcp import agent_tools

    source = inspect.getsource(agent_tools)
    assert '"category": CAT_MCP' in source
    assert '"mcp_server": server_key' in source


def test_edit_and_read_tools_are_guarded():
    """Категория edit / read / exec / network обещает, что решение примет
    защита этой зоны: гейт такие категории пропускает. Тул с этой категорией
    без защиты — дыра. Список выводится из реестра, а не ведётся руками."""
    markers = {
        CAT_EDIT: ("enforce_write_guard", "check_write_guard_path"),
        CAT_EXEC: ("moderate_command_execution",),
        CAT_NETWORK: ("enforce_url_policy",),
    }
    unguarded = []
    for name, entry in get_builtin_tools_data(_settings()).items():
        expected = markers.get(entry["category"])
        if expected is None:
            continue
        source = inspect.getsource(inspect.getmodule(entry["tool_fabric"]))
        if not any(marker in source for marker in expected):
            unguarded.append(name)
    assert unguarded == [], f"тулы без защиты своей зоны: {unguarded}"
