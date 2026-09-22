"""Path-aware якоря writer'а: одинаковые leaf-имена в разных секциях не путаются."""

import pytest
from gc_core import onboarding_config_writer as w

DOC = (
    "security:\n"
    "  command_moderation:\n"
    "    # режим модерации\n"
    "    enabled: true\n"
    '    mode: "strict"\n'
    "  write_guard:\n"
    "    enabled: false\n"
    '    mode: "warn"\n'
    "backend:\n"
    '  completions_url: "http://x"\n'
)


def test_find_anchor_resolves_by_section():
    start, end, indent, rest = w.find_anchor(DOC, "security.write_guard.mode")
    assert DOC[start:end] == '    mode: "warn"\n'
    assert indent == "    "


def test_find_anchor_top_level_requires_zero_indent():
    start, end, _, _ = w.find_anchor(DOC, "backend")
    assert DOC[start:end] == "backend:\n"


def test_find_anchor_missing_raises():
    with pytest.raises(KeyError):
        w.find_anchor(DOC, "security.read_guard.mode")


def test_set_scalar_at_path_touches_only_target_section():
    out = w.set_scalar_at_path(DOC, "security.write_guard.mode", "block")
    assert '    mode: "block"' in out
    assert 'mode: "strict"' in out  # соседняя секция не тронута
    assert out.count("mode:") == 2


def test_section_end_spans_nested_lines():
    start, end, indent, _ = w.find_anchor(DOC, "security.command_moderation")
    block_end = w.section_end(DOC, end, indent)
    block = DOC[start:block_end]
    assert 'mode: "strict"' in block
    assert "write_guard" not in block


# ── Task 7: insert_missing + write_values (template + validate) ──────────────

TEMPLATE = (
    "backend:\n"
    '  completions_url: "http://sentinel"\n'
    "update:\n"
    "  # авто-проверка обновлений: auto | off\n"
    '  check: "auto"\n'
    "  check_interval_days: 1\n"
)


def test_insert_missing_key_into_existing_section():
    user = 'backend:\n  completions_url: "http://real"\nupdate:\n  check: "off"\n'
    out = w.insert_missing(user, "update.check_interval_days", TEMPLATE)
    assert "check_interval_days: 1" in out
    assert out.index("check_interval_days") > out.index('check: "off"')


def test_insert_missing_section_appended_with_comments():
    user = 'backend:\n  completions_url: "http://real"\n'
    out = w.insert_missing(user, "update.check", TEMPLATE)
    assert "update:" in out
    assert "# авто-проверка обновлений: auto | off" in out
    assert 'check: "auto"' in out


def test_write_values_inserts_missing_via_template(tmp_path):
    cfg = tmp_path / "config.yml"
    cfg.write_text('backend:\n  completions_url: "http://real"\n', encoding="utf-8")
    tpl = tmp_path / "config.default.yml"
    tpl.write_text(TEMPLATE, encoding="utf-8")
    missing = w.write_values(cfg, {"update.check": "off"}, template_path=tpl)
    assert missing == []
    assert 'check: "off"' in cfg.read_text(encoding="utf-8")


def test_write_values_validation_failure_leaves_file_untouched(tmp_path):
    cfg = tmp_path / "config.yml"
    original = 'backend:\n  completions_url: "http://real"\n'
    cfg.write_text(original, encoding="utf-8")

    def _reject(text):
        raise ValueError("схема не прошла")

    import pytest

    with pytest.raises(ValueError):
        w.write_values(cfg, {"backend.completions_url": "http://new"}, validate=_reject)
    assert cfg.read_text(encoding="utf-8") == original


def test_validate_config_text_accepts_renamed_key(monkeypatch):
    from gc_core.config import validate_config_text
    from gc_core.config_modules import compat as compat

    monkeypatch.setattr(compat, "RENAMES", {"backend.legacy_url": "backend.completions_url"})
    validate_config_text('backend:\n  legacy_url: "http://x"\n')  # не бросает


def test_validate_config_text_rejects_broken_yaml():
    import pytest
    from gc_core.config import validate_config_text

    with pytest.raises(ValueError):
        validate_config_text("backend: [незакрытый\n  список")


def test_validate_config_text_ignores_unresolvable_sections():
    """Фаза 1 ссылочную целостность не проверяет — слот на несуществующий алиас
    проходит (её ловит только `validate_config_text_with_sections`)."""
    from gc_core.config import validate_config_text

    validate_config_text('models:\n  flash:\n    url: "http://x"\n    token: "sk"\nslots:\n  default: "нет-такого"\n')


def test_validate_config_text_with_sections_rejects_dangling_slot():
    import pytest
    from gc_core.config import validate_config_text_with_sections

    with pytest.raises(ValueError):
        validate_config_text_with_sections(
            'models:\n  flash:\n    url: "http://x"\n    token: "sk"\nslots:\n  default: "нет-такого"\n'
        )


def test_validate_config_text_with_sections_accepts_resolvable_registry():
    from gc_core.config import validate_config_text_with_sections

    validate_config_text_with_sections(
        'models:\n  flash:\n    url: "http://x"\n    token: "sk"\nslots:\n  default: "flash"\n'
    )


def test_field_anchor_paths_resolve_in_real_template():
    from pathlib import Path

    from gc_core import onboarding
    from gc_core.config import _default_config_path

    template = Path(_default_config_path()).read_text(encoding="utf-8")
    for _leaf, dotted in onboarding.FIELD_ANCHOR_PATHS.items():
        w.find_anchor(template, dotted)  # KeyError = спека/шаблон разошлись
