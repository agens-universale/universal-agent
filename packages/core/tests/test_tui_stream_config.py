"""stream → tui.stream: канонический ключ в tui, с legacy-фолбэком на backend.stream.

resolve_tui_stream_enabled: явный tui_stream (True/False) побеждает; иначе legacy
top-level stream; иначе дефолт True. tui.stream по умолчанию None (не задан) —
чтобы отличить «явно выключил» от «не трогал» и уважать legacy backend.stream."""
from gc_core.config import resolve_tui_stream_enabled
from gc_core.config_modules.tui import TuiConfig


def test_tui_stream_field_default_unset():
    assert TuiConfig().stream is None
    assert TuiConfig(stream=False).stream is False
    assert TuiConfig(stream=True).stream is True


def test_explicit_tui_stream_wins():
    assert resolve_tui_stream_enabled({"tui_stream": True}) is True
    assert resolve_tui_stream_enabled({"tui_stream": False}) is False
    # явный tui_stream побеждает legacy stream
    assert resolve_tui_stream_enabled({"tui_stream": False, "stream": True}) is False


def test_legacy_stream_fallback_when_tui_unset():
    # tui_stream не задан (None или отсутствует) → падаем на legacy backend.stream
    assert resolve_tui_stream_enabled({"tui_stream": None, "stream": False}) is False
    assert resolve_tui_stream_enabled({"stream": False}) is False


def test_default_true_when_neither_set():
    assert resolve_tui_stream_enabled({}) is True
    assert resolve_tui_stream_enabled({"tui_stream": None}) is True
