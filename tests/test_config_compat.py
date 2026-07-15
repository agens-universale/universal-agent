"""compat-слой: rename/remove устаревших ключей ДО pydantic-валидации."""
import config_modules.compat as compat


def test_rename_moves_value(monkeypatch):
    monkeypatch.setattr(compat, "RENAMES", {"backend.old_url": "backend.new_url"})
    raw = {"backend": {"old_url": "http://x"}}
    facts = compat.apply_compat(raw)
    assert raw == {"backend": {"new_url": "http://x"}}
    assert facts and "backend.old_url" in facts[0] and "backend.new_url" in facts[0]


def test_rename_does_not_override_explicit_new_value(monkeypatch):
    monkeypatch.setattr(compat, "RENAMES", {"backend.old_url": "backend.new_url"})
    raw = {"backend": {"old_url": "http://old", "new_url": "http://new"}}
    facts = compat.apply_compat(raw)
    assert raw["backend"]["new_url"] == "http://new"      # явный новый побеждает
    assert "old_url" not in raw["backend"]                 # старый убран из raw
    # M4a: при конфликте explicit_new текст факта говорит об «отброшенном» старом
    assert facts and "устарел" in facts[0] and "отброшено" in facts[0]


def test_rename_with_converter(monkeypatch):
    monkeypatch.setattr(compat, "RENAMES", {"a.timeout": "a.timeout_seconds"})
    monkeypatch.setattr(compat, "RENAME_CONVERTERS", {"a.timeout": lambda v: int(v) // 1000})
    raw = {"a": {"timeout": "5000"}}
    compat.apply_compat(raw)
    assert raw == {"a": {"timeout_seconds": 5}}


def test_rename_converter_failure_drops_value_with_fact(monkeypatch):
    monkeypatch.setattr(compat, "RENAMES", {"a.timeout": "a.timeout_seconds"})
    monkeypatch.setattr(compat, "RENAME_CONVERTERS", {"a.timeout": lambda v: int(v)})
    raw = {"a": {"timeout": "не число"}}
    facts = compat.apply_compat(raw)
    assert "timeout_seconds" not in raw.get("a", {})
    assert any("задайте заново" in f for f in facts)


def test_removed_key_dropped(monkeypatch):
    monkeypatch.setattr(compat, "REMOVED", {"legacy.flag": "функция удалена"})
    raw = {"legacy": {"flag": True}, "keep": 1}
    facts = compat.apply_compat(raw)
    assert raw == {"legacy": {}, "keep": 1}
    assert any("legacy.flag" in f for f in facts)


def test_untouched_config_yields_no_facts():
    assert compat.apply_compat({"backend": {"x": 1}}) == []


def test_load_config_survives_renamed_key(monkeypatch, tmp_path):
    monkeypatch.setattr(compat, "RENAMES", {"backend.legacy_url": "backend.completions_url"})
    from config import load_config
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        "backend:\n  legacy_url: \"http://x/v1/chat/completions\"\n",
        encoding="utf-8")
    config_dict = load_config(str(cfg_file))   # без compat здесь упал бы extra=forbid
    assert config_dict["completions_url"] == "http://x/v1/chat/completions"
    assert config_dict["compat_notices"]
