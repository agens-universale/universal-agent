"""Гейт: общий ``ToolsCache`` не протекает мимо фильтра тулов субагента.

Вопрос, ради которого тест написан. Кэш дискавери теперь ОБЩИЙ у родителя и
субагента (см. ``test_tools_cache_injectable``): ключ считается по содержимому
``mcp-servers``, а дочерний конфиг эту секцию не меняет — значит попадание.
Но у ребёнка СВОЙ набор доступных тулов: ``subagent_tools_allow`` /
``subagent_tools_stop`` из определения субагента.

Если бы фильтр применялся внутри кэшируемого участка, общий кэш означал бы,
что ребёнок получает родительский набор — то есть тулы, которые ему запрещены.
Тихо, без ошибки, с полным доступом.

Сегодня этого не происходит: ``aget_mcp_tools_data_for_config`` кладёт в кэш СЫРОЙ набор
MCP-тулов, а ``apply_subagent_tools_filter`` работает уже над собранным
``tools_data`` и читает allow/stop из ПЕРЕДАННОГО ``config_dict``
(``tools/__init__.py``: дискавери, потом фильтр). Тест закрепляет этот порядок.
"""
from typing import Any, Dict

import pytest

import gc_core.tools as tools_pkg
from gc_core.tools import SUBAGENT_TOOLS_ALLOW_KEY, SUBAGENT_TOOLS_STOP_KEY


_MCP_TOOL = "mcp_srv_dangerous"


@pytest.fixture()
def shared_discovery(monkeypatch):
    """Один «кэш» на всех: считает, сколько раз ходили в дискавери."""
    calls = {"n": 0}
    payload = {
        _MCP_TOOL: {
            "tool_fabric": lambda **_kw: None,
            "spec": {"type": "function", "function": {"name": _MCP_TOOL}},
            "description": f"{_MCP_TOOL}: тестовый MCP-тул",
        }
    }

    async def fake_get_mcp(config_dict, on_progress=None, **_kw):
        calls["n"] += 1
        return (dict(payload), {})

    from gc_core.tools.connect_mcp import agent_tools as _at

    monkeypatch.setattr(_at, "aget_mcp_tools_data_for_config", fake_get_mcp)
    return calls


def _cfg(**extra: Any) -> Dict[str, Any]:
    cfg: Dict[str, Any] = {
        "mcp-servers": {"srv": {"transport": "stdio", "command": "x"}},
        "backend_api_mode": "chat_completions",
        "use_local_subagents": False,
    }
    cfg.update(extra)
    return cfg


async def test_child_stop_list_still_applies_with_a_shared_cache(shared_discovery):
    """Родитель видит MCP-тул, ребёнок с stop-списком — нет."""
    parent_tools, *_ = await tools_pkg.abuild_tools_data(_cfg())
    child_tools, *_ = await tools_pkg.abuild_tools_data(
        _cfg(**{SUBAGENT_TOOLS_STOP_KEY: [_MCP_TOOL]})
    )

    assert _MCP_TOOL in parent_tools, "родитель почему-то не получил MCP-тул"
    assert _MCP_TOOL not in child_tools, (
        "запрещённый тул доехал до субагента — фильтр применяется не там, "
        "где нужно, либо уехал внутрь кэшируемого участка"
    )


async def test_child_allow_list_narrows_the_set(shared_discovery):
    """allow-список сужает набор ребёнка, не трогая родительский."""
    parent_tools, *_ = await tools_pkg.abuild_tools_data(_cfg())
    child_tools, *_ = await tools_pkg.abuild_tools_data(
        _cfg(**{SUBAGENT_TOOLS_ALLOW_KEY: ["session_think"]})
    )

    assert _MCP_TOOL in parent_tools
    assert _MCP_TOOL not in child_tools
    assert len(child_tools) < len(parent_tools)


def test_filter_runs_after_mcp_assembly():
    """Структурно: сначала подмешать MCP, потом фильтровать — порядок обязателен.

    Если фильтр переедет ВЫШЕ подмешивания ``mcp_tools_data``, ``mcp_*`` станет
    нефильтруемым (об этом прямо сказано в докстроке
    ``apply_subagent_tools_filter``), и общий ToolsCache начнёт отдавать ребёнку
    набор, который ему запрещён.

    Смотрим ``_finish_tools_data``: досборка живёт там с тех пор, как у
    ``abuild_tools_data`` появился асинхронный двойник — тело у них общее, и
    порядок достаточно закрепить в одном месте.
    """
    import ast

    from gc_testkit.paths import REPO

    src = (REPO / "packages/core/src/gc_core/tools/__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_finish_tools_data"
    )

    merge_line = None
    for node in ast.walk(fn):
        # `for k, v in mcp_tools_data.items(): tools_data[k] = v`
        if isinstance(node, ast.For) and isinstance(node.iter, ast.Call):
            src_obj = getattr(node.iter.func, "value", None)
            if isinstance(src_obj, ast.Name) and src_obj.id == "mcp_tools_data":
                merge_line = node.lineno
                break
    assert merge_line is not None, "не нашёл подмешивание mcp_tools_data"

    filter_line = None
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            fname = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if fname == "apply_subagent_tools_filter":
                filter_line = node.lineno
                break
    assert filter_line is not None, "не нашёл apply_subagent_tools_filter"

    assert merge_line < filter_line, (
        "фильтр субагента оказался ВЫШЕ подмешивания MCP — mcp_* перестанут "
        "фильтроваться, а общий ToolsCache начнёт отдавать ребёнку чужой набор"
    )


def test_the_single_build_form_calls_the_shared_finish():
    """Сборка тулов одна, и досборку она обязана звать общую.

    Тест назывался «обе формы делят досборку» и сверял синхронную с
    асинхронной. Синхронной больше нет — она была тонкой обёрткой через свой
    loop и после перехода дискавери на нативный путь осталась без вызывающих.
    Проверяемое свойство осталось: фильтр набора живёт в `_finish_tools_data`,
    и сборка не имеет права собирать набор мимо него.
    """
    import ast

    from gc_testkit.paths import REPO

    src = (REPO / "packages/core/src/gc_core/tools/__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    forms = [n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name == "build_tools_data"]
    assert forms == [], "вернулась синхронная сборка тулов"

    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "abuild_tools_data")
    calls = {
        getattr(c.func, "id", None) or getattr(c.func, "attr", None)
        for c in ast.walk(fn) if isinstance(c, ast.Call)
    }
    assert "_finish_tools_data" in calls, (
        "сборка собирает набор мимо общей досборки — фильтр субагента разъедется"
    )
