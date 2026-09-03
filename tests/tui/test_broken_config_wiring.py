"""gc-ua на сломанном конфиге: объяснение в stderr вместо трейсбека, TuiApp не
создаётся (падение случается ДО неё — собственный crash-репорт Textual тут не
поможет, см. tests/tui/test_main_wiring.py — тот же принцип для happy path)."""
import pytest


def _fake_app(monkeypatch, created):
    from tui import app as tui_app

    class _FakeApp:
        def __init__(self, **kw):
            created.append(kw)

        def set_config_path(self, path):
            pass

        def _maybe_notice_profile_activation(self, *a, **k):
            pass

        def run(self):
            return None

    monkeypatch.setattr(tui_app, "TuiApp", _FakeApp)


def test_main_prints_explanation_and_exits_1_on_invalid_yaml(monkeypatch, tmp_path, capsys):
    pytest.importorskip("textual")

    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text("this is: : : not [valid yaml }\n", encoding="utf-8")

    created: list = []
    _fake_app(monkeypatch, created)

    from tui import main
    rc = main(argv=["--config", str(cfg_path)])

    assert rc == 1
    assert created == []  # TuiApp не создан — падение случилось раньше
    err = capsys.readouterr().err
    assert "Конфиг не загружается" in err
    assert str(cfg_path) in err
    assert "невалидный YAML" in err


def test_main_prints_explanation_for_missing_profile(monkeypatch, tmp_path, capsys):
    pytest.importorskip("textual")

    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(
        'backend:\n  api_mode: chat_completions\n'
        '  completions_url: "http://127.0.0.1:8014/v1/chat/completions"\n'
        "profile: ghost\n",
        encoding="utf-8",
    )

    created: list = []
    _fake_app(monkeypatch, created)

    from tui import main
    rc = main(argv=["--config", str(cfg_path)])

    assert rc == 1
    assert created == []
    err = capsys.readouterr().err
    assert "Конфиг не загружается" in err
    assert 'профиль "ghost" не найден' in err


# --- `--init-config` — «чинилка» обязана работать именно на сломанном конфиге ---


def _isolate_default_config_at(tmp_path, monkeypatch):
    from config_modules import env_vars
    from config_modules.env_vars import EnvVars

    monkeypatch.setattr(env_vars, "env", EnvVars(gc_home=tmp_path))


def test_init_config_passive_guide_on_broken_config_non_interactive(
    monkeypatch, tmp_path, capsys
):
    """`gc-ua --init-config` на РЕАЛЬНОМ сломанном config.yml (non-interactive)
    обязан довести до пассивного гайда, а не упасть тем же трейсбеком, что и
    обычный запуск — `load_config_ex(None)` внутри ветки init-config игнорирует
    --config и всегда целится в дефолтный путь, поэтому поломка изолируется
    подменой env_vars.env, а не флагом --config."""
    pytest.importorskip("textual")
    _isolate_default_config_at(tmp_path, monkeypatch)
    (tmp_path / "config.yml").write_text("this is: : : not [valid yaml }\n", encoding="utf-8")

    created: list = []
    _fake_app(monkeypatch, created)

    import tui as tui_mod
    monkeypatch.setattr(tui_mod, "_interactive", lambda: False, raising=False)

    from tui import main
    rc = main(argv=["--init-config"])

    assert rc == 0
    assert created == []
    err = capsys.readouterr().err
    assert "не читается" in err
    assert "config.yml" in err


def test_init_config_reaches_review_mode_on_broken_config_interactive(
    monkeypatch, tmp_path
):
    """Тот же вход, интерактивная ветка: доходит до app._onboarding_mode='review'
    и до app.run(), а не падает на resolve_threads_dir() — `{}` не годится
    основой (рантайм требует threads_data_path, которого в пустом словаре нет),
    поэтому восстановление обязано опираться на ВАЛИДНЫЙ пакетный шаблон.

    resolve_threads_dir НЕ замокан намеренно (ревью нашло: прежняя версия теста
    мокала его лямбдой и тем самым прятала настоящий крах на пустом `{}`)."""
    pytest.importorskip("textual")
    _isolate_default_config_at(tmp_path, monkeypatch)
    (tmp_path / "config.yml").write_text("this is: : : not [valid yaml }\n", encoding="utf-8")

    captured: dict = {}
    from tui import app as tui_app

    class _FakeApp:
        def __init__(self, **kw):
            captured["config"] = kw.get("config")

        def set_config_path(self, path):
            pass

        def _maybe_notice_profile_activation(self, *a, **k):
            captured["notice_args"] = a

        def run(self):
            captured["ran"] = True

    monkeypatch.setattr(tui_app, "TuiApp", _FakeApp)

    import tui as tui_mod
    monkeypatch.setattr(tui_mod, "_interactive", lambda: True, raising=False)

    import mcp_daemon_autostart as mcp_da_mod
    monkeypatch.setattr(mcp_da_mod, "ensure_mcp_daemons", lambda cfg: None, raising=False)
    monkeypatch.setattr(mcp_da_mod, "shutdown_mcp_daemons", lambda: None, raising=False)

    from tui import main
    rc = main(argv=["--init-config"])

    assert rc == 0
    assert captured.get("ran") is True
    # Пакетный шаблон, не пользовательские данные и не пустой словарь: сентинел
    # адреса — тот же, каким onboarding_wizard/setup_guide узнают «не настроено».
    import onboarding
    assert captured["config"].get("completions_url") == onboarding.URL_SENTINEL
    assert captured["notice_args"][0] == ""  # активного профиля нет — восстановление сняло его


# --- `--profile` у gc-ua: обход поломки, унесённой активным профилем ---------


def test_profile_empty_flag_recovers_config_broken_by_active_profile(monkeypatch, tmp_path):
    """Без --profile "" конфиг не грузится вовсе (активный профиль ссылается на
    несуществующий алиас) — ровно совет, который печатает explain_broken_config.
    С флагом --profile "" (спека §9.1: пустая строка = не мержить профиль
    вовсе) профильный слой временно снят, и та же база грузится."""
    pytest.importorskip("textual")

    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    base = tmp_path / "config.yml"
    base.write_text(
        'backend:\n  api_mode: chat_completions\n'
        '  completions_url: "http://127.0.0.1:8014/v1/chat/completions"\n'
        "models:\n  flash: {}\n"
        "profile: work\n"
        "paths:\n"
        f'  threads_data_path: "{threads_dir.as_posix()}"\n',
        encoding="utf-8",
    )
    (profiles_dir / "work.yml").write_text("slots:\n  default: strong\n", encoding="utf-8")

    from tui import main

    # Без флага — падает (Section 3: объяснение вместо трейсбека, TuiApp не создан).
    created_broken: list = []
    _fake_app(monkeypatch, created_broken)
    rc_broken = main(argv=["--config", str(base)])
    assert rc_broken == 1
    assert created_broken == []

    # С флагом --profile "" — грузится и доходит до TuiApp.
    created_ok: list = []
    _fake_app(monkeypatch, created_ok)
    monkeypatch.setattr("mcp_daemon_autostart.ensure_mcp_daemons", lambda cfg: None)
    monkeypatch.setattr("mcp_daemon_autostart.shutdown_mcp_daemons", lambda: None)
    rc_ok = main(argv=["--config", str(base), "--profile", ""])
    assert rc_ok == 0
    assert len(created_ok) == 1


# --- Сломан ТОЛЬКО профиль: данные человека не выбрасываются ----------------

_STAND_BASE = (
    'profile: "work"\n'
    "backend:\n"
    "  api_mode: chat_completions\n"
    '  completions_url: "https://openrouter.ai/api/v1/chat/completions"\n'
    "  chat_completion_headers:\n"
    '    Authorization: "Bearer sk-user-token"\n'
    "models:\n"
    "  fast:\n"
    '    model: "qwen/qwen3-8b"\n'
)


def _broken_profile_stand(tmp_path, monkeypatch):
    """Раскладка живого смока: рабочие адрес/токен/реестр в базе, нерезолвимый
    слот в профиле — весь стек не грузится, но виноват только профиль."""
    _isolate_default_config_at(tmp_path, monkeypatch)
    (tmp_path / "config.yml").write_text(_STAND_BASE, encoding="utf-8")
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "work.yml").write_text(
        'slots:\n  default: "strong"\n', encoding="utf-8")
    monkeypatch.setattr("mcp_daemon_autostart.ensure_mcp_daemons", lambda cfg: None)
    monkeypatch.setattr("mcp_daemon_autostart.shutdown_mcp_daemons", lambda: None)
    import tui as tui_mod
    monkeypatch.setattr(tui_mod, "_interactive", lambda: True, raising=False)


def _capturing_app(monkeypatch):
    captured: dict = {}
    from tui import app as tui_app

    class _FakeApp:
        def __init__(self, **kw):
            captured["config"] = kw.get("config")

        def set_config_path(self, path):
            pass

        def _maybe_notice_profile_activation(self, *a, **k):
            pass

        def run(self):
            captured["ran"] = True

    monkeypatch.setattr(tui_app, "TuiApp", _FakeApp)
    return captured


@pytest.fixture
def clean_notices():
    import runtime_notices
    runtime_notices.clear_runtime_notices()
    yield runtime_notices
    runtime_notices.clear_runtime_notices()


def test_init_config_keeps_user_connection_when_only_profile_is_broken(
    monkeypatch, tmp_path, clean_notices
):
    """Мастер открывается на НАСТОЯЩИХ адресе, токене и реестре человека, а не на
    заглушках пакетного шаблона: на шаблоне мастер уходил в сеть на
    `your-backend:PORT` и не получал список моделей при рабочем токене, а сводка
    шага «Подключение» расходилась с полем под ней (сводка — из шаблона, поле —
    из сырого файла человека)."""
    pytest.importorskip("textual")
    _broken_profile_stand(tmp_path, monkeypatch)
    captured = _capturing_app(monkeypatch)

    from tui import main
    rc = main(argv=["--init-config"])

    assert rc == 0
    assert captured.get("ran") is True
    cfg = captured["config"]
    assert cfg.get("completions_url") == "https://openrouter.ai/api/v1/chat/completions"
    assert (cfg.get("chat_completion_headers") or {}).get("Authorization") == "Bearer sk-user-token"
    assert list(cfg.get("models") or {}) == ["fast"]


def test_init_config_degradation_reaches_notices(monkeypatch, tmp_path, clean_notices):
    """Задача D: сообщение обязано пережить старт TUI. В stderr его через мгновение
    стирает альтернативный буфер Textual — живой смок подтвердил, что человек не
    видит НИ СТРОЧКИ и не понимает, почему мастер ведёт себя странно. Значит, оно
    едет тем же каналом, что и остальные рантайм-уведомления (F4 / `/notices`)."""
    pytest.importorskip("textual")
    _broken_profile_stand(tmp_path, monkeypatch)
    _capturing_app(monkeypatch)

    from tui import main
    assert main(argv=["--init-config"]) == 0

    degraded = [n for n in clean_notices.collect_runtime_notices()
                if n.code == "config_load_degraded"]
    assert len(degraded) == 1
    assert degraded[0].level == "warning"
    assert 'профилем "work"' in degraded[0].message
    # Не прочитано → бейдж /notices зажжётся.
    assert degraded[0] in clean_notices.collect_unread_runtime_notices()


def test_healthy_config_leaves_no_degradation_notice(monkeypatch, tmp_path, clean_notices):
    """Здоровый конфиг — ступень 1: ни уведомления, ни строчки в stderr."""
    pytest.importorskip("textual")
    _broken_profile_stand(tmp_path, monkeypatch)
    (tmp_path / "profiles" / "work.yml").write_text(
        'slots:\n  default: "fast"\n', encoding="utf-8")
    _capturing_app(monkeypatch)

    from tui import main
    assert main(argv=["--init-config"]) == 0

    assert [n for n in clean_notices.collect_runtime_notices()
            if n.code == "config_load_degraded"] == []
