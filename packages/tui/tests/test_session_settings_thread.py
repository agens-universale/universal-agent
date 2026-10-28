from gc_testkit.develop_files import meta_of
from gc_testkit.tui_support import tui_settings
import pytest

pytest.importorskip("textual")


def _mk_thread(tmp_path, tid, overrides):
    """Реальный тред с сохранёнными сессионными настройками."""
    from gc_tui.services.stores.file_store import FileTaskStore

    store = FileTaskStore(threads_dir=str(tmp_path / "threads"), system="cli")
    store.update_tui_meta(tid, {"session_settings": overrides})


class _StubRunner:
    """Минимальный стаб раннера — прогнать ветку автосоздания /init без
    реального агента."""

    def is_running(self) -> bool:
        return False

    def start(self, *, query, thread_id, system=None, message_id=None, **kw):
        return True


@pytest.mark.asyncio
async def test_opening_thread_applies_its_settings(tmp_path):
    """Возврат к треду восстанавливает его значения."""
    from gc_tui.app import TuiApp

    _mk_thread(tmp_path, "t-own", {"tui.finish_gate_mode": "off"})
    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "tui.finish_gate_mode": "on"}
        ),
    )
    async with app.run_test() as pilot:
        pilot.app.screen.attach_active_task("t-own")
        await pilot.pause(0.1)
        assert app.effective_settings().settings.tui.finish_gate_mode == "off"


@pytest.mark.asyncio
async def test_opening_thread_without_settings_falls_back_to_default(tmp_path):
    """Тред без своих значений (старый) — умолчание из конфига, прежние
    переопределения окна снимаются."""
    from gc_tui.app import TuiApp

    _mk_thread(tmp_path, "t-plain", {})
    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "tui.finish_gate_mode": "on"}
        ),
    )
    async with app.run_test() as pilot:
        app.set_session_override("tui.finish_gate_mode", "off")
        pilot.app.screen.attach_active_task("t-plain")
        await pilot.pause(0.1)
        assert app.effective_settings().settings.tui.finish_gate_mode == "on"


@pytest.mark.asyncio
async def test_init_auto_create_inherits_session_overrides(tmp_path):
    """/init на пустой сессии создаёт тред через единую точку автосоздания:
    новый тред обязан унести ТЕКУЩИЕ сессионные переопределения, а не пустые.
    Иначе attach_active_task (вызывается следом) читает пустые meta и сбрасывает
    живые настройки сессии к умолчанию конфига — регрессия, которую чинит
    _begin_auto_created_task для всех входов (текст, inline /skill, /init)."""
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "tui.finish_gate_mode": "on"}
        ),
    )
    rec = _StubRunner()
    app.active_runner = rec
    app.get_runner = lambda: rec
    async with app.run_test() as pilot:
        app.set_session_override("tui.finish_gate_mode", "off")
        pilot.app.screen._cmd_init()
        await pilot.pause(0.1)
        tid = app.tui_state.active_task_id
        assert tid is not None

        from gc_tui.services.stores.file_store import FileTaskStore

        store = FileTaskStore(
            threads_dir=str(tmp_path / "threads"), system="cli"
        )
        saved = (meta_of(store.get_record(tid)).get("tui") or {}).get(
            "session_settings"
        )
        assert saved == {"tui.finish_gate_mode": "off"}
        # attach_active_task отработал следом и не должен был затереть значение.
        assert app.effective_settings().settings.tui.finish_gate_mode == "off"


@pytest.mark.asyncio
async def test_freetext_auto_create_inherits_session_overrides(tmp_path):
    """Свободный текст (основной путь) на пустой сессии создаёт тред через
    единую точку автосоздания: новый тред обязан унести ТЕКУЩИЕ сессионные
    переопределения. Если убрать persist_session_overrides(), тест падает."""
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "tui.finish_gate_mode": "on"}
        ),
    )
    rec = _StubRunner()
    app.active_runner = rec
    app.get_runner = lambda: rec
    async with app.run_test() as pilot:
        app.set_session_override("tui.finish_gate_mode", "off")
        # Ввод свободного текста — вызывает on_command_text_area_submitted →
        # _begin_auto_created_task
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "привет"
        await pilot.press("enter")
        await pilot.pause(0.1)
        tid = app.tui_state.active_task_id
        assert tid is not None

        from gc_tui.services.stores.file_store import FileTaskStore

        store = FileTaskStore(
            threads_dir=str(tmp_path / "threads"), system="cli"
        )
        saved = (meta_of(store.get_record(tid)).get("tui") or {}).get(
            "session_settings"
        )
        assert saved == {"tui.finish_gate_mode": "off"}
        # Не должен был затереть значение
        assert app.effective_settings().settings.tui.finish_gate_mode == "off"


@pytest.mark.asyncio
async def test_attach_without_settings_change_keeps_warm_runner(tmp_path):
    """Прогретый executor не выбрасывается зря: у треда те же значения, что уже
    применены в окне, — пересобирать нечего. Пересборка тянет MCP-дискавери внутри
    worker-потока, и пользователь ждёт её до начала хода (переключение через
    /sessions, второе сообщение автосозданного треда)."""
    from gc_tui.app import TuiApp

    _mk_thread(tmp_path, "t-same", {"tui.finish_gate_mode": "off"})
    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "tui.finish_gate_mode": "on"}
        ),
    )
    async with app.run_test() as pilot:
        app.set_session_override("tui.finish_gate_mode", "off")
        calls = []
        app.invalidate_runner = lambda: calls.append(1)
        pilot.app.screen.attach_active_task("t-same")
        await pilot.pause(0.1)
        assert calls == []


@pytest.mark.asyncio
async def test_attach_with_other_settings_invalidates_runner(tmp_path):
    """Обратная сторона: значения треда РЕАЛЬНО другие — раннер обязан пересобраться,
    иначе ход пойдёт на конфиге прошлого треда."""
    from gc_tui.app import TuiApp

    _mk_thread(tmp_path, "t-other", {"tui.finish_gate_mode": "off"})
    app = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "tui.finish_gate_mode": "on"}
        ),
    )
    async with app.run_test() as pilot:
        calls = []
        app.invalidate_runner = lambda: calls.append(1)
        pilot.app.screen.attach_active_task("t-other")
        await pilot.pause(0.1)
        assert calls == [1]


@pytest.mark.asyncio
async def test_two_windows_with_different_threads_are_independent(tmp_path):
    """Два приложения на одном каталоге тредов с разными тредами не влияют
    друг на друга — исходная проблема, ради которой всё делалось (спека
    2026-07-20-session-scoped-settings §0: «два окна с разными тредами честно
    показывают разное»). Свойство — на диске (настройки треда персистятся
    отдельно от инсталляции), не про живой параллелизм: два TuiApp.run_test()
    не держим открытыми одновременно — Screen.app у Textual резолвится через
    contextvar активного app, и у вложенных run_test() он один на двоих
    (эмпирически проверено — второй run_test() перехватывает self.app у
    первого); нигде в tests/tui/ так не делают, см. test_session_lock_tui.py,
    где «другое окно» эмулируется на уровне lock-файла, а не вторым живым
    инстансом."""
    from gc_tui.app import TuiApp

    _mk_thread(tmp_path, "t-a", {"tui.finish_gate_mode": "off"})
    _mk_thread(tmp_path, "t-b", {"tui.finish_gate_mode": "on"})
    cfg = {"agent.model": "M", "tui.finish_gate_mode": "on"}

    app_a = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings(dict(cfg)),
    )
    app_b = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings(dict(cfg)),
    )

    async with app_a.run_test() as pa:
        pa.app.screen.attach_active_task("t-a")
        await pa.pause(0.1)
        assert (
            app_a.effective_settings().settings.tui.finish_gate_mode == "off"
        )

    async with app_b.run_test() as pb:
        pb.app.screen.attach_active_task("t-b")
        await pb.pause(0.1)
        assert app_b.effective_settings().settings.tui.finish_gate_mode == "on"


@pytest.mark.asyncio
async def test_settings_screen_open_picks_up_default_saved_by_other_window(
    tmp_path,
):
    """Окно B видит умолчание, сохранённое окном A (дефект, подтверждённый живым
    прогоном в двух терминалах): TuiApp._config читается один раз при старте
    и раньше обновлялся только у того окна, которое само сохранило умолчание.
    Открытие /settings обязано подхватить чужую запись через freshness-проверку
    по mtime config.yml, а не показывать/наследовать конфиг, с которым окно
    было создано."""
    from gc_tui.app import TuiApp
    from gc_tui.screens.settings import SettingsScreen

    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text('tui:\n  finish_gate_mode: "on"\n', encoding="utf-8")

    # Окно B создано ДО того, как окно A сохранило новое умолчание — его
    # in-memory база всё ещё "on". set_config_path (не голое присвоение
    # _config_path) засевает _config_mtime снимком ДО правки окна A — иначе
    # гейт свежести сработал бы просто от отсутствия снимка (mtime is None),
    # а не от реального расхождения mtime, и тест не отличил бы честную
    # перечитку по mtime от «перечитывать всегда».
    app_b = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings({"tui.finish_gate_mode": "on"}),
    )
    app_b.set_config_path(str(cfg_file))

    # Окно A сохранило «Сохранить как умолчание»: файл на диске меняется.
    cfg_file.write_text('tui:\n  finish_gate_mode: "off"\n', encoding="utf-8")
    import os
    import time

    t = time.time() + 5.0
    os.utime(cfg_file, (t, t))

    async with app_b.run_test() as pb:
        pb.app.screen._cmd_settings()
        await pb.pause(0.1)
        assert isinstance(pb.app.screen, SettingsScreen)
        assert pb.app.screen._settings.settings.tui.finish_gate_mode == "off"
        assert (
            app_b.effective_settings().settings.tui.finish_gate_mode == "off"
        )


@pytest.mark.asyncio
async def test_entering_settings_section_picks_up_other_window_default(
    tmp_path,
):
    """Вход в секцию /settings при уже открытом экране подхватывает чужую правку
    глобала (дизайн 2026-07-21 §4: свежесть в двух точках — открытие Settings и
    вход в секцию). _sync_config_from_app зовёт reload_config_if_changed явно."""
    from gc_testkit.tui_support import open_settings_section
    from gc_tui.app import TuiApp
    from gc_tui.screens.settings import SettingsScreen

    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text('tui:\n  finish_gate_mode: "on"\n', encoding="utf-8")
    app_b = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings({"tui.finish_gate_mode": "on"}),
    )
    app_b.set_config_path(str(cfg_file))

    async with app_b.run_test() as pb:
        scr = SettingsScreen(
            settings=app_b.base_settings(),
            config_path=str(cfg_file),
            on_apply=app_b.reload_config,
        )
        pb.app.push_screen(scr)
        await pb.pause(0.1)

        cfg_file.write_text(
            'tui:\n  finish_gate_mode: "off"\n', encoding="utf-8"
        )
        import os
        import time

        t = time.time() + 5.0
        os.utime(cfg_file, (t, t))

        open_settings_section(
            scr, "Режимы работы"
        )  # → _open_section → _sync_config_from_app
        await pb.pause(0.1)
        assert (
            app_b.effective_settings().settings.tui.finish_gate_mode == "off"
        )


@pytest.mark.asyncio
async def test_config_screen_open_does_not_reload_other_window_default(
    tmp_path,
):
    """/config больше НЕ подхватывает чужую правку глобала (дизайн 2026-07-21 §4):
    автоперечитку сняли со всех экранов, кроме Settings. Окно показывает базу, с
    которой живёт; свежесть — только на пути /settings (тесты ниже)."""
    from gc_tui.app import TuiApp
    from gc_tui.screens.config import ConfigScreen

    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text('tui:\n  finish_gate_mode: "on"\n', encoding="utf-8")
    app_b = TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=tui_settings({"tui.finish_gate_mode": "on"}),
    )
    app_b.set_config_path(str(cfg_file))

    cfg_file.write_text('tui:\n  finish_gate_mode: "off"\n', encoding="utf-8")
    import os
    import time

    t = time.time() + 5.0
    os.utime(cfg_file, (t, t))

    async with app_b.run_test() as pb:
        pb.app.screen._cmd_config()
        await pb.pause(0.1)
        assert isinstance(pb.app.screen, ConfigScreen)
        assert (
            pb.app.screen._settings.settings.tui.finish_gate_mode == "on"
        )  # не перечитано
