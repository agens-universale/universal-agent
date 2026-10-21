"""Стадия режима разрешений в защитах записи, чтения и команд.

Режим применяется ДО проверки `<guard>.enabled`: выключенная в конфиге защита
режим не отменяет. Запомненный запрет человека сильнее режима.
"""

import json

import pytest
from gc_testkit.settings import fake_ctx

from gc_core.execution.state import SharedStateData
from gc_core.security.path_policy import enforce_write_guard
from gc_core.security.permission_store import PermissionStore


class _Logger:
    def __init__(self):
        self.events = []

    def info(self, message, *args):
        text = message % args if args else message
        self.events.append(json.loads(text.split(" ", 1)[1]))


def make_ctx(mode, *, launch_root, config=None, answers=None):
    """Контекст с настоящим стором. `answers` — очередь ответов человека;
    None — человека нет вовсе (вопрос вырождается в отказ)."""
    shared = SharedStateData(permission_mode=mode)
    asked = []

    def tui_confirm(*, title, question, fields, **kwargs):
        asked.append(title)
        return answers.pop(0) if answers else "deny_once"

    ctx = fake_ctx(
        config={"launch_root": str(launch_root), **(config or {})},
        permission_store=PermissionStore(shared.confirm_rules, shared=shared),
        logger=_Logger(),
        is_tui=answers is not None,
        tui_confirm=tui_confirm if answers is not None else None,
    )
    ctx.asked = asked
    ctx.shared = shared
    return ctx


def audit(ctx):
    return [(e["decision"], e["reason_code"]) for e in ctx.logger.events]


@enforce_write_guard(path_arg="path")
def _fake_write(path: str, ctx=None) -> str:
    return "written"


def _outside(tmp_path):
    """Цель записи ВНЕ периметра: launch_root — соседний каталог."""
    root = tmp_path / "root"
    root.mkdir()
    return str(tmp_path / "outside.txt"), root


# --- запись ---------------------------------------------------------------------


def test_edit_writes_inside_the_project_silently(tmp_path):
    ctx = make_ctx("edit", launch_root=tmp_path, answers=[])
    assert _fake_write(path=str(tmp_path / "a.txt"), ctx=ctx) == "written"
    assert ctx.asked == []
    assert audit(ctx) == [("executed", "policy_allow")]


def test_manual_asks_inside_the_project(tmp_path):
    ctx = make_ctx("manual", launch_root=tmp_path, answers=["deny_once"])
    out = json.loads(_fake_write(path=str(tmp_path / "a.txt"), ctx=ctx))
    assert ctx.asked == ["Запись в файл"]
    error = out["error"]
    assert error["type"] == "declined_by_user"
    assert error["reason"] == "Declined by the user."
    assert "Do NOT" not in error["reason"]
    assert "Do NOT retry" in error["guidance"]
    assert audit(ctx) == [("blocked", "mode_ask_cancelled")]


def test_manual_writes_after_confirmation(tmp_path):
    ctx = make_ctx("manual", launch_root=tmp_path, answers=["allow_once"])
    assert _fake_write(path=str(tmp_path / "a.txt"), ctx=ctx) == "written"
    assert audit(ctx) == [("executed", "mode_ask_confirmed")]


def test_manual_without_a_human_denies_the_write(tmp_path):
    """Сервер, консоль без терминала: спросить некого — запись запрещена."""
    ctx = make_ctx("manual", launch_root=tmp_path)
    out = json.loads(_fake_write(path=str(tmp_path / "a.txt"), ctx=ctx))
    assert out["error"]["type"] == "confirmation_unavailable"


def test_disabled_guard_does_not_cancel_manual(tmp_path):
    ctx = make_ctx(
        "manual",
        launch_root=tmp_path,
        config={"security.write_guard.enabled": False},
        answers=["deny_once"],
    )
    out = json.loads(_fake_write(path=str(tmp_path / "a.txt"), ctx=ctx))
    assert out["error"]["type"] == "declined_by_user"
    assert ctx.asked == ["Запись в файл"]


def test_manual_asks_once_outside_the_project(tmp_path):
    """Вне периметра manual спрашивает сам — и после «да» защита периметра
    второй вопрос про тот же путь не задаёт."""
    target, root = _outside(tmp_path)
    ctx = make_ctx("manual", launch_root=root, answers=["allow_once"])
    assert _fake_write(path=target, ctx=ctx) == "written"
    assert ctx.asked == ["Запись в файл"]


def test_explicit_config_deny_beats_the_manual_question(tmp_path):
    target, root = _outside(tmp_path)
    ctx = make_ctx(
        "manual",
        launch_root=root,
        config={"security.write_guard.mode": "deny"},
        answers=["allow_once"],
    )
    out = json.loads(_fake_write(path=target, ctx=ctx))
    assert out["ok"] is False
    assert ctx.asked == []
    assert audit(ctx) == [("blocked", "mode_deny")]


def test_edit_outside_the_project_is_left_to_the_guard(tmp_path):
    """В edit режим ничего не добавляет: вне периметра спрашивает сама защита
    — своим вопросом и по своему ключу `mode`."""
    target, root = _outside(tmp_path)
    ctx = make_ctx("edit", launch_root=root, answers=["allow_once"])
    assert _fake_write(path=target, ctx=ctx) == "written"
    assert ctx.asked == ["Защита записи"]
    assert audit(ctx) == [("executed", "policy_ask_confirmed")]


def test_unclassifiable_path_is_not_a_mode_question(tmp_path):
    """Пустой путь режим не классифицирует и про него не спрашивает: дальше
    всё идёт как без режима (в edit исход тот же)."""
    for mode in ("manual", "edit"):
        ctx = make_ctx(mode, launch_root=tmp_path, answers=["allow_once"])
        with pytest.raises(ValueError):
            _fake_write(path="", ctx=ctx)
        assert ctx.asked == [], mode


# --- запомненные ответы и смена режима -------------------------------------------


def test_remembered_denial_holds_after_switching_to_edit(tmp_path):
    """N в manual на запись ВНУТРИ проекта; в edit такая запись не спрашивается
    вовсе — но запрет человека про этот файл действует и там."""
    target = str(tmp_path / "a.py")
    ctx = make_ctx("manual", launch_root=tmp_path, answers=["deny_remember"])
    assert json.loads(_fake_write(path=target, ctx=ctx))["ok"] is False

    ctx.shared.permission_mode = "edit"
    out = json.loads(_fake_write(path=target, ctx=ctx))
    assert out["ok"] is False
    assert out["error"]["type"] == "denied_by_user_for_session"
    assert out["error"]["reason"] == "Denied by the user for this session."
    assert out["error"]["path"] == target
    assert ctx.asked == ["Запись в файл"]  # второго вопроса не было
    assert audit(ctx)[-1] == ("blocked", "session_rule_deny")
    # другой файл запрет не задевает
    assert _fake_write(path=str(tmp_path / "b.py"), ctx=ctx) == "written"


def test_remembered_denial_covers_other_spellings_of_the_same_path(tmp_path):
    ctx = make_ctx("manual", launch_root=tmp_path, answers=["deny_remember"])
    _fake_write(path=str(tmp_path / "a.py"), ctx=ctx)
    ctx.shared.permission_mode = "edit"
    for spelling in ("a.py", "./a.py", "sub/../a.py"):
        out = json.loads(_fake_write(path=spelling, ctx=ctx))
        assert out["error"]["type"] == "denied_by_user_for_session", spelling


def test_one_time_denial_does_not_outlive_the_mode_switch(tmp_path):
    target = str(tmp_path / "a.py")
    ctx = make_ctx("manual", launch_root=tmp_path, answers=["deny_once"])
    assert json.loads(_fake_write(path=target, ctx=ctx))["ok"] is False
    ctx.shared.permission_mode = "edit"
    assert _fake_write(path=target, ctx=ctx) == "written"


def test_remembered_yes_is_not_asked_again(tmp_path):
    target = str(tmp_path / "a.py")
    ctx = make_ctx("manual", launch_root=tmp_path, answers=["allow_remember"])
    assert _fake_write(path=target, ctx=ctx) == "written"
    assert _fake_write(path="./a.py", ctx=ctx) == "written"
    assert ctx.asked == ["Запись в файл"]


# --- пол файлов инструкций --------------------------------------------------------


def _agents_md(tmp_path, monkeypatch):
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    return str(tmp_path / "AGENTS.md")


def _global_agents_md(tmp_path, monkeypatch):
    """Глобальный файл инструкций: ВНЕ проекта, но под полом instructions."""
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    (tmp_path / "gc").mkdir()
    root = tmp_path / "root"
    root.mkdir()
    return str(tmp_path / "gc" / "AGENTS.md"), root


def test_floor_question_declined_is_audited(tmp_path, monkeypatch):
    ctx = make_ctx("edit", launch_root=tmp_path, answers=["deny_once"])
    out = json.loads(
        _fake_write(path=_agents_md(tmp_path, monkeypatch), ctx=ctx)
    )
    assert out["error"]["type"] == "protected_path_denied"
    assert ctx.asked == ["Защищённый путь"]
    assert audit(ctx) == [("blocked", "protected_ask_cancelled")]


def test_floor_question_confirmed_is_audited_before_the_outcome(
    tmp_path, monkeypatch
):
    """«Да» пишется отдельной строкой `confirmed`: запись ещё не выполнена,
    её итог даёт следующая строка."""
    ctx = make_ctx("edit", launch_root=tmp_path, answers=["allow_once"])
    assert (
        _fake_write(path=_agents_md(tmp_path, monkeypatch), ctx=ctx)
        == "written"
    )
    assert audit(ctx) == [
        ("confirmed", "protected_ask_confirmed"),
        ("executed", "policy_allow"),
    ]
    assert ctx.logger.events[0]["risk_flags"] == ["touches_protected_paths"]


def test_floor_yes_is_not_asked_again_by_manual(tmp_path, monkeypatch):
    """Вопрос пола и вопрос режима — про одно действие. Второй не задаётся по
    явному правилу: через запомненный ответ итогом было бы `mode_ask_confirmed`."""
    ctx = make_ctx("manual", launch_root=tmp_path, answers=["allow_once"])
    assert (
        _fake_write(path=_agents_md(tmp_path, monkeypatch), ctx=ctx)
        == "written"
    )
    assert ctx.asked == ["Защищённый путь"]
    assert audit(ctx) == [
        ("confirmed", "protected_ask_confirmed"),
        ("executed", "protected_ask_confirmed"),
    ]


def test_floor_yes_covers_the_perimeter_question_in_edit(
    tmp_path, monkeypatch
):
    target, root = _global_agents_md(tmp_path, monkeypatch)
    ctx = make_ctx("edit", launch_root=root, answers=["allow_once"])
    ctx.permission_store = None  # без стора «да» запомнить негде
    assert _fake_write(path=target, ctx=ctx) == "written"
    assert ctx.asked == ["Защищённый путь"]
    assert audit(ctx) == [
        ("confirmed", "protected_ask_confirmed"),
        ("executed", "policy_ask_confirmed"),
    ]


def test_floor_yes_does_not_lift_explicit_config_deny(tmp_path, monkeypatch):
    target, root = _global_agents_md(tmp_path, monkeypatch)
    for mode in ("edit", "manual"):
        ctx = make_ctx(
            mode,
            launch_root=root,
            config={"security.write_guard.mode": "deny"},
            answers=["allow_once"],
        )
        out = json.loads(_fake_write(path=target, ctx=ctx))
        assert out["ok"] is False, mode
        assert audit(ctx)[0] == ("confirmed", "protected_ask_confirmed"), mode
        assert audit(ctx)[-1][0] == "blocked", mode


# --- чтение ---------------------------------------------------------------------


def _read_tool():
    from gc_core.security.read_guard import enforce_read_guard

    @enforce_read_guard(path_arg="path")
    def _fake_read(path: str, ctx=None) -> str:
        return "content"

    return _fake_read


def _read_outside(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("s", encoding="utf-8")
    return str(outside), root


def test_reading_inside_the_project_is_never_asked(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    for mode in ("edit", "manual"):
        ctx = make_ctx(mode, launch_root=tmp_path, answers=[])
        assert _read_tool()(path=str(target), ctx=ctx) == "content"
        assert ctx.asked == [], mode


def test_manual_asks_reading_outside_even_with_the_guard_disabled(tmp_path):
    """Выключенная защита чтения режим не отменяет — симметрия с записью."""
    target, root = _read_outside(tmp_path)
    ctx = make_ctx(
        "manual",
        launch_root=root,
        config={"security.read_guard.enabled": False},
        answers=["deny_once"],
    )
    out = json.loads(_read_tool()(path=target, ctx=ctx))
    assert ctx.asked == ["Чтение файла"]
    assert out["error"]["type"] == "declined_by_user"
    assert out["error"]["reason"] == "Declined by the user."
    assert "Do NOT retry" in out["error"]["guidance"]
    assert audit(ctx) == [("blocked", "mode_ask_cancelled")]


def test_manual_reads_outside_after_one_confirmation(tmp_path):
    target, root = _read_outside(tmp_path)
    ctx = make_ctx("manual", launch_root=root, answers=["allow_once"])
    assert _read_tool()(path=target, ctx=ctx) == "content"
    assert ctx.asked == ["Чтение файла"]


def test_edit_reading_outside_is_left_to_the_guard(tmp_path):
    target, root = _read_outside(tmp_path)
    ctx = make_ctx("edit", launch_root=root, answers=["allow_once"])
    assert _read_tool()(path=target, ctx=ctx) == "content"
    assert ctx.asked == ["Защита чтения"]


def test_guard_and_mode_questions_share_one_read_key(tmp_path):
    """Ответ, запомненный на вопросе защиты (edit), закрывает и вопрос режима
    (manual) про тот же файл: ключ один, по пути из вызова."""
    target, root = _read_outside(tmp_path)
    ctx = make_ctx("edit", launch_root=root, answers=["allow_remember"])
    assert _read_tool()(path=target, ctx=ctx) == "content"
    ctx.shared.permission_mode = "manual"
    assert _read_tool()(path=target, ctx=ctx) == "content"
    assert ctx.asked == ["Защита чтения"]


def test_remembered_read_denial_holds_in_every_mode(tmp_path):
    target, root = _read_outside(tmp_path)
    ctx = make_ctx("manual", launch_root=root, answers=["deny_remember"])
    _read_tool()(path=target, ctx=ctx)
    ctx.shared.permission_mode = "edit"
    out = json.loads(_read_tool()(path=target, ctx=ctx))
    assert out["error"]["type"] == "denied_by_user_for_session"
    assert ctx.asked == ["Чтение файла"]


# --- команды ----------------------------------------------------------------------


def _exec_tool(*, shell=True, judge=None):
    from gc_core.security.command_moderation import moderate_command_execution

    @moderate_command_execution(
        payload_arg="command",
        tool_name="exec_command" if shell else "exec_python",
        shell_payload=shell,
        judge_client_extractor=lambda *a, **kw: judge or _judge("safe"),
    )
    def _fake_exec(command: str, ctx=None, env_vars=None, cwd=None) -> str:
        return "executed"

    return _fake_exec


def _judge(verdict):
    """Судья-двойник: считает обращения и отвечает одним вердиктом."""

    def client(_payload):
        client.calls += 1
        return {
            "verdict": verdict,
            "confidence": 0.9,
            "reasons": ["r"],
            "risk_flags": [],
        }

    client.calls = 0
    return client


def test_harmless_command_runs_without_judge_or_question(tmp_path):
    for mode in ("edit", "manual"):
        judge = _judge("potentially_dangerous")
        ctx = make_ctx(mode, launch_root=tmp_path, answers=[])
        assert _exec_tool(judge=judge)(command="pwd", ctx=ctx) == "executed"
        assert judge.calls == 0, mode
        assert ctx.asked == [], mode
        assert audit(ctx) == [("executed", "mode_readonly_command")], mode


def test_command_that_prints_file_contents_takes_the_usual_route(tmp_path):
    """`cat` не в списке безобидных: в edit его смотрит судья, как в develop."""
    judge = _judge("safe")
    ctx = make_ctx("edit", launch_root=tmp_path, answers=[])
    assert _exec_tool(judge=judge)(command="cat .env", ctx=ctx) == "executed"
    assert judge.calls == 1


def test_python_payload_never_matches_the_harmless_list(tmp_path):
    judge = _judge("safe")
    ctx = make_ctx("edit", launch_root=tmp_path, answers=[])
    assert (
        _exec_tool(shell=False, judge=judge)(command="ls", ctx=ctx)
        == "executed"
    )
    assert judge.calls == 1


def test_manual_asks_the_human_instead_of_the_judge(tmp_path):
    judge = _judge("safe")
    ctx = make_ctx("manual", launch_root=tmp_path, answers=["deny_once"])
    out = json.loads(_exec_tool(judge=judge)(command="pip install x", ctx=ctx))
    assert judge.calls == 0
    assert ctx.asked == ["Выполнение команды"]
    assert out["error"]["type"] == "declined_by_user"
    assert out["error"]["reason"] == "Declined by the user."
    assert "Do NOT retry" in out["error"]["guidance"]
    assert audit(ctx) == [("blocked", "mode_ask_cancelled")]


def test_manual_runs_the_command_after_confirmation(tmp_path):
    judge = _judge("potentially_dangerous")
    ctx = make_ctx("manual", launch_root=tmp_path, answers=["allow_once"])
    assert (
        _exec_tool(judge=judge)(command="pip install x", ctx=ctx) == "executed"
    )
    assert judge.calls == 0
    assert audit(ctx) == [("executed", "mode_ask_confirmed")]


def test_disabled_moderation_does_not_cancel_manual(tmp_path):
    ctx = make_ctx(
        "manual",
        launch_root=tmp_path,
        config={"security.command_moderation.enabled": False},
        answers=["deny_once"],
    )
    out = json.loads(_exec_tool()(command="pip install x", ctx=ctx))
    assert out["error"]["type"] == "declined_by_user"


def test_user_extension_of_the_harmless_list(tmp_path):
    judge = _judge("potentially_dangerous")
    ctx = make_ctx(
        "manual",
        launch_root=tmp_path,
        config={"security.permissions.readonly_commands": ["rg"]},
        answers=[],
    )
    assert _exec_tool(judge=judge)(command="rg TODO", ctx=ctx) == "executed"
    assert ctx.asked == []


def test_remembered_command_denial_holds_after_switching_to_edit(tmp_path):
    judge = _judge("safe")
    ctx = make_ctx("manual", launch_root=tmp_path, answers=["deny_remember"])
    tool = _exec_tool(judge=judge)
    tool(command="pip install x", ctx=ctx)
    ctx.shared.permission_mode = "edit"
    out = json.loads(tool(command="pip install x", ctx=ctx))
    assert out["error"]["type"] == "denied_by_user_for_session"
    assert judge.calls == 0  # судью не спрашивали: запрет человека первее
    assert audit(ctx)[-1] == ("blocked", "session_rule_deny")
    # другая команда идёт обычным путём
    assert tool(command="pip install y", ctx=ctx) == "executed"
    assert judge.calls == 1


# --- пол защищённых путей на командах ---------------------------------------------


def _guarded_command(tmp_path, monkeypatch, answer, command):
    from gc_core.config_modules import env_vars
    from gc_core.security.command_guard import enforce_command_guard

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    (tmp_path / "AGENTS.md").write_text("instructions", encoding="utf-8")
    ctx = make_ctx("edit", launch_root=tmp_path, answers=[answer])

    @enforce_command_guard(payload_arg="command", tool_name="exec_command")
    def run(command: str, ctx=None) -> str:
        return "executed"

    return run(command=command.format(root=tmp_path), ctx=ctx), ctx


def test_command_floor_audits_a_confirmed_question(tmp_path, monkeypatch):
    """«Да» — строка `confirmed`: дальше команду ещё проверит модерация команд
    (вопрос был только про защищённый путь), итог исполнения пишет она."""
    out, ctx = _guarded_command(
        tmp_path, monkeypatch, "allow_once", "cat {root}/AGENTS.md"
    )
    assert out == "executed"
    assert audit(ctx) == [("confirmed", "protected_ask_confirmed")]
    assert ctx.logger.events[0]["risk_flags"] == ["touches_protected_paths"]


def test_command_floor_audits_a_declined_question(tmp_path, monkeypatch):
    out, ctx = _guarded_command(
        tmp_path, monkeypatch, "deny_once", "cat {root}/AGENTS.md"
    )
    assert json.loads(out)["ok"] is False
    assert audit(ctx) == [("blocked", "protected_ask_cancelled")]


def test_command_floor_audits_a_hard_denial(tmp_path, monkeypatch):
    out, ctx = _guarded_command(
        tmp_path, monkeypatch, "allow_once", "cat {root}/.gc/config.yml"
    )
    assert json.loads(out)["ok"] is False
    assert ctx.asked == []
    assert audit(ctx) == [("blocked", "protected_deny")]


# --- plan: запреты — пол -------------------------------------------------------


def test_plan_does_not_ask_about_a_protected_path_it_will_deny_anyway(
    tmp_path, monkeypatch
):
    """Вопрос пола в plan бессмыслен: запись и команду режим всё равно
    запретит, что бы человек ни ответил. Решает режим, без вопроса."""
    from gc_core.config_modules import env_vars
    from gc_core.security.command_guard import enforce_command_guard

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    (tmp_path / "AGENTS.md").write_text("instructions", encoding="utf-8")
    ctx = make_ctx("plan", launch_root=tmp_path, answers=["allow_remember"])
    out = json.loads(_fake_write(path=str(tmp_path / "AGENTS.md"), ctx=ctx))
    assert out["error"]["type"] == "write_denied_by_mode"
    assert ctx.asked == []

    @enforce_command_guard(payload_arg="command", tool_name="exec_command")
    def run(command: str, ctx=None) -> str:
        return "executed"

    run(command=f"sed -i s/a/b/ {tmp_path}/AGENTS.md", ctx=ctx)
    assert ctx.asked == []
    assert ctx.permission_store.session_rules() == {}


def test_plan_denies_writes_inside_and_outside_without_asking(tmp_path):
    ctx = make_ctx(
        "plan", launch_root=tmp_path / "p", answers=["allow_remember"]
    )
    (tmp_path / "p").mkdir()
    for path in (tmp_path / "p" / "a.txt", tmp_path / "out.txt"):
        out = json.loads(_fake_write(path=str(path), ctx=ctx))
        assert out["error"]["type"] == "write_denied_by_mode"
    assert ctx.asked == []
    assert audit(ctx) == [("blocked", "mode_deny")] * 2


def test_plan_floor_survives_a_remembered_allow_and_an_explicit_guard_mode(
    tmp_path,
):
    """Пол: ни «разрешить в сессии», данное раньше в manual, ни явный
    `write_guard.mode: allow` запись в plan не открывают."""
    ctx = make_ctx(
        "manual",
        launch_root=tmp_path,
        config={"security.write_guard.mode": "allow"},
        answers=["allow_remember"],
    )
    path = str(tmp_path / "a.txt")
    assert _fake_write(path=path, ctx=ctx) == "written"
    ctx.shared.permission_mode = "plan"
    assert json.loads(_fake_write(path=path, ctx=ctx))["error"]["type"] == (
        "write_denied_by_mode"
    )
    outside = str(tmp_path.parent / "elsewhere.txt")
    assert json.loads(_fake_write(path=outside, ctx=ctx))["error"]["type"] == (
        "write_denied_by_mode"
    )


def test_plan_denies_commands_but_runs_harmless_ones(tmp_path):
    judge = _judge("safe")
    ctx = make_ctx("plan", launch_root=tmp_path, answers=["allow_remember"])
    out = json.loads(_exec_tool(judge=judge)(command="pip install x", ctx=ctx))
    assert out["error"]["type"] == "exec_denied_by_mode"
    assert judge.calls == 0 and ctx.asked == []
    assert _exec_tool(judge=judge)(command="pwd", ctx=ctx) == "executed"
    assert judge.calls == 0 and ctx.asked == []


def test_plan_reads_inside_the_project_without_asking(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    ctx = make_ctx("plan", launch_root=tmp_path, answers=[])
    assert "error" not in str(_read_tool()(path=str(target), ctx=ctx))
    assert ctx.asked == []


def test_plan_denials_tell_the_model_to_propose_a_plan(tmp_path):
    """Иначе агент, получив запрет на запись, советует сменить режим."""
    ctx = make_ctx("plan", launch_root=tmp_path, answers=[])
    write = json.loads(_fake_write(path=str(tmp_path / "a.txt"), ctx=ctx))
    run = json.loads(
        _exec_tool(judge=_judge("safe"))(command="pip install x", ctx=ctx)
    )
    for error in (write["error"], run["error"]):
        assert "turn_propose_plan" in error["guidance"]
        assert "turn_propose_plan" not in error["reason"]


def test_plan_denial_tells_a_subagent_to_return_findings_not_propose_a_plan(
    tmp_path,
):
    """`turn_propose_plan` субагенту не предлагается и отвечает ему отказом:
    совет звать его тратил бы шаги. Субагенту — вернуть выводы текстом."""
    from types import SimpleNamespace

    from gc_core.security.call_scope import call_scope

    ctx = make_ctx("plan", launch_root=tmp_path, answers=[])
    child = SimpleNamespace(parent_state_id="root")
    with call_scope(state=child, call_id="c1", tool_name="fs_write_file"):
        write = json.loads(_fake_write(path=str(tmp_path / "a.txt"), ctx=ctx))
    guidance = write["error"]["guidance"]
    assert "turn_propose_plan" not in guidance
    assert "findings" in guidance


@pytest.mark.parametrize("mode", ["edit", "manual", "plan", "auto"])
def test_harmless_command_with_environment_variables_is_not_harmless(
    tmp_path, mode
):
    """Окружение меняет то, что исполнит «безобидная» команда (`LD_PRELOAD`,
    `GIT_CONFIG_*`, `BASH_ENV`): с переменными окружения команда идёт обычным
    путём режима — вопрос, судья или запрет, — а не мимо всех."""
    judge = _judge("safe")
    ctx = make_ctx(mode, launch_root=tmp_path, answers=[])
    env = [{"name": "LD_PRELOAD", "value": "/tmp/evil.so"}]
    out = _exec_tool(judge=judge)(command="pwd", ctx=ctx, env_vars=env)
    passed_silently = (
        out == "executed" and judge.calls == 0 and ctx.asked == []
    )
    assert not passed_silently


def test_git_in_another_directory_is_not_harmless(tmp_path):
    """Как у эталона: git с явным каталогом исполнения исполнит конфиг и хуки
    того репозитория — безобидным не считается, как бы ни было записано имя
    команды. `ls` в другом каталоге безобиден по-прежнему."""
    judge = _judge("safe")
    ctx = make_ctx("manual", launch_root=tmp_path, answers=["allow_once"] * 5)
    assert _exec_tool(judge=judge)(command="git status", ctx=ctx) == "executed"
    assert ctx.asked == []
    other = str(tmp_path.parent)
    # разные аргументы — разные ключи: разовый ответ мимо диспетчера не тянется
    for i, cmd in enumerate(
        ["git status", "'git' status -s", '"git" status -b', "\\git status -u"]
    ):
        _exec_tool(judge=judge)(command=cmd, ctx=ctx, cwd=other)
        assert len(ctx.asked) == i + 1, cmd
    _exec_tool(judge=judge)(command="ls", ctx=ctx, cwd=other)
    assert len(ctx.asked) == 4
    # модели часто передают cwd, равный корню проекта, — это тот же каталог
    for i, root_cwd in enumerate((str(tmp_path), ".", str(tmp_path) + "/")):
        out = _exec_tool(judge=judge)(
            command=f"git status -{'sbz'[i]}", ctx=ctx, cwd=root_cwd
        )
        assert out == "executed", root_cwd
    assert len(ctx.asked) == 4


def test_declined_git_write_is_not_called_an_instruction_file(
    tmp_path, monkeypatch
):
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    ctx = make_ctx("edit", launch_root=tmp_path, answers=["deny_once"])
    out = json.loads(
        _fake_write(path=str(tmp_path / ".git" / "config"), ctx=ctx)
    )
    assert out["error"]["type"] == "protected_path_denied"
    assert "instruction" not in out["error"]["reason"]


def test_floor_decision_follows_the_outcomes_of_its_own_action():
    """Пол решает по исходам режима для СВОЕГО действия (запись — внутри и
    снаружи, команда — её исход): судья, если режим отдаёт действие ему;
    никто, если режим запретит сам; иначе человек."""
    from gc_core.security import permission_mode as pm

    assert pm.floor_question_decision((pm.DENY, pm.DENY)) == pm.PASS
    assert pm.floor_question_decision((pm.ALLOW, pm.ALIGN)) == pm.ALIGN
    assert pm.floor_question_decision((pm.ASK, pm.ASK)) == pm.ASK
    assert pm.floor_question_decision((pm.DENY, pm.ASK)) == pm.ASK
    assert pm.floor_question_decision((pm.JUDGE,)) == pm.ASK
