import os
import tempfile

from config import load_config
from config_modules.backend import BackendConfig


def test_backend_stream_defaults_true():
    assert BackendConfig().stream is True


def test_backend_stream_can_be_disabled():
    assert BackendConfig(stream=False).stream is False


def test_load_config_stream_default_true():
    with tempfile.TemporaryDirectory() as tmp_dir:
        cfg_path = os.path.join(tmp_dir, "config.yml")
        with open(cfg_path, "w", encoding="utf-8") as fd:
            fd.write("backend:\n  api_mode: chat_completions\n")
        cfg = load_config(cfg_path)
        assert cfg["stream"] is True
