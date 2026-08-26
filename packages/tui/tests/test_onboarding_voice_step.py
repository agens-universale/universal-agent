"""Шаг мастера «Голосовой ввод»: навигация, выбор, поля endpoint, commit."""
import pytest

pytest.importorskip("textual")

from gc_tui.screens.onboarding import _VOICE_STEP


class FakeVoice:
    def __init__(self, installed=False):
        self.calls = []
        self.installed = installed
        self.models = None            # что отдаёт list_models (None = не достучались)
        self.model_calls = []         # (url, token, verify) каждой загрузки списка
        self.model_proxies = []       # прокси агента, дошедший до списка (канон /settings)

    def list_models(self, url, token, verify=True, config_proxy=None):
        self.model_calls.append((url, token, verify))
        self.model_proxies.append(config_proxy)
        return self.models

    def is_installed(self):
        return self.installed

    def switch_to_local(self, echo=print):
        self.calls.append("switch_to_local")
        return True


class FakeMcp:
    def installable_names(self):
        return []
    def descriptions(self):
        return {}
    def wired_names(self):
        return []
    def install(self, name):
        raise AssertionError("не должен зваться")


def _screen(app, voice):
    from gc_tui.screens.onboarding import OnboardingScreen
    return OnboardingScreen(
        config={}, config_path="X:/nope/config.yml", review_all=True,
        on_commit=lambda: None, mcp=FakeMcp(), voice=voice)


async def _to_voice_step(pilot, screen):
    # дать домонтироваться шагу 0 (EndpointEditor.focus_input идёт через
    # call_after_refresh — без паузы прыжок на шаг 4 гонится с ним за DOM)
    await pilot.pause()
    # адрес: невалидный ввод не пропустит — идём по шагам с пустыми submit'ами
    screen._values["completions_url"] = "http://h:1/v1/chat/completions"
    screen._values["Authorization"] = ""
    screen._values["model"] = "m"
    screen._step = _VOICE_STEP                     # шаг «Голосовой ввод»
    screen._render_step()
    await pilot.pause()


@pytest.mark.asyncio
async def test_voice_step_default_cursor_and_options(monkeypatch):
    from gc_tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    voice = FakeVoice()
    async with app.run_test() as pilot:
        screen = _screen(app, voice)
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        opts = [w.render() for w in screen.query(".onb-option")]
        texts = [str(t) for t in opts]
        assert any("Локальный сервис (рекомендуется)" in t for t in texts)
        assert any("Свой endpoint" in t for t in texts)
        assert any("Без голосового ввода" in t for t in texts)
        assert screen._idx.get(_VOICE_STEP, 0) == 0            # дефолт-курсор — локальный


@pytest.mark.asyncio
async def test_voice_step_local_option_desc_shows_short_model(monkeypatch, tmp_path):
    """Описание «Локальный сервис» собирается динамически: базовая строка из
    _VOICE_OPTIONS + короткое имя модели батарейки (не установлена → DEFAULT_MODEL)."""
    from gc_tui.app import TuiApp
    from gc_core.voice_runtime import paths as vpaths
    monkeypatch.setattr(vpaths, "voice_data_dir", lambda: tmp_path)
    monkeypatch.setattr("gc_core.voice_runtime.state.voice_state_path", lambda: tmp_path / "state.json")
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        screen = _screen(app, FakeVoice())
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        texts = [str(w.render()) for w in screen.query(".onb-option")]
        assert any("распознаёт офлайн, на вашем компьютере; скачает ~0.9 ГБ" in t for t in texts)
        # модель — во второй строке описания, вместе с ценой в ресурсах
        cost = [str(w.render()) for w in screen.query(".onb-hint") if "RAM" in str(w.render())]
        assert cost and "faster-whisper-small" in cost[0]


@pytest.mark.asyncio
async def test_voice_step_local_confirm_shows_short_model(monkeypatch, tmp_path):
    """Экран подтверждения: значение «голос» для local включает короткое имя модели."""
    from gc_tui.app import TuiApp
    from gc_core.voice_runtime import paths as vpaths
    monkeypatch.setattr(vpaths, "voice_data_dir", lambda: tmp_path)
    monkeypatch.setattr("gc_core.voice_runtime.state.voice_state_path", lambda: tmp_path / "state.json")
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        screen = _screen(app, FakeVoice())
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        await pilot.press("enter")                    # «Локальный сервис» — выбор
        await pilot.press("enter")                    # …и далее
        await pilot.pause()
        assert screen._step == 4
        body = str(screen.query(".onb-dyn").first().render())
        assert "локальный сервис · faster-whisper-small" in body


def test_voice_step_footer_follows_mark():
    """Футер шага голоса динамичен: пока пометки нет — «Space/Enter — выбрать · PgDn —
    далее», после пометки — «Space — выбрать · Enter — далее». Space назван обязательно:
    поля endpoint видны уже под курсором, но до выбора клавиши принадлежат шагу, и без
    подсказки непонятно, чем этот выбор сделать. До пометки клавиши названы вместе —
    отдельным пунктом строка уходит за 100 колонок."""
    from gc_tui.screens.onboarding import OnboardingScreen
    scr = OnboardingScreen(config={}, config_path="X:/nope/config.yml", review_all=True,
                           on_commit=lambda: None)
    scr._step = _VOICE_STEP
    footer = scr._footer_text()
    assert "Space/Enter — выбрать" in footer and "PgDn — далее" in footer
    assert len(footer) <= 90
    scr._voice_choice = "local"                 # курсор по умолчанию на «Локальном сервисе»
    footer = scr._footer_text()
    assert "Space — выбрать" in footer and "Enter — далее" in footer
    assert "выбрать  ·  Enter — далее" in footer          # Space идёт перед ролью Enter
    assert "Enter — выбрать" not in footer and "PgDn — далее" not in footer
    assert len(footer) <= 90


def test_mcp_step_footer_keeps_space_hint():
    """На шаге MCP Space — тумблер чекбоксов (не дубль Enter'а): подсказка остаётся."""
    from gc_tui.screens.onboarding import OnboardingScreen
    scr = OnboardingScreen(config={}, config_path="X:/nope/config.yml", review_all=True,
                           on_commit=lambda: None)
    scr._step = 2
    assert "Space — выбрать" in scr._footer_text()


@pytest.mark.asyncio
async def test_voice_space_selects_stays_then_enter_advances(monkeypatch):
    """Space — выбрать под курсором (видимое ●), остаться на шаге; Enter, когда
    выбор уже сделан, — просто далее (канон MCP-шага, перенесённый на радио голоса)."""
    from gc_tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        screen = _screen(app, FakeVoice())
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        await pilot.press("space")                   # курсор по умолчанию на «Локальный сервис»
        await pilot.pause()
        assert screen._voice_choice == "local"
        assert screen._step == _VOICE_STEP                     # остались на шаге
        texts = [str(w.render()) for w in screen.query(".onb-option")]
        assert any("(●) Локальный сервис" in t for t in texts)
        await pilot.press("enter")                    # выбор уже сделан → далее
        await pilot.pause()
        assert screen._step == 4
        assert screen._voice_choice == "local"


@pytest.mark.asyncio
async def test_voice_pagedown_after_space_ignores_cursor_drift(monkeypatch):
    """Выбор «липнет» к _voice_choice, а не к позиции курсора: PgDn («далее» откуда
    угодно) коммитит уже сделанный выбор, даже если курсор ушёл на другую опцию."""
    from gc_tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        screen = _screen(app, FakeVoice())
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        await pilot.press("space")                    # выбрали «Локальный сервис»
        await pilot.pause()
        await pilot.press("down")                     # курсор → «Свой endpoint»
        await pilot.press("down")                     # курсор → «Без голосового ввода»
        await pilot.pause()
        await pilot.press("pagedown")
        await pilot.pause()
        assert screen._step == 4
        assert screen._voice_choice == "local"          # не «skip» — курсор не при чём


@pytest.mark.asyncio
async def test_voice_enter_on_other_option_reselects(monkeypatch):
    """Enter на ДРУГОЙ (непомеченной) строке — перевыбор: пометка переезжает, шаг тот же."""
    from gc_tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        screen = _screen(app, FakeVoice())
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        await pilot.press("space")                    # ● «Локальный сервис»
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("down")                     # курсор → «Без голосового ввода»
        await pilot.press("enter")
        await pilot.pause()
        assert screen._step == _VOICE_STEP                       # не «далее», а перевыбор
        assert screen._voice_choice == "skip"
        texts = [str(w.render()) for w in screen.query(".onb-option")]
        assert any("(●) Без голосового ввода" in t for t in texts)
        assert any("( ) Локальный сервис" in t for t in texts)


@pytest.mark.asyncio
async def test_voice_enter_marks_then_advances(monkeypatch):
    """Двухролевой Enter: первый помечает вариант под курсором (остаёмся на шаге),
    второй (курсор на помеченном) — далее."""
    from gc_tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        screen = _screen(app, FakeVoice())
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        await pilot.press("down")                     # курсор → «Свой endpoint»
        await pilot.press("down")                     # курсор → «Без голосового ввода»
        await pilot.pause()
        assert screen._voice_choice == ""              # выбор ещё не сделан
        await pilot.press("enter")
        await pilot.pause()
        assert screen._voice_choice == "skip"
        assert screen._step == _VOICE_STEP                       # пометили и остались
        await pilot.press("enter")
        await pilot.pause()
        assert screen._step == 4


@pytest.mark.asyncio
async def test_voice_pagedown_without_choice_takes_cursor_option(monkeypatch):
    """PgDn без пометки — тот же фолбэк, что у Enter-далее: берём вариант под курсором."""
    from gc_tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        screen = _screen(app, FakeVoice())
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        await pilot.press("down")
        await pilot.press("down")                     # курсор → «Без голосового ввода»
        await pilot.pause()
        await pilot.press("pagedown")
        await pilot.pause()
        assert screen._voice_choice == "skip"
        assert screen._step == 4


@pytest.mark.asyncio
async def test_voice_variants_cursor_never_holds_focus_in_fields(monkeypatch):
    """Пока курсор в группе вариантов, фокуса в полях нет — даже когда «Свой endpoint»
    уже помечен (иначе Space/печать достаются полю, а не шагу, а стрелки при этом двигают
    строки режима — тот самый рассинхрон, который чинит спека §4). В поля вводит Tab."""
    from gc_tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        screen = _screen(app, FakeVoice())
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        await pilot.press("down")                     # курсор → «Свой endpoint»
        await pilot.press("enter")                    # пометить + раскрыть поля
        await pilot.pause()
        assert screen.query("#voice-url")              # поля показаны…
        assert screen.focused is None                  # …но курсор всё ещё в вариантах
        await pilot.press("tab")
        await pilot.pause()
        assert screen.query_one("#voice-url", Input).has_focus
        await pilot.press("tab")                       # обратно в варианты
        await pilot.pause()
        assert screen.focused is None
        await pilot.press("down")                     # курсор → «Без голосового ввода»
        await pilot.pause()
        assert not screen.query("#voice-url")          # поля ушли вместе с курсором


@pytest.mark.asyncio
async def test_voice_reselect_away_from_cloud_collapses_fields_and_keeps_draft(monkeypatch):
    """Перевыбор с «Свой endpoint» сворачивает поля сразу, а набранное не теряется:
    возврат на endpoint снова показывает введённый URL."""
    from gc_tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        screen = _screen(app, FakeVoice())
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        await pilot.press("down")
        await pilot.press("enter")                    # ● endpoint + поля
        await pilot.pause()
        screen.query_one("#voice-url", Input).value = "https://stt.x/v1/audio/transcriptions"
        await pilot.press("down")                     # курсор → «Без голосового ввода»
        await pilot.press("enter")                    # перевыбор
        await pilot.pause()
        assert screen._voice_choice == "skip"
        assert not screen.query("#voice-url")          # поля свернулись
        await pilot.press("up")
        await pilot.press("enter")                    # снова endpoint
        await pilot.pause()
        assert screen.query_one("#voice-url", Input).value == \
            "https://stt.x/v1/audio/transcriptions"


@pytest.mark.asyncio
async def test_voice_space_on_cloud_reveals_fields_and_stays(monkeypatch):
    """Space на «Свой endpoint»: выбирает cloud, поля показаны (их показывает курсор),
    шаг не меняется (не advance) — симметрично Enter на cloud впервые.

    Курсор пометка НЕ двигает: он остаётся в группе вариантов, и фокус в поля не уходит
    (спека §4.1 — фокус следствие курсора). Раньше здесь ждали фокус в URL; проверять
    это обязательно, иначе тест зелен при любом из двух взаимоисключающих поведений."""
    from gc_tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        screen = _screen(app, FakeVoice())
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        await pilot.press("down")                     # курсор → «Свой endpoint»
        await pilot.press("space")
        await pilot.pause()
        assert screen._voice_choice == "cloud"
        assert screen._step == _VOICE_STEP                       # шаг тот же — не advance
        assert screen.query_one("#voice-url", Input)
        assert screen._voice_cursor_group() == "variants"
        assert screen.focused is None


@pytest.mark.asyncio
async def test_voice_step_cloud_reveals_fields_and_confirm_line(monkeypatch):
    from gc_tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        screen = _screen(app, FakeVoice())
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        await pilot.press("down")                    # курсор на «Свой endpoint»
        await pilot.press("enter")
        await pilot.pause()
        url = screen.query_one("#voice-url", Input)
        url.value = "https://stt.x/v1/audio/transcriptions"
        screen.query_one("#voice-token", Input).value = "sk"
        screen.query_one("#voice-model", Input).value = "wh-1"
        await pilot.press("enter")                   # submit полей → advance
        await pilot.pause()
        assert screen._step == 4                     # подтверждение
        assert screen._voice_choice == "cloud"
        body = str(screen.query(".onb-dyn").first().render())
        assert "endpoint stt.x" in body


@pytest.mark.asyncio
async def test_commit_local_calls_switch_to_local(monkeypatch, tmp_path):
    from gc_tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    voice = FakeVoice()
    async with app.run_test() as pilot:
        screen = _screen(app, voice)
        screen._config_path = str(tmp_path / "config.yml")
        (tmp_path / "config.yml").write_text("model: m\n", encoding="utf-8")
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        await pilot.press("enter")                   # выбор «Локальный сервис»
        await pilot.press("enter")                   # …и далее
        await pilot.pause()
        assert screen._voice_choice == "local"
        assert screen._step == 4
        monkeypatch.setattr(screen, "_commit_config", lambda: True)
        screen._commit()
        assert voice.calls == ["switch_to_local"]


@pytest.mark.asyncio
async def test_pageup_after_cloud_confirm_not_stuck(monkeypatch):
    """Регресс: раскрытие cloud-полей оставляло фокус на удалённом при ремонтировании
    Input → detached focused рвал Screen._binding_chain → priority PgUp умирал насовсем."""
    from gc_tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        screen = _screen(app, FakeVoice())
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        await pilot.press("down")                    # курсор на «Свой endpoint»
        await pilot.press("enter")                   # раскрыть поля (фокус в URL)
        await pilot.pause()
        screen.query_one("#voice-url", Input).value = "https://stt.x/v1/audio/transcriptions"
        # submit() требует модель (спека §4.2, ошибка формы, не молчаливый whisper-1) —
        # тест проверяет живучесть PgUp, а не валидацию, поэтому модель тоже задаём.
        screen.query_one("#voice-model", Input).value = "wh-1"
        await pilot.press("enter")                   # submit полей → advance (confirm)
        await pilot.pause()
        assert screen._step == 4
        await pilot.press("pageup")
        await pilot.pause()
        assert screen._step == _VOICE_STEP                     # PgUp жив после cloud-полей
        await pilot.press("pageup")
        await pilot.pause()
        assert screen._step == 2                     # и остаётся живым дальше


@pytest.mark.asyncio
async def test_pageup_from_live_cloud_fields_not_stuck(monkeypatch):
    """Каскад-вариант: PgUp прямо из ЖИВЫХ cloud-полей отрабатывает и без фикса
    (виджет ещё в дереве в момент нажатия), но следующий PgUp уже наследует
    повисший после ремонтирования фокус и должен был бы стать no-op."""
    from gc_tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        screen = _screen(app, FakeVoice())
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        await pilot.press("down")                    # курсор на «Свой endpoint»
        await pilot.press("enter")                   # раскрыть поля (фокус в URL)
        await pilot.pause()
        await pilot.press("pageup")                   # назад прямо из живых полей
        await pilot.pause()
        assert screen._step == 2
        await pilot.press("pageup")                   # каскад: ещё раз назад
        await pilot.pause()
        assert screen._step == 1


@pytest.mark.asyncio
async def test_commit_local_starts_service_after_reload(monkeypatch, tmp_path):
    """Мастер закончился — голос работает сразу, без перезапуска: сервис поднимаем
    ПОСЛЕ _on_commit (там reload_config), по перечитанному конфигу — установка только
    что переписала config.local.yml, и гейт автостарта смотрит именно туда."""
    from gc_tui.app import TuiApp
    from gc_core.voice_runtime import autostart as autostart
    app = TuiApp(config={"model": "M"})
    voice = FakeVoice()
    async with app.run_test() as pilot:
        seen = []
        monkeypatch.setattr(autostart, "ensure_voice_service",
                            lambda cfg: seen.append(cfg) or True)
        new_cfg = {"voice": {"url": "http://127.0.0.1:8735/v1/audio/transcriptions"}}
        screen = _screen(app, voice)
        screen._config_path = str(tmp_path / "config.yml")
        (tmp_path / "config.yml").write_text("model: m\n", encoding="utf-8")
        screen._on_commit = lambda: setattr(app, "_config", new_cfg)
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        await pilot.press("enter")                   # «Локальный сервис»
        await pilot.pause()
        monkeypatch.setattr(screen, "_commit_config", lambda: True)
        screen._commit()
        for _ in range(60):                            # подъём идёт воркером — дожидаемся
            await pilot.pause()
            if seen:
                break
        assert voice.calls == ["switch_to_local"]
        assert seen == [new_cfg]                      # конфиг ПОСЛЕ reload


@pytest.mark.asyncio
async def test_commit_cloud_unwires_managed(monkeypatch, tmp_path):
    """«Свой endpoint» при уже установленной батарейке: managed-слой (config.local.yml)
    перекрыл бы только что записанный юзерский voice — снимаем его после успешной
    записи, симметрично канону /settings (voice_setup.wire_cloud)."""
    from gc_tui.app import TuiApp
    from gc_core.voice_runtime import wire as wire_mod
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        calls = []
        monkeypatch.setattr(wire_mod, "unwire_voice", lambda: calls.append("unwire") or True)
        screen = _screen(app, FakeVoice(installed=True))
        screen._config_path = str(tmp_path / "config.yml")
        (tmp_path / "config.yml").write_text("model: m\n", encoding="utf-8")
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        screen._voice_choice = "cloud"
        screen._voice_cloud = {"url": "https://stt.x/v1/audio/transcriptions",
                               "token": "sk", "model": "wh-1"}
        monkeypatch.setattr(screen, "_commit_config", lambda: True)
        screen._commit()
        assert calls == ["unwire"]


@pytest.mark.asyncio
async def test_commit_local_does_not_unwire(monkeypatch, tmp_path):
    """…и наоборот: локальный выбор managed-слой не трогает (его только что записал install)."""
    from gc_tui.app import TuiApp
    from gc_core.voice_runtime import wire as wire_mod
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        calls = []
        monkeypatch.setattr(wire_mod, "unwire_voice", lambda: calls.append("unwire") or True)
        screen = _screen(app, FakeVoice())
        screen._config_path = str(tmp_path / "config.yml")
        (tmp_path / "config.yml").write_text("model: m\n", encoding="utf-8")
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        await pilot.press("enter")                   # «Локальный сервис»
        await pilot.pause()
        monkeypatch.setattr(screen, "_commit_config", lambda: True)
        screen._commit()
        assert calls == []


@pytest.mark.asyncio
async def test_voice_cloud_field_placeholders(monkeypatch):
    """Пустые поля подсказывают формат — как поля шагов «Адрес»/«Модель». Языка
    в мастере нет (спека §3): дефолт схемы `ru`, правится в /settings."""
    from gc_tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        screen = _screen(app, FakeVoice())
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        assert screen.query_one("#voice-url", Input).placeholder == \
            "https://…/v1/audio/transcriptions"
        assert screen.query_one("#voice-model", Input).placeholder == "whisper-1"
        assert not screen.query("#voice-lang")


@pytest.mark.asyncio
async def test_voice_cloud_draft_survives_pageup(monkeypatch):
    """Черновик endpoint не теряется при уходе назад: PgUp снимает набранное, как и
    перевыбор стрелками, — вернувшись, человек видит свой URL."""
    from gc_tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        screen = _screen(app, FakeVoice())
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        await pilot.press("down")
        await pilot.press("enter")                   # ● endpoint + поля
        await pilot.pause()
        screen.query_one("#voice-url", Input).value = "https://stt.x/v1/audio/transcriptions"
        await pilot.press("pageup")                  # назад на шаг MCP
        await pilot.pause()
        assert screen._step == 2
        await pilot.press("pagedown")                # и снова вперёд
        await pilot.pause()
        assert screen._step == _VOICE_STEP
        assert screen.query_one("#voice-url", Input).value == \
            "https://stt.x/v1/audio/transcriptions"


@pytest.mark.asyncio
async def test_voice_cloud_fields_are_framed_editor(monkeypatch):
    """Поля «Свой endpoint» — канон-рамки VoiceEndpointEditor (подпись + поле в рамке),
    а не голые Static+Input; языка в мастере нет. Рамка одна и она всегда, но у поля со
    списком её рисует группа: «URL» (пресеты адреса, спека 2026-08-05 §3) и «Модель»
    (список с сервера) — в группе, у «Токена» (списка нет вовсе) рамка своя (§3.1)."""
    from gc_tui.app import TuiApp
    from textual.widgets import Input
    from gc_tui.editors.voice_endpoint import VoiceEndpointEditor
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        screen = _screen(app, FakeVoice())
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        ed = screen.query_one(VoiceEndpointEditor)
        # Строки-подсказки списков носят тот же класс `onb-hint` и с канона 2026-08-20
        # стоят под полями с первого кадра — в перечень подписей они не входят.
        labels = [str(w.render()) for w in ed.query(".onb-hint")
                  if not any(c in w.classes for c in ("voice-url-preset-hint",
                                                      "voice-model-hint",
                                                      "voice-lang-hint"))]
        assert labels == ["URL", "Токен", "Модель"]
        assert "set-framed" in ed.query_one("#voice-token", Input).classes
        for wid, group_id in (("#voice-url", "#voice-url-group"),
                              ("#voice-model", "#voice-model-group")):
            field = ed.query_one(wid, Input)
            assert "group-field" in field.classes and "set-framed" not in field.classes
            assert field.parent is ed.query_one(group_id)
        assert not screen.query("#voice-lang")


@pytest.mark.asyncio
async def test_editor_lookup_survives_a_pending_removal(monkeypatch):
    """Берём ПОСЛЕДНИЙ редактор: `remove()` в Textual отложенный, и сразу после
    перерисовки шага старый ещё висит в дереве.

    Мастер нарушал канон в обе стороны (ревью 2026-08-07): `_voice_endpoint_editor`
    брал `.first()` — то есть обречённый, со значениями прошлого рендера, — а сабмит
    шага звал `query_one`, который на двух матчах просто падает TooManyMatches."""
    from gc_tui.app import TuiApp
    from gc_tui.editors.voice_endpoint import VoiceEndpointEditor

    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        screen = _screen(app, FakeVoice())
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()

        doomed = screen.query_one(VoiceEndpointEditor)
        doomed.remove()                       # отложенное удаление: пока в дереве
        # Новый встаёт СРАЗУ ПОСЛЕ обречённого и в того же родителя — так и делает
        # перерисовка шага; иначе симуляция ловила бы не тот порядок обхода дерева.
        fresh = VoiceEndpointEditor(values={"url": "https://new.example/v1"},
                                    autofocus=False)
        doomed.parent.mount(fresh, after=doomed)
        assert len(list(screen.query(VoiceEndpointEditor))) == 2, \
            "не удалось воспроизвести два матча — тест перестал проверять то, что должен"

        assert screen._voice_endpoint_editor() is fresh, \
            "взят обречённый редактор вместо свежего"
        # Чем был плох голый query_one на этом месте: он не падает на двух матчах, а
        # МОЛЧА отдаёт первый — обречённый. То есть шаг сабмитил бы значения прошлого
        # рендера, и заметить это было бы нечем.
        assert screen.query_one(VoiceEndpointEditor) is doomed


@pytest.mark.asyncio
async def test_voice_cloud_invalid_url_error_inside_editor(monkeypatch):
    """Невалидный URL: шаг не продвигается, ошибка — ВНУТРИ редактора (канон
    EndpointEditor), а не подменяет заголовок шага в #onb-body."""
    from gc_tui.app import TuiApp
    from textual.widgets import Input, Static
    from gc_tui.editors.voice_endpoint import VoiceEndpointEditor
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        screen = _screen(app, FakeVoice())
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        await pilot.press("down")
        await pilot.press("enter")                   # раскрыть поля
        await pilot.pause()
        screen.query_one("#voice-url", Input).value = "ttp://bad"
        await pilot.press("enter")
        await pilot.pause()
        assert screen._step == _VOICE_STEP                      # остались на шаге
        ed = screen.query_one(VoiceEndpointEditor)
        assert any("http" in str(w.render()) for w in ed.query(".onb-warn"))
        assert "Голосовой ввод" == str(screen.query_one("#onb-body", Static).render())


@pytest.mark.asyncio
async def test_service_start_does_not_block_wizard_finish(monkeypatch, tmp_path):
    """Финал мастера не должен замирать: после свежей установки порт мёртв, и
    ensure_voice_service блокирующе ждёт готовности (до 15 с). Подъём уходит на
    воркер ПРИЛОЖЕНИЯ (экран мастера тут же сменяется на TraceScreen), результат —
    toast приложения, не виджеты уже размонтированного мастера."""
    import threading
    import time
    from gc_tui.app import TuiApp
    from gc_core.voice_runtime import autostart as autostart
    app = TuiApp(config={"model": "M"})
    voice = FakeVoice()
    async with app.run_test() as pilot:
        entered = threading.Event()
        release = threading.Event()
        toasts = []
        monkeypatch.setattr(app, "notify",
                            lambda msg, **kw: toasts.append(msg), raising=False)

        def _slow(cfg):
            entered.set()
            release.wait(10)
            return False                       # не поднялся → предупреждающий toast
        monkeypatch.setattr(autostart, "ensure_voice_service", _slow)
        screen = _screen(app, voice)
        screen._config_path = str(tmp_path / "config.yml")
        (tmp_path / "config.yml").write_text("model: m\n", encoding="utf-8")
        screen._on_commit = lambda: None
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        await pilot.press("enter")                    # «Локальный сервис»
        await pilot.pause()
        monkeypatch.setattr(screen, "_commit_config", lambda: True)

        t0 = time.monotonic()
        screen._commit()
        assert time.monotonic() - t0 < 1.0, "финал мастера ждал подъёма сервиса"
        for _ in range(60):
            await pilot.pause()
            if entered.is_set():
                break
        assert entered.is_set(), "воркер подъёма не стартовал"
        release.set()
        for _ in range(60):
            await pilot.pause()
            if toasts:
                break
        assert any("gc-voice status" in m for m in toasts)


# --- список моделей у «Своего endpoint» ---------------------------------------

_MODELS = ["whisper-large-v3-turbo", "gpt-4o-mini", "whisper-large-v3"]


def _voice_editor(screen):
    from gc_tui.editors.voice_endpoint import VoiceEndpointEditor
    return screen.query(VoiceEndpointEditor).first()


def _model_rows(screen):
    from textual.widgets import Static
    return [str(w.render()) for w in screen.query(".voice-model-row") if w.display]


def _list_hint(screen, cls: str):
    """Строка-подсказка под полем со списком (канон `_lang_hint` /settings)."""
    hints = list(screen.query(f".{cls}"))
    return str(hints[-1].render()) if hints else None


async def _to_cloud_fields(pilot, screen, url="https://s.x/v1/audio/transcriptions"):
    """Пометить «Свой endpoint» — под ним разворачиваются поля, фокус в URL."""
    from textual.widgets import Input
    await pilot.press("down")
    await pilot.press("space")
    await pilot.pause()
    screen.query_one("#voice-url", Input).value = url
    return _voice_editor(screen)


@pytest.mark.asyncio
async def test_model_field_loads_the_list_from_the_endpoint():
    """Фокус пришёл в «Модель» — список тянется с указанного endpoint: id модели больше
    не вписывают вслепую, а ошибка в адресе/токене видна сразу, а не при первой диктовке."""
    from gc_tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    voice = FakeVoice()
    voice.models = _MODELS
    async with app.run_test() as pilot:
        screen = _screen(app, voice)
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        ed = await _to_cloud_fields(pilot, screen)
        assert voice.model_calls == []          # пока курсор не в поле — в сеть не ходим
        ed.focus_field("model")
        await pilot.pause()
        await pilot.pause()
        assert voice.model_calls == [("https://s.x/v1/audio/transcriptions", "", True)]
        assert ed.model_list is not None and ed.model_list.is_open


@pytest.mark.asyncio
async def test_the_list_stays_shut_without_a_url():
    """Адреса нет — в сеть не ходим и молчим (то же правило, что в /settings)."""
    from gc_tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    voice = FakeVoice()
    voice.models = _MODELS
    async with app.run_test() as pilot:
        screen = _screen(app, voice)
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        ed = await _to_cloud_fields(pilot, screen, url="")
        ed.focus_field("model")
        await pilot.pause()
        assert voice.model_calls == []


@pytest.mark.asyncio
async def test_arrows_walk_an_open_list_not_the_mode_rows():
    """Раскрытый список забирает стрелки себе — иначе ↑/↓ увели бы курсор с «Своего
    endpoint» и свернули поля прямо во время выбора модели."""
    from gc_tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    voice = FakeVoice()
    voice.models = _MODELS
    async with app.run_test() as pilot:
        screen = _screen(app, voice)
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        ed = await _to_cloud_fields(pilot, screen)
        ed.focus_field("model")
        await pilot.pause()
        await pilot.pause()
        before = screen._idx.get(_VOICE_STEP)
        await pilot.press("down")
        await pilot.pause()
        assert screen._idx.get(_VOICE_STEP) == before      # курсор шага не сдвинулся


@pytest.mark.asyncio
async def test_enter_on_an_open_list_picks_a_model_instead_of_advancing():
    """Enter при раскрытом списке — «подставить», а не «дальше»: шаг остаётся на месте.
    Поле модели в мастере пустое, поэтому подсветки сперва нет — её ставит стрелка
    (канон списков /settings: Enter подставляет ровно подсвеченное)."""
    from gc_tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    voice = FakeVoice()
    voice.models = _MODELS
    async with app.run_test() as pilot:
        screen = _screen(app, voice)
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        ed = await _to_cloud_fields(pilot, screen)
        ed.focus_field("model")
        await pilot.pause()
        await pilot.pause()
        await pilot.press("down")                                 # подсветить первую
        await pilot.press("enter")
        await pilot.pause()
        assert screen._step == _VOICE_STEP                                  # не ушли дальше
        assert screen.query_one("#voice-model", Input).value in _MODELS
        assert not ed.model_list.is_open


@pytest.mark.asyncio
async def test_escape_on_an_open_list_only_shuts_it():
    """Esc сворачивает список и НЕ выходит из мастера — модалка выхода не появляется."""
    from gc_tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    voice = FakeVoice()
    voice.models = _MODELS
    async with app.run_test() as pilot:
        screen = _screen(app, voice)
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        ed = await _to_cloud_fields(pilot, screen)
        ed.focus_field("model")
        await pilot.pause()
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert not ed.model_list.is_open
        assert app.screen is screen                # модалки подтверждения нет


@pytest.mark.asyncio
async def test_alt_down_reopens_the_list_only_from_the_model_field():
    """Alt+↓ (спека 2026-08-06 §2) — путь назад к свёрнутому списку, но список
    принадлежит своему полю: из URL/Token он не раскрывается (в /settings это решает
    курсор ленты, здесь — фокус). Ctrl+Space на этом шаге пробы не даёт вовсе
    (§2.5, §5.1 спеки) — про список она больше не спрашивает."""
    from gc_tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    voice = FakeVoice()
    voice.models = _MODELS
    async with app.run_test() as pilot:
        screen = _screen(app, voice)
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        ed = await _to_cloud_fields(pilot, screen)
        await pilot.press("alt+down")                # фокус в URL — не наше поле
        await pilot.pause()
        assert not ed.model_list.is_open and voice.model_calls == []
        ed.focus_field("model")
        await pilot.pause()
        await pilot.pause()
        await pilot.press("escape")                  # свернуть
        await pilot.pause()
        assert not ed.model_list.is_open
        await pilot.press("alt+down")                # и вернуть с того же поля
        await pilot.pause()
        await pilot.pause()
        assert ed.model_list.is_open


# --- пресет-лист адреса «Своего endpoint» (URL, спека 2026-08-05 §3) -------------


def _url_rows(screen):
    return [str(w.render()) for w in screen.query(".voice-url-preset-row") if w.display]


@pytest.mark.asyncio
async def test_alt_down_on_a_preset_url_opens_the_full_list_with_the_match_highlighted():
    """Поле уже содержит адрес пресета (черновик/повторный проход) — вход на «URL»
    список НЕ раскрывает (канон форм 2026-08-17: пресет-лист раскрыт, только пока
    значение складывается), а Alt+↓ раскрывает его ЦЕЛИКОМ, курсор — на СОВПАВШЕМ
    пресете. Тот же канон, что у адреса «Подключения»."""
    from gc_tui.app import TuiApp
    from gc_core.config_modules.voice_endpoint_presets import DEFAULT_VOICE_ENDPOINT_PRESETS
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        screen = _screen(app, FakeVoice())
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        groq = DEFAULT_VOICE_ENDPOINT_PRESETS[1]
        ed = await _to_cloud_fields(pilot, screen, url=groq.url)
        await pilot.press("tab")                      # курсор → в поля, на «URL»
        await pilot.pause()
        assert ed.url_list is not None and not ed.url_list.is_open   # адрес задан
        await pilot.press("alt+down")                 # явная просьба показать список
        await pilot.pause()
        assert ed.url_list.is_open
        rows = _url_rows(screen)
        assert len(rows) == len(DEFAULT_VOICE_ENDPOINT_PRESETS)
        assert any(r.startswith("▸") and "Groq" in r for r in rows)


@pytest.mark.asyncio
async def test_enter_on_a_foreign_url_preset_substitutes_and_clears_the_model():
    """Enter по ЧУЖОМУ пресету — реальным путём через `on_key` экрана (не напрямую
    через редактор): URL подставлен, «Модель» стёрта (спека §4.1), список свёрнут."""
    from gc_tui.app import TuiApp
    from textual.widgets import Input
    from gc_core.config_modules.voice_endpoint_presets import DEFAULT_VOICE_ENDPOINT_PRESETS
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        screen = _screen(app, FakeVoice())
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        openai = DEFAULT_VOICE_ENDPOINT_PRESETS[0]
        ed = await _to_cloud_fields(pilot, screen, url=openai.url)
        screen.query_one("#voice-model", Input).value = "whisper-1"
        await pilot.press("tab")                      # в поля, на «URL» (адрес задан — список свёрнут)
        await pilot.pause()
        await pilot.press("alt+down")                 # раскрыть явно: подсветка на OpenAI
        await pilot.pause()
        await pilot.press("down")                     # подсветить следующий пресет (Groq)
        await pilot.press("enter")
        await pilot.pause()
        groq = DEFAULT_VOICE_ENDPOINT_PRESETS[1]
        assert screen.query_one("#voice-url", Input).value == groq.url
        assert screen.query_one("#voice-model", Input).value == ""
        assert not ed.url_list.is_open


@pytest.mark.asyncio
async def test_escape_on_the_url_list_only_shuts_it_and_keeps_the_typed_value():
    """Esc сворачивает список пресетов и не выходит из мастера; набранное остаётся —
    печать в адрес это не только фильтр, но и ввод СВОЕГО адреса
    (`restore_on_close=False`, канон `preset_list.preset_list_controller`)."""
    from gc_tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        screen = _screen(app, FakeVoice())
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        ed = await _to_cloud_fields(pilot, screen, url="")
        await pilot.press("tab")
        await pilot.pause()
        assert ed.url_list.is_open
        own = "https://my-own-host.internal/v1/audio/transcriptions"
        screen.query_one("#voice-url", Input).value = own
        await pilot.press("escape")
        await pilot.pause()
        assert not ed.url_list.is_open
        assert screen.query_one("#voice-url", Input).value == own
        assert app.screen is screen                  # модалка выхода не появилась


@pytest.mark.asyncio
async def test_alt_down_on_the_url_field_reopens_a_closed_list():
    """Alt+↓ на «URL» со свёрнутым списком раскрывает его — второй путь назад
    после Esc/своего адреса, а не no-op (симметрично списку моделей)."""
    from gc_tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        screen = _screen(app, FakeVoice())
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        own = "https://my-own-host.internal/v1/audio/transcriptions"
        ed = await _to_cloud_fields(pilot, screen, url=own)
        await pilot.press("tab")                      # свой адрес — список молчит на входе
        await pilot.pause()
        assert not ed.url_list.is_open
        await pilot.press("alt+down")
        await pilot.pause()
        assert ed.url_list.is_open


@pytest.mark.asyncio
async def test_advancing_after_a_url_pick_with_empty_model_shows_the_form_error():
    """Подстановка пресета стёрла «Модель» (спека §4.1) — Enter-«далее» с пустой
    моделью останавливается ошибкой формы `submit()`а (Task 2), шаг не уходит."""
    from gc_tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        screen = _screen(app, FakeVoice())
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        ed = await _to_cloud_fields(pilot, screen, url="")
        screen.query_one("#voice-model", Input).value = "whisper-1"
        await pilot.press("tab")                      # в поля, «URL», список раскрыт
        await pilot.pause()
        await pilot.press("enter")                    # подставить подсвеченный (первый) пресет
        await pilot.pause()
        assert screen.query_one("#voice-model", Input).value == ""
        await pilot.press("enter")                    # «далее»
        await pilot.pause()
        assert screen._step == _VOICE_STEP
        assert any("выберите модель" in str(w.render()) for w in ed.query(".onb-warn"))


# --- поля живут под своей опцией (курсор), Tab ходит по ним ---------------------

def _has_fields(screen):
    from gc_tui.editors.voice_endpoint import VoiceEndpointEditor
    return bool(screen.query(VoiceEndpointEditor))


@pytest.mark.asyncio
async def test_fields_follow_the_cursor_not_the_mark():
    """Поля принадлежат «Своему endpoint»: курсор на нём — они есть, ушёл — скрылись.
    Иначе поля чужой опции висят под «Локальным сервисом» и выглядят как его."""
    from gc_tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        screen = _screen(app, FakeVoice())
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        assert not _has_fields(screen)                 # курсор на «Локальном сервисе»
        await pilot.press("down")
        await pilot.pause()
        assert _has_fields(screen)                     # пометки ещё нет — а поля уже тут
        assert screen._voice_choice == ""
        await pilot.press("up")
        await pilot.pause()
        assert not _has_fields(screen)
        await pilot.press("down")
        await pilot.press("down")
        await pilot.pause()
        assert not _has_fields(screen)                 # «Без голосового ввода»


@pytest.mark.asyncio
async def test_typed_values_survive_a_trip_off_the_option():
    """Ушёл посмотреть другие варианты и вернулся — набранное на месте (черновик шага)."""
    from gc_tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        screen = _screen(app, FakeVoice())
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        await pilot.press("down")
        await pilot.pause()
        screen.query_one("#voice-url", Input).value = "https://s.x/v1/audio/transcriptions"
        await pilot.press("up")
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert screen.query_one("#voice-url", Input).value == "https://s.x/v1/audio/transcriptions"


@pytest.mark.asyncio
async def test_marking_keeps_the_fields_and_what_was_typed():
    """Пометка — про ●, а не про поля: курсор не двигался, значит пересобирать их
    (и терять фокус с раскрытым списком) незачем."""
    from gc_tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        screen = _screen(app, FakeVoice())
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        await pilot.press("down")
        await pilot.pause()
        screen.query_one("#voice-url", Input).value = "https://s.x/v1/audio/transcriptions"
        field = screen.query_one("#voice-url", Input)
        await pilot.press("space")
        await pilot.pause()
        assert screen._voice_choice == "cloud"
        assert screen.query_one("#voice-url", Input) is field      # тот же виджет
        assert field.value == "https://s.x/v1/audio/transcriptions"


# --- курсор ходит группами: стрелки внутри, Tab между (спека §4) ---------------


@pytest.mark.asyncio
async def test_shift_tab_is_symmetric_to_tab(tmp_path):
    """Shift+Tab — предыдущая группа, симметрично Tab, и тоже перехвачен: без явной
    ветки он остался бы живым focus_previous и увёл бы фокус мимо ленты (спека §4.2.7)."""
    from textual.widgets import Input
    from gc_testkit.tui_support import wizard_voice_step
    async with wizard_voice_step(tmp_path, mode="cloud") as (pilot, scr):
        await pilot.press("shift+tab")
        await pilot.pause()
        assert scr._voice_cursor_group() == "fields"
        assert scr.query_one("#voice-url", Input).has_focus
        await pilot.press("shift+tab")
        await pilot.pause()
        assert scr._voice_cursor_group() == "variants"
        assert pilot.app.focused is None


@pytest.mark.asyncio
async def test_tab_moves_between_groups(tmp_path):
    """Tab уводит из вариантов в поля и обратно — на ту строку, где курсор оставили:
    позиция каждой группы помнится своя (спека §4.2.1)."""
    from textual.widgets import Input
    from gc_testkit.tui_support import wizard_voice_step
    async with wizard_voice_step(tmp_path, mode="cloud") as (pilot, scr):
        # У «URL» свой пресет-лист (спека 2026-08-05 §3), и на пустом поле он раскрыт
        # сам — стрелка тогда уходит списку, а не курсору формы (свой сценарий, см.
        # test_arrows_walk_an_open_list_not_the_mode_rows). Задаём НЕ-пресетный адрес
        # заранее, чтобы список молчал и тест остался про саму ленту полей.
        scr.query_one("#voice-url", Input).value = "https://s.x/v1/audio/transcriptions"
        assert scr._voice_cursor_group() == "variants"
        variant_before = scr._voice_cursor_position()      # строка «Свой endpoint»
        await pilot.press("tab")
        await pilot.pause()
        assert scr._voice_cursor_group() == "fields"
        assert scr._voice_cursor_field() == "url"
        await pilot.press("down")                          # сместить курсор ВНУТРИ полей
        await pilot.pause()
        assert scr._voice_cursor_field() == "token"
        await pilot.press("tab")
        await pilot.pause()
        assert scr._voice_cursor_group() == "variants"
        assert scr._voice_cursor_position() == variant_before, "позиция должна помниться"
        # Клавиша съедена экраном, а не отдана focus_next: тот увёл бы фокус в соседнее
        # поле, а курсор в вариантах фокуса в полях не держит вовсе (спека §4.2.6).
        assert pilot.app.focused is None
        await pilot.press("tab")
        await pilot.pause()
        assert scr._voice_cursor_group() == "fields"
        assert scr._voice_cursor_field() == "token", "позиция группы полей тоже помнится"


@pytest.mark.asyncio
async def test_arrows_in_fields_do_not_move_variants(tmp_path):
    """Стоя в поле, стрелка двигает по полям, а не по строкам режима (спека §4.2.5):
    раньше экран перехватывал стрелки всегда, и из поля ими было не выйти иначе как
    сменив вариант под собой."""
    from textual.widgets import Input
    from gc_testkit.tui_support import wizard_voice_step
    async with wizard_voice_step(tmp_path, mode="cloud") as (pilot, scr):
        # НЕ-пресетный адрес — иначе список URL раскрывается сам (спека 2026-08-05 §3)
        # и первая же стрелка уходит ему, а не курсору формы (см. test_tab_moves_between_groups).
        scr.query_one("#voice-url", Input).value = "https://s.x/v1/audio/transcriptions"
        variant_before = scr._idx.get(_VOICE_STEP, 0)
        choice_before = scr._voice_choice
        await pilot.press("tab")                           # в поля
        await pilot.press("down")
        await pilot.pause()
        assert scr._voice_cursor_group() == "fields"
        assert scr._voice_cursor_field() == "token"
        assert scr._idx.get(_VOICE_STEP, 0) == variant_before, "строка режима не сдвинулась"
        assert scr._voice_choice == choice_before
        assert scr.query_one("#voice-token", Input).has_focus


@pytest.mark.asyncio
async def test_tab_is_swallowed_when_no_second_group(tmp_path):
    """На варианте без полей группа одна: Tab молчит — но клавишу всё равно съедает,
    иначе её заберёт штатный focus_next Textual и фокус уедет мимо ленты (спека §4.2.6)."""
    from gc_testkit.tui_support import wizard_voice_step
    async with wizard_voice_step(tmp_path, mode="local") as (pilot, scr):
        focused_before = pilot.app.focused
        await pilot.press("tab")
        await pilot.pause()
        assert scr._voice_cursor_group() == "variants"
        assert scr._voice_cursor_position() == 0
        assert pilot.app.focused is focused_before, "Tab не должен водить фокус мимо ленты"


@pytest.mark.asyncio
async def test_tab_collapses_open_list_before_leaving(tmp_path):
    """Tab при раскрытом списке сначала сворачивает его, потом уводит в другую группу —
    чтобы список не остался висеть за спиной (спека §4.2.4)."""
    from textual.widgets import Input
    from gc_testkit.tui_support import wizard_voice_step
    async with wizard_voice_step(tmp_path, mode="cloud") as (pilot, scr):
        # Без адреса список не раскрывается вовсе (идти некуда) — задаём его.
        scr.query_one("#voice-url", Input).value = "https://s.x/v1/audio/transcriptions"
        await pilot.press("tab")                           # в поля
        for _ in range(3):                                 # дойти до поля со списком
            if scr._voice_cursor_field() == "model":
                break
            await pilot.press("down")
        await pilot.pause()
        assert scr._voice_cursor_field() == "model"
        assert scr._voice_model_list().is_open
        await pilot.press("tab")
        await pilot.pause()
        assert not scr._voice_model_list().is_open
        assert scr._voice_cursor_group() == "variants"


@pytest.mark.asyncio
async def test_cursor_in_fields_leaves_no_highlighted_variant(tmp_path):
    """Курсор один (спека §4.1): ушёл в поля — подложки нет ни у одной строки режима,
    иначе на экране две подсветки сразу (строка И фокус-рамка поля). ▸ при этом
    остаётся — он маркер владельца показанных полей, а не курсора (канон /settings,
    `_render_voice`: «подсветка строго по курсору ленты, ▸ живёт по владельцу панели»)."""
    from textual.widgets import Input
    from gc_testkit.tui_support import wizard_voice_step
    async with wizard_voice_step(tmp_path, mode="cloud") as (pilot, scr):
        # НЕ-пресетный адрес — иначе список URL раскрывается сам на пустом поле (спека
        # 2026-08-05 §3), и Tab тащит за собой чужую асинхронность (монтаж строк
        # списка): тест про один курсор, а не про поведение списка (см. соседние
        # test_tab_moves_between_groups/test_arrows_in_fields_do_not_move_variants).
        scr.query_one("#voice-url", Input).value = "https://s.x/v1/audio/transcriptions"
        # `.onb-voice-row`, а не общий `.onb-option`: тот несёт и строки раскрытых
        # списков поля («URL» — пресеты, «Модель» — ответ сервера, см. комментарий
        # `onboarding._VOICE_OPTIONS`), и запрос по нему смешал бы их со строками режима.
        def highlighted():
            return [str(w.render()) for w in scr.query(".onb-voice-row") if "selected" in w.classes]

        def marked():
            return [str(w.render()) for w in scr.query(".onb-voice-row") if "▸" in str(w.render())]

        assert len(highlighted()) == 1 and "Свой endpoint" in highlighted()[0]
        await pilot.press("tab")                           # курсор → в поля
        await pilot.pause()
        assert highlighted() == [], "подсветка строки при курсоре в полях — второй курсор"
        assert len(marked()) == 1 and "Свой endpoint" in marked()[0]
        await pilot.press("tab")                           # …и обратно
        await pilot.pause()
        assert len(highlighted()) == 1 and "Свой endpoint" in highlighted()[0]


@pytest.mark.asyncio
async def test_marking_does_not_reopen_a_list_shut_by_escape(tmp_path):
    """Пометка — про ●, а не про списки: свёрнутый Esc'ом список остаётся свёрнутым.
    Иначе он возвращается сам (пометка сводила фокус, а сведение раскрывает список поля
    под курсором), и Esc выглядит сломанным.

    Клавиша здесь Enter, а не Space: Space, пока фокус в поле, забирает себе сам Input —
    он печатает пробел, и раскрытие списка на печать это канон (`reopen_for_typing`),
    а не побочка."""
    from textual.widgets import Input
    from gc_testkit.tui_support import wizard_voice_step
    async with wizard_voice_step(tmp_path, mode="cloud") as (pilot, scr):
        scr.query_one("#voice-url", Input).value = "https://s.x/v1/audio/transcriptions"
        await pilot.press("tab")                           # в поля
        for _ in range(3):
            if scr._voice_cursor_field() == "model":
                break
            await pilot.press("down")
        await pilot.pause()
        assert scr._voice_model_list().is_open
        await pilot.press("escape")                        # свернуть список
        await pilot.pause()
        assert not scr._voice_model_list().is_open
        await pilot.press("enter")                         # пометить «Свой endpoint»
        await pilot.pause()
        assert scr._voice_choice == "cloud"                # пометка сделана…
        assert scr._step == _VOICE_STEP                    # …и шаг не прыгнул
        assert not scr._voice_model_list().is_open, "Esc отменён пометкой"


@pytest.mark.asyncio
async def test_field_cursor_resets_with_the_group_on_leaving_the_step(monkeypatch, tmp_path):
    """Позиция в группе полей сбрасывается ВМЕСТЕ с группой: поля пересобраны, и вход в
    них обязан начинаться с URL. Иначе первый Tab после возврата встаёт на «Модель» — а
    вход на это поле сразу дёргает список с сервера, ничего не спросив."""
    from gc_tui.app import TuiApp
    from textual.widgets import Input
    _isolate_voice_state(monkeypatch, tmp_path)
    app = TuiApp(config={"model": "M"})
    voice = FakeVoice()
    voice.models = _MODELS
    async with app.run_test() as pilot:
        screen = _screen(app, voice)
        await app.push_screen(screen)
        await _goto_voice_step(pilot, screen)
        await pilot.press("down")                          # курсор → «Свой endpoint»
        await pilot.press("space")                         # ● endpoint
        await pilot.pause()
        screen.query_one("#voice-url", Input).value = "https://s.x/v1/audio/transcriptions"
        await pilot.press("tab")                           # в поля
        for _ in range(3):
            if screen._voice_cursor_field() == "model":
                break
            await pilot.press("down")
        await pilot.pause()
        assert screen._voice_cursor_field() == "model"
        assert voice.model_calls                           # список сходил за моделями
        # submit() требует модель (спека §4.2) — без значения PgDn упёрся бы в ошибку
        # формы и шаг никуда не ушёл бы; тест проверяет сброс курсора, не валидацию.
        screen.query_one("#voice-model", Input).value = "wh-1"
        await pilot.press("pagedown")                      # ушли со шага…
        await pilot.pause()
        assert screen._step == 4
        await pilot.press("pageup")                        # …и вернулись
        await pilot.pause()
        calls_before = len(voice.model_calls)
        await pilot.press("tab")
        await pilot.pause()
        assert screen._voice_cursor_field() == "url"
        assert len(voice.model_calls) == calls_before, "вход в поля не должен звать сервер"


@pytest.mark.asyncio
async def test_returning_to_the_step_starts_in_the_variants_group(tmp_path):
    """Уход со шага и возврат начинают с группы вариантов: поля пересобраны и фокуса не
    держат, а курсор, оставшийся в полях с прошлого визита, обещал бы движение по ним от
    пустого места (стрелка прыгала бы в поле, которого никто не выбирал)."""
    from textual.widgets import Input
    from gc_testkit.tui_support import wizard_voice_step
    async with wizard_voice_step(tmp_path, mode="cloud") as (pilot, scr):
        await pilot.press("space")                         # ● «Свой endpoint»
        await pilot.press("tab")                           # курсор → в поля
        await pilot.pause()
        scr.query_one("#voice-url", Input).value = "https://s.x/v1/audio/transcriptions"
        # submit() требует модель (спека §4.2) — без неё PgDn не продвинет шаг, и тест
        # (о сбросе группы курсора, не о валидации) не дойдёт до подтверждения.
        scr.query_one("#voice-model", Input).value = "wh-1"
        assert scr._voice_cursor_group() == "fields"
        await pilot.press("pagedown")                      # далее — на подтверждение
        await pilot.pause()
        assert scr._step == 4
        await pilot.press("pageup")                        # и обратно на шаг голоса
        await pilot.pause()
        assert scr._step == _VOICE_STEP
        assert scr._voice_cursor_group() == "variants"
        assert pilot.app.focused is None


@pytest.mark.asyncio
async def test_click_on_a_field_moves_the_cursor_there(monkeypatch, tmp_path):
    """Клик мышью по полю переносит туда курсор ленты (спека §4.3): иначе следующая
    стрелка пойдёт из «прошлого» места, где курсор остался. Окно шире обычного —
    поля шага лежат ниже сгиба, и кликнуть по ним иначе нечем."""
    from gc_tui.app import TuiApp
    _isolate_voice_state(monkeypatch, tmp_path)
    app = TuiApp(config={"model": "M"})
    async with app.run_test(size=(120, 55)) as pilot:
        screen = _screen(app, FakeVoice())
        await app.push_screen(screen)
        await _goto_voice_step(pilot, screen)
        await pilot.press("down")                          # курсор → «Свой endpoint»
        await pilot.pause()
        assert screen._voice_cursor_group() == "variants"
        await pilot.click("#voice-token")
        await pilot.pause()
        assert screen._voice_cursor_group() == "fields"
        assert screen._voice_cursor_field() == "token"
        await pilot.press("down")                          # …и стрелка идёт ОТ него
        await pilot.pause()
        assert screen._voice_cursor_field() == "model"


@pytest.mark.asyncio
async def test_modal_screens_keep_focus_moves_but_not_global_nav():
    """Гейт app-биндингов на модальном экране: ходьба фокуса ВНУТРИ экрана разрешена,
    глобальная навигация — нет (ради неё гейт и заведён)."""
    from gc_tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        screen = _screen(app, FakeVoice())
        await app.push_screen(screen)
        await pilot.pause()
        assert screen.BLOCKS_GLOBAL_NAV
        assert app.check_action("focus_next", ()) is True
        assert app.check_action("focus_previous", ()) is True
        assert app.check_action("back", ()) is True
        assert app.check_action("help", ()) is False
        assert app.check_action("settings", ()) is False


@pytest.mark.asyncio
async def test_local_option_always_shows_its_cost():
    """Цена локального сервиса видна всегда, а не под курсором: она про сам вариант, и
    от её появления-исчезания список опций дёргался на строку при каждом движении."""
    from gc_tui.app import TuiApp
    from gc_tui.screens.onboarding import _VOICE_LOCAL_COST
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        screen = _screen(app, FakeVoice())
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)

        def cost_rows():
            return [str(w.render()) for w in screen.query(".onb-hint")
                    if w.display and "RAM" in str(w.render())]

        assert len(cost_rows()) == 1 and _VOICE_LOCAL_COST in cost_rows()[0]
        for _ in range(3):                     # весь круг по опциям — строка на месте
            await pilot.press("down")
            await pilot.pause()
            assert len(cost_rows()) == 1


@pytest.mark.asyncio
async def test_no_key_hints_inside_the_step_body():
    """Подсказки про клавиши внутри тела шага не живут (спека §4.3/§6.2): их место —
    футер, который один знает, что работает здесь и сейчас. Прежняя строка «Tab —
    переключение между полями» висела под опцией и обещала свою, ни на что не похожую
    роль Tab; теперь Tab водит курсор по группам, как и везде."""
    from gc_tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        screen = _screen(app, FakeVoice())
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)

        def key_hints():
            return [str(w.render()) for w in screen.query(".onb-hint")
                    if w.display and "Tab" in str(w.render())]

        for _ in range(3):                     # весь круг по опциям, в т.ч. с полями
            assert key_hints() == []
            await pilot.press("down")
            await pilot.pause()


def test_local_cost_line_names_both_resources():
    """Строка цены называет оба ресурса и не растекается: «пока запущен» из неё убрано
    — что сервис занимает память, пока работает, и так ясно."""
    from gc_tui.screens.onboarding import _VOICE_LOCAL_COST
    assert "~1 ГБ RAM" in _VOICE_LOCAL_COST and "CPU до 4 потоков" in _VOICE_LOCAL_COST
    assert len(_VOICE_LOCAL_COST) <= 60


@pytest.mark.asyncio
async def test_desc_column_moves_left_without_the_installed_mark(monkeypatch, tmp_path):
    """Колонка описаний считается по самой длинной левой части, а не константой: без
    статуса «установлен» строки короче, и держать описания на прежнем месте значило бы
    оставить между ними дыру. Статус берётся из состояния сервиса — уводим его в tmp,
    иначе результат зависит от того, стоит ли сервис на машине разработчика."""
    from gc_tui.app import TuiApp
    from gc_core.voice_runtime import paths as vpaths
    monkeypatch.setattr(vpaths, "voice_data_dir", lambda: tmp_path)
    monkeypatch.setattr("gc_core.voice_runtime.state.voice_state_path", lambda: tmp_path / "state.json")
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        screen = _screen(app, FakeVoice())
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        without = screen._voice_desc_col(False)
        with_mark = screen._voice_desc_col(True)
        assert without < with_mark
        row = str(screen.query(".onb-option").first().render())
        assert "установлен" not in row
        assert row.index("распознаёт") == without


@pytest.mark.asyncio
async def test_cost_line_sits_tight_under_its_option_with_air_before_the_next():
    """Раскладка второй строки — вплотную под своей опцией, в колонке описаний, с
    воздухом до следующей. Проверяется по отрисованному экрану: раздельные
    margin-longhand'ы из разных CSS-правил Textual перетирает, и строка молча слипается
    со следующей опцией."""
    from gc_tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test(size=(150, 40)) as pilot:   # шире строки: проверяем раскладку, не перенос
        screen = _screen(app, FakeVoice())
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        rows = {}
        for w in screen.query(".onb-option, .onb-hint"):
            s = str(w.render())
            for key in ("Локальный сервис", "RAM", "Свой endpoint"):
                if key in s:
                    rows[key] = w.region
        local, cost, cloud = rows["Локальный сервис"], rows["RAM"], rows["Свой endpoint"]
        assert cost.y == local.y + 1                 # вплотную под своей опцией
        assert cost.height == 1                      # на широком окне — одной строкой
        assert cloud.y == cost.y + 2                 # и воздух перед следующей опцией
        # Единый столбец: описания опций и вторая строка начинаются на одной ЭКРАННОЙ
        # колонке. Считать по индексу в тексте нельзя — у строки-опции есть собственный
        # отступ в колонку, у второй строки его нет.
        screen_rows = ["".join(seg.text for seg in strip)
                       for strip in screen._compositor.render_strips()]
        col = {probe: screen_rows[y].index(probe)
               for probe, y in (("распознаёт", local.y), ("faster-whisper-small", cost.y),
                                ("любой OpenAI", cloud.y))}
        assert len(set(col.values())) == 1, col


@pytest.mark.asyncio
async def test_cost_line_keeps_its_column_when_it_wraps():
    """На узком окне вторая строка переносится — и хвост обязан остаться в своей
    колонке. Отступ пробелами в тексте удержал бы только её начало, а хвост уехал бы к
    левому краю: поэтому колонка задана отступом виджета."""
    from gc_tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test(size=(85, 40)) as pilot:
        screen = _screen(app, FakeVoice())
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        cost = next(w for w in screen.query(".onb-hint") if "RAM" in str(w.render()))
        assert cost.region.height > 1                     # окно узкое — строка переносится
        rows = ["".join(seg.text for seg in strip)
                for strip in screen._compositor.render_strips()]
        col = rows[cost.region.y].index("faster-whisper-small")
        for extra in range(1, cost.region.height):
            line = rows[cost.region.y + extra]
            # слева от колонки — только рамка экрана и пробелы, текст начинается с неё
            assert line[:col].strip() in ("", "│"), line[:col]
            assert line[col:].strip()


# --- шаг голоса открывается на действующем режиме (повторный проход мастера) ----


def _screen_with_config(app, voice, config):
    """Как _screen, но с непустым конфигом — нужен тестам префилла шага голоса."""
    from gc_tui.screens.onboarding import OnboardingScreen
    return OnboardingScreen(
        config=config, config_path="X:/nope/config.yml", review_all=True,
        on_commit=lambda: None, mcp=FakeMcp(), voice=voice)


async def _goto_voice_step(pilot, screen):
    """Как _to_voice_step, но ставит актуальный номер шага (_VOICE_STEP): тот хелпер
    завязан на нумерацию ДО объединения шагов «Адрес»/«Авторизация» в «Подключение»
    и чинится отдельной задачей плана — здесь его не трогаем."""
    from gc_tui.screens.onboarding import _VOICE_STEP
    await pilot.pause()
    screen._values["completions_url"] = "http://h:1/v1/chat/completions"
    screen._values["Authorization"] = ""
    screen._values["model"] = "m"
    screen._step = _VOICE_STEP
    screen._render_step()
    await pilot.pause()


def _isolate_voice_state(monkeypatch, tmp_path):
    """_voice_installed()/current_model_short() читают state.json НАПРЯМУЮ, мимо
    инжектированного voice= (предсуществующий разнобой этого файла) — без изоляции
    тесты стучались бы в РЕАЛЬНЫЙ каталог данных агента на машине разработчика (тот
    же приём, что test_onboarding_screen.py)."""
    from gc_core.voice_runtime import paths as vpaths
    monkeypatch.setattr(vpaths, "voice_data_dir", lambda: tmp_path / "voice_data")
    monkeypatch.setattr("gc_core.voice_runtime.state.voice_state_path",
                        lambda: tmp_path / "voice_data" / "state.json")


def _write_cloud_config(tmp_path, *, url: str, token: str, model: str) -> str:
    """Реальный config.yml с уже сохранённой voice cloud-секцией: шаблон + write_values
    (тот же путь, что test_onboarding_config_writer.py::test_write_voice_values_uncomments_template_block)
    — гарантированно валиден и, в отличие от self._config, не слит с managed-слоем и
    не резолвит ${ENV:...} (источник _voice_raw_cloud)."""
    from pathlib import Path
    from gc_core import onboarding_config_writer as w
    from gc_core.config import _default_config_path, validate_config_text
    template = Path(_default_config_path())
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    w.write_values(cfg_path, {"voice.url": url, "voice.token": token, "voice.model": model},
                   template_path=template, validate=validate_config_text)
    return str(cfg_path)


@pytest.mark.asyncio
async def test_voice_step_prefills_cloud_from_config(tmp_path, monkeypatch):
    """Повторный проход мастера (--init-config) на конфиге с уже рабочим облачным
    голосом: шаг открывается на ДЕЙСТВУЮЩЕМ режиме, а не на дефолте «Локальный
    сервис», и поля endpoint'а уже показывают сохранённые адрес и модель.

    Источник полей — РЕАЛЬНЫЙ файл (_voice_raw_cloud читает config_path, не
    self._config, см. task-7-report.md «Раунд фиксов 1», находка 2), поэтому конфиг
    для этого теста — не только in-memory словарь, но и файл на диске."""
    from gc_tui.app import TuiApp
    from gc_tui.services.voice_setup import VoiceStatus
    from gc_tui.screens.onboarding import _VOICE_OPTIONS, _VOICE_STEP
    from textual.widgets import Input
    from gc_core.onboarding import display_secret
    _isolate_voice_state(monkeypatch, tmp_path)
    app = TuiApp(config={"model": "M"})
    voice = FakeVoice()
    voice.status_cheap = lambda cfg: VoiceStatus(
        installed=False, service_alive=False, port=None, model=None, mode="cloud")
    config = {"voice": {"url": "https://stt.example.com/v1/audio/transcriptions",
                        "token": "sk-real-secret-value", "model": "whisper-large-v3",
                        "language": "ru"}}
    cfg_path = _write_cloud_config(tmp_path, url=config["voice"]["url"],
                                   token=config["voice"]["token"],
                                   model=config["voice"]["model"])
    async with app.run_test() as pilot:
        screen = _screen_with_config(app, voice, config)
        screen._config_path = cfg_path
        await app.push_screen(screen)
        await _goto_voice_step(pilot, screen)
        assert screen._voice_choice == "cloud"
        assert _VOICE_OPTIONS[screen._idx[_VOICE_STEP]][0] == "cloud"
        assert screen.query_one("#voice-url", Input).value == \
            "https://stt.example.com/v1/audio/transcriptions"
        assert screen.query_one("#voice-model", Input).value == "whisper-large-v3"
        token_field = screen.query_one("#voice-token", Input)
        assert token_field.value == ""                  # секрет не подставляется
        assert token_field.placeholder == display_secret("sk-real-secret-value")


@pytest.mark.asyncio
async def test_ctrl_u_clears_the_saved_voice_token_in_the_wizard(tmp_path, monkeypatch):
    """Голосовой шаг мастера: Ctrl+U на «Токене» стирает СОХРАНЁННЫЙ секрет.

    Тот же смысл и та же клавиша, что на шаге «Подключение» и в /settings — иначе
    одна и та же маска на соседних экранах вела бы себя по-разному. Пустое поле само
    по себе значит «оставить как было», поэтому убрать секрет было нечем."""
    from gc_tui.app import TuiApp
    from textual.widgets import Input
    from gc_tui.services.voice_setup import VoiceStatus
    _isolate_voice_state(monkeypatch, tmp_path)
    app = TuiApp(config={"model": "M"})
    voice = FakeVoice()
    voice.status_cheap = lambda cfg: VoiceStatus(
        installed=False, service_alive=False, port=None, model=None, mode="cloud")
    config = {"voice": {"url": "https://stt.example.com/v1/audio/transcriptions",
                        "token": "sk-real-secret-value", "model": "whisper-large-v3",
                        "language": "ru"}}
    cfg_path = _write_cloud_config(tmp_path, url=config["voice"]["url"],
                                   token=config["voice"]["token"],
                                   model=config["voice"]["model"])
    async with app.run_test() as pilot:
        screen = _screen_with_config(app, voice, config)
        screen._config_path = cfg_path
        await app.push_screen(screen)
        await _goto_voice_step(pilot, screen)
        await pilot.pause()
        # Ходим КЛАВИШАМИ, как человек: программный focus() обходит реальный маршрут
        # события, и живой дефект (Ctrl+U на шаге голоса не срабатывал) так не виден.
        await pilot.press("tab")                        # из строк режима — в поля, на «URL»
        await pilot.pause()
        await pilot.press("down")                       # на «Токен»
        await pilot.pause()
        assert screen.focused is not None and screen.focused.id == "voice-token"
        ed = _voice_editor(screen)

        await pilot.press("ctrl+u")
        await pilot.pause()
        token_field = screen.query_one("#voice-token", Input)
        assert token_field.placeholder == ""            # маски сохранённого больше нет
        assert ed.token_cleared is True

        screen._capture_voice_fields()                  # то же, что делает уход с шага
        assert screen._voice_token_cleared is True, "пометка обязана пережить пересборку"


@pytest.mark.asyncio
async def test_voice_step_prefills_local_from_config(tmp_path, monkeypatch):
    """То же самое для локального сервиса: курсор встаёт на «Локальный сервис»."""
    from gc_tui.app import TuiApp
    from gc_tui.services.voice_setup import VoiceStatus
    from gc_tui.screens.onboarding import _VOICE_OPTIONS, _VOICE_STEP
    _isolate_voice_state(monkeypatch, tmp_path)
    app = TuiApp(config={"model": "M"})
    voice = FakeVoice(installed=True)
    voice.status_cheap = lambda cfg: VoiceStatus(
        installed=True, service_alive=True, port=8765, model="faster-whisper-small",
        mode="local")
    async with app.run_test() as pilot:
        screen = _screen_with_config(app, voice, {})
        await app.push_screen(screen)
        await _goto_voice_step(pilot, screen)
        assert screen._voice_choice == "local"
        assert _VOICE_OPTIONS[screen._idx[_VOICE_STEP]][0] == "local"


@pytest.mark.asyncio
async def test_prefill_reads_raw_document_not_merged_config(tmp_path, monkeypatch):
    """Источник префилла — СЫРОЙ config.yml (config_path), а не self._config: тот
    слит с managed-слоем и с уже раскрытыми ${ENV:...} (канон /settings,
    _voice_raw_cloud). Конфликтующие значения в merged-словаре и в реальном файле —
    префилл обязан показать файл, а не память (task-7-report.md, находка 2):
    иначе managed-заглушка локальной батарейки читалась бы как облачный секрет, а
    url/model на коммите запекались бы в файл уже раскрытыми литералами."""
    from gc_tui.app import TuiApp
    from gc_tui.services.voice_setup import VoiceStatus
    from textual.widgets import Input
    from gc_core.onboarding import display_secret
    _isolate_voice_state(monkeypatch, tmp_path)
    app = TuiApp(config={"model": "M"})
    voice = FakeVoice()
    voice.status_cheap = lambda cfg: VoiceStatus(
        installed=False, service_alive=False, port=None, model=None, mode="cloud")
    # merged (в памяти, self._config) — то, что видел бы старый код; в файле — другое,
    # ровно так расходятся managed-слой/${ENV:...} с сырым документом в жизни.
    merged_config = {"voice": {"url": "http://127.0.0.1:8735/v1/audio/transcriptions",
                               "token": "local", "model": "merged-model", "language": "ru"}}
    cfg_path = _write_cloud_config(tmp_path, url="https://stt.example.com/v1/audio/transcriptions",
                                   token="sk-raw-secret", model="whisper-large-v3")
    async with app.run_test() as pilot:
        screen = _screen_with_config(app, voice, merged_config)
        screen._config_path = cfg_path
        await app.push_screen(screen)
        await _goto_voice_step(pilot, screen)
        assert screen.query_one("#voice-url", Input).value == \
            "https://stt.example.com/v1/audio/transcriptions"          # из файла, не merged
        assert screen.query_one("#voice-model", Input).value == "whisper-large-v3"
        token_field = screen.query_one("#voice-token", Input)
        assert token_field.placeholder == display_secret("sk-raw-secret")   # не маска "local"


@pytest.mark.asyncio
async def test_voice_step_prefill_noop_when_nothing_configured(tmp_path, monkeypatch):
    """mode == "off" — ничего не настроено (первичный сценарий, --init-config на
    пустом конфиге): префилл не выбирает ничего, курсор остаётся дефолтным —
    «Локальный сервис», как и до фичи.

    Ветку кодом никто не менял (см. task-7-report.md), но её не проверял НИ ОДИН
    тест: все прочие тесты файла подсовывают FakeVoice без status_cheap и
    попадают в except Exception раньше, чем доходят до mode == "off"."""
    from gc_tui.app import TuiApp
    from gc_tui.services.voice_setup import VoiceStatus
    from gc_tui.screens.onboarding import _VOICE_STEP
    _isolate_voice_state(monkeypatch, tmp_path)
    app = TuiApp(config={"model": "M"})
    voice = FakeVoice()
    voice.status_cheap = lambda cfg: VoiceStatus(
        installed=False, service_alive=False, port=None, model=None, mode="off")
    async with app.run_test() as pilot:
        screen = _screen_with_config(app, voice, {})
        await app.push_screen(screen)
        await _goto_voice_step(pilot, screen)
        assert screen._voice_choice == ""
        assert screen._idx.get(_VOICE_STEP, 0) == 0


@pytest.mark.asyncio
async def test_prefill_reads_snapshot_only_once_per_step_visit(tmp_path, monkeypatch):
    """Гейт префилла — «прочитали снимок один раз», а не «пока выбор не сделан»:
    при mode == "off" выбор так и остаётся пустым, а тело шага голоса пересобирается
    на КАЖДОЕ движение курсора (смена опции меняет _voice_body_shape) — без
    отдельного флага снимок дёргался бы на каждую стрелку (сейчас он дешёвый,
    status_cheap, но лишние чтения state.json на каждый ход курсора не нужны)."""
    from gc_tui.app import TuiApp
    from gc_tui.services.voice_setup import VoiceStatus
    _isolate_voice_state(monkeypatch, tmp_path)
    app = TuiApp(config={"model": "M"})
    voice = FakeVoice()
    calls = []

    def _snap(cfg):
        calls.append(1)
        return VoiceStatus(installed=False, service_alive=False, port=None,
                           model=None, mode="off")
    voice.status_cheap = _snap
    async with app.run_test() as pilot:
        screen = _screen_with_config(app, voice, {})
        await app.push_screen(screen)
        await _goto_voice_step(pilot, screen)
        assert len(calls) == 1
        await pilot.press("down")               # local → cloud: тело шага пересобирается
        await pilot.press("down")               # cloud → skip: тоже пересобирается
        await pilot.press("up")                 # skip → cloud: и снова
        await pilot.pause()
        assert len(calls) == 1                  # снапшот прочитан ровно один раз


@pytest.mark.asyncio
async def test_passing_through_voice_step_keeps_cloud_backend(tmp_path, monkeypatch):
    """Главная защита: пролистывание шага голоса (PgDn), НЕ трогая ничего, не
    подменяет уже рабочий облачный провайдер локальным сервисом — регресс из
    Task 7 (_voice_advance брал вариант под курсором, а тот по умолчанию нулевой)."""
    from gc_tui.app import TuiApp
    from gc_tui.services.voice_setup import VoiceStatus
    from gc_tui.screens.onboarding import _LAST_STEP
    _isolate_voice_state(monkeypatch, tmp_path)
    app = TuiApp(config={"model": "M"})
    voice = FakeVoice()
    voice.status_cheap = lambda cfg: VoiceStatus(
        installed=False, service_alive=False, port=None, model=None, mode="cloud")
    config = {"voice": {"url": "https://stt.example.com/v1/audio/transcriptions",
                        "token": "sk-real-secret-value", "model": "whisper-large-v3",
                        "language": "ru"}}
    cfg_path = _write_cloud_config(tmp_path, url=config["voice"]["url"],
                                   token=config["voice"]["token"],
                                   model=config["voice"]["model"])
    async with app.run_test() as pilot:
        screen = _screen_with_config(app, voice, config)
        screen._config_path = cfg_path
        await app.push_screen(screen)
        await _goto_voice_step(pilot, screen)
        await pilot.press("pagedown")                    # пролистали, ничего не тронув
        await pilot.pause()
        assert screen._step == _LAST_STEP
        assert screen._voice_choice == "cloud"


@pytest.mark.asyncio
async def test_commit_cloud_empty_token_keeps_saved_secret(tmp_path, monkeypatch):
    """Критический регресс: префилл ставит voice.token = "" (канон «пустое поле =
    оставить сохранённый», секрет в поле не кладём), но пролистывание шага писало
    его в файл БЕЗ подстановки сохранённого — коммит стирал рабочий токен на диске.

    Идём РЕАЛЬНЫМ путём записи (_commit_config → файл на диске), а не проверкой
    _voice_choice — тот тест слома не увидел (task-7-report.md, находка 1)."""
    from pathlib import Path
    from gc_tui.app import TuiApp
    from gc_tui.services.voice_setup import VoiceStatus
    _isolate_voice_state(monkeypatch, tmp_path)
    app = TuiApp(config={"model": "M"})
    voice = FakeVoice()
    voice.status_cheap = lambda cfg: VoiceStatus(
        installed=False, service_alive=False, port=None, model=None, mode="cloud")
    config = {"voice": {"url": "https://stt.example.com/v1/audio/transcriptions",
                        "token": "sk-real-secret-value", "model": "whisper-large-v3",
                        "language": "ru"}}
    cfg_path = _write_cloud_config(tmp_path, url=config["voice"]["url"],
                                   token=config["voice"]["token"],
                                   model=config["voice"]["model"])
    async with app.run_test() as pilot:
        screen = _screen_with_config(app, voice, config)
        screen._config_path = cfg_path
        await app.push_screen(screen)
        await _goto_voice_step(pilot, screen)
        await pilot.press("pagedown")                    # пролистали, поле токена не трогали
        await pilot.pause()
        assert screen._commit_config()
        text = Path(cfg_path).read_text(encoding="utf-8")
        assert "sk-real-secret-value" in text
        # Проверяем ЗНАЧЕНИЕ в секции, а не подстроку по файлу: шаблон несёт
        # закомментированный пример `models:` со строкой `token: ""`, и поиск по тексту
        # спотыкался бы о комментарий (поймано синком с профилями).
        import yaml
        assert yaml.safe_load(text)["voice"]["token"] == "sk-real-secret-value"


@pytest.mark.asyncio
async def test_pagedown_recovers_cloud_fields_when_cursor_drifted_away(tmp_path, monkeypatch):
    """Предсуществующий крэш стал достижимым БЕЗ осознанных действий: префилл сам
    ставит _voice_choice = "cloud", человек уводит курсор на другую опцию и жмёт
    PgDn — раньше это падало NoMatches (поля живут под курсором, а не под пометкой,
    и на «Без голосового ввода» их в DOM нет). Нужная реакция — тот же «первый
    такт», что и пометка: вернуть курсор на «Свой endpoint», раскрыть поля, не
    продвигаться дальше (task-7-report.md, находка 6)."""
    from gc_tui.app import TuiApp
    from gc_tui.services.voice_setup import VoiceStatus
    from gc_tui.screens.onboarding import _VOICE_OPTIONS, _VOICE_STEP
    from gc_tui.editors.voice_endpoint import VoiceEndpointEditor
    _isolate_voice_state(monkeypatch, tmp_path)
    app = TuiApp(config={"model": "M"})
    voice = FakeVoice()
    voice.status_cheap = lambda cfg: VoiceStatus(
        installed=False, service_alive=False, port=None, model=None, mode="cloud")
    config = {"voice": {"url": "https://stt.example.com/v1/audio/transcriptions",
                        "token": "sk-real-secret-value", "model": "whisper-large-v3",
                        "language": "ru"}}
    async with app.run_test() as pilot:
        screen = _screen_with_config(app, voice, config)
        await app.push_screen(screen)
        await _goto_voice_step(pilot, screen)
        assert screen._voice_choice == "cloud"
        await pilot.press("down")                        # cloud → «Без голосового ввода»
        await pilot.pause()
        assert _VOICE_OPTIONS[screen._idx[_VOICE_STEP]][0] == "skip"
        await pilot.press("pagedown")                     # раньше здесь падал NoMatches
        await pilot.pause()
        assert screen._step == _VOICE_STEP                # не продвинулись
        assert _VOICE_OPTIONS[screen._idx[_VOICE_STEP]][0] == "cloud"   # курсор вернулся
        assert screen.query(VoiceEndpointEditor)           # поля снова раскрыты


@pytest.mark.asyncio
async def test_model_list_uses_saved_token_when_field_empty(tmp_path, monkeypatch):
    """Список моделей уходит с СОХРАНЁННЫМ токеном, когда поле токена пусто (канон
    saved_token из /settings, VoiceEndpointEditor.list_source_auth): без него запрос
    на рабочем облаке 401-ит — под полем «Модель» повисает «не удалось получить
    список» при полностью рабочей связке (task-7-report.md, находка 3)."""
    from gc_tui.app import TuiApp
    _isolate_voice_state(monkeypatch, tmp_path)
    app = TuiApp(config={"model": "M"})
    voice = FakeVoice()
    voice.models = _MODELS
    cfg_path = _write_cloud_config(tmp_path, url="https://s.x/v1/audio/transcriptions",
                                   token="sk-saved-secret", model="whisper-1")
    async with app.run_test() as pilot:
        screen = _screen(app, voice)
        screen._config_path = cfg_path
        await app.push_screen(screen)
        await _goto_voice_step(pilot, screen)
        ed = await _to_cloud_fields(pilot, screen)
        ed.focus_field("model")
        await pilot.pause()
        await pilot.pause()
        assert voice.model_calls == [
            ("https://s.x/v1/audio/transcriptions", "sk-saved-secret", True)]


# --- Раунд фиксов 2 -------------------------------------------------------------


@pytest.mark.asyncio
async def test_voice_cloud_draft_token_does_not_leak_saved_secret(tmp_path, monkeypatch):
    """Регресс: подставленный при коммите сохранённый токен не имеет права осесть в
    черновике (`self._voice_cloud`) — черновик восстанавливает ПОЛЯ при возврате на
    шаг, а поле токена намеренно держат пустым (секрет — не в поле, см. префилл).
    PgDn (submit облачных полей, токен не трогали) → PgUp обратно на шаг голоса не
    должны показать в поле реальный секрет из config.yml.

    Второй ассерт обязателен: без него фикс мог бы «починить» поле ценой возврата
    к прошлому Critical — коммит без подстановки стирал бы сохранённый токен
    (см. test_commit_cloud_empty_token_keeps_saved_secret, находка 1 раунда 1)."""
    from pathlib import Path
    from gc_tui.app import TuiApp
    from gc_tui.services.voice_setup import VoiceStatus
    from textual.widgets import Input
    _isolate_voice_state(monkeypatch, tmp_path)
    app = TuiApp(config={"model": "M"})
    voice = FakeVoice()
    voice.status_cheap = lambda cfg: VoiceStatus(
        installed=False, service_alive=False, port=None, model=None, mode="cloud")
    config = {"voice": {"url": "https://stt.example.com/v1/audio/transcriptions",
                        "token": "sk-real-secret-value", "model": "whisper-large-v3",
                        "language": "ru"}}
    cfg_path = _write_cloud_config(tmp_path, url=config["voice"]["url"],
                                   token=config["voice"]["token"],
                                   model=config["voice"]["model"])
    async with app.run_test() as pilot:
        screen = _screen_with_config(app, voice, config)
        screen._config_path = cfg_path
        await app.push_screen(screen)
        await _goto_voice_step(pilot, screen)
        await pilot.press("pagedown")                    # submit полей (токен не трогали) → далее
        await pilot.pause()
        await pilot.press("pageup")                       # обратно на шаг голоса
        await pilot.pause()
        token_field = screen.query_one("#voice-token", Input)
        assert token_field.value == ""                    # черновик не хранит секрет
        assert screen._commit_config()                    # запись всё равно идёт с фолбэком
        text = Path(cfg_path).read_text(encoding="utf-8")
        assert "sk-real-secret-value" in text
        # Значение секции, а не подстрока файла: в шаблоне есть закомментированный
        # пример `models:` со строкой `token: ""` (см. соседний тест выше).
        import yaml
        assert yaml.safe_load(text)["voice"]["token"] == "sk-real-secret-value"


@pytest.mark.asyncio
async def test_model_list_carries_verify_ssl_and_proxy_from_config(tmp_path, monkeypatch):
    """Список моделей «Своего endpoint» обязан ходить с TLS-настройкой и прокси
    ДЕЙСТВУЮЩЕГО конфига (канон /settings — `SettingsScreen._mount_voice_section`
    передаёт `verify_ssl=`/`config_proxy=` в тот же `VoiceEndpointEditor`): в
    корп-сети (self-signed TLS, обязательный прокси) иначе запрос уходит с
    `verify=True` мимо `backend.proxy`, и рабочий endpoint выглядит как «не
    удалось получить список»."""
    from gc_tui.app import TuiApp
    _isolate_voice_state(monkeypatch, tmp_path)
    config = {"proxy": "http://corp:3128", "voice": {"verify_ssl": False}}
    app = TuiApp(config={"model": "M"})
    voice = FakeVoice()
    voice.models = _MODELS
    async with app.run_test() as pilot:
        screen = _screen_with_config(app, voice, config)
        await app.push_screen(screen)
        await _goto_voice_step(pilot, screen)
        ed = await _to_cloud_fields(pilot, screen)
        ed.focus_field("model")
        await pilot.pause()
        await pilot.pause()
        assert voice.model_calls == [
            ("https://s.x/v1/audio/transcriptions", "", False)]
        assert voice.model_proxies == ["http://corp:3128"]


# --- Раунд фиксов 3 -------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_list_prefers_typed_connection_proxy_over_config(tmp_path, monkeypatch):
    """Регресс: набранный на шаге «Подключение» прокси обязан перевешивать конфиг —
    в мастере это ОДИН проход, и на первичном запуске конфига с рабочим прокси нет
    вовсе (человек его только что вписал). Раньше `_mount_voice_fields` брал прокси
    ИСКЛЮЧИТЕЛЬНО из self._config (канон /settings, где конфиг уже существует и
    голос/прокси редко правят в одну сессию) — в мастере это значило, что список
    моделей на шаге голоса уходил мимо только что введённого прокси."""
    from gc_tui.app import TuiApp
    _isolate_voice_state(monkeypatch, tmp_path)
    config = {"proxy": "http://stale-config-proxy:1"}   # в конфиге — ДРУГОЙ прокси
    app = TuiApp(config={"model": "M"})
    voice = FakeVoice()
    voice.models = _MODELS
    async with app.run_test() as pilot:
        screen = _screen_with_config(app, voice, config)
        await app.push_screen(screen)
        await _goto_voice_step(pilot, screen)
        screen._values["proxy"] = "http://typed-on-connection-step:2"   # шаг пройден
        ed = await _to_cloud_fields(pilot, screen)
        ed.focus_field("model")
        await pilot.pause()
        await pilot.pause()
        assert voice.model_proxies == ["http://typed-on-connection-step:2"]


@pytest.mark.asyncio
async def test_model_list_falls_back_to_config_proxy_when_connection_step_not_visited(
        tmp_path, monkeypatch):
    """Парный случай: шаг «Подключение» не проходили (`self._values["proxy"]` —
    ключа нет вовсе, а не пустая строка — «не трогали», а не «стёрли осознанно»),
    но в конфиге прокси уже есть (повторный проход мастера / прыжок сразу на шаг
    голоса) — список моделей обязан взять его, а не остаться без прокси вовсе."""
    from gc_tui.app import TuiApp
    _isolate_voice_state(monkeypatch, tmp_path)
    config = {"proxy": "http://corp:3128"}
    app = TuiApp(config={"model": "M"})
    voice = FakeVoice()
    voice.models = _MODELS
    async with app.run_test() as pilot:
        screen = _screen_with_config(app, voice, config)
        await app.push_screen(screen)
        await _goto_voice_step(pilot, screen)          # шаг «Подключение» не проходили
        assert "proxy" not in screen._values            # предпосылка теста: ключа нет
        ed = await _to_cloud_fields(pilot, screen)
        ed.focus_field("model")
        await pilot.pause()
        await pilot.pause()
        assert voice.model_proxies == ["http://corp:3128"]


@pytest.mark.asyncio
async def test_list_hints_stand_under_every_field_from_the_first_frame():
    """Шаг мастера — тот же контракт, что у секции /settings: строка «чем раскрыть
    список» стоит под каждым полем со списком сразу, а не после первого раскрытия."""
    from gc_tui.app import TuiApp
    from gc_tui.editors.field_list import LIST_CLOSED_HINT
    from gc_tui.editors.voice_endpoint import _MODEL_RELOAD_HINT
    app = TuiApp(config={"model": "M"})
    voice = FakeVoice()
    voice.models = _MODELS
    async with app.run_test() as pilot:
        screen = _screen(app, voice)
        await app.push_screen(screen)
        await _to_voice_step(pilot, screen)
        await _to_cloud_fields(pilot, screen)
        await pilot.pause()
        assert _list_hint(screen, "voice-url-preset-hint") == LIST_CLOSED_HINT
        assert _list_hint(screen, "voice-model-hint") == _MODEL_RELOAD_HINT
