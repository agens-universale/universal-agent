"""config_sync: ре-материализация user config.yml из свежего шаблона."""

import pytest
from gc_core import config_sync
from gc_core.config_modules import compat as compat

TEMPLATE = (
    "backend:\n"
    '  completions_url: "http://sentinel"\n'
    "  # kwargs передаются провайдеру как есть\n"
    "  chat_completion_kwargs: {}\n"
    "security:\n"
    "  write_guard:\n"
    "    enabled: true\n"
    '    mode: "ask"\n'
    "  read_guard:\n"
    "    enabled: false\n"
    '    mode: "ask"\n'
    "tui:\n"
    '  stream_display: "minimal"\n'
)

USER = (
    "backend:\n"
    '  completions_url: "http://real/v1"\n'
    "  chat_completion_kwargs:\n"
    "    temperature: 0.2\n"
    "security:\n"
    "  write_guard:\n"
    "    enabled: true\n"
    '    mode: "deny"\n'
    "my_unknown_key: 42\n"
)


def _run_sync(tmp_path, user=USER, template=TEMPLATE):
    cfg = tmp_path / "config.yml"
    cfg.write_text(user, encoding="utf-8")
    tpl = tmp_path / "config.default.yml"
    tpl.write_text(template, encoding="utf-8")
    report = config_sync.sync_config(cfg, tpl, timestamp="20260711-1200")
    return cfg, report


def test_sync_transfers_scalars_path_aware(tmp_path):
    cfg, report = _run_sync(tmp_path)
    out = cfg.read_text(encoding="utf-8")
    assert 'completions_url: "http://real/v1"' in out
    assert 'mode: "deny"' in out  # write_guard юзера
    assert 'mode: "ask"' in out  # read_guard остался шаблонным
    assert 'stream_display: "minimal"' in out  # новый ключ приехал из шаблона


def test_sync_transfers_nested_block_under_inline_leaf(tmp_path):
    cfg, _ = _run_sync(tmp_path)
    out = cfg.read_text(encoding="utf-8")
    assert "temperature: 0.2" in out
    assert "chat_completion_kwargs: {}" not in out


def test_sync_reports_unknown_and_backs_up(tmp_path):
    cfg, report = _run_sync(tmp_path)
    out = cfg.read_text(encoding="utf-8")
    assert "my_unknown_key" not in out
    assert report.unknown == ["my_unknown_key"]
    backup = cfg.parent / "backups" / "config.yml.bak-20260711-1200"
    assert str(backup) == report.backup_path
    assert "my_unknown_key: 42" in backup.read_text(encoding="utf-8")


def test_backup_goes_to_backups_subdir_and_caps(tmp_path):
    from pathlib import Path

    cfg = tmp_path / "config.yml"
    tmpl = Path(config_sync._default_config_path())
    for i in range(12):
        cfg.write_text(tmpl.read_text(encoding="utf-8").replace("\n", f"\n# c{i}\n", 1), encoding="utf-8")
        config_sync.sync_config(cfg, timestamp=f"20260714-0000{i:02d}")
    backups = sorted((tmp_path / "backups").glob("config.yml.bak-*"))
    assert backups and all(b.parent.name == "backups" for b in backups)  # подпапка
    assert len(backups) <= 10  # кап 10


def test_sync_applies_renames(monkeypatch, tmp_path):
    monkeypatch.setattr(compat, "RENAMES", {"backend.legacy_url": "backend.completions_url"})
    user = 'backend:\n  legacy_url: "http://old/v1"\n'
    cfg, report = _run_sync(tmp_path, user=user)
    out = cfg.read_text(encoding="utf-8")
    assert 'completions_url: "http://old/v1"' in out
    assert "legacy_url" not in out
    assert report.renamed == 1


def test_needs_sync_true_for_legacy_llm_section():
    """Секционный RENAMES: лист chat_completions.llm.max_tokens тоже триггерит sync."""
    user = "chat_completions:\n  llm:\n    max_tokens: 1111\n"
    template = 'backend:\n  completions_url: "http://x"\n'
    assert config_sync.needs_sync(user, template) is True


def test_sync_renames_legacy_llm_in_verbatim_section(tmp_path):
    """chat_completions нет в шаблоне — verbatim + финальный rename llm → auxiliary_llm."""
    import re

    import yaml

    user = 'backend:\n  completions_url: "http://real/v1"\nchat_completions:\n  llm:\n    max_tokens: 1111\n'
    cfg, report = _run_sync(tmp_path, user=user)
    out = cfg.read_text(encoding="utf-8")
    assert "auxiliary_llm:" in out
    assert "max_tokens: 1111" in out
    assert re.search(r"(?m)^\s+llm:", out) is None
    parsed = yaml.safe_load(out)
    assert "llm" not in parsed.get("chat_completions", {})
    assert parsed["chat_completions"]["auxiliary_llm"]["max_tokens"] == 1111
    assert report.renamed >= 1


def test_sync_renames_legacy_llm_when_template_has_auxiliary(tmp_path):
    """Шаблон уже с auxiliary_llm — line-copy с rename секции."""
    import re

    import yaml

    template = (
        'backend:\n  completions_url: "http://sentinel"\nchat_completions:\n  auxiliary_llm:\n    max_tokens: 8192\n'
    )
    user = 'backend:\n  completions_url: "http://real/v1"\nchat_completions:\n  llm:\n    max_tokens: 1111\n'
    cfg, report = _run_sync(tmp_path, user=user, template=template)
    out = cfg.read_text(encoding="utf-8")
    assert "auxiliary_llm:" in out
    assert "max_tokens: 1111" in out
    assert re.search(r"(?m)^\s+llm:", out) is None
    parsed = yaml.safe_load(out)
    assert "llm" not in parsed.get("chat_completions", {})
    assert report.renamed >= 1


def test_sync_removed_key_stays_only_in_backup(monkeypatch, tmp_path):
    monkeypatch.setattr(compat, "REMOVED", {"my_unknown_key": "удалён"})
    cfg, report = _run_sync(tmp_path)
    assert report.removed == ["my_unknown_key"]
    assert report.unknown == []
    assert "my_unknown_key" not in cfg.read_text(encoding="utf-8")


def test_needs_sync_true_when_template_has_new_keys():
    assert config_sync.needs_sync(USER, TEMPLATE) is True


def test_needs_sync_false_when_nothing_to_update():
    assert config_sync.needs_sync(TEMPLATE, TEMPLATE) is False


def test_sync_real_template_over_its_own_copy(tmp_path):
    """Sync копии реального шаблона на сам шаблон: без unknown, файл валиден."""
    from pathlib import Path as P

    from gc_core.config import _default_config_path

    real = P(_default_config_path()).read_text(encoding="utf-8")
    cfg = tmp_path / "config.yml"
    cfg.write_text(real, encoding="utf-8")
    report = config_sync.sync_config(cfg, _default_config_path(), timestamp="t")
    assert report.unknown == []
    assert report.reenter == []


# --- C1: валидные секции вне шаблона переносятся verbatim ---


def test_sync_valid_section_outside_template_transferred(tmp_path):
    """working_memory (валидно по схеме, нет в шаблоне) → в результате, не в unknown."""
    user = 'backend:\n  completions_url: "http://real/v1"\nworking_memory:\n  max_chars_goal: 123\n'
    cfg, report = _run_sync(tmp_path, user=user)
    out = cfg.read_text(encoding="utf-8")
    assert "max_chars_goal: 123" in out
    assert report.unknown == []
    assert report.transferred >= 2  # backend + working_memory


def test_sync_invalid_section_outside_template_stays_unknown(tmp_path):
    """Настоящий мусор (my_unknown_key) по-прежнему в unknown."""
    user = 'backend:\n  completions_url: "http://real/v1"\nmy_unknown_key: 42\n'
    cfg, report = _run_sync(tmp_path, user=user)
    out = cfg.read_text(encoding="utf-8")
    assert "my_unknown_key" not in out
    assert "my_unknown_key" in report.unknown


# --- I2: needs_sync идемпотентность с непустым chat_completion_kwargs ---


def test_needs_sync_idempotent_after_sync_with_kwargs(tmp_path):
    """После sync файл с chat_completion_kwargs не требует повторного sync."""
    user_with_kwargs = (
        "backend:\n"
        '  completions_url: "http://real/v1"\n'
        "  chat_completion_kwargs:\n"
        "    temperature: 0.2\n"
        "security:\n"
        "  write_guard:\n"
        "    enabled: true\n"
        '    mode: "ask"\n'
        "  read_guard:\n"
        "    enabled: false\n"
        '    mode: "ask"\n'
        "tui:\n"
        '  stream_display: "minimal"\n'
    )
    assert config_sync.needs_sync(user_with_kwargs, TEMPLATE) is False


# --- I3: битый YAML в needs_sync и sync_config → ValueError, не трейсбек ---


def test_needs_sync_raises_value_error_on_broken_yaml():
    broken = "backend:\n  completions_url: [unclosed\n"
    with pytest.raises(ValueError, match="невалидный YAML"):
        config_sync.needs_sync(broken, TEMPLATE)


def test_sync_config_raises_value_error_on_broken_yaml(tmp_path):
    broken = "backend:\n  completions_url: [unclosed\n"
    cfg = tmp_path / "config.yml"
    cfg.write_text(broken, encoding="utf-8")
    tpl = tmp_path / "config.default.yml"
    tpl.write_text(TEMPLATE, encoding="utf-8")
    with pytest.raises(ValueError, match="невалидный YAML"):
        config_sync.sync_config(cfg, tpl)


def test_sync_keeps_new_template_keys_when_user_has_key_outside_template(tmp_path):
    """Ключ юзера вне шаблона не утаскивает за собой всю секцию.

    Корп-сетап: у юзера в backend: заданы cert_path/key_path (mTLS — схема их знает,
    в шаблоне их нет). Прямого якоря для них в шаблоне нет, поэтому sync поднимается
    к предку и переносит ВЕСЬ блок backend: юзера поверх шаблонного. Новый ключ
    шаблона в той же секции (request_timeout_sec) при этом терялся: sync отчитывался
    «✓ обновлён», а needs_sync сразу после него снова возвращал True — агент вечно
    предлагал обновить конфиг и плодил бэкап на каждом прогоне.
    """
    tmpl = (
        "backend:\n"
        '  completions_url: "http://sentinel"\n'
        "  # HTTP-таймаут одного запроса к LLM\n"
        "  request_timeout_sec: 600\n"
        "tui:\n"
        '  stream_display: "minimal"\n'
    )
    user = (
        "backend:\n"
        '  completions_url: "http://real/v1"\n'
        '  cert_path: "/etc/ssl/client.crt"\n'
        '  key_path: "/etc/ssl/client.key"\n'
        "tui:\n"
        '  stream_display: "minimal"\n'
    )
    t = tmp_path / "config.default.yml"
    t.write_text(tmpl, encoding="utf-8")
    u = tmp_path / "config.yml"
    u.write_text(user, encoding="utf-8")

    config_sync.sync_config(u, t)
    out = u.read_text(encoding="utf-8")

    assert "http://real/v1" in out  # значения юзера целы
    assert "/etc/ssl/client.crt" in out  # ключ вне шаблона сохранён
    assert "/etc/ssl/client.key" in out
    assert "request_timeout_sec: 600" in out  # новый ключ шаблона не потерян
    assert config_sync.needs_sync(out, tmpl) is False  # и sync идемпотентен
