"""Сессионная механика TUI: profile/level (план «Профили v2, волна A», Task 11).

См. .superpowers/sdd/2026-07-27-profiles-wave-a-plan/task-11-brief.md,
docs/specs/2026-07-23-profiles-design.md §6.4, §8, §9, §3.2, §3.3, §3.5.
"""

from gc_testkit.settings import make_settings
from gc_testkit.tui_support import tui_settings
import os
import time
from pathlib import Path

import pytest
import yaml

pytest.importorskip("textual")

from gc_core.config import load_settings
from gc_core.runtime_notices import (
    clear_runtime_notices,
    collect_runtime_notices,
)
from gc_tui.app import TuiApp
from gc_tui.services.stores.file_store import FileTaskStore
from gc_tui.session_settings import SESSION_KEYS, read_breaker_from_meta


def _write_yaml(path: Path, body: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return path


def _bump_mtime(path: Path, seconds: float = 5.0) -> None:
    """Гарантированно другой mtime — не полагаемся на разрешение файловой системы."""
    t = time.time() + seconds
    os.utime(path, (t, t))


_MINIMAL_BASE = {
    "backend": {
        "api_mode": "chat_completions",
        "completions_url": "http://base/v1/chat/completions",
    },
}


def _app_with_base(tmp_path: Path) -> TuiApp:
    """App на реальном config.yml без профиля — база для сценариев с
    load_settings(profile_override=...) (профиль-файлы кладём «рядом с базой»,
    root 2 из profile_roots — не завязано на cwd)."""
    base = tmp_path / "config.yml"
    _write_yaml(base, _MINIMAL_BASE)
    cfg = load_settings(str(base))
    app = TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", settings=cfg
    )
    app.set_config_path(str(base))
    return app


def _write_work_profile(
    tmp_path: Path, url: str = "http://work/v1/chat/completions"
) -> Path:
    return _write_yaml(
        tmp_path / "profiles" / "work.yml",
        {
            "models": {"flash": {"url": url, "token": ""}},
            "slots": {"default": "flash"},
        },
    )


# --- 1. `level` — обычный сессионный ключ: whitelist + restore на attach ------------


def test_level_registered_in_session_keys():
    assert "level" in SESSION_KEYS
    assert SESSION_KEYS["level"].dotted == "level"


@pytest.mark.asyncio
async def test_level_restored_on_attach(tmp_path):
    store = FileTaskStore(threads_dir=str(tmp_path / "threads"), system="cli")
    store.update_tui_meta("t-level", {"session_settings": {"level": "max"}})
    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings({"model": "M", "level": "default"}),
    )
    async with app.run_test() as pilot:
        pilot.app.screen.attach_active_task("t-level")
        await pilot.pause(0.1)
        assert app.effective_settings().settings.level == "max"


# --- 2. `/level` не инвалидирует раннер; провайдер видит смену немедленно ----------


def test_level_change_does_not_invalidate_runner(tmp_path):
    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings({"model": "M", "level": "default"}),
    )
    calls = {"n": 0}
    app.invalidate_runner = lambda: calls.__setitem__("n", calls["n"] + 1)
    app.apply_session_override("level", "light")
    assert calls["n"] == 0


def test_level_provider_reflects_session_override_without_rebuild(tmp_path):
    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings({"model": "M", "level": "default"}),
    )
    assert app._level_provider() == "default"
    app.set_session_override("level", "max")
    assert app._level_provider() == "max"  # без пересборки executor


# --- 3. session-профиль → пере-мерж effective_settings -------------------------------


def test_switch_session_profile_remerges_effective_settings(tmp_path):
    _write_work_profile(tmp_path)
    app = _app_with_base(tmp_path)
    assert "flash" not in (app.effective_settings().settings.models or {})
    app.switch_session_profile("work")
    assert "flash" in app.effective_settings().settings.models
    assert app.effective_settings().runtime.active_profile == "work"


def test_switch_session_profile_signals_the_screen(tmp_path, monkeypatch):
    """Смена профиля сессии обязана дойти до экрана — тем же сигналом, что и
    перечитка конфига с диска.

    Экран кэширует куски конфига один раз на секцию (голосовой flow строится на
    voice-секцию), и без сигнала `/profiles work`, где в профиле своя секция
    `voice`, оставлял бы Ctrl+T диктовать на ПРЕЖНИЙ endpoint: витрины и футер
    профиль уже показывают, а голос молча идёт мимо. Сигнал один на оба пути —
    `reload_config` и этот; отдельного канала для профиля не заводим."""
    _write_work_profile(tmp_path)
    app = _app_with_base(tmp_path)
    seen = []

    class _FakeMain:
        def config_reloaded(self):
            seen.append(True)

    monkeypatch.setattr(type(app), "is_running", property(lambda self: True))
    monkeypatch.setattr(app, "_main_screen", lambda: _FakeMain())
    app.switch_session_profile("work")
    assert seen == [True]


def test_session_layer_stays_above_profile_in_both_directions(tmp_path):
    """§2, позиционное правило: сессия — ВЕРХНИЙ слой, а не снимок момента.

    Обе стороны проверяются одним сценарием, потому что ломаются они по
    отдельности. Сначала тоггл, потом профиль: пере-мерж не имеет права смыть
    сессионное значение ключом из профиля. Потом наоборот: тоггл поверх уже
    активного профиля обязан лечь сверху. Между ними — ключ, которого сессия НЕ
    трогала: он берётся из профиля, иначе «сессия сверху» выродилась бы в
    «сессия вместо профиля».
    """
    _write_yaml(
        tmp_path / "profiles" / "work.yml",
        {
            "models": {
                "flash": {
                    "url": "http://work/v1/chat/completions",
                    "token": "",
                }
            },
            "slots": {"default": "flash"},
            "level": "max",
        },
    )
    app = _app_with_base(tmp_path)

    # 1) сессия раньше профиля — профиль её не смывает
    app.set_session_override("level", "light")
    app.switch_session_profile("work")
    assert app.effective_settings().settings.level == "light"
    assert (
        "flash" in app.effective_settings().settings.models
    )  # прочее из профиля пришло

    # 2) сессия позже профиля — ложится сверху
    app.set_session_override("level", "default")
    assert app.effective_settings().settings.level == "default"

    # 3) снять сессионный ключ — из-под него проступает значение профиля
    app._session_overrides.pop("level")
    assert app.effective_settings().settings.level == "max"


# --- 4. кэш: повторные вызовы effective_settings не гоняют load_settings ----------


def test_effective_settings_caches_profile_remerge(tmp_path, monkeypatch):
    _write_work_profile(tmp_path)
    app = _app_with_base(tmp_path)
    app.switch_session_profile("work")

    from gc_core import config as cfg_mod

    calls = {"n": 0}
    orig = cfg_mod.load_settings

    def counting(*a, **kw):
        calls["n"] += 1
        return orig(*a, **kw)

    monkeypatch.setattr(cfg_mod, "load_settings", counting)
    app.effective_settings()
    app.effective_settings()
    app.effective_settings()
    assert calls["n"] == 0  # уже закэшировано в switch_session_profile


def test_reload_config_refreshes_stale_profile_cache(tmp_path):
    """reload_config() чистит кэш пере-мержа (мтайм-набор базы сменился) — следующее
    обращение к effective_settings() пере-мержит заново, а не отдаёт запись,
    закэшированную под СТАРЫЙ мтайм-набор."""
    _write_work_profile(tmp_path)
    app = _app_with_base(tmp_path)
    app.switch_session_profile("work")
    old_key = app._session_profile_cache[0]

    base = Path(app._config_path)
    _write_yaml(base, {**_MINIMAL_BASE, "level": "max"})
    _bump_mtime(base)
    app.reload_config()

    assert app._session_profile_cache is not None
    new_key = app._session_profile_cache[0]
    assert new_key != old_key
    assert new_key[0] == frozenset((app._config_mtime_set or {}).items())


# --- Ревью Task 11: мтайм-набор следит за СЕССИОННО выбранным профилем, не только --
# --- за базовым указателем («выбор, не снимок», §3.3) ------------------------------


def test_reload_config_if_changed_picks_up_edit_of_session_selected_profile(
    tmp_path,
):
    """Репро ревьюера: база `profile: ""`, сессия выбрала `work` (базовый указатель
    её не называет). Правка work.yml на диске обязана всплыть на ближайшей
    перечитке (reload_config_if_changed — существующий тикер), а не остаться
    невидимой бессрочно."""
    _write_work_profile(tmp_path, url="http://work/v1/chat/completions")
    app = _app_with_base(tmp_path)
    app.switch_session_profile("work")
    assert (
        app.effective_settings().settings.models["flash"].url
        == "http://work/v1/chat/completions"
    )

    work_path = tmp_path / "profiles" / "work.yml"
    _write_yaml(
        work_path,
        {
            "models": {
                "flash": {
                    "url": "http://work-v2/v1/chat/completions",
                    "token": "",
                }
            },
            "slots": {"default": "flash"},
        },
    )
    _bump_mtime(work_path)

    app.reload_config_if_changed()

    assert (
        app.effective_settings().settings.models["flash"].url
        == "http://work-v2/v1/chat/completions"
    )


def test_reload_config_if_changed_sanitizes_session_profile_broken_by_edit(
    tmp_path,
):
    """Тот же сценарий, но правка ЛОМАЕТ файл (а не просто меняет значение):
    ближайшая перечитка обязана дойти до sanitize_session_profile (снять оверрайд +
    notice), а не тихо проглотить ошибку где-то по пути."""
    clear_runtime_notices()
    _write_work_profile(tmp_path)
    app = _app_with_base(tmp_path)
    app.switch_session_profile("work")
    assert "flash" in app.effective_settings().settings.models

    work_path = tmp_path / "profiles" / "work.yml"
    work_path.write_text(
        "models: [this is not, valid: yaml:", encoding="utf-8"
    )
    _bump_mtime(work_path)

    app.reload_config_if_changed()

    assert "profile" not in app._session_overrides
    codes = [n.code for n in collect_runtime_notices()]
    assert "session_profile_stale" in codes


def test_profile_remerge_notices_once_on_race_and_falls_back(tmp_path):
    """`_profile_remerge_settings`, вызванный МИМО reload_config/attach (гонка —
    файл ломается между двумя рендер-кадрами без промежуточной перечитки), не
    глотает ошибку молча: notice один раз на эпизод (тот же ключ кэша — no-op на
    повторных кадрах), фоллбэк на прежнюю базу (база окна — существующий
    фоллбэк, не тронута)."""
    clear_runtime_notices()
    _write_work_profile(tmp_path)
    app = _app_with_base(tmp_path)
    app.switch_session_profile("work")
    assert "flash" in app.effective_settings().settings.models

    # Файл ломается ПОД НОГАМИ без bump'а мтайма (иначе сработала бы обычная
    # перечитка) — прямой вызов _profile_remerge_settings с изменившимся содержимым,
    # но старым кэш-ключом, эмулирует гонку "испортилось между кадрами".
    work_path = tmp_path / "profiles" / "work.yml"
    app._session_profile_cache = (
        None  # форсируем реальный поход в load_settings
    )
    work_path.write_text("models: [broken", encoding="utf-8")

    cfg1 = app._profile_remerge_settings("work")
    assert (
        cfg1 is app.base_settings()
    )  # фоллбэк — прежняя (базовая) рабочая конфигурация
    codes = [n.code for n in collect_runtime_notices()]
    assert codes.count("session_profile_remerge_failed") == 1

    cfg2 = app._profile_remerge_settings(
        "work"
    )  # тот же (испорченный) ключ — кэш, БЕЗ повторного notice
    assert cfg2 is app.base_settings()
    codes_after = [n.code for n in collect_runtime_notices()]
    assert codes_after.count("session_profile_remerge_failed") == 1


# --- 5. Санация битого профиля — сессионная ветка vs конфиг-указатель (§9.4) -------


def test_sanitize_drops_stale_session_profile_with_notice(tmp_path):
    clear_runtime_notices()
    app = _app_with_base(tmp_path)  # никакого profiles/ghost.yml на диске
    app.set_session_override("profile", "ghost")
    calls = {"n": 0}
    app.invalidate_runner = lambda: calls.__setitem__("n", calls["n"] + 1)

    app.sanitize_session_profile()

    assert "profile" not in app._session_overrides
    codes = [n.code for n in collect_runtime_notices()]
    assert "session_profile_stale" in codes
    msg = next(
        n.message
        for n in collect_runtime_notices()
        if n.code == "session_profile_stale"
    )
    assert "ghost" in msg
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_attach_sanitizes_stale_session_profile(tmp_path):
    clear_runtime_notices()
    base = tmp_path / "config.yml"
    _write_yaml(base, _MINIMAL_BASE)
    cfg = load_settings(str(base))
    store = FileTaskStore(threads_dir=str(tmp_path / "threads"), system="cli")
    store.update_tui_meta(
        "t-stale", {"session_settings": {"profile": "ghost"}}
    )
    app = TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", settings=cfg
    )
    app.set_config_path(str(base))
    async with app.run_test() as pilot:
        pilot.app.screen.attach_active_task("t-stale")
        await pilot.pause(0.1)
        assert "profile" not in app._session_overrides
        codes = [n.code for n in collect_runtime_notices()]
        assert "session_profile_stale" in codes


def test_reload_config_if_changed_keeps_base_when_config_profile_pointer_broken(
    tmp_path,
):
    """Ветка «выбран конфигом» (§9.4): исчезнувший/невалидный указатель В ФАЙЛЕ
    базового конфига — существующий канон reload_config_if_changed (config_reload_failed,
    прежняя база), НЕ санация сессии — это разные ветки одной спеки."""
    clear_runtime_notices()
    app = _app_with_base(tmp_path)  # profile: "" — валидно на старте
    good_config = app.base_settings()

    base = Path(app._config_path)
    _write_yaml(base, {**_MINIMAL_BASE, "profile": "ghost"})
    _bump_mtime(base)

    app.reload_config_if_changed()

    assert app.base_settings() is good_config
    assert [n.code for n in collect_runtime_notices()] == [
        "config_reload_failed"
    ]


# --- 6. Предохранитель: персист/restore/сброс по mtime и по смене профиля ----------


def test_circuit_breaker_persists_and_restores_across_windows(tmp_path):
    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings({"model": "M"}),
    )
    app.tui_state.active_task_id = "t-breaker"
    app._circuit_breaker.trip("flash", "недоступна")
    app.persist_circuit_breaker()

    store = FileTaskStore(threads_dir=str(tmp_path / "threads"), system="cli")
    snapshot = read_breaker_from_meta(store.get_task_meta("t-breaker"))
    assert snapshot.get("flash") == "недоступна"

    app2 = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings({"model": "M"}),
    )
    app2.load_circuit_breaker(snapshot)
    assert app2._circuit_breaker.is_open("flash") is True


def test_run_completion_persists_circuit_breaker(tmp_path):
    """Проводка: `_on_runner_complete` персистит предохранитель — трипы во время
    рана обязаны пережить пересборку executor следующего хода."""
    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings({"model": "M"}),
    )
    app.tui_state.active_task_id = "t-run"

    class _Exec:
        last_terminal_outcome = "finished"

        def add_message(self, m):
            pass

        async def arun(self, *a, **k):
            app._circuit_breaker.trip("flash", "недоступна")
            return "done"

        def stop_run(self):
            pass

    app._executor_factory = lambda: _Exec()
    runner = app.get_runner()
    runner.start(query="q", thread_id="t-run")
    runner.wait(timeout=5)

    store = FileTaskStore(threads_dir=str(tmp_path / "threads"), system="cli")
    snapshot = read_breaker_from_meta(store.get_task_meta("t-run"))
    assert snapshot.get("flash") == "недоступна"


def test_run_failure_persists_circuit_breaker(tmp_path):
    """Ран упал (бэкенд лёг) — путь `_emit_error`, а не `_on_runner_complete`.
    Трип обязан пережить падение: иначе следующий ход снова ломится в мёртвый
    алиас, и §8 не работает ровно там, где нужен."""
    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings({"model": "M"}),
    )
    app.tui_state.active_task_id = "t-fail"

    class _Exec:
        last_terminal_outcome = None

        def add_message(self, m):
            pass

        async def arun(self, *a, **k):
            app._circuit_breaker.trip("flash", "5xx")
            raise RuntimeError("backend down")

        def stop_run(self):
            pass

    app._executor_factory = lambda: _Exec()
    runner = app.get_runner()
    runner.start(query="q", thread_id="t-fail")
    runner.wait(timeout=5)

    store = FileTaskStore(threads_dir=str(tmp_path / "threads"), system="cli")
    snapshot = read_breaker_from_meta(store.get_task_meta("t-fail"))
    assert snapshot.get("flash") == "5xx"


def test_run_persists_circuit_breaker_even_after_runner_replaced(tmp_path):
    """Ctrl+C: `_stop_and_replace_runner` обнуляет `active_runner`, и проверка
    владельца в колбэке отсекает UI-поля чужого раннера. Предохранитель под неё
    попадать не должен — объект один на приложение, трипы уже случились."""
    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings({"model": "M"}),
    )
    app.tui_state.active_task_id = "t-stop"

    class _Exec:
        last_terminal_outcome = "stopped"

        def add_message(self, m):
            pass

        async def arun(self, *a, **k):
            app._circuit_breaker.trip("flash", "недоступна")
            app.active_runner = None  # как _stop_and_replace_runner
            return "done"

        def stop_run(self):
            pass

    app._executor_factory = lambda: _Exec()
    runner = app.get_runner()
    runner.start(query="q", thread_id="t-stop")
    runner.wait(timeout=5)

    store = FileTaskStore(threads_dir=str(tmp_path / "threads"), system="cli")
    snapshot = read_breaker_from_meta(store.get_task_meta("t-stop"))
    assert snapshot.get("flash") == "недоступна"


def test_breaker_persist_is_marshalled_to_ui_thread(tmp_path):
    """`persist_circuit_breaker` — read-modify-write meta.json, а UI-поток пишет
    туда же на спаде is_running (статус треда, session overrides). Запись прямо
    с воркер-потока теряет одну из правок и может оставить обрезанный файл —
    мост на UI-поток обязателен, как у остальных колбэков раннера."""
    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings({"model": "M"}),
    )
    app.tui_state.active_task_id = "t-thread"
    marshalled = []

    def _fake_call_from_thread(fn, *a, **k):
        marshalled.append(fn)
        return fn(*a, **k)

    app.call_from_thread = _fake_call_from_thread

    class _Exec:
        last_terminal_outcome = "finished"

        def add_message(self, m):
            pass

        async def arun(self, *a, **k):
            app._circuit_breaker.trip("flash", "недоступна")
            return "done"

        def stop_run(self):
            pass

    app._executor_factory = lambda: _Exec()
    runner = app.get_runner()
    runner.start(query="q", thread_id="t-thread")
    runner.wait(timeout=5)

    assert app.persist_circuit_breaker in marshalled


def test_reload_config_resets_breaker_on_mtime_change(tmp_path):
    app = _app_with_base(tmp_path)
    app._circuit_breaker.trip("flash", "недоступна")

    base = Path(app._config_path)
    _write_yaml(base, {**_MINIMAL_BASE, "level": "max"})
    _bump_mtime(base)

    app.reload_config()

    assert app._circuit_breaker.is_open("flash") is False


def test_switch_session_profile_resets_breaker(tmp_path):
    _write_work_profile(tmp_path)
    app = _app_with_base(tmp_path)
    app._circuit_breaker.trip("flash", "недоступна")

    app.switch_session_profile("work")

    assert app._circuit_breaker.is_open("flash") is False


def test_load_circuit_breaker_restores_in_place_keeping_executor_wired(
    tmp_path,
):
    """Important-1: attach нового треда НЕ подменяет объект предохранителя —
    прогретый executor держит ссылку на ТОТ ЖЕ объект. Иначе трипы рана уходят в
    осиротевший breaker, а persist_circuit_breaker пишет пустой новый (§8)."""
    from gc_core.model_router import ModelRouter

    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings({"model": "M"}),
    )
    app.tui_state.active_task_id = "t-2"

    # executor-router собран с текущим breaker — как _build_real_executor (circuit_breaker=self._circuit_breaker)
    breaker_at_build = app._circuit_breaker
    executor_router = ModelRouter(
        make_settings(
            {"models": {"flash": {}}, "slots": {"default": "flash"}}
        ),
        level_provider=lambda: "default",
        breaker=app._circuit_breaker,
    )

    # attach другого треда с непустым персистированным снапшотом
    app.load_circuit_breaker({"deepseek-max": "5xx"})

    # объект не подменён — executor видит новое содержимое (a)/(c)
    assert app._circuit_breaker is breaker_at_build
    assert executor_router.breaker is app._circuit_breaker
    assert executor_router.breaker.is_open("deepseek-max") is True

    # трип рана через тот же объект виден в persist (b)
    executor_router.breaker.trip("flash", "run trip")
    app.persist_circuit_breaker()
    store = FileTaskStore(threads_dir=str(tmp_path / "threads"), system="cli")
    snapshot = read_breaker_from_meta(store.get_task_meta("t-2"))
    assert snapshot.get("flash") == "run trip"
    assert snapshot.get("deepseek-max") == "5xx"


@pytest.mark.asyncio
async def test_footer_model_name_survives_open_active_alias(tmp_path):
    """Important-3: открытый активный алиас не должен молча замораживать футер —
    `_footer_model_name` показывает модель честно, без AliasCircuitOpenError."""
    cfg = {
        "backend_api_mode": "chat_completions",
        "models": {"flash": {}},
        "slots": {"default": "flash"},
        "level": "default",
    }
    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings(cfg),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        app._circuit_breaker.trip("flash", "5xx")
        assert scr._footer_model_name() == "flash"


@pytest.mark.asyncio
async def test_footer_model_name_hides_unset_sentinel(tmp_path):
    """Легаси-режим (пустой реестр) с ещё не заполненным `agent.model` (шаблонный
    сентинел, Task 14) — футер не должен показывать сырой плейсхолдер как имя модели."""
    from gc_core import onboarding

    cfg = {
        "backend_api_mode": "chat_completions",
        "model": onboarding.MODEL_SENTINEL,
    }
    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings(cfg),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        assert scr._footer_model_name() != onboarding.MODEL_SENTINEL


@pytest.mark.asyncio
async def test_status_survives_open_active_alias(tmp_path):
    """Important-3: `/status` при открытом активном алиасе не должен ломаться."""
    cfg = {
        "backend_api_mode": "chat_completions",
        "models": {"flash": {}},
        "slots": {"default": "flash"},
        "level": "default",
    }
    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings(cfg),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        app._circuit_breaker.trip("flash", "5xx")
        scr._cmd_status("")  # не должно бросить AliasCircuitOpenError
        await pilot.pause(0.05)


@pytest.mark.asyncio
async def test_new_session_thread_gets_fresh_breaker(tmp_path):
    """Новая сессия (другой тред без персистированного предохранителя) не
    наследует трипы окна (§8: «сброс — новая сессия»)."""
    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings({"model": "M"}),
    )
    app.tui_state.active_task_id = "t-old"
    app._circuit_breaker.trip("flash", "недоступна")
    async with app.run_test() as pilot:
        pilot.app.screen.attach_active_task("t-new")
        await pilot.pause(0.1)
        assert app._circuit_breaker.is_open("flash") is False


# --- 7. Boot-frozen-diff при пересборке (спека §3.2) --------------------------------


def test_boot_frozen_diff_notice_on_profile_switch(tmp_path):
    clear_runtime_notices()
    threads_override = str(tmp_path / "work_threads")
    _write_yaml(
        tmp_path / "profiles" / "work.yml",
        {"paths": {"threads_data_path": threads_override}},
    )
    app = _app_with_base(tmp_path)
    assert (
        app.effective_settings().settings.paths.threads_data_path
        != threads_override
    )

    app.switch_session_profile("work")

    assert (
        app.effective_settings().settings.paths.threads_data_path
        == threads_override
    )
    codes = [n.code for n in collect_runtime_notices()]
    assert "boot_frozen_pending" in codes
    msg = next(
        n.message
        for n in collect_runtime_notices()
        if n.code == "boot_frozen_pending"
    )
    assert "paths.threads_data_path" in msg
    assert "следующего запуска" in msg


def test_boot_frozen_diff_no_notice_when_unchanged(tmp_path):
    _write_work_profile(tmp_path)  # профиль не трогает paths.*
    clear_runtime_notices()
    app = _app_with_base(tmp_path)
    app.switch_session_profile("work")
    codes = [n.code for n in collect_runtime_notices()]
    assert "boot_frozen_pending" not in codes


# --- Живой ран: смена профиля откладывается до завершения + notice -----------------


def test_switch_session_profile_defers_while_run_is_live(tmp_path):
    clear_runtime_notices()
    _write_work_profile(tmp_path)
    app = _app_with_base(tmp_path)

    class _LiveRunner:
        def is_running(self):
            return True

    app.active_runner = _LiveRunner()
    app.switch_session_profile("work")

    assert app._runner_stale is True
    assert app.active_runner is not None  # не выброшен — довершит ход
    codes = [n.code for n in collect_runtime_notices()]
    assert "profile_switch_deferred" in codes


# --- Item 8: notice активации проектного профиля (спека §3.5) ----------------------


def test_project_profile_activation_notice(tmp_path, monkeypatch):
    clear_runtime_notices()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)
    base = tmp_path / "config.yml"
    _write_yaml(base, _MINIMAL_BASE)
    _write_yaml(
        project_dir / ".gc" / "profiles" / "work.yml",
        {
            "models": {
                "flash": {
                    "url": "http://proj/v1/chat/completions",
                    "token": "",
                }
            },
            "slots": {"default": "flash"},
        },
    )
    cfg = load_settings(str(base))
    app = TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", settings=cfg
    )
    app.set_config_path(str(base))

    app.switch_session_profile("work")

    codes = [n.code for n in collect_runtime_notices()]
    assert "project_profile_activated" in codes
    msg = next(
        n.message
        for n in collect_runtime_notices()
        if n.code == "project_profile_activated"
    )
    assert "work" in msg and "proj/v1" in msg


# --- 9. Смена ИСТОЧНИКА активного профиля под ногами (спека §3.1/§14) --------------


def test_notice_when_project_namesake_takes_over_active_profile(
    tmp_path, monkeypatch
):
    """`git pull` принёс `.gc/profiles/work.yml` — активный профиль того же имени
    молча переехал бы с базового корня на проектный, сменив модели, адреса и
    токены. Имя в футере при этом не меняется, и заметить подмену не по чему.

    Мтайм-набор такой переезд уже видит (оба кандидатных пути в нём есть, у
    несуществующего — `None`), а `reload_config_if_changed` на него срабатывает;
    не хватало сравнения самого источника «до/после» и уведомления.
    """
    clear_runtime_notices()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)

    base = tmp_path / "config.yml"
    _write_yaml(base, {**_MINIMAL_BASE, "profile": "work"})
    base_profile = _write_yaml(
        tmp_path / "profiles" / "work.yml",
        {
            "models": {
                "flash": {
                    "url": "http://base-root/v1/chat/completions",
                    "token": "",
                }
            },
            "slots": {"default": "flash"},
        },
    )

    cfg = load_settings(str(base))
    app = TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", settings=cfg
    )
    app.set_config_path(str(base))
    assert app.active_profile_ref().root_kind == "base"

    # приехал проектный тёзка — он выигрывает по §3.1
    proj_profile = _write_yaml(
        project_dir / ".gc" / "profiles" / "work.yml",
        {
            "models": {
                "flash": {
                    "url": "http://project-root/v1/chat/completions",
                    "token": "",
                }
            },
            "slots": {"default": "flash"},
        },
    )
    app.reload_config_if_changed()

    assert app.active_profile_ref().root_kind == "project"
    notices = collect_runtime_notices()
    match = [n for n in notices if n.code == "profile_source_changed"]
    assert match, (
        f"нет notice о смене источника профиля; коды: {[n.code for n in notices]}"
    )
    msg = match[0].message
    assert "work" in msg
    assert str(proj_profile) in msg  # куда переехал
    assert str(base_profile) in msg  # откуда


def test_no_source_notice_when_profile_file_merely_edited(
    tmp_path, monkeypatch
):
    """Обратная половина: правка того же файла — не смена источника. Без неё
    notice сыпался бы на каждое сохранение профиля из /settings и его перестали
    бы читать."""
    clear_runtime_notices()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)

    base = tmp_path / "config.yml"
    _write_yaml(base, {**_MINIMAL_BASE, "profile": "work"})
    profile = _write_yaml(
        tmp_path / "profiles" / "work.yml",
        {
            "models": {
                "flash": {"url": "http://one/v1/chat/completions", "token": ""}
            },
            "slots": {"default": "flash"},
        },
    )

    cfg = load_settings(str(base))
    app = TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", settings=cfg
    )
    app.set_config_path(str(base))

    _write_yaml(
        profile,
        {
            "models": {
                "flash": {"url": "http://two/v1/chat/completions", "token": ""}
            },
            "slots": {"default": "flash"},
        },
    )
    _bump_mtime(profile)
    app.reload_config_if_changed()

    assert [
        n
        for n in collect_runtime_notices()
        if n.code == "profile_source_changed"
    ] == []


def test_no_source_notice_when_the_profile_name_itself_changed(
    tmp_path, monkeypatch
):
    """Третья ветка: сменилось само ИМЯ активного профиля — файл, разумеется,
    другой, но это осознанное переключение, и о нём говорят свои каналы
    (`project_profile_activated`, `profile_switch_deferred`). Notice «профиль
    теперь берётся из другого файла» здесь был бы не предупреждением о подмене,
    а пересказом того, что пользователь только что сделал сам.

    Ветка гарда `old_name != new_name` до этого теста не стереглась ничем: её
    снятие оставляло зелёными все существующие тесты.
    """
    clear_runtime_notices()
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.chdir(project_dir)

    base = tmp_path / "config.yml"
    _write_yaml(base, {**_MINIMAL_BASE, "profile": "work"})
    for name, url in (
        ("work", "http://work/v1/chat/completions"),
        ("home", "http://home/v1/chat/completions"),
    ):
        _write_yaml(
            tmp_path / "profiles" / f"{name}.yml",
            {
                "models": {"flash": {"url": url, "token": ""}},
                "slots": {"default": "flash"},
            },
        )

    cfg = load_settings(str(base))
    app = TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", settings=cfg
    )
    app.set_config_path(str(base))
    assert app.active_profile_ref().name == "work"

    # указатель переписан на другой профиль — оба файла существуют, менялось имя
    _write_yaml(base, {**_MINIMAL_BASE, "profile": "home"})
    _bump_mtime(base)
    app.reload_config_if_changed()

    assert app.active_profile_ref().name == "home"
    codes = [n.code for n in collect_runtime_notices()]
    assert "profile_source_changed" not in codes, (
        f"смена имени профиля — не смена источника; коды: {codes}"
    )


def test_sanitize_does_not_apply_the_probed_profile_to_the_process(tmp_path):
    """Санация СПРАШИВАЕТ, собирается ли сессионный профиль, и ответ выбрасывает
    (`load_settings` без присваивания) — это проба, а не применение.

    Профиль применяет `_profile_remerge_settings` — он и обязан нести побочки;
    проба, идущая другим поводом (attach треда, перечитка), не должна менять
    `effective_settings`."""
    app = _app_with_base(tmp_path)
    _write_yaml(
        tmp_path / "profiles" / "work.yml",
        {"strip_tags_projection": {"tags": ["scratchpad"]}},
    )
    app.set_session_override("profile", "work")
    before = (
        app.effective_settings().settings.strip_tags_projection.model_dump()
    )

    app.sanitize_session_profile()

    assert (
        "profile" in app._session_overrides
    )  # профиль здоров — санация не сняла
    after = (
        app.effective_settings().settings.strip_tags_projection.model_dump()
    )
    assert after == before


# --- 10. Гейт сессионного выбора: несобираемый профиль отклоняется на месте --------


def _write_unreadable_profile(
    tmp_path: Path, name: str = "broken-enc"
) -> Path:
    """Профиль в UTF-16: файл на месте, в списке пикера есть, а прочитать нельзя
    (живой смок владельца)."""
    path = tmp_path / "profiles" / f"{name}.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "models": {"gc": {"url": "http://y", "token": ""}},
                "slots": {"default": "gc"},
            }
        ),
        encoding="utf-16",
    )
    return path


def _meta_overrides(tmp_path: Path, task_id: str) -> dict:
    from gc_tui.session_settings import read_from_meta

    store = FileTaskStore(threads_dir=str(tmp_path / "threads"), system="cli")
    return read_from_meta(store.get_task_meta(task_id))


def test_switch_session_profile_refuses_a_profile_that_does_not_assemble(
    tmp_path,
):
    """Выбор профиля, с которым конфиг не собирается, обязан быть отклонён
    вызывающему исключением: тихий фоллбэк `_profile_remerge` оставлял имя в
    футере и в мете треда, а работала при этом база — витрина называла профиль,
    которого нет ни в одном запросе."""
    path = _write_unreadable_profile(tmp_path)
    app = _app_with_base(tmp_path)
    app.tui_state.active_task_id = "t-gate"

    with pytest.raises(ValueError) as excinfo:
        app.switch_session_profile("broken-enc")

    assert str(path) in str(excinfo.value)
    assert "profile" not in app._session_overrides
    assert "profile" not in _meta_overrides(tmp_path, "t-gate")


def test_switch_session_profile_refuses_invalid_yaml_profile(tmp_path):
    """Тот же класс, другой способ сломать файл: неразбираемый YAML."""
    _write_yaml(tmp_path / "profiles" / "ok.yml", {"models": {}})
    (tmp_path / "profiles" / "bad.yml").write_text(
        "models: [broken", encoding="utf-8"
    )
    app = _app_with_base(tmp_path)
    app.tui_state.active_task_id = "t-yaml"

    with pytest.raises(ValueError):
        app.switch_session_profile("bad")

    assert "profile" not in app._session_overrides
    assert "profile" not in _meta_overrides(tmp_path, "t-yaml")


def test_switch_session_profile_refuses_profile_with_dangling_slot(tmp_path):
    """Файл читается и разбирается, а конфиг всё равно не собирается: `slots.default`
    указывает на алиас, которого нет в merged-словаре models. Гейт судит по сборке,
    а не по читаемости файла."""
    _write_yaml(
        tmp_path / "profiles" / "dangling.yml",
        {"slots": {"default": "nosuch"}},
    )
    app = _app_with_base(tmp_path)
    app.tui_state.active_task_id = "t-slot"

    with pytest.raises(ValueError):
        app.switch_session_profile("dangling")

    assert "profile" not in app._session_overrides
    assert "profile" not in _meta_overrides(tmp_path, "t-slot")


def test_switch_session_profile_still_applies_a_healthy_profile(tmp_path):
    """Законный случай рядом с битым соседом: гейт не мешает здоровому выбору."""
    _write_work_profile(tmp_path)
    _write_unreadable_profile(tmp_path)
    app = _app_with_base(tmp_path)

    app.switch_session_profile("work")

    assert app._session_overrides["profile"] == "work"
    assert "flash" in app.effective_settings().settings.models


def test_session_profile_removal_works_even_when_current_choice_is_broken(
    tmp_path,
):
    """Снятие профиля — дверь наружу: она обязана открываться и тогда, когда в
    сессии лежит негодное имя (его положила прежняя версия, до гейта). Судить
    снятие по сборке снимаемого профиля значило бы запереть человека внутри."""
    _write_unreadable_profile(tmp_path)
    app = _app_with_base(tmp_path)
    app.set_session_override("profile", "broken-enc")

    app.switch_session_profile("")

    assert app._session_overrides["profile"] == ""


def test_session_profile_removal_does_not_probe_at_all(tmp_path):
    """Та же дверь, вторая петля: снятие не спрашивает диск вовсе. Сломаться под
    ногами может и сама база — и именно тогда снятие нужнее всего; проба на
    снятии читала бы битый `config.yml` и запирала выход."""
    _write_unreadable_profile(tmp_path)
    app = _app_with_base(tmp_path)
    app.set_session_override("profile", "broken-enc")
    (tmp_path / "config.yml").write_text("backend: [broken", encoding="utf-8")

    app.switch_session_profile("")

    assert app._session_overrides["profile"] == ""


def test_switch_session_profile_gate_probes_without_applying_to_process(
    tmp_path,
):
    """Проба гейта не применяет профиль к процессу — иначе сорвавшийся персист
    (каталог тредов недоступен) оставлял бы процесс с чужим
    `strip_tags_projection`, которого нет ни в сессии, ни в мете."""
    _write_yaml(
        tmp_path / "profiles" / "work.yml",
        {"strip_tags_projection": {"tags": ["scratchpad"]}},
    )
    app = _app_with_base(tmp_path)

    def _no_disk() -> None:
        raise OSError("read-only")

    app.persist_session_overrides = _no_disk
    before = (
        app.effective_settings().settings.strip_tags_projection.model_dump()
    )

    with pytest.raises(OSError):
        app.switch_session_profile("work")

    assert "profile" not in app._session_overrides
    after = (
        app.effective_settings().settings.strip_tags_projection.model_dump()
    )
    assert after == before
    assert after.get("tags") != ["scratchpad"]


def test_profile_broken_after_the_choice_still_degrades_to_the_base(tmp_path):
    """Профиль сломали ПОСЛЕ выбора — отклонять уже нечего, выбор состоялся.
    Существующее поведение `_profile_remerge_settings` (notice + прежняя база) гейт не
    трогает: он судит МОМЕНТ выбора, а не каждый кадр рендера."""
    clear_runtime_notices()
    _write_work_profile(tmp_path)
    app = _app_with_base(tmp_path)
    app.switch_session_profile("work")

    (tmp_path / "profiles" / "work.yml").write_text(
        "models: [broken", encoding="utf-8"
    )
    app._session_profile_cache = None

    assert app._profile_remerge_settings("work") is app.base_settings()
    codes = [n.code for n in collect_runtime_notices()]
    assert "session_profile_remerge_failed" in codes


def test_sanitize_drops_an_unreadable_session_profile_naming_the_reason(
    tmp_path,
):
    """Санация снимает не только ПРОПАВШИЙ профиль, но и нечитаемый — и говорит,
    почему: «не найден» посылало искать файл, который лежит на месте."""
    clear_runtime_notices()
    path = _write_unreadable_profile(tmp_path)
    app = _app_with_base(tmp_path)
    app.set_session_override("profile", "broken-enc")

    app.sanitize_session_profile()

    assert "profile" not in app._session_overrides
    msg = next(
        n.message
        for n in collect_runtime_notices()
        if n.code == "session_profile_stale"
    )
    assert "broken-enc" in msg and str(path) in msg


@pytest.mark.asyncio
async def test_attach_sanitizes_an_unreadable_session_profile(tmp_path):
    """Тред, в мету которого битый выбор положила прежняя версия: на attach он
    обязан сняться, иначе восстанавливался бы на каждом открытии треда — и
    каждый раз безуспешно."""
    clear_runtime_notices()
    base = tmp_path / "config.yml"
    _write_yaml(base, _MINIMAL_BASE)
    _write_unreadable_profile(tmp_path)
    cfg = load_settings(str(base))
    store = FileTaskStore(threads_dir=str(tmp_path / "threads"), system="cli")
    store.update_tui_meta(
        "t-broken", {"session_settings": {"profile": "broken-enc"}}
    )
    app = TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", settings=cfg
    )
    app.set_config_path(str(base))
    async with app.run_test() as pilot:
        pilot.app.screen.attach_active_task("t-broken")
        await pilot.pause(0.1)
        assert "profile" not in app._session_overrides
        codes = [n.code for n in collect_runtime_notices()]
        assert "session_profile_stale" in codes
