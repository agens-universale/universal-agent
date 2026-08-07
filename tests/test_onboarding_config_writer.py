import pytest
from pathlib import Path

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
    text = "voice:\n#  model: whisper-1\n"
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

def test_write_voice_values_uncomments_template_block(tmp_path):
    """§6.4: voice-блок шаблона закомментирован — write_values разворачивает его."""
    from config import _default_config_path, validate_config_text

    template = Path(_default_config_path())
    cfg = tmp_path / "config.yml"
    cfg.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")

    missing = w.write_values(
        cfg,
        {"voice.url": "https://stt.example/v1/audio/transcriptions",
         "voice.token": "sk-x", "voice.model": "whisper-1"},
        template_path=template, validate=validate_config_text,
    )
    assert missing == []
    text = cfg.read_text(encoding="utf-8")
    assert 'url: "https://stt.example/v1/audio/transcriptions"' in text
    assert "provider: openai" in text            # блок развёрнут целиком
    # повторная запись идемпотентна: якоря теперь есть
    assert w.write_values(cfg, {"voice.model": "whisper-large"},
                          template_path=template, validate=validate_config_text) == []
    assert 'model: "whisper-large"' in cfg.read_text(encoding="utf-8")


def test_uncomment_template_block_shape():
    tpl = ("# Описание секции\n"
           "#voice:\n"
           "#  provider: openai\n"
           "#  url: https://x\n")
    block = w.uncomment_template_block(tpl, "voice")
    assert "# Описание секции\n" in block          # описание остаётся комментарием
    assert "\nvoice:\n" in "\n" + block            # шапка раскомментирована
    assert "  provider: openai" in block


# ── Fix round 1: третий fallback не трогает частично живую секцию ──

def test_write_values_skips_uncomment_fallback_when_section_already_live(tmp_path):
    """Юзер сам завёл voice: с одним полем (секция уже живая, но неполная).

    Третий fallback (uncomment_template_block) не должен дописывать в хвост
    ВТОРОЙ, шаблонный voice-блок: PyYAML не ругается на дубли top-level ключей
    и молча берёт последний — юзерский url тихо потерялся бы."""
    import re
    import yaml
    from config import _default_config_path

    template = Path(_default_config_path())
    cfg = tmp_path / "config.yml"
    user_url = "https://my-real-stt.example/v1"
    cfg.write_text(
        template.read_text(encoding="utf-8") + f'voice:\n  url: "{user_url}"\n',
        encoding="utf-8",
    )

    missing = w.write_values(cfg, {"voice.model": "wh"}, template_path=template)

    assert missing == ["voice.model"]
    text = cfg.read_text(encoding="utf-8")
    assert len(re.findall(r"^voice:", text, re.MULTILINE)) == 1   # блок не задублирован
    assert f'url: "{user_url}"' in text                            # юзерское значение цело
    assert yaml.safe_load(text)["voice"]["url"] == user_url
