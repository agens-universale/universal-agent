"""gc-cli на сломанном конфиге: объяснение в stderr вместо трейсбека.

В отличие от tests/test_cli_onboarding_wiring.py (там load_config* подменены
лямбдами), здесь настоящий сломанный файл на диске и настоящий cli.main() —
такого стража на точке входа раньше не было вовсе (см. tests/tui/
test_main_wiring.py: тот же принцип для gc-ua).
"""

import getpass
import importlib
import os
import subprocess
import sys

import pytest

# `import cli` парсит sys.argv на импорте — пин валидного argv, чтобы файл
# гонялся детерминированно при любом способе запуска pytest (тот же приём,
# что в test_cli_onboarding_wiring.py).
sys.argv = ["gc-cli", "-q", "x"]

from gc_cli import cli
from gc_core.settings import settings_of


def _make_layer_unreadable(path, request):
    """Настоящий нечитаемый слой: файл НА МЕСТЕ (его пропускает `isfile`, которым
    слой и отбирается), а `open()` отвечает OSError — права, каталог на месте
    файла, отвалившийся сетевой диск. Мока намеренно нет: сторож заводили ровно
    ради этого исхода, и подменённый `open()` доказывал бы только сам себя.
    Права возвращаются финализатором — иначе tmp_path не уберётся."""
    if os.name == "nt":
        user = getpass.getuser()
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/deny", f"{user}:(R)"],
            capture_output=True,
        )
        request.addfinalizer(
            lambda: subprocess.run(
                ["icacls", str(path), "/remove:d", user], capture_output=True
            )
        )
    else:
        os.chmod(path, 0)
        request.addfinalizer(lambda: os.chmod(path, 0o600))
    try:
        path.read_text(encoding="utf-8")
    except OSError:
        return
    pytest.skip("слой остался читаемым (права ФС) — стенд негоден")


def _reload_arg_parser_with_argv(argv, monkeypatch):
    monkeypatch.setattr(sys, "argv", argv)
    from gc_cli import arg_parser as ap_mod

    importlib.reload(ap_mod)
    return ap_mod


def test_main_prints_explanation_and_exits_1_on_invalid_yaml(
    monkeypatch, tmp_path, capsys
):
    base = tmp_path / "base.yml"
    base.write_text("this is: : : not [valid yaml }\n", encoding="utf-8")

    _reload_arg_parser_with_argv(
        ["gc-cli", "--config", str(base), "-q", "x"], monkeypatch
    )
    monkeypatch.setattr(cli, "_ensure_project_gc_dir", lambda: None)
    monkeypatch.setattr(cli, "_ensure_user_skills_dir", lambda: None)

    rc = cli.main()

    assert rc == 1
    err = capsys.readouterr().err
    assert "Конфиг не загружается" in err
    assert str(base) in err
    assert "невалидный YAML" in err


def test_main_prints_explanation_for_missing_profile(
    monkeypatch, tmp_path, capsys
):
    """Тот же вход, другая поломка (профиль не найден) — тоже код 1 и объяснение,
    не другой класс исключения/трейсбек."""
    base = tmp_path / "base.yml"
    base.write_text(
        "backend:\n  api_mode: chat_completions\n"
        '  completions_url: "http://127.0.0.1:8014/v1/chat/completions"\n'
        "profile: ghost\n",
        encoding="utf-8",
    )

    _reload_arg_parser_with_argv(
        ["gc-cli", "--config", str(base), "-q", "x"], monkeypatch
    )
    monkeypatch.setattr(cli, "_ensure_project_gc_dir", lambda: None)
    monkeypatch.setattr(cli, "_ensure_user_skills_dir", lambda: None)

    rc = cli.main()

    assert rc == 1
    err = capsys.readouterr().err
    assert "Конфиг не загружается" in err
    assert 'профиль "ghost" не найден' in err


def test_main_explains_the_profile_from_the_flag_not_the_pointer(
    monkeypatch, tmp_path, capsys
):
    """Ревью (H1): `--profile broken`, сломан именно он — объяснение резолвило
    профиль указателем из файла (его нет), карта называла одну базу, совет —
    «поправить slots.default в базовом файле» (где ключ верный), подсказки
    `--profile ""` не было. Объяснение обязано строиться по профилю, который
    действовал в сессии."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "gc_core.config_modules.env_vars.managed_config_path",
        lambda: tmp_path / "config.local.yml",
    )
    base = tmp_path / "base.yml"
    base.write_text(
        "backend:\n  api_mode: chat_completions\n"
        '  completions_url: "http://127.0.0.1:8014/v1/chat/completions"\n'
        "models:\n  flash: {}\n",
        encoding="utf-8",
    )
    (tmp_path / "profiles").mkdir()
    profile = tmp_path / "profiles" / "broken.yml"
    profile.write_text("slots:\n  default: strong\n", encoding="utf-8")

    _reload_arg_parser_with_argv(
        ["gc-cli", "--config", str(base), "--profile", "broken", "-q", "x"],
        monkeypatch,
    )
    monkeypatch.setattr(cli, "_ensure_project_gc_dir", lambda: None)
    monkeypatch.setattr(cli, "_ensure_user_skills_dir", lambda: None)

    rc = cli.main()

    assert rc == 1
    err = capsys.readouterr().err
    assert f"поправить slots.default в:  {profile}" in err
    assert f"в:  {base}" not in err
    assert f"{profile}   (активен" in err
    # Сессия шла с --config — совет обязан нести его (M9), иначе `--profile ""`
    # запустит не тот конфиг.
    assert f'gc-cli --config "{base}" --profile ""' in err


def test_main_names_the_flag_when_the_profile_in_it_does_not_exist(
    monkeypatch, tmp_path, capsys
):
    """Ревью (H1): опечатка в `--profile` — советовали «поправить файл: <база>» и
    `--reset-config`, которые ни при чём. Сказать прямо: имя пришло из флага,
    такого профиля нет; исправить имя или запустить без профиля."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "gc_core.config_modules.env_vars.managed_config_path",
        lambda: tmp_path / "config.local.yml",
    )
    base = tmp_path / "base.yml"
    base.write_text(
        "backend:\n  api_mode: chat_completions\n"
        '  completions_url: "http://127.0.0.1:8014/v1/chat/completions"\n'
        "models:\n  flash: {}\n",
        encoding="utf-8",
    )
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "work.yml").write_text(
        "slots:\n  default: flash\n", encoding="utf-8"
    )

    _reload_arg_parser_with_argv(
        ["gc-cli", "--config", str(base), "--profile", "wrok", "-q", "x"],
        monkeypatch,
    )
    monkeypatch.setattr(cli, "_ensure_project_gc_dir", lambda: None)
    monkeypatch.setattr(cli, "_ensure_user_skills_dir", lambda: None)

    rc = cli.main()

    assert rc == 1
    err = capsys.readouterr().err
    assert 'профиль "wrok" не найден' in err
    assert "--profile" in err.split("Что сделать:")[1]
    assert "поправить файл" not in err
    assert "--reset-config" not in err
    assert (
        f'gc-cli --config "{base}" --profile ""' in err
    )  # --config сессии (M9)


# --- `--init-config` — «чинилка» обязана работать именно на сломанном конфиге ---


def _isolate_default_config_at(tmp_path, monkeypatch):
    """Подменяет ДЕФОЛТНЫЙ путь пользовательского config.yml (тот, что откроет
    `--init-config`) на tmp_path/config.yml — тем же приёмом, что isolated_env
    в tests/test_config_layered.py."""
    from gc_core.config_modules import env_vars
    from gc_core.config_modules.env_vars import EnvVars

    monkeypatch.setattr(env_vars, "env", EnvVars(gc_home=tmp_path))


def test_run_init_reaches_wizard_on_broken_config_interactive(
    monkeypatch, tmp_path
):
    """`--init-config` на РЕАЛЬНОМ сломанном config.yml обязан довести до мастера
    (интерактив), а не упасть тем же трейсбеком, что и обычный запуск — иначе
    штатной «чинилкой» просто нельзя воспользоваться."""
    _isolate_default_config_at(tmp_path, monkeypatch)
    (tmp_path / "config.yml").write_text(
        "this is: : : not [valid yaml }\n", encoding="utf-8"
    )

    monkeypatch.setattr(cli, "_interactive", lambda: True)
    seen = {}
    from gc_cli import onboarding_wizard

    monkeypatch.setattr(
        onboarding_wizard,
        "run",
        lambda cfg, path, *, review_all, io, mcp=None, deactivate_profile=False: (
            seen.update(cfg=cfg) or True
        ),
    )

    assert cli._run_init() == 0
    # Пакетный шаблон, а не пустой словарь: `{}` рантайм не переживает
    # (resolve_threads_dir падает на нём сразу следом), и обе «чинилки» обязаны
    # приземляться на одно и то же — сентинел адреса тот же, каким
    # onboarding_wizard/setup_guide узнают «не настроено».
    from gc_core import onboarding

    assert (
        settings_of(seen["cfg"]).backend.completions_url
        == onboarding.URL_SENTINEL
    )


def test_run_init_passive_guide_on_broken_config_non_interactive(
    monkeypatch, tmp_path, capsys
):
    """Без TTY — тот же пустой конфиг доходит до пассивного текстового гайда, а
    не до трейсбека."""
    _isolate_default_config_at(tmp_path, monkeypatch)
    (tmp_path / "config.yml").write_text(
        "this is: : : not [valid yaml }\n", encoding="utf-8"
    )

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
    (tmp_path / "config.yml").write_text(
        "this is: : : not [valid yaml }\n", encoding="utf-8"
    )

    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(
        "gc_core.model_picker.fetch_models", lambda *a, **k: None
    )

    answers = iter(
        [
            "https://x/v1/chat/completions",  # адрес
            "1",
            "sk-1",  # авторизация: токен
            "qwen/qwen3-coder",  # модель
            "",
            "",  # уровни — пропустить
        ]
    )
    from gc_cli import onboarding_wizard

    fake_io = onboarding_wizard.WizardIO(
        reader=lambda p, s: next(answers, ""), printer=lambda m: None
    )
    monkeypatch.setattr(onboarding_wizard, "default_io", lambda: fake_io)

    assert cli._run_init() == 1


def test_run_init_refuses_before_questions_when_a_pointer_survives_the_template_stage(
    monkeypatch, tmp_path, capsys
):
    """Ревью (M10): ступень шаблона + живой указатель профиля — мастер вёл через
    все вопросы (с вводом токена) к отказу записи: гейт судит стек следующего
    старта, а там указатель поднимает сломанный профиль поверх несобираемой базы.
    Честный отказ ДО вопросов: печать причины, код 1, мастер не запускается."""
    _isolate_default_config_at(tmp_path, monkeypatch)
    (tmp_path / "config.yml").write_text(
        _STAND_BASE + 'slots:\n  light: "nope-prof"\n', encoding="utf-8"
    )
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "work.yml").write_text(
        'slots:\n  default: "strong"\n', encoding="utf-8"
    )

    monkeypatch.setattr(cli, "_interactive", lambda: True)
    reached = []
    from gc_cli import onboarding_wizard

    monkeypatch.setattr(
        onboarding_wizard, "run", lambda *a, **k: reached.append(1) or True
    )

    assert cli._run_init() == 1
    assert reached == []  # до вопросов не дошли
    err = capsys.readouterr().err
    assert "не починит" in err
    assert '"work"' in err
    assert "Traceback" not in err


def _broken_project_layer_stand(tmp_path, monkeypatch):
    """Ревью (H3): исправная база, в проектном `.gc/config.yml` слот на
    несуществующий алиас — все ступени лестницы накладывают этот слой, и даже
    шаблон не собирается."""
    _isolate_default_config_at(tmp_path, monkeypatch)
    (tmp_path / "config.yml").write_text(
        "backend:\n  api_mode: chat_completions\n"
        '  completions_url: "https://openrouter.ai/api/v1/chat/completions"\n'
        "  chat_completion_headers:\n"
        '    Authorization: "Bearer sk-user-token"\n'
        "models:\n  fast:\n"
        '    model: "qwen/qwen3-8b"\n',
        encoding="utf-8",
    )
    project = tmp_path / "project"
    (project / ".gc").mkdir(parents=True)
    overlay = project / ".gc" / "config.yml"
    overlay.write_text('slots:\n  default: "ghost"\n', encoding="utf-8")
    monkeypatch.chdir(project)
    return overlay


def test_run_init_explains_a_broken_project_layer_instead_of_a_traceback(
    monkeypatch, tmp_path, capsys
):
    """Ревью (H3): `--init-config` на сломанном проектном слое падал трейсбеком
    (ступень шаблона не перехвачена), а обычный старт — человеческим
    объяснением. Мастером это не чинится (он пишет в пользовательский файл, а
    сломан оверлей): то же объяснение, что на обычном старте, с называнием
    оверлея, код 1; до мастера не доходим."""
    overlay = _broken_project_layer_stand(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    reached = []
    from gc_cli import onboarding_wizard

    monkeypatch.setattr(
        onboarding_wizard, "run", lambda *a, **k: reached.append(1) or True
    )

    assert cli._run_init() == 1
    assert reached == []
    err = capsys.readouterr().err
    assert "Конфиг не загружается" in err
    assert f"поправить slots.default в:  {overlay}" in err
    assert "Traceback" not in err


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
        'slots:\n  default: "strong"\n', encoding="utf-8"
    )


def test_run_init_keeps_user_connection_when_only_profile_is_broken(
    monkeypatch, tmp_path
):
    """Мастер открывается на НАСТОЯЩИХ адресе и токене человека, а не на
    заглушках шаблона: иначе он ушёл бы в сеть на `your-backend:PORT` и не
    получил список моделей при полностью рабочем токене."""
    _broken_profile_stand(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_interactive", lambda: True)

    seen = {}
    from gc_cli import onboarding_wizard

    monkeypatch.setattr(
        onboarding_wizard,
        "run",
        lambda cfg, path, *, review_all, io, mcp=None, deactivate_profile=False: (
            seen.update(cfg=cfg) or True
        ),
    )

    assert cli._run_init() == 0
    settings = settings_of(seen["cfg"])
    assert (
        settings.backend.completions_url
        == "https://openrouter.ai/api/v1/chat/completions"
    )
    assert (settings.backend.chat_completion_headers or {}).get(
        "Authorization"
    ) == "Bearer sk-user-token"
    assert list(settings.models or {}) == ["fast"]


def test_run_init_tells_which_layer_was_dropped(monkeypatch, tmp_path, capsys):
    """Текст деградации приезжает из общего места (`_repair_message`) и называет
    отброшенный профиль — у gc-cli мастер плоский, экран не занимает, поэтому
    stderr здесь достаточен."""
    _broken_profile_stand(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_interactive", lambda: True)

    from gc_cli import onboarding_wizard

    monkeypatch.setattr(
        onboarding_wizard,
        "run",
        lambda cfg, path, *, review_all, io, mcp=None, deactivate_profile=False: (
            True
        ),
    )

    assert cli._run_init() == 0
    err = capsys.readouterr().err
    assert 'профилем "work"' in err
    assert (
        "пакетном шаблоне" not in err
    )  # шаблон не подставлялся — врать нельзя


def test_run_init_passive_guide_on_broken_profile_tells_the_truth(
    monkeypatch, tmp_path, capsys
):
    """Ревью (Q): без TTY на ступени «без профиля» гайд печатал «Конфиг настроен —
    можно запускать», хотя следующий старт (с профилем) упадёт той же ошибкой.
    Гайд обязан сказать правду о деградировавшей ступени."""
    _broken_profile_stand(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_interactive", lambda: False)

    assert cli._run_init() == 0
    err = capsys.readouterr().err
    assert 'профилем "work"' in err  # сообщение лестницы — как прежде
    assert "можно запускать" not in err
    assert 'профиль "work"' in err and 'gc-cli --profile ""' in err


def test_run_init_asks_the_wizard_to_deactivate_on_the_without_profile_stage(
    monkeypatch, tmp_path
):
    """Ступень лестницы обязана доехать до мастера: без этого признака он проходит
    все вопросы и не может записать собственную починку — гейт судит стек
    следующего старта, а там указатель снова поднимает сломанный профиль."""
    _broken_profile_stand(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_interactive", lambda: True)

    seen = {}
    from gc_cli import onboarding_wizard

    monkeypatch.setattr(
        onboarding_wizard,
        "run",
        lambda cfg, path, *, review_all, io, mcp=None, deactivate_profile=False: (
            seen.update(deactivate=deactivate_profile) or True
        ),
    )

    assert cli._run_init() == 0
    assert seen["deactivate"] is True


def test_run_init_leaves_a_healthy_profile_alone(monkeypatch, tmp_path):
    """Отрицательный сторож: тот же стенд, но профиль исправен (ступень `full`) —
    мастеру нечего снимать, и просить его об этом нельзя."""
    _broken_profile_stand(tmp_path, monkeypatch)
    (tmp_path / "profiles" / "work.yml").write_text(
        'slots:\n  default: "fast"\n', encoding="utf-8"
    )
    monkeypatch.setattr(cli, "_interactive", lambda: True)

    seen = {}
    from gc_cli import onboarding_wizard

    monkeypatch.setattr(
        onboarding_wizard,
        "run",
        lambda cfg, path, *, review_all, io, mcp=None, deactivate_profile=False: (
            seen.update(deactivate=deactivate_profile) or True
        ),
    )

    assert cli._run_init() == 0
    assert seen["deactivate"] is False


def test_run_init_does_not_promise_a_deactivation_the_writer_cannot_do(
    monkeypatch, tmp_path
):
    """Ревью (D3), вторая половина: ступень та же (`without_profile`), но указатель
    лежит в managed-файле — правило адреса записи его не рассматривает. Признак,
    выведенный из одной лишь ступени, обещал человеку деактивацию, писатель клал
    пустой якорь в базу, а managed поднимал тот же сломанный профиль и гейт
    отвергал запись: человек оставался и без починки, и с чужим якорем в конфиге.
    Ответ у лестницы уже есть (`RepairLoad.deactivate_profile`) — берём его, а не
    вычисляем заново."""
    _broken_profile_stand(tmp_path, monkeypatch)
    (tmp_path / "config.yml").write_text(
        _STAND_BASE.replace('profile: "work"\n', ""), encoding="utf-8"
    )
    (tmp_path / "config.local.yml").write_text(
        'profile: "work"\n', encoding="utf-8"
    )
    monkeypatch.setattr(cli, "_interactive", lambda: True)

    seen = {}
    from gc_cli import onboarding_wizard

    monkeypatch.setattr(
        onboarding_wizard,
        "run",
        lambda cfg, path, *, review_all, io, mcp=None, deactivate_profile=False: (
            seen.update(deactivate=deactivate_profile) or True
        ),
    )

    assert cli._run_init() == 0
    from gc_core import onboarding

    assert (
        onboarding.load_config_for_repair().stage
        == onboarding.REPAIR_STAGE_WITHOUT_PROFILE
    )
    assert seen["deactivate"] is False


def test_main_explains_an_unreadable_layer_instead_of_a_traceback(
    monkeypatch, tmp_path, capsys, request
):
    """Ревью: сторож точки входа ловит только ValueError, а слои читались голым
    `open()` — существующий, но неоткрываемый `~/.gc/config.local.yml` уносил
    OSError мимо него, и человек получал трейсбек ровно там, где объяснение и
    заводили. Исход обязан быть тот же, что на битом YAML: код 1, имя файла."""
    base = tmp_path / "base.yml"
    base.write_text(
        'backend:\n  api_mode: chat_completions\n  completions_url: "http://127.0.0.1:8014/v1/chat/completions"\n',
        encoding="utf-8",
    )
    overlay = tmp_path / "config.local.yml"  # managed-слой песочницы conftest
    overlay.write_text("models:\n  fast: {}\n", encoding="utf-8")
    _make_layer_unreadable(overlay, request)

    _reload_arg_parser_with_argv(
        ["gc-cli", "--config", str(base), "-q", "x"], monkeypatch
    )
    monkeypatch.setattr(cli, "_ensure_project_gc_dir", lambda: None)
    monkeypatch.setattr(cli, "_ensure_user_skills_dir", lambda: None)

    rc = cli.main()

    assert rc == 1
    err = capsys.readouterr().err
    assert "Конфиг не загружается" in err
    assert str(overlay) in err
    assert "не удалось прочитать" in err
    assert "Traceback" not in err


def test_run_init_explains_an_unreadable_layer_instead_of_a_traceback(
    monkeypatch, tmp_path, capsys, request
):
    """Та же поломка на «чинилке»: лестница починки ловит ValueError на каждой
    ступени, и OSError уносил мимо всех трёх. Мастером нечитаемый чужой слой не
    чинится — объяснение и код 1, до мастера не доходим."""
    _isolate_default_config_at(tmp_path, monkeypatch)
    (tmp_path / "config.yml").write_text(
        'backend:\n  api_mode: chat_completions\n  completions_url: "https://openrouter.ai/api/v1/chat/completions"\n',
        encoding="utf-8",
    )
    overlay = tmp_path / "config.local.yml"
    overlay.write_text("models:\n  fast: {}\n", encoding="utf-8")
    _make_layer_unreadable(overlay, request)

    monkeypatch.setattr(cli, "_interactive", lambda: True)
    reached = []
    from gc_cli import onboarding_wizard

    monkeypatch.setattr(
        onboarding_wizard, "run", lambda *a, **k: reached.append(1) or True
    )

    assert cli._run_init() == 1
    assert reached == []
    err = capsys.readouterr().err
    assert "Конфиг не загружается" in err
    assert str(overlay) in err
    assert "не удалось прочитать" in err
    assert "Traceback" not in err


def test_invalid_param_value_is_reported_without_the_value(
    monkeypatch, tmp_path, capsys
):
    """Негодный `--param`: координата и причина, без значения (там токен)."""
    base = tmp_path / "base.yml"
    base.write_text("agent: {model: m}\n", encoding="utf-8")
    ap = _reload_arg_parser_with_argv(
        [
            "gc-cli",
            "--config",
            str(base),
            "--agent.max_steps=Bearer sk-SECRET-123",
            "-q",
            "x",
        ],
        monkeypatch,
    )
    monkeypatch.setattr(cli, "arg_parser", ap)
    monkeypatch.setattr(cli, "_ensure_project_gc_dir", lambda: None)
    monkeypatch.setattr(cli, "_ensure_user_skills_dir", lambda: None)

    rc = cli.main()

    assert rc == 2
    captured = capsys.readouterr()
    assert "agent.max_steps" in captured.err
    assert "sk-SECRET-123" not in captured.err + captured.out


def test_flat_param_name_fails_with_path_hint(monkeypatch, tmp_path, capsys):
    """Плоских имён `--param` нет: `--max_steps` — код 2 и подсказка пути."""
    base = tmp_path / "base.yml"
    base.write_text("agent: {model: m}\n", encoding="utf-8")
    ap = _reload_arg_parser_with_argv(
        ["gc-cli", "--config", str(base), "--max_steps", "50", "-q", "x"],
        monkeypatch,
    )
    monkeypatch.setattr(cli, "arg_parser", ap)
    monkeypatch.setattr(cli, "_ensure_project_gc_dir", lambda: None)
    monkeypatch.setattr(cli, "_ensure_user_skills_dir", lambda: None)

    rc = cli.main()

    assert rc == 2
    err = capsys.readouterr().err
    assert "неизвестная настройка --max_steps" in err
    assert "--agent.max_steps" in err


def test_registry_pair_violation_is_reported_without_traceback(
    monkeypatch, tmp_path, capsys
):
    base = tmp_path / "base.yml"
    base.write_text(
        "agent: {model: m}\n"
        "models:\n"
        "  ds: {model: x, url: 'https://a.example/v1/chat/completions', token: T}\n",
        encoding="utf-8",
    )
    ap = _reload_arg_parser_with_argv(
        [
            "gc-cli",
            "--config",
            str(base),
            "--models.ds.url",
            "https://evil.example/v1/chat/completions",
            "-q",
            "x",
        ],
        monkeypatch,
    )
    monkeypatch.setattr(cli, "arg_parser", ap)
    monkeypatch.setattr(cli, "_ensure_project_gc_dir", lambda: None)
    monkeypatch.setattr(cli, "_ensure_user_skills_dir", lambda: None)

    assert cli.main() == 2
    err = capsys.readouterr().err
    assert "models.ds" in err and "Traceback" not in err


def test_unwritable_log_dir_is_one_line_not_a_traceback(
    monkeypatch, tmp_path, capsys
):
    """``save_logs: true``, а каталог логов недоступен на запись (корень
    контейнера только на чтение, чужие права): код 1 и строка — какой каталог
    и что сделать; агент не стартует, трейсбека нет."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")
    logs = blocker / "logs"
    base = tmp_path / "base.yml"
    base.write_text(
        "agent: {model: m}\n"
        "backend: {completions_url: 'http://127.0.0.1:9/v1/chat/completions'}\n"
        f"paths: {{logs_dump_dir: '{logs.as_posix()}'}}\n"
        "logging: {save_logs: true}\n",
        encoding="utf-8",
    )
    ap = _reload_arg_parser_with_argv(
        ["gc-cli", "--config", str(base), "-q", "x"], monkeypatch
    )
    monkeypatch.setattr(cli, "arg_parser", ap)
    monkeypatch.setattr(cli, "_ensure_project_gc_dir", lambda: None)
    monkeypatch.setattr(cli, "_ensure_user_skills_dir", lambda: None)

    rc = cli.main()

    assert rc == 1
    err = capsys.readouterr().err
    assert "[gc] каталог логов прогона" in err
    assert "logging.save_logs" in err
    assert "Traceback" not in err
