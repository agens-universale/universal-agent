"""TUI-клей: Ctrl+T → запись (бордер+пилюля), Enter → стоп+вставка, Esc → отмена."""

from gc_testkit.tui_support import tui_settings
from footer_text import footer_rows
import pytest


class FakeRecorder:
    def __init__(self):
        self.elapsed = 0.0
        self.cancelled = False

    def start(self):
        pass

    @property
    def elapsed_seconds(self):
        return self.elapsed

    def stop(self):
        return b"pcm"

    def cancel(self):
        self.cancelled = True


VOICE_CFG = {
    "provider": "openai",
    "url": "https://stt.local/v1/audio/transcriptions",
    "token": "t",
    "model": "whisper-1",
    "language": "ru",
    "max_seconds": 120,
    "timeout_seconds": 60,
    "verify_ssl": True,
}


def _wire_fake_voice(screen, rec, transcribe):
    """Подменить flow фейками через тестовый шов (_ensure_voice_flow вернёт его)."""
    from gc_tui.services.voice_input import VoiceInputFlow

    screen._voice = VoiceInputFlow(
        VOICE_CFG,
        recorder_factory=lambda: rec,
        transcribe=transcribe,
        ui_call=lambda fn, *a: fn(*a),
        on_state=screen._on_voice_state,
        on_text=screen._insert_transcribed,
        on_error=screen._on_voice_error,
        spawn=lambda fn: fn(),
    )


@pytest.mark.asyncio
async def test_ctrl_t_records_enter_inserts(monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.command_input import CommandTextArea
    from textual.containers import Horizontal

    monkeypatch.setattr("gc_tui.voice.recorder.available", lambda: True)
    app = TuiApp(
        settings=tui_settings({"agent.model": "M", "voice": dict(VOICE_CFG)})
    )
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        rec = FakeRecorder()
        _wire_fake_voice(screen, rec, lambda pcm, cfg: "привет мир")

        await pilot.press("ctrl+t")
        await pilot.pause()
        assert screen._voice.state == "recording"
        assert pilot.app.query_one("#input-row", Horizontal).has_class(
            "recording"
        )
        mode, slot = footer_rows(pilot.app)
        assert "стоп" in mode  # dim-хвост подсказки при записи
        assert slot == "▸▸"  # режим виден глифом во второй строке

        rec.elapsed = 2.0
        await pilot.press("enter")
        await pilot.pause()
        assert screen._voice.state == "idle"
        inp = pilot.app.query_one("#cmdinput", CommandTextArea)
        assert "привет мир" in inp.text  # вставлено, НЕ отправлено
        assert not pilot.app.query_one("#input-row", Horizontal).has_class(
            "recording"
        )


def _voice_app():
    from gc_tui.app import TuiApp

    return TuiApp(
        settings=tui_settings({"agent.model": "M", "voice": dict(VOICE_CFG)})
    )


@pytest.mark.asyncio
async def test_voice_pill_replaces_mode_row_and_glyph_leads_the_slot(
    monkeypatch,
):
    """Запись: пилюля голоса в строке 1 вместо режима; в строке 2 первым —
    глиф режима его цветом, за ним бейдж. Shift+Tab меняет режим сразу
    (глиф следует), пояснение режима — только после записи."""
    pytest.importorskip("textual")
    from gc_tui.palette import DEEP_DIM

    monkeypatch.setattr("gc_tui.voice.recorder.available", lambda: True)
    async with _voice_app().run_test(size=(100, 24)) as pilot:
        pilot.app._update_available = True
        screen = pilot.app.screen
        _wire_fake_voice(screen, FakeRecorder(), lambda pcm, cfg: "x")
        await pilot.press("ctrl+t")
        await pilot.pause()
        mode, slot = footer_rows(pilot.app)
        assert "REC" in mode and "mode" not in mode
        assert slot == "▸▸  ℹ 1 · F4/notices"

        await pilot.press("shift+tab")
        await pilot.pause()
        assert pilot.app.effective_permission_mode() == "manual"
        mode, slot = footer_rows(pilot.app)
        assert "REC" in mode and "manual mode" not in mode + slot
        assert slot.startswith("⏸  ")
        glyph = screen._footer_slot_line(screen._footer_frame().badge).spans[0]
        assert (glyph.end, glyph.style) == (1, DEEP_DIM)  # цвет режима

        await pilot.press("escape")  # запись кончилась
        await pilot.pause()
        mode, slot = footer_rows(pilot.app)
        assert mode.startswith("⏸ manual mode — ")


@pytest.mark.asyncio
async def test_voice_during_ask_human_returns_answer_tail(monkeypatch):
    """Голос во время вопроса агента: после записи строка 1 снова держит
    хвост «Esc/stop  Enter/ответить»."""
    pytest.importorskip("textual")
    from gc_tui.state import AskHumanState

    monkeypatch.setattr("gc_tui.voice.recorder.available", lambda: True)
    async with _voice_app().run_test(size=(100, 24)) as pilot:
        screen = pilot.app.screen
        pilot.app.tui_state.ask_human_state = AskHumanState(
            call_id="c1", question="Q?"
        )
        rec = FakeRecorder()
        _wire_fake_voice(screen, rec, lambda pcm, cfg: "ответ")
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert "Esc/stop" not in footer_rows(pilot.app)[0]
        rec.elapsed = 2.0
        await pilot.press("enter")
        await pilot.pause()
        assert screen._voice.state == "idle"
        assert footer_rows(pilot.app)[0].endswith("Esc/stop  Enter/ответить")


@pytest.mark.asyncio
async def test_escape_cancels_without_insert(monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.command_input import CommandTextArea

    monkeypatch.setattr("gc_tui.voice.recorder.available", lambda: True)
    app = TuiApp(
        settings=tui_settings({"agent.model": "M", "voice": dict(VOICE_CFG)})
    )
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        rec = FakeRecorder()
        _wire_fake_voice(screen, rec, lambda pcm, cfg: "не должно вставиться")

        await pilot.press("ctrl+t")
        rec.elapsed = 2.0
        await pilot.press("escape")
        await pilot.pause()
        assert screen._voice.state == "idle"
        assert rec.cancelled
        assert pilot.app.query_one("#cmdinput", CommandTextArea).text == ""


@pytest.mark.asyncio
async def test_ctrl_t_without_config_shows_hint_toast():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        settings=tui_settings({"agent.model": "M"})
    )  # секции voice нет
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        calls = []
        screen.notify = lambda msg, **kw: calls.append(msg)  # перехват toast
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert screen._voice is None  # flow даже не создавался
        assert any("/settings → Голосовой ввод" in msg for msg in calls)


@pytest.mark.asyncio
async def test_ensure_voice_flow_builds_real_flow(monkeypatch):
    """Прод-ветка _ensure_voice_flow: реальные импорты MicRecorder/transcribe,
    flow собирается один раз и переиспользуется (сеть/микрофон не трогаются)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    monkeypatch.setattr("gc_tui.voice.recorder.available", lambda: True)
    app = TuiApp(
        settings=tui_settings({"agent.model": "M", "voice": dict(VOICE_CFG)})
    )
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        flow = screen._ensure_voice_flow()
        assert flow is screen._voice
        assert flow.state == "idle"
        assert (
            screen._ensure_voice_flow() is flow
        )  # повторный вызов — тот же объект


@pytest.mark.asyncio
async def test_paste_chip_survives_enter_stop_of_recording(
    monkeypatch, tmp_path
):
    """Плашка переживает Enter-стоп записи: submission_text() после стопа
    разворачивает вставку; чипы сбрасываются только по реальной очистке буфера."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.command_input import CommandTextArea

    class _Fake:  # no-op executor: финальный Enter
        async def arun(
            self, messages, *, thread_id, **kw
        ):  # не строит реальный AgentExecutor
            pass

    monkeypatch.setattr("gc_tui.voice.recorder.available", lambda: True)
    app = TuiApp(
        settings=tui_settings({"agent.model": "M", "voice": dict(VOICE_CFG)}),
        threads_dir=str(tmp_path),
    )
    app._executor_factory = lambda: _Fake()
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        rec = FakeRecorder()
        _wire_fake_voice(screen, rec, lambda pcm, cfg: "голос")

        inp = pilot.app.query_one("#cmdinput", CommandTextArea)
        inp._handle_paste("l1\nl2\nl3\nl4\nl5\nl6")  # свернётся в плашку
        await pilot.pause()
        assert "[Вставка" in inp.text

        await pilot.press("ctrl+t")  # запись
        rec.elapsed = 2.0
        await pilot.press("enter")  # стоп+распознать (НЕ отправка)
        await pilot.pause()
        assert screen._voice.state == "idle"
        assert "голос" in inp.text  # вставлено, поле не очищено
        sub = inp.submission_text()
        assert "l1\nl2" in sub  # плашка развернулась содержимым
        assert "[Вставка" not in sub  # не литеральный ярлык

        await pilot.press("enter")  # теперь настоящая отправка
        await pilot.pause()
        assert inp.text == ""  # буфер очищен → чипы сброшены
        inp._handle_paste(
            "l1\nl2\nl3\nl4\nl5\nl6"
        )  # повторная вставка сворачивается
        await pilot.pause()  # заново с #1 (стор сброшен)
        assert inp.text == "[Вставка #1 · 6 строк]"


@pytest.mark.asyncio
async def test_footer_right_width_accounts_for_voice_pill(monkeypatch):
    """Во время записи левая колонка футера — пилюля, и лесенка правой обязана мерить
    ИМЕННО ЕЁ, а не строку команд, которой в этот момент на экране нет.

    Модель ширины сменилась вместе с профилями (develop, Task 17): правая колонка
    больше не «остаток от левой» — она заявляет свой потолок, а сколько останется
    левой, решает перебор с предикатом «встаёт ли строка 1». Предикат берёт
    строку у `_footer_mode_variants()`, поэтому проверяем именно её: там должна
    быть пилюля целиком, с таймером и лентой эквалайзера, иначе правая колонка
    посчитает место по командам и обрежет пилюлю."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    monkeypatch.setattr("gc_tui.voice.recorder.available", lambda: True)
    app = TuiApp(
        settings=tui_settings({"agent.model": "M", "voice": dict(VOICE_CFG)})
    )
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        rec = FakeRecorder()
        _wire_fake_voice(screen, rec, lambda pcm, cfg: "x")
        idle_commands = screen._footer_mode_variants()[1].plain
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert screen._voice.state == "recording"
        pill = screen._voice_footer_render().plain
        assert pill.strip()
        measured = [line.plain for line in screen._footer_mode_variants()]
        assert measured == [pill, pill]  # мерим пилюлю, а не команды
        assert pill != idle_commands


@pytest.mark.asyncio
async def test_insert_adds_space_after_word(monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.command_input import CommandTextArea

    monkeypatch.setattr("gc_tui.voice.recorder.available", lambda: True)
    app = TuiApp(
        settings=tui_settings({"agent.model": "M", "voice": dict(VOICE_CFG)})
    )
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        inp = pilot.app.query_one("#cmdinput", CommandTextArea)
        inp.text = "почини"
        inp.move_cursor((0, len("почини")))
        screen._insert_transcribed("тест в модуле")
        assert inp.text == "почини тест в модуле"


@pytest.mark.asyncio
async def test_slash_voice_starts_recording(monkeypatch):
    """Команда /voice = алиас Ctrl+T: стартует запись через те же гарды."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    monkeypatch.setattr("gc_tui.voice.recorder.available", lambda: True)
    app = TuiApp(
        settings=tui_settings({"agent.model": "M", "voice": dict(VOICE_CFG)})
    )
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        rec = FakeRecorder()
        _wire_fake_voice(screen, rec, lambda pcm, cfg: "x")
        inp = screen.query_one("#cmdinput")
        inp.insert("/voice")
        await pilot.press("enter")
        await pilot.pause()
        assert screen._voice.state == "recording"


@pytest.mark.asyncio
async def test_slash_voice_unconfigured_toast(monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(settings=tui_settings({"agent.model": "M", "voice": {}}))
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        toasts = []
        monkeypatch.setattr(
            screen,
            "notify",
            lambda msg, **kw: toasts.append(msg),
            raising=False,
        )
        inp = screen.query_one("#cmdinput")
        inp.insert("/voice")
        await pilot.press("enter")
        await pilot.pause()
        assert any("/settings → Голосовой ввод" in m for m in toasts)
        assert screen._voice is None or screen._voice.state == "idle"


@pytest.mark.asyncio
async def test_voice_flow_cache_dropped_on_config_hot_reload(
    monkeypatch, tmp_path
):
    """Смена endpoint в /settings доезжает до живого Ctrl+T без перезапуска: flow
    получает voice-секцию КОПИЕЙ в конструктор и кэшируется, поэтому hot-reload
    конфига обязан сбросить кэш — иначе диктовка продолжает стучаться на старый
    адрес до конца сессии."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    monkeypatch.setattr("gc_tui.voice.recorder.available", lambda: True)
    # Оверлеи машины (managed ~/.gc/config.local.yml с wire'нутым локальным сервисом,
    # project-local <cwd>/.gc/config.yml) не должны перекрывать конфиг теста — тот же
    # приём, что в tests/test_voice_config.py.
    monkeypatch.setattr(
        "gc_core.config_modules.env_vars.managed_config_path",
        lambda: tmp_path / "no-managed.yml",
    )
    monkeypatch.setattr(
        "gc_core.config_modules.env_vars.project_local_config_path",
        lambda: None,
    )
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        "voice:\n  url: https://new.x/v1/audio/transcriptions\n  token: t2\n",
        encoding="utf-8",
    )
    app = TuiApp(
        settings=tui_settings({"agent.model": "M", "voice": dict(VOICE_CFG)})
    )
    app._config_path = str(cfg_file)
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        old = screen._ensure_voice_flow()
        assert old._cfg["url"] == VOICE_CFG["url"]
        app.reload_config()  # тот же путь, что у /settings → _on_apply
        await pilot.pause()
        new = screen._ensure_voice_flow()
        assert new is not old
        assert new._cfg["url"] == "https://new.x/v1/audio/transcriptions"


@pytest.mark.asyncio
async def test_voice_flow_cache_kept_while_recording(monkeypatch, tmp_path):
    """…но живую запись hot-reload не рвёт: сброс кэша под работающим рекордером
    осиротил бы поток транскрибации (flow ушёл, микрофон остался)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    monkeypatch.setattr("gc_tui.voice.recorder.available", lambda: True)
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        "voice:\n  url: https://new.x/v1/audio/transcriptions\n",
        encoding="utf-8",
    )
    app = TuiApp(
        settings=tui_settings({"agent.model": "M", "voice": dict(VOICE_CFG)})
    )
    app._config_path = str(cfg_file)
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        rec = FakeRecorder()
        _wire_fake_voice(screen, rec, lambda pcm, cfg: "x")
        flow = screen._voice
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert flow.state == "recording"
        app.reload_config()
        await pilot.pause()
        assert screen._voice is flow  # запись не осиротела


@pytest.mark.asyncio
async def test_ctrl_t_raises_a_dead_local_service_instead_of_recording(
    monkeypatch,
):
    """Батарейка умерла после старта TUI — Ctrl+T поднимает её, а не пишет в пустоту.

    Находка ревью коллеги: `ensure_voice_service` зовут только при старте агента и из
    экранов настройки, а в пути записи проверок не было вовсе — после смерти сервиса
    (OOM, `gc-voice stop`, ручной kill, гонка light-switch) диктовка уходила в мёртвый
    порт и терялась на распознавании, и выхода из этого состояния без перезапуска окна
    не существовало."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    monkeypatch.setattr("gc_tui.voice.recorder.available", lambda: True)
    monkeypatch.setattr(
        "gc_tui.services.voice_setup.probe_alive", lambda: False
    )
    raised = []
    monkeypatch.setattr(
        "gc_tui.voice_runtime.autostart.ensure_voice_service",
        lambda cfg: raised.append(cfg) or True,
    )
    app = TuiApp(
        settings=tui_settings({"agent.model": "M", "voice": dict(VOICE_CFG)})
    )
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        monkeypatch.setattr(screen, "_voice_local_mode", lambda: True)
        rec = FakeRecorder()
        _wire_fake_voice(screen, rec, lambda pcm, cfg: "текст")
        flow = screen._voice

        await pilot.press("ctrl+t")
        for _ in range(50):
            await pilot.pause()
            if raised:
                break
        assert raised, "мёртвый сервис не стали поднимать"
        assert flow.state == "idle", "запись пошла в мёртвый порт"


@pytest.mark.asyncio
async def test_ctrl_t_records_when_the_local_service_answers(monkeypatch):
    """Живая батарейка — проверка не мешает: запись начинается как обычно."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    monkeypatch.setattr("gc_tui.voice.recorder.available", lambda: True)
    monkeypatch.setattr(
        "gc_tui.services.voice_setup.probe_alive", lambda: True
    )
    monkeypatch.setattr(
        "gc_tui.voice_runtime.autostart.ensure_voice_service",
        lambda cfg: pytest.fail("живой сервис поднимать незачем"),
    )
    app = TuiApp(
        settings=tui_settings({"agent.model": "M", "voice": dict(VOICE_CFG)})
    )
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        monkeypatch.setattr(screen, "_voice_local_mode", lambda: True)
        rec = FakeRecorder()
        _wire_fake_voice(screen, rec, lambda pcm, cfg: "текст")
        flow = screen._voice

        await pilot.press("ctrl+t")
        for _ in range(50):
            await pilot.pause()
            if flow.state == "recording":
                break
        assert flow.state == "recording"


@pytest.mark.asyncio
async def test_cloud_endpoint_is_not_probed_before_recording(monkeypatch):
    """Свой endpoint проверять перед записью нечем и незачем: он не наш и в сеть за
    ним мы не ходим — Ctrl+T начинает запись сразу."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    monkeypatch.setattr("gc_tui.voice.recorder.available", lambda: True)
    monkeypatch.setattr(
        "gc_tui.services.voice_setup.probe_alive",
        lambda: pytest.fail("облачный режим порт не меряет"),
    )
    app = TuiApp(
        settings=tui_settings({"agent.model": "M", "voice": dict(VOICE_CFG)})
    )
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        monkeypatch.setattr(screen, "_voice_local_mode", lambda: False)
        rec = FakeRecorder()
        _wire_fake_voice(screen, rec, lambda pcm, cfg: "текст")
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert screen._voice.state == "recording"


@pytest.mark.asyncio
async def test_voice_flow_rebuilds_after_the_recording_that_survived_a_reload(
    monkeypatch, tmp_path
):
    """…а как только запись кончилась — кэш всё-таки сбрасывается: следующая диктовка
    идёт на НОВЫЙ endpoint.

    Находка ревью коллеги: `config_reloaded` при живой записи просто выходил и следа
    не оставлял, поэтому все диктовки до перезапуска TUI продолжали стучаться на старый
    адрес — хотя докстринг обещал пересборку «на следующем Ctrl+T»."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    monkeypatch.setattr("gc_tui.voice.recorder.available", lambda: True)
    monkeypatch.setattr(
        "gc_core.config_modules.env_vars.managed_config_path",
        lambda: tmp_path / "no-managed.yml",
    )
    monkeypatch.setattr(
        "gc_core.config_modules.env_vars.project_local_config_path",
        lambda: None,
    )
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        "voice:\n  url: https://new.x/v1/audio/transcriptions\n  token: t2\n",
        encoding="utf-8",
    )
    app = TuiApp(
        settings=tui_settings({"agent.model": "M", "voice": dict(VOICE_CFG)})
    )
    app._config_path = str(cfg_file)
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        rec = FakeRecorder()
        _wire_fake_voice(screen, rec, lambda pcm, cfg: "текст")
        flow = screen._voice
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert flow.state == "recording"

        app.reload_config()  # смена endpoint посреди записи
        await pilot.pause()
        assert screen._voice is flow  # её не рвём

        rec.elapsed = 2.0
        await pilot.press("enter")  # стоп → распознавание → idle
        await pilot.pause()
        assert flow.state == "idle"

        new = screen._ensure_voice_flow()
        assert new is not flow, "устаревший flow пережил запись"
        assert new._cfg["url"] == "https://new.x/v1/audio/transcriptions"


@pytest.mark.asyncio
async def test_voice_config_follows_the_session_profile(tmp_path, monkeypatch):
    """Голос читает конфиг из ЕДИНОЙ точки — `effective_settings()`, а не из базы
    окна (`base_settings()`).

    Профиль, выбранный на сессию (`/profiles work`), — верхний слой: витрины и
    футер показывают его сразу. Голос же брал базу и с собственной секцией `voice`
    в профиле продолжал бы диктовать на прежний endpoint — тот же класс, что
    сигнал экрану при переключении профиля."""
    pytest.importorskip("textual")
    import yaml
    from gc_core.config import load_settings
    from gc_tui.app import TuiApp

    monkeypatch.setattr(
        "gc_core.config_modules.env_vars.managed_config_path",
        lambda: tmp_path / "no-managed.yml",
    )
    monkeypatch.setattr(
        "gc_core.config_modules.env_vars.project_local_config_path",
        lambda: None,
    )
    base = tmp_path / "config.yml"
    base.write_text(
        yaml.safe_dump(
            {
                "backend": {"completions_url": "http://h/v1/chat/completions"},
                "agent": {"model": "M"},
                "voice": dict(VOICE_CFG),
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "work.yml").write_text(
        "voice:\n  url: https://from-profile/v1/audio/transcriptions\n  token: pt\n",
        encoding="utf-8",
    )

    cfg = load_settings(str(base))
    app = TuiApp(
        threads_dir=str(tmp_path / "threads"), system="cli", settings=cfg
    )
    app.set_config_path(str(base))
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        assert screen._voice_config()["url"] == VOICE_CFG["url"]  # предусловие
        app.switch_session_profile("work")
        await pilot.pause()
        assert (
            app.effective_settings().settings.voice.url
            == "https://from-profile/v1/audio/transcriptions"
        ), "предусловие: профиль лёг"
        assert (
            screen._voice_config()["url"]
            == "https://from-profile/v1/audio/transcriptions"
        )
