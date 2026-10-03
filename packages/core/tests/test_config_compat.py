"""compat-слой: rename/remove устаревших ключей ДО pydantic-валидации."""

from gc_core.config_modules import compat as compat


def test_rename_moves_value(monkeypatch):
    monkeypatch.setattr(
        compat, "RENAMES", {"backend.old_url": "backend.new_url"}
    )
    raw = {"backend": {"old_url": "http://x"}}
    facts = compat.apply_compat(raw)
    assert raw == {"backend": {"new_url": "http://x"}}
    assert (
        facts
        and "backend.old_url" in facts[0]
        and "backend.new_url" in facts[0]
    )


def test_rename_does_not_override_explicit_new_value(monkeypatch):
    monkeypatch.setattr(
        compat, "RENAMES", {"backend.old_url": "backend.new_url"}
    )
    raw = {"backend": {"old_url": "http://old", "new_url": "http://new"}}
    facts = compat.apply_compat(raw)
    assert raw["backend"]["new_url"] == "http://new"  # явный новый побеждает
    assert "old_url" not in raw["backend"]  # старый убран из raw
    # M4a: при конфликте explicit_new текст факта говорит об «отброшенном» старом
    assert facts and "устарел" in facts[0] and "отброшено" in facts[0]


def test_rename_with_converter(monkeypatch):
    monkeypatch.setattr(compat, "RENAMES", {"a.timeout": "a.timeout_seconds"})
    monkeypatch.setattr(
        compat, "RENAME_CONVERTERS", {"a.timeout": lambda v: int(v) // 1000}
    )
    raw = {"a": {"timeout": "5000"}}
    compat.apply_compat(raw)
    assert raw == {"a": {"timeout_seconds": 5}}


def test_rename_converter_failure_drops_value_with_fact(monkeypatch):
    monkeypatch.setattr(compat, "RENAMES", {"a.timeout": "a.timeout_seconds"})
    monkeypatch.setattr(
        compat, "RENAME_CONVERTERS", {"a.timeout": lambda v: int(v)}
    )
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


def test_llm_to_auxiliary_llm_rename_in_compat():
    """Реальный RENAMES: llm → auxiliary_llm до model_validate."""
    raw = {"chat_completions": {"llm": {"max_tokens": 1111}}}
    facts = compat.apply_compat(raw)
    assert raw == {"chat_completions": {"auxiliary_llm": {"max_tokens": 1111}}}
    assert facts and "chat_completions.llm" in facts[0]


def test_llm_dropped_when_auxiliary_llm_already_present():
    """Layered merge: user.llm + project.auxiliary_llm — без compat падает extra=forbid."""
    raw = {
        "chat_completions": {
            "llm": {"max_tokens": 1111},
            "auxiliary_llm": {"max_tokens": 4096},
        }
    }
    facts = compat.apply_compat(raw)
    assert "llm" not in raw["chat_completions"]
    assert raw["chat_completions"]["auxiliary_llm"]["max_tokens"] == 4096
    assert facts and "отброшено" in facts[0]


def test_layered_llm_and_auxiliary_llm_loads(tmp_path, monkeypatch):
    """User llm + project auxiliary_llm: агент стартует, побеждает project."""
    from gc_core.config import load_settings
    from gc_core.config_modules import env_vars
    from gc_core.config_modules.env_vars import EnvVars

    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(env_vars, "env", EnvVars(gc_home=tmp_path))

    global_cfg = tmp_path / "global.yml"
    global_cfg.write_text(
        "backend:\n  api_mode: chat_completions\nchat_completions:\n  llm:\n    max_tokens: 1111\n",
        encoding="utf-8",
    )
    proj = tmp_path / "proj"
    proj.mkdir()
    gc = proj / ".gc"
    gc.mkdir()
    (gc / "config.yml").write_text(
        "chat_completions:\n  auxiliary_llm:\n    max_tokens: 4096\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(proj)

    loaded = load_settings(str(global_cfg))
    from gc_core.config_modules.chat_completions import (
        auxiliary_llm_from_config_dict,
    )

    settings = loaded.settings
    assert settings.chat_completions.auxiliary_llm.max_tokens == 4096
    assert auxiliary_llm_from_config_dict(loaded).max_tokens == 4096
    assert "llm" not in settings.chat_completions.model_dump(by_alias=True)


def test_load_settings_survives_renamed_key(monkeypatch, tmp_path):
    monkeypatch.setattr(
        compat, "RENAMES", {"backend.legacy_url": "backend.completions_url"}
    )
    from gc_core.config import load_settings

    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        'backend:\n  legacy_url: "http://x/v1/chat/completions"\n',
        encoding="utf-8",
    )
    loaded = load_settings(
        str(cfg_file)
    )  # без compat здесь упал бы extra=forbid
    assert (
        loaded.settings.backend.completions_url
        == "http://x/v1/chat/completions"
    )
    assert loaded.runtime.notices
