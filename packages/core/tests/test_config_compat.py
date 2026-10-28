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
    raw = {
        "agent_edits": {
            "preview_context_lines": 50,
            "full_file_max_lines": 300,
        }
    }
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


def test_old_diff_tui_section_moves_to_agent_edits():
    """Секция `diff_tui:` переименована в `agent_edits:` — старый конфиг грузится.

    Запись правок давно в ядре и идёт не только в TUI: имя секции врало."""
    raw = {"diff_tui": {"hunk_context_lines": 5, "shell_tracking": False}}
    facts = compat.apply_compat(raw)
    assert raw == {
        "agent_edits": {"hunk_context_lines": 5, "shell_tracking": False}
    }
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
    assert raw == {
        "agent_edits": {"hunk_context_lines": 9, "shell_tracking": False}
    }
    assert facts


def test_old_section_in_a_higher_layer_still_overrides_the_new_one_below(
    tmp_path, monkeypatch
):
    """Старая `diff_tui:` в ПРОЕКТНОМ слое побеждает новую `agent_edits:` в
    пользовательском — как побеждала бы под одним именем.

    Compat, применённый к уже склеенному конфигу, не знает, из какого слоя
    пришёл ключ: правило «новое имя побеждает» переворачивало порядок слоёв, и
    настройка проекта молча терялась — вместе со старым листом `cache_max_orig`
    (ревью коммитов режима записи, находка 1). Слои выше базы проходят compat
    ДО склейки."""
    from gc_core.config import load_settings
    from gc_core.config_modules import env_vars
    from gc_core.config_modules.env_vars import EnvVars

    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(env_vars, "env", EnvVars(gc_home=tmp_path))

    global_cfg = tmp_path / "global.yml"
    global_cfg.write_text(
        "backend:\n  api_mode: chat_completions\n"
        "agent_edits:\n  trace_diff_preview_lines: 30\n  snapshots_max_files: 500\n",
        encoding="utf-8",
    )
    proj = tmp_path / "proj"
    (proj / ".gc").mkdir(parents=True)
    (proj / ".gc" / "config.yml").write_text(
        "diff_tui:\n  trace_diff_preview_lines: 50\n  cache_max_orig: 42\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(proj)

    loaded = load_settings(str(global_cfg))

    assert loaded.settings.agent_edits.trace_diff_preview_lines == 50
    assert loaded.settings.agent_edits.snapshots_max_files == 42
    assert any(
        "config.yml" in n and "diff_tui" in n for n in loaded.runtime.notices
    )


def test_notice_for_a_file_sync_does_not_update_asks_to_edit_it_by_hand(
    tmp_path, monkeypatch
):
    """`--sync-config` обновляет только пользовательский config.yml. Совет
    «запустите его» в уведомлении про проектный слой вёл в тупик: команда
    такой файл обновлять отказывается. Для него — править ключ вручную; для
    пользовательского файла совет прежний."""
    from gc_core.config import load_settings
    from gc_core.config_modules import env_vars
    from gc_core.config_modules.env_vars import EnvVars

    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(env_vars, "env", EnvVars(gc_home=tmp_path))

    global_cfg = tmp_path / "global.yml"
    global_cfg.write_text(
        "backend:\n  api_mode: chat_completions\n"
        "diff_tui:\n  trace_diff_preview_lines: 30\n",
        encoding="utf-8",
    )
    proj = tmp_path / "proj"
    (proj / ".gc").mkdir(parents=True)
    project_cfg = proj / ".gc" / "config.yml"
    project_cfg.write_text(
        "diff_tui:\n  replay_expanded_turns: 3\n", encoding="utf-8"
    )
    monkeypatch.chdir(proj)

    notices = load_settings(str(global_cfg)).runtime.notices

    project_notes = [n for n in notices if str(project_cfg) in n]
    base_notes = [n for n in notices if str(global_cfg) in n]
    assert project_notes and base_notes
    assert all(
        "вручную" in n and "--sync-config" not in n for n in project_notes
    )
    assert all("--sync-config" in n for n in base_notes)


def test_overrides_with_the_old_section_name_still_apply():
    """Перекрытие старым именем секции (`agent_overrides` сервера, `--ключ` CLI)
    доходит до новой секции — с уведомлением.

    Перекрытия кладутся прямо в готовый конфиг, мимо compat загрузки: старое
    `diff_tui` оседало мёртвым ключом, и настройка молча не действовала (ревью
    коммитов режима записи, находка 3)."""
    from gc_core.config import apply_cli_overrides
    from gc_testkit.settings import make_settings

    base = make_settings({"agent_edits.trace_diff_preview_lines": 30})
    loaded = apply_cli_overrides(
        base, extra_overrides={"diff_tui": {"trace_diff_preview_lines": 50}}
    )

    assert loaded.settings.agent_edits.trace_diff_preview_lines == 50
    assert any("diff_tui" in n for n in loaded.runtime.notices)


def test_old_name_in_the_base_and_above_keeps_the_base_values(
    tmp_path, monkeypatch
):
    """Старое имя и в базе, и в слое выше: значения базы, не перекрытые сверху,
    не теряются.

    Верхний слой переименовывался до склейки, база — только после: в склеенном
    конфиге сталкивались `llm` базы и `auxiliary_llm` проекта, и проход после
    склейки отбрасывал `llm` базы целиком (ревью починок compat). Все слои, база
    тоже, приходят к склейке с новыми именами."""
    from gc_core.config import load_settings
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
        "chat_completions:\n  llm:\n    max_tokens: 4096\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(proj)

    from gc_core.config_modules.chat_completions import (
        auxiliary_llm_from_config_dict,
    )

    loaded = load_settings(str(global_cfg))

    aux = auxiliary_llm_from_config_dict(loaded)
    assert aux.max_tokens == 4096
    assert aux.judge_max_tokens == 2222
    assert not any("отброшено" in n for n in loaded.runtime.notices)


def test_removed_auto_judge_binding_is_dropped_with_notice():
    """`assign.auto_judge` был в релизном demo_profile.yml активной строкой:
    скопированный конфиг не должен перестать грузиться (спека PR140 Р2)."""
    raw = {"assign": {"auto_judge": "max", "finish_gate": "default"}}
    facts = compat.apply_compat(raw)
    assert raw == {"assign": {"finish_gate": "default"}}
    assert any("assign.auto_judge" in f for f in facts)


def test_load_settings_survives_the_old_auto_judge(tmp_path):
    from gc_core.config import load_settings

    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text("assign:\n  auto_judge: max\n", encoding="utf-8")
    loaded = load_settings(str(cfg_file))
    assert loaded.settings.assign.finish_gate is None
    assert any("assign.auto_judge" in n for n in loaded.runtime.notices)


def test_a_profile_with_the_old_auto_judge_still_loads(tmp_path):
    from gc_core.config import load_settings

    base = tmp_path / "config.yml"
    base.write_text(
        "backend:\n  api_mode: chat_completions\n", encoding="utf-8"
    )
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "old.yml").write_text(
        "assign:\n  auto_judge: max\n", encoding="utf-8"
    )
    loaded = load_settings(str(base), profile_override="old")
    assert loaded.runtime.active_profile == "old"
    assert any("assign.auto_judge" in n for n in loaded.runtime.notices)


def test_auto_judge_is_rejected_without_compat():
    import pytest
    from pydantic import ValidationError

    from gc_core.config_modules.root import AppConfig

    with pytest.raises(ValidationError):
        AppConfig.model_validate({"assign": {"auto_judge": "max"}})


def test_old_trace_inline_max_lines_is_dropped_not_carried(
    tmp_path, monkeypatch
):
    """Ключ сменил смысл (было «30 экранных строк», стало «строк превью на
    файл») и имя: старое значение не переносится — действует умолчание
    нового ключа, уведомление называет замену."""
    from gc_core.config import load_settings
    from gc_core.config_modules import env_vars
    from gc_core.config_modules.env_vars import EnvVars

    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(env_vars, "env", EnvVars(gc_home=tmp_path))
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        "backend:\n  api_mode: chat_completions\n"
        "agent_edits:\n  trace_inline_max_lines: 30\n",
        encoding="utf-8",
    )

    loaded = load_settings(str(cfg))

    assert loaded.settings.agent_edits.trace_diff_preview_lines == 10
    (note,) = [
        n for n in loaded.runtime.notices if "trace_inline_max_lines" in n
    ]
    assert "trace_diff_preview_lines" in note
    assert "устарел и игнорируется" in note


def test_trace_diff_preview_lines_is_read(tmp_path, monkeypatch):
    from gc_core.config import load_settings
    from gc_core.config_modules import env_vars
    from gc_core.config_modules.env_vars import EnvVars

    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(env_vars, "env", EnvVars(gc_home=tmp_path))
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        "backend:\n  api_mode: chat_completions\n"
        "agent_edits:\n  trace_diff_preview_lines: 7\n",
        encoding="utf-8",
    )

    loaded = load_settings(str(cfg))

    assert loaded.settings.agent_edits.trace_diff_preview_lines == 7
    assert not [n for n in loaded.runtime.notices if "agent_edits" in n]
