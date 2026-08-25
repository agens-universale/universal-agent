"""Гейт: общий ``ToolsCache`` не протекает мимо фильтра тулов субагента.

Вопрос, ради которого тест написан. Кэш дискавери теперь ОБЩИЙ у родителя и
субагента (см. ``test_tools_cache_injectable``): ключ считается по содержимому
``mcp-servers``, а дочерний конфиг эту секцию не меняет — значит попадание.
Но у ребёнка СВОЙ набор доступных тулов: ``subagent_tools_allow`` /
``subagent_tools_stop`` из определения субагента.

Если бы фильтр применялся внутри кэшируемого участка, общий кэш означал бы,
что ребёнок получает родительский набор — то есть тулы, которые ему запрещены.
Тихо, без ошибки, с полным доступом.

Сегодня этого не происходит: ``get_mcp_tools_data`` кладёт в кэш СЫРОЙ набор
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

    def fake_get_mcp(config_dict, on_progress=None, **_kw):
        calls["n"] += 1
        return (dict(payload), {})

    monkeypatch.setattr(tools_pkg, "get_mcp_tools_data", fake_get_mcp)
    return calls


def _cfg(**extra: Any) -> Dict[str, Any]:
    cfg: Dict[str, Any] = {
        "mcp-servers": {"srv": {"transport": "stdio", "command": "x"}},
        "backend_api_mode": "chat_completions",
        "use_local_subagents": False,
    }
    cfg.update(extra)
    return cfg


def test_child_stop_list_still_applies_with_a_shared_cache(shared_discovery):
    """Родитель видит MCP-тул, ребёнок с stop-списком — нет."""
    parent_tools, *_ = tools_pkg.build_tools_data(_cfg())
    child_tools, *_ = tools_pkg.build_tools_data(
        _cfg(**{SUBAGENT_TOOLS_STOP_KEY: [_MCP_TOOL]})
    )

    assert _MCP_TOOL in parent_tools, "родитель почему-то не получил MCP-тул"
    assert _MCP_TOOL not in child_tools, (
        "запрещённый тул доехал до субагента — фильтр применяется не там, "
        "где нужно, либо уехал внутрь кэшируемого участка"
    )


def test_child_allow_list_narrows_the_set(shared_discovery):
    """allow-список сужает набор ребёнка, не трогая родительский."""
    parent_tools, *_ = tools_pkg.build_tools_data(_cfg())
    child_tools, *_ = tools_pkg.build_tools_data(
        _cfg(**{SUBAGENT_TOOLS_ALLOW_KEY: ["session_think"]})
    )

    assert _MCP_TOOL in parent_tools
    assert _MCP_TOOL not in child_tools
    assert len(child_tools) < len(parent_tools)


def test_filter_runs_after_mcp_assembly():
    """Структурно: сначала MCP, потом фильтр — порядок обязателен.

    Если фильтр переедет выше ``get_mcp_tools_data``, ``mcp_*`` станет
    нефильтруемым (об этом прямо сказано в докстроке
    ``apply_subagent_tools_filter``), и общий кэш начнёт протекать.
    """
    import ast

    from gc_testkit.paths import REPO

    src = (REPO / "packages/core/src/gc_core/tools/__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "build_tools_data"
    )

    def line_of(name: str) -> int:
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                fname = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if fname == name:
                    return node.lineno
        raise AssertionError(f"не нашёл вызов {name} в build_tools_data")

    assert line_of("get_mcp_tools_data") < line_of("apply_subagent_tools_filter"), (
        "фильтр субагента оказался ВЫШЕ сборки MCP — mcp_* перестанут "
        "фильтроваться, а общий ToolsCache начнёт отдавать ребёнку чужой набор"
    )
