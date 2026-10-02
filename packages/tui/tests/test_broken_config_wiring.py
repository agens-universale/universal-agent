"""gc-ua на сломанном конфиге: объяснение в stderr вместо трейсбека, TuiApp не
создаётся (падение случается ДО неё — собственный crash-репорт Textual тут не
поможет, см. tests/tui/test_main_wiring.py — тот же принцип для happy path)."""

import getpass
import os
import subprocess

import pytest


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


def _fake_app(monkeypatch, created):
    from gc_tui import app as tui_app

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


def test_main_prints_explanation_and_exits_1_on_invalid_yaml(
    monkeypatch, tmp_path, capsys
):
    pytest.importorskip("textual")

    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text("this is: : : not [valid yaml }\n", encoding="utf-8")

    created: list = []
    _fake_app(monkeypatch, created)

    from gc_tui import main

    rc = main(argv=["--config", str(cfg_path)])

    assert rc == 1
    assert created == []  # TuiApp не создан — падение случилось раньше
    err = capsys.readouterr().err
    assert "Конфиг не загружается" in err
    assert str(cfg_path) in err
    assert "невалидный YAML" in err


def test_main_prints_explanation_for_missing_profile(
    monkeypatch, tmp_path, capsys
):
    pytest.importorskip("textual")

    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(
        "backend:\n  api_mode: chat_completions\n"
        '  completions_url: "http://127.0.0.1:8014/v1/chat/completions"\n'
        "profile: ghost\n",
        encoding="utf-8",
    )

    created: list = []
    _fake_app(monkeypatch, created)

    from gc_tui import main

    rc = main(argv=["--config", str(cfg_path)])

    assert rc == 1
    assert created == []
    err = capsys.readouterr().err
    assert "Конфиг не загружается" in err
    assert 'профиль "ghost" не найден' in err


def test_main_explains_the_profile_from_the_flag_not_the_pointer(
    monkeypatch, tmp_path, capsys
):
    """Ревью (H1), gc-ua: `--profile broken` — объяснение строилось по указателю
    из файла (его нет): карта без профиля, совет чинить базу, подсказки
    `--profile ""` нет. Строится по профилю, который действовал в сессии."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "gc_core.config_modules.env_vars.managed_config_path",
        lambda: tmp_path / "config.local.yml",
    )
    base = tmp_path / "config.yml"
    base.write_text(
        "backend:\n  api_mode: chat_completions\n"
        '  completions_url: "http://127.0.0.1:8014/v1/chat/completions"\n'
        "models:\n  flash: {}\n",
        encoding="utf-8",
    )
    (tmp_path / "profiles").mkdir()
    profile = tmp_path / "profiles" / "broken.yml"
    profile.write_text("slots:\n  default: strong\n", encoding="utf-8")

    created: list = []
    _fake_app(monkeypatch, created)

    from gc_tui import main

    rc = main(argv=["--config", str(base), "--profile", "broken"])

    assert rc == 1
    assert created == []
    err = capsys.readouterr().err
    assert f"поправить slots.default в:  {profile}" in err
    assert f"в:  {base}" not in err
    assert f"{profile}   (активен" in err
    # Сессия шла с --config — совет обязан нести его (M9), иначе `--profile ""`
    # запустит не тот конфиг.
    assert f'gc-ua --config "{base}" --profile ""' in err


# --- `--init-config` — «чинилка» обязана работать именно на сломанном конфиге ---


def _isolate_default_config_at(tmp_path, monkeypatch):
    from gc_core.config_modules import env_vars
    from gc_core.config_modules.env_vars import EnvVars

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
    (tmp_path / "config.yml").write_text(
        "this is: : : not [valid yaml }\n", encoding="utf-8"
    )

    created: list = []
    _fake_app(monkeypatch, created)

    import gc_tui as tui_mod

    monkeypatch.setattr(tui_mod, "_interactive", lambda: False, raising=False)

    from gc_tui import main

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
    (tmp_path / "config.yml").write_text(
        "this is: : : not [valid yaml }\n", encoding="utf-8"
    )

    captured: dict = {}
    from gc_tui import app as tui_app

    class _FakeApp:
        def __init__(self, **kw):
            from gc_core.config import flat_config

            captured["config"] = flat_config(kw["settings"])

        def set_config_path(self, path):
            pass

        def _maybe_notice_profile_activation(self, *a, **k):
            captured["notice_args"] = a

        def run(self):
            captured["ran"] = True

    monkeypatch.setattr(tui_app, "TuiApp", _FakeApp)

    import gc_tui as tui_mod

    monkeypatch.setattr(tui_mod, "_interactive", lambda: True, raising=False)

    from recommended_mcp.runtime import daemon_autostart as mcp_da_mod

    monkeypatch.setattr(
        mcp_da_mod, "ensure_mcp_daemons", lambda cfg, **kw: None, raising=False
    )
    monkeypatch.setattr(
        mcp_da_mod, "shutdown_mcp_daemons", lambda: None, raising=False
    )

    from gc_tui import main

    rc = main(argv=["--init-config"])

    assert rc == 0
    assert captured.get("ran") is True
    # Пакетный шаблон, не пользовательские данные и не пустой словарь: сентинел
    # адреса — тот же, каким onboarding_wizard/setup_guide узнают «не настроено».
    from gc_core import onboarding

    assert captured["config"].get("completions_url") == onboarding.URL_SENTINEL
    assert (
        captured["notice_args"][0] == ""
    )  # активного профиля нет — восстановление сняло его


def test_init_config_refuses_before_questions_when_a_pointer_survives_the_template_stage(
    monkeypatch, tmp_path, capsys
):
    """Ревью (M10), gc-ua: ступень шаблона + живой указатель профиля — экран
    мастера открывался, человек проходил все шаги (с вводом токена) и получал
    отказ записи. Честный отказ ДО вопросов: stderr с причиной, код 1, TuiApp
    не создаётся."""
    pytest.importorskip("textual")
    _isolate_default_config_at(tmp_path, monkeypatch)
    (tmp_path / "config.yml").write_text(
        _STAND_BASE + 'slots:\n  light: "nope-prof"\n', encoding="utf-8"
    )
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "work.yml").write_text(
        'slots:\n  default: "strong"\n', encoding="utf-8"
    )

    created: list = []
    _fake_app(monkeypatch, created)
    import gc_tui as tui_mod

    monkeypatch.setattr(tui_mod, "_interactive", lambda: True, raising=False)

    from gc_tui import main

    rc = main(argv=["--init-config"])

    assert rc == 1
    assert created == []
    err = capsys.readouterr().err
    assert "не починит" in err
    assert '"work"' in err


def test_init_config_explains_a_broken_project_layer_instead_of_a_traceback(
    monkeypatch, tmp_path, capsys
):
    """Ревью (H3), gc-ua: `--init-config` на исправной базе со сломанным проектным
    `.gc/config.yml` падал трейсбеком ступени шаблона. Мастером не чинится —
    объяснение как на обычном старте (оверлей назван), код 1, TuiApp не создан."""
    pytest.importorskip("textual")
    _isolate_default_config_at(tmp_path, monkeypatch)
    (tmp_path / "config.yml").write_text(
        _STAND_BASE.replace('profile: "work"\n', ""), encoding="utf-8"
    )
    project = tmp_path / "project"
    (project / ".gc").mkdir(parents=True)
    overlay = project / ".gc" / "config.yml"
    overlay.write_text('slots:\n  default: "ghost"\n', encoding="utf-8")
    monkeypatch.chdir(project)

    created: list = []
    _fake_app(monkeypatch, created)
    import gc_tui as tui_mod

    monkeypatch.setattr(tui_mod, "_interactive", lambda: True, raising=False)

    from gc_tui import main

    rc = main(argv=["--init-config"])

    assert rc == 1
    assert created == []
    err = capsys.readouterr().err
    assert "Конфиг не загружается" in err
    assert f"поправить slots.default в:  {overlay}" in err


# --- `--profile` у gc-ua: обход поломки, унесённой активным профилем ---------


def test_profile_empty_flag_recovers_config_broken_by_active_profile(
    monkeypatch, tmp_path
):
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
        "backend:\n  api_mode: chat_completions\n"
        '  completions_url: "http://127.0.0.1:8014/v1/chat/completions"\n'
        "models:\n  flash: {}\n"
        "profile: work\n"
        "paths:\n"
        f'  threads_data_path: "{threads_dir.as_posix()}"\n',
        encoding="utf-8",
    )
    (profiles_dir / "work.yml").write_text(
        "slots:\n  default: strong\n", encoding="utf-8"
    )

    from gc_tui import main

    # Без флага — падает (Section 3: объяснение вместо трейсбека, TuiApp не создан).
    created_broken: list = []
    _fake_app(monkeypatch, created_broken)
    rc_broken = main(argv=["--config", str(base)])
    assert rc_broken == 1
    assert created_broken == []

    # С флагом --profile "" — грузится и доходит до TuiApp.
    created_ok: list = []
    _fake_app(monkeypatch, created_ok)
    monkeypatch.setattr(
        "recommended_mcp.runtime.daemon_autostart.ensure_mcp_daemons",
        lambda cfg, **kw: None,
    )
    monkeypatch.setattr(
        "recommended_mcp.runtime.daemon_autostart.shutdown_mcp_daemons",
        lambda: None,
    )
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
        'slots:\n  default: "strong"\n', encoding="utf-8"
    )
    monkeypatch.setattr(
        "recommended_mcp.runtime.daemon_autostart.ensure_mcp_daemons",
        lambda cfg, **kw: None,
    )
    monkeypatch.setattr(
        "recommended_mcp.runtime.daemon_autostart.shutdown_mcp_daemons",
        lambda: None,
    )
    import gc_tui as tui_mod

    monkeypatch.setattr(tui_mod, "_interactive", lambda: True, raising=False)


def _capturing_app(monkeypatch):
    captured: dict = {}
    from gc_tui import app as tui_app

    class _FakeApp:
        def __init__(self, **kw):
            from gc_core.config import flat_config

            captured["config"] = flat_config(kw["settings"])
            captured["app"] = (
                self  # признаки ставятся атрибутами уже после создания
            )

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
    from gc_core import runtime_notices

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

    from gc_tui import main

    rc = main(argv=["--init-config"])

    assert rc == 0
    assert captured.get("ran") is True
    cfg = captured["config"]
    assert (
        cfg.get("completions_url")
        == "https://openrouter.ai/api/v1/chat/completions"
    )
    assert (cfg.get("chat_completion_headers") or {}).get(
        "Authorization"
    ) == "Bearer sk-user-token"
    assert list(cfg.get("models") or {}) == ["fast"]


def test_init_config_passive_guide_on_broken_profile_tells_the_truth(
    monkeypatch, tmp_path, capsys, clean_notices
):
    """Ревью (Q), gc-ua без TTY: на ступени «без профиля» гайд печатал «Конфиг
    настроен — можно запускать», хотя следующий старт с профилем упадёт."""
    pytest.importorskip("textual")
    _broken_profile_stand(tmp_path, monkeypatch)
    import gc_tui as tui_mod

    monkeypatch.setattr(tui_mod, "_interactive", lambda: False, raising=False)
    created: list = []
    _fake_app(monkeypatch, created)

    from gc_tui import main

    assert main(argv=["--init-config"]) == 0
    assert created == []
    err = capsys.readouterr().err
    assert "можно запускать" not in err
    assert 'профиль "work"' in err and 'gc-ua --profile ""' in err


def test_init_config_degradation_reaches_notices(
    monkeypatch, tmp_path, clean_notices
):
    """Задача D: сообщение обязано пережить старт TUI. В stderr его через мгновение
    стирает альтернативный буфер Textual — живой смок подтвердил, что человек не
    видит НИ СТРОЧКИ и не понимает, почему мастер ведёт себя странно. Значит, оно
    едет тем же каналом, что и остальные рантайм-уведомления (F4 / `/notices`)."""
    pytest.importorskip("textual")
    _broken_profile_stand(tmp_path, monkeypatch)
    _capturing_app(monkeypatch)

    from gc_tui import main

    assert main(argv=["--init-config"]) == 0

    degraded = [
        n
        for n in clean_notices.collect_runtime_notices()
        if n.code == "config_load_degraded"
    ]
    assert len(degraded) == 1
    assert degraded[0].level == "warning"
    assert 'профилем "work"' in degraded[0].message
    # Не прочитано → бейдж /notices зажжётся.
    assert degraded[0] in clean_notices.collect_unread_runtime_notices()


def test_healthy_config_leaves_no_degradation_notice(
    monkeypatch, tmp_path, clean_notices
):
    """Здоровый конфиг — ступень 1: ни уведомления, ни строчки в stderr."""
    pytest.importorskip("textual")
    _broken_profile_stand(tmp_path, monkeypatch)
    (tmp_path / "profiles" / "work.yml").write_text(
        'slots:\n  default: "fast"\n', encoding="utf-8"
    )
    _capturing_app(monkeypatch)

    from gc_tui import main

    assert main(argv=["--init-config"]) == 0

    assert [
        n
        for n in clean_notices.collect_runtime_notices()
        if n.code == "config_load_degraded"
    ] == []


def test_init_config_carries_the_without_profile_stage_to_the_master(
    monkeypatch, tmp_path, clean_notices
):
    """Ступень лестницы обязана доехать до мастера: без признака его финал упрётся
    в тот же отказ записи, что и на живом стенде («алиас "strong" не найден») —
    гейт судит стек следующего старта, а там указатель поднимает сломанный профиль."""
    pytest.importorskip("textual")
    _broken_profile_stand(tmp_path, monkeypatch)
    captured = _capturing_app(monkeypatch)

    from gc_tui import main

    assert main(argv=["--init-config"]) == 0
    assert captured["app"]._onboarding_deactivate_profile is True


def test_healthy_config_does_not_ask_the_master_to_deactivate(
    monkeypatch, tmp_path, clean_notices
):
    """Отрицательный сторож: профиль исправен (ступень `full`) — снимать нечего."""
    pytest.importorskip("textual")
    _broken_profile_stand(tmp_path, monkeypatch)
    (tmp_path / "profiles" / "work.yml").write_text(
        'slots:\n  default: "fast"\n', encoding="utf-8"
    )
    captured = _capturing_app(monkeypatch)

    from gc_tui import main

    assert main(argv=["--init-config"]) == 0
    assert captured["app"]._onboarding_deactivate_profile is False


def test_init_config_carries_the_degradation_text_to_the_master(
    monkeypatch, tmp_path, clean_notices
):
    """Текст деградации обязан доехать до мастера: рантайм-уведомление под ним
    недостижимо (`OnboardingScreen.BLOCKS_GLOBAL_NAV` гасит F4), и до финала
    показать его негде — человек проходит весь мастер, не понимая, почему поля
    выглядят иначе, чем его файл."""
    pytest.importorskip("textual")
    _broken_profile_stand(tmp_path, monkeypatch)
    captured = _capturing_app(monkeypatch)

    from gc_tui import main

    assert main(argv=["--init-config"]) == 0
    assert 'профилем "work"' in captured["app"]._onboarding_degraded


def test_healthy_config_hands_the_master_no_degradation_text(
    monkeypatch, tmp_path, clean_notices
):
    """Отрицательный сторож: ступень `full` — деградации не было, показывать нечего."""
    pytest.importorskip("textual")
    _broken_profile_stand(tmp_path, monkeypatch)
    (tmp_path / "profiles" / "work.yml").write_text(
        'slots:\n  default: "fast"\n', encoding="utf-8"
    )
    captured = _capturing_app(monkeypatch)

    from gc_tui import main

    assert main(argv=["--init-config"]) == 0
    assert captured["app"]._onboarding_degraded is None


def test_a_managed_pointer_is_not_promised_to_the_master(
    monkeypatch, tmp_path, clean_notices
):
    """Ревью (D3), вторая половина: ступень та же (`without_profile`), но указатель
    лежит в managed-файле — правило адреса записи его не рассматривает. Признак,
    выведенный из одной лишь ступени, обещал деактивацию на экране подтверждения,
    писатель клал пустой якорь в базу, а managed поднимал тот же сломанный профиль
    и гейт отвергал запись: человек оставался и без починки, и с чужим якорем.
    Ответ у лестницы уже есть (`RepairLoad.deactivate_profile`) — берём его, а не
    вычисляем заново."""
    pytest.importorskip("textual")
    _broken_profile_stand(tmp_path, monkeypatch)
    (tmp_path / "config.yml").write_text(
        _STAND_BASE.replace('profile: "work"\n', ""), encoding="utf-8"
    )
    (tmp_path / "config.local.yml").write_text(
        'profile: "work"\n', encoding="utf-8"
    )
    captured = _capturing_app(monkeypatch)

    from gc_tui import main

    assert main(argv=["--init-config"]) == 0
    from gc_core import onboarding

    assert (
        onboarding.load_config_for_repair().stage
        == onboarding.REPAIR_STAGE_WITHOUT_PROFILE
    )
    assert captured["app"]._onboarding_deactivate_profile is False


def test_the_repair_wizard_is_not_handed_the_profile_flag(
    monkeypatch, tmp_path
):
    """Зеркало проводки флага (`test_main_wiring`): обычный старт отдаёт
    `--profile` приложению, чинилка — нет.

    Она читает и пишет ПОЛЬЗОВАТЕЛЬСКИЙ файл, а не тот, что назван флагами
    (см. комментарий в `tui.main`), и перечитывание после мастера обязано
    собрать стек того конфига, который человек только что чинил. Страж нужен
    именно отрицательный: мутация «передавать флаг всегда» проходила весь
    набор молча."""
    pytest.importorskip("textual")
    _isolate_default_config_at(tmp_path, monkeypatch)
    (tmp_path / "config.yml").write_text(
        "this is: : : not [valid yaml }\n", encoding="utf-8"
    )

    captured: dict = {}
    from gc_tui import app as tui_app

    class _FakeApp:
        def __init__(self, **kw):
            captured.update(kw)

        def set_config_path(self, path):
            pass

        def _maybe_notice_profile_activation(self, *a, **k):
            pass

        def run(self):
            captured["ran"] = True

    monkeypatch.setattr(tui_app, "TuiApp", _FakeApp)

    import gc_tui as tui_mod

    monkeypatch.setattr(tui_mod, "_interactive", lambda: True, raising=False)

    rc = tui_mod.main(argv=["--init-config", "--profile", "work"])
    assert rc == 0
    assert captured.get("ran") is True  # дошли до сборки приложения
    assert captured.get("profile_override") is None  # …и флаг в неё не уехал


def test_main_explains_an_unreadable_layer_instead_of_a_traceback(
    monkeypatch, tmp_path, capsys, request
):
    """Ревью, gc-ua: слои читались голым `open()`, и существующий, но
    неоткрываемый `~/.gc/config.local.yml` уносил OSError мимо `except
    ValueError` точки входа — трейсбек вместо объяснения, TuiApp ещё не создана,
    так что и crash-репорт Textual не включился бы."""
    pytest.importorskip("textual")
    monkeypatch.chdir(tmp_path)
    base = tmp_path / "config.yml"
    base.write_text(
        'backend:\n  api_mode: chat_completions\n  completions_url: "http://127.0.0.1:8014/v1/chat/completions"\n',
        encoding="utf-8",
    )
    overlay = tmp_path / "config.local.yml"  # managed-слой песочницы conftest
    overlay.write_text("models:\n  fast: {}\n", encoding="utf-8")
    _make_layer_unreadable(overlay, request)

    created: list = []
    _fake_app(monkeypatch, created)

    from gc_tui import main

    rc = main(argv=["--config", str(base)])

    assert rc == 1
    assert created == []
    err = capsys.readouterr().err
    assert "Конфиг не загружается" in err
    assert str(overlay) in err
    assert "не удалось прочитать" in err
    assert "Traceback" not in err


def test_init_config_explains_an_unreadable_layer_instead_of_a_traceback(
    monkeypatch, tmp_path, capsys, request
):
    """Та же поломка на «чинилке» gc-ua: лестница починки ловит ValueError на
    каждой ступени, и OSError уносил мимо всех трёх. Чужой нечитаемый слой
    мастером не чинится — объяснение, код 1, TuiApp не создана."""
    pytest.importorskip("textual")
    _isolate_default_config_at(tmp_path, monkeypatch)
    (tmp_path / "config.yml").write_text(
        'backend:\n  api_mode: chat_completions\n  completions_url: "https://openrouter.ai/api/v1/chat/completions"\n',
        encoding="utf-8",
    )
    overlay = tmp_path / "config.local.yml"
    overlay.write_text("models:\n  fast: {}\n", encoding="utf-8")
    _make_layer_unreadable(overlay, request)

    created: list = []
    _fake_app(monkeypatch, created)
    import gc_tui as tui_mod

    monkeypatch.setattr(tui_mod, "_interactive", lambda: True, raising=False)

    from gc_tui import main

    rc = main(argv=["--init-config"])

    assert rc == 1
    assert created == []
    err = capsys.readouterr().err
    assert "Конфиг не загружается" in err
    assert str(overlay) in err
    assert "не удалось прочитать" in err
    assert "Traceback" not in err
