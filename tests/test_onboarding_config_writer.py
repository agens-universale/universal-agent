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
           "# gizmo:\n"
           "#   provider: openai\n"
           "#   url: https://x\n")
    block = w.uncomment_template_block(tpl, "gizmo")
    assert "# Описание секции\n" in block          # описание остаётся комментарием
    assert "\ngizmo:\n" in "\n" + block            # шапка раскомментирована
    assert "  provider: openai" in block           # поле — с отступом канона


def test_uncomment_template_block_reads_the_tight_comment_form_too():
    """Форма без пробела после решётки тоже понимается — шаблоны бывают и такие."""
    tpl = "#gizmo:\n#  provider: openai\n"
    block = w.uncomment_template_block(tpl, "gizmo")
    assert "\ngizmo:\n" in "\n" + block
    assert "provider: openai" in block


# ── §6.4 на НАСТОЯЩЕМ шаблоне: якорь там пишется «# key:», с пробелом ──

def _real_template() -> str:
    from pathlib import Path
    from config import _default_config_path
    return Path(_default_config_path()).read_text(encoding="utf-8")


def test_uncomment_template_block_finds_the_real_anchor():
    """Живые шаблоны комментируют секции как «# gc:» — с пробелом после решётки.
    Regex требовал «#gc:», блок не находился, и write_values молча уносил ключ в
    missing. Тест выше собирает шаблон сам и проверяет формат, которого в шаблонах
    не бывает, — поэтому дыру не видел."""
    block = w.uncomment_template_block(_real_template(), "gc")
    assert "\ngc:\n" in "\n" + block            # шапка раскомментирована и БЕЗ отступа
    assert "\n  force_collect:" in "\n" + block  # поле — вложенным, как положено YAML


def test_uncomment_template_block_unfolds_a_list_section():
    """Список пресетов — тот же случай: строки блока не «ключ: значение», а элементы."""
    block = w.uncomment_template_block(_real_template(), "endpoint_presets")
    assert "\nendpoint_presets:\n" in "\n" + block
    assert "\n  - name: OpenRouter" in "\n" + block


def test_write_values_unfolds_a_commented_section_and_keeps_yaml_valid(tmp_path):
    """Сквозной путь: секции в файле нет вовсе → блок разворачивается из шаблона,
    значение пишется, документ остаётся валидным YAML (шапка с ведущим пробелом
    сломала бы разбор)."""
    import yaml
    from config import _default_config_path
    cfg = tmp_path / "config.yml"
    cfg.write_text("backend:\n  completions_url: http://h/v1/chat/completions\n",
                   encoding="utf-8")
    missing = w.write_values(cfg, {"gc.force_collect": "true"},
                             template_path=_default_config_path())
    assert missing == []
    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert "gc" in data and "force_collect" in data["gc"]   # секция развернулась целиком
    assert data["backend"]["completions_url"] == "http://h/v1/chat/completions"


# ── §6.4 на voice-секции: она в шаблоне тоже закомментирована ──

def test_uncomment_voice_block_keeps_the_canonical_indent():
    """Отступ развёрнутого voice-блока — 2 пробела, как во всём файле.

    Разворот снимает решётку И один пробел за ней, поэтому шаблон обязан писать
    закомментированную секцию формой «# voice:» / «#␣␣␣url:». Прежняя форма
    «#voice:» с двумя пробелами давала после разворота отступ 1: YAML при этом
    валиден, поэтому ни один сторож этого не ловил, — а канон файла и последующая
    дописка ключа блоком из шаблона (у него отступ 2) разъезжались."""
    block = w.uncomment_template_block(_real_template(), "voice")
    assert "\nvoice:\n" in "\n" + block            # шапка без отступа
    assert "\n  provider: openai" in "\n" + block   # поле — ровно два пробела


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
