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


def test_removed_agent_edits_keys_are_dropped_with_notice():
    """Ключи умершего visual-diff не роняют старый config.yml (extra=forbid)."""
    raw = {"agent_edits": {"preview_context_lines": 50, "full_file_max_lines": 300}}
    facts = compat.apply_compat(raw)
    assert raw["agent_edits"] == {}
    assert len(facts) == 2
    assert all("устарел и игнорируется" in f for f in facts)


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
    from gc_core.config import load_config
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

    cfg = load_config(str(global_cfg))
    from gc_core.config_modules.chat_completions import (
        auxiliary_llm_from_config_dict,
    )

    assert cfg["auxiliary_llm_params"]["max_tokens"] == 4096
    assert auxiliary_llm_from_config_dict(cfg).max_tokens == 4096
    assert "llm" not in cfg["chat_completions_params"]


def test_load_config_survives_renamed_key(monkeypatch, tmp_path):
    monkeypatch.setattr(
        compat, "RENAMES", {"backend.legacy_url": "backend.completions_url"}
    )
    from gc_core.config import load_config

    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        'backend:\n  legacy_url: "http://x/v1/chat/completions"\n',
        encoding="utf-8",
    )
    config_dict = load_config(
        str(cfg_file)
    )  # без compat здесь упал бы extra=forbid
    assert config_dict["completions_url"] == "http://x/v1/chat/completions"
    assert config_dict["compat_notices"]


def test_old_diff_tui_section_moves_to_agent_edits():
    """Секция `diff_tui:` переименована в `agent_edits:` — старый конфиг грузится.

    Запись правок давно в ядре и идёт не только в TUI: имя секции врало."""
    raw = {"diff_tui": {"hunk_context_lines": 5, "shell_tracking": False}}
    facts = compat.apply_compat(raw)
    assert raw == {"agent_edits": {"hunk_context_lines": 5, "shell_tracking": False}}
    assert any("diff_tui" in f and "agent_edits" in f for f in facts)


def test_legacy_leaves_inside_the_old_section_are_still_handled():
    """Старые листья внутри старой секции: переименованный — переносится, удалённый — уходит."""
    raw = {"diff_tui": {"cache_max_orig": 42, "preview_context_lines": 50}}
    compat.apply_compat(raw)
    assert raw == {"agent_edits": {"snapshots_max_files": 42}}


def test_old_section_merges_under_the_new_one_new_wins():
    """Старая секция в одном слое конфига, новая — в другом: не выбрасывать старую
    целиком, а слить под новую; заданное в новой побеждает."""
    raw = {
        "diff_tui": {"hunk_context_lines": 5, "shell_tracking": False},
        "agent_edits": {"hunk_context_lines": 9},
    }
    facts = compat.apply_compat(raw)
    assert raw == {"agent_edits": {"hunk_context_lines": 9, "shell_tracking": False}}
    assert facts


def test_old_section_in_a_higher_layer_still_overrides_the_new_one_below(tmp_path, monkeypatch):
    """Старая `diff_tui:` в ПРОЕКТНОМ слое побеждает новую `agent_edits:` в
    пользовательском — как побеждала бы под одним именем.

    Compat, применённый к уже склеенному конфигу, не знает, из какого слоя
    пришёл ключ: правило «новое имя побеждает» переворачивало порядок слоёв, и
    настройка проекта молча терялась — вместе со старым листом `cache_max_orig`
    (ревью коммитов режима записи, находка 1). Слои выше базы проходят compat
    ДО склейки."""
    from gc_core.config import load_config
    from gc_core.config_modules import env_vars
    from gc_core.config_modules.env_vars import EnvVars

    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(env_vars, "env", EnvVars(gc_home=tmp_path))

    global_cfg = tmp_path / "global.yml"
    global_cfg.write_text(
        "backend:\n  api_mode: chat_completions\n"
        "agent_edits:\n  trace_inline_max_lines: 30\n  snapshots_max_files: 500\n",
        encoding="utf-8",
    )
    proj = tmp_path / "proj"
    (proj / ".gc").mkdir(parents=True)
    (proj / ".gc" / "config.yml").write_text(
        "diff_tui:\n  trace_inline_max_lines: 50\n  cache_max_orig: 42\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(proj)

    cfg = load_config(str(global_cfg))

    assert cfg["agent_edits"]["trace_inline_max_lines"] == 50
    assert cfg["agent_edits"]["snapshots_max_files"] == 42
    assert any("config.yml" in n and "diff_tui" in n for n in cfg["compat_notices"])


def test_overrides_with_the_old_section_name_still_apply():
    """Перекрытие старым именем секции (`agent_overrides` сервера, `--ключ` CLI)
    доходит до новой секции — с уведомлением.

    Перекрытия кладутся прямо в готовый конфиг, мимо compat загрузки: старое
    `diff_tui` оседало мёртвым ключом, и настройка молча не действовала (ревью
    коммитов режима записи, находка 3)."""
    from gc_core.config import apply_cli_overrides

    cfg = {"agent_edits": {"trace_inline_max_lines": 30}, "compat_notices": []}
    apply_cli_overrides(cfg, extra_overrides={"diff_tui": {"trace_inline_max_lines": 50}})

    assert "diff_tui" not in cfg
    assert cfg["agent_edits"] == {"trace_inline_max_lines": 50}
    assert any("diff_tui" in n for n in cfg["compat_notices"])


def test_old_name_in_the_base_and_above_keeps_the_base_values(tmp_path, monkeypatch):
    """Старое имя и в базе, и в слое выше: значения базы, не перекрытые сверху,
    не теряются.

    Верхний слой переименовывался до склейки, база — только после: в склеенном
    конфиге сталкивались `llm` базы и `auxiliary_llm` проекта, и проход после
    склейки отбрасывал `llm` базы целиком (ревью починок compat). Все слои, база
    тоже, приходят к склейке с новыми именами."""
    from gc_core.config import load_config
    from gc_core.config_modules import env_vars
    from gc_core.config_modules.env_vars import EnvVars

    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(env_vars, "env", EnvVars(gc_home=tmp_path))

    global_cfg = tmp_path / "global.yml"
    global_cfg.write_text(
        "backend:\n  api_mode: chat_completions\n"
        "chat_completions:\n  llm:\n    max_tokens: 1111\n    judge_max_tokens: 2222\n",
        encoding="utf-8",
    )
    proj = tmp_path / "proj"
    (proj / ".gc").mkdir(parents=True)
    (proj / ".gc" / "config.yml").write_text(
        "chat_completions:\n  llm:\n    max_tokens: 4096\n", encoding="utf-8",
    )
    monkeypatch.chdir(proj)

    cfg = load_config(str(global_cfg))

    assert cfg["auxiliary_llm_params"]["max_tokens"] == 4096
    assert cfg["auxiliary_llm_params"]["judge_max_tokens"] == 2222
    assert not any("отброшено" in n for n in cfg["compat_notices"])
