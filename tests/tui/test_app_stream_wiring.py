"""TuiApp._stream_sink: app owns a StreamSink and wires it to the executor context."""
from gc_tui.app import TuiApp


def test_app_has_stream_sink():
    app = TuiApp()
    assert app._stream_sink is not None
    app._stream_sink.push("answer", "x")
    assert app._stream_sink.snapshot()[1] == "x"
