"""reload_config: перечитка конфига + сброс кэшированного runner (спека B, B3)."""

from gc_testkit.tui_support import tui_settings
import pytest

pytest.importorskip("textual")

from gc_tui import app as app_mod


class _FakeRunner:
    def __init__(self, running):
        self._running = running

    def is_running(self):
        return self._running


def _mk_app(tmp_path):
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        'backend:\n  completions_url: "http://x/v1"\n', encoding="utf-8"
    )
    app = app_mod.TuiApp(settings=tui_settings({"completions_url": "old"}))
    app._config_path = str(cfg_file)
    return app


def _bump_mtime(path, seconds=5.0):
    """Гарантированно другой mtime — не полагаемся на разрешение файловой системы."""
    import os
    import time

    t = time.time() + seconds
    os.utime(path, (t, t))


def test_reload_config_rereads_and_resets_idle_runner(tmp_path):
    app = _mk_app(tmp_path)
    app.active_runner = _FakeRunner(running=False)
    app.reload_config()
    assert _url(app.base_settings()) == "http://x/v1"
    assert (
        app.active_runner is None
    )  # следующий submit соберёт свежий executor


def test_reload_config_defers_reset_while_running(tmp_path):
    app = _mk_app(tmp_path)
    runner = _FakeRunner(running=True)
    app.active_runner = runner
    app.reload_config()
    assert app.active_runner is runner  # живой run не трогаем
    assert app._runner_stale is True
    runner._running = False
    got = app.get_runner()  # run завершился → пересборка
    assert got is not runner
    assert app._runner_stale is False


def test_reload_config_idle_clears_stale_flag(tmp_path):
    """I1: утечка _runner_stale — idle-reload должен сбрасывать флаг."""
    app = _mk_app(tmp_path)
    # (а) runner жил и stale=True был выставлен на первом reload
    runner = _FakeRunner(running=True)
    app.active_runner = runner
    app.reload_config()
    assert app._runner_stale is True
    # (б) runner завершился, но второй reload зовут до get_runner (idle-ветка)
    runner._running = False
    app.reload_config()
    assert app.active_runner is None
    assert app._runner_stale is False
    # (в) get_runner создаёт новый runner, флаг по-прежнему False
    fake_runner = _FakeRunner(running=False)
    app._executor_factory = lambda: None
    # перехватываем AgentRunner: подменяем класс в tui.services.agent_runner
    from gc_tui.services import agent_runner as runner_mod

    orig = runner_mod.AgentRunner
    runner_mod.AgentRunner = lambda **_kw: fake_runner
    try:
        got = app.get_runner()
    finally:
        runner_mod.AgentRunner = orig
    assert got is fake_runner
    assert app._runner_stale is False


@pytest.mark.asyncio
async def test_reload_config_preserves_runtime_fields(tmp_path, monkeypatch):
    """Hot-apply не теряет поля, вычисленные рантаймом TUI.

    launch_root ставит tui/__init__ (снапшот cwd на старте) — в файле конфига его нет.
    reload_config перечитывает файл, и без явного переноса поле исчезало:
      - /diff переставал работать («diff: launch_root не задан»);
      - периметр path_policy съезжал со снапшота на живой os.getcwd(), хотя агент мог
        сделать cd внутри сессии.
    """
    from gc_core import config as cfg_mod
    from gc_core import thread_store as ts_mod
    from gc_core.config_modules.profiles import ProfileMeta
    from dataclasses import replace

    from gc_testkit.settings import make_settings
    from gc_tui.app import TuiApp
    from gc_tui.runtime_config import apply_tui_runtime

    on_disk = {
        "completions_url": "https://h/v1/chat/completions",
        "chat_completion_headers": {"Authorization": "Bearer sk"},
        "model": "m",
    }
    meta = ProfileMeta(active_name=None, ref=None, mtime_set={})
    # файл: без launch_root
    monkeypatch.setattr(
        cfg_mod,
        "load_settings",
        lambda p=None, **kw: replace(
            make_settings(on_disk), profile_meta=meta
        ),
    )
    monkeypatch.setattr(cfg_mod, "apply_cli_overrides", lambda d, **kw: d)
    monkeypatch.setattr(
        ts_mod, "resolve_threads_dir", lambda c: str(tmp_path / "t")
    )

    # как собирает tui/__init__: снапшот cwd на старте + рантайм TUI
    started = apply_tui_runtime(
        replace(make_settings(on_disk), profile_meta=meta),
        launch_root="D:/proj/snapshot-at-start",
    )

    app = TuiApp(threads_dir=str(tmp_path), system="cli", settings=started)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.reload_config()
        assert app._settings.runtime.launch_root == "D:/proj/snapshot-at-start"
        assert app._settings.settings.logging.console_trace is False


# --- reload_config_if_changed: гейт свежести по mtime (два окна, один файл) ---


def test_reload_config_if_changed_noop_without_config_path():
    """_config_path не задан (типично для тестов/раннего старта) — тихий
    no-op, ни чтения, ни падения."""
    app = app_mod.TuiApp(settings=tui_settings({"completions_url": "old"}))
    app.reload_config_if_changed()
    assert _url(app.base_settings()) == "old"


def test_reload_config_if_changed_first_check_after_startup_is_noop(
    tmp_path, monkeypatch
):
    """Самая первая проверка сразу после старта окна — БЕЗ единого явного
    reload_config() до неё, ровно как в main(): конфиг уже загружен в
    конструктор, set_config_path только связывает путь — не должна
    перечитывать конфиг и не должна инвалидировать раннер. База и так
    только что загружена из этого самого файла; если снимок mtime не взят
    сразу при связывании пути, первая проверка видит «файл изменился» и
    зря перечитывает + инвалидирует прогретый раннер."""
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        'backend:\n  completions_url: "http://x/v1"\n', encoding="utf-8"
    )
    app = app_mod.TuiApp(
        settings=tui_settings({"completions_url": "http://x/v1"})
    )
    app.set_config_path(
        str(cfg_file)
    )  # ровно то, что делает main() сразу после конструктора
    app.active_runner = _FakeRunner(running=False)

    calls = {"load": 0, "invalidate": 0}
    monkeypatch.setattr(
        "gc_core.config.load_settings",
        lambda *_a, **_k: calls.__setitem__("load", calls["load"] + 1) or {},
    )
    orig_invalidate = app.invalidate_runner

    def counting_invalidate():
        calls["invalidate"] += 1
        orig_invalidate()

    app.invalidate_runner = counting_invalidate

    app.reload_config_if_changed()

    assert calls == {"load": 0, "invalidate": 0}


def test_reload_config_if_changed_skips_when_file_untouched(
    tmp_path, monkeypatch
):
    """Файл не менялся с последнего известного снимка mtime — НИЧЕГО не
    происходит: ни load_settings, ни invalidate_runner (I1 — пересборка
    раннера тянет MCP-дискавери и держит юзера на старте хода)."""
    app = _mk_app(tmp_path)
    app.reload_config()  # устанавливает базовый снимок mtime

    calls = {"load": 0, "invalidate": 0}
    monkeypatch.setattr(
        "gc_core.config.load_settings",
        lambda *_a, **_k: calls.__setitem__("load", calls["load"] + 1) or {},
    )
    orig_invalidate = app.invalidate_runner

    def counting_invalidate():
        calls["invalidate"] += 1
        orig_invalidate()

    app.invalidate_runner = counting_invalidate

    app.reload_config_if_changed()

    assert calls == {"load": 0, "invalidate": 0}
    assert _url(app.base_settings()) == "http://x/v1"


def test_reload_config_if_changed_reloads_when_file_changed(tmp_path):
    """Файл реально изменился (другое окно сохранило умолчание) — база
    перечитывается и раннер инвалидируется, как обычный reload_config."""
    app = _mk_app(tmp_path)
    app.reload_config()
    app.active_runner = _FakeRunner(running=False)

    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(
        'backend:\n  completions_url: "http://y/v2"\n', encoding="utf-8"
    )
    _bump_mtime(cfg_path)

    app.reload_config_if_changed()

    assert _url(app.base_settings()) == "http://y/v2"
    assert app.active_runner is None  # инвалидация реально произошла


def test_reload_config_if_changed_keeps_old_base_on_broken_file(tmp_path):
    """Юзер сохранил config.yml в промежуточном/битом состоянии — чтение
    кидает, окно не падает и продолжает работать на прежней базе. Молчать при
    этом нельзя: причина уходит в runtime-уведомления (/notices), иначе
    регрессия загрузки конфига незаметна."""
    from gc_core.runtime_notices import collect_runtime_notices

    app = _mk_app(tmp_path)
    app.reload_config()
    good_config = app.base_settings()

    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(
        "backend: [this is not, valid: yaml:", encoding="utf-8"
    )
    _bump_mtime(cfg_path)

    app.reload_config_if_changed()  # не должно бросить исключение

    assert app.base_settings() is good_config
    assert [n.code for n in collect_runtime_notices()] == [
        "config_reload_failed"
    ]


def test_reload_config_if_changed_own_write_does_not_retrigger(
    tmp_path, monkeypatch
):
    """Собственное сохранение (reload_config уже перечитал и обновил снимок
    mtime) не должно вызвать повторное перечитывание на следующей проверке —
    иначе executor пересобирался бы впустую сразу после /settings → Сохранить
    как умолчание."""
    app = _mk_app(tmp_path)
    app.reload_config()  # «сохранение» — сам конфиг тоже перечитан этим вызовом

    calls = {"load": 0}
    monkeypatch.setattr(
        "gc_core.config.load_settings",
        lambda *_a, **_k: calls.__setitem__("load", calls["load"] + 1) or {},
    )

    app.reload_config_if_changed()

    assert calls == {"load": 0}


def test_build_real_executor_does_not_reload_config(tmp_path, monkeypatch):
    """Сборка executor больше НЕ перечитывает config.yml (дизайн 2026-07-21 §4):
    свежесть глобала перенесена на путь Settings. Ход агента идёт по текущей базе
    окна; чужая правка файла подхватывается при следующем открытии Settings."""
    app = _mk_app(tmp_path)
    app.reload_config()  # база = http://x/v1

    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(
        'backend:\n  completions_url: "http://y/v2"\n', encoding="utf-8"
    )
    _bump_mtime(cfg_path)

    class _Ctx:
        # У настоящего RunContext бэкенд есть всегда; двойнику он нужен, потому что
        # TuiApp включает diff-журнал сам (attach_to_context) — раньше это делало
        # ядро внутри acreate_run_context, то есть внутри подменённой функции.
        os_backend = None
        tools_data = {}
        mcp_status = {}
        logging_path = "/tmp/agent_main_test.log"

    class _Ex:
        def __init__(self, ctx):
            pass

        def stop_run(self):
            pass

    seen_cfg = {}

    async def _fake_acreate_run_context(loaded, **kw):
        seen_cfg["completions_url"] = _url(loaded)
        return _Ctx()

    monkeypatch.setattr(
        "gc_core.config_modules.auth.build_auth_connector", lambda cfg: None
    )
    monkeypatch.setattr(
        "gc_core.run_context.acreate_run_context", _fake_acreate_run_context
    )
    monkeypatch.setattr("gc_core.agent_executor.AgentExecutor", _Ex)

    app._build_real_executor()

    assert (
        seen_cfg["completions_url"] == "http://x/v1"
    )  # база НЕ перечитана из файла


# --- флаг запуска --profile переживает перечитывание, как сессионный выбор ---
#
# Старт передавал выбранный флагом профиль в загрузчик, а горячее перечитывание —
# нет: первое же сохранение из /settings молча возвращало указатель из файла. Совет
# «gc-ua --profile "", чтобы обойти сломанный профиль» жил до первой записи — после
# неё окно снова упиралось в него. Флаг хранится у приложения отдельно от сессионного
# выбора (тот персистится в мету треда и переживает перезапуск сессии — у флага
# запуска такого права нет) и передаётся загрузчику при каждом перечитывании.


@pytest.fixture
def _profile_stack(tmp_path, monkeypatch):
    """База с указателем на профиль `work` и каталог профилей рядом, изолированные
    от слоёв машины (тот же приём, что `stack` в tests/config)."""
    from gc_core.config_modules import env_vars
    from gc_core.config_modules.env_vars import EnvVars
    from gc_core.runtime_notices import clear_runtime_notices

    monkeypatch.setattr(env_vars, "env", EnvVars(gc_home=tmp_path / "gc_home"))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        'profile: "work"\nbackend:\n  completions_url: "http://x/v1"\nmodels:\n  gc:\n    model: "base-m"\n',
        encoding="utf-8",
    )
    (profiles / "work.yml").write_text(
        'slots:\n  default: "ghost"\n', encoding="utf-8"
    )
    clear_runtime_notices()
    return cfg_file, profiles


def _app_started_with(cfg_file, profile_override):
    """Приложение, как его собирает main(): конфиг загружен С флагом, путь связан."""
    from gc_core import config as cfg_mod

    loaded = cfg_mod.load_settings(
        str(cfg_file), profile_override=profile_override
    )
    app = app_mod.TuiApp(settings=loaded, profile_override=profile_override)
    app.set_config_path(str(cfg_file))
    return app


def test_reload_config_keeps_the_profile_flag_that_bypasses_a_broken_pointer(
    _profile_stack,
):
    """`gc-ua --profile ""` на сломанном профиле из указателя: перечитывание обязано
    собираться так же, как старт, — без профиля. Без флага загрузчик падает на
    `slots.default: ghost`, и окно упиралось бы в профиль, который флаг обошёл."""
    cfg_file, _profiles = _profile_stack
    app = _app_started_with(cfg_file, "")
    assert app.base_settings().runtime.active_profile == ""

    app.reload_config()  # без флага здесь ValueError

    assert app.base_settings().runtime.active_profile == ""
    assert app.effective_settings().runtime.active_profile == ""


def test_reload_config_keeps_the_profile_named_by_the_flag(_profile_stack):
    cfg_file, profiles = _profile_stack
    (profiles / "corp.yml").write_text(
        'models:\n  corp:\n    model: "corp-m"\nslots:\n  default: "corp"\n',
        encoding="utf-8",
    )
    app = _app_started_with(cfg_file, "corp")

    app.reload_config()

    assert app.base_settings().runtime.active_profile == "corp"
    assert "corp" in app.base_settings().settings.models
    # Мтайм-набор следит за файлом профиля флага, а не за указателем файла.
    assert any("corp.yml" in path for path in app._config_mtime_set)


def test_session_choice_still_wins_over_the_profile_flag(_profile_stack):
    """Выбор из приложения (`/profiles`) сильнее флага запуска — как и сильнее
    указателя файла: сессионное снятие профиля при флаге `corp` действует."""
    cfg_file, profiles = _profile_stack
    (profiles / "corp.yml").write_text(
        'models:\n  corp:\n    model: "corp-m"\nslots:\n  default: "corp"\n',
        encoding="utf-8",
    )
    app = _app_started_with(cfg_file, "corp")
    app.set_session_override("profile", "")

    app.reload_config()

    assert (
        app.base_settings().runtime.active_profile == "corp"
    )  # база окна — по флагу
    assert (
        app.effective_settings().runtime.active_profile == ""
    )  # действует выбор сессии


_CORP = 'models:\n  corp:\n    model: "corp-m"\nslots:\n  default: "corp"\n'


def _app_that_could_lose_the_flag(cfg_file, profiles):
    """Стек, из которого флаг РЕАЛЬНО может выпасть: указатель базы пуст (без
    профиля база собирается), профиль флага `corp` — на диске.

    Дефолтный `_profile_stack` для этого не годится: там указатель ведёт на битый
    `work`, и подмена профиля указателем упала бы сама — «флаг на месте» зеленело
    бы, ничего не проверив."""
    cfg_file.write_text(
        'profile: ""\nbackend:\n  completions_url: "http://x/v1"\nmodels:\n  gc:\n    model: "base-m"\n',
        encoding="utf-8",
    )
    (profiles / "corp.yml").write_text(_CORP, encoding="utf-8")
    return _app_started_with(cfg_file, "corp")


def test_a_vanished_flag_profile_keeps_the_flag_and_the_previous_base(
    _profile_stack,
):
    """Профиль флага пропал под ногами: окно остаётся на прежней базе с причиной в
    `/notices` — той же деградацией, что и на битом config.yml, — и НЕ подменяет
    профиль указателем из файла.

    Флаг переживает отказ: он свойство запуска, а не файла, и снятый однажды не
    возвращается — `--profile ""` тем же вечером молча воскресил бы сломанный
    профиль на первом сохранении из /settings. Снимок мтайм-набора отказ не
    двигает, поэтому следующий заход в Настройки пробует снова: вернули файл —
    окно чинится само."""
    from gc_core.runtime_notices import collect_runtime_notices

    cfg_file, profiles = _profile_stack
    corp = profiles / "corp.yml"
    app = _app_that_could_lose_the_flag(cfg_file, profiles)
    corp.unlink()

    app.reload_config_if_changed()  # профиля флага больше нет

    assert app._profile_override == "corp"
    assert (
        app.base_settings().runtime.active_profile == "corp"
    )  # база — прежняя, не подменённая
    notices = [
        n
        for n in collect_runtime_notices()
        if n.code == "config_reload_failed"
    ]
    assert notices, collect_runtime_notices()
    # Причина названа флагом, а не одним лишь config.yml: чинить надо профиль.
    assert 'профиль "corp" из флага --profile' in notices[-1].message

    corp.write_text(_CORP, encoding="utf-8")
    _bump_mtime(corp)
    app.reload_config_if_changed()  # вернули файл — окно чинится само

    assert app._profile_override == "corp"
    assert "corp" in app.base_settings().settings.models


def test_a_broken_write_race_on_the_flag_profile_does_not_lose_the_flag(
    _profile_stack,
):
    """Соседнее окно (или редактор) пишет файл профиля в момент перечитки: половина
    YAML — это не «профиль исчез навсегда», и отличить одно от другого одной
    неудачной попыткой нечем. Поэтому не гасим ничего: прежняя база, причина в
    уведомлении, следующая попытка — на дописанном файле."""
    cfg_file, profiles = _profile_stack
    corp = profiles / "corp.yml"
    app = _app_that_could_lose_the_flag(cfg_file, profiles)
    corp.write_text(
        "models:\n  corp:\n   model: [\n", encoding="utf-8"
    )  # недописанный файл
    _bump_mtime(corp)

    app.reload_config_if_changed()

    assert app._profile_override == "corp"
    assert (
        app.base_settings().runtime.active_profile == "corp"
    )  # и профиль не подменён базой
    corp.write_text(  # запись дописана
        'models:\n  corp:\n    model: "corp-m"\n  extra:\n    model: "x"\nslots:\n  default: "corp"\n',
        encoding="utf-8",
    )
    _bump_mtime(corp, seconds=10.0)
    app.reload_config_if_changed()

    assert "extra" in app.base_settings().settings.models


def test_a_bug_inside_the_loader_is_not_relabelled_as_a_stale_profile(
    _profile_stack, monkeypatch
):
    """`TypeError`/`KeyError` из загрузчика — дефект кода, а не «профиль больше не
    собирается»: под флагом ловим только классы, которыми загрузчик докладывает о
    СОСТОЯНИИ файлов (`ValueError`/`OSError`), остальное летит как есть — иначе
    первый же баг в схеме живёт под видом сломанного профиля."""
    from gc_core import config as cfg_mod

    cfg_file, profiles = _profile_stack
    app = _app_that_could_lose_the_flag(cfg_file, profiles)
    real = cfg_mod.load_settings

    def boom(path=None, **kw):
        # Ломается ТОЛЬКО сборка под флагом: сборка по указателю обязана
        # оставаться рабочей, иначе тест краснел бы на её отказе, а не на том,
        # проглочен ли дефект кода под видом протухшего профиля.
        if kw.get("profile_override") is not None:
            raise TypeError("схема поехала")
        return real(path, **kw)

    monkeypatch.setattr(cfg_mod, "load_settings", boom)

    with pytest.raises(TypeError):
        app.reload_config()


# --- base_settings: единая точка получения базы для ОТКРЫТИЯ экрана ---


def test_base_settings_does_not_reload_when_file_changed(tmp_path):
    """base_settings() НЕ гоняет гейт свежести (дизайн 2026-07-21 §4):
    отдаёт текущую базу как есть. Перечитку по mtime вызывают явно только на
    пути Settings (main._cmd_settings / settings._sync_config_from_app)."""
    app = _mk_app(tmp_path)
    app.reload_config()
    app.active_runner = _FakeRunner(running=False)

    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(
        'backend:\n  completions_url: "http://y/v2"\n', encoding="utf-8"
    )
    _bump_mtime(cfg_path)

    cfg = app.base_settings()

    assert _url(cfg) == "http://x/v1"  # база не перечитана
    assert app.active_runner is not None  # раннер не тронут


def test_base_settings_noop_when_file_untouched(tmp_path, monkeypatch):
    """Файл не менялся — base_settings() не перечитывает и не инвалидирует
    раннер (I1: пересборка раннера тянет MCP-дискавери и держит пользователя на
    старте хода)."""
    app = _mk_app(tmp_path)
    app.reload_config()

    calls = {"load": 0, "invalidate": 0}
    monkeypatch.setattr(
        "gc_core.config.load_settings",
        lambda *_a, **_k: calls.__setitem__("load", calls["load"] + 1) or {},
    )
    orig_invalidate = app.invalidate_runner

    def counting_invalidate():
        calls["invalidate"] += 1
        orig_invalidate()

    app.invalidate_runner = counting_invalidate

    app.base_settings()

    assert calls == {"load": 0, "invalidate": 0}


# --- атомарность перечитки: всё или ничего ----------------------------------------
#
# `reload_config` собирает новое состояние целиком (`_plan_reload`) и применяет его
# одним движением (`_commit_reload`). Сбой на любом шаге после загрузки базы —
# окно ровно в прежнем состоянии, причина — в `/notices`.

_STATE_ATTRS = (
    "_settings",
    "_session_overrides",
    "_session_profile_cache",
    "_effective_cache",
    "_config_mtime_set",
    "_threads_dir",
    "_config_generation",
    "_profile_source",
)


def _state(app):
    return {name: getattr(app, name) for name in _STATE_ATTRS}


def _assert_same_state(before, after):
    for name, value in before.items():
        got = after[name]
        if isinstance(value, (dict, int, str)) or value is None:
            assert got == value, name
        else:
            assert got is value, name


@pytest.fixture
def _typed_app(tmp_path, monkeypatch):
    """Окно, собранное как в main(): типизированные настройки из config.yml,
    каталог тредов привязан на старте, путь связан."""
    from gc_core.config import apply_cli_overrides, load_settings
    from gc_core.config_modules import env_vars
    from gc_core.config_modules.env_vars import EnvVars
    from gc_core.runtime_notices import clear_runtime_notices
    from gc_core.thread_store import resolve_threads_dir
    from gc_tui.runtime_config import apply_tui_runtime

    monkeypatch.setattr(env_vars, "env", EnvVars(gc_home=tmp_path / "gc_home"))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    (tmp_path / "profiles").mkdir()
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        'backend:\n  completions_url: "http://x/v1"\n'
        f'paths:\n  threads_data_path: "{tmp_path / "threads"}"\n',
        encoding="utf-8",
    )
    loaded = apply_tui_runtime(
        apply_cli_overrides(load_settings(str(cfg_file)))
    )
    app = app_mod.TuiApp(
        threads_dir=resolve_threads_dir(loaded),
        system="cli",
        settings=loaded,
    )
    app.set_config_path(str(cfg_file))
    clear_runtime_notices()
    return app, cfg_file


def _url(loaded):
    return loaded.settings.backend.completions_url


def _rewrite(cfg_file, url="http://y/v2", threads=None):
    threads = threads or str(cfg_file.parent / "threads")
    cfg_file.write_text(
        f'backend:\n  completions_url: "{url}"\n'
        f'paths:\n  threads_data_path: "{threads}"\n',
        encoding="utf-8",
    )
    _bump_mtime(cfg_file)


@pytest.mark.parametrize(
    "step",
    ["_mtime_set_for", "_build_effective_entry", "resolve_threads_dir"],
)
def test_reload_failing_after_base_load_changes_nothing(
    _typed_app, monkeypatch, step
):
    """Сбой на шаге ПОСЛЕ загрузки базы: прежняя перечитка успевала подменить
    `_settings`/кэши до санации и снимка мтайма — окно оставалось
    наполовину на новой базе. Теперь — ни одного изменённого поля, раннер не
    тронут, notice `config_reload_failed`."""
    from gc_core.runtime_notices import collect_runtime_notices

    app, cfg_file = _typed_app
    app.effective_settings()  # прогретый кэш — тоже часть состояния
    runner = _FakeRunner(running=False)
    app.active_runner = runner
    before = _state(app)
    _rewrite(cfg_file)

    def boom(*_a, **_k):
        raise RuntimeError("сбой посреди перечитки")

    if step == "resolve_threads_dir":
        monkeypatch.setattr("gc_core.thread_store.resolve_threads_dir", boom)
    elif step == "_build_effective_entry":
        monkeypatch.setattr(app_mod.TuiApp, step, classmethod(boom))
    else:
        monkeypatch.setattr(app, step, boom)

    app._reload_config_or_notice()  # путь гейта и сохранения из /settings

    _assert_same_state(before, _state(app))
    assert app.active_runner is runner
    assert _url(app.effective_settings()) == "http://x/v1"
    assert [n.code for n in collect_runtime_notices()] == [
        "config_reload_failed"
    ]
    monkeypatch.undo()
    app.reload_config_if_changed()  # снимок мтайма не сдвинут — следующая попытка
    assert _url(app.effective_settings()) == "http://y/v2"


def test_reload_failing_session_profile_step_keeps_session_choice(
    _typed_app, monkeypatch
):
    """Сессия выбрала профиль; перечитка падает уже ПОСЛЕ пере-мержа профиля —
    сессионный выбор, его кэш и предохранитель моделей остаются прежними."""
    app, cfg_file = _typed_app
    (cfg_file.parent / "profiles" / "work.yml").write_text(
        'models:\n  w:\n    model: "w-m"\nslots:\n  default: "w"\n',
        encoding="utf-8",
    )
    app.switch_session_profile("work")
    app._circuit_breaker.trip("w", "5xx")
    before = _state(app)
    _rewrite(cfg_file)

    def boom(*_a, **_k):
        raise RuntimeError("сбой")

    monkeypatch.setattr(app, "_mtime_set_for", boom)
    app._reload_config_or_notice()

    _assert_same_state(before, _state(app))
    assert app._session_overrides["profile"] == "work"
    assert app._circuit_breaker.is_open("w")


def test_reload_drops_session_override_invalid_under_new_base(_typed_app):
    """Сессионный слой проверяется под новой базой: ключ, который не проходит
    схему (мета треда старой версии), снимается с notice — а не роняет
    перечитку и не оставляет окно на прежней базе."""
    from gc_core.runtime_notices import collect_runtime_notices

    app, cfg_file = _typed_app
    app.load_session_overrides(
        {"level": "bogus", "tui.text_answer_mode": "allow"}
    )
    _rewrite(cfg_file)

    app.reload_config()

    assert "level" not in app._session_overrides
    assert app._session_overrides["tui.text_answer_mode"] == "allow"
    assert app.effective_settings().settings.tui.text_answer_mode == "allow"
    assert _url(app.effective_settings()) == "http://y/v2"
    dropped = [
        n
        for n in collect_runtime_notices()
        if n.code == "session_override_dropped"
    ]
    assert len(dropped) == 1 and '"level"' in dropped[0].message


def test_restart_flagged_path_change_applies_with_notice(_typed_app, tmp_path):
    """`paths.threads_data_path` (флаг `restart` схемы) перечитка применяет
    сразу: каталог тредов окна и действующие настройки (по ним executor
    соберётся заново) — новые; notice `boot_frozen_pending` в прежней
    редакции. Повторная перечитка без смены пути — без повторного notice."""
    from gc_core.runtime_notices import collect_runtime_notices

    app, cfg_file = _typed_app
    new_dir = str(tmp_path / "elsewhere")
    _rewrite(cfg_file, threads=new_dir)

    app.reload_config()

    assert app._threads_dir == new_dir
    effective = app.effective_settings()
    assert effective.settings.paths.threads_data_path == new_dir
    assert _url(effective) == "http://y/v2"
    frozen = [
        n for n in collect_runtime_notices() if n.code == "boot_frozen_pending"
    ]
    assert len(frozen) == 1
    assert frozen[0].message == (
        "paths.threads_data_path — вступит в силу со следующего запуска"
    )

    _rewrite(cfg_file, url="http://z/v3", threads=new_dir)
    app.reload_config()
    assert app._threads_dir == new_dir
    assert (
        len(
            [
                n
                for n in collect_runtime_notices()
                if n.code == "boot_frozen_pending"
            ]
        )
        == 1
    )


def test_reader_during_reload_sees_one_consistent_snapshot(
    _typed_app, monkeypatch
):
    """Сборка раннера (worker-поток) читает `effective_settings()` в любой
    момент перечитки: посреди сборки нового состояния — целиком прежнее; читатель,
    снявший снимок ДО применения и закончивший ПОСЛЕ, получает прежнее целиком и
    не затирает своим результатом уже применённый кэш."""
    import threading

    app, cfg_file = _typed_app
    app.load_session_overrides({"level": "max"})
    app._effective_cache = None  # читатель обязан собирать сам
    entered = threading.Event()
    release = threading.Event()
    real_build = app_mod.TuiApp._build_effective_entry.__func__

    def gated(cls, base, overrides):
        if threading.current_thread().name == "reader":
            entered.set()
            release.wait(5)
        return real_build(cls, base, overrides)

    monkeypatch.setattr(
        app_mod.TuiApp, "_build_effective_entry", classmethod(gated)
    )
    seen = {}
    reader = threading.Thread(
        target=lambda: seen.setdefault("loaded", app.effective_settings()),
        name="reader",
    )
    reader.start()
    assert entered.wait(5)

    mid_plan = []
    real_mtime = app._mtime_set_for

    def spy(name):
        mid_plan.append(app.effective_settings())
        return real_mtime(name)

    monkeypatch.setattr(app, "_mtime_set_for", spy)
    _rewrite(cfg_file)
    app.reload_config()
    release.set()
    reader.join(5)

    old = seen["loaded"]
    assert _url(old) == "http://x/v1" and old.settings.level == "max"
    assert [_url(x) for x in mid_plan] == ["http://x/v1"]
    current = app.effective_settings()
    assert _url(current) == "http://y/v2" and current.settings.level == "max"
    assert app._effective_cache[1] is current  # кэш перечитки не затёрт
