import pytest

import onboarding_config_writer as w


def test_replaces_sentinel_preserving_comment_and_indent():
    text = (
        "backend:\n"
        "  # Впишите endpoint\n"
        '  completions_url: "http://your-backend:PORT/v1/chat/completions"\n'
        "  use_ssl: false\n"
    )
    out = w.set_scalar(text, "completions_url", "https://x/v1/chat/completions")
    assert '  completions_url: "https://x/v1/chat/completions"' in out
    assert "# Впишите endpoint" in out          # комментарий сохранён
    assert "use_ssl: false" in out              # соседние строки целы


def test_replaces_empty_value():
    text = "agent:\n  model: \"\"\n"
    out = w.set_scalar(text, "model", "qwen3-coder")
    assert '  model: "qwen3-coder"' in out


def test_ignores_commented_line_and_raises_when_absent():
    text = "agent:\n#  model: gigacode_agent_v1.6_rc\n"
    with pytest.raises(KeyError):
        w.set_scalar(text, "model", "x")


def test_escapes_quotes_and_backslashes():
    text = 'h:\n    Authorization: "Bearer <ВПИШИТЕ_ТОКЕН>"\n'
    out = w.set_scalar(text, "Authorization", 'Bearer a"b\\c')
    assert '    Authorization: "Bearer a\\"b\\\\c"' in out


def test_write_values_updates_file_and_reports_missing(tmp_path):
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        "backend:\n"
        '  completions_url: "http://your-backend:PORT/v1/chat/completions"\n'
        "  chat_completion_headers:\n"
        '    Authorization: "Bearer <ВПИШИТЕ_ТОКЕН>"\n'
        "agent:\n"
        '  model: "gigacode_agent_v1.6_rc"\n',
        encoding="utf-8",
    )
    missing = w.write_values(
        cfg,
        {"completions_url": "https://x/v1", "Authorization": "Bearer sk-1", "nope": "z"},
    )
    out = cfg.read_text(encoding="utf-8")
    assert '  completions_url: "https://x/v1"' in out
    assert '    Authorization: "Bearer sk-1"' in out
    assert '  model: "gigacode_agent_v1.6_rc"' in out    # не тронут
    assert missing == ["nope"]


# ── Task 5: §6.4 — write_values разворачивает закомментированный блок шаблона ──

def test_uncomment_template_block_shape():
    tpl = ("# Описание секции\n"
           "#gizmo:\n"
           "#  provider: openai\n"
           "#  url: https://x\n")
    block = w.uncomment_template_block(tpl, "gizmo")
    assert "# Описание секции\n" in block          # описание остаётся комментарием
    assert "\ngizmo:\n" in "\n" + block            # шапка раскомментирована
    assert "  provider: openai" in block


_TEMPLATE_WITH_COMMENTED_SECTION = (
    'agent:\n  model: "m"\n\n'
    "# Сборка мусора — намеренно выключена из коробки\n"
    "#gc:\n"
    "#  enabled: true\n"
)


def test_write_values_expands_commented_block_for_key_it_contains(tmp_path):
    """Охранный: рабочий путь §6.4 — ключ ЕСТЬ в блоке шаблона, блок
    разворачивается и значение пишется."""
    cfg = tmp_path / "config.yml"
    cfg.write_text('agent:\n  model: "m"\n', encoding="utf-8")
    tpl = tmp_path / "config.default.yml"
    tpl.write_text(_TEMPLATE_WITH_COMMENTED_SECTION, encoding="utf-8")

    missing = w.write_values(cfg, {"gc.enabled": "false"}, template_path=tpl)

    out = cfg.read_text(encoding="utf-8")
    assert missing == []
    assert "gc:" in out
    assert "enabled:" in out


def test_write_values_rolls_back_expanded_block_when_key_still_missing(tmp_path):
    """Ключа нет и в блоке шаблона: `setter` падает KeyError уже ПОСЛЕ того, как
    блок дописан в текст. Ключ уходит в missing — значит и раскомментированная
    секция не должна попасть в файл, иначе юзер молча получает включённой
    секцию, которую сознательно держал выключенной."""
    cfg = tmp_path / "config.yml"
    cfg.write_text('agent:\n  model: "m"\n', encoding="utf-8")
    before = cfg.read_text(encoding="utf-8")
    tpl = tmp_path / "config.default.yml"
    tpl.write_text(_TEMPLATE_WITH_COMMENTED_SECTION, encoding="utf-8")

    missing = w.write_values(cfg, {"gc.threshold": "10"}, template_path=tpl)

    assert missing == ["gc.threshold"]
    assert cfg.read_text(encoding="utf-8") == before
