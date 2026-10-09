from gc_core.config import load_settings
from gc_core.settings import is_explicit


def test_backend_stream_flagged_when_explicit(tmp_path):
    cfg = tmp_path / "c.yml"
    cfg.write_text(
        "backend:\n  completions_url: http://x/v1\n  stream: false\n",
        encoding="utf-8",
    )
    loaded = load_settings(str(cfg))
    # Явно заданный устаревший ключ виден по явности поля (startup_notices).
    assert is_explicit(loaded.settings, "backend.stream")


def test_no_deprecated_when_absent(tmp_path):
    cfg = tmp_path / "c.yml"
    cfg.write_text(
        "backend:\n  completions_url: http://x/v1\n", encoding="utf-8"
    )
    loaded = load_settings(str(cfg))
    assert not is_explicit(loaded.settings, "backend.stream")
