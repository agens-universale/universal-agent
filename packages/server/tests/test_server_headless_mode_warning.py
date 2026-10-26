"""Старт gc-serve с режимом, который без человека не работает: предупреждение,
а не отказ."""

import logging

from gc_server import server
from gc_testkit.settings import make_settings


def _start(monkeypatch, caplog, mode):
    started = []
    monkeypatch.setattr(
        server.uvicorn, "run", lambda *a, **kw: started.append(kw)
    )
    monkeypatch.setattr(
        server,
        "BASE_AGENT_SETTINGS",
        make_settings({"security.permissions.default_mode": mode}),
    )
    with caplog.at_level(logging.WARNING, logger=server.logger.name):
        assert server.serve() == 0
    assert started  # старт не прерван
    return caplog.text


def test_plan_mode_is_warned_about_with_the_way_back(monkeypatch, caplog):
    text = _start(monkeypatch, caplog, "plan")
    assert "режим разрешений по умолчанию — plan" in text
    assert "agent_overrides" in text


def test_manual_mode_is_warned_about(monkeypatch, caplog):
    assert "— manual" in _start(monkeypatch, caplog, "manual")


def test_edit_mode_starts_silently(monkeypatch, caplog):
    assert "режим разрешений" not in _start(monkeypatch, caplog, "edit")
