"""Старт gc-serve с режимом, который без человека не работает: предупреждение,
а не отказ."""

from gc_server import cli
from gc_server.config import ServerConfig
from gc_server.webhook.surface import WebhookSurface
from server_harness import runtime


def _warnings(capsys, mode: str) -> str:
    surface = WebhookSurface(
        runtime(**{"security.permissions.default_mode": mode})
    )
    cli._warn(ServerConfig(), [surface])
    return capsys.readouterr().err


def test_plan_mode_is_warned_about_with_the_way_back(capsys):
    text = _warnings(capsys, "plan")
    assert "режим разрешений по умолчанию — plan" in text
    assert "agent_overrides" in text


def test_manual_mode_is_warned_about(capsys):
    assert "— manual" in _warnings(capsys, "manual")


def test_edit_mode_starts_silently(capsys):
    assert "режим разрешений" not in _warnings(capsys, "edit")
