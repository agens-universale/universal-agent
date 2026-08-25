"""Секция «Голосовой ввод» в /settings: две радио-строки режима со статусом в хвосте,
поля режима ПОД КУРСОРОМ (локальные — факты, облачные — правятся), Del-удаление."""
import pytest

pytest.importorskip("textual")

from tui.services.voice_setup import ProbeResult, VoiceStatus


@pytest.fixture(autouse=True)
def _isolate_config_layers(monkeypatch, tmp_path):
    """Слои поверх базового config.yml — на tmp. Без этого правило ⚠-строки читало бы
    РЕАЛЬНЫЙ ~/.gc/config.local.yml разработчика (voice-секцию туда пишет наш же UI)
    и .gc/config.yml рабочего каталога — вердикт зависел бы от машины."""
    import config as config_mod
    import mcp_runtime.paths as mcp_paths
    monkeypatch.setattr(mcp_paths, "managed_config_path",
                        lambda: tmp_path / "config.local.yml")
    monkeypatch.setattr(config_mod, "_project_local_config_path", lambda: None)


class FakeVoiceSetup:
    """Фасад voice_setup: статус — изменяемый (действия меняют его, как в жизни)."""

    def __init__(self, status):
        self.status = status
        self.calls = []
        self.wired = {}
        self.probe_result = None
        self.probe_calls = []         # (url, token, model) каждой проверки
        self.probe_proxies = []       # прокси агента, дошедший до проверки
        self.probe_verifies = []      # verify_ssl, дошедший до проверки
        self.language = "ru"
        self.switch_config = None
        self.update_config = None
        self.snapshots = 0
        self.models = None            # что отдаёт list_models (None = не достучались)
        self.model_calls = []         # (url, token, verify) каждой загрузки списка
        self.model_proxies = []       # прокси агента, дошедший до списка

    def status_cheap(self, config):
        self.snapshots += 1        # обращения к снимку — сторожа кэша считают их
        return self.status

    def probe_alive(self):
        # Живость для воркера экрана: у фейка она уже в статусе.
        return self.status.service_alive

    def switch_to_local(self, echo=print, config=None):
        self.calls.append("switch_to_local")
        self.switch_config = config
        return True

    def remove_local(self):
        self.calls.append("remove_local")
        return True

    def update_local(self, config, echo=print):
        self.calls.append("update_local")
        self.update_config = config
        return True

    def wire_cloud(self, path, url, token, model, language="ru"):
        self.calls.append("wire_cloud")
        self.wired = dict(url=url, token=token, model=model, language=language)

    def probe_endpoint(self, url, token, model, timeout=10, config_proxy=None,
                       verify=True):
        self.calls.append("probe_endpoint")
        self.probe_calls.append((url, token, model))
        self.probe_proxies.append(config_proxy)
        self.probe_verifies.append(verify)
        return self.probe_result

    def list_models(self, url, token, verify=True, config_proxy=None):
        self.model_calls.append((url, token, verify))
        self.model_proxies.append(config_proxy)
        return self.models

    def local_language(self, config=None):
        return self.language

    def set_local_language(self, language):
        self.calls.append("set_local_language")
        self.language = language
        return self.status.installed


LOCAL = VoiceStatus(installed=True, service_alive=True, port=8735,
                    model="Systran/faster-whisper-small", mode="local")
LOCAL_INSTALLED_CLOUD = VoiceStatus(installed=True, service_alive=False, port=8735,
                                    model="Systran/faster-whisper-small", mode="cloud")
LOCAL_DEAD = VoiceStatus(installed=True, service_alive=False, port=8735,
                         model="Systran/faster-whisper-small", mode="local")
# Движок отстал от версии сборки — единственное состояние, в котором секция обещает
# Ctrl+U и рисует «· доступно …».
LOCAL_OUTDATED = VoiceStatus(installed=True, service_alive=True, port=8735,
                             model="Systran/faster-whisper-small", mode="local",
                             available_version="v0.9.0")
CLOUD = VoiceStatus(installed=False, service_alive=False, port=None, model=None, mode="cloud")
OFF = VoiceStatus(installed=False, service_alive=False, port=None, model=None, mode="off")

_URL = "https://s.x/v1/audio/transcriptions"
_OURS = "http://127.0.0.1:8735/v1/audio/transcriptions"


def _mk_screen(tmp_path, status, on_apply=None, config_text="model: m\n"):
    from tui.screens.settings import SettingsScreen
    cfg = tmp_path / "config.yml"
    cfg.write_text(config_text, encoding="utf-8")
    fake = FakeVoiceSetup(status)
    scr = SettingsScreen(config={"voice": {}}, config_path=str(cfg),
                         on_apply=on_apply or (lambda: None), voice=fake)
    return scr, fake


CLOUD_CONFIG = ("model: m\nvoice:\n  url: https://old.x/v1/audio/transcriptions\n"
                "  token: old-token\n  model: whisper-old\n")


def _no_state(monkeypatch, tmp_path):
    """Батарейка не установлена → current_model_short() = DEFAULT_MODEL."""
    monkeypatch.setattr("voice_runtime.state.voice_state_path",
                        lambda: tmp_path / "nowhere" / "state.json")


def _rows(scr):
    return [str(w.render()) for w in scr.query(".set-voicerow")]


def _hint(scr):
    return str(list(scr.query(".set-voicehint"))[-1].render())


def _focused_id(scr):
    return scr.focused.id if scr.focused is not None else None


def _cursor_row(scr):
    """Индекс строки режима под ▸ или None (ни одной). ▸ остаётся на строке-ВЛАДЕЛЬЦЕ
    полей, пока курсор ленты стоит в её полях: иначе не видно, к какому режиму они
    относятся."""
    for i, row in enumerate(_rows(scr)):
        if row.startswith("▸"):
            return i
    return None


def _selected_rows(scr):
    """Индексы строк режима с фоновой подсветкой (класс выделения). Подсветка — про
    «курсор НА строке», в отличие от ▸ (см. _cursor_row)."""
    rows = list(scr.query(".set-voicerow"))[-2:]
    return [i for i, w in enumerate(rows) if "selected" in w.classes]


async def _expand_list(pilot):
    """Раскрыть список поля под курсором, если он не раскрылся сам.

    С канона 2026-08-20 списки панели раскрываются автоматически только на ПУСТОМ
    поле; там, где значение уже задано, раскрытие — часть сценария человека (`Alt+↓`),
    а не проверяемое поведение. Само правило стерегут параметризованные тесты в
    test_editors_voice_endpoint.py."""
    ctrl = getattr(pilot.app.screen, "_voice_list_ctrl", lambda: None)()
    if ctrl is not None and not ctrl.is_open:
        await pilot.press("alt+down")
        await pilot.pause()


_LIST_HINT_CLASSES = ("voice-url-preset-hint", "voice-model-hint", "voice-lang-hint")


def _field_labels(ed) -> list[str]:
    """Подписи полей редактора.

    Строки-подсказки списков носят тот же класс `onb-hint` и с канона 2026-08-20 стоят
    под полями с первого кадра — в перечень подписей они не входят."""
    return [str(w.render()) for w in ed.query(".onb-hint")
            if not any(c in w.classes for c in _LIST_HINT_CLASSES)]


async def _open_voice(app, scr, pilot):
    from tui.screens.settings import _VOICE
    await app.push_screen(scr)
    scr._open_section(_VOICE)
    await pilot.pause()


async def _install_local(pilot):
    """Установка батарейки: первый Enter спрашивает подтверждение, второй ставит
    (канон confirm секции — тот же, что у Del)."""
    await pilot.press("enter")
    await pilot.pause()
    await pilot.press("enter")


async def _open_cloud(app, scr, pilot):
    """Секция + курсор на «Своём endpoint»: поля endpoint показываются под его строкой.
    Дефолт-курсор зависит от действующего режима — доводим, только если он не там."""
    from tui.screens.settings import _VOICE_ROW_CLOUD
    await _open_voice(app, scr, pilot)
    if scr._voice_row != _VOICE_ROW_CLOUD:
        await pilot.press("down")
        await pilot.pause()


async def _open_local(app, scr, pilot):
    """То же для «Локального сервиса»."""
    from tui.screens.settings import _VOICE_ROW_LOCAL
    await _open_voice(app, scr, pilot)
    if scr._voice_row != _VOICE_ROW_LOCAL:
        await pilot.press("up")
        await pilot.pause()


async def _to_field(pilot, n=0, *, expand=True):
    """Курсор со строки режима на n-е поле её панели (0 — первое). Tab входит в ГРУППУ
    полей, стрелки двигают уже внутри неё (спека §4.1): сквозной ленты «строки → поля»
    больше нет, и одной стрелкой с режима в поле не попасть.

    `expand` — раскрыть список поля, если он не раскрылся сам. С канона 2026-08-20 он
    раскрывается автоматически только на ПУСТОМ поле, а большинство тестов писалось,
    когда вход раскрывал список всегда: чтобы их смысл не поехал, делаем то же, что
    человек, — жмём `Alt+↓`. Тесты про САМО правило раскрытия передают `expand=False`."""
    await pilot.press("tab")
    for _ in range(n):
        await pilot.press("down")
    await pilot.pause()
    if expand:
        await _expand_list(pilot)


# --- макет секции: строки режима со статусом, поля панели ---------------------

@pytest.mark.asyncio
async def test_menu_has_voice_section(tmp_path):
    from tui.app import TuiApp
    from tui.screens.settings import SECTIONS
    assert "Голосовой ввод" in SECTIONS
    app = TuiApp(config={"model": "M"})
    async with app.run_test():
        scr, _ = _mk_screen(tmp_path, OFF)
        await app.push_screen(scr)


@pytest.mark.asyncio
async def test_menu_preview_names_the_cloud_endpoint(tmp_path):
    """Превью «Голосовой ввод» в меню — сам адрес и модель, зеркально локальной строке
    («локальный сервис · модель»): родовое «свой endpoint» не говорило ничего, а
    строка «Подключение» рядом уже показывает полный URL (смок владельца 2026-08-07)."""
    from textual.widgets import Static
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, CLOUD, config_text=CLOUD_CONFIG)
        await app.push_screen(scr)
        await pilot.pause()
        body = " ".join(str(w.render()) for w in scr.query(Static))
        assert "https://old.x/v1/audio/transcriptions · whisper-old" in body
        assert "свой endpoint ·" not in body


@pytest.mark.asyncio
async def test_status_lives_in_the_local_row_without_a_duplicate_head(tmp_path):
    """Статус — приглушённый хвост строки режима, а не отдельная шапка: шапка
    дублировала бы и название режима, и модель (её показывает поле «Модель»)."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL)
        await _open_voice(app, scr, pilot)
        rows = _rows(scr)
        assert rows[0].startswith("▸ (●) Локальный сервис")
        assert "установлен · запущен · порт 8735" in rows[0]
        assert rows[1].startswith("  ( ) Свой endpoint")
        # единственная .set-info секции — заголовок «Режим:»; шапки статуса больше нет
        infos = [str(w.render()) for w in scr.query(".set-info")]
        assert infos == ["Режим:"]


@pytest.mark.asyncio
async def test_local_row_says_service_is_down(tmp_path):
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL_DEAD)
        await _open_voice(app, scr, pilot)
        assert "установлен · не запущен · порт 8735" in _rows(scr)[0]


@pytest.mark.asyncio
async def test_cloud_mode_marks_second_row(tmp_path):
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, CLOUD)
        await _open_voice(app, scr, pilot)
        rows = _rows(scr)
        assert "(●)" not in rows[0] and "( )" in rows[0]
        assert "(●) Свой endpoint" in rows[1]


@pytest.mark.asyncio
async def test_off_mode_marks_nothing(tmp_path):
    """Голос выключен — ни одной ●: экран не врёт, что режим уже выбран."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, OFF)
        await _open_voice(app, scr, pilot)
        assert "(●)" not in " ".join(_rows(scr))


@pytest.mark.asyncio
async def test_not_installed_row_carries_no_tail(tmp_path, monkeypatch):
    """Пока батарейки нет, статуса не существует — и хвоста нет: цену с именем модели
    говорит описание режима, и в строке они были дословным его повтором."""
    from tui.app import TuiApp
    _no_state(monkeypatch, tmp_path)
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, OFF)
        await _open_voice(app, scr, pilot)
        assert _rows(scr)[0].strip() == "▸ ( ) Локальный сервис"
        assert "faster-whisper-small" in _hint(scr)      # но в описании — на месте


@pytest.mark.asyncio
async def test_installed_row_keeps_its_status_tail(tmp_path):
    """У поставленной батарейки хвост — её статус, он ничего не дублирует."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL)
        await _open_voice(app, scr, pilot)
        assert "установлен · запущен · порт 8735" in _rows(scr)[0]


_LOCAL_HINT = ("Распознавание речи прямо на вашем компьютере: сервис speaches + модель "
               "faster-whisper-small (~0.9 ГБ на диске, ~1 ГБ RAM, CPU до 4 потоков "
               "при распознавании).")


@pytest.mark.asyncio
async def test_mode_descriptions_are_about_the_mode(tmp_path, monkeypatch):
    """Подсказка под строками объясняет РЕЖИМ (что это и чем платишь), а не клавиши.

    Ни примеров провайдеров, ни правила про пустой Token: первые — шум (адрес всё равно
    вводить свой), второе — про поле, и живёт подписью под ним (и только когда есть
    сохранённый токен, см. `test_token_hint_*`)."""
    from tui.app import TuiApp
    _no_state(monkeypatch, tmp_path)
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, OFF)
        await _open_voice(app, scr, pilot)
        assert _hint(scr) == _LOCAL_HINT
        await pilot.press("down")
        await pilot.pause()
        assert _hint(scr) == (
            "Распознавание речи на внешнем сервисе по OpenAI-совместимому API. "
            "Нужны адрес и, обычно, токен.")


@pytest.mark.asyncio
async def test_local_description_carries_no_keys(tmp_path, monkeypatch):
    """В описании режима клавиш нет — их держит футер (канон §6). «Удалить — Del.»
    висело здесь дубликатом футерной подсказки и жило по своему правилу."""
    from tui.app import TuiApp
    _no_state(monkeypatch, tmp_path)
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL)
        await _open_voice(app, scr, pilot)
        assert _hint(scr) == _LOCAL_HINT
        assert "Del" not in _hint(scr)


@pytest.mark.asyncio
async def test_air_under_mode_heading(tmp_path):
    """Воздух между заголовком «Режим:» и первой строкой — существующий класс отступа."""
    from tui.app import TuiApp
    from textual.widgets import Static
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL)
        await _open_voice(app, scr, pilot)
        head = scr.query_one(".set-info", Static)
        row = list(scr.query(".set-voicerow"))[0]
        assert row.region.y - head.region.y == 2      # строка заголовка + пустая


# --- поля показывают панель под курсором --------------------------------------

@pytest.mark.asyncio
async def test_cloud_pane_fields_prefilled_and_editable(tmp_path):
    """Курсор на «Своём endpoint» — поля из сырого config.yml, все редактируемые;
    token (секрет) не префиллится."""
    from tui.app import TuiApp
    from tui.editors.voice_endpoint import VoiceEndpointEditor
    from textual.widgets import Input, Static
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL,
                            config_text=CLOUD_CONFIG + "  language: en\n")
        await _open_cloud(app, scr, pilot)
        ed = scr.query_one(VoiceEndpointEditor)
        assert _field_labels(ed) == ["URL", "Токен", "Модель", "Язык"]
        assert scr.query_one("#voice-url", Input).value == "https://old.x/v1/audio/transcriptions"
        assert scr.query_one("#voice-model", Input).value == "whisper-old"
        assert scr.query_one("#voice-lang", Input).value == "en"
        assert scr.query_one("#voice-token", Input).value == ""
        assert all(not w.disabled for w in ed.query(Input))
        frame = scr.query_one("#set-frame")
        assert isinstance(frame.children[-1], Static)          # probe — под редактором
        assert "set-probe" in frame.children[-1].classes
        assert frame.children[-2] is ed


@pytest.mark.asyncio
async def test_local_pane_fields_are_facts_only_language_editable(tmp_path):
    """Курсор на «Локальном сервисе» — поля показывают сервис: адрес, «не требуется»
    вместо токена и модель. Править их нечем (правит установка), редактируется язык."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, LOCAL, config_text=CLOUD_CONFIG)
        fake.language = "en"
        await _open_voice(app, scr, pilot)
        assert scr.query_one("#voice-url", Input).value == _OURS
        assert scr.query_one("#voice-token", Input).value == "не требуется"
        # адрес не собираем сами — берём у владельца формата (voice_runtime.wire)
        from voice_runtime.wire import voice_wire_section
        assert scr.query_one("#voice-url", Input).value == \
            voice_wire_section(8735, "m")["url"]
        assert scr.query_one("#voice-model", Input).value == "Systran/faster-whisper-small"
        assert scr.query_one("#voice-lang", Input).value == "en"
        for wid in ("#voice-url", "#voice-token", "#voice-model"):
            assert scr.query_one(wid, Input).disabled is True, wid
        assert scr.query_one("#voice-lang", Input).disabled is False
        assert not scr.query(".set-probe")     # проверять нечего: адрес не вводится


@pytest.mark.asyncio
async def test_local_pane_without_battery_shows_install_hint_instead_of_fields(tmp_path):
    """Батарейки нет — показывать в полях нечего: вместо них строка с ценой действия."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        await _open_voice(app, scr, pilot)
        assert not scr.query(".set-framed")
        assert str(scr.query_one(".set-voiceinstall").render()) == "Enter — установить"


@pytest.mark.asyncio
async def test_cursor_starts_on_the_active_mode_row(tmp_path):
    """Вход в секцию — на строке ДЕЙСТВУЮЩЕГО режима: облачный юзер без батарейки иначе
    первым кадром видел бы «Enter — установить», а не свои настройки."""
    from tui.app import TuiApp
    from textual.widgets import Input
    from tui.screens.settings import _VOICE_ROW_CLOUD, _VOICE_ROW_LOCAL
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, CLOUD, config_text=CLOUD_CONFIG)
        await _open_voice(app, scr, pilot)
        assert (scr._voice_cursor_position(), _cursor_row(scr)) == (_VOICE_ROW_CLOUD, 1)
        assert scr.query_one("#voice-url", Input).value == \
            "https://old.x/v1/audio/transcriptions"      # видны СВОИ настройки
        app.pop_screen()
        await pilot.pause()

        for status in (LOCAL, OFF):
            scr2, _ = _mk_screen(tmp_path, status)
            await _open_voice(app, scr2, pilot)
            assert (scr2._voice_cursor_position(), _cursor_row(scr2)) == (_VOICE_ROW_LOCAL, 0)
            app.pop_screen()
            await pilot.pause()


@pytest.mark.asyncio
async def test_pane_follows_the_cursor_row(tmp_path):
    """Панель полей — про место под курсором, а не про включённый режим: ● стоит на
    локальном, а поля показывают endpoint, как только курсор встал на его строку."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL, config_text=CLOUD_CONFIG)
        await _open_voice(app, scr, pilot)
        assert scr.query_one("#voice-url", Input).value == _OURS
        await pilot.press("down")
        await pilot.pause()
        assert _rows(scr)[0].startswith("  (●)")          # ● не поехал
        assert scr.query_one("#voice-url", Input).value == "https://old.x/v1/audio/transcriptions"
        assert scr.query_one("#voice-url", Input).disabled is False
        await pilot.press("up")
        await pilot.pause()
        assert scr.query_one("#voice-url", Input).value == _OURS


@pytest.mark.asyncio
async def test_fields_entered_from_a_row_keep_its_pane(tmp_path):
    """Поля принадлежат строке, с которой в них вошли: спуск в поля панель не меняет."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL, config_text=CLOUD_CONFIG)
        await _open_voice(app, scr, pilot)
        await _to_field(pilot)                           # с локальной строки — в её поля
        assert _focused_id(scr) == "voice-lang"
        assert scr.query_one("#voice-url", Input).value == _OURS   # панель осталась локальной
        await pilot.press("escape")                      # свернуть список языков
        await pilot.press("tab")                         # обратно на строку режима
        await pilot.pause()
        assert (_cursor_row(scr), _selected_rows(scr)) == (0, [0])


# --- честность перекрытия ------------------------------------------------------

def _overlay(monkeypatch, tmp_path, managed=None, project=None):
    """СОДЕРЖИМОЕ слоёв поверх базового config.yml (пути изолирует автофикстура):
    managed — ~/.gc/config.local.yml, project — .gc/config.yml проекта."""
    if managed is not None:
        (tmp_path / "config.local.yml").write_text(managed, encoding="utf-8")
    if project is not None:
        import config as config_mod
        path = tmp_path / "project.yml"
        path.write_text(project, encoding="utf-8")
        monkeypatch.setattr(config_mod, "_project_local_config_path", lambda: str(path))


@pytest.mark.asyncio
async def test_no_override_warning_in_plain_local_mode(tmp_path, monkeypatch):
    """Локальный режим И РАБОТАЕТ тем, что managed-слой перекрывает config.yml —
    предупреждать здесь не о чем, ⚠ в штатном режиме кричал бы волком."""
    from tui.app import TuiApp
    _overlay(monkeypatch, tmp_path, managed=f"voice:\n  url: {_OURS}\n")
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL, config_text=CLOUD_CONFIG)
        await _open_voice(app, scr, pilot)
        assert not scr.query(".set-warnflash")


@pytest.mark.asyncio
async def test_override_warning_when_project_overlay_wins(tmp_path, monkeypatch):
    """Перекрыл не наш managed, а проектный .gc/config.yml — это неожиданно: поля
    показывают одно, а диктовка пойдёт на другое."""
    from tui.app import TuiApp
    from textual.widgets import Static
    _overlay(monkeypatch, tmp_path,
             project="voice:\n  url: https://proj.x/v1/audio/transcriptions\n")
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL, config_text=CLOUD_CONFIG)
        await _open_voice(app, scr, pilot)
        warns = [str(w.render()) for w in scr.query(".set-warnflash")]
        assert len(warns) == 1
        assert "⚠ действует https://proj.x/v1/audio/transcriptions" in warns[0]
        assert "перекрыто .gc/config.yml в проекте" in warns[0]
        node = scr.query_one(".set-warnflash", Static)
        assert "set-hint" in node.classes and "set-dyn" in node.classes   # канон, не свой стиль


@pytest.mark.asyncio
async def test_override_warning_when_managed_points_elsewhere(tmp_path, monkeypatch):
    """managed есть, но ведёт НЕ на наш локальный сервис (чужой порт/хост) — молчать
    нельзя: это уже не «так работает локальный режим»."""
    from tui.app import TuiApp
    _overlay(monkeypatch, tmp_path,
             managed="voice:\n  url: https://api.groq.com/openai/v1/audio/transcriptions\n")
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL, config_text=CLOUD_CONFIG)
        await _open_voice(app, scr, pilot)
        warns = [str(w.render()) for w in scr.query(".set-warnflash")]
        assert len(warns) == 1
        assert "перекрыто ~/.gc/config.local.yml" in warns[0]


@pytest.mark.asyncio
async def test_override_line_remounts_after_removal_in_one_tick(tmp_path, monkeypatch):
    """`remove()` у Textual отложенный: снесённая в этом же такте строка ещё в дереве,
    и обновление легло бы в труп — ⚠ не появился бы вовсе (канон `_singleton`)."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL, config_text=CLOUD_CONFIG)
        await _open_voice(app, scr, pilot)
        assert not scr.query(".set-voiceoverride")
        texts = ["⚠ первое", None, "⚠ третье"]
        monkeypatch.setattr(scr, "_voice_override_text", lambda: texts.pop(0), raising=False)
        scr._refresh_voice_override()          # смонтировать
        scr._refresh_voice_override()          # снести (удаление отложено)
        scr._refresh_voice_override()          # завести заново, а не оживлять обречённую
        await pilot.pause()
        assert [str(w.render()) for w in scr.query(".set-voiceoverride")] == ["⚠ третье"]


@pytest.mark.asyncio
async def test_no_override_hint_when_effective_matches_file(tmp_path, monkeypatch):
    from tui.app import TuiApp
    _overlay(monkeypatch, tmp_path, managed="model: m\n")   # оверлей есть, voice.* не трогает
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, CLOUD, config_text=CLOUD_CONFIG)
        await _open_voice(app, scr, pilot)
        assert not scr.query(".set-warnflash")


# --- placeholder токена --------------------------------------------------------

@pytest.mark.asyncio
async def test_token_placeholder_masks_the_saved_secret(tmp_path):
    """Сохранённый токен не префиллится (секрет), но и молчать нельзя: placeholder
    показывает его под канонной маской. voice.token — ГОЛЫЙ ключ (Groq, OpenAI), и
    маска заголовка на нём вырождалась в «***»: узнать свой ключ было невозможно."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, CLOUD, config_text=(
            "model: m\nvoice:\n  url: https://old.x/v1/audio/transcriptions\n"
            "  token: gsk_8xEXAMPLEabcdefghVZdf4\n"))
        await _open_cloud(app, scr, pilot)
        assert scr.query_one("#voice-token", Input).placeholder == "gsk_…Zdf4"
        app.pop_screen()
        await pilot.pause()

        scr2, _ = _mk_screen(tmp_path, CLOUD, config_text=CLOUD_CONFIG)
        await _open_cloud(app, scr2, pilot)
        assert scr2.query_one("#voice-token", Input).placeholder == "***"   # короткий не режем


@pytest.mark.asyncio
async def test_ctrl_u_clears_the_saved_voice_token(tmp_path):
    """Ctrl+U на «Токене» стирает СОХРАНЁННЫЙ секрет — как на «Подключении».

    До этой правки убрать голосовой токен через интерфейс было нечем: поле пустое,
    под ним маска, а пустое поле при сохранении значит «оставить как было» — секрет
    жил в config.yml, пока его не удалят руками. Футер обещает клавишу ровно там, где
    ей есть что делать."""
    from tui.app import TuiApp
    from textual.widgets import Input
    from tui.screens.settings import _VOICE_FOOTER_CLOUD_TOKEN
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, CLOUD, config_text=CLOUD_CONFIG)
        await _open_cloud(app, scr, pilot)
        await _to_field(pilot, 1, expand=False)          # курсор на «Токен»
        assert _focused_id(scr) == "voice-token"
        token = scr.query_one("#voice-token", Input)
        assert token.value == "" and token.placeholder == "***"   # маска сохранённого
        assert _footer(scr) == _VOICE_FOOTER_CLOUD_TOKEN

        await pilot.press("ctrl+u")
        await pilot.pause()
        assert token.placeholder == ""                    # стирать больше нечего
        assert _footer(scr) != _VOICE_FOOTER_CLOUD_TOKEN  # и клавишу больше не обещаем

        await pilot.press("enter")                        # сохранить форму
        await pilot.pause()
        assert fake.calls == ["wire_cloud"]
        assert fake.wired["token"] == "", "сохранённый токен обязан уйти из конфига"


@pytest.mark.asyncio
async def test_ctrl_u_on_a_plain_voice_field_is_not_ours(tmp_path):
    """На поле без сохранённого секрета клавиша не наша — она уходит полю ввода
    (штатное «удалить до начала строки»), а токен остаётся нетронутым."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, CLOUD, config_text=CLOUD_CONFIG)
        await _open_cloud(app, scr, pilot)
        await _to_field(pilot, expand=False)              # курсор на «URL»
        await pilot.press("ctrl+u")
        await pilot.pause()
        assert scr.query_one("#voice-token", Input).placeholder == "***"


@pytest.mark.asyncio
async def test_token_placeholder_shows_env_ref_as_is(tmp_path):
    """Ссылка ${ENV:VAR} секретом не является: маска врала бы «токен задан» там, где
    его может не быть (канон display_secret)."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, CLOUD, config_text=(
            "model: m\nvoice:\n  url: https://old.x/v1/audio/transcriptions\n"
            "  token: ${ENV:VOICE_TOKEN}\n"))
        await _open_cloud(app, scr, pilot)
        assert scr.query_one("#voice-token", Input).placeholder.startswith("${ENV:VOICE_TOKEN}")


@pytest.mark.asyncio
async def test_token_placeholder_empty_without_saved_secret(tmp_path):
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, OFF)
        await _open_cloud(app, scr, pilot)
        assert scr.query_one("#voice-token", Input).placeholder == ""


# --- контекстная подпись под полем Token ---------------------------------------

def _token_hint(scr):
    """Подпись ПОД полем Token (канон `_mount_field_hints`) или None. Подписи полей
    редактора несут `.onb-hint`, контекстная — `.set-hint`: по нему и различаем."""
    from textual.widgets import Static
    from tui.editors.voice_endpoint import VoiceEndpointEditor
    editors = list(scr.query(VoiceEndpointEditor))
    if not editors:
        return None
    children = list(editors[-1].children)
    for i, w in enumerate(children):
        if getattr(w, "id", None) != "voice-token":
            continue
        nxt = children[i + 1] if i + 1 < len(children) else None
        if isinstance(nxt, Static) and "set-hint" in nxt.classes:
            return nxt
    return None


@pytest.mark.asyncio
async def test_no_token_hint_under_the_field(tmp_path):
    """Подписи под Token нет: маска сохранённого значения в самом поле уже говорит,
    что там что-то есть, — вторая строка про то же была бы шумом."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, CLOUD, config_text=CLOUD_CONFIG)
        await _open_cloud(app, scr, pilot)
        assert _token_hint(scr) is None      # даже когда сохранённый токен есть


@pytest.mark.asyncio
async def test_no_token_hint_in_the_local_pane(tmp_path):
    """В локальной панели токена не существует («не требуется»), и сохранять там нечего —
    правило облачного поля туда не переезжает даже при заполненном voice.token."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL, config_text=CLOUD_CONFIG)
        await _open_local(app, scr, pilot)
        assert _token_hint(scr) is None


@pytest.mark.asyncio
async def test_cloud_field_placeholders_and_default_language(tmp_path):
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, OFF)
        await _open_cloud(app, scr, pilot)
        assert scr.query_one("#voice-url", Input).placeholder == \
            "https://…/v1/audio/transcriptions"
        assert scr.query_one("#voice-model", Input).placeholder == "whisper-1"
        assert scr.query_one("#voice-lang", Input).value == "ru"     # дефолт схемы


def _footer(scr):
    from textual.widgets import Static
    return str(scr.query_one("#set-footer", Static).render())


@pytest.mark.asyncio
async def test_outdated_engine_is_visible_and_offers_the_key(tmp_path):
    """Устаревший движок: строка говорит «· доступно …», футер обещает Ctrl+U.

    Раньше об устаревании знал только экран после апгрейда агента — в секции не было
    даже версии, и футер того экрана обещал /settings впустую. Язык взят у MCP-строки,
    где этот сценарий уже живёт («· доступно 0.3.1» + Ctrl+U)."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL_OUTDATED)
        await _open_local(app, scr, pilot)
        assert "доступно v0.9.0" in _rows(scr)[0]
        # Глагол — общий с MCP: «обновить» без уточнения в футерах запрещён
        # (test_no_footer_says_bare_update), Alt+↓ там же значит «перечитать список».
        assert "Ctrl+U — обновить версию" in _footer(scr)


@pytest.mark.asyncio
async def test_up_to_date_engine_promises_nothing(tmp_path):
    """Движок на версии сборки — ни пометки, ни клавиши: обещать Ctrl+U, который здесь
    ничего не делает, значит врать (канон секции)."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, LOCAL)
        await _open_local(app, scr, pilot)
        assert "доступно" not in _rows(scr)[0]
        assert "Ctrl+U" not in _footer(scr)
        await pilot.press("ctrl+u")
        await pilot.pause()
        assert "update_local" not in fake.calls      # клавиша молчит


@pytest.mark.asyncio
async def test_ctrl_u_updates_the_engine(tmp_path):
    """Ctrl+U на строке «Локальный сервис» обновляет движок действующим конфигом —
    им же потом поднимается сервис."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, LOCAL_OUTDATED)
        await _open_local(app, scr, pilot)
        await pilot.press("ctrl+u")
        await pilot.pause()
        assert fake.calls.count("update_local") == 1
        assert fake.update_config is scr._config
        assert "движок обновлён" in str(scr.query_one("#set-flash").render())


@pytest.mark.asyncio
async def test_ctrl_u_is_silent_on_the_cloud_row(tmp_path):
    """Клавиша принадлежит строке локального сервиса: на «Своём endpoint» — no-op,
    тем же правилом, что и Del."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, LOCAL_OUTDATED)
        await _open_cloud(app, scr, pilot)
        await pilot.press("ctrl+u")
        await pilot.pause()
        assert "update_local" not in fake.calls


@pytest.mark.asyncio
async def test_footer_lists_real_keys_of_the_cloud_pane(tmp_path):
    """Футер облачной панели перечисляет ровно те клавиши, что работают. Del сюда не
    входит: `_voice_request_remove` стреляет только на строке «Локальный сервис», и на
    «Своём endpoint» клавиша молчит — обещать её значит врать."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL)
        await _open_cloud(app, scr, pilot)
        for part in ("↑/↓", "Enter — сохранить и включить",
                     "Ctrl+Space — проверить", "PgUp/PgDn — секции", "Esc — меню"):
            assert part in _footer(scr)
        assert "Del" not in _footer(scr)


@pytest.mark.asyncio
async def test_footer_follows_the_pane_and_the_cursor(tmp_path):
    """Футер не имеет права обещать несуществующее: в локальной панели Ctrl+Space ничего
    не проверяет, а Enter на «Языке» сохраняет язык, а не включает режим."""
    from tui.app import TuiApp
    from tui.screens.settings import _VOICE_FOOTER_CLOUD
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL)
        await _open_local(app, scr, pilot)
        assert "Ctrl+Space — проверить" not in _footer(scr)
        assert "Enter — включить" in _footer(scr)
        await _to_field(pilot)                           # в поле «Язык» (список раскрылся)
        await pilot.press("escape")                      # свернуть — футер снова про место
        await pilot.pause()
        assert "Enter — сохранить язык" in _footer(scr)
        assert "Ctrl+Space — проверить" not in _footer(scr)
        await pilot.press("tab")                         # обратно на строку режима
        await pilot.press("up")                          # на «Свой endpoint»
        await pilot.pause()
        assert "Ctrl+Space — проверить" in _footer(scr)
        # в облаке Enter делает два дела разом — футер обещает ровно это
        assert "Enter — сохранить и включить" in _footer(scr)
        # на строке (не на «Языке») футер облака — ровно _VOICE_FOOTER_CLOUD, без хинта
        # Ctrl+Space (он только на самих полях со списком, см. _VOICE_FOOTER_CLOUD_LIST)
        assert _footer(scr) == _VOICE_FOOTER_CLOUD


@pytest.mark.asyncio
async def test_footer_turns_tab_around_when_the_cursor_enters_a_field(tmp_path):
    """Единственное, чем футер поля отличается от футера строки режима, — направление
    Tab: со строки он ведёт в поля, из поля — обратно к режиму. Без этого разворота
    вошедший в поля не знает, чем выйти: стрелки ходят только внутри группы.

    Поле — «Токен», не «URL»: у адреса теперь свой список пресетов (спека 2026-08-05
    §3), и его футер — про клавиши списка (`_VOICE_FOOTER_CLOUD_LIST`), а не про Tab;
    «Токен» списка не несёт и остаётся чистым примером обычного поля панели.

    Конфиг здесь БЕЗ сохранённого токена: с ним футер обещал бы ещё и `Ctrl+U`
    (`_VOICE_FOOTER_CLOUD_TOKEN`), а тест — про разворот Tab, не про очистку."""
    from tui.app import TuiApp
    from tui.screens.settings import (_TAB_TO_FIELDS, _TAB_TO_VARIANTS,
                                      _VOICE_FOOTER_CLOUD_FIELD)
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, CLOUD, config_text=(
            "model: m\nvoice:\n  url: https://old.x/v1/audio/transcriptions\n"
            "  model: whisper-old\n"))
        await _open_cloud(app, scr, pilot)
        assert _TAB_TO_FIELDS in _footer(scr)
        await _to_field(pilot, 1)                        # Tab → «URL», ↓ → «Токен»
        assert scr._voice_cursor_field() == "token"
        assert _footer(scr) == _VOICE_FOOTER_CLOUD_FIELD
        assert _TAB_TO_VARIANTS in _footer(scr) and _TAB_TO_FIELDS not in _footer(scr)
        await pilot.press("tab")                         # обратно на строку режима
        await pilot.pause()
        assert _TAB_TO_FIELDS in _footer(scr)


def test_voice_footers_say_the_right_thing_about_lists_and_del():
    """Что голосовые футеры обещают, когда места на всё не хватает.

    На поле «Язык» облачной панели в две строки укладываемся, выкинув «Del — удалить
    локальный сервис»: этот сегмент там и так лжёт — `_voice_request_remove` стреляет
    только на строке «Локальный сервис». Сама ширина стережётся отдельно, переносом на
    самом узком окне (`test_footer_contract.test_all_footers_fit_the_two_line_budget`)."""
    from tui.editors.field_list import LIST_CLOSED_HINT
    from tui.screens.settings import (
        _VOICE_FOOTER_CLOUD, _VOICE_FOOTER_CLOUD_LIST, _VOICE_FOOTER_LOCAL_FRESH)
    assert LIST_CLOSED_HINT in _VOICE_FOOTER_CLOUD_LIST
    for footer in (_VOICE_FOOTER_CLOUD, _VOICE_FOOTER_CLOUD_LIST, _VOICE_FOOTER_LOCAL_FRESH):
        assert "Del" not in footer          # облако + непоставленный сервис: сносить нечего


@pytest.mark.asyncio
async def test_escape_after_a_pick_does_not_undo_the_saved_language(tmp_path):
    """Enter зафиксировал язык — Esc после следующей печати откатывает к НЕМУ, а не к
    значению входа на поле: иначе доверчивый Enter записал бы старый код поверх только
    что сохранённого."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, LOCAL)          # действующий язык — ru
        await _open_local(app, scr, pilot)
        await _to_field(pilot)                           # в поле «Язык», список раскрыт
        scr.query_one("#voice-lang", Input).value = ""
        await pilot.pause()
        await _type(pilot, "англ")                       # фильтр → en
        await pilot.press("enter")                       # подставили и сохранили
        await pilot.pause()
        assert fake.language == "en"
        assert scr.query_one("#voice-lang", Input).value == "en"
        await _type(pilot, "k")                          # печать раскрывает список заново
        await pilot.press("escape")                      # откат фильтра
        await pilot.pause()
        assert scr.query_one("#voice-lang", Input).value == "en"
        await pilot.press("enter")                       # доверчивый Enter — язык тот же
        await pilot.pause()
        assert fake.language == "en"


@pytest.mark.asyncio
async def test_footer_follows_a_mouse_click_into_the_field(tmp_path):
    """Клик мышью — тоже перемещение по ленте: футер обязан догнать его так же,
    как курсор и подсказка (клик в «Язык» раскрывает список — футер про его клавиши)."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL)
        await _open_local(app, scr, pilot)
        scr.query_one("#voice-lang", Input).focus()
        await pilot.pause()
        await _expand_list(pilot)                         # код задан — раскрываем Alt+↓
        assert "Enter — включить" not in _footer(scr)     # футер строки режима ушёл
        assert "Esc — свернуть" in _footer(scr)
        await pilot.press("escape")
        await pilot.pause()
        assert "Enter — сохранить язык" in _footer(scr)


# --- снимок статуса: один на кадр -------------------------------------------

@pytest.mark.asyncio
async def test_frame_takes_a_single_status_snapshot(tmp_path):
    """Снимок статуса подключается к порту сервиса (таймаут 0.5 с), а за кадр его
    спрашивают и строка меню, и её глиф, и тело секции. Пока сервис лежит, это копило
    задержку на КАЖДОЕ нажатие — кадр обязан обходиться одним снимком."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, LOCAL_DEAD)
        await app.push_screen(scr)
        await pilot.pause()
        scr._invalidate_voice_snapshot()
        fake.snapshots = 0
        scr._render_view()                               # два полных рендера меню
        scr._render_view()
        await pilot.pause()
        assert fake.snapshots == 1


@pytest.mark.asyncio
async def test_status_snapshot_refreshed_after_an_action(tmp_path):
    """Кэш — про кадр, а не про состояние: действие меняет статус, и секция обязана
    показать новый, а не протухший."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, LOCAL_DEAD)
        await _open_voice(app, scr, pilot)
        assert "не запущен" in _rows(scr)[0]
        before = fake.snapshots
        fake.status = LOCAL                              # сервис поднялся
        scr._refresh_voice_section()                     # так же зовёт колбэк подъёма
        await pilot.pause()
        assert fake.snapshots > before
        assert "установлен · запущен · порт 8735" in _rows(scr)[0]


@pytest.mark.asyncio
async def test_status_snapshot_refreshed_after_remove(tmp_path):
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, LOCAL)

        def _remove():
            fake.calls.append("remove_local")
            fake.status = OFF
            return True
        fake.remove_local = _remove
        await _open_voice(app, scr, pilot)
        await pilot.press("delete")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert "установлен" not in _rows(scr)[0]         # статус перечитан, а не из кэша


# --- навигация курсора и фокус ----------------------------------------------

@pytest.mark.asyncio
async def test_mark_does_not_follow_the_cursor(tmp_path):
    """● не едет за курсором (метка — про конфиг, курсор — про место): действует
    локальный режим, а курсор стоит на облачной строке и спускается в её поля."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL)
        await _open_voice(app, scr, pilot)
        await pilot.press("down")
        await pilot.pause()
        rows = _rows(scr)
        assert rows[0].startswith("  (●)") and rows[1].startswith("▸ ( )")
        await _to_field(pilot)                          # в поля
        rows = _rows(scr)
        assert rows[0].startswith("  (●)") and rows[1].startswith("▸ ( )")


@pytest.mark.asyncio
async def test_row_states_marker_highlight_and_neither(tmp_path):
    """Три состояния строки режима: курсор НА ней — ▸ и фоновая подсветка; курсор в ЕЁ
    полях — ▸ остаётся (иначе не видно, чьи это поля), подсветка снята; чужая строка —
    ни ▸, ни подсветки."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        await _open_cloud(app, scr, pilot)
        assert (_cursor_row(scr), _selected_rows(scr)) == (1, [1])
        assert _rows(scr)[0].startswith("  ")            # чужая строка — без ▸
        await _to_field(pilot)                           # в поле URL облачной панели
        assert _focused_id(scr) == "voice-url"
        assert (_cursor_row(scr), _selected_rows(scr)) == (1, [])
        assert _rows(scr)[0].startswith("  ")


@pytest.mark.asyncio
async def test_owner_marker_moves_with_the_pane(tmp_path):
    """▸ принадлежит панели: вернулись из полей на строку — подсветка вернулась, ушли на
    соседнюю строку — ▸ переехал вместе с полями."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL, config_text=CLOUD_CONFIG)
        await _open_cloud(app, scr, pilot)
        await _to_field(pilot)                           # в поля облачной панели
        assert (_cursor_row(scr), _selected_rows(scr)) == (1, [])
        await pilot.press("tab")                         # назад на её строку
        await pilot.pause()
        assert (_cursor_row(scr), _selected_rows(scr)) == (1, [1])
        await pilot.press("up")                          # на «Локальный сервис»
        await pilot.pause()
        assert (_cursor_row(scr), _selected_rows(scr)) == (0, [0])
        await _to_field(pilot)                           # в поле «Язык» локальной панели
        assert _focused_id(scr) == "voice-lang"
        assert (_cursor_row(scr), _selected_rows(scr)) == (0, [])


@pytest.mark.asyncio
async def test_arrows_walk_the_rows_then_tab_walks_all_cloud_fields(tmp_path):
    """Курсор ходит группами (спека §4.1): стрелки — по строкам режима, Tab — в поля
    панели, и там стрелки идут уже по полям, циклом. На строках — блюр, в полях — фокус
    поля (индикатор места — фокус-рамка, ▸ держится на строке-владельце); с последнего
    поля ↓ возвращается на первое (сперва Esc: на «Языке» раскрыт список, и стрелки
    принадлежат ему)."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, OFF)
        await _open_voice(app, scr, pilot)
        assert (_cursor_row(scr), _focused_id(scr)) == (0, None)
        await pilot.press("down")
        await pilot.pause()
        assert (_cursor_row(scr), _focused_id(scr)) == (1, None)
        await pilot.press("tab")
        await pilot.pause()
        assert (_cursor_row(scr), _focused_id(scr)) == (1, "voice-url")
        await pilot.press("escape")                      # пустой адрес раскрыл список пресетов — свернуть
        for focus in ("voice-token", "voice-model", "voice-lang"):
            await pilot.press("down")
            await pilot.pause()
            assert (_cursor_row(scr), _focused_id(scr)) == (1, focus)
        # Список языка сам не раскрылся — код в поле уже есть (канон 2026-08-20), и
        # сворачивать перед следующей стрелкой нечего.
        await pilot.press("down")
        await pilot.pause()
        assert (_cursor_row(scr), _focused_id(scr)) == (1, "voice-url")


@pytest.mark.asyncio
async def test_arrows_walk_the_cloud_fields_back_up(tmp_path):
    """Свой (не пресетный) адрес в поле заранее — иначе пустое поле URL раскрывало бы
    список пресетов на КАЖДЫЙ вход (канон §4.2.3: «вход на поле — всегда раскрыть»), и
    простая ходьба стрелками по группе была бы не про неё, а про список."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, OFF)
        await _open_cloud(app, scr, pilot)
        scr.query_one("#voice-url", Input).value = _URL
        await _to_field(pilot, 3, expand=False)          # до последнего поля группы
        assert _focused_id(scr) == "voice-lang"
        # Список языка сам не раскрылся (код в поле есть) — сворачивать нечего.
        for focus in ("voice-model", "voice-token", "voice-url", "voice-lang"):
            await pilot.press("up")
            await pilot.pause()
            assert (_cursor_row(scr), _focused_id(scr)) == (1, focus)
        await pilot.press("tab")                         # из полей — на строку режима
        await pilot.pause()
        assert (_cursor_row(scr), _focused_id(scr)) == (1, None)


@pytest.mark.asyncio
async def test_local_ribbon_without_battery_is_rows_only(tmp_path):
    """Полей у ненастановленного локального режима нет — и лента в нём короткая:
    останавливать курсор не на чем."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, OFF)
        await _open_voice(app, scr, pilot)
        await pilot.press("up")                          # ↑ с первой строки — в конец ленты
        await pilot.pause()
        assert (_cursor_row(scr), _focused_id(scr)) == (1, None)


@pytest.mark.asyncio
async def test_local_fields_group_stops_only_on_the_editable_field(tmp_path):
    """В локальной панели курсор останавливается только на «Языке»: остальные поля
    показывают факты сервиса, править их нечем — остановка была бы тупиком. Группа из
    одного поля: стрелка внутри неё никуда не ведёт, выход — Tab."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL)
        await _open_voice(app, scr, pilot)
        await _to_field(pilot)
        assert (_cursor_row(scr), _focused_id(scr)) == (0, "voice-lang")   # ▸ — у владельца
        await pilot.press("escape")                      # свернуть раскрытый список языков
        await pilot.press("up")
        await pilot.pause()
        assert (_cursor_row(scr), _focused_id(scr)) == (0, "voice-lang")   # поле одно
        await pilot.press("tab")
        await pilot.press("up")
        await pilot.pause()
        assert (_cursor_row(scr), _focused_id(scr)) == (1, None)      # строка «Свой endpoint»


@pytest.mark.asyncio
async def test_cloud_row_itself_does_not_grab_the_field(tmp_path):
    """«Свой endpoint» — строка режима, а не вход в поля: фокус там снят, иначе строка и
    поле URL были бы неразличимы, а Tab с неё «ничего не делал бы»."""
    from tui.app import TuiApp
    from tui.screens.settings import _VOICE_ROW_CLOUD
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, OFF)
        await _open_cloud(app, scr, pilot)
        assert scr._voice_cursor_position() == _VOICE_ROW_CLOUD
        assert scr.focused is None
        assert _cursor_row(scr) == 1


@pytest.mark.asyncio
async def test_reentering_section_starts_from_the_default_cursor(tmp_path):
    """Повторный вход в секцию ставит курсор на дефолт — строку действующего режима.

    Память места между заходами была исключением одной этой секции (живой смок
    2026-08-20): возврат попадал сразу в поле «Модель» или «Язык» и раскрывал там
    список, которого человек не просил. Остальные секции хаба входят с дефолта, и
    голос теперь тоже — правило одно на экран."""
    from tui.app import TuiApp
    from tui.screens.settings import _MODES, _VOICE, _VOICE_GROUP_VARIANTS
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, OFF)
        await _open_voice(app, scr, pilot)
        await pilot.press("down")                        # на «Свой endpoint»
        await pilot.press("tab")                         # «URL» — пустой адрес раскрыл пресеты
        await pilot.press("escape")                       # свернуть — стрелки снова про поля
        await pilot.press("down")                         # token
        await pilot.press("down")                         # model
        await pilot.pause()
        assert _focused_id(scr) == "voice-model"
        await pilot.press("pagedown")
        await pilot.pause()
        assert scr._view == _MODES
        await pilot.press("pageup")
        await pilot.pause()
        assert scr._view == _VOICE
        assert _focused_id(scr) is None                  # курсор в строках режима
        assert scr._voice_cursor_group() == _VOICE_GROUP_VARIANTS
        assert not _model_rows(scr) and not _lang_rows(scr)   # и ни один список не раскрыт
        assert app.is_running


@pytest.mark.asyncio
async def test_mouse_click_into_field_moves_the_cursor(tmp_path):
    """Клик мышью — тоже перемещение курсора: он подтягивается к полю, и следующая
    стрелка идёт от него, а не из «прошлого» места (пин механизма on_descendant_focus)."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, OFF)
        await _open_cloud(app, scr, pilot)
        scr.query_one("#voice-lang", Input).focus()       # эмуляция клика по полю
        await pilot.pause()
        await _expand_list(pilot)                          # код задан — раскрываем Alt+↓
        assert (scr._voice_cursor_group(), scr._voice_cursor_field()) == ("fields", "language")
        assert (_cursor_row(scr), _selected_rows(scr)) == (1, [])   # ▸ у владельца, фон снят
        await pilot.press("escape")                       # свернуть раскрытый список
        await pilot.press("up")
        await pilot.pause()
        assert _focused_id(scr) == "voice-model"


@pytest.mark.asyncio
async def test_enter_from_any_cloud_field_saves_endpoint(tmp_path):
    """Enter из любого поля облачной панели — сохранить+включить (не только из URL)."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        for steps, field in ((1, "voice-token"), (3, "voice-lang")):
            scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
            await _open_cloud(app, scr, pilot)
            await _to_field(pilot, steps)
            assert _focused_id(scr) == field
            await pilot.press("enter")
            await pilot.pause()
            assert fake.calls == ["wire_cloud"], field
            assert fake.wired["url"] == "https://old.x/v1/audio/transcriptions"
            app.pop_screen()
            await pilot.pause()


@pytest.mark.asyncio
async def test_walking_the_groups_keeps_all_drafts(tmp_path):
    """Ходьба по группам не теряет набранное: смена панели пересобирает поля, но черновик
    endpoint принадлежит человеку — он возвращается на место."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, LOCAL)
        await _open_cloud(app, scr, pilot)
        scr.query_one("#voice-url", Input).value = _URL
        scr.query_one("#voice-token", Input).value = "sk-draft"
        scr.query_one("#voice-model", Input).value = "wh-draft"
        await _to_field(pilot, 3)                        # по полям — до «Языка»
        await pilot.press("escape")                      # там раскрыт список: вернуть стрелки полям
        await pilot.press("tab")                         # из полей — на строку режима
        await pilot.press("up")                          # круг через локальную панель…
        await pilot.press("down")                        # …и обратно в облачную
        await _to_field(pilot)                           # снова в поля
        assert scr.query_one("#voice-url", Input).value == _URL
        assert scr.query_one("#voice-token", Input).value == "sk-draft"
        assert scr.query_one("#voice-model", Input).value == "wh-draft"


@pytest.mark.asyncio
async def test_draft_survives_a_trip_to_the_local_row(tmp_path):
    """Заглянуть в локальный режим и вернуться — набранный endpoint на месте."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL)
        await _open_cloud(app, scr, pilot)
        scr.query_one("#voice-url", Input).value = "https://draft.x/v1/audio/transcriptions"
        await pilot.press("up")                          # на «Локальный сервис» — поля другие
        await pilot.pause()
        assert scr.query_one("#voice-url", Input).value == _OURS
        await pilot.press("down")
        await pilot.pause()
        assert scr.query_one("#voice-url", Input).value == \
            "https://draft.x/v1/audio/transcriptions"


@pytest.mark.asyncio
async def test_pagedown_from_live_cloud_fields_not_stuck(tmp_path):
    """Регресс 9c0a405b: PageDown прямо из живых полей оставлял app.focused на удалённом
    Input — соседние секции фокус не перехватывают, и PgUp/PgDn переставали работать."""
    from tui.app import TuiApp
    from tui.screens.settings import _MODES, _VOICE
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, OFF)
        await _open_cloud(app, scr, pilot)
        await _to_field(pilot)                           # в поля: фокус в #voice-url
        assert _focused_id(scr) == "voice-url"
        await pilot.press("pagedown")
        await pilot.pause()
        assert scr._view == _MODES
        await pilot.press("pageup")
        await pilot.pause()
        assert scr._view == _VOICE
        await pilot.press("pagedown")
        await pilot.pause()
        assert scr._view == _MODES


# --- Enter: включение режимов ------------------------------------------------

@pytest.mark.asyncio
async def test_enter_on_local_installs_then_reloads_then_starts_service(tmp_path, monkeypatch):
    """Батарейки нет: Enter ставит её (под suspend), затем hot-reload и подъём сервиса
    УЖЕ по перечитанному конфигу (гейт автостарта смотрит именно туда)."""
    from tui.app import TuiApp
    import voice_runtime.autostart as autostart
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        seen = []
        monkeypatch.setattr(autostart, "ensure_voice_service",
                            lambda cfg: seen.append(cfg) or True)
        new_cfg = {"voice": {"url": _OURS}}

        def _apply():
            app._config = new_cfg                        # так же, как reload_config
        scr, fake = _mk_screen(tmp_path, OFF, on_apply=_apply)
        await _open_voice(app, scr, pilot)
        await _install_local(pilot)
        for _ in range(60):
            await pilot.pause()
            if seen:
                break
        assert fake.calls == ["switch_to_local"]
        assert seen == [new_cfg]                         # конфиг ПОСЛЕ reload


@pytest.mark.asyncio
async def test_enter_on_local_when_installed_but_cloud_switches_back(tmp_path, monkeypatch):
    """Батарейка есть, режим — свой endpoint: Enter возвращает локальный (wire+reload),
    без второй установки."""
    from tui.app import TuiApp
    import voice_runtime.autostart as autostart
    # Конфиг с языком — у ПРИЛОЖЕНИЯ: при входе в секцию экран берёт базу оттуда
    # (`_sync_config_from_app`), как и в проде, где /settings строится из того же
    # объекта. Раньше носителем был конфиг экрана, и тест сверял словари целиком.
    app = TuiApp(config={"model": "M", "voice": {"language": "en"}})
    async with app.run_test() as pilot:
        monkeypatch.setattr(autostart, "ensure_voice_service", lambda cfg: True)
        applied = []
        scr, fake = _mk_screen(tmp_path, LOCAL_INSTALLED_CLOUD,
                               on_apply=lambda: applied.append("apply"))
        await _open_local(app, scr, pilot)
        await pilot.press("enter")
        await pilot.pause()
        assert fake.calls == ["switch_to_local"]
        assert applied == ["apply"]
        # Действующий конфиг доезжает до фасада: из него берётся язык, когда
        # managed-секции уже нет — её снёс переезд на свой endpoint. Проверяем именно
        # это, а не совпадение словарей: чей объект доедет — деталь реализации.
        assert (fake.switch_config or {}).get("voice", {}).get("language") == "en"


@pytest.mark.asyncio
async def test_switching_to_installed_local_does_not_leave_the_tui(tmp_path, monkeypatch):
    """Включение УЖЕ установленной батарейки — это запись секции в config.local.yml:
    мгновенно и молча. Нырять за этим в терминал незачем: на живом смоке каждое
    переключение режима мигало alt-screen'ом и добавляло строку «Включаю…»."""
    from tui.app import TuiApp
    import voice_runtime.autostart as autostart
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        monkeypatch.setattr(autostart, "ensure_voice_service", lambda cfg: True)
        log = []
        scr, fake = _mk_screen(tmp_path, LOCAL_INSTALLED_CLOUD)

        def _switch(echo=print, config=None):
            fake.calls.append("switch_to_local")
            fake.status = LOCAL                          # режим переключился
            return True
        fake.switch_to_local = _switch
        app.suspend = _FakeSuspend(log)
        await _open_local(app, scr, pilot)
        await pilot.press("enter")
        await pilot.pause()
        assert log == []                                 # в терминал не выходили
        assert fake.calls == ["switch_to_local"]
        assert _rows(scr)[0].startswith("▸ (●)")          # ● переехал на локальный


@pytest.mark.asyncio
async def test_install_still_runs_under_suspend(tmp_path):
    """Установка — единственный шумный шаг (клон + uv sync + модель ~0.5 ГБ): её вывод
    человек обязан видеть, и она остаётся под suspend."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        log = []
        scr, fake = _mk_screen(tmp_path, OFF)
        app.suspend = _FakeSuspend(log)
        await _open_voice(app, scr, pilot)
        await _install_local(pilot)
        await pilot.pause()
        assert log == ["enter", "exit"]
        assert fake.calls == ["switch_to_local"]


@pytest.mark.asyncio
async def test_enter_on_local_with_dead_service_starts_it(tmp_path, monkeypatch):
    """Режим локальный, а сервис лежит — Enter обязан поднять его: иначе строка тупик
    (переключать нечего, «уже включён» врёт, поднять из UI больше нечем)."""
    from tui.app import TuiApp
    import voice_runtime.autostart as autostart
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        seen = []
        monkeypatch.setattr(autostart, "ensure_voice_service",
                            lambda cfg: seen.append(cfg) or True)
        scr, fake = _mk_screen(tmp_path, LOCAL_DEAD)
        await _open_voice(app, scr, pilot)
        await pilot.press("enter")
        for _ in range(60):
            await pilot.pause()
            if seen:
                break
        assert seen, "воркер подъёма не стартовал"
        assert fake.calls == []                          # ничего не переустанавливаем


def _type_draft(scr):
    """Черновик во всех четырёх полях endpoint (набран, но не сохранён)."""
    from textual.widgets import Input
    draft = {"voice-url": "https://draft.x/v1/audio/transcriptions", "voice-token": "sk-draft",
             "voice-model": "wh-draft", "voice-lang": "en"}
    for wid, value in draft.items():
        scr.query_one(f"#{wid}", Input).value = value
    return draft


def _assert_draft(scr, draft):
    from textual.widgets import Input
    for wid, value in draft.items():
        assert scr.query_one(f"#{wid}", Input).value == value, wid


@pytest.mark.asyncio
async def test_install_keeps_endpoint_draft(tmp_path, monkeypatch):
    """Набрал endpoint, передумал, поставил локальный сервис — черновик не должен
    исчезнуть: у установки своя панель, а набранное принадлежит человеку."""
    from tui.app import TuiApp
    import voice_runtime.autostart as autostart
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        monkeypatch.setattr(autostart, "ensure_voice_service", lambda cfg: True)
        _overlay(monkeypatch, tmp_path)                  # managed-оверлея ещё нет
        scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)

        def _switch(echo=print, config=None):
            fake.calls.append("switch_to_local")
            fake.status = LOCAL                          # батарейка появилась и включилась
            (tmp_path / "config.local.yml").write_text(   # ...и перекрыла voice.url
                f"voice:\n  url: {_OURS}\n", encoding="utf-8")
            return True
        fake.switch_to_local = _switch
        await _open_cloud(app, scr, pilot)
        assert not scr.query(".set-warnflash")
        draft = _type_draft(scr)
        await pilot.press("up")                          # на «Локальный сервис»
        await _install_local(pilot)
        await pilot.pause()
        assert fake.calls == ["switch_to_local"]
        assert _rows(scr)[0].startswith("▸ (●)")          # ● переехал на локальный
        assert "установлен · запущен · порт 8735" in _rows(scr)[0]
        assert not scr.query(".set-warnflash")            # штатный локальный режим — молчим
        await pilot.press("down")                        # вернулись к своему endpoint
        await pilot.pause()
        _assert_draft(scr, draft)


@pytest.mark.asyncio
async def test_remove_drops_the_fields_with_the_battery(tmp_path):
    """Снос сервиса убирает и его поля: показывать факты несуществующего сервиса нечестно."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, LOCAL)

        def _remove():
            fake.calls.append("remove_local")
            fake.status = OFF
            return True
        fake.remove_local = _remove
        await _open_voice(app, scr, pilot)
        await pilot.press("delete")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert fake.calls == ["remove_local"]
        assert not scr.query(".set-framed")               # поля ушли вместе с батарейкой
        assert "установлен" not in _rows(scr)[0]          # батарейки нет — и хвоста нет
        assert "(●)" not in " ".join(_rows(scr))          # режим больше не локальный
        assert "Enter — удалить" not in _hint(scr)        # confirm-баннер снят


@pytest.mark.asyncio
async def test_enter_on_already_local_is_noop_with_note(tmp_path):
    """Режим уже локальный: Enter ничего не переключает, говорит об этом заметкой."""
    from tui.app import TuiApp
    from textual.widgets import Static
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, LOCAL)
        await _open_voice(app, scr, pilot)
        await pilot.press("enter")
        await pilot.pause()
        assert fake.calls == []
        assert "уже включён" in str(scr.query_one("#set-title", Static).render())


@pytest.mark.asyncio
async def test_enter_on_cloud_row_saves_and_reloads(tmp_path):
    """Enter на строке «Свой endpoint» — это и есть включение режима: запись + hot-reload."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        applied = []
        scr, fake = _mk_screen(tmp_path, OFF, on_apply=lambda: applied.append("apply"))
        await _open_cloud(app, scr, pilot)
        scr.query_one("#voice-url", Input).value = _URL
        scr.query_one("#voice-token", Input).value = "sk"
        scr.query_one("#voice-model", Input).value = "wh"
        await pilot.press("enter")
        await pilot.pause()
        assert fake.wired == dict(url=_URL, token="sk", model="wh", language="ru")
        assert applied == ["apply"]


@pytest.mark.asyncio
async def test_cloud_save_blocked_on_a_readonly_config(tmp_path, monkeypatch):
    """Установка из пакета: config.yml — шаблон, писать в него нельзя. Остальные секции
    в этом случае объясняются, а голосовая шла писать вслепую и ловила только ValueError:
    прилетавший из файловой системы OSError ронял приложение."""
    from tui.app import TuiApp
    from textual.widgets import Input, Static
    import config as config_mod

    def _readonly(*a, **kw):
        raise PermissionError("Permission denied")

    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF)
        monkeypatch.setattr(config_mod, "_default_config_path", lambda: scr._config_path)
        fake.wire_cloud = _readonly
        await _open_cloud(app, scr, pilot)
        scr.query_one("#voice-url", Input).value = _URL
        scr.query_one("#voice-token", Input).value = "sk"
        scr.query_one("#voice-model", Input).value = "whisper-1"
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is scr                     # приложение живо
        notice = " ".join(str(w.render()) for w in scr.query(Static))
        assert "недоступен для записи" in notice


@pytest.mark.asyncio
async def test_cloud_save_survives_a_filesystem_refusal(tmp_path):
    """Конфиг писуемый, но файловая система отказала (нет прав, диск полон): экран
    обязан объясниться плашкой, а не уронить приложение трейсбеком."""
    from tui.app import TuiApp
    from textual.widgets import Input, Static

    def _refuse(*a, **kw):
        raise OSError("No space left on device")

    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF)
        fake.wire_cloud = _refuse
        await _open_cloud(app, scr, pilot)
        scr.query_one("#voice-url", Input).value = _URL
        scr.query_one("#voice-token", Input).value = "sk"
        scr.query_one("#voice-model", Input).value = "whisper-1"
        await pilot.press("enter")
        await pilot.pause()
        assert app.screen is scr
        notice = " ".join(str(w.render()) for w in scr.query(Static))
        assert "No space left on device" in notice


@pytest.mark.asyncio
async def test_enter_on_cloud_without_url_shows_error_and_focuses_url(tmp_path):
    """Пустой URL: не сохраняем, ошибка — внутри редактора (канон EndpointEditor),
    фокус уходит в URL — правят там же, где сказано."""
    from tui.app import TuiApp
    from tui.editors.voice_endpoint import VoiceEndpointEditor
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF)
        await _open_cloud(app, scr, pilot)
        await pilot.press("enter")                       # URL пуст
        await pilot.pause()
        assert fake.calls == []
        ed = scr.query_one(VoiceEndpointEditor)
        assert any("введите адрес" in str(w.render()) for w in ed.query(".onb-warn"))
        assert scr.focused is scr.query_one("#voice-url", Input)


@pytest.mark.asyncio
async def test_cloud_save_keeps_old_token_when_field_left_empty(tmp_path):
    """Пустое поле token = «не менять» (канон «Подключения»), а не затереть сохранённый."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        await _open_cloud(app, scr, pilot)
        await pilot.press("enter")                       # token не тронут
        await pilot.pause()
        assert fake.wired["token"] == "old-token"
        assert fake.wired["url"] == "https://old.x/v1/audio/transcriptions"


@pytest.mark.asyncio
async def test_cloud_save_language_empty_falls_back_to_ru(tmp_path):
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF,
                               config_text=CLOUD_CONFIG + "  language: en\n")
        await _open_cloud(app, scr, pilot)
        assert scr.query_one("#voice-lang", Input).value == "en"
        scr.query_one("#voice-lang", Input).value = ""
        await pilot.press("enter")
        await pilot.pause()
        assert fake.wired["language"] == "ru"


@pytest.mark.asyncio
async def test_cloud_save_moves_the_mark(tmp_path):
    """После сохранения ● переезжает на «Свой endpoint» — секция показывает факт конфига."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF)

        def _wire(path, url, token, model, language="ru"):
            fake.calls.append("wire_cloud")
            fake.status = CLOUD                          # конфиг стал облачным
        fake.wire_cloud = _wire
        await _open_cloud(app, scr, pilot)
        scr.query_one("#voice-url", Input).value = _URL
        scr.query_one("#voice-model", Input).value = "whisper-1"
        await pilot.press("enter")
        await pilot.pause()
        assert "(●) Свой endpoint" in _rows(scr)[1]


# --- Enter на «Языке» локального режима ---------------------------------------

@pytest.mark.asyncio
async def test_enter_on_local_language_saves_it_to_managed(tmp_path):
    """Язык локального режима правится здесь и ложится в managed-слой (его пишем мы),
    дальше — тот же hot-reload, что у облачного сохранения."""
    from tui.app import TuiApp
    from textual.widgets import Input, Static
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        applied = []
        scr, fake = _mk_screen(tmp_path, LOCAL, on_apply=lambda: applied.append("apply"))
        await _open_voice(app, scr, pilot)
        await _to_field(pilot)                           # в единственное правимое поле
        assert _focused_id(scr) == "voice-lang"
        scr.query_one("#voice-lang", Input).value = "en"
        await pilot.press("enter")
        await pilot.pause()
        assert fake.calls == ["set_local_language"]
        assert fake.language == "en"
        assert applied == ["apply"]
        assert "en" in str(scr.query_one("#set-flash", Static).render())


@pytest.mark.asyncio
async def test_local_language_save_does_not_touch_the_endpoint(tmp_path):
    """Сохранение языка — не сохранение endpoint'а: config.yml юзера не трогаем."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, LOCAL, config_text=CLOUD_CONFIG)
        await _open_voice(app, scr, pilot)
        await _to_field(pilot)
        scr.query_one("#voice-lang", Input).value = "de"
        await pilot.press("enter")
        await pilot.pause()
        assert "wire_cloud" not in fake.calls
        assert (tmp_path / "config.yml").read_text(encoding="utf-8") == CLOUD_CONFIG


@pytest.mark.asyncio
async def test_local_language_empty_falls_back_to_ru(tmp_path):
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, LOCAL)
        await _open_voice(app, scr, pilot)
        await _to_field(pilot)
        scr.query_one("#voice-lang", Input).value = ""
        await pilot.press("enter")
        await pilot.pause()
        assert fake.language == "ru"


# --- выпадающий список языка ---------------------------------------------------

def _lang_widgets(scr):
    """Строки списка ТЕКУЩЕГО редактора (у прошлых рендеров свой, обречённый)."""
    from tui.editors.voice_endpoint import VoiceEndpointEditor
    editors = list(scr.query(VoiceEndpointEditor))
    return list(editors[-1].query(".voice-lang-row")) if editors else []


def _lang_rows(scr):
    return [str(w.render()) for w in _lang_widgets(scr) if w.display]


def _lang_selected(scr):
    return [r for r in _lang_rows(scr) if r.startswith("▸")]


def _lang_hint(scr):
    from tui.editors.voice_endpoint import VoiceEndpointEditor
    editors = list(scr.query(VoiceEndpointEditor))
    hints = list(editors[-1].query(".voice-lang-hint")) if editors else []
    return str(hints[-1].render()) if hints else None


async def _type(pilot, text):
    for ch in text:
        await pilot.press(ch)
    await pilot.pause()


@pytest.mark.asyncio
async def test_lang_list_opens_when_the_cursor_enters_the_field(tmp_path):
    """Вход курсора на «Язык» раскрывает список (канон пресет-листа адресов):
    хинт + все коды whisper прямо под полем, ▸ на действующем."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, LOCAL)
        fake.language = "en"
        await _open_local(app, scr, pilot)
        assert not _lang_rows(scr)                        # на строке режима списка нет
        await _to_field(pilot)                            # в поле «Язык»
        assert _focused_id(scr) == "voice-lang"
        assert _lang_hint(scr) == (
            "языки · печать — фильтр/свой код · Enter — подставить · Esc — свернуть")
        assert len(_lang_rows(scr)) == 100
        assert _lang_selected(scr) == ["▸ en    английский"]


@pytest.mark.asyncio
async def test_lang_list_rows_sit_between_the_field_and_the_probe_line(tmp_path):
    """Список приклеен к своему полю: строки — в рамке группы «Языка», хинт — сразу под
    ней, probe-строка секции остаётся под редактором."""
    from tui.app import TuiApp
    from tui.editors.voice_endpoint import VoiceEndpointEditor
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        await _open_cloud(app, scr, pilot)
        await _to_field(pilot, 3)
        ed = scr.query_one(VoiceEndpointEditor)
        group = scr.query_one("#voice-lang-group")
        assert scr.query_one("#voice-lang", Input).parent is group
        scroll = group.children[-1]
        assert "voice-lang-scroll" in scroll.classes and "dropdown-scroll" in scroll.classes
        assert all("voice-lang-row" in w.classes for w in scroll.children)
        children = list(ed.children)
        assert "voice-lang-hint" in children[children.index(group) + 1].classes
        frame = scr.query_one("#set-frame")
        assert "set-probe" in frame.children[-1].classes      # probe — ниже редактора


@pytest.mark.asyncio
async def test_lang_list_arrows_walk_the_list_not_the_cursor(tmp_path):
    """Пока список раскрыт, ↑/↓ ходят ПО НЕМУ: курсор секции стоит, фокус остаётся в поле."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL)
        await _open_local(app, scr, pilot)
        await _to_field(pilot)
        place = (scr._voice_cursor_group(), scr._voice_cursor_position())
        await pilot.press("down")
        await pilot.pause()
        assert (scr._voice_cursor_group(), scr._voice_cursor_position()) == place
        assert _focused_id(scr) == "voice-lang"
        assert _lang_selected(scr) == ["▸ en    английский"]
        await pilot.press("up")
        await pilot.press("up")
        await pilot.pause()
        assert (scr._voice_cursor_group(), scr._voice_cursor_position()) == place
        assert _focused_id(scr) == "voice-lang"
        assert _lang_selected(scr) == ["▸ yue   кантонский"]
        assert _cursor_row(scr) == 0                       # ▸ у владельца полей


@pytest.mark.asyncio
async def test_typing_filters_the_list_by_name_and_keeps_the_input(tmp_path):
    """Печать в поле фильтрует список (по названию тоже: «нем» → de) и НЕ теряет
    набранное — поле остаётся вводимым."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL)
        await _open_local(app, scr, pilot)
        await _to_field(pilot)
        scr.query_one("#voice-lang", Input).value = ""     # очистить префилл
        await pilot.pause()
        await _type(pilot, "нем")
        assert scr.query_one("#voice-lang", Input).value == "нем"
        assert _lang_rows(scr) == ["▸ de    немецкий"]


@pytest.mark.asyncio
async def test_typing_filters_the_list_by_code(tmp_path):
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL)
        await _open_local(app, scr, pilot)
        await _to_field(pilot)
        scr.query_one("#voice-lang", Input).value = ""
        await pilot.pause()
        await _type(pilot, "zh")
        assert _lang_rows(scr) == ["▸ zh    китайский"]


@pytest.mark.asyncio
async def test_enter_picks_the_code_and_saves_the_local_language(tmp_path):
    """Enter при раскрытом списке: подставить код, свернуть список и сохранить (локальный
    режим — set_local_language)."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, LOCAL)
        await _open_local(app, scr, pilot)
        await _to_field(pilot)
        scr.query_one("#voice-lang", Input).value = ""
        await pilot.pause()
        await _type(pilot, "тур")
        await pilot.press("enter")
        await pilot.pause()
        assert scr.query_one("#voice-lang", Input).value == "tr"
        assert fake.calls == ["set_local_language"] and fake.language == "tr"
        assert not _lang_rows(scr)                        # список свернулся
        assert _focused_id(scr) == "voice-lang"


@pytest.mark.asyncio
async def test_enter_picks_the_code_and_saves_the_cloud_endpoint(tmp_path):
    """То же в облачной панели: Enter подставляет код и уходит в обычное сохранение
    endpoint'а (wire_cloud с выбранным языком)."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        await _open_cloud(app, scr, pilot)
        await _to_field(pilot, 3)
        assert _focused_id(scr) == "voice-lang"
        scr.query_one("#voice-lang", Input).value = ""
        await pilot.pause()
        await _type(pilot, "исп")
        assert _lang_rows(scr) == ["▸ es    испанский"]
        await pilot.press("enter")
        await pilot.pause()
        assert fake.calls == ["wire_cloud"]
        assert fake.wired["language"] == "es"
        assert fake.wired["url"] == "https://old.x/v1/audio/transcriptions"


@pytest.mark.asyncio
async def test_escape_collapses_the_list_and_keeps_the_cursor_in_the_field(tmp_path):
    """Esc сворачивает список и оставляет курсор в поле (из секции НЕ выходим); после
    него стрелки снова водят ленту, а на месте списка остаётся строка о том, чем его
    раскрыть обратно."""
    from tui.app import TuiApp
    from tui.editors.field_list import LIST_CLOSED_HINT as _LIST_CLOSED_HINT
    from tui.screens.settings import _VOICE
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL)
        await _open_local(app, scr, pilot)
        await _to_field(pilot)
        place = (scr._voice_cursor_group(), scr._voice_cursor_position())
        await pilot.press("escape")
        await pilot.pause()
        assert not _lang_rows(scr) and _lang_hint(scr) == _LIST_CLOSED_HINT
        assert scr._view == _VOICE
        assert (scr._voice_cursor_group(), scr._voice_cursor_position()) == place
        assert _focused_id(scr) == "voice-lang"
        await pilot.press("tab")                          # курсор снова свободен
        await pilot.press("up")
        await pilot.pause()
        assert (_cursor_row(scr), _focused_id(scr)) == (1, None)
        # Строка под полем остаётся: она про поле, а не про курсор (канон 2026-08-20 —
        # подсказки стоят под всеми полями со списком с первого кадра).
        assert _lang_hint(scr) == _LIST_CLOSED_HINT


@pytest.mark.asyncio
async def test_own_code_is_saved_as_typed(tmp_path):
    """Свой код (которого у whisper нет) сохраняется как есть: фильтр без совпадений — Enter
    про введённое, и хинт про это честно говорит."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, LOCAL)
        await _open_local(app, scr, pilot)
        await _to_field(pilot)
        scr.query_one("#voice-lang", Input).value = ""
        await pilot.pause()
        await _type(pilot, "zz")
        assert _lang_rows(scr) == []
        assert _lang_hint(scr) == "свой код · Enter — сохранить как есть · Esc — свернуть"
        await pilot.press("enter")
        await pilot.pause()
        assert fake.language == "zz"
        assert scr.query_one("#voice-lang", Input).value == "zz"


@pytest.mark.asyncio
async def test_saved_own_code_highlights_nothing_on_open(tmp_path):
    """В поле уже лежит свой код: подсвеченной строки нет — Enter сразу после входа не
    имеет права подменить его первой строкой списка."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, LOCAL)
        fake.language = "zz"
        await _open_local(app, scr, pilot)
        await _to_field(pilot)
        assert len(_lang_rows(scr)) == 100 and _lang_selected(scr) == []
        await pilot.press("enter")
        await pilot.pause()
        assert fake.language == "zz"


@pytest.mark.asyncio
async def test_leaving_the_field_collapses_the_list(tmp_path):
    """Уход курсора с поля сворачивает список — в т.ч. кликом мышью в другое поле.

    Строка под полем при этом ОСТАЁТСЯ (канон форм 2026-08-17): она называет способ
    вернуть список, а не команду текущего момента, и её судьбой распоряжается сам
    контроллер. Раньше голосовой экран снимал её вручную вслед за курсором."""
    from tui.app import TuiApp
    from tui.editors.field_list import LIST_CLOSED_HINT
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        await _open_cloud(app, scr, pilot)
        scr.query_one("#voice-lang", Input).focus()        # как клик мышью по полю
        await pilot.pause()
        await _expand_list(pilot)                           # код задан — раскрываем Alt+↓
        assert len(_lang_rows(scr)) == 100
        scr.query_one("#voice-url", Input).focus()         # клик в соседнее поле
        await pilot.pause()
        assert not _lang_rows(scr)                          # строки ушли со списком
        assert _lang_hint(scr) == LIST_CLOSED_HINT          # строка-способ осталась


@pytest.mark.asyncio
async def test_leaving_the_section_collapses_the_list(tmp_path):
    """Уход из секции уносит список, а возврат начинается с дефолта — со строк режима.

    Раньше секция помнила место и раскрывала список заново (живой смок 2026-08-20:
    возврат попадал сразу в поле и раскрывал то, чего человек не просил)."""
    from tui.app import TuiApp
    from tui.screens.settings import _MODES, _VOICE, _VOICE_GROUP_VARIANTS
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL)
        await _open_local(app, scr, pilot)
        await _to_field(pilot)
        assert len(_lang_rows(scr)) == 100
        await pilot.press("pagedown")
        await pilot.pause()
        assert scr._view == _MODES and not scr.query(".voice-lang-row")
        await pilot.press("pageup")
        await pilot.pause()
        assert scr._view == _VOICE
        assert not _lang_rows(scr)                                   # вернулись — свёрнут
        assert scr._voice_cursor_group() == _VOICE_GROUP_VARIANTS    # и курсор на дефолте


@pytest.mark.asyncio
async def test_escape_returns_the_previous_code_and_no_filter_leaks_into_the_config(tmp_path):
    """Esc возвращает в поле прежний код: остаток фильтра («нем») — не язык, и следующий
    Enter не имеет права записать его в конфиг."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, LOCAL)
        await _open_local(app, scr, pilot)
        await _to_field(pilot)
        scr.query_one("#voice-lang", Input).value = ""
        await pilot.pause()
        await _type(pilot, "нем")
        await pilot.press("escape")
        await pilot.pause()
        assert scr.query_one("#voice-lang", Input).value == "ru"    # фильтр отменён
        await pilot.press("enter")
        await pilot.pause()
        assert fake.calls == ["set_local_language"] and fake.language == "ru"


@pytest.mark.asyncio
async def test_a_bad_language_code_is_not_saved_locally(tmp_path):
    """Код языка проверяется перед записью: свернули список (Esc), набрали руками мусор
    («abcd» — 4 буквы, не 2-3, и не входит ни в один код/название списка, иначе печать
    раскрыла бы список заново с совпадением — см. Reopen) — сохранения нет, экран
    говорит правило."""
    from tui.app import TuiApp
    from textual.widgets import Input, Static
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, LOCAL)
        await _open_local(app, scr, pilot)
        await _to_field(pilot)
        await pilot.press("escape")                       # список свёрнут — фильтра больше нет
        await pilot.pause()
        scr.query_one("#voice-lang", Input).value = "abcd"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert fake.calls == [] and fake.language == "ru"
        assert "2-3 латинские буквы" in str(scr.query_one("#set-flash", Static).render())


@pytest.mark.asyncio
async def test_a_bad_language_code_is_not_saved_to_the_cloud(tmp_path):
    """То же в облачной панели: мусор не уезжает в config.yml юзера через wire_cloud."""
    from tui.app import TuiApp
    from textual.widgets import Input, Static
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        await _open_cloud(app, scr, pilot)                # курсор на строке — списка нет
        scr.query_one("#voice-lang", Input).value = "нем"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert fake.calls == [] and fake.wired == {}
        assert "2-3 латинские буквы" in str(scr.query_one("#set-flash", Static).render())


@pytest.mark.asyncio
async def test_cloud_save_leaves_the_list_collapsed(tmp_path):
    """После сохранения список свёрнут: пересборка полей автофокусирует «Язык», и без
    гейта список раскрывался бы заново поверх только что сохранённого выбора. Гейт
    одноразовый — следующий вход на поле раскрывает список как обычно."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        await _open_cloud(app, scr, pilot)
        await _to_field(pilot, 3)
        assert len(_lang_rows(scr)) == 100
        await pilot.press("enter")
        await pilot.pause()
        assert fake.calls == ["wire_cloud"]
        assert not _lang_rows(scr)
        assert "Esc — свернуть" not in _footer(scr)
        assert _focused_id(scr) == "voice-lang"
        await pilot.press("up")                           # стрелки свободны — ушли на «Модель»
        await pilot.press("down")                         # и вернулись на «Язык»
        await pilot.pause()
        assert scr.query_one("#voice-lang", Input).value == "ru"
        # Код в поле есть — вход его список не раскрывает (канон 2026-08-20); показать
        # список по-прежнему может Alt+↓.
        assert not _lang_rows(scr)
        await _expand_list(pilot)
        assert len(_lang_rows(scr)) == 100


@pytest.mark.asyncio
async def test_arrows_work_again_when_the_filter_matches_nothing(tmp_path):
    """Фильтр без совпадений ходить не даёт — значит и клавишу забирать не должен: ↑/↓
    снова водят курсор по группе полей, иначе поле «запирает» стрелки без обратной связи."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL, config_text=CLOUD_CONFIG)
        await _open_cloud(app, scr, pilot)               # в облаке полей четыре — ходьба видна
        await _to_field(pilot, 3)                        # поле «Язык», список раскрыт
        scr.query_one("#voice-lang", Input).value = ""
        await pilot.pause()
        await _type(pilot, "zz")
        assert _lang_rows(scr) == []
        await pilot.press("up")
        await pilot.pause()
        assert _focused_id(scr) == "voice-model"          # стрелка досталась группе полей
        assert not scr.query(".voice-lang-row")


@pytest.mark.asyncio
async def test_the_filter_does_not_leak_into_the_endpoint_draft(tmp_path):
    """Смена панели снимает черновик полей — и не имеет права унести с собой остаток
    фильтра: список сворачивается ДО снимка, в поле возвращается прежний код."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL, config_text=CLOUD_CONFIG)
        await _open_cloud(app, scr, pilot)
        await _to_field(pilot, 3)
        scr.query_one("#voice-lang", Input).value = "ниме"  # опечатка в фильтре — совпадений нет
        await pilot.pause()
        assert _lang_rows(scr) == []
        await pilot.press("tab")                            # из полей на строку режима
        await pilot.press("up")                             # ушли в локальную панель
        await pilot.pause()
        await pilot.press("down")                           # и вернулись в облачную
        await _to_field(pilot, 3)
        assert scr.query_one("#voice-lang", Input).value == "ru"


@pytest.mark.asyncio
async def test_footer_matches_the_hint_when_nothing_matches(tmp_path):
    """Футер не спорит с хинтом: подставлять нечего — обещаем «сохранить как есть», а
    ↑/↓ снова про группу полей."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL)
        await _open_local(app, scr, pilot)
        await _to_field(pilot)
        scr.query_one("#voice-lang", Input).value = ""
        await pilot.pause()
        await _type(pilot, "zz")
        assert _footer(scr) == ("↑/↓ · Enter — сохранить как есть · Esc — свернуть · "
                                "PgUp/PgDn — секции")
        await pilot.press("backspace")
        await pilot.press("backspace")                     # вернули совпадения — футер списочный
        await pilot.pause()
        assert _lang_rows(scr) and "Enter — подставить и сохранить" in _footer(scr)


@pytest.mark.asyncio
async def test_footer_talks_about_the_list_while_it_is_open(tmp_path):
    """Пока список раскрыт, футер про его клавиши: ↑/↓ ходят по списку, Esc сворачивает
    список (а не выходит в меню). Свернули — футер снова про место."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL)
        await _open_local(app, scr, pilot)
        await _to_field(pilot)
        assert _footer(scr) == ("↑/↓ — выбор языка · Enter — подставить и сохранить · "
                               "Esc — свернуть · PgUp/PgDn — секции")
        await pilot.press("escape")
        await pilot.pause()
        assert "Enter — сохранить язык" in _footer(scr)


@pytest.mark.asyncio
async def test_lang_list_footer_promises_the_probe_only_in_the_cloud_pane(tmp_path):
    """Список языка — единственное поле, общее у local и cloud панели (спека 2026-08-06
    §4.2): модели и URL раскрываются только у облака. Тот же раскрытый список в двух
    панелях обязан звать Ctrl+Space по-разному — проба есть лишь в облаке (§2.5 спеки,
    §9.2 канона) — ради этого константа и раздвоена на `_VOICE_FOOTER_LANG_LIST_LOCAL`/
    `_VOICE_FOOTER_LANG_LIST_CLOUD`."""
    from tui.app import TuiApp
    from tui.screens.settings import (
        _VOICE_FOOTER_LANG_LIST_LOCAL, _VOICE_FOOTER_LANG_LIST_CLOUD)
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL)
        await _open_local(app, scr, pilot)
        await _to_field(pilot)                          # локальная «Язык», список раскрыт
        assert _footer(scr) == _VOICE_FOOTER_LANG_LIST_LOCAL
        assert "Ctrl+Space" not in _footer(scr)
    app2 = TuiApp(config={"model": "M"})
    async with app2.run_test() as pilot:
        scr2, _ = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        await _open_cloud(app2, scr2, pilot)
        await _to_field(pilot, 3)                       # облачная «Язык», список раскрыт
        assert _footer(scr2) == _VOICE_FOOTER_LANG_LIST_CLOUD
        assert "Ctrl+Space — проверить связь" in _footer(scr2)


# --- список моделей своего endpoint -----------------------------------------

_MODELS = ["whisper-large-v3-turbo", "Systran/faster-whisper-small"]
# Группа полей облака: url, token, model, language.
_TO_MODEL_FIELD = 2          # ↓ от первого поля группы до «Модели»


def _model_widgets(scr):
    from tui.editors.voice_endpoint import VoiceEndpointEditor
    editors = list(scr.query(VoiceEndpointEditor))
    return list(editors[-1].query(".voice-model-row")) if editors else []


def _model_rows(scr):
    return [str(w.render()) for w in _model_widgets(scr) if w.display]


def _model_hint(scr):
    from tui.editors.voice_endpoint import VoiceEndpointEditor
    editors = list(scr.query(VoiceEndpointEditor))
    hints = list(editors[-1].query(".voice-model-hint")) if editors else []
    return str(hints[-1].render()) if hints else None


async def _open_model_field(app, scr, pilot, tries=100):
    """Курсор — на поле «Модель» облачной панели, список раскрыт; ждём ответа загрузки.

    Сам список раскрывается только на ПУСТОМ поле (канон 2026-08-20): при сохранённом
    id раскрытие — явная просьба человека, `Alt+↓`. Тестам нужен раскрытый список в
    обоих случаях, поэтому просим явно там, где автовход промолчал."""
    await _open_cloud(app, scr, pilot)
    await _to_field(pilot, _TO_MODEL_FIELD)
    ctrl = scr._voice_list_ctrl()
    if ctrl is not None and not ctrl.is_open:
        await pilot.press("alt+down")
        await pilot.pause()
    await _wait(pilot, lambda: _model_hint(scr) != "загружаю модели…", tries=tries)


@pytest.mark.asyncio
async def test_model_list_opens_on_alt_down_over_a_saved_id(tmp_path):
    """Модель уже сохранена — вход курсора список НЕ раскрывает и в сеть не ходит
    (живой смок 2026-08-20: раскрытие поверх выбранного значения только мешало, а
    каждый заход на поле стоил запроса). Раскрывает `Alt+↓`, и вот он идёт на сервер.

    Сохранённый id (`whisper-old`) среди ответа сервера не значится — ни одна строка
    не подсвечена (видно по отсутствию `▸` в `_model_rows`), и хинт обязан звать Enter
    «сохранить как есть», а не «подставить» (находка A финального ревью 2026-08-06:
    хинт читал `has_matches`, а не то, есть ли подсветка, и врал именно в этом
    состоянии)."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        fake.models = _MODELS
        assert not _model_rows(scr)
        await _open_cloud(app, scr, pilot)
        await _to_field(pilot, _TO_MODEL_FIELD, expand=False)
        assert not _model_rows(scr), "вход на заполненное поле список не раскрывает"
        assert fake.model_calls == [], "и в сеть за списком не ходит"
        await pilot.press("alt+down")                   # явная просьба показать список
        await _wait(pilot, lambda: _model_hint(scr) != "загружаю модели…")
        assert _focused_id(scr) == "voice-model"
        assert _model_rows(scr) == ["  whisper-large-v3-turbo",
                                    "  Systran/faster-whisper-small"]
        assert _model_hint(scr) == "свой id · Enter — сохранить как есть · Esc — свернуть"
        # адрес и сохранённый токен — из полей/файла, verify — дефолт схемы
        assert fake.model_calls == [("https://old.x/v1/audio/transcriptions",
                                     "old-token", True)]


@pytest.mark.asyncio
async def test_model_list_says_nothing_without_a_url(tmp_path):
    """Адреса ещё нет — в сеть не ходим и списка не показываем: человек только начал
    заполнять форму, и вход курсора в поле не повод его отчитывать.

    Строка под полем при этом стоит (канон 2026-08-20: у полей со списком она есть с
    первого кадра) и называет клавишу — та сходит и объяснит, чего не хватает."""
    from tui.app import TuiApp
    from tui.editors.voice_endpoint import _MODEL_RELOAD_HINT
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF)          # voice-секции в файле нет
        fake.models = _MODELS
        await _open_model_field(app, scr, pilot)
        assert fake.model_calls == []
        assert not _model_rows(scr)
        assert _model_hint(scr) == _MODEL_RELOAD_HINT


@pytest.mark.asyncio
async def test_alt_down_on_empty_url_model_field_explains_instead_of_silence(tmp_path):
    """Находка B финального ревью 2026-08-06 (риск §6 спеки): раньше явный Alt+↓ на
    «Модели» с пустым URL молчал — `_load` сворачивал только что смонтированный хинт
    тем же тактом, клавиша съедалась, а на экране ничего не менялось. Автовход курсора
    остаётся тихим (см. test_model_list_says_nothing_without_a_url) — это другой путь,
    и отчитывать за него не за что; явная просьба человека — другое дело.

    Пустой адрес раскрывает СВОЙ пресет-лист на входе (канон §2.1) — обычный
    `_open_model_field`/`_to_field` до «Модели» тогда не доезжает, стрелки уходят в
    список адреса. Закрываем его Esc'ом явно, дальше — как обычно."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF)          # voice-секции в файле нет — URL пуст
        await _open_cloud(app, scr, pilot)
        await pilot.press("tab")                        # в группу полей, на «URL» (список раскрыт сам)
        await pilot.pause()
        await pilot.press("escape")                     # свернуть список адреса, курсор остаётся на поле
        await pilot.pause()
        await pilot.press("down", "down")               # URL → Токен → Модель
        await pilot.pause()
        from tui.editors.voice_endpoint import _MODEL_RELOAD_HINT
        assert _focused_id(scr) == "voice-model"         # предусловие: курсор на «Модели»
        # Предусловие: автовход тих — строк нет и в сеть не ходили. Под полем при этом
        # стоит обещание клавиши (канон 2026-08-20), а не объяснение: отчитывать
        # человека за то, что он просто дошёл до поля, не за что.
        assert not _model_rows(scr)
        assert _model_hint(scr) == _MODEL_RELOAD_HINT
        await pilot.press("alt+down")
        await pilot.pause()
        assert _model_hint(scr) == "сначала укажите адрес endpoint"


@pytest.mark.asyncio
async def test_model_list_closed_hint_says_reread_not_show(tmp_path):
    """Находка C финального ревью 2026-08-06: `LIST_CLOSED_HINT` — общий глагол
    «показать» для локальных списков (пресеты, язык), но список моделей endpoint'а
    сетевой — Alt+↓ у него ВСЕГДА идёт на сервер, раскрыт список или нет. Свёрнутое
    состояние обязано звать клавишу так же, как раскрытое (`_MODEL_HINT` — «перечитать
    список»), иначе одна клавиша на одном поле звалась бы двумя глаголами."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        fake.models = _MODELS
        await _open_model_field(app, scr, pilot)
        await pilot.press("escape")            # свернуть список, курсор остаётся на поле
        await pilot.pause()
        hint = _model_hint(scr)
        assert hint == "Alt+↓ — перечитать список"


@pytest.mark.asyncio
async def test_model_list_failure_does_not_break_the_section(tmp_path):
    """Сервер не ответил: секция жива, статус честный, ручной ввод не заперт —
    набранный id уезжает в сохранение как есть."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        fake.models = None                             # не достучались
        await _open_model_field(app, scr, pilot)
        assert _model_hint(scr) == "не удалось получить список — можно вписать вручную"
        scr.query_one("#voice-model", Input).value = "my-own-stt"
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert fake.wired["model"] == "my-own-stt"


@pytest.mark.asyncio
async def test_model_list_reports_an_empty_server(tmp_path):
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        fake.models = []
        await _open_model_field(app, scr, pilot)
        assert _model_hint(scr) == "сервер не вернул моделей"


@pytest.mark.asyncio
async def test_alt_down_reloads_the_model_list(tmp_path):
    """Alt+↓ на «Модели» — перечитать с сервера (он мог ожить, адрес/токен —
    смениться), в отличие от языка, где список и так уже весь тут. Ctrl+Space
    сюда не заходит вовсе (развод §2 спеки 2026-08-06) — он теперь про пробу."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        fake.models = None
        await _open_model_field(app, scr, pilot)
        assert len(fake.model_calls) == 1
        fake.models = _MODELS                          # сервис подняли
        await pilot.press("alt+down")
        await _wait(pilot, lambda: _model_rows(scr))
        assert len(fake.model_calls) == 2
        assert _model_rows(scr)[0] == "  whisper-large-v3-turbo"


@pytest.mark.asyncio
async def test_enter_on_a_model_row_saves_the_endpoint(tmp_path):
    """Enter при раскрытом списке: подставить id в поле и уйти в обычное сохранение
    endpoint'а (как у языка)."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        fake.models = _MODELS
        await _open_model_field(app, scr, pilot)
        scr.query_one("#voice-model", Input).value = ""      # очистить сохранённый id
        await pilot.pause()
        await _type(pilot, "faster")
        assert _model_rows(scr) == ["▸ Systran/faster-whisper-small"]
        await pilot.press("enter")
        await pilot.pause()
        assert fake.calls == ["wire_cloud"]
        assert fake.wired["model"] == "Systran/faster-whisper-small"
        assert not _model_rows(scr)                    # список свернулся вместе с Enter


@pytest.mark.asyncio
async def test_saved_model_is_highlighted_and_kept_by_a_trusting_enter(tmp_path):
    """В поле уже лежит id, которого сервер не отдал: подсвеченной строки нет, и Enter
    сразу после входа сохраняет ЕГО, а не первую строку списка."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)   # model: whisper-old
        fake.models = _MODELS
        await _open_model_field(app, scr, pilot)
        assert len(_model_rows(scr)) == 2 and not [r for r in _model_rows(scr)
                                                   if r.startswith("▸")]
        await pilot.press("enter")
        await pilot.pause()
        assert fake.wired["model"] == "whisper-old"


@pytest.mark.asyncio
async def test_leaving_the_model_field_collapses_its_list(tmp_path):
    """Уход курсора с поля сворачивает список моделей — как у языка, в т.ч. кликом мышью
    в соседнее поле.

    Строка под полем остаётся и здесь (канон 2026-08-17), но текст у неё свой: список
    сетевой, и `Alt+↓` ему всегда «перечитать», а не «показать»."""
    from tui.app import TuiApp
    from tui.editors.voice_endpoint import _MODEL_RELOAD_HINT
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        fake.models = _MODELS
        await _open_model_field(app, scr, pilot)
        assert _model_rows(scr)
        scr.query_one("#voice-url", Input).focus()     # как клик мышью по соседнему полю
        await pilot.pause()
        assert not _model_rows(scr)                     # строки ушли со списком
        assert _model_hint(scr) == _MODEL_RELOAD_HINT   # строка-способ осталась


@pytest.mark.asyncio
async def test_only_one_list_is_open_at_a_time(tmp_path):
    """Списков в панели два, но раскрыт тот, на чьём поле курсор: переход «Модель» →
    «Язык» сворачивает первый и раскрывает второй."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        fake.models = _MODELS
        await _open_model_field(app, scr, pilot)
        assert _model_rows(scr) and not _lang_rows(scr)
        await pilot.press("escape")                    # свернуть список — стрелки группе полей
        await pilot.press("down")                      # на «Язык»
        await pilot.pause()
        assert _focused_id(scr) == "voice-lang"
        await _expand_list(pilot)                      # код задан — раскрываем Alt+↓
        assert len(_lang_rows(scr)) == 100 and not _model_rows(scr)


@pytest.mark.asyncio
async def test_local_pane_has_no_model_list(tmp_path):
    """Локальная панель модель показывает ФАКТОМ (её задаёт установка сервиса): списка
    там нет вовсе, и в сеть за ним никто не ходит."""
    from tui.app import TuiApp
    from tui.editors.voice_endpoint import VoiceEndpointEditor
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, LOCAL)
        fake.models = _MODELS
        await _open_local(app, scr, pilot)
        ed = list(scr.query(VoiceEndpointEditor))[-1]
        assert ed.model_list is None
        await _to_field(pilot)                         # единственное поле локальной панели
        for _ in range(4):                             # весь круг курсора по ней
            await pilot.press("up")
        await pilot.pause()
        assert fake.model_calls == [] and not _model_rows(scr)


@pytest.mark.asyncio
async def test_footer_follows_the_model_field_and_its_list(tmp_path):
    """Футер на «Модели» — про её клавиши: свёрнут — Alt+↓ (глагол сети — «перечитать»,
    находка C) и проба, раскрыт — про подстановку и проба (спека 2026-08-06 §2.2, §2.5:
    список забирает себе только Esc/↑↓/Enter, проба работает и тут, и там).

    Сохранённый id (`whisper-old`) среди ответа сервера не значится — подсвеченной
    строки нет (находка A), и раскрытое состояние обязано звать Enter «сохранить как
    есть», а не «подставить»."""
    from tui.app import TuiApp
    from tui.editors.voice_endpoint import _MODEL_RELOAD_HINT
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        fake.models = _MODELS
        await _open_model_field(app, scr, pilot)
        assert _footer(scr) == ("↑/↓ · Enter — сохранить как есть · Esc — свернуть · "
                                "Ctrl+Space — проверить связь · PgUp/PgDn — секции")
        await pilot.press("escape")                    # свернуть список
        await pilot.pause()
        assert _MODEL_RELOAD_HINT in _footer(scr)
        assert "Enter — сохранить" in _footer(scr)
        assert "Ctrl+Space — проверить связь" in _footer(scr)


@pytest.mark.asyncio
async def test_alt_down_over_a_filtered_model_list_keeps_the_saved_id(tmp_path):
    """Сквозная трасса живыми клавишами: перечитывание списка (Alt+↓) не имеет права
    сделать остаток фильтра значением поля — иначе Esc вернёт «faster», а доверчивый
    Enter запишет его в voice.model вместо сохранённой модели."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)   # model: whisper-old
        fake.models = _MODELS
        await _open_model_field(app, scr, pilot)
        scr.query_one("#voice-model", Input).value = ""
        await pilot.pause()
        await _type(pilot, "faster")
        await pilot.press("alt+down")                  # перечитать список
        await _wait(pilot, lambda: _model_rows(scr))
        # список догнал поле: показывает ровно то, что отфильтровано (а не всё подряд)
        assert _model_rows(scr) == ["▸ Systran/faster-whisper-small"]
        await pilot.press("escape")                    # свернуть — откат фильтра
        await pilot.pause()
        assert scr.query_one("#voice-model", Input).value == "whisper-old"
        await pilot.press("enter")                     # доверчивый Enter — модель прежняя
        await pilot.pause()
        assert fake.wired["model"] == "whisper-old"


@pytest.mark.asyncio
async def test_picking_a_model_from_the_list_drops_the_probe_verdict(tmp_path):
    """Выбор модели из списка — такая же правка поля, как печать: зелёная ✓ относится к
    ПРОВЕРЕННОЙ модели и не имеет права пережить смену на другую (подстановка идёт через
    set_reactive и Input.Changed не рождает — гасим вердикт сами)."""
    from tui.app import TuiApp
    from textual.widgets import Input, Static
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        fake.models = _MODELS
        fake.probe_result = ProbeResult(ok=True, latency_ms=42, error=None)
        await _open_cloud(app, scr, pilot)
        await pilot.press("ctrl+space")                # проверили whisper-old
        await pilot.pause()
        assert "42" in str(scr.query_one(".set-probe", Static).render())
        await _to_field(pilot, _TO_MODEL_FIELD)
        await pilot.press("alt+down")                  # id сохранён — раскрываем явно
        await _wait(pilot, lambda: _model_rows(scr))
        await pilot.press("down")                      # встали на строку списка
        await pilot.pause()
        assert _model_rows(scr)[0].startswith("▸")
        # ровно то, что делает Enter перед сохранением (проверяем сам шов подстановки:
        # дальше сохранение переносит эталон сравнения на новые значения и вердикт,
        # доживший до него, уже никем не гасится)
        assert scr._voice_pick_from_list() is True
        assert scr.query_one("#voice-model", Input).value == "whisper-large-v3-turbo"
        assert scr._voice_probe_result is None         # ✓ не про эту модель
        assert "не проверено" in str(scr.query_one(".set-probe", Static).render())


@pytest.mark.asyncio
async def test_ctrl_space_on_a_plain_voice_field_reprobes(tmp_path):
    """Курсор на поле без списка (токен) — Ctrl+Space идёт прямо в пробу: развод §2
    спеки 2026-08-06 убрал у неё вопрос про список вовсе. У «Адреса» список теперь
    есть (спека 2026-08-05 §3, пресеты), но клавиша про него не спрашивает ни
    свёрнутый (см. `test_own_url_does_not_auto_open_but_alt_down_opens_it`), ни
    раскрытый (см. `test_ctrl_space_probes_the_url_field_regardless_of_the_list_state`)."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        fake.probe_result = ProbeResult(ok=True, latency_ms=10, error=None)
        await _open_cloud(app, scr, pilot)
        await _to_field(pilot, 1)                      # в поле «Токен» — списка у него нет
        assert not fake.probe_calls
        await pilot.press("ctrl+space")
        assert await _wait(pilot, lambda: fake.probe_calls), "ручная проверка должна уйти"


@pytest.mark.asyncio
async def test_ctrl_space_reprobe_ignores_the_debounce_snapshot(tmp_path):
    """Ручная проверка проходит и при неизменных полях — иначе залипший красный вердикт
    нечем сбросить (спека §5.2)."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        fake.probe_result = ProbeResult(ok=True, latency_ms=10, error=None)
        await _open_cloud(app, scr, pilot)
        await _to_field(pilot, 1)                      # «Токен» — списка у него нет
        await pilot.press("ctrl+space")
        assert await _wait(pilot, lambda: len(fake.probe_calls) == 1)
        await pilot.press("ctrl+space")                # поля не трогали
        assert await _wait(pilot, lambda: len(fake.probe_calls) == 2), \
            "снимок против дребезга не должен глушить ручную"


@pytest.mark.asyncio
async def test_alt_down_on_the_model_field_reloads_without_probing(tmp_path):
    """Alt+↓ на поле со списком: перечитывается список, проба не шлётся — раскрытие/
    перечитывание списка теперь ЦЕЛИКОМ его роль (спека 2026-08-06 §2)."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        fake.models = _MODELS
        fake.probe_result = ProbeResult(ok=True, latency_ms=10, error=None)
        await _open_model_field(app, scr, pilot)
        loads = len(fake.model_calls)
        await pilot.press("alt+down")
        # Безгоночный сторож: `_voice_probe_in_flight` взводится СИНХРОННО, до
        # run_worker, а `probe_calls` пишет поток — пустой журнал сразу после нажатия
        # доказывал бы разве что «поток не успел».
        assert not scr._voice_probe_in_flight, "проба не имеет права стартовать"
        assert await _wait(pilot, lambda: len(fake.model_calls) > loads), \
            "список должен перечитаться"
        assert not fake.probe_calls, "второй запрос (проба) здесь не нужен"


@pytest.mark.asyncio
async def test_ctrl_space_probes_even_when_the_model_list_is_open(tmp_path):
    """Ctrl+Space на «Модели» облачной панели проверяет связь, даже когда список
    раскрыт (§2.2 спеки 2026-08-06: список забирает себе только Esc/↑↓/Enter) —
    в отличие от Alt+↓, эта клавиша про список больше не спрашивает вовсе."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        fake.models = _MODELS
        fake.probe_result = ProbeResult(ok=True, latency_ms=10, error=None)
        await _open_model_field(app, scr, pilot)
        assert _model_rows(scr), "предусловие: список раскрыт"
        loads = len(fake.model_calls)
        await pilot.press("ctrl+space")
        assert await _wait(pilot, lambda: fake.probe_calls), "проба обязана уйти"
        assert len(fake.model_calls) == loads, "список трогать не должны"
        assert _model_rows(scr), "проба не имеет права закрыть список"


@pytest.mark.asyncio
async def test_alt_down_model_list_reload_does_not_touch_the_verdict(tmp_path):
    """Перечитывание списка моделей (Alt+↓) не красит вердикт: список идёт на /models,
    а вердикт отвечает за /audio/transcriptions — успех первого не доказывает второго."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        fake.models = _MODELS
        fake.probe_result = ProbeResult(ok=True, latency_ms=42, error=None)
        await _open_cloud(app, scr, pilot)
        await pilot.press("ctrl+space")                # настоящий вердикт: ✓ 42 мс
        assert await _wait(pilot, lambda: scr._voice_probe_result is not None)
        verdict = scr._voice_probe_result
        await _to_field(pilot, _TO_MODEL_FIELD)
        await _wait(pilot, lambda: _model_rows(scr))
        loads = len(fake.model_calls)
        await pilot.press("alt+down")
        # Тот же безгоночный сторож: вердикт меняет только осевшая проба, а её старт
        # виден синхронно — сравнение самого вердикта поймало бы подмену лишь после
        # того, как поток успеет ответить.
        assert not scr._voice_probe_in_flight, "проба не имеет права стартовать"
        assert await _wait(pilot, lambda: len(fake.model_calls) > loads), \
            "список должен перечитаться"
        assert scr._voice_probe_result is verdict, "вердикт трогать нельзя"


# --- пресеты адреса «URL» (спека 2026-08-05 §3) ------------------------------

def _url_widgets(scr):
    """Строки пресет-листа адреса ТЕКУЩЕГО редактора (канон `_lang_widgets`)."""
    from tui.editors.voice_endpoint import VoiceEndpointEditor
    editors = list(scr.query(VoiceEndpointEditor))
    return list(editors[-1].query(".voice-url-preset-row")) if editors else []


def _url_rows(scr):
    return [str(w.render()) for w in _url_widgets(scr) if w.display]


def _url_selected(scr):
    return [r for r in _url_rows(scr) if r.startswith("▸")]


def _url_hint(scr):
    """Подсказка рамки под полем «URL» (канон `_lang_hint`)."""
    from tui.editors.voice_endpoint import VoiceEndpointEditor
    editors = list(scr.query(VoiceEndpointEditor))
    hints = list(editors[-1].query(".voice-url-preset-hint")) if editors else []
    return str(hints[-1].render()) if hints else None


@pytest.mark.asyncio
async def test_preset_url_opens_only_on_alt_down_and_highlights_the_match(tmp_path):
    """Адрес из каталога — список всё равно не раскрывается на входе (канон форм
    2026-08-17: пресет-лист помощник, а не содержание поля, и раскрыт он, только пока
    значение складывается). Alt+↓ раскрывает его целиком, ▸ — на совпавшем пресете.

    До канона вход на заполненное поле раскрывал список сам, и это расходилось с
    соседним `test_own_url_does_not_auto_open_but_alt_down_opens_it`: одно и то же
    поле вело себя по-разному в зависимости от того, попал ли адрес в наш каталог."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        groq = "https://api.groq.com/openai/v1/audio/transcriptions"
        scr, _ = _mk_screen(tmp_path, OFF,
                            config_text=f"model: m\nvoice:\n  url: {groq}\n  model: whisper-1\n")
        await _open_cloud(app, scr, pilot)
        assert not _url_rows(scr)                      # на строке режима списка нет
        await _to_field(pilot, expand=False)            # в поле «URL», как это делает человек
        assert _focused_id(scr) == "voice-url"
        assert not _url_rows(scr)                      # адрес задан — список свёрнут
        await pilot.press("alt+down")                   # явная просьба показать список
        await pilot.pause()
        rows = _url_rows(scr)
        assert len(rows) == 3
        selected = _url_selected(scr)
        assert len(selected) == 1 and "api.groq.com" in selected[0]


@pytest.mark.asyncio
async def test_own_url_does_not_auto_open_but_alt_down_opens_it(tmp_path):
    """Свой адрес (не из списка) сам список не раскрывает — иначе стрелки/Enter ушли бы
    в чужой пресет поверх рабочего адреса (канон пресет-листа подключения, `can_open`);
    Alt+↓ раскрывает его явно, тем же путём, что и у языка/моделей (спека 2026-08-06 §2)."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)   # url: https://old.x/…
        await _open_cloud(app, scr, pilot)
        await _to_field(pilot, expand=False)
        assert not _url_rows(scr)
        await pilot.press("alt+down")
        await pilot.pause()
        assert len(_url_rows(scr)) == 3


@pytest.mark.asyncio
async def test_ctrl_space_probes_the_url_field_regardless_of_the_list_state(tmp_path):
    """Ctrl+Space на «URL» проверяет связь независимо от состояния списка пресетов
    (§2.2 спеки 2026-08-06: клавиши не конкурируют, порядок любой) — список свёрнут,
    раскрыт Alt+↓ явно или так и остаётся раскрытым, проба уходит в обоих случаях."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        fake.probe_result = ProbeResult(ok=True, latency_ms=10, error=None)
        await _open_cloud(app, scr, pilot)
        await _to_field(pilot, expand=False)             # url: чужой адрес, список свёрнут
        await pilot.press("ctrl+space")                  # порядок 1: проба раньше раскрытия
        assert await _wait(pilot, lambda: fake.probe_calls), \
            "ручная проверка должна уйти со свёрнутым списком"
        assert not _url_rows(scr), "проба не имеет права раскрыть список"
        fake.probe_calls.clear()
        await pilot.press("alt+down")                    # раскрыть явно — списку клавиши не мешают
        await pilot.pause()
        assert len(_url_rows(scr)) == 3
        await pilot.press("ctrl+space")                  # порядок 2: проба поверх раскрытого списка
        assert await _wait(pilot, lambda: fake.probe_calls), "ручная проверка должна уйти"
        assert len(_url_rows(scr)) == 3, "проба не имеет права закрыть список"


@pytest.mark.asyncio
async def test_picking_a_foreign_url_preset_blocks_the_save_until_a_model_is_chosen(tmp_path):
    """Пресет другого провайдера сносит «Модель» (спека §4.1: старое имя больше не
    гарантия для нового сервера) — тот же Enter, что подставил адрес, тут же пытается
    сохранить панель и натыкается на пустое поле: ошибка формы, конфиг не тронут.

    Находка финального ревью 2026-08-05: курсор/фокус обязаны уйти на «Модель» — там
    названа ошибка, — а не остаться (как раньше `editor.focus_first()`) на URL; и
    список моделей обязан честно раскрыться следом, а не быть съеденным гейтом
    одноразового не-раскрытия, взведённым этим же Enter'ом для подстановки пресета."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        fake.models = _MODELS
        await _open_cloud(app, scr, pilot)
        await _to_field(pilot)                          # url: чужой адрес, список свёрнут
        await pilot.press("alt+down")                    # раскрыть явно
        await pilot.pause()
        assert scr.query_one("#voice-model", Input).value == "whisper-old"
        await pilot.press("down")                        # подсветка — на другом пресете
        await pilot.pause()
        await pilot.press("enter")                       # подставить и попытаться сохранить
        await pilot.pause()
        assert scr.query_one("#voice-model", Input).value == ""
        assert fake.calls == []
        assert any("выберите модель" in str(w.render()) for w in scr.query(".onb-warn"))
        assert (tmp_path / "config.yml").read_text(encoding="utf-8") == CLOUD_CONFIG
        # Курсор ленты и фокус — на названном поле, не на URL.
        assert scr._voice_cursor_field() == "model"
        assert _focused_id(scr) == "voice-model"
        # Список моделей раскрылся сам, как на любом честном входе в поле (гейт не
        # съел автораскрытие — он взведён тем же Enter'ом, но погашен ДО переноса
        # курсора, см. `_voice_cloud_save`).
        assert await _wait(pilot, lambda: fake.model_calls), \
            "список моделей должен был сам уйти за данными"


@pytest.mark.asyncio
async def test_local_pane_has_no_url_list(tmp_path):
    """Локальная панель адрес показывает ФАКТОМ (его задаёт автостарт сервиса): списка
    пресетов там нет — readonly-поле список не заводит (см. `VoiceEndpointEditor`)."""
    from tui.app import TuiApp
    from tui.editors.voice_endpoint import VoiceEndpointEditor
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL)
        await _open_local(app, scr, pilot)
        ed = list(scr.query(VoiceEndpointEditor))[-1]
        assert ed.url_list is None


@pytest.mark.asyncio
async def test_footer_follows_the_url_field_and_its_list(tmp_path):
    """Футер на «URL» — про её клавиши: свёрнут — Alt+↓ и проба, форсированное
    раскрытие поверх своего адреса (находка A финального ревью 2026-08-06: строки
    видны, подсвеченной нет — §2.4) — «оставить свой адрес», фильтр нашёл пресет —
    «выбор адреса», фильтр без совпадений — общая строка «сохранить как есть» (та же,
    что у языка и моделей, спека §4) с пробой (облачная панель, спека 2026-08-06 §2.5)."""
    from textual.widgets import Input
    from tui.app import TuiApp
    from tui.screens.settings import (
        _VOICE_FOOTER_CLOUD_LIST, _VOICE_FOOTER_URL_LIST, _VOICE_FOOTER_LIST_NO_MATCH_CLOUD)
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        await _open_cloud(app, scr, pilot)
        await _to_field(pilot, expand=False)            # url: чужой адрес, список свёрнут
        assert _footer(scr) == _VOICE_FOOTER_CLOUD_LIST
        await pilot.press("alt+down")                    # раскрыть явно — поверх своего адреса
        await pilot.pause()
        # Все строки видны, но своего адреса среди них нет — подсвеченной нет вовсе
        # (initial_index → None, §2.4): Enter ничего не подставит, футер обязан звать
        # его «оставить свой адрес», а не «выбор адреса»/«подставить».
        assert _footer(scr) == _VOICE_FOOTER_LIST_NO_MATCH_CLOUD
        scr.query_one("#voice-url", Input).value = ""
        await pilot.pause()
        await _type(pilot, "openai")                     # фильтр нашёл пресет — подсветка есть
        # Глагол — тот же, что у языка/моделей (находка финального ревью 2026-08-05):
        # Enter здесь ТОЖЕ безусловно сохраняет панель, и молчать об этом — ложь.
        assert _footer(scr) == ("↑/↓ — выбор адреса · Enter — подставить и сохранить · "
                                "Esc — свернуть · Ctrl+Space — проверить связь · "
                                "PgUp/PgDn — секции")
        assert _footer(scr) == _VOICE_FOOTER_URL_LIST
        await _type(pilot, "-и-точно-больше-ничего-не-подходит")
        assert _footer(scr) == _VOICE_FOOTER_LIST_NO_MATCH_CLOUD
        # Рамка и футер обязаны звать Enter ОДНИМ глаголом (находка финального ревью
        # 2026-08-05: рамка обещала «оставить свой адрес», футер — «сохранить как
        # есть», обе строки видны разом). Enter здесь и правда сохраняет панель
        # (`_voice_cloud_save`), набранное остаётся в поле — «сохранить как есть»
        # верно для обоих смыслов разом.
        assert _url_hint(scr) == "свой адрес · Enter — сохранить как есть · Esc — свернуть"
        assert "сохранить как есть" in _footer(scr)


@pytest.mark.asyncio
async def test_configured_url_presets_override_the_default_three(tmp_path):
    """`voice_endpoint_presets` в действующем конфиге перекрывает кодовый дефолт (тот
    же контракт, что у пресетов адреса подключения, гейт §8.3) — список показывает
    ИМЕННО его, а не OpenAI/Groq/Mistral. Оверрайд заводим в конфиге ПРИЛОЖЕНИЯ, не
    конструктора экрана: вход в секцию подтягивает базу оттуда (`_sync_config_from_app`)
    и конструкторское значение немедленно перезаписывает."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M", "voice_endpoint_presets": [
        {"name": "Мой сервис", "url": "https://stt.example.local/v1/audio/transcriptions"}]})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, OFF)
        await _open_cloud(app, scr, pilot)
        await _to_field(pilot)
        rows = _url_rows(scr)
        assert len(rows) == 1
        assert "Мой сервис" in rows[0] and "stt.example.local" in rows[0]
        assert not any("openai" in r.lower() for r in rows)


# --- Del: удаление локального сервиса ---------------------------------------

@pytest.mark.asyncio
async def test_delete_on_local_row_asks_confirm_then_removes(tmp_path):
    """Канон удаления списковых экранов (/sessions — Del): подтверждение, потом снос."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, LOCAL)
        await _open_voice(app, scr, pilot)
        await pilot.press("delete")
        await pilot.pause()
        assert scr._voice_awaiting_remove and fake.calls == []
        assert "Enter — удалить" in _hint(scr) and "Esc — отмена" in _hint(scr)
        await pilot.press("enter")
        await pilot.pause()
        assert fake.calls == ["remove_local"]
        assert not scr._voice_awaiting_remove


@pytest.mark.asyncio
async def test_delete_confirm_escape_cancels(tmp_path):
    """Esc гасит баннер, remove не зван, из секции не выходим (канон «Обновления»)."""
    from tui.app import TuiApp
    from tui.screens.settings import _VOICE
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, LOCAL)
        await _open_voice(app, scr, pilot)
        await pilot.press("delete")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert not scr._voice_awaiting_remove
        assert fake.calls == []
        assert scr._view == _VOICE


@pytest.mark.asyncio
async def test_delete_confirm_reset_by_navigation_and_by_leaving(tmp_path):
    """Навигация и уход из секции сбрасывают незавершённый confirm — баннер не «протухает»."""
    from tui.app import TuiApp
    from tui.screens.settings import _MODES, _VOICE
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, LOCAL)
        await _open_voice(app, scr, pilot)
        await pilot.press("delete")
        await pilot.pause()
        await pilot.press("down")                        # навигация
        await pilot.pause()
        assert not scr._voice_awaiting_remove and fake.calls == []
        await pilot.press("up")
        await pilot.press("delete")
        await pilot.pause()
        assert scr._voice_awaiting_remove
        scr._open_section(_MODES)
        scr._open_section(_VOICE)
        await pilot.pause()
        assert not scr._voice_awaiting_remove and fake.calls == []


@pytest.mark.asyncio
async def test_confirm_dropped_when_focus_goes_into_fields(tmp_path):
    """Мышью в поле можно попасть мимо навигации, которая confirm сбрасывает: уход
    фокуса в редактор обязан гасить баннер, иначе Enter после набора сносит сервис."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, LOCAL)
        await _open_voice(app, scr, pilot)
        await pilot.press("delete")
        await pilot.pause()
        assert scr._voice_awaiting_remove
        lang = scr.query_one("#voice-lang", Input)
        lang.focus()                                     # как клик мышью по полю
        await pilot.pause()
        assert not scr._voice_awaiting_remove
        assert "Enter — удалить" not in _hint(scr)
        lang.value = "en"
        await pilot.press("enter")
        await pilot.pause()
        assert fake.calls == ["set_local_language"]       # сохранение, а не снос


@pytest.mark.asyncio
async def test_delete_on_cloud_row_is_noop(tmp_path):
    """На строке «Свой endpoint» Del не сносит локальный сервис (и ничего не спрашивает)."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, LOCAL)
        await _open_cloud(app, scr, pilot)
        await pilot.press("delete")
        await pilot.pause()
        assert not scr._voice_awaiting_remove and fake.calls == []


@pytest.mark.asyncio
async def test_delete_without_battery_is_noop(tmp_path):
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF)
        await _open_voice(app, scr, pilot)
        await pilot.press("delete")
        await pilot.pause()
        assert not scr._voice_awaiting_remove and fake.calls == []


@pytest.mark.asyncio
async def test_delete_inside_fields_edits_text_not_service(tmp_path):
    """Фокус в поле — Del правит текст: секция не имеет права перехватывать его."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, LOCAL)
        await _open_cloud(app, scr, pilot)
        url = scr.query_one("#voice-url", Input)
        url.value = "abc"
        url.focus()
        await pilot.pause()
        url.cursor_position = 0          # снять выделение select_on_focus
        await pilot.press("delete")
        await pilot.pause()
        assert not scr._voice_awaiting_remove and fake.calls == []
        assert url.value == "bc"


# --- Ctrl+Space: проверка endpoint -------------------------------------------

@pytest.mark.asyncio
async def test_ctrl_space_probes_endpoint(tmp_path):
    from tui.app import TuiApp
    from textual.widgets import Input, Static
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF)
        fake.probe_result = ProbeResult(ok=True, latency_ms=340, error=None)
        await _open_cloud(app, scr, pilot)
        scr.query_one("#voice-url", Input).value = _URL
        await pilot.press("ctrl+space")
        await pilot.pause()
        assert "340" in str(scr.query_one(".set-probe", Static).render())


@pytest.mark.asyncio
async def test_probe_uses_the_saved_token_when_the_field_is_empty(tmp_path):
    """Поле токена — секрет, оно не префиллено: пустое значит «тот же, что сохранён»
    (правило Enter-сохранения). Проверка обязана слать ТОТ ЖЕ токен, иначе она отвечает
    401 там, где Enter сохранил бы рабочую связку."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, CLOUD, config_text=CLOUD_CONFIG)
        fake.probe_result = ProbeResult(ok=True, latency_ms=10, error=None)
        await _open_cloud(app, scr, pilot)
        await pilot.press("ctrl+space")
        await pilot.pause()
        assert fake.probe_calls == [("https://old.x/v1/audio/transcriptions",
                                     "old-token", "whisper-old")]


@pytest.mark.asyncio
async def test_voice_requests_carry_the_agent_proxy(tmp_path):
    """За корп-прокси наружу выпускает только он: если у агента задан backend.proxy,
    облачный STT обязан идти тем же путём, что и запросы к модели — иначе проверка и
    список моделей молча упираются в таймаут там, где LLM-поля работают."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M", "proxy": "http://corp:3128"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        fake.probe_result = ProbeResult(ok=True, latency_ms=10, error=None)
        fake.models = _MODELS
        await _open_model_field(app, scr, pilot)
        await pilot.press("escape")                    # свернуть список — курсор вернуть на поле
        await pilot.press("up")                        # на «Токен» — поле без списка
        await pilot.press("ctrl+space")                # …и здесь клавиша проверяет связь
        await pilot.pause()
    assert fake.model_proxies == ["http://corp:3128"]
    assert fake.probe_proxies == ["http://corp:3128"]


@pytest.mark.asyncio
async def test_cloud_pane_tells_about_the_agent_proxy(tmp_path):
    """Прокси задаётся на «Подключении», рядом с адресом модели — там связь видна.
    У голоса свой экран и свой адрес, и то, что наружу он идёт через тот же прокси,
    угадать неоткуда: говорим об этом прямо."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M", "proxy": "http://corp:3128"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        await _open_cloud(app, scr, pilot)
        line = " ".join(str(w.render()) for w in scr.query(".set-voiceproxy"))
    assert "прокси" in line


@pytest.mark.asyncio
async def test_no_proxy_line_without_a_proxy(tmp_path):
    """Прокси не задан — лишней строки нет: у большинства её нечем наполнить."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        await _open_cloud(app, scr, pilot)
        assert not scr.query(".set-voiceproxy")


@pytest.mark.asyncio
async def test_local_pane_never_mentions_the_proxy(tmp_path):
    """Локальная батарейка живёт на этой же машине и идёт мимо прокси — упоминание
    его в этой панели было бы прямой ложью."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M", "proxy": "http://corp:3128"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL)
        await _open_local(app, scr, pilot)
        assert not scr.query(".set-voiceproxy")


@pytest.mark.asyncio
async def test_probe_carries_voice_verify_ssl(tmp_path):
    """voice.verify_ssl (корп-CA) действует на транскрибацию и на список моделей —
    проверка обязана идти с ним же, иначе она пугает «ошибкой TLS» там, где голос
    работает."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M", "voice": {"verify_ssl": "/ca.pem"}})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        fake.probe_result = ProbeResult(ok=True, latency_ms=10, error=None)
        await _open_cloud(app, scr, pilot)
        await pilot.press("ctrl+space")
        await pilot.pause()
    assert fake.probe_verifies == ["/ca.pem"]


@pytest.mark.asyncio
async def test_a_token_typed_into_the_field_wins_over_the_saved_one(tmp_path):
    """Набранный токен — намерение сменить его: проверяем именно его, а не сохранённый."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, CLOUD, config_text=CLOUD_CONFIG)
        fake.probe_result = ProbeResult(ok=True, latency_ms=10, error=None)
        await _open_cloud(app, scr, pilot)
        scr.query_one("#voice-token", Input).value = "new-token"
        await pilot.press("ctrl+space")
        await pilot.pause()
        assert fake.probe_calls[-1][1] == "new-token"


@pytest.mark.asyncio
async def test_ctrl_space_in_local_pane_probes_nothing(tmp_path):
    """В локальной панели проверять нечего: адрес не вводится, а жив ли сервис —
    говорит хвост его строки."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, LOCAL)
        await _open_voice(app, scr, pilot)
        await pilot.press("ctrl+space")
        await pilot.pause()
        assert fake.calls == [] and not scr.query(".set-probe")


@pytest.mark.asyncio
async def test_leaving_the_cloud_pane_drops_a_flying_probe(tmp_path):
    """Ушли с «Своего endpoint», пока проверка летит: её вердикт и волна относятся к
    полям, которых на экране больше нет — иначе тик волны смонтировал бы probe-строку
    в локальную панель, где проверять нечего."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, LOCAL)
        fake.probe_endpoint = lambda url, token, model, timeout=10, **kw: None
        await _open_cloud(app, scr, pilot)
        scr.query_one("#voice-url", Input).value = _URL
        scr._voice_probe()                               # без pause — probe «в полёте»
        assert scr._voice_probe_in_flight
        await pilot.press("up")                          # на «Локальный сервис»
        await pilot.pause()
        assert not scr._voice_probe_in_flight
        scr._wave_tick()
        await pilot.pause()
        assert not scr.query(".set-probe")


@pytest.mark.asyncio
async def test_probe_without_url_says_what_to_do(tmp_path):
    """Ручная проверка с пустым URL не молчит: строка probe объясняет, чего не хватает
    (канон draft-probe «Подключения»)."""
    from tui.app import TuiApp
    from textual.widgets import Static
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF)
        await _open_cloud(app, scr, pilot)
        await pilot.press("ctrl+space")
        await pilot.pause()
        assert "сначала укажите адрес endpoint" in \
            str(scr.query_one(".set-probe", Static).render())
        assert fake.calls == []


@pytest.mark.asyncio
async def test_probe_with_an_unselected_filter_says_what_to_do(tmp_path):
    """§2.3 спеки 2026-08-06: пока список URL раскрыт, в поле может лежать ФИЛЬТР
    (`gr`), а не адрес — Ctrl+Space не бьёт пробой по нему вслепую, строка probe
    говорит, что адрес ещё не выбран (своя формулировка, как и просит уточнение
    задачи — отличная от «Подключения»)."""
    from tui.app import TuiApp
    from textual.widgets import Input, Static
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        await _open_cloud(app, scr, pilot)
        await _to_field(pilot)                          # url: чужой адрес, список свёрнут
        scr.query_one("#voice-url", Input).value = "gr"  # непустой невалидный текст
        await pilot.pause()
        await pilot.press("ctrl+space")
        await pilot.pause()
        assert "адрес" in str(scr.query_one(".set-probe", Static).render())
        assert fake.calls == []


@pytest.mark.asyncio
async def test_ctrl_space_on_an_env_reference_still_probes(tmp_path):
    """Регресс ревью (фикс-раунд 1): облачная панель голоса тоже держит СЫРОЕ
    значение файла (`_voice_raw_cloud`, без резолва) — `${ENV:GC_URL}` там штатное
    состояние адреса, не фильтр. §2.3 бракует её так же, как «gr», но блокировать
    пробу здесь нельзя — та же дыра, что чинили у «Подключения». Ctrl+Space обязан
    довести до `_voice_probe`, реальным нажатием на строке режима (список URL не
    раскрыт — курсор туда не заходил)."""
    from tui.app import TuiApp
    from textual.widgets import Static
    config_text = ("model: m\nvoice:\n  url: \"${ENV:GC_URL}\"\n"
                   "  token: old-token\n  model: whisper-old\n")
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF, config_text=config_text)
        await _open_cloud(app, scr, pilot)
        await pilot.press("ctrl+space")
        assert await _wait(pilot, lambda: fake.probe_calls), \
            "ссылка обязана дойти до пробы, а не быть принятой за фильтр"
        assert fake.probe_calls[0][0] == "${ENV:GC_URL}"
        probe_line = str(scr.query_one(".set-probe", Static).render())
        assert "сначала выберите адрес" not in probe_line


@pytest.mark.asyncio
async def test_probe_verdict_dropped_when_values_change(tmp_path):
    """Правка полей обесценивает вердикт (канон _reset_draft_state). Сравниваем ЗНАЧЕНИЯ,
    а не факт события: ре-рендер пересоздаёт поля и сам рождает Input.Changed — по нему
    вердикт гас бы на ровном месте."""
    from tui.app import TuiApp
    from textual.widgets import Input, Static
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        fake.probe_result = ProbeResult(ok=True, latency_ms=42, error=None)
        await _open_cloud(app, scr, pilot)
        await pilot.press("ctrl+space")
        await pilot.pause()
        assert "42" in str(scr.query_one(".set-probe", Static).render())
        scr._render_view()                               # пересоздание полей — не правка
        await pilot.pause()
        assert "42" in str(scr.query_one(".set-probe", Static).render())
        scr.query_one("#voice-url", Input).value = "https://other.x/v1/audio/transcriptions"
        await pilot.pause()
        assert "не проверено" in str(scr.query_one(".set-probe", Static).render())
        assert scr._voice_probe_result is None


@pytest.mark.asyncio
async def test_voice_editor_is_the_freshly_mounted_one(tmp_path):
    """После _render_view старый редактор ещё в дереве (remove() отложенный): работать
    надо со свежим, иначе submit/values читают обречённые поля."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, OFF)
        await _open_cloud(app, scr, pilot)
        before = scr._voice_editor()
        scr._render_view()                               # без pause: удаление отложено
        assert scr._voice_editor() is not before


@pytest.mark.asyncio
async def test_probe_in_flight_animates_like_connection(tmp_path):
    """«В полёте» — та же волна, что у server-probe «Подключения», а не статичная строка."""
    from tui.app import TuiApp
    from textual.widgets import Input, Static
    from tui.widgets.loading_wave import wave_glyph
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF)
        fake.probe_endpoint = lambda url, token, model, timeout=10, **kw: None
        await _open_cloud(app, scr, pilot)
        scr.query_one("#voice-url", Input).value = _URL
        scr._voice_probe()                               # без pause: воркер не прилетел
        assert scr._voice_probe_in_flight
        assert scr._wave_timer is not None
        line = scr._voice_probe_line()
        assert line == wave_glyph(scr._wave_i)
        before = str(scr.query_one(".set-probe", Static).render())
        scr._wave_tick()
        assert scr._voice_probe_line() != line            # анимация, не статика
        assert str(scr.query_one(".set-probe", Static).render()) == before


@pytest.mark.asyncio
async def test_probe_verdict_dropped_on_leaving_section(tmp_path):
    """Уход из секции обесценивает вердикт и бампает generation: стухший воркер не
    ляжет на свежие поля, а повторный вход показывает чистую строку."""
    from tui.app import TuiApp
    from tui.screens.settings import _MODES, _VOICE
    from textual.widgets import Input, Static
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF)
        fake.probe_result = ProbeResult(ok=True, latency_ms=42, error=None)
        await _open_cloud(app, scr, pilot)
        scr.query_one("#voice-url", Input).value = _URL
        await pilot.press("ctrl+space")
        await pilot.pause()
        assert scr._voice_probe_result is not None
        gen = scr._voice_probe_gen
        scr._open_section(_MODES)
        scr._open_section(_VOICE)
        await pilot.pause()
        assert scr._voice_probe_result is None and not scr._voice_probe_in_flight
        assert scr._voice_probe_gen > gen


@pytest.mark.asyncio
async def test_save_while_probe_in_flight_leaves_single_probe_line(tmp_path):
    """Enter (сохранить) во время летящего probe: тик волны не имеет права смонтировать
    вторую probe-строку (`_set_probe_line` умеет монтировать) поверх секции, а сам
    летящий probe — лечь вердиктом уже после сохранения (generation бампается)."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF)
        fake.probe_endpoint = lambda url, token, model, timeout=10, **kw: None
        await _open_cloud(app, scr, pilot)
        scr.query_one("#voice-url", Input).value = _URL
        scr.query_one("#voice-model", Input).value = "whisper-1"
        scr._voice_probe()                               # без pause — probe «в полёте»
        gen = scr._voice_probe_gen
        scr._voice_cloud_save()
        assert scr._voice_probe_gen > gen and not scr._voice_probe_in_flight
        await pilot.pause()
        scr._wave_tick()
        await pilot.pause()
        assert len(scr.query(".set-probe")) == 1


# --- плашка результата не теряется -------------------------------------------

@pytest.mark.asyncio
async def test_save_flash_visible_after_install_and_mouse_click(tmp_path, monkeypatch):
    """Преамбула on_key гасит прошлую плашку через remove() (отложенный), и следующая
    плашка того же такта ложилась в обречённый виджет — исчезала вместе с ним. Путь без
    промежуточной клавиши (мышью в поле) вскрывает это: подтверждение сохранения не видно."""
    from tui.app import TuiApp
    from textual.widgets import Input
    import voice_runtime.autostart as autostart
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        seen = []
        monkeypatch.setattr(autostart, "ensure_voice_service",
                            lambda cfg: seen.append(cfg) or True)
        scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)

        def _switch(echo=print, config=None):
            fake.calls.append("switch_to_local")
            fake.status = LOCAL
            return True
        fake.switch_to_local = _switch
        await _open_voice(app, scr, pilot)
        await _install_local(pilot)                       # установка → своя плашка
        assert await _wait(pilot, lambda: bool(seen)), "воркер подъёма не стартовал"
        await _wait_flash(pilot, scr, "локальный сервис подключён")

        await pilot.press("down")                        # к своему endpoint
        await pilot.pause()
        scr.query_one("#voice-url", Input).focus()       # клик мышью — БЕЗ нажатия клавиши
        await pilot.pause()
        await pilot.press("enter")                       # сохранить endpoint
        await pilot.pause()
        await pilot.pause()
        assert fake.calls == ["switch_to_local", "wire_cloud"]
        flash = [str(w.render()) for w in scr.query("#set-flash")]
        assert flash and "свой endpoint подключён" in flash[-1]


@pytest.mark.asyncio
async def test_second_save_flash_visible_without_keys_in_between(tmp_path):
    """Два сохранения подряд: плашка второго тоже обязана быть видна."""
    from tui.app import TuiApp
    from textual.widgets import Static
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        await _open_cloud(app, scr, pilot)
        await pilot.press("enter")
        await pilot.pause()
        assert "свой endpoint подключён" in str(scr.query_one("#set-flash", Static).render())
        await pilot.press("enter")                       # второе сохранение подряд
        await pilot.pause()
        await pilot.pause()
        flash = [str(w.render()) for w in scr.query("#set-flash")]
        assert flash and "свой endpoint подключён" in flash[-1]
        assert fake.calls == ["wire_cloud", "wire_cloud"]


# --- suspend вокруг install/remove ------------------------------------------

class _FakeSuspend:
    """Подмена app.suspend(): пишет в лог вход/выход и умеет бросать в заданной точке."""

    def __init__(self, log, *, on_enter=None, on_exit=None):
        self._log = log
        self._on_enter = on_enter
        self._on_exit = on_exit

    def __call__(self):
        return self

    def __enter__(self):
        self._log.append("enter")
        if self._on_enter is not None:
            raise self._on_enter
        return None

    def __exit__(self, *exc):
        self._log.append("exit")
        if self._on_exit is not None:
            raise self._on_exit
        return False


async def _confirmed_remove(pilot, app, tmp_path, suspend):
    scr, fake = _mk_screen(tmp_path, LOCAL)
    app.suspend = suspend
    await _open_voice(app, scr, pilot)
    await pilot.press("delete")                          # confirm-баннер
    await pilot.pause()
    await pilot.press("enter")                           # подтверждение → выполнение
    await pilot.pause()
    return scr, fake


@pytest.mark.asyncio
async def test_remove_runs_under_suspend(tmp_path):
    """Вывод gc-voice идёт в обычный терминал — операция ВНУТРИ app.suspend()."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        log = []
        scr, fake = await _confirmed_remove(pilot, app, tmp_path, _FakeSuspend(log))
        assert log == ["enter", "exit"]
        assert fake.calls == ["remove_local"]


@pytest.mark.asyncio
async def test_suspend_failing_after_operation_does_not_repeat_it(tmp_path):
    """Исключение из suspend.__exit__ прилетает УЖЕ после операции — fallback не имеет
    права прогнать её второй раз."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        log = []
        scr, fake = await _confirmed_remove(
            pilot, app, tmp_path, _FakeSuspend(log, on_exit=RuntimeError("no suspend")))
        assert fake.calls == ["remove_local"]


@pytest.mark.asyncio
async def test_suspend_unsupported_falls_back_once_and_survives_failure(tmp_path):
    """Драйвер без suspend: операция выполняется один раз, её падение экран не роняет."""
    from tui.app import TuiApp
    from textual.widgets import Static
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        log = []
        calls = []

        def _boom():
            calls.append("remove_local")
            raise RuntimeError("gc-voice сломался")
        scr, fake = _mk_screen(tmp_path, LOCAL)
        fake.remove_local = _boom
        app.suspend = _FakeSuspend(log, on_enter=RuntimeError("no suspend"))
        await _open_voice(app, scr, pilot)
        await pilot.press("delete")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert calls == ["remove_local"]
        flash = str(scr.query_one("#set-flash", Static).render())
        assert flash.startswith("⚠") and "не удалось" in flash


@pytest.mark.asyncio
async def test_install_suspend_failing_after_operation_does_not_repeat(tmp_path):
    """Тот же контракт для установки, где цена повтора максимальна (~0.9 ГБ)."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        log = []
        scr, fake = _mk_screen(tmp_path, OFF)
        app.suspend = _FakeSuspend(log, on_exit=RuntimeError("no suspend"))
        await _open_voice(app, scr, pilot)
        await _install_local(pilot)
        await pilot.pause()
        assert fake.calls == ["switch_to_local"]


# --- подъём сервиса воркером и гейт колбэка ---------------------------------

async def _wait_flash(pilot, scr, needle, tries=60):
    from textual.widgets import Static
    from textual.css.query import NoMatches
    for _ in range(tries):
        await pilot.pause()
        try:
            text = str(scr.query_one("#set-flash", Static).render())
        except NoMatches:
            continue
        if needle in text:
            return text
    raise AssertionError(f"плашка с «{needle}» не появилась")


async def _wait(pilot, cond, tries=60):
    for _ in range(tries):
        await pilot.pause()
        if cond():
            return True
    return False


@pytest.mark.asyncio
async def test_service_start_does_not_block_ui(tmp_path, monkeypatch):
    """ensure_voice_service блокирующе ждёт готовности (до 15 с) — подъём уходит на
    воркер, обработчик клавиши возвращается сразу."""
    import threading
    import time
    from tui.app import TuiApp
    import voice_runtime.autostart as autostart
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def _slow(cfg):
            calls.append(cfg)
            entered.set()
            release.wait(10)
            return True
        monkeypatch.setattr(autostart, "ensure_voice_service", _slow)
        scr, fake = _mk_screen(tmp_path, OFF)
        await _open_voice(app, scr, pilot)

        scr._voice_enter()                   # спрашивает подтверждение установки
        t0 = time.monotonic()
        scr._voice_confirm_install()         # и только оно запускает установку с подъёмом
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0, "UI-поток ждал подъёма сервиса"
        assert await _wait(pilot, entered.is_set), "воркер подъёма не стартовал"
        assert len(calls) == 1

        release.set()
        text = await _wait_flash(pilot, scr, "подключён")
        assert "не запустился" not in text
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_service_not_started_flashes_hint(tmp_path, monkeypatch):
    """Сервис не поднялся — честная плашка со ссылкой на gc-voice status."""
    from tui.app import TuiApp
    import voice_runtime.autostart as autostart
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        monkeypatch.setattr(autostart, "ensure_voice_service", lambda cfg: False)
        scr, fake = _mk_screen(tmp_path, OFF)
        await _open_voice(app, scr, pilot)
        await _install_local(pilot)
        flash = await _wait_flash(pilot, scr, "gc-voice status")
        assert "gc-voice status" in flash


async def _install_with_slow_service(pilot, app, tmp_path, monkeypatch, status=OFF):
    """Секция «Голосовой ввод» с летящим (заблокированным) подъёмом сервиса."""
    import threading
    import voice_runtime.autostart as autostart
    entered = threading.Event()
    release = threading.Event()

    def _slow(cfg):
        entered.set()
        release.wait(10)
        return True
    monkeypatch.setattr(autostart, "ensure_voice_service", _slow)
    scr, fake = _mk_screen(tmp_path, status)
    await _open_local(app, scr, pilot)      # установка — действие локальной строки
    scr._voice_enter()
    if scr._voice_awaiting_install:         # батарейки нет — Enter спросил, подтверждаем
        scr._voice_confirm_install()
    assert await _wait(pilot, entered.is_set), "воркер подъёма не стартовал"
    return scr, fake, release


@pytest.mark.asyncio
async def test_service_callback_does_not_rerender_while_typing(tmp_path, monkeypatch):
    """Вердикт подъёма кладём точечно: поля endpoint не пересоздаются, набранное и
    каретка целы — человек мог печатать все эти 15 секунд."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake, release = await _install_with_slow_service(pilot, app, tmp_path, monkeypatch)
        await pilot.press("down")                          # к своему endpoint
        await pilot.pause()
        url = scr.query_one("#voice-url", Input)
        url.value = "https://draft.x/v1/audio/transcriptions"
        url.focus()
        await pilot.pause()
        editor_before = scr._voice_editor()

        release.set()
        await _wait_flash(pilot, scr, "подключён")
        assert app.is_running                              # TUI жив (не упал на DuplicateIds)
        assert scr._voice_editor() is editor_before        # поля НЕ пересобирались
        assert len(scr.query("#voice-url")) == 1           # второго комплекта полей нет
        assert scr.query_one("#voice-url", Input).value == \
            "https://draft.x/v1/audio/transcriptions"      # черновик цел
        assert scr.focused is url                          # фокус не сбит


@pytest.mark.asyncio
async def test_service_callback_does_not_touch_foreign_section(tmp_path, monkeypatch):
    """Юзер ушёл в соседнюю секцию: колбэк не перерисовывает ЧУЖУЮ секцию."""
    from tui.app import TuiApp
    from tui.screens.settings import _MODES
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        toasts = []
        monkeypatch.setattr(app, "notify",
                            lambda msg, **kw: toasts.append(msg), raising=False)
        scr, fake, release = await _install_with_slow_service(pilot, app, tmp_path, monkeypatch)
        await pilot.press("pagedown")
        await pilot.pause()
        assert scr._view == _MODES
        renders = []
        orig = scr._render_view
        monkeypatch.setattr(scr, "_render_view",
                            lambda: renders.append(1) or orig(), raising=False)

        release.set()
        assert await _wait(pilot, lambda: bool(toasts)), "итог подъёма не показан"
        assert renders == []
        assert scr._view == _MODES


@pytest.mark.asyncio
async def test_service_callback_does_not_rerender_under_confirm(tmp_path, monkeypatch):
    """Висит confirm удаления — ре-рендер снёс бы баннер из-под руки: только toast."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        toasts = []
        monkeypatch.setattr(app, "notify",
                            lambda msg, **kw: toasts.append(msg), raising=False)
        # Батарейка уже стоит, режим был облачный: Enter вернул локальный, подъём летит,
        # и Del по видимой строке «установлен» открывает confirm ровно как обычно.
        scr, fake, release = await _install_with_slow_service(
            pilot, app, tmp_path, monkeypatch, status=LOCAL_INSTALLED_CLOUD)
        await pilot.press("delete")
        await pilot.pause()
        assert scr._voice_awaiting_remove

        release.set()
        assert await _wait(pilot, lambda: bool(toasts)), "итог подъёма не показан"
        assert scr._voice_awaiting_remove                  # баннер на месте
        assert "Enter — удалить" in _hint(scr)


@pytest.mark.asyncio
async def test_service_callback_updates_status_in_place_keeping_draft(tmp_path, monkeypatch):
    """Гейт по фокусу не спасает черновик: курсор мог уйти со строки полей (фокус снят),
    а набранное в них осталось — и до 15 с ждёт вердикта подъёма. Колбэк обязан обновлять
    статус/строки/подсказку НА МЕСТЕ, а не пересобирать секцию: поля не пересоздаются."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake, release = await _install_with_slow_service(pilot, app, tmp_path, monkeypatch)
        await pilot.press("down")                          # к своему endpoint
        await _to_field(pilot)                             # в поля
        scr.query_one("#voice-url", Input).value = "https://draft.x/v1/audio/transcriptions"
        editor_before = scr._voice_editor()

        fake.status = LOCAL                                # установка завершилась, сервис жив
        release.set()
        await _wait_flash(pilot, scr, "подключён")
        assert scr._voice_editor() is editor_before        # поля НЕ пересобирались
        assert scr.query_one("#voice-url", Input).value == \
            "https://draft.x/v1/audio/transcriptions"      # черновик цел
        assert len(scr.query("#voice-url")) == 1
        assert "установлен · запущен · порт 8735" in _rows(scr)[0]   # статус обновился
        assert len(_rows(scr)) == 2


# --- Reopen: раскрыть свёрнутый список без выхода из поля --------------------
# После Esc список свёрнут, а раскрыть его нечем (курсор остаётся в поле, но не в
# самой ленте) — печать и Ctrl+Space дают два пути назад, не выходя из поля.

@pytest.mark.asyncio
async def test_typing_after_escape_reopens_the_list_and_filters(tmp_path):
    """Esc свернул список (курсор остался в поле) — печать раскрывает его заново и сразу
    фильтрует набранным, тем же действием, что и обычная печать в открытом списке."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL)
        await _open_local(app, scr, pilot)
        await _to_field(pilot)
        await pilot.press("escape")
        await pilot.pause()
        assert not _lang_rows(scr)                        # свёрнут — раскрыть было нечем
        scr.query_one("#voice-lang", Input).value = ""
        await pilot.pause()
        await _type(pilot, "нем")
        assert _lang_rows(scr) == ["▸ de    немецкий"]
        assert _focused_id(scr) == "voice-lang"


@pytest.mark.asyncio
async def test_alt_down_reopens_the_list_without_a_filter(tmp_path):
    """Alt+↓ на поле «Язык» со свёрнутым списком раскрывает его явно, ничего не
    печатая: полный список, курсор на действующем коде, фокус остаётся в поле.
    Локальная панель — Ctrl+Space там про пробу молчит (§2.5 спеки 2026-08-06,
    пробы нет по §9.2 канона), а список всё равно раскрывается — своей клавишей."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, LOCAL)
        fake.language = "en"
        await _open_local(app, scr, pilot)
        await _to_field(pilot)
        await pilot.press("escape")
        await pilot.pause()
        assert not _lang_rows(scr)
        await pilot.press("alt+down")
        await pilot.pause()
        assert len(_lang_rows(scr)) == 100
        assert _lang_selected(scr) == ["▸ en    английский"]
        assert _focused_id(scr) == "voice-lang"


@pytest.mark.asyncio
async def test_ctrl_space_is_a_noop_when_the_list_is_already_open(tmp_path):
    """Локальная панель, список уже раскрыт — Ctrl+Space съедается молча (пробы там
    нет, §2.5/§9.2 спеки 2026-08-06): список не трогает, строки не задваивает."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL)
        await _open_local(app, scr, pilot)
        await _to_field(pilot)
        assert len(_lang_rows(scr)) == 100
        await pilot.press("ctrl+@")
        await pilot.pause()
        assert len(_lang_rows(scr)) == 100                 # не задвоились
        assert _focused_id(scr) == "voice-lang"


@pytest.mark.asyncio
async def test_escape_after_typing_reopen_restores_the_pre_edit_code(tmp_path):
    """Раскрытие по печати не путается с restore-механикой: `_value0` остаётся тем, что
    было ДО правки, и второй Esc возвращает именно его, а не то, что напечатали заново."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL)
        await _open_local(app, scr, pilot)
        await _to_field(pilot)
        assert scr.query_one("#voice-lang", Input).value == "ru"
        await pilot.press("escape")
        await pilot.pause()
        scr.query_one("#voice-lang", Input).value = ""
        await pilot.pause()
        await _type(pilot, "нем")
        assert _lang_rows(scr) == ["▸ de    немецкий"]
        await pilot.press("escape")
        await pilot.pause()
        assert scr.query_one("#voice-lang", Input).value == "ru"   # не "нем", не ""
        assert not _lang_rows(scr)


@pytest.mark.asyncio
async def test_footer_hints_alt_down_when_collapsed_on_the_local_language_field(tmp_path):
    from tui.app import TuiApp
    from tui.editors.field_list import LIST_CLOSED_HINT
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL)
        await _open_local(app, scr, pilot)
        await _to_field(pilot)
        assert LIST_CLOSED_HINT not in _footer(scr)  # раскрыт — футер про список
        await pilot.press("escape")
        await pilot.pause()
        assert LIST_CLOSED_HINT in _footer(scr)
        assert "Enter — сохранить язык" in _footer(scr)   # прежний текст никуда не делся
        await pilot.press("alt+down")
        await pilot.pause()
        assert LIST_CLOSED_HINT not in _footer(scr)  # снова раскрыт — футер списочный


@pytest.mark.asyncio
async def test_footer_hints_alt_down_and_ctrl_space_when_collapsed_on_the_cloud_language_field(tmp_path):
    """Сохранённый адрес — чужой пресетам (`CLOUD_CONFIG`), и вход на «URL» список сам
    не раскрывает (`can_open` гейтит автораскрытие, спека 2026-08-05 §3) — хинт «Ctrl+Space
    — раскрыть список» там честен и должен быть виден сразу, как и у языка ниже. У
    «Токена» списка нет вовсе — хинта нет и не будет. У «Модели» список пытается
    раскрыться, но сервер не ответил (`fake.models` не задан) — пустой результат даёт
    футер «сохранить как есть», тоже без этого хинта (см. `_VOICE_FOOTER_LIST_NO_MATCH_CLOUD`).

    Проба (спека 2026-08-06 §2.2, §2.5) теперь работает и здесь: список свёрнут, но
    Ctrl+Space про него больше не спрашивает — он сразу проверяет связь, и молчать об
    этом в футере значило бы не назвать работающую клавишу."""
    from tui.app import TuiApp
    from tui.editors.field_list import LIST_CLOSED_HINT
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        await _open_cloud(app, scr, pilot)
        await _to_field(pilot, expand=False)               # url — чужой адрес, список свёрнут
        assert LIST_CLOSED_HINT in _footer(scr)
        await pilot.press("down")                          # token — списка у него нет вовсе
        await pilot.pause()
        assert LIST_CLOSED_HINT not in _footer(scr)
        await pilot.press("down")                          # model — сервер не ответил
        await pilot.pause()
        assert LIST_CLOSED_HINT not in _footer(scr)
        await pilot.press("down")                          # voice-lang
        await pilot.pause()
        assert _focused_id(scr) == "voice-lang"
        await _expand_list(pilot)                          # код задан — раскрываем Alt+↓
        assert LIST_CLOSED_HINT not in _footer(scr)
        await pilot.press("escape")
        await pilot.pause()
        assert LIST_CLOSED_HINT in _footer(scr)
        assert "Enter — сохранить" in _footer(scr)
        assert "Ctrl+Space — проверить связь" in _footer(scr)   # проба работает и здесь


@pytest.mark.asyncio
async def test_ctrl_space_outside_the_language_field_does_nothing(tmp_path):
    """Ctrl+Space вне поля «Язык» (строка режима, другое поле) списка не раскрывает —
    секция и фокус остаются как были. То, что вместо раскрытия клавиша перепроверяет
    связь (§5.1), стережёт `test_ctrl_space_on_a_plain_voice_field_reprobes`."""
    from tui.app import TuiApp
    from tui.screens.settings import _VOICE
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, LOCAL)
        await _open_voice(app, scr, pilot)                # курсор на строке режима
        await pilot.press("ctrl+@")
        await pilot.pause()
        assert not _lang_rows(scr)
        assert scr._view == _VOICE

        app.pop_screen()
        await pilot.pause()
        scr2, _ = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        await _open_cloud(app, scr2, pilot)
        await _to_field(pilot)                             # в URL, не в «Языке»
        assert _focused_id(scr2) == "voice-url"
        await pilot.press("ctrl+@")
        await pilot.pause()
        assert not _lang_rows(scr2)
        assert _focused_id(scr2) == "voice-url"            # курсор с поля не сдвинулся


@pytest.mark.asyncio
async def test_esc_leaves_a_hint_on_how_to_reopen_the_list(tmp_path):
    """Свернули список Esc'ом — на его месте остаётся строка о том, чем раскрыть обратно.
    Без неё поле молчит: список исчез, и как его вернуть, взять неоткуда.

    Список моделей сетевой — свёрнутое состояние зовёт клавишу тем же глаголом, что и
    раскрытое («перечитать список»), а не канон-словом локальных списков («показать»,
    находка C финального ревью 2026-08-06)."""
    from tui.app import TuiApp
    from tui.editors.voice_endpoint import _MODEL_RELOAD_HINT
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        fake.models = _MODELS
        await _open_model_field(app, scr, pilot)
        assert _model_rows(scr)                       # список раскрыт
        await pilot.press("escape")
        await pilot.pause()
        assert not _model_rows(scr)
        assert _model_hint(scr) == _MODEL_RELOAD_HINT
        await pilot.press("alt+down")                 # и она не врёт: список возвращается
        await _wait(pilot, lambda: bool(_model_rows(scr)))
        assert _model_hint(scr) != _MODEL_RELOAD_HINT


@pytest.mark.asyncio
async def test_the_reopen_hint_stays_under_its_field(tmp_path):
    """Ушли с поля — подсказка остаётся под ним (канон форм 2026-08-17).

    Она ярлык способа («вот чем список раскрывается»), а не команда момента: что
    нажимать сейчас, отвечает футер — он обещает `Alt+↓` только на своём поле. До
    канона голосовой экран снимал строку вслед за курсором, и правило расходилось
    с «Подключением», где она стоит постоянно."""
    from tui.app import TuiApp
    from tui.editors.voice_endpoint import _MODEL_RELOAD_HINT
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        fake.models = _MODELS
        await _open_model_field(app, scr, pilot)
        await pilot.press("escape")
        await pilot.pause()
        assert _model_hint(scr) == _MODEL_RELOAD_HINT
        await pilot.press("up")                       # курсор — на предыдущее поле
        await pilot.pause()
        assert _model_hint(scr) == _MODEL_RELOAD_HINT


def test_open_list_hint_offers_refresh_not_reopen():
    """У раскрытого списка Alt+↓ перечитывает модели с сервера — так и написано (спека
    2026-08-06 §2, §4.1: роль ушла с Ctrl+Space, хинт её больше не называет); «вернуть»
    там было бы про действие, которое в этот момент не нужно."""
    from tui.editors.field_list import LIST_CLOSED_HINT as _LIST_CLOSED_HINT
    from tui.editors.voice_endpoint import _MODEL_HINT, _LANG_HINT
    assert "Alt+↓ — перечитать список" in _MODEL_HINT
    assert "Ctrl+Space" not in _MODEL_HINT
    assert "Ctrl+Space" not in _LANG_HINT          # локальный список перечитывать неоткуда
    assert _LIST_CLOSED_HINT == "Alt+↓ — показать список"


def _hint_widgets(scr, cls):
    from tui.editors.voice_endpoint import VoiceEndpointEditor
    editors = list(scr.query(VoiceEndpointEditor))
    return [w for w in editors[-1].query(f".{cls}") if not w._pruning] if editors else []


@pytest.mark.asyncio
async def test_reopening_replaces_the_hint_instead_of_stacking(tmp_path):
    """Раскрытие поверх свёрнутого списка ЗАМЕНЯЕТ его подсказку, а не дописывает свою
    под ней. Строка одна: `remove()` у Textual отложенный, и снести+смонтировать в одном
    такте значит показать обе (живьём: после Alt+↓ видны две строки подряд)."""
    from tui.app import TuiApp
    from tui.editors.voice_endpoint import _MODEL_RELOAD_HINT
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        fake.models = _MODELS
        await _open_model_field(app, scr, pilot)
        assert len(_hint_widgets(scr, "voice-model-hint")) == 1
        await pilot.press("escape")
        await pilot.pause()
        assert _model_hint(scr) == _MODEL_RELOAD_HINT
        assert len(_hint_widgets(scr, "voice-model-hint")) == 1
        await pilot.press("alt+down")                     # раскрыть обратно
        await _wait(pilot, lambda: bool(_model_rows(scr)))
        assert len(_hint_widgets(scr, "voice-model-hint")) == 1
        assert _model_hint(scr) != _MODEL_RELOAD_HINT
        await pilot.press("alt+down")                      # и повторное нажатие не плодит строк
        await pilot.pause()
        await pilot.pause()
        assert len(_hint_widgets(scr, "voice-model-hint")) == 1


@pytest.mark.asyncio
async def test_footer_offers_del_only_when_there_is_something_to_delete(tmp_path):
    """Сервиса нет — Del не обещаем: он и сам no-op (`_voice_request_remove` требует
    installed), а футер, зовущий в никуда, читается как поломка."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, OFF)               # батарейки нет
        await _open_local(app, scr, pilot)
        assert "Del" not in _footer(scr)
        assert "Enter — установить" in _footer(scr)
        app.pop_screen()
        await pilot.pause()

        scr2, _ = _mk_screen(tmp_path, LOCAL)            # установлена — Del на месте
        await _open_local(app, scr2, pilot)
        # Короткая форма — как в MCP-секции: длинная не оставляла места клавише
        # обновления в пределах двух строк футера (см. test_footer_contract).
        assert "Del — удалить" in _footer(scr2)


@pytest.mark.asyncio
async def test_both_modes_report_being_switched_on_the_same_way(tmp_path):
    """Включение режима сообщается одной формулой «<режим> подключён» — разнобой
    («локальный сервис подключён» против «голосовой ввод: свой endpoint») читался как
    два разных события."""
    from tui.app import TuiApp
    from textual.widgets import Input, Static
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, CLOUD, config_text=CLOUD_CONFIG)
        await _open_cloud(app, scr, pilot)
        scr.query_one("#voice-url", Input).value = _URL
        await pilot.press("enter")
        await pilot.pause()
        assert "свой endpoint подключён" in str(scr.query_one("#set-flash", Static).render())


@pytest.mark.asyncio
async def test_install_asks_before_it_starts(tmp_path):
    """Установка качает ~0.9 ГБ и уводит экран в терминал на минуты — Enter сперва
    спрашивает, ровно как перед сносом. Один Enter ничего не ставит."""
    from tui.app import TuiApp
    from textual.widgets import Static
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF)
        await _open_local(app, scr, pilot)
        await pilot.press("enter")
        await pilot.pause()
        assert fake.calls == []                       # ничего не ставили
        banner = str(scr.query_one(".set-voicehint", Static).render())
        assert "Enter — установить · Esc — отмена" in banner
        assert "~0.9 ГБ" in banner
        await pilot.press("enter")                    # подтверждение — вот теперь ставим
        await pilot.pause()
        assert fake.calls == ["switch_to_local"]


@pytest.mark.asyncio
async def test_escape_cancels_the_install_confirm(tmp_path):
    """Esc снимает подтверждение и оставляет в секции — как у сноса."""
    from tui.app import TuiApp
    from textual.widgets import Static
    from tui.screens.settings import _VOICE
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF)
        await _open_local(app, scr, pilot)
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert scr._view == _VOICE and fake.calls == []
        assert "Esc — отмена" not in str(scr.query_one(".set-voicehint", Static).render())


@pytest.mark.asyncio
async def test_navigation_cancels_the_install_confirm(tmp_path):
    """Стрелка отменяет незавершённое подтверждение: курсор ушёл — вопрос снят."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF)
        await _open_local(app, scr, pilot)
        await pilot.press("enter")
        await pilot.pause()
        assert scr._voice_awaiting_install
        await pilot.press("down")
        await pilot.pause()
        assert not scr._voice_awaiting_install and fake.calls == []


# --- курсор ходит группами: стрелки внутри группы, Tab между ними (спека §4) ---


@pytest.mark.asyncio
async def test_tab_moves_between_groups(tmp_path):
    """Tab уводит со строк режима в поля и обратно — на то место, где курсор оставили:
    позиция у каждой группы своя и помнится отдельно (спека §4.2.1)."""
    from tests.tui.conftest import settings_voice_section
    async with settings_voice_section(tmp_path, mode="cloud") as (pilot, scr):
        assert scr._voice_cursor_group() == "variants"
        row_before = scr._voice_cursor_position()          # строка «Свой endpoint»
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
        assert scr._voice_cursor_position() == row_before, "позиция должна помниться"
        # Клавиша съедена секцией, а не отдана focus_next: тот увёл бы фокус в соседнее
        # поле, а курсор в строках режима фокуса в полях не держит вовсе (спека §4.2.6).
        assert pilot.app.focused is None
        await pilot.press("tab")
        await pilot.pause()
        assert scr._voice_cursor_field() == "token", "позиция группы полей тоже помнится"


@pytest.mark.asyncio
async def test_shift_tab_is_symmetric_to_tab(tmp_path):
    """Shift+Tab — предыдущая группа, симметрично Tab, и тоже перехвачен: без явной
    ветки он остался бы живым focus_previous и увёл бы фокус мимо курсора (§4.2.7)."""
    from textual.widgets import Input
    from tests.tui.conftest import settings_voice_section
    async with settings_voice_section(tmp_path, mode="cloud") as (pilot, scr):
        await pilot.press("shift+tab")
        await pilot.pause()
        assert scr._voice_cursor_group() == "fields"
        assert scr.query_one("#voice-url", Input).has_focus
        await pilot.press("shift+tab")
        await pilot.pause()
        assert scr._voice_cursor_group() == "variants"
        assert pilot.app.focused is None


@pytest.mark.asyncio
async def test_arrows_in_fields_do_not_move_the_variant_rows(tmp_path):
    """Стоя в поле, стрелка двигает по полям, а не по строкам режима (спека §4.2.5):
    раньше лента была сквозной, и стрелка из поля переключала режим под собой."""
    from textual.widgets import Input
    from tests.tui.conftest import settings_voice_section
    async with settings_voice_section(tmp_path, mode="cloud") as (pilot, scr):
        row_before = scr._voice_cursor_position()
        await pilot.press("tab")                           # в поля
        await pilot.press("down")
        await pilot.pause()
        assert scr._voice_cursor_group() == "fields"
        assert scr._voice_cursor_field() == "token"
        assert _cursor_row(scr) == row_before, "строка режима не сдвинулась"
        assert scr.query_one("#voice-token", Input).has_focus


@pytest.mark.asyncio
async def test_tab_is_swallowed_when_there_is_no_second_group(tmp_path):
    """Локальный режим без батарейки: полей нет, группа одна — Tab молчит, но клавишу
    всё равно съедает, иначе её заберёт штатный focus_next Textual (спека §4.2.6)."""
    from tests.tui.conftest import settings_voice_section
    async with settings_voice_section(tmp_path, mode="local", installed=False) as (pilot, scr):
        focused_before = pilot.app.focused
        await pilot.press("tab")
        await pilot.pause()
        assert scr._voice_cursor_group() == "variants"
        assert scr._voice_cursor_position() == 0
        assert pilot.app.focused is focused_before, "Tab не должен водить фокус мимо курсора"


@pytest.mark.asyncio
async def test_tab_collapses_the_open_list_before_leaving(tmp_path):
    """Tab при раскрытом списке сначала сворачивает его, потом уводит в другую группу —
    чтобы список не остался висеть за спиной (спека §4.2.4)."""
    from tests.tui.conftest import settings_voice_section
    async with settings_voice_section(tmp_path, mode="cloud") as (pilot, scr):
        scr._set_voice_cursor(group="fields", position=3)  # «Язык» — поле со списком
        await pilot.pause()
        await _expand_list(pilot)                          # код задан — раскрываем Alt+↓
        ctrl = scr._voice_list_ctrl()
        assert ctrl is not None and ctrl.is_open
        await pilot.press("tab")
        await pilot.pause()
        assert not ctrl.is_open
        assert scr._voice_cursor_group() == "variants"


@pytest.mark.asyncio
async def test_remembered_field_position_is_clamped(tmp_path):
    """Курсор стоял на дальнем поле cloud, переключились на local (там полей
    меньше), Tab обратно в поля — попадаем на существующее поле, а не мимо."""
    # installed=True: у локальной панели поле «Язык» есть только при установленном
    # сервисе, иначе полей там нет вовсе и клэмпать нечего.
    from tests.tui.conftest import settings_voice_section
    async with settings_voice_section(tmp_path, mode="cloud", installed=True) as (pilot, scr):
        await pilot.press("tab")                      # в поля cloud, курсор на url
        # Дальнее поле ставим напрямую: пройти туда стрелками нельзя — на поле со
        # списком он раскрывается сам и забирает ↑/↓ себе (спека §4.2.3).
        scr._set_voice_cursor(group="fields", position=3)
        await pilot.pause()
        assert scr._voice_cursor_position() == 3
        await pilot.press("tab")                      # обратно в строки режима
        await pilot.press("up")                       # выбрать «Локальный сервис»
        await pilot.press("tab")                      # снова в поля — их там меньше
        await pilot.pause()
        assert scr._voice_cursor_position() == 0, "позиция должна зажаться в диапазон"
        assert scr._voice_cursor_field() is not None, "курсор обязан стоять на поле"


@pytest.mark.asyncio
async def test_mouse_click_moves_the_cursor(tmp_path):
    """Клик по полю переносит туда курсор, а не только фокус (спека §4.3)."""
    from textual.widgets import Input
    from tests.tui.conftest import settings_voice_section
    async with settings_voice_section(tmp_path, mode="cloud") as (pilot, scr):
        assert scr._voice_cursor_group() == "variants"
        scr.query_one("#voice-model", Input).focus()
        await pilot.pause()
        assert scr._voice_cursor_group() == "fields"
        assert scr._voice_cursor_field() == "model"


# --- живость порта: только воркер, UI без сокетов ---------------------------
# Замер на живой машине (2026-08-07): connect к мёртвому loopback-порту сгорает
# полным таймаутом ~0.5 с — файрвол молча роняет пакеты вместо RST. Синхронный
# снимок в UI-потоке превращал КАЖДОЕ действие /settings в подвисание.


class _GatedProbe(FakeVoiceSetup):
    """Живость не в снимке, а у воркера — как у настоящего фасада; момент доставки
    держит threading.Event, чтобы «…» ловилось без гонки со скоростью потока."""

    def __init__(self, status):
        super().__init__(status)
        import threading
        self.gate = threading.Event()
        self.probe_on_main = []            # True == вызвали из UI-потока (запрещено)

    def status_cheap(self, config):
        from dataclasses import replace
        self.snapshots += 1
        return replace(self.status, service_alive=None)

    def probe_alive(self):
        import threading
        self.probe_on_main.append(
            threading.current_thread() is threading.main_thread())
        self.gate.wait(5)
        return self.status.service_alive


def _mk_gated_screen(tmp_path, status):
    from tui.screens.settings import SettingsScreen
    cfg = tmp_path / "config.yml"
    cfg.write_text("model: m\n", encoding="utf-8")
    fake = _GatedProbe(status)
    scr = SettingsScreen(config={"voice": {}}, config_path=str(cfg),
                         on_apply=lambda: None, voice=fake)
    return scr, fake


@pytest.mark.asyncio
async def test_alive_is_measured_by_a_worker_and_lands_in_the_row(tmp_path):
    """Пока проба летит — честное «…» (не мгновенное «не запущен», которое через
    полсекунды дёргалось бы в «запущен»); доставка воркера дописывает статус."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_gated_screen(tmp_path, LOCAL)
        await _open_voice(app, scr, pilot)
        assert "установлен · … · порт 8735" in _rows(scr)[0]
        fake.gate.set()
        assert await _wait(pilot, lambda: "установлен · запущен · порт 8735"
                           in _rows(scr)[0]), "живость воркера не доехала до строки"
        assert fake.probe_on_main and not any(fake.probe_on_main), \
            "probe_alive обязан звучать только из воркер-потока"


@pytest.mark.asyncio
async def test_alive_delivery_updates_the_menu_glyph(tmp_path):
    """Меню: локальный режим даёт ✓ только живым сервисом — доставка воркера обязана
    перерисовать глиф с «—» на «✓», не дожидаясь захода в секцию."""
    from textual.widgets import Static
    from tui.app import TuiApp
    from tui.screens.settings import _VOICE
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_gated_screen(tmp_path, LOCAL)
        await app.push_screen(scr)
        await pilot.pause()

        def _voice_glyph():
            rows = list(scr.query(".set-menurow"))
            if len(rows) <= _VOICE:
                return ""
            return str(rows[_VOICE].query_one(".set-glyph", Static).render())
        assert "✓" not in _voice_glyph()             # живость ещё не мерялась
        fake.gate.set()
        assert await _wait(pilot, lambda: "✓" in _voice_glyph()), \
            "глиф меню не обновился после доставки живости"


@pytest.mark.asyncio
async def test_menu_navigation_never_probes_synchronously(tmp_path):
    """Сторож канона: ходы курсора по меню перерисовывают превью, но пробу порта
    не рожают — иначе каждое действие снова стоило бы таймаут сокета."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_gated_screen(tmp_path, LOCAL_DEAD)
        fake.gate.set()                              # пробы отвечают мгновенно
        await app.push_screen(scr)
        await pilot.pause()
        started = len(fake.probe_on_main)            # единственный kick — on_mount
        for _ in range(6):
            await pilot.press("down")
        for _ in range(6):
            await pilot.press("up")
        await pilot.pause()
        assert len(fake.probe_on_main) == started, \
            "навигация по меню не имеет права запускать пробы порта"
        assert not any(fake.probe_on_main)


@pytest.mark.asyncio
async def test_alive_delivery_updates_menu_rows_in_place(tmp_path):
    """Доставка живости обновляет ТЕКСТЫ существующих строк меню, а не пересобирает
    меню: полная пересборка (_render_view) видна глазом как мигание экрана через
    ~полсекунды после каждого захода в /settings (живая приёмка 2026-08-07 — свой
    endpoint + не поднятый сервис, где видимая часть меню от живости не зависит
    вовсе). Канон точечности — refresh_update_glyph."""
    from textual.widgets import Static
    from tui.app import TuiApp
    from tui.screens.settings import _VOICE
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_gated_screen(tmp_path, LOCAL)
        await app.push_screen(scr)
        await pilot.pause()
        rows_before = list(scr.query(".set-menurow"))
        assert rows_before, "меню ещё не смонтировано — тест меряет пустоту"

        def _voice_glyph():
            rows = list(scr.query(".set-menurow"))
            if len(rows) <= _VOICE:
                return ""
            return str(rows[_VOICE].query_one(".set-glyph", Static).render())
        fake.gate.set()
        assert await _wait(pilot, lambda: "✓" in _voice_glyph()), \
            "живость не доехала до глифа"
        rows_after = list(scr.query(".set-menurow"))
        assert [id(w) for w in rows_after] == [id(w) for w in rows_before], \
            "доставка живости пересобрала меню вместо точечного обновления строк"


@pytest.mark.asyncio
async def test_escape_over_a_saved_model_keeps_it_in_the_field(tmp_path):
    """Список раскрыт поверх сохранённого id — Esc возвращает ЕГО, а не пустоту.

    Живой смок 2026-08-20: список моделей раскрывался сам при каждом входе на поле,
    и `Esc` («убрать ненужный список») оставлял поле пустым — на экране это читалось
    как «отменил выбор модели». Раскрытие поверх значения теперь идёт только явным
    `Alt+↓`, а закрытие возвращает в поле ровно то, что там было."""
    from tui.app import TuiApp
    from textual.widgets import Input
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        fake.models = _MODELS
        await _open_model_field(app, scr, pilot)       # заполнено → раскрыл Alt+↓
        assert _model_rows(scr), "предусловие: список раскрыт"
        assert scr.query_one("#voice-model", Input).value == "whisper-old"

        await pilot.press("escape")                    # убрать ненужный список
        await pilot.pause()
        assert not _model_rows(scr)
        assert scr.query_one("#voice-model", Input).value == "whisper-old"


@pytest.mark.asyncio
async def test_leaving_to_the_menu_and_coming_back_opens_no_list(tmp_path):
    """Ушли в меню настроек с курсором в поле со списком, вернулись — секция начинается
    со строк режима, и ни один список не раскрыт.

    Живой смок 2026-08-20: секция помнила место, возврат попадал сразу в «Модель» или
    «Язык», и список раскрывался там, где человек его не просил — а `Esc`, которым его
    убирали, откатывал поле. Память места была исключением одной этой секции."""
    from tui.app import TuiApp
    from tui.screens.settings import _VOICE, _VOICE_GROUP_VARIANTS
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG + "  language: en\n")
        fake.models = _MODELS
        await _open_cloud(app, scr, pilot)
        await _to_field(pilot, _TO_MODEL_FIELD)         # курсор в поле «Модель»
        assert _focused_id(scr) == "voice-model"
        await pilot.press("escape")                     # свернуть список
        await pilot.pause()
        await pilot.press("escape")                     # из секции — в меню настроек
        await pilot.pause()
        assert scr._view == "menu"

        scr._open_section(_VOICE)                       # Enter по строке «Голосовой ввод»
        await pilot.pause()
        assert scr._voice_cursor_group() == _VOICE_GROUP_VARIANTS
        assert _focused_id(scr) is None
        assert not _model_rows(scr) and not _lang_rows(scr) and not _url_rows(scr)


@pytest.mark.asyncio
async def test_suspended_never_repeats_a_failed_action(tmp_path, monkeypatch):
    """Упало САМО действие — второй раз его не заводим.

    Находка ревью коллеги: `_suspended` различал только «успели ли доделать», и на
    исключении из `do()` прогонял его повторно — для установки это второй clone,
    uv sync и скачивание модели.

    `suspend` подменяем рабочей заглушкой: headless-драйвер тестов его не умеет и
    бросает на входе, а тогда проверялся бы fallback-путь, а не этот."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, OFF)
        await _open_voice(app, scr, pilot)

        class _Suspend:
            def __enter__(self):
                return None

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(pilot.app, "suspend", lambda: _Suspend())
        calls = []

        def _boom():
            calls.append(1)
            raise OSError("каталог занят")

        assert scr._suspended(_boom, "\nставлю…\n") is False
        assert calls == [1], "упавшее действие запущено повторно"


@pytest.mark.asyncio
async def test_suspended_falls_back_when_the_driver_cannot_suspend(tmp_path, monkeypatch):
    """Драйвер не умеет suspend — действие всё равно выполняется, ровно один раз."""
    from tui.app import TuiApp
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, _ = _mk_screen(tmp_path, OFF)
        await _open_voice(app, scr, pilot)

        class _NoSuspend:
            def __enter__(self):
                raise RuntimeError("драйвер не умеет suspend")

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(pilot.app, "suspend", lambda: _NoSuspend())
        calls = []
        assert scr._suspended(lambda: calls.append(1) or True, "\nставлю…\n") is True
        assert calls == [1]


@pytest.mark.asyncio
async def test_list_hints_stand_under_every_field_from_the_first_frame(tmp_path):
    """Строка «чем раскрыть список» стоит под КАЖДЫМ полем со списком сразу.

    Живой смок 2026-08-20: строку монтировал сам контроллер — при первом раскрытии.
    Под «URL» она появлялась, только когда курсор туда заходил, под «Моделью» — после
    первого Alt+↓, и панель показывала мешанину: у одного поля подсказка есть, у
    соседнего нет. На «Подключении» этого не видно — там поле со списком одно и курсор
    стартует на нём."""
    from tui.app import TuiApp
    from tui.editors.field_list import LIST_CLOSED_HINT
    from tui.editors.voice_endpoint import _MODEL_RELOAD_HINT
    app = TuiApp(config={"model": "M"})
    async with app.run_test() as pilot:
        scr, fake = _mk_screen(tmp_path, OFF, config_text=CLOUD_CONFIG)
        fake.models = _MODELS
        await _open_cloud(app, scr, pilot)      # курсор на строке режима, в поля не заходили
        await pilot.pause()
        assert _url_hint(scr) == LIST_CLOSED_HINT
        assert _lang_hint(scr) == LIST_CLOSED_HINT
        # Сетевой список обещает свой глагол и до первой загрузки: клавиша идёт на сервер.
        assert _model_hint(scr) == _MODEL_RELOAD_HINT
