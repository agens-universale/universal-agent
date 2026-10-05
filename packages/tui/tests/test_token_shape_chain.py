"""Цепочка: проба → поле/конфиг as-is → build_call_headers шлёт как записано."""

from types import SimpleNamespace

import pytest
from gc_core import onboarding_config_writer as ocw
from gc_core.utils.connect_backend import build_call_headers
from gc_tui.services.settings_probe import ProbeResult, probe_connection

pytest.importorskip("textual")


class _StateStub:
    state_id = "state-1"
    x_target = None


def test_probe_accepts_naked_key_and_reports_accepted_auth():
    """Сервер берёт голый ключ — accepted_auth = та же строка, без Bearer."""

    def fake(
        endpoint, auth, verify, model, *, config_proxy=None, post_ex=None
    ):
        if auth is None:
            return ProbeResult(
                "rejected", 401, None, 5, "токен отклонён (401)"
            )
        if auth == "sk-naked":
            return ProbeResult("ok", 200, None, 5, "доступен")
        return ProbeResult("rejected", 401, None, 5, "токен отклонён (401)")

    result = probe_connection(
        "http://x/v1/chat/completions",
        "sk-naked",
        True,
        "",
        probe_fn=fake,
    )
    assert result.auth_state == "accepted"
    assert result.accepted_auth == "sk-naked"


def test_probe_accepts_bearer_after_naked_rejected():
    """Сырой отклонён → Bearer принят — accepted_auth с префиксом."""

    def fake(
        endpoint, auth, verify, model, *, config_proxy=None, post_ex=None
    ):
        if auth is None:
            return ProbeResult(
                "rejected", 401, None, 5, "токен отклонён (401)"
            )
        if auth == "Bearer sk-naked":
            return ProbeResult("ok", 200, None, 5, "доступен")
        return ProbeResult("rejected", 401, None, 5, "токен отклонён (401)")

    result = probe_connection(
        "http://x/v1/chat/completions",
        "sk-naked",
        True,
        "",
        probe_fn=fake,
    )
    assert result.auth_state == "accepted"
    assert result.accepted_auth == "Bearer sk-naked"


@pytest.mark.asyncio
async def test_naked_in_field_survives_save_into_call_headers(tmp_path):
    """Поле as-is → config.yml → build_call_headers: без тихой подстановки Bearer."""
    from gc_tui.editors.connection import ConnectionEditor
    from textual.app import App, ComposeResult
    from textual.widgets import Input

    template = (
        "backend:\n"
        '  completions_url: "http://old/v1/chat/completions"\n'
        "  chat_completion_headers:\n"
        '    Authorization: "Bearer <ВПИШИТЕ_ТОКЕН>"\n'
        "  use_ssl: false\n"
        "agent:\n"
        '  model: "m"\n'
    )
    cfg = tmp_path / "config.yml"
    cfg.write_text(template, encoding="utf-8")

    ed = ConnectionEditor(
        url_current="http://old/v1/chat/completions", auth_header=None
    )

    class _App(App):
        def compose(self) -> ComposeResult:
            yield ed

    async with _App().run_test() as pilot:
        await pilot.pause()
        ed.query_one(
            "#conn-url", Input
        ).value = "https://api.example/v1/chat/completions"
        ed.move(1)
        await pilot.pause()
        ed.query_one("#conn-token", Input).value = "sk-naked"
        _url, header, error, _proxy = ed.submit()
        assert error is None
        assert header == "sk-naked"

    ocw.write_values(
        cfg,
        {
            "completions_url": "https://api.example/v1/chat/completions",
            "Authorization": header,
        },
    )
    text = cfg.read_text(encoding="utf-8")
    assert 'Authorization: "sk-naked"' in text
    assert "Bearer sk-naked" not in text

    ctx = SimpleNamespace(
        config_dict={
            "x_cl_id": 1,
            "backend_api_mode": "chat_completions",
            "chat_completion_headers": {"Authorization": "sk-naked"},
        },
        auth_connector=None,
    )
    headers = build_call_headers(_StateStub(), ctx)
    assert headers.get("Authorization") == "sk-naked"


@pytest.mark.asyncio
async def test_bearer_applied_to_field_then_saved_as_written(tmp_path):
    """Проба приняла Bearer → поле показывает Bearer → конфиг и runtime с Bearer."""
    from gc_tui.editors.connection import ConnectionEditor
    from textual.app import App, ComposeResult
    from textual.widgets import Input

    template = (
        "backend:\n"
        '  completions_url: "http://old/v1/chat/completions"\n'
        "  chat_completion_headers:\n"
        '    Authorization: "Bearer <ВПИШИТЕ_ТОКЕН>"\n'
        "  use_ssl: false\n"
        "agent:\n"
        '  model: "m"\n'
    )
    cfg = tmp_path / "config.yml"
    cfg.write_text(template, encoding="utf-8")

    ed = ConnectionEditor(
        url_current="http://old/v1/chat/completions", auth_header=None
    )

    class _App(App):
        def compose(self) -> ComposeResult:
            yield ed

    async with _App().run_test() as pilot:
        await pilot.pause()
        ed.query_one(
            "#conn-url", Input
        ).value = "https://api.example/v1/chat/completions"
        ed.move(1)
        await pilot.pause()
        ed.query_one("#conn-token", Input).value = "sk-naked"
        assert ed.apply_accepted_auth("Bearer sk-naked") is True
        assert ed.query_one("#conn-token", Input).value == "Bearer sk-naked"
        _url, header, error, _proxy = ed.submit()
        assert error is None
        assert header == "Bearer sk-naked"

    ocw.write_values(cfg, {"Authorization": header})
    assert 'Authorization: "Bearer sk-naked"' in cfg.read_text(
        encoding="utf-8"
    )

    ctx = SimpleNamespace(
        config_dict={
            "x_cl_id": 1,
            "backend_api_mode": "chat_completions",
            "chat_completion_headers": {"Authorization": "Bearer sk-naked"},
        },
        auth_connector=None,
    )
    assert (
        build_call_headers(_StateStub(), ctx).get("Authorization")
        == "Bearer sk-naked"
    )
