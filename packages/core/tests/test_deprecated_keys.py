from gc_core.config import load_config


def test_backend_stream_flagged_when_explicit(tmp_path):
    cfg = tmp_path / "c.yml"
    cfg.write_text(
        "backend:\n  completions_url: http://x/v1\n  stream: false\n",
        encoding="utf-8",
    )
    d = load_config(str(cfg))
    assert "backend.stream" in d.get("deprecated_keys_used", [])


def test_no_deprecated_when_absent(tmp_path):
    cfg = tmp_path / "c.yml"
    cfg.write_text(
        "backend:\n  completions_url: http://x/v1\n", encoding="utf-8"
    )
    d = load_config(str(cfg))
    assert d.get("deprecated_keys_used", []) == []
