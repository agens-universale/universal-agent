"""gc-cli на сломанном конфиге: объяснение в stderr вместо трейсбека.

В отличие от tests/test_cli_onboarding_wiring.py (там load_config* подменены
лямбдами), здесь настоящий сломанный файл на диске и настоящий cli.main() —
такого стража на точке входа раньше не было вовсе (см. tests/tui/
test_main_wiring.py: тот же принцип для gc-ua).
"""
import importlib
import sys

# `import cli` парсит sys.argv на импорте — пин валидного argv, чтобы файл
# гонялся детерминированно при любом способе запуска pytest (тот же приём,
# что в test_cli_onboarding_wiring.py).
sys.argv = ["gc-cli", "-q", "x"]

import cli  # noqa: E402


def _reload_arg_parser_with_argv(argv, monkeypatch):
    monkeypatch.setattr(sys, "argv", argv)
    import arg_parser as ap_mod
    importlib.reload(ap_mod)
    return ap_mod


def test_main_prints_explanation_and_exits_1_on_invalid_yaml(monkeypatch, tmp_path, capsys):
    base = tmp_path / "base.yml"
    base.write_text("this is: : : not [valid yaml }\n", encoding="utf-8")

    _reload_arg_parser_with_argv(["gc-cli", "--config", str(base), "-q", "x"], monkeypatch)
    monkeypatch.setattr(cli, "_ensure_project_gc_dir", lambda: None)
    monkeypatch.setattr(cli, "_ensure_user_skills_dir", lambda: None)

    rc = cli.main()

    assert rc == 1
    err = capsys.readouterr().err
    assert "Конфиг не загружается" in err
    assert str(base) in err
    assert "невалидный YAML" in err


def test_main_prints_explanation_for_missing_profile(monkeypatch, tmp_path, capsys):
    """Тот же вход, другая поломка (профиль не найден) — тоже код 1 и объяснение,
    не другой класс исключения/трейсбек."""
    base = tmp_path / "base.yml"
    base.write_text(
        'backend:\n  api_mode: chat_completions\n'
        '  completions_url: "http://127.0.0.1:8014/v1/chat/completions"\n'
        "profile: ghost\n",
        encoding="utf-8",
    )

    _reload_arg_parser_with_argv(["gc-cli", "--config", str(base), "-q", "x"], monkeypatch)
    monkeypatch.setattr(cli, "_ensure_project_gc_dir", lambda: None)
    monkeypatch.setattr(cli, "_ensure_user_skills_dir", lambda: None)

    rc = cli.main()

    assert rc == 1
    err = capsys.readouterr().err
    assert "Конфиг не загружается" in err
    assert 'профиль "ghost" не найден' in err


# --- `--init-config` — «чинилка» обязана работать именно на сломанном конфиге ---


def _isolate_default_config_at(tmp_path, monkeypatch):
    """Подменяет ДЕФОЛТНЫЙ путь пользовательского config.yml (тот, что откроет
    `--init-config`) на tmp_path/config.yml — тем же приёмом, что isolated_env
    в tests/test_config_layered.py."""
    from config_modules import env_vars
    from config_modules.env_vars import EnvVars

    monkeypatch.setattr(env_vars, "env", EnvVars(gc_home=tmp_path))


def test_run_init_reaches_wizard_on_broken_config_interactive(monkeypatch, tmp_path):
    """`--init-config` на РЕАЛЬНОМ сломанном config.yml обязан довести до мастера
    (интерактив), а не упасть тем же трейсбеком, что и обычный запуск — иначе
    штатной «чинилкой» просто нельзя воспользоваться."""
    _isolate_default_config_at(tmp_path, monkeypatch)
    (tmp_path / "config.yml").write_text("this is: : : not [valid yaml }\n", encoding="utf-8")

    monkeypatch.setattr(cli, "_interactive", lambda: True)
    seen = {}
    import onboarding_wizard
    monkeypatch.setattr(
        onboarding_wizard, "run",
        lambda cfg, path, *, review_all, io, mcp=None: seen.update(cfg=cfg) or True,
    )

    assert cli._run_init() == 0
    # Пакетный шаблон, а не пустой словарь: `{}` рантайм не переживает
    # (resolve_threads_dir падает на нём сразу следом), и обе «чинилки» обязаны
    # приземляться на одно и то же — сентинел адреса тот же, каким
    # onboarding_wizard/setup_guide узнают «не настроено».
    import onboarding
    assert seen["cfg"].get("completions_url") == onboarding.URL_SENTINEL


def test_run_init_passive_guide_on_broken_config_non_interactive(monkeypatch, tmp_path, capsys):
    """Без TTY — тот же пустой конфиг доходит до пассивного текстового гайда, а
    не до трейсбека."""
    _isolate_default_config_at(tmp_path, monkeypatch)
    (tmp_path / "config.yml").write_text("this is: : : not [valid yaml }\n", encoding="utf-8")

    monkeypatch.setattr(cli, "_interactive", lambda: False)

    assert cli._run_init() == 0
    err = capsys.readouterr().err
    assert "не читается" in err
    assert "config.yml" in err


def test_run_init_returns_1_when_wizard_answers_cannot_fix_the_file(
    monkeypatch, tmp_path
):
    """Сквозной сценарий находки ревью: файл синтаксически сломан НАСТОЛЬКО, что
    патч мастера отклоняется валидацией ещё до записи (файл не тронут); человек
    честно отвечает на все вопросы; run() возвращает False — и _run_init()
    ОБЯЗАН отразить это в коде возврата, а не молча рапортовать успех, пока
    stderr говорит обратное. Мокается только IO мастера (default_io), не run()
    целиком — reader/printer, как и в test_onboarding_wizard_run.py."""
    _isolate_default_config_at(tmp_path, monkeypatch)
    (tmp_path / "config.yml").write_text("this is: : : not [valid yaml }\n", encoding="utf-8")

    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr("model_picker.fetch_models", lambda *a, **k: None)

    answers = iter([
        "https://x/v1/chat/completions",  # адрес
        "1", "sk-1",                      # авторизация: токен
        "qwen/qwen3-coder",                 # модель
        "", "",                             # уровни — пропустить
    ])
    import onboarding_wizard
    fake_io = onboarding_wizard.WizardIO(
        reader=lambda p, s: next(answers, ""), printer=lambda m: None)
    monkeypatch.setattr(onboarding_wizard, "default_io", lambda: fake_io)

    assert cli._run_init() == 1


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


def test_run_init_keeps_user_connection_when_only_profile_is_broken(monkeypatch, tmp_path):
    """Мастер открывается на НАСТОЯЩИХ адресе и токене человека, а не на
    заглушках шаблона: иначе он ушёл бы в сеть на `your-backend:PORT` и не
    получил список моделей при полностью рабочем токене."""
    _broken_profile_stand(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_interactive", lambda: True)

    seen = {}
    import onboarding_wizard
    monkeypatch.setattr(
        onboarding_wizard, "run",
        lambda cfg, path, *, review_all, io, mcp=None: seen.update(cfg=cfg) or True,
    )

    assert cli._run_init() == 0
    cfg = seen["cfg"]
    assert cfg.get("completions_url") == "https://openrouter.ai/api/v1/chat/completions"
    assert (cfg.get("chat_completion_headers") or {}).get("Authorization") == "Bearer sk-user-token"
    assert list(cfg.get("models") or {}) == ["fast"]


def test_run_init_tells_which_layer_was_dropped(monkeypatch, tmp_path, capsys):
    """Текст деградации приезжает из общего места (`_repair_message`) и называет
    отброшенный профиль — у gc-cli мастер плоский, экран не занимает, поэтому
    stderr здесь достаточен."""
    _broken_profile_stand(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_interactive", lambda: True)

    import onboarding_wizard
    monkeypatch.setattr(
        onboarding_wizard, "run",
        lambda cfg, path, *, review_all, io, mcp=None: True,
    )

    assert cli._run_init() == 0
    err = capsys.readouterr().err
    assert 'профилем "work"' in err
    assert "пакетном шаблоне" not in err     # шаблон не подставлялся — врать нельзя
