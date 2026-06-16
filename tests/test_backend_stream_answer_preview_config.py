from config_modules.backend import BackendConfig


def test_stream_answer_preview_defaults_true():
    assert BackendConfig().stream_answer_preview is True


def test_stream_answer_preview_can_be_disabled():
    assert BackendConfig(stream_answer_preview=False).stream_answer_preview is False
