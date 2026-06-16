from config_modules.backend import BackendConfig


def test_stream_answer_preview_defaults_false():
    assert BackendConfig().stream_answer_preview is False


def test_stream_answer_preview_can_be_enabled():
    assert BackendConfig(stream_answer_preview=True).stream_answer_preview is True
