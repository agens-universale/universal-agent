"""TUI-клей: Ctrl+T → запись (бордер+пилюля), Enter → стоп+вставка, Esc → отмена."""
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


VOICE_CFG = {"provider": "openai", "url": "https://stt.local/v1/audio/transcriptions",
             "token": "t", "model": "whisper-1", "language": "ru",
             "max_seconds": 120, "timeout_seconds": 60, "verify_ssl": True}


def _wire_fake_voice(screen, rec, transcribe):
    """Подменить flow фейками через тестовый шов (_ensure_voice_flow вернёт его)."""
    from tui.services.voice_input import VoiceInputFlow
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
    from textual.containers import Horizontal
    from textual.widgets import Static
    from tui.app import TuiApp
    from tui.widgets.command_input import CommandTextArea

    monkeypatch.setattr("voice.recorder.available", lambda: True)
    app = TuiApp(config={"model": "M", "voice": dict(VOICE_CFG)})
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        rec = FakeRecorder()
        _wire_fake_voice(screen, rec, lambda pcm, cfg: "привет мир")

        await pilot.press("ctrl+t")
        await pilot.pause()
        assert screen._voice.state == "recording"
        assert pilot.app.query_one("#input-row", Horizontal).has_class("recording")
        footer = str(pilot.app.query_one("#footer-left", Static).render())
        assert "стоп" in footer          # dim-хвост подсказки при записи

        rec.elapsed = 2.0
        await pilot.press("enter")
        await pilot.pause()
        assert screen._voice.state == "idle"
        inp = pilot.app.query_one("#cmdinput", CommandTextArea)
        assert "привет мир" in inp.text  # вставлено, НЕ отправлено
        assert not pilot.app.query_one("#input-row", Horizontal).has_class("recording")


@pytest.mark.asyncio
async def test_escape_cancels_without_insert(monkeypatch):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.command_input import CommandTextArea

    monkeypatch.setattr("voice.recorder.available", lambda: True)
    app = TuiApp(config={"model": "M", "voice": dict(VOICE_CFG)})
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
    from tui.app import TuiApp

    app = TuiApp(config={"model": "M"})   # секции voice нет
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        calls = []
        screen.notify = lambda msg, **kw: calls.append(msg)   # перехват toast
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert screen._voice is None      # flow даже не создавался
        assert any("/settings → Голосовой ввод" in msg for msg in calls)


@pytest.mark.asyncio
async def test_ensure_voice_flow_builds_real_flow(monkeypatch):
    """Прод-ветка _ensure_voice_flow: реальные импорты MicRecorder/transcribe,
    flow собирается один раз и переиспользуется (сеть/микрофон не трогаются)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    monkeypatch.setattr("voice.recorder.available", lambda: True)
    app = TuiApp(config={"model": "M", "voice": dict(VOICE_CFG)})
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        flow = screen._ensure_voice_flow()
        assert flow is screen._voice
        assert flow.state == "idle"
        assert screen._ensure_voice_flow() is flow   # повторный вызов — тот же объект


@pytest.mark.asyncio
async def test_paste_chip_survives_enter_stop_of_recording(monkeypatch, tmp_path):
    """Плашка переживает Enter-стоп записи: submission_text() после стопа
    разворачивает вставку; чипы сбрасываются только по реальной очистке буфера."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.command_input import CommandTextArea

    class _Fake:                                      # no-op executor: финальный Enter
        def run(self, messages, *, thread_id, **kw):  # не строит реальный AgentExecutor
            pass

    monkeypatch.setattr("voice.recorder.available", lambda: True)
    app = TuiApp(config={"model": "M", "voice": dict(VOICE_CFG)},
                 threads_dir=str(tmp_path))
    app._executor_factory = lambda: _Fake()
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        rec = FakeRecorder()
        _wire_fake_voice(screen, rec, lambda pcm, cfg: "голос")

        inp = pilot.app.query_one("#cmdinput", CommandTextArea)
        inp._handle_paste("l1\nl2\nl3\nl4\nl5\nl6")   # свернётся в плашку
        await pilot.pause()
        assert "[Вставка" in inp.text

        await pilot.press("ctrl+t")                   # запись
        rec.elapsed = 2.0
        await pilot.press("enter")                    # стоп+распознать (НЕ отправка)
        await pilot.pause()
        assert screen._voice.state == "idle"
        assert "голос" in inp.text                    # вставлено, поле не очищено
        sub = inp.submission_text()
        assert "l1\nl2" in sub                        # плашка развернулась содержимым
        assert "[Вставка" not in sub                  # не литеральный ярлык

        await pilot.press("enter")                    # теперь настоящая отправка
        await pilot.pause()
        assert inp.text == ""                         # буфер очищен → чипы сброшены
        inp._handle_paste("l1\nl2\nl3\nl4\nl5\nl6")   # повторная вставка сворачивается
        await pilot.pause()                           # заново с #1 (стор сброшен)
        assert inp.text == "[Вставка #1 · 6 строк]"


@pytest.mark.asyncio
async def test_footer_right_width_accounts_for_voice_pill(monkeypatch):
    """Во время записи footer-left = пилюля + хвост клавиш; доступная ширина
    footer-right обязана считаться от ФАКТИЧЕСКОГО рендера пилюли, а не от
    статичной строки команд — иначе правый край выдавливает подсказки.
    (Сравнения с idle-шириной нет: после F-клавишной полосы develop idle-футер
    может быть и шире, и уже пилюли — это не контракт.)"""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    monkeypatch.setattr("voice.recorder.available", lambda: True)
    app = TuiApp(config={"model": "M", "voice": dict(VOICE_CFG)})
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        rec = FakeRecorder()
        _wire_fake_voice(screen, rec, lambda pcm, cfg: "x")
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert screen._voice.state == "recording"
        left_cells = screen._voice_footer_render().cell_len
        assert left_cells > 0
        avail = screen._footer_right_available_width()
        # −5 = хром футера (2 + 1) плюс зазор между колонками 2 (padding-right у
        # #footer-left). Было −4 с зазором в один столбец, пока правая часть не
        # упиралась в левую вплотную.
        assert avail == max(10, pilot.app.size.width - left_cells - 5)


@pytest.mark.asyncio
async def test_insert_adds_space_after_word(monkeypatch):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.command_input import CommandTextArea

    monkeypatch.setattr("voice.recorder.available", lambda: True)
    app = TuiApp(config={"model": "M", "voice": dict(VOICE_CFG)})
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
    from tui.app import TuiApp

    monkeypatch.setattr("voice.recorder.available", lambda: True)
    app = TuiApp(config={"model": "M", "voice": dict(VOICE_CFG)})
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
    from tui.app import TuiApp

    app = TuiApp(config={"model": "M", "voice": {}})
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        toasts = []
        monkeypatch.setattr(screen, "notify",
                            lambda msg, **kw: toasts.append(msg), raising=False)
        inp = screen.query_one("#cmdinput")
        inp.insert("/voice")
        await pilot.press("enter")
        await pilot.pause()
        assert any("/settings → Голосовой ввод" in m for m in toasts)
        assert screen._voice is None or screen._voice.state == "idle"


@pytest.mark.asyncio
async def test_voice_flow_cache_dropped_on_config_hot_reload(monkeypatch, tmp_path):
    """Смена endpoint в /settings доезжает до живого Ctrl+T без перезапуска: flow
    получает voice-секцию КОПИЕЙ в конструктор и кэшируется, поэтому hot-reload
    конфига обязан сбросить кэш — иначе диктовка продолжает стучаться на старый
    адрес до конца сессии."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    monkeypatch.setattr("voice.recorder.available", lambda: True)
    # Оверлеи машины (managed ~/.gc/config.local.yml с wire'нутым локальным сервисом,
    # project-local <cwd>/.gc/config.yml) не должны перекрывать конфиг теста — тот же
    # приём, что в tests/test_voice_config.py.
    monkeypatch.setattr("config.managed_config_path", lambda: tmp_path / "no-managed.yml")
    monkeypatch.setattr("config._project_local_config_path", lambda: None)
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        "voice:\n  url: https://new.x/v1/audio/transcriptions\n  token: t2\n",
        encoding="utf-8")
    app = TuiApp(config={"model": "M", "voice": dict(VOICE_CFG)})
    app._config_path = str(cfg_file)
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        old = screen._ensure_voice_flow()
        assert old._cfg["url"] == VOICE_CFG["url"]
        app.reload_config()                      # тот же путь, что у /settings → _on_apply
        await pilot.pause()
        new = screen._ensure_voice_flow()
        assert new is not old
        assert new._cfg["url"] == "https://new.x/v1/audio/transcriptions"


@pytest.mark.asyncio
async def test_voice_flow_cache_kept_while_recording(monkeypatch, tmp_path):
    """…но живую запись hot-reload не рвёт: сброс кэша под работающим рекордером
    осиротил бы поток транскрибации (flow ушёл, микрофон остался)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    monkeypatch.setattr("voice.recorder.available", lambda: True)
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text("voice:\n  url: https://new.x/v1/audio/transcriptions\n",
                        encoding="utf-8")
    app = TuiApp(config={"model": "M", "voice": dict(VOICE_CFG)})
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
        assert screen._voice is flow                # запись не осиротела


@pytest.mark.asyncio
async def test_voice_flow_rebuilds_after_the_recording_that_survived_a_reload(
        monkeypatch, tmp_path):
    """…а как только запись кончилась — кэш всё-таки сбрасывается: следующая диктовка
    идёт на НОВЫЙ endpoint.

    Находка ревью коллеги: `config_reloaded` при живой записи просто выходил и следа
    не оставлял, поэтому все диктовки до перезапуска TUI продолжали стучаться на старый
    адрес — хотя докстринг обещал пересборку «на следующем Ctrl+T»."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    monkeypatch.setattr("voice.recorder.available", lambda: True)
    monkeypatch.setattr("config.managed_config_path", lambda: tmp_path / "no-managed.yml")
    monkeypatch.setattr("config._project_local_config_path", lambda: None)
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        "voice:\n  url: https://new.x/v1/audio/transcriptions\n  token: t2\n",
        encoding="utf-8")
    app = TuiApp(config={"model": "M", "voice": dict(VOICE_CFG)})
    app._config_path = str(cfg_file)
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        rec = FakeRecorder()
        _wire_fake_voice(screen, rec, lambda pcm, cfg: "текст")
        flow = screen._voice
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert flow.state == "recording"

        app.reload_config()                         # смена endpoint посреди записи
        await pilot.pause()
        assert screen._voice is flow                # её не рвём

        rec.elapsed = 2.0
        await pilot.press("enter")                  # стоп → распознавание → idle
        await pilot.pause()
        assert flow.state == "idle"

        new = screen._ensure_voice_flow()
        assert new is not flow, "устаревший flow пережил запись"
        assert new._cfg["url"] == "https://new.x/v1/audio/transcriptions"
