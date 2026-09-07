"""reload_config: перечитка конфига + сброс кэшированного runner (спека B, B3)."""
import pytest

pytest.importorskip("textual")

import tui.app as app_mod


class _FakeRunner:
    def __init__(self, running):
        self._running = running
    def is_running(self):
        return self._running


def _mk_app(tmp_path):
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text("backend:\n  completions_url: \"http://x/v1\"\n", encoding="utf-8")
    app = app_mod.TuiApp(config={"completions_url": "old"})
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
    assert app._config["completions_url"] == "http://x/v1"
    assert app.active_runner is None                    # следующий submit соберёт свежий executor


def test_reload_config_defers_reset_while_running(tmp_path):
    app = _mk_app(tmp_path)
    runner = _FakeRunner(running=True)
    app.active_runner = runner
    app.reload_config()
    assert app.active_runner is runner                  # живой run не трогаем
    assert app._runner_stale is True
    runner._running = False
    got = app.get_runner()                              # run завершился → пересборка
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
    import tui.services.agent_runner as runner_mod
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
    from tui.app import TuiApp
    import config as cfg_mod
    import thread_store as ts_mod
    from config_modules.profiles import ProfileMeta

    on_disk = {"completions_url": "https://h/v1/chat/completions",
               "chat_completion_headers": {"Authorization": "Bearer sk"}, "model": "m"}
    # файл: без launch_root
    monkeypatch.setattr(cfg_mod, "load_config_ex", lambda p=None, **kw: (
        dict(on_disk), ProfileMeta(active_name=None, ref=None, mtime_set={})
    ))
    monkeypatch.setattr(cfg_mod, "apply_cli_overrides", lambda d: None)
    monkeypatch.setattr(ts_mod, "resolve_threads_dir", lambda c: str(tmp_path / "t"))

    started = dict(on_disk)
    started["launch_root"] = "D:/proj/snapshot-at-start"     # как ставит tui/__init__
    started["console_trace"] = False

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config=started)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.reload_config()
        assert app._config["launch_root"] == "D:/proj/snapshot-at-start"
        assert app._config["console_trace"] is False


# --- reload_config_if_changed: гейт свежести по mtime (два окна, один файл) ---


def test_reload_config_if_changed_noop_without_config_path():
    """_config_path не задан (типично для тестов/раннего старта) — тихий
    no-op, ни чтения, ни падения."""
    app = app_mod.TuiApp(config={"completions_url": "old"})
    app.reload_config_if_changed()
    assert app._config["completions_url"] == "old"


def test_reload_config_if_changed_first_check_after_startup_is_noop(tmp_path, monkeypatch):
    """Самая первая проверка сразу после старта окна — БЕЗ единого явного
    reload_config() до неё, ровно как в main(): конфиг уже загружен в
    конструктор, set_config_path только связывает путь — не должна
    перечитывать конфиг и не должна инвалидировать раннер. База и так
    только что загружена из этого самого файла; если снимок mtime не взят
    сразу при связывании пути, первая проверка видит «файл изменился» и
    зря перечитывает + инвалидирует прогретый раннер."""
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text("backend:\n  completions_url: \"http://x/v1\"\n", encoding="utf-8")
    app = app_mod.TuiApp(config={"completions_url": "http://x/v1"})
    app.set_config_path(str(cfg_file))   # ровно то, что делает main() сразу после конструктора
    app.active_runner = _FakeRunner(running=False)

    calls = {"load": 0, "invalidate": 0}
    monkeypatch.setattr("config.load_config", lambda *_a, **_k: (
        calls.__setitem__("load", calls["load"] + 1) or {}
    ))
    orig_invalidate = app.invalidate_runner

    def counting_invalidate():
        calls["invalidate"] += 1
        orig_invalidate()

    app.invalidate_runner = counting_invalidate

    app.reload_config_if_changed()

    assert calls == {"load": 0, "invalidate": 0}


def test_reload_config_if_changed_skips_when_file_untouched(tmp_path, monkeypatch):
    """Файл не менялся с последнего известного снимка mtime — НИЧЕГО не
    происходит: ни load_config, ни invalidate_runner (I1 — пересборка
    раннера тянет MCP-дискавери и держит юзера на старте хода)."""
    app = _mk_app(tmp_path)
    app.reload_config()   # устанавливает базовый снимок mtime

    calls = {"load": 0, "invalidate": 0}
    monkeypatch.setattr("config.load_config", lambda *_a, **_k: (
        calls.__setitem__("load", calls["load"] + 1) or {}
    ))
    orig_invalidate = app.invalidate_runner

    def counting_invalidate():
        calls["invalidate"] += 1
        orig_invalidate()

    app.invalidate_runner = counting_invalidate

    app.reload_config_if_changed()

    assert calls == {"load": 0, "invalidate": 0}
    assert app._config["completions_url"] == "http://x/v1"


def test_reload_config_if_changed_reloads_when_file_changed(tmp_path):
    """Файл реально изменился (другое окно сохранило умолчание) — база
    перечитывается и раннер инвалидируется, как обычный reload_config."""
    app = _mk_app(tmp_path)
    app.reload_config()
    app.active_runner = _FakeRunner(running=False)

    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text("backend:\n  completions_url: \"http://y/v2\"\n", encoding="utf-8")
    _bump_mtime(cfg_path)

    app.reload_config_if_changed()

    assert app._config["completions_url"] == "http://y/v2"
    assert app.active_runner is None   # инвалидация реально произошла


def test_reload_config_if_changed_keeps_old_base_on_broken_file(tmp_path):
    """Юзер сохранил config.yml в промежуточном/битом состоянии — чтение
    кидает, окно не падает и продолжает работать на прежней базе. Молчать при
    этом нельзя: причина уходит в runtime-уведомления (/notices), иначе
    регрессия загрузки конфига незаметна."""
    from runtime_notices import collect_runtime_notices

    app = _mk_app(tmp_path)
    app.reload_config()
    good_config = dict(app._config)

    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text("backend: [this is not, valid: yaml:", encoding="utf-8")
    _bump_mtime(cfg_path)

    app.reload_config_if_changed()   # не должно бросить исключение

    assert app._config == good_config
    assert [n.code for n in collect_runtime_notices()] == ["config_reload_failed"]


def test_reload_config_if_changed_own_write_does_not_retrigger(tmp_path, monkeypatch):
    """Собственное сохранение (reload_config уже перечитал и обновил снимок
    mtime) не должно вызвать повторное перечитывание на следующей проверке —
    иначе executor пересобирался бы впустую сразу после /settings → Сохранить
    как умолчание."""
    app = _mk_app(tmp_path)
    app.reload_config()   # «сохранение» — сам конфиг тоже перечитан этим вызовом

    calls = {"load": 0}
    monkeypatch.setattr("config.load_config", lambda *_a, **_k: (
        calls.__setitem__("load", calls["load"] + 1) or {}
    ))

    app.reload_config_if_changed()

    assert calls == {"load": 0}


def test_build_real_executor_does_not_reload_config(tmp_path, monkeypatch):
    """Сборка executor больше НЕ перечитывает config.yml (дизайн 2026-07-21 §4):
    свежесть глобала перенесена на путь Settings. Ход агента идёт по текущей базе
    окна; чужая правка файла подхватывается при следующем открытии Settings."""
    app = _mk_app(tmp_path)
    app.reload_config()   # база = http://x/v1

    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text("backend:\n  completions_url: \"http://y/v2\"\n", encoding="utf-8")
    _bump_mtime(cfg_path)

    class _Ctx:
        tools_data = {}
        mcp_status = {}
        logging_path = "/tmp/agent_main_test.log"

    class _Ex:
        def __init__(self, ctx):
            pass
        def stop_run(self):
            pass

    seen_cfg = {}

    def _fake_create_run_context(cfg, **kw):
        seen_cfg.update(cfg)
        return _Ctx()

    monkeypatch.setattr("config.build_auth_connector", lambda cfg: None)
    monkeypatch.setattr("run_context.create_run_context", _fake_create_run_context)
    monkeypatch.setattr("agent_executor.AgentExecutor", _Ex)

    app._build_real_executor()

    assert seen_cfg["completions_url"] == "http://x/v1"   # база НЕ перечитана из файла


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
    from config_modules import env_vars
    from config_modules.env_vars import EnvVars
    from runtime_notices import clear_runtime_notices

    monkeypatch.setattr(env_vars, "env", EnvVars(gc_home=tmp_path / "gc_home"))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        'profile: "work"\nbackend:\n  completions_url: "http://x/v1"\n'
        'models:\n  gc:\n    model: "base-m"\n', encoding="utf-8")
    (profiles / "work.yml").write_text('slots:\n  default: "ghost"\n', encoding="utf-8")
    clear_runtime_notices()
    return cfg_file, profiles


def _app_started_with(cfg_file, profile_override):
    """Приложение, как его собирает main(): конфиг загружен С флагом, путь связан."""
    import config as cfg_mod
    cfg, _meta = cfg_mod.load_config_ex(str(cfg_file), profile_override=profile_override)
    app = app_mod.TuiApp(config=cfg, profile_override=profile_override)
    app.set_config_path(str(cfg_file))
    return app


def test_reload_config_keeps_the_profile_flag_that_bypasses_a_broken_pointer(_profile_stack):
    """`gc-ua --profile ""` на сломанном профиле из указателя: перечитывание обязано
    собираться так же, как старт, — без профиля. Без флага загрузчик падает на
    `slots.default: ghost`, и окно упиралось бы в профиль, который флаг обошёл."""
    cfg_file, _profiles = _profile_stack
    app = _app_started_with(cfg_file, "")
    assert app._config["profile"] == ""

    app.reload_config()                      # без флага здесь ValueError

    assert app._config["profile"] == ""
    assert app.effective_config()["profile"] == ""


def test_reload_config_keeps_the_profile_named_by_the_flag(_profile_stack):
    cfg_file, profiles = _profile_stack
    (profiles / "corp.yml").write_text(
        'models:\n  corp:\n    model: "corp-m"\nslots:\n  default: "corp"\n',
        encoding="utf-8")
    app = _app_started_with(cfg_file, "corp")

    app.reload_config()

    assert app._config["profile"] == "corp"
    assert "corp" in app._config["models"]
    # Мтайм-набор следит за файлом профиля флага, а не за указателем файла.
    assert any("corp.yml" in path for path in app._config_mtime_set)


def test_session_choice_still_wins_over_the_profile_flag(_profile_stack):
    """Выбор из приложения (`/profiles`) сильнее флага запуска — как и сильнее
    указателя файла: сессионное снятие профиля при флаге `corp` действует."""
    cfg_file, profiles = _profile_stack
    (profiles / "corp.yml").write_text(
        'models:\n  corp:\n    model: "corp-m"\nslots:\n  default: "corp"\n',
        encoding="utf-8")
    app = _app_started_with(cfg_file, "corp")
    app.set_session_override("profile", "")

    app.reload_config()

    assert app._config["profile"] == "corp"            # база окна — по флагу
    assert app.effective_config()["profile"] == ""     # действует выбор сессии


_CORP = 'models:\n  corp:\n    model: "corp-m"\nslots:\n  default: "corp"\n'


def _app_that_could_lose_the_flag(cfg_file, profiles):
    """Стек, из которого флаг РЕАЛЬНО может выпасть: указатель базы пуст (без
    профиля база собирается), профиль флага `corp` — на диске.

    Дефолтный `_profile_stack` для этого не годится: там указатель ведёт на битый
    `work`, и подмена профиля указателем упала бы сама — «флаг на месте» зеленело
    бы, ничего не проверив."""
    cfg_file.write_text(
        'profile: ""\nbackend:\n  completions_url: "http://x/v1"\n'
        'models:\n  gc:\n    model: "base-m"\n', encoding="utf-8")
    (profiles / "corp.yml").write_text(_CORP, encoding="utf-8")
    return _app_started_with(cfg_file, "corp")


def test_a_vanished_flag_profile_keeps_the_flag_and_the_previous_base(_profile_stack):
    """Профиль флага пропал под ногами: окно остаётся на прежней базе с причиной в
    `/notices` — той же деградацией, что и на битом config.yml, — и НЕ подменяет
    профиль указателем из файла.

    Флаг переживает отказ: он свойство запуска, а не файла, и снятый однажды не
    возвращается — `--profile ""` тем же вечером молча воскресил бы сломанный
    профиль на первом сохранении из /settings. Снимок мтайм-набора отказ не
    двигает, поэтому следующий заход в Настройки пробует снова: вернули файл —
    окно чинится само."""
    from runtime_notices import collect_runtime_notices
    cfg_file, profiles = _profile_stack
    corp = profiles / "corp.yml"
    app = _app_that_could_lose_the_flag(cfg_file, profiles)
    corp.unlink()

    app.reload_config_if_changed()           # профиля флага больше нет

    assert app._profile_override == "corp"
    assert app._config["profile"] == "corp"          # база — прежняя, не подменённая
    notices = [n for n in collect_runtime_notices() if n.code == "config_reload_failed"]
    assert notices, collect_runtime_notices()
    # Причина названа флагом, а не одним лишь config.yml: чинить надо профиль.
    assert 'профиль "corp" из флага --profile' in notices[-1].message

    corp.write_text(_CORP, encoding="utf-8")
    _bump_mtime(corp)
    app.reload_config_if_changed()           # вернули файл — окно чинится само

    assert app._profile_override == "corp"
    assert "corp" in app._config["models"]


def test_a_broken_write_race_on_the_flag_profile_does_not_lose_the_flag(_profile_stack):
    """Соседнее окно (или редактор) пишет файл профиля в момент перечитки: половина
    YAML — это не «профиль исчез навсегда», и отличить одно от другого одной
    неудачной попыткой нечем. Поэтому не гасим ничего: прежняя база, причина в
    уведомлении, следующая попытка — на дописанном файле."""
    cfg_file, profiles = _profile_stack
    corp = profiles / "corp.yml"
    app = _app_that_could_lose_the_flag(cfg_file, profiles)
    corp.write_text("models:\n  corp:\n   model: [\n", encoding="utf-8")   # недописанный файл
    _bump_mtime(corp)

    app.reload_config_if_changed()

    assert app._profile_override == "corp"
    assert app._config["profile"] == "corp"          # и профиль не подменён базой
    corp.write_text(                                                       # запись дописана
        'models:\n  corp:\n    model: "corp-m"\n  extra:\n    model: "x"\n'
        'slots:\n  default: "corp"\n', encoding="utf-8")
    _bump_mtime(corp, seconds=10.0)
    app.reload_config_if_changed()

    assert "extra" in app._config["models"]


def test_a_bug_inside_the_loader_is_not_relabelled_as_a_stale_profile(
        _profile_stack, monkeypatch):
    """`TypeError`/`KeyError` из загрузчика — дефект кода, а не «профиль больше не
    собирается»: под флагом ловим только классы, которыми загрузчик докладывает о
    СОСТОЯНИИ файлов (`ValueError`/`OSError`), остальное летит как есть — иначе
    первый же баг в схеме живёт под видом сломанного профиля."""
    import config as cfg_mod
    cfg_file, profiles = _profile_stack
    app = _app_that_could_lose_the_flag(cfg_file, profiles)
    real = cfg_mod.load_config_ex

    def boom(path=None, **kw):
        # Ломается ТОЛЬКО сборка под флагом: сборка по указателю обязана
        # оставаться рабочей, иначе тест краснел бы на её отказе, а не на том,
        # проглочен ли дефект кода под видом протухшего профиля.
        if kw.get("profile_override") is not None:
            raise TypeError("схема поехала")
        return real(path, **kw)

    monkeypatch.setattr(cfg_mod, "load_config_ex", boom)

    with pytest.raises(TypeError):
        app.reload_config()


# --- screen_config: единая точка получения базы для ОТКРЫТИЯ экрана ---


def test_screen_config_does_not_reload_when_file_changed(tmp_path):
    """screen_config() больше НЕ гоняет гейт свежести (дизайн 2026-07-21 §4):
    отдаёт текущую базу как есть. Перечитку по mtime вызывают явно только на
    пути Settings (main._cmd_settings / settings._sync_config_from_app)."""
    app = _mk_app(tmp_path)
    app.reload_config()
    app.active_runner = _FakeRunner(running=False)

    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text("backend:\n  completions_url: \"http://y/v2\"\n", encoding="utf-8")
    _bump_mtime(cfg_path)

    cfg = app.screen_config()

    assert cfg["completions_url"] == "http://x/v1"   # база не перечитана
    assert app.active_runner is not None              # раннер не тронут


def test_screen_config_noop_when_file_untouched(tmp_path, monkeypatch):
    """Файл не менялся — screen_config() не перечитывает и не инвалидирует
    раннер (I1: пересборка раннера тянет MCP-дискавери и держит пользователя на
    старте хода)."""
    app = _mk_app(tmp_path)
    app.reload_config()

    calls = {"load": 0, "invalidate": 0}
    monkeypatch.setattr("config.load_config", lambda *_a, **_k: (
        calls.__setitem__("load", calls["load"] + 1) or {}
    ))
    orig_invalidate = app.invalidate_runner

    def counting_invalidate():
        calls["invalidate"] += 1
        orig_invalidate()

    app.invalidate_runner = counting_invalidate

    app.screen_config()

    assert calls == {"load": 0, "invalidate": 0}
