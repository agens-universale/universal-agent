"""Стрим — только для TUI: без on_delta в payload не ставится stream:true.

CLI/server/swarm ходят не-стримом независимо от cfg["stream"] — флаг TUI-only.
"""
from gc_core.utils import connect_backend as cb

from transport_fakes import install_transport


def test_no_on_delta_sends_non_stream_payload(monkeypatch):
    spy = install_transport(monkeypatch)

    cb.request_backend_chat_completions(
        {"model": "m", "messages": []}, {}, url="http://x", on_delta=None,
    )

    assert "stream" not in spy.last_payload
