"""Гейт режима перед вызовом тула: MCP, скрипты скиллов, неразмеченные тулы."""

import asyncio
import json

from gc_testkit.settings import fake_ctx

from gc_core.execution.state import SharedStateData
from gc_core.security.dual_wrapper import arun_guard_flow
from gc_core.security.permission_gate import permission_gate_flow
from gc_core.security.permission_mode import (
    ACTION_CATEGORIES_KEY,
    CAT_EDIT,
    CAT_MCP,
    CAT_PERSISTENT_STATE,
    CAT_SKILL,
    CAT_SKILL_EXEC,
    CAT_STATE,
    CAT_TURN,
)
from gc_core.security.permission_store import PermissionStore

_MCP = {"category": CAT_MCP, "mcp_server": "ddgs"}
_SCRIPT = {
    "category": CAT_SKILL_EXEC,
    "decision_args": ("skill_id", "script_name"),
}


class _Logger:
    def __init__(self):
        self.events = []

    def info(self, message, *args):
        text = message % args if args else message
        self.events.append(json.loads(text.split(" ", 1)[1]))


def _ctx(mode=None, *, answers=None):
    """Контекст с настоящим стором; `answers` — очередь ответов человека
    (нет очереди — человека нет вовсе, вопрос вырождается в отказ)."""
    shared = SharedStateData(permission_mode=mode)
    asked = []

    def tui_confirm(*, title, question, fields, **kwargs):
        asked.append(
            {"title": title, "question": question, "fields": dict(fields)}
        )
        return answers.pop(0) if answers else "deny_once"

    ctx = fake_ctx(
        permission_store=PermissionStore(shared.confirm_rules, shared=shared),
        logger=_Logger(),
        is_tui=answers is not None,
        tui_confirm=tui_confirm if answers is not None else None,
    )
    ctx.asked = asked
    ctx.shared = shared
    return ctx


def _run(tool_name, entry, ctx, arguments=None):
    """Прогнать гейт; вернуть (результат, вызван ли тул)."""
    called = []

    async def invoke():
        called.append(True)
        return "tool result"

    result = asyncio.run(
        arun_guard_flow(
            permission_gate_flow(tool_name, entry, ctx, arguments), invoke
        )
    )
    return result, bool(called)


def _audit(ctx):
    return [(e["decision"], e["reason_code"]) for e in ctx.logger.events]


def test_tools_with_their_own_guard_pass_through():
    assert _run("fs_write_file", {"category": CAT_EDIT}, _ctx("manual")) == (
        "tool result",
        True,
    )


def test_turn_and_skill_reading_are_always_allowed():
    for entry in ({"category": CAT_TURN}, {"category": CAT_SKILL}):
        ctx = _ctx("manual")
        assert _run("t", entry, ctx) == ("tool result", True)
        assert ctx.asked == []


def test_mcp_runs_silently_in_edit():
    ctx = _ctx("edit", answers=[])
    assert _run("ddgs_search", _MCP, ctx) == ("tool result", True)
    assert ctx.asked == []


def test_mcp_asks_in_manual_and_runs_on_yes():
    ctx = _ctx("manual", answers=["allow_once"])
    assert _run("ddgs_search", _MCP, ctx) == ("tool result", True)
    assert ctx.asked[0]["fields"] == {
        "Сервер": "ddgs",
        "Инструмент": "ddgs_search",
    }
    assert _audit(ctx) == [("executed", "mode_ask_confirmed")]


def test_mcp_denied_in_manual_when_user_declines():
    ctx = _ctx("manual", answers=["deny_once"])
    out, called = _run("ddgs_search", _MCP, ctx)
    assert called is False
    error = json.loads(out)["error"]
    assert error["type"] == "declined_by_user"
    assert error["category"] == "mcp"
    assert error["reason"] == "Declined by the user."
    assert "Do NOT retry" in error["guidance"]
    assert _audit(ctx) == [("blocked", "mode_ask_cancelled")]


def test_remembered_yes_covers_the_tool_not_the_whole_server():
    """Y запоминает инструмент целиком (без привязки к аргументам), но не
    сервер: другой инструмент того же сервера спросит сам."""
    ctx = _ctx("manual", answers=["allow_remember", "deny_once"])
    assert _run("ddgs_search", _MCP, ctx, {"q": "a"})[1] is True
    assert _run("ddgs_search", _MCP, ctx, {"q": "b"})[1] is True
    assert len(ctx.asked) == 1
    assert _run("ddgs_news", _MCP, ctx)[1] is False
    assert len(ctx.asked) == 2


def test_unmarked_tool_keeps_old_behaviour_in_edit_and_asks_in_manual():
    """Режим по умолчанию не меняет поведение ни одного тула — и неразмеченного
    тоже. В manual такой тул спрашивается; нет человека — отказ."""
    assert _run("weird_tool", {}, _ctx("edit")) == ("tool result", True)
    out, called = _run("weird_tool", {}, _ctx("manual"))
    assert called is False
    assert json.loads(out)["error"]["category"] == "unknown"


def test_skill_script_question_identifies_the_script():
    ctx = _ctx("manual", answers=["deny_once"])
    _run(
        "skills_run_script",
        _SCRIPT,
        ctx,
        {"skill_id": "pdf", "script_name": "x.py"},
    )
    assert ctx.asked[0]["fields"]["skill_id"] == "pdf"
    assert ctx.asked[0]["fields"]["script_name"] == "x.py"
    assert "x.py" in ctx.asked[0]["question"]


def test_different_scripts_are_asked_separately():
    ctx = _ctx("manual", answers=["allow_remember", "allow_remember"])
    a = {"skill_id": "pdf", "script_name": "a.py", "argv": ["1"]}
    b = {"skill_id": "pdf", "script_name": "b.py"}
    assert _run("skills_run_script", _SCRIPT, ctx, a)[1] is True
    assert (
        _run("skills_run_script", _SCRIPT, ctx, {**a, "argv": ["2"]})[1]
        is True
    )
    assert len(ctx.asked) == 1  # тот же скрипт с другими argv — уже разрешён
    assert _run("skills_run_script", _SCRIPT, ctx, b)[1] is True
    assert len(ctx.asked) == 2


# --- запомненный запрет сильнее режима ------------------------------------------


def test_remembered_denial_holds_after_switching_to_edit():
    """N в manual, затем переключение в edit: MCP-тул в edit не спрашивается
    вовсе — но запрет человека про ЭТОТ тул действует и там."""
    ctx = _ctx("manual", answers=["deny_remember"])
    assert _run("ddgs_search", _MCP, ctx)[1] is False

    ctx.shared.permission_mode = "edit"
    out, called = _run("ddgs_search", _MCP, ctx)
    assert called is False
    error = json.loads(out)["error"]
    assert error["type"] == "denied_by_user_for_session"
    assert error["reason"] == "Denied by the user for this session."
    assert len(ctx.asked) == 1  # второго вопроса не было
    assert _audit(ctx)[-1] == ("blocked", "session_rule_deny")
    # другой инструмент того же сервера запрет не задевает
    assert _run("ddgs_news", _MCP, ctx)[1] is True


def test_one_time_denial_does_not_outlive_the_mode_switch():
    ctx = _ctx("manual", answers=["deny_once"])
    assert _run("ddgs_search", _MCP, ctx)[1] is False
    ctx.shared.permission_mode = "edit"
    assert _run("ddgs_search", _MCP, ctx) == ("tool result", True)


# --- гейт стоит на пути настоящего диспатча ----------------------------------------


class _FakeTool:
    def __init__(self, **kwargs):
        pass

    async def acall(self, **kwargs):
        return "tool ran"


def _dispatch(ctx, entry):
    from gc_core.tools import aapply_tool

    spec = {
        "name": "mcp_ddgs_search",
        "parameters": {"type": "object", "properties": {}},
    }
    data = {
        "mcp_ddgs_search": {"tool_fabric": _FakeTool, "spec": spec, **entry}
    }
    return asyncio.run(aapply_tool("mcp_ddgs_search", {}, data, ctx=ctx))


def test_dispatch_runs_the_gate_before_the_tool():
    assert _dispatch(_ctx("edit"), _MCP) == "tool ran"
    out = _dispatch(
        _ctx("manual"), _MCP
    )  # человека нет — вопрос вырождается в отказ
    assert json.loads(out)["error"]["type"] == "confirmation_unavailable"


def test_dispatch_of_an_unmarked_tool():
    assert _dispatch(_ctx("edit"), {}) == "tool ran"
    out = _dispatch(_ctx("manual"), {})
    assert json.loads(out)["error"]["category"] == "unknown"


def test_one_time_answer_does_not_carry_over_to_the_next_dispatch():
    """«Разрешить разово» — на один вызов: следующий такой же вызов спрашивает
    снова. «Разрешить в сессии» — спрашивает один раз."""
    ctx = _ctx("manual", answers=["allow_once", "allow_once"])
    assert _dispatch(ctx, _MCP) == "tool ran"
    assert _dispatch(ctx, _MCP) == "tool ran"
    assert len(ctx.asked) == 2

    ctx = _ctx("manual", answers=["allow_remember"])
    assert _dispatch(ctx, _MCP) == "tool ran"
    assert _dispatch(ctx, _MCP) == "tool ran"
    assert len(ctx.asked) == 1


def test_one_time_refusal_asks_again_on_the_next_dispatch():
    ctx = _ctx("manual", answers=["deny_once", "allow_once"])
    assert (
        json.loads(_dispatch(ctx, _MCP))["error"]["type"] == "declined_by_user"
    )
    assert _dispatch(ctx, _MCP) == "tool ran"
    assert len(ctx.asked) == 2


def test_plan_denies_mcp_and_unmarked_tools_without_asking():
    for entry in (_MCP, {}):
        ctx = _ctx("plan", answers=["allow_remember"])
        out, called = _run("ddgs_search", entry, ctx)
        assert called is False
        error = json.loads(out)["error"]
        assert error["type"] == "permission_denied_by_mode"
        assert error["mode"] == "plan"
        assert ctx.asked == []


def test_denied_envelope_separates_human_reason_from_model_instruction():
    ctx = _ctx("plan")
    out, _called = _run("ddgs_search", _MCP, ctx)
    error = json.loads(out)["error"]
    assert error["category"] == "mcp"
    assert (
        error["reason"]
        == "Permission mode 'plan' does not allow 'mcp' actions."
    )
    assert "Do NOT" not in error["reason"]
    # инструкция модели — отдельным полем, как у отказов модерации команд
    assert "Do NOT retry" in error["guidance"]
    assert "message" not in error


#: Как в реестре (`tools/registry._project_registry`): чтение — состояние,
#: запись — постоянное состояние.
_PROJECT_MEMORY = {
    "category": CAT_STATE,
    ACTION_CATEGORIES_KEY: {
        "append": CAT_PERSISTENT_STATE,
        "write": CAT_PERSISTENT_STATE,
    },
}


def test_manual_asks_before_writing_project_memory():
    """Память проекта попадает в промпт следующих сессий: в manual её
    запись — действие с вопросом, как запись файла."""
    ctx = _ctx("manual", answers=["deny_once"])
    out, called = _run(
        "project_memory", _PROJECT_MEMORY, ctx, {"action": "append"}
    )
    assert called is False
    assert json.loads(out)["error"]["type"] == "declined_by_user"
    assert ctx.asked[0]["fields"]["Категория"] == CAT_PERSISTENT_STATE


def test_session_answer_covers_later_project_memory_writes():
    ctx = _ctx("manual", answers=["allow_remember"])
    for action in ("append", "write", "append"):
        assert _run(
            "project_memory", _PROJECT_MEMORY, ctx, {"action": action}
        ) == ("tool result", True), action
    assert len(ctx.asked) == 1


def test_project_memory_reading_and_other_modes_are_not_asked():
    ctx = _ctx("manual", answers=[])
    assert _run(
        "project_memory", _PROJECT_MEMORY, ctx, {"action": "read"}
    ) == ("tool result", True)
    for mode in ("edit", "auto"):
        ctx = _ctx(mode, answers=[])
        assert _run(
            "project_memory", _PROJECT_MEMORY, ctx, {"action": "append"}
        ) == ("tool result", True), mode
        assert ctx.asked == [], mode
