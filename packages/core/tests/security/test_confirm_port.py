import types

from gc_core.security import confirm_port as cp
from gc_core.security.confirm_port import confirm_available, request_confirm
from gc_core.security.permission_store import PermissionStore
from gc_core.settings import RuntimeEnv
from gc_testkit.settings import fake_ctx, make_settings


def _ctx(**kw):
    c = fake_ctx(is_tui=False, tui_confirm=None)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_unavailable_in_headless_denies(monkeypatch):
    monkeypatch.setattr(
        "sys.stdin", types.SimpleNamespace(isatty=lambda: False)
    )
    ctx = _ctx()
    assert confirm_available(ctx) is False
    assert (
        request_confirm(title="t", question="q?", fields=[], ctx=ctx) is False
    )


def test_tui_always_available_and_routes_to_banner():
    answered = {}

    def banner(**kw):
        answered.update(kw)
        return "once"

    ctx = _ctx(is_tui=True, tui_confirm=banner)
    assert confirm_available(ctx) is True
    assert (
        request_confirm(title="t", question="q?", fields=[("k", "v")], ctx=ctx)
        is True
    )
    assert answered["question"] == "q?"


def test_cli_disabled_when_ask_human_in_stop_tools(monkeypatch):
    monkeypatch.setattr(
        "sys.stdin", types.SimpleNamespace(isatty=lambda: True)
    )
    ctx = _ctx(
        settings=make_settings(
            {"agent.stop_tools": ["turn_ask_human"]}
        ).settings
    )
    assert confirm_available(ctx) is False


def _tui_ctx(choice, store=None):
    # is_tui ctx с tui_confirm, возвращающим заданный choice; счётчик вызовов.
    calls = {"n": 0}

    def tui_confirm(*, title, question, fields):
        calls["n"] += 1
        return choice

    ctx = fake_ctx(
        is_tui=True,
        tui_confirm=tui_confirm,
        permission_store=store,
    )
    return ctx, calls


def test_choice_once_allows_and_records_run_only():
    store = PermissionStore()
    ctx, calls = _tui_ctx("once", store)
    assert (
        cp.request_confirm(
            title="t",
            question="q",
            fields=[],
            ctx=ctx,
            decision_key="write:/x",
        )
        is True
    )
    assert calls["n"] == 1
    assert store.session_rules() == {}  # once не в session
    # повтор того же ключа — без нового вопроса (run-кэш)
    assert (
        cp.request_confirm(
            title="t",
            question="q",
            fields=[],
            ctx=ctx,
            decision_key="write:/x",
        )
        is True
    )
    assert calls["n"] == 1


def test_choice_session_allows_and_records_session():
    store = PermissionStore()
    ctx, calls = _tui_ctx("session", store)
    assert (
        cp.request_confirm(
            title="t",
            question="q",
            fields=[],
            ctx=ctx,
            decision_key="write:/x",
        )
        is True
    )
    assert store.session_rules() == {"write:/x": True}


def test_choice_deny_blocks_and_caches_run():
    store = PermissionStore()
    ctx, calls = _tui_ctx("deny", store)
    assert (
        cp.request_confirm(
            title="t",
            question="q",
            fields=[],
            ctx=ctx,
            decision_key="write:/x",
        )
        is False
    )
    # повтор — без нового вопроса, всё ещё deny (anti-loop)
    assert (
        cp.request_confirm(
            title="t",
            question="q",
            fields=[],
            ctx=ctx,
            decision_key="write:/x",
        )
        is False
    )
    assert calls["n"] == 1


def test_no_decision_key_always_asks():
    store = PermissionStore()
    ctx, calls = _tui_ctx("once", store)
    cp.request_confirm(title="t", question="q", fields=[], ctx=ctx)
    cp.request_confirm(title="t", question="q", fields=[], ctx=ctx)
    assert calls["n"] == 2  # без ключа — кэш не работает


def test_headless_fail_closed_without_tty(monkeypatch):
    monkeypatch.setattr(
        cp.sys, "stdin", types.SimpleNamespace(isatty=lambda: False)
    )
    store = PermissionStore()
    ctx = fake_ctx(permission_store=store)
    assert (
        cp.request_confirm(
            title="t",
            question="q",
            fields=[],
            ctx=ctx,
            decision_key="write:/x",
        )
        is False
    )
    # unavailable → deny_once: run cache only, no session / few-shot poison
    assert store.session_rules() == {}
    assert store.decision_for("write:/x") is False


def test_invalid_tui_confirm_return_maps_to_deny():
    # tui_confirm, возвращающий невалидное значение, должен приводить к deny (не allow).
    for bad_choice in ("yes", None, "", "ok", 1):
        ctx, _ = _tui_ctx(bad_choice)
        result = cp.request_confirm(
            title="t",
            question="q",
            fields=[],
            ctx=ctx,
            decision_key="write:/x",
        )
        assert result is False, (
            f"bad choice {bad_choice!r} должен давать deny, но вернул {result}"
        )


def test_choice_hint_matches_the_kind_of_question():
    """Консольная подсказка, как и баннер: «и подобные» — только там, где
    ответ уйдёт судье команд, то есть под ключом `command:`."""
    from gc_core.security.confirm_port import (
        CONFIRM_CHOICE_HINT,
        CONFIRM_CHOICE_HINT_PLAIN,
        confirm_choice_hint,
    )
    from gc_core.security.permission_store import (
        PermissionStore,
        is_command_key,
    )

    assert is_command_key("command:ls") is True
    assert is_command_key("write:/p/a.py") is False
    assert is_command_key(None) is False
    assert confirm_choice_hint("command:ls") == CONFIRM_CHOICE_HINT
    assert confirm_choice_hint("write:/p/a.py") == CONFIRM_CHOICE_HINT_PLAIN
    assert confirm_choice_hint(None) == CONFIRM_CHOICE_HINT_PLAIN
    assert "подобные" not in CONFIRM_CHOICE_HINT_PLAIN
    for key in ("[Y/Н]", "[O/Щ]", "[N/Т]", "[T/Е]"):
        assert key in CONFIRM_CHOICE_HINT and key in CONFIRM_CHOICE_HINT_PLAIN

    # одно правило на подпись и на механизм: судье уходят ровно те ответы,
    # про которые подпись говорит «и подобные»
    store = PermissionStore()
    store.record("command:pip install x", True, session=True)
    store.record("write:/p/a.py", True, session=True)
    assert store.insession_examples()["allowed"] == ["pip install x"]


def test_presenter_gets_only_the_optional_parameters_it_accepts():
    """Показ вопроса идёт через несколько звеньев с разными сигнатурами:
    новый необязательный параметр не должен ломать короткие реализации."""
    from gc_core.security.confirm_port import call_with_optional

    assert call_with_optional(
        lambda t, q: (t, q), 1, 2, via="v", decision_key="k"
    ) == (1, 2)
    assert call_with_optional(
        lambda t, via=None: (t, via), 1, via="v", decision_key="k"
    ) == (1, "v")
    assert call_with_optional(lambda *a: a, 1, 2, decision_key="k") == (1, 2)
    assert call_with_optional(
        lambda t, **kw: kw, 1, via="v", decision_key="k"
    ) == {
        "via": "v",
        "decision_key": "k",
    }


def test_decision_key_reaches_the_tui_presenter():
    """Ключ решения доезжает до показа вопроса: по нему баннер выбирает подписи."""
    from gc_core.security.confirm_port import request_confirm

    seen = {}

    def tui_confirm(*, title, question, fields, via=None, decision_key=None):
        seen["decision_key"] = decision_key
        return "allow_once"

    ctx = _ctx(is_tui=True, tui_confirm=tui_confirm)
    assert request_confirm(
        title="t",
        question="q?",
        fields=[],
        ctx=ctx,
        decision_key="write:/p/a.py",
    )
    assert seen["decision_key"] == "write:/p/a.py"


def test_declared_channel_wins_over_the_autodetection(monkeypatch):
    """Канал объявил человека — так и есть, что бы ни нашла автопроверка:
    ACP без терминала и без turn_ask_human — с человеком; сервер и A2A с
    терминалом, портом или признаком TUI — без."""
    monkeypatch.setattr(
        "sys.stdin", types.SimpleNamespace(isatty=lambda: True)
    )
    without_human = fake_ctx(
        runtime=RuntimeEnv(human_channel=False),
        is_tui=True,
        human_request=lambda request: None,
    )
    assert confirm_available(without_human) is False
    monkeypatch.setattr(
        "sys.stdin", types.SimpleNamespace(isatty=lambda: False)
    )
    with_human = fake_ctx(
        {"agent.stop_tools": ["turn_ask_human"]},
        runtime=RuntimeEnv(human_channel=True),
    )
    assert confirm_available(with_human) is True


def test_undeclared_channel_keeps_the_autodetection(monkeypatch):
    """`gc-cli` не объявляет канал: человек есть, только если есть терминал."""
    monkeypatch.setattr(
        "sys.stdin", types.SimpleNamespace(isatty=lambda: True)
    )
    assert confirm_available(fake_ctx()) is True
    monkeypatch.setattr(
        "sys.stdin", types.SimpleNamespace(isatty=lambda: False)
    )
    assert confirm_available(fake_ctx()) is False


def test_declared_no_human_does_not_read_the_terminal(monkeypatch):
    """Без человека вопрос сразу отказ: терминал процесса не читается."""
    monkeypatch.setattr(
        "sys.stdin", types.SimpleNamespace(isatty=lambda: True)
    )

    def terminal(**_kwargs):
        raise AssertionError("stdin")

    monkeypatch.setattr(
        "gc_core.security.path_policy.prompt_user_confirmation_choice",
        terminal,
    )
    ctx = fake_ctx(
        runtime=RuntimeEnv(human_channel=False), is_tui=False, tui_confirm=None
    )
    assert (
        request_confirm(title="t", question="q?", fields=[], ctx=ctx) is False
    )
