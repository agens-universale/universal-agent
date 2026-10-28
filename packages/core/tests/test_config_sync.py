"""config_sync: ре-материализация user config.yml из свежего шаблона."""

import pytest
import yaml
from gc_core import config_sync
from gc_core.config import _default_config_path
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
        cfg.write_text(
            tmpl.read_text(encoding="utf-8").replace("\n", f"\n# c{i}\n", 1),
            encoding="utf-8",
        )
        config_sync.sync_config(cfg, timestamp=f"20260714-0000{i:02d}")
    backups = sorted((tmp_path / "backups").glob("config.yml.bak-*"))
    assert backups and all(
        b.parent.name == "backups" for b in backups
    )  # подпапка
    assert len(backups) <= 10  # кап 10


def test_sync_applies_renames(monkeypatch, tmp_path):
    monkeypatch.setattr(
        compat, "RENAMES", {"backend.legacy_url": "backend.completions_url"}
    )
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

    template = 'backend:\n  completions_url: "http://sentinel"\nchat_completions:\n  auxiliary_llm:\n    max_tokens: 8192\n'
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
    report = config_sync.sync_config(
        cfg, _default_config_path(), timestamp="t"
    )
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
    user = (
        'backend:\n  completions_url: "http://real/v1"\nmy_unknown_key: 42\n'
    )
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


def test_sync_keeps_new_template_keys_when_user_has_key_outside_template(
    tmp_path,
):
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


# --- поля backend, неприменимые на модели (REMOVED с `*`) ---


def test_sync_keeps_models_and_drops_model_level_backend_fields(tmp_path):
    """Модель с `backend.stream` переносится целиком без этого поля, а не
    уезжает в бэкап вся (строгая проверка поддерева раньше отвергала секцию)."""
    from pathlib import Path as P

    from gc_core.config import _default_config_path, load_settings

    user = (
        "models:\n"
        "  flash:\n"
        "    model: qwen\n"
        "    url: http://h/v1/chat/completions\n"
        "    token: t\n"
        "    backend:\n"
        "      stream: false\n"
        "      use_retry: true\n"
        "  plain:\n"
        "    model: p\n"
        "    backend:\n"
        "      api_mode: chat_completions\n"
        "slots:\n"
        "  default: flash\n"
    )
    cfg = tmp_path / "config.yml"
    cfg.write_text(user, encoding="utf-8")
    report = config_sync.sync_config(
        cfg, _default_config_path(), timestamp="t"
    )
    assert report.unknown == []
    assert sorted(report.removed) == [
        "models.flash.backend.stream",
        "models.plain.backend.api_mode",
    ]
    out = P(cfg).read_text(encoding="utf-8")
    assert (
        "stream: false" not in out
        and "api_mode: chat_completions\n" not in out.split("\nmodels:")[1]
    )
    loaded = load_settings(str(cfg))
    assert sorted(loaded.settings.models) == ["flash", "plain"]
    assert loaded.settings.models["flash"].backend.use_retry is True
    assert not any("models." in n for n in loaded.runtime.notices)


def test_needs_sync_true_for_model_level_backend_field():
    from pathlib import Path as P

    from gc_core.config import _default_config_path

    template = P(_default_config_path()).read_text(encoding="utf-8")
    user = (
        template
        + "\nmodels:\n  m:\n    model: x\n    backend:\n      stream: false\n"
    )
    assert config_sync.needs_sync(user, template) is True


def test_sync_moves_the_old_diff_tui_section_to_agent_edits(tmp_path):
    """`--sync-config` переносит старую секцию под новое имя вместе со старыми листьями
    — без мусора и без повторного требования синка."""
    import yaml
    from gc_testkit.paths import ASSETS

    template = (ASSETS / "config.default.yml").read_text(encoding="utf-8")
    user = (
        "diff_tui:\n"
        "  cache_max_orig: 42\n"
        "  preview_context_lines: 50\n"
        "  hunk_context_lines: 5\n"
    )
    cfg, _ = _run_sync(tmp_path, user=user, template=template)
    out = cfg.read_text(encoding="utf-8")
    parsed = yaml.safe_load(out)

    assert "diff_tui" not in parsed
    assert parsed["agent_edits"]["snapshots_max_files"] == 42
    assert parsed["agent_edits"]["hunk_context_lines"] == 5
    assert "preview_context_lines" not in parsed["agent_edits"]
    assert config_sync.needs_sync(out, template) is False


@pytest.mark.parametrize("new_first", [True, False])
def test_sync_keeps_the_new_name_when_both_sections_are_in_one_file(
    tmp_path, new_first
):
    """Секция под обоими именами в одном файле: после sync действует то же, что
    до него. Загрузчик сливает старую под новую, заданное в новой побеждает —
    sync обязан прийти к тому же, в каком бы порядке секции ни стояли в файле
    (раньше исход решал порядок: новое 99 молча становилось старым 11)."""
    import yaml
    from gc_testkit.paths import ASSETS

    template = (ASSETS / "config.default.yml").read_text(encoding="utf-8")
    new = "agent_edits:\n  trace_diff_preview_lines: 99\n"
    old = (
        "diff_tui:\n"
        "  trace_diff_preview_lines: 11\n"
        "  replay_expanded_turns: 4\n"
    )
    user = new + old if new_first else old + new
    cfg, _ = _run_sync(tmp_path, user=user, template=template)
    parsed = yaml.safe_load(cfg.read_text(encoding="utf-8"))

    assert "diff_tui" not in parsed
    assert parsed["agent_edits"]["trace_diff_preview_lines"] == 99
    # лист, заданный только старым именем, не теряется — как у загрузчика
    assert parsed["agent_edits"]["replay_expanded_turns"] == 4


def test_sync_strips_exact_removed_key_inside_section_outside_template(
    tmp_path,
):
    """Точный REMOVED-ключ в блоке вне шаблона (assign) не переезжает в файл
    вместе с блоком, попадает в removed один раз, повторный синк ничего не меняет."""
    user = (
        "models:\n"
        "  flash:\n"
        "    model: qwen\n"
        "    url: http://h/v1/chat/completions\n"
        "    token: t\n"
        "slots:\n"
        "  default: flash\n"
        "assign:\n"
        "  auto_judge: max\n"
        "  finish_gate: default\n"
    )
    cfg = tmp_path / "config.yml"
    cfg.write_text(user, encoding="utf-8")
    report = config_sync.sync_config(
        cfg, _default_config_path(), timestamp="t1"
    )
    assert report.removed == ["assign.auto_judge"]
    out = cfg.read_text(encoding="utf-8")
    assert "auto_judge" not in out
    assert "finish_gate: default" in out

    again = config_sync.sync_config(
        cfg, _default_config_path(), timestamp="t2"
    )
    assert again.removed == []
    assert yaml.safe_load(cfg.read_text(encoding="utf-8")) == yaml.safe_load(
        out
    )


def test_sync_removed_key_does_not_leave_empty_section_over_template_defaults(
    tmp_path,
):
    """Секция, опустевшая после вырезания REMOVED-ключа, не перекрывает
    умолчания шаблона пустым значением юзера."""
    user = (
        "update:\n"
        "  check_interval_days: 3\n"
        "logging:\n"
        "  level: INFO\n"
        "  dump_storage: true\n"
    )
    cfg = tmp_path / "config.yml"
    cfg.write_text(user, encoding="utf-8")
    report = config_sync.sync_config(
        cfg, _default_config_path(), timestamp="t"
    )
    assert report.removed == [
        "update.check_interval_days",
        "logging.dump_storage",
    ]
    out = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert out["update"]["check"] == "auto"
    assert "dump_storage" not in out["logging"]
    assert out["logging"]["level"] == "INFO"


def test_strip_removed_drops_emptied_top_level_section_from_text():
    """Опустевшая секция верхнего уровня уходит и из текста, не только из
    данных: иначе ``update: {}`` осталось бы в тексте юзера."""
    text = "update:\n  check_interval_days: 3\nlogging:\n  level: INFO\n"
    raw = yaml.safe_load(text)
    out_text, stripped = config_sync._strip_removed_patterns(text, raw)
    assert stripped == ["update.check_interval_days"]
    assert out_text == "logging:\n  level: INFO\n"
    assert raw == {"logging": {"level": "INFO"}}


def test_sync_drops_old_trace_inline_max_lines(tmp_path):
    """Ключ со сменившимся смыслом sync не переносит: старое значение — только
    в бэкапе, в файле — новый ключ с умолчанием шаблона."""
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        "agent_edits:\n  trace_inline_max_lines: 30\n", encoding="utf-8"
    )
    report = config_sync.sync_config(
        cfg, _default_config_path(), timestamp="t"
    )

    assert report.removed == ["agent_edits.trace_inline_max_lines"]
    edits = yaml.safe_load(cfg.read_text(encoding="utf-8"))["agent_edits"]
    assert "trace_inline_max_lines" not in edits
    assert edits["trace_diff_preview_lines"] == 10
