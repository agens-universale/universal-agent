"""Режим auto в резолвере: где решает судья действий, а где — настройки и полы."""

from types import SimpleNamespace

import pytest
from gc_core.security import permission_mode
from gc_core.security.permission_mode import (
    ALIGN,
    ALLOW,
    CAT_MCP,
    CAT_SKILL,
    CAT_SKILL_EXEC,
    CAT_STATE,
    CAT_SUBAGENT,
    CAT_TURN,
    CAT_UNKNOWN,
    DENY,
    Mode,
    category_decision,
    exec_decision,
    guard_mode_key,
    read_decision,
    write_decision,
)
from gc_testkit.settings import fake_ctx


@pytest.fixture(autouse=True)
def _auto_assembled(monkeypatch):
    """Режим включается последней задачей этапа; до неё собираем его в тесте."""
    monkeypatch.setattr(
        permission_mode,
        "AVAILABLE_MODES",
        (Mode.EDIT, Mode.MANUAL, Mode.PLAN, Mode.AUTO),
    )


def _ctx(mode="auto", config=None):
    store = SimpleNamespace(permission_mode=lambda: mode, session_rules=dict)
    return fake_ctx(config=config, permission_store=store)


def test_work_inside_the_project_needs_no_judge():
    ctx = _ctx()
    assert write_decision(ctx, inside=True) == ALLOW
    assert read_decision(ctx, inside=True) == ALLOW
    assert exec_decision(ctx, readonly=True) == ALLOW


def test_what_other_modes_ask_about_goes_to_the_judge():
    ctx = _ctx()
    assert write_decision(ctx, inside=False) == ALIGN
    assert read_decision(ctx, inside=False) == ALIGN
    assert exec_decision(ctx, readonly=False) == ALIGN


@pytest.mark.parametrize(
    "category", [CAT_MCP, CAT_SKILL_EXEC, CAT_SUBAGENT, CAT_UNKNOWN]
)
def test_actions_edit_lets_through_silently_are_judged(category):
    assert category_decision(category, _ctx()) == ALIGN
    assert category_decision(category, _ctx(mode="edit")) == ALLOW


@pytest.mark.parametrize("category", [CAT_SKILL, CAT_STATE, CAT_TURN])
def test_harmless_categories_stay_allowed(category):
    assert category_decision(category, _ctx()) == ALLOW


def test_subagent_launch_is_not_asked_about_in_manual():
    """В manual спрашиваются действия самого субагента, а не его запуск."""
    assert category_decision(CAT_SUBAGENT, _ctx(mode="manual")) == ALLOW
    assert category_decision(CAT_SUBAGENT, _ctx(mode="plan")) == ALLOW


@pytest.mark.parametrize("guard", ["write_guard", "read_guard"])
def test_shipped_ask_does_not_replace_the_judge(guard):
    """`mode: ask` стоит в поставляемом конфиге: подставь его как исход — и
    auto вёл бы себя как manual в каждой установке."""
    decide = write_decision if guard == "write_guard" else read_decision
    ctx = _ctx(config={guard_mode_key(guard): "ask"})
    assert decide(ctx, inside=False) == ALIGN


@pytest.mark.parametrize("guard", ["write_guard", "read_guard"])
def test_explicit_allow_and_deny_are_stronger_than_the_judge(guard):
    decide = write_decision if guard == "write_guard" else read_decision
    assert (
        decide(_ctx(config={guard_mode_key(guard): "allow"}), inside=False)
        == ALLOW
    )
    assert (
        decide(_ctx(config={guard_mode_key(guard): "deny"}), inside=False)
        == DENY
    )


def test_explicit_allow_for_commands_switches_the_judge_off():
    """Осознанное ослабление — как `write_guard.mode: allow` для записи вне
    проекта. `ask` и `deny` у команд — политика эскалации судьи команд, а не
    исход: в auto решает судья действий."""
    assert (
        exec_decision(
            _ctx(config={"security.command_moderation.mode": "allow"}),
            readonly=False,
        )
        == ALLOW
    )
    for value in ("ask", "deny"):
        ctx = _ctx(config={"security.command_moderation.mode": value})
        assert exec_decision(ctx, readonly=False) == ALIGN


def test_command_moderation_allow_changes_nothing_outside_auto():
    ctx = _ctx(
        mode="edit", config={"security.command_moderation.mode": "allow"}
    )
    assert exec_decision(ctx, readonly=False) == permission_mode.JUDGE


def test_auto_is_not_selectable_until_assembled(monkeypatch):
    monkeypatch.setattr(
        permission_mode, "AVAILABLE_MODES", (Mode.EDIT, Mode.MANUAL, Mode.PLAN)
    )
    assert permission_mode.coerce_mode("auto") is None
    assert permission_mode.resolve_mode(_ctx()) is Mode.EDIT
