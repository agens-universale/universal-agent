"""Резолвер режимов разрешений: слои, таблицы исходов, ключи решений."""

from types import SimpleNamespace

import pytest
from gc_core.message_policy.permission_denials import (
    session_denied_error,
)
from gc_core.security.permission_mode import (
    ALLOW,
    ASK,
    AVAILABLE_MODES,
    CAT_EDIT,
    CAT_MCP,
    CAT_SKILL,
    CAT_SKILL_EXEC,
    CAT_TURN,
    CAT_UNKNOWN,
    DEFAULT_MODE,
    DENY,
    JUDGE,
    MODE_LABELS,
    PASS,
    Mode,
    category_decision,
    coerce_mode,
    decision_identity,
    decision_path_key,
    exec_decision,
    gate_decision_key,
    guard_mode_key,
    mode_label,
    next_mode,
    read_decision,
    resolve_mode,
    resolve_mode_with_source,
    session_deny,
    write_decision,
)
from gc_testkit.settings import fake_ctx


def _ctx(*, session_mode=None, config=None, session_rules=None):
    store = SimpleNamespace(
        permission_mode=lambda: session_mode,
        session_rules=lambda: dict(session_rules or {}),
    )
    return fake_ctx(config=config, permission_store=store)


def test_default_mode_is_edit():
    assert DEFAULT_MODE is Mode.EDIT
    assert resolve_mode(_ctx()) is Mode.EDIT


def test_source_is_session_config_default():
    assert resolve_mode_with_source(_ctx(session_mode="manual")) == (
        Mode.MANUAL,
        "session",
    )
    cfg = {"security.permissions.default_mode": "manual"}
    assert resolve_mode_with_source(_ctx(config=cfg)) == (
        Mode.MANUAL,
        "config",
    )
    assert resolve_mode_with_source(_ctx()) == (Mode.EDIT, "default")


def test_session_choice_beats_config():
    cfg = {"security.permissions.default_mode": "manual"}
    ctx = _ctx(session_mode="edit", config=cfg)
    assert resolve_mode_with_source(ctx) == (Mode.EDIT, "session")


def test_context_without_store_falls_back_to_config():
    ctx = fake_ctx(config={"security.permissions.default_mode": "manual"})
    assert resolve_mode(ctx) is Mode.MANUAL


def test_unknown_mode_is_not_selectable():
    assert coerce_mode("manual") is Mode.MANUAL
    assert coerce_mode(Mode.EDIT) is Mode.EDIT
    assert coerce_mode("auto") is Mode.AUTO
    assert coerce_mode("bypass") is None
    assert coerce_mode("нет такого") is None
    assert coerce_mode(None) is None
    # незнакомое значение в сессии не включается молча — действует конфиг
    assert resolve_mode_with_source(_ctx(session_mode="bypass")) == (
        Mode.EDIT,
        "default",
    )


def test_cycle_covers_available_modes():
    assert next_mode(Mode.EDIT) is Mode.MANUAL
    assert next_mode(Mode.MANUAL) is Mode.PLAN
    assert next_mode(Mode.PLAN) is Mode.AUTO
    assert next_mode(Mode.AUTO) is Mode.EDIT


def test_every_available_mode_has_a_label_and_table_rows():
    """Сторож полноты: собранный режим без подписи или без строки таблицы —
    KeyError на живом вызове защиты."""
    for mode in AVAILABLE_MODES:
        assert MODE_LABELS[mode.value]
        ctx = _ctx(session_mode=mode.value)
        write_decision(ctx, inside=True)
        read_decision(ctx, inside=False)
        exec_decision(ctx, readonly=False)
        for category in (CAT_MCP, CAT_SKILL, CAT_SKILL_EXEC, CAT_UNKNOWN):
            category_decision(category, ctx)
    assert mode_label("edit") == "edit mode"
    assert mode_label(Mode.MANUAL) == "manual mode"


@pytest.mark.parametrize(
    "mode,inside,expected",
    [
        ("edit", True, ALLOW),
        # Вне периметра режим по умолчанию ничего не добавляет: решает ветка
        # нарушения периметра самой защиты (её ключ mode, её конверт).
        ("edit", False, PASS),
        ("manual", True, ASK),
        # manual строже защиты и решает сам — в том числе когда она выключена.
        ("manual", False, ASK),
    ],
)
def test_write_decision_by_mode(mode, inside, expected):
    assert write_decision(_ctx(session_mode=mode), inside=inside) == expected


def test_read_decision_by_mode():
    assert read_decision(_ctx(session_mode="manual"), inside=True) == ALLOW
    assert read_decision(_ctx(session_mode="manual"), inside=False) == ASK
    assert read_decision(_ctx(), inside=False) == PASS


def test_exec_decision_whitelist_and_modes():
    assert exec_decision(_ctx(session_mode="manual"), readonly=True) == ALLOW
    assert exec_decision(_ctx(session_mode="manual"), readonly=False) == ASK
    # edit оставляет существующего судью команд
    assert exec_decision(_ctx(), readonly=False) == JUDGE


def test_explicit_config_rule_beats_mode_outside_perimeter():
    """Слой 2 сильнее дефолта режима: явный `write_guard.mode` отменяет вопрос,
    который manual задал бы вне периметра."""
    cfg = {"security.write_guard.mode": "deny"}
    ctx = _ctx(session_mode="manual", config=cfg)
    assert write_decision(ctx, inside=False) == DENY
    # внутри периметра явный ключ (он про нарушение периметра) не действует
    assert write_decision(ctx, inside=True) == ASK


def test_explicit_allow_does_not_remove_the_question_manual_asks():
    """Manual выбран в сессии сознательно: давнее `allow` в конфиге не снимает
    его вопрос — иначе запись вне проекта шла бы молча, а внутри — с вопросом.
    `deny` сильнее по-прежнему; в auto `allow` заменяет судью (там это осознанное
    ослабление проверки, а не вопроса человеку)."""
    for guard, decide in (
        ("write_guard", write_decision),
        ("read_guard", read_decision),
    ):
        cfg = {guard_mode_key(guard): "allow"}
        assert (
            decide(_ctx(session_mode="manual", config=cfg), inside=False)
            == ASK
        ), guard
    cfg = {"security.write_guard.mode": "allow"}
    assert (
        write_decision(_ctx(session_mode="auto", config=cfg), inside=False)
        == ALLOW
    )
    # только manual: в plan явный allow по-прежнему открывает чтение вне проекта
    cfg = {"security.read_guard.mode": "allow"}
    assert (
        read_decision(_ctx(session_mode="plan", config=cfg), inside=False)
        == ALLOW
    )


def test_default_guard_mode_is_not_an_explicit_rule():
    """Умолчание схемы — не слово пользователя: manual вне периметра спрашивает."""
    assert write_decision(_ctx(session_mode="manual"), inside=False) == ASK


def test_explicit_config_rule_stays_with_the_guard_in_edit():
    """Там, где режим отдал зону защите (PASS), явный ключ применяет она сама."""
    cfg = {"security.write_guard.mode": "deny"}
    assert write_decision(_ctx(config=cfg), inside=False) == PASS


def test_category_decisions():
    manual = _ctx(session_mode="manual")
    edit = _ctx()
    assert category_decision(CAT_MCP, manual) == ASK
    assert category_decision(CAT_MCP, edit) == ALLOW
    assert category_decision(CAT_SKILL_EXEC, manual) == ASK
    assert category_decision(CAT_SKILL_EXEC, edit) == ALLOW
    assert category_decision(CAT_SKILL, manual) == ALLOW
    assert category_decision(CAT_TURN, manual) == ALLOW
    # неразмеченный тул: в edit — прежнее поведение, в manual — спросить
    assert category_decision(CAT_UNKNOWN, manual) == ASK
    assert category_decision(CAT_UNKNOWN, edit) == ALLOW
    assert category_decision("такой категории нет", manual) == ASK
    assert category_decision("такой категории нет", edit) == ALLOW
    # у категорий со своей защитой гейт не решает
    assert category_decision(CAT_EDIT, manual) == PASS


# --- запомненный запрет ---------------------------------------------------------


def test_session_deny_reads_only_remembered_denials():
    ctx = _ctx(session_rules={"write:/p/a.py": False, "write:/p/b.py": True})
    assert session_deny(ctx, "write:/p/a.py") is True
    assert (
        session_deny(ctx, "write:/p/b.py") is False
    )  # разрешение — не запрет
    assert session_deny(ctx, "write:/p/c.py") is False
    assert session_deny(ctx, None) is False
    assert session_deny(fake_ctx(), "write:/p/a.py") is False  # стора нет


def test_one_time_denial_is_not_a_session_denial():
    """T («запретить разово») пишется в run-правила и на другие вызовы и
    режимы не переносится: `session_deny` его не видит."""
    from gc_core.security.permission_store import PermissionStore

    store = PermissionStore()
    store.record("write:/p/a.py", False, session=False)
    store.record("write:/p/b.py", False, session=True)
    ctx = fake_ctx(permission_store=store)
    assert session_deny(ctx, "write:/p/a.py") is False
    assert session_deny(ctx, "write:/p/b.py") is True


# --- ключи решений ---------------------------------------------------------------


def test_path_key_is_the_same_for_different_spellings(tmp_path):
    base = str(tmp_path)
    key = decision_path_key("write", str(tmp_path / "a.py"), base)
    assert decision_path_key("write", "a.py", base) == key
    assert decision_path_key("write", "./a.py", base) == key
    assert decision_path_key("write", "sub/../a.py", base) == key
    assert decision_path_key("read", "a.py", base) != key
    assert decision_path_key("write", "b.py", base) != key


def test_path_key_survives_empty_path_and_missing_base():
    assert decision_path_key("write", "", "/p") == "write:"
    assert decision_path_key("write", "a.py", None).startswith("write:")


def test_gate_key_for_mcp_is_per_tool_not_per_call():
    entry = {"category": CAT_MCP, "mcp_server": "ddgs"}
    assert gate_decision_key("search", entry, {"q": "a"}) == "mcp:ddgs:search"
    assert gate_decision_key("search", entry, {"q": "b"}) == "mcp:ddgs:search"
    assert gate_decision_key("search", {"category": CAT_MCP}) == "mcp:search"


def test_gate_key_uses_declared_decision_args():
    entry = {"category": CAT_SKILL_EXEC, "decision_args": ("skill", "script")}
    args = {"skill": "pdf", "script": "build.py", "argv": ["x"]}
    assert gate_decision_key("skills_run_script", entry, args) == (
        "skill_exec:skills_run_script:pdf:build.py"
    )
    assert decision_identity(entry, '{"skill": "pdf"}') == [
        ("skill", "pdf"),
        ("script", "?"),
    ]
    assert gate_decision_key("t", {"category": CAT_UNKNOWN}) == "unknown:t"


# --- конверты отказа -------------------------------------------------------------


def test_session_denied_envelope_names_the_user_as_the_source():
    error = session_denied_error(path="/p/a.py")
    assert error["type"] == "denied_by_user_for_session"
    assert error["path"] == "/p/a.py"
    assert error["reason"] == "Denied by the user for this session."
    assert "Do NOT retry" in error["guidance"]
    assert "ask the user" in error["guidance"]


def test_refusals_forbid_retrying_on_initiative_but_not_on_request():
    """Запрет повтора — про инициативу модели. Человек мог передумать или
    сменить режим: на его прямую просьбу модель обязана вызвать тул снова
    (живая приёмка: после разового «нет» модель отказывалась сама)."""
    from gc_core.message_policy import permission_denials as pm

    for error in (
        pm.declined_error(),
        pm.unattended_error(),
        pm.session_denied_error(),
        pm.mode_denied_error("x", "y"),
    ):
        assert "Do NOT retry the same call on your own" in error["guidance"]
        assert pm.RETRY_ON_REQUEST in error["guidance"]


def test_path_decision_key_expands_home_and_variables(monkeypatch):
    """Ключ запомненного ответа о пути не зависит от написания: `~/.ssh/id_rsa`
    и `$HOME/.ssh/id_rsa` — тот же файл, что `/home/u/.ssh/id_rsa`, и запрет
    «в сессии» на одно написание действует на все (как было на develop)."""
    from gc_core.security.permission_mode import decision_path_key

    monkeypatch.setenv("HOME", "/home/u")
    plain = decision_path_key("read", "/home/u/.ssh/id_rsa", "/proj")
    assert decision_path_key("read", "~/.ssh/id_rsa", "/proj") == plain
    assert decision_path_key("read", "$HOME/.ssh/id_rsa", "/proj") == plain
    assert decision_path_key("read", "${HOME}/.ssh/id_rsa", "/proj") == plain


def test_plan_executes_in_every_mode_except_plan_in_the_offered_order():
    """Один список на TUI и консоль: номера вариантов одобрения идут отсюда."""
    from gc_core.security.permission_mode import PLAN_EXECUTION_MODES

    assert PLAN_EXECUTION_MODES == (Mode.EDIT, Mode.AUTO, Mode.MANUAL)
    assert set(PLAN_EXECUTION_MODES) == set(AVAILABLE_MODES) - {Mode.PLAN}
