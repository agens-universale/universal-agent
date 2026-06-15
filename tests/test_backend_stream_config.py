from config_modules.backend import BackendConfig


def test_backend_stream_defaults_true():
    assert BackendConfig().stream is True


def test_backend_stream_can_be_disabled():
    assert BackendConfig(stream=False).stream is False
