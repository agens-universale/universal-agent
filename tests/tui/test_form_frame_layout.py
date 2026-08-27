"""Канон раскладки экранов-форм: футер есть всегда, рамка цельная, скролл — внутри неё.

Стражи `test_footer_contract` проверяют ТЕКСТ футера и его бюджет по ширине — но не
то, что футер вообще нарисован. На живом смоке /settings → «Модели» футера не было
видно вовсе: скролл с `height: auto; max-height: 100%` мерил потолок по ВСЕЙ высоте
`#root` (шапка и футер в 100% входят), вырастал на весь экран и выдавливал футер за
нижний край, а рамка (она сидела ВНУТРИ скролла) уезжала концами за вьюпорт.

Сторож формулирует канон целиком и на ВСЕХ экранах-карточках сразу — мастер и
пост-апдейт устроены теми же тремя виджетами (`#onb-*`, `#pu-*`), и починка одного
экрана без остальных прошла бы мимо: владелец назвал два экрана, потому что видел
два, а канон сформулировал общий — «какой бы экран ни был по высоте»."""
import pytest

pytest.importorskip("textual")

from tests.tui.conftest import ok_probe, open_settings_section, wait_until

# Окно заведомо ниже содержимого любой секции/шага: канон проверяется там, где
# контент ГАРАНТИРОВАННО не влезает (на просторном экране пройдёт и сломанная
# раскладка — выдавливать футер нечем).
_TINY = (80, 14)


def _cfg():
    return {
        "completions_url": "http://host/v1/chat/completions",
        "chat_completion_headers": {"Authorization": "Bearer secret-token"},
        "model": "test-model",
    }


def _assert_canon(scr, *, scroll_id, frame_id, footer_id):
    """Три обещания канона разом — на любом экране-форме.

    Кто из двух виджетов (скролл или его содержимое) несёт рамку, сторож не
    предписывает — он ищет носителя `border` сам и требует от него цельности. Так
    проверка формулирует канон, а не сегодняшнюю расстановку правил в tcss."""
    scroll = scr.query_one(scroll_id)
    frame = scr.query_one(frame_id)
    footer = scr.query_one(footer_id)
    viewport = scr.region
    # (1) футер виден целиком
    assert footer.display and footer.region.height > 0, f"{footer_id} не отрисован"
    assert viewport.contains_region(footer.region), (
        f"{footer_id} за краем экрана: {footer.region} ⊄ {viewport}")
    # (2) рамка цельная: её носитель целиком во вьюпорте — ни верх, ни низ границы
    #     не уехали за край (а значит, скроллится содержимое ВНУТРИ неё, не она сама)
    # styles.border — четвёрка (тип, цвет) по сторонам; «рамки нет» = пустой ТИП
    # (сама четвёрка есть всегда, поэтому проверяем именно тип).
    bordered = [w for w in (scroll, frame) if any(edge[0] for edge in w.styles.border)]
    assert bordered, f"ни {scroll_id}, ни {frame_id} не рисуют рамку"
    for widget in bordered:
        assert viewport.contains_region(widget.region), (
            f"рамка ({widget.id}) за краем экрана: {widget.region} ⊄ {viewport}")
    # (3) …и всё это — на переполненном содержимом: проверяется ПОСЛЕДНИМ, чтобы
    #     сломанная раскладка краснела по существу (пп. 1-2), а не по этой оговорке.
    #     Оговорка нужна на случай, если окно сторожа станет просторным: тогда канон
    #     соблюдётся сам собой и тест перестанет что-либо доказывать.
    assert scroll.virtual_size.height > scroll.size.height, (
        f"{scroll_id}: контент влез ({scroll.virtual_size.height} ≤ {scroll.size.height}) — "
        f"сторож проверяет переполнение, подберите более тесное окно")


@pytest.mark.parametrize("section", ["Подключение", "Модели", "MCP-серверы",
                                     "Режимы работы", "Обновление"])
async def test_settings_footer_survives_overflowing_section(tmp_path, section):
    from tui.app import TuiApp
    from tui.screens.settings import SettingsScreen
    app = TuiApp(config=_cfg())
    async with app.run_test(size=_TINY) as pilot:
        scr = SettingsScreen(config=_cfg(), config_path=str(tmp_path / "config.yml"),
                             on_apply=lambda: None, probe_fn=ok_probe())
        await pilot.app.push_screen(scr)
        await pilot.pause()
        open_settings_section(scr, section)
        await pilot.pause()
        await pilot.pause()
        _assert_canon(scr, scroll_id="#set-scroll", frame_id="#set-frame",
                      footer_id="#set-footer")


async def test_selected_row_is_pulled_into_the_capped_frame(tmp_path):
    """Курсор по длинному списку остаётся ВИДИМЫМ и после того, как рамка получила потолок.

    До правки скролл был выше экрана, и `scroll_visible` подводить было некуда — строка
    «видима» в его собственных координатах и невидима на экране. Теперь вьюпорт рамки
    настоящий, и подводка обязана работать по-настоящему: гоним курсор в самый низ
    реестра из полутора десятков записей на тесном окне."""
    from unittest import mock

    import model_picker
    import tui.app as app_mod
    from model_picker import ModelInfo
    from tests.tui.conftest import _isolated_config_layers
    from tests.tui.test_settings_models_section import _SECTION, _write_cfg
    from tests.tui.test_settings_screen import _loaded, _mk_screen

    registry = "models:\n" + "".join(
        f"  m{i}:\n    model: \"vendor/model-{i}\"\n" for i in range(15))
    cfg_file = _write_cfg(tmp_path, registry)
    cfg = _loaded(cfg_file)
    app = app_mod.TuiApp(config=cfg)
    with _isolated_config_layers(tmp_path), \
         mock.patch.object(model_picker, "fetch_models",
                           lambda *a, **k: [ModelInfo(id="test-model")]):
        async with app.run_test(size=_TINY) as pilot:
            scr = _mk_screen(config=cfg, config_path=str(cfg_file))
            app.push_screen(scr)
            await pilot.pause()
            open_settings_section(scr, _SECTION)
            await pilot.pause()
            for _ in range(20):
                await pilot.press("down")
            await pilot.pause()
            selected = scr.query(".models-row.selected")
            assert selected, "нет подсвеченной строки реестра"
            row = selected.last()
            assert scr.region.contains_region(row.region), (
                f"подсвеченная строка не подведена в кадр: {row.region} ⊄ {scr.region}")
            _assert_canon(scr, scroll_id="#set-scroll", frame_id="#set-frame",
                          footer_id="#set-footer")


async def test_onboarding_footer_survives_overflowing_step(tmp_path):
    from tui.app import TuiApp
    from tui.screens.onboarding import OnboardingScreen

    class _FakeMcp:
        def installable_names(self): return ["duckduckgo", "office"]
        def wired_names(self): return []
        def descriptions(self): return {}
        def install(self, name=None): return 0

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test(size=_TINY) as pilot:
        scr = OnboardingScreen(
            config={"completions_url": "http://your-backend:PORT/v1/chat/completions",
                    "chat_completion_headers": {"Authorization": "Bearer <ВПИШИТЕ_ТОКЕН>"},
                    "model": "<ВПИШИТЕ_МОДЕЛЬ>"},
            config_path=str(tmp_path / "config.yml"), review_all=False,
            on_commit=lambda: None, fetch=lambda *a, **k: None, mcp=_FakeMcp())
        await pilot.app.push_screen(scr)
        await pilot.pause()
        await pilot.pause()
        _assert_canon(scr, scroll_id="#onb-scroll", frame_id="#onb-frame",
                      footer_id="#onb-footer")


def _target(scr, selector: str):
    """Последний смонтированный элемент по селектору (баннер, поле, строка курсора)."""
    found = [w for w in scr.query(selector) if w.display]
    assert found, f"{selector} не смонтирован"
    return found[-1]


async def _assert_revealed(pilot, scr, selector: str) -> None:
    """Элемент оказывается в кадре.

    Ждём наблюдаемое условие общим `wait_until` (канон conftest), а не считаем такты:
    подводка отложена и повторяет себя несколько кадров, и число тактов — деталь
    реализации, за которую тесту цепляться незачем."""
    await wait_until(pilot,
                     lambda: scr.region.contains_region(_target(scr, selector).region),
                     f"{selector} подведён в кадр", steps=40)


async def test_delete_confirm_banner_is_pulled_into_view(tmp_path):
    """Вопрос, на который просят ответить Enter'ом, обязан быть ВИДЕН.

    Баннер монтируется в конец тела секции, а тело длиннее вьюпорта: без подводки
    футер честно менялся на «Enter — удалить · Esc — отмена», а самого вопроса на
    экране не было — Enter приходилось жать вслепую."""
    from unittest import mock

    import model_picker
    import tui.app as app_mod
    from model_picker import ModelInfo
    from tests.tui.conftest import _isolated_config_layers
    from tests.tui.test_settings_models_section import _SECTION, _write_cfg
    from tests.tui.test_settings_screen import _loaded, _mk_screen

    registry = "models:\n" + "".join(
        f"  m{i}:\n    model: \"vendor/model-{i}\"\n" for i in range(12))
    cfg_file = _write_cfg(tmp_path, registry)
    cfg = _loaded(cfg_file)
    app = app_mod.TuiApp(config=cfg)
    with _isolated_config_layers(tmp_path), \
         mock.patch.object(model_picker, "fetch_models",
                           lambda *a, **k: [ModelInfo(id="test-model")]):
        async with app.run_test(size=_TINY) as pilot:
            scr = _mk_screen(config=cfg, config_path=str(cfg_file))
            app.push_screen(scr)
            await pilot.pause()
            open_settings_section(scr, _SECTION)
            await pilot.pause()
            await pilot.press("down")
            await pilot.press("delete")
            await pilot.pause()
            await _assert_revealed(pilot, scr, ".models-confirm")


async def test_update_confirm_banner_is_pulled_into_view(tmp_path):
    """Тот же канон у баннера, который монтирует САМ экран (а не редактор секции)."""
    from tui.app import TuiApp
    from tui.screens.settings import SettingsScreen
    app = TuiApp(config=_cfg())
    async with app.run_test(size=_TINY) as pilot:
        scr = SettingsScreen(config=_cfg(), config_path=str(tmp_path / "config.yml"),
                             on_apply=lambda: None, probe_fn=ok_probe())
        await pilot.app.push_screen(scr)
        await pilot.pause()
        open_settings_section(scr, "Обновление")
        await pilot.pause()
        scr._request_update()          # Enter на «Обновить агента» без сети
        await pilot.pause()
        await _assert_revealed(pilot, scr, ".set-confirm")


async def test_env_overwrite_confirm_is_pulled_into_view(tmp_path):
    """Вопрос о перезаписи ссылки ${ENV:VAR} приезжает БЕЗ пересборки тела.

    Он монтируется `_singleton`'ом прямо в конец секции — сам по себе `reveal_focus`
    его бы не застал (тот работает от пересборки). Цена промаха здесь выше обычной:
    Enter вслепую подменяет ссылку на переменную её значением, то есть кладёт секрет
    открытым текстом в файл, который коммитят."""
    from tests.tui.conftest import _isolated_config_layers
    from tui.app import TuiApp
    from tui.screens.settings import SettingsScreen

    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        "backend:\n"
        '  completions_url: "${ENV:GC_URL}"\n'
        "  chat_completion_headers:\n"
        '    Authorization: "${ENV:GC_TOKEN}"\n'
        "agent:\n"
        '  model: "test-model"\n', encoding="utf-8")
    app = TuiApp(config=_cfg())
    with _isolated_config_layers(tmp_path):
        async with app.run_test(size=_TINY) as pilot:
            scr = SettingsScreen(config=_cfg(), config_path=str(cfg_file),
                                 on_apply=lambda: None, probe_fn=ok_probe())
            app.push_screen(scr)
            await pilot.pause()
            open_settings_section(scr, "Подключение")
            await pilot.pause()
            scr._do_save(0, {"completions_url": "http://new/v1/chat/completions"})
            await pilot.pause()
            await _assert_revealed(pilot, scr, "#set-envconfirm")


async def test_profile_name_prompt_is_pulled_into_view(tmp_path):
    """Поле ввода имени профиля — тот же канон: печатать вслепую нельзя."""
    from tests.tui.conftest import _isolated_config_layers
    from tests.tui.test_settings_models_section import _write_cfg
    from tests.tui.test_settings_screen import _loaded, _mk_screen
    import tui.app as app_mod

    registry = "models:\n" + "".join(
        f"  m{i}:\n    model: \"vendor/model-{i}\"\n" for i in range(12))
    cfg_file = _write_cfg(tmp_path, registry)
    cfg = _loaded(cfg_file)
    app = app_mod.TuiApp(config=cfg)
    with _isolated_config_layers(tmp_path):
        async with app.run_test(size=_TINY) as pilot:
            scr = _mk_screen(config=cfg, config_path=str(cfg_file))
            app.push_screen(scr)
            await pilot.pause()
            open_settings_section(scr, "Профили")
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            await _assert_revealed(pilot, scr, "#set-profile-name")


async def test_settings_menu_cursor_is_pulled_into_view(tmp_path):
    """Курсор МЕНЮ настроек: строки обновляются на месте, без пересборки тела.

    Отдельный тест, потому что и путь другой: подводка от `_render_view` сюда не
    приходит — меню перерисовывает строки `_refresh_menu_rows`'ом."""
    from tui.app import TuiApp
    from tui.screens.settings import SettingsScreen
    app = TuiApp(config=_cfg())
    async with app.run_test(size=(80, 16)) as pilot:
        scr = SettingsScreen(config=_cfg(), config_path=str(tmp_path / "config.yml"),
                             on_apply=lambda: None, probe_fn=ok_probe())
        app.push_screen(scr)
        await pilot.pause()
        for _ in range(5):
            await pilot.press("down")
        await pilot.pause()
        await _assert_revealed(pilot, scr, ".set-menurow.selected")


async def test_post_update_footer_survives_overflowing_card(monkeypatch):
    """Третий экран той же раскладки — пост-апдейт.

    Владелец назвал два экрана (он видел два), но канон общий: «какой бы экран ни был
    по высоте — футер есть всегда, рамка цельная». Карточка пост-апдейта короткая,
    поэтому окно берём совсем тесное: дефект тут спит, пока экран просторный, — ровно
    как спал на `/settings`, пока волна не сделала секции высокими."""
    import config_sync
    import mcp_runtime.cli as cli
    import tui.app as app_mod
    from tui.screens.post_update import PostUpdateScreen

    class _Upg:
        old_version = "0.3.0"
        new_version = "0.4.0"

    outdated = [f"server-{i}" for i in range(6)]     # длинная строка → перенос
    monkeypatch.setattr(cli, "wired_names", lambda config=None: outdated)
    monkeypatch.setattr(cli, "load_manifest_entries", lambda config=None: ([], None))
    monkeypatch.setattr(cli, "installed_state",
                        lambda name, config=None, manifest=None:
                        {"installed": True, "version": "0.1.0", "update": "0.2.0"})
    monkeypatch.setattr(config_sync, "needs_sync", lambda *a, **k: True)
    app = app_mod.TuiApp(config={})
    async with app.run_test(size=(60, 12)) as pilot:
        scr = PostUpdateScreen(upgrade=_Upg(), config={}, config_path="c.yml")
        app.push_screen(scr)
        await pilot.pause()
        await pilot.pause()
        _assert_canon(scr, scroll_id="#pu-scroll", frame_id="#pu-frame",
                      footer_id="#footer")


@pytest.mark.parametrize("h", [14, 24, 30])
async def test_notice_does_not_outrank_the_cursor(tmp_path, h):
    """Плашка ИТОГА не держит кадр: она сообщение, а не вопрос.

    Маркер приоритета несёт то, на что человек обязан ответить (баннер подтверждения,
    поле ввода), — и держит кадр, пока висит. Плашка итога (`#set-notice`, `#set-flash`)
    ответа не требует, а `#set-notice` живёт до следующей пересборки тела: получив тот
    же приоритет, она навсегда перебивала курсор, и стрелки на обычном терминале
    переставали быть видны — ровно тот дефект, ради которого задача и делалась."""
    from unittest import mock

    import model_picker
    import tui.app as app_mod
    from model_picker import ModelInfo
    from tests.tui.conftest import _isolated_config_layers
    from tests.tui.test_settings_models_section import _SECTION, _write_cfg
    from tests.tui.test_settings_screen import _loaded, _mk_screen

    registry = "models:\n" + "".join(
        f"  m{i}:\n    model: \"vendor/model-{i}\"\n" for i in range(15))
    cfg_file = _write_cfg(tmp_path, registry)
    cfg = _loaded(cfg_file)
    app = app_mod.TuiApp(config=cfg)
    with _isolated_config_layers(tmp_path), \
         mock.patch.object(model_picker, "fetch_models",
                           lambda *a, **k: [ModelInfo(id="test-model")]):
        async with app.run_test(size=(80, h)) as pilot:
            scr = _mk_screen(config=cfg, config_path=str(cfg_file))
            app.push_screen(scr)
            await pilot.pause()
            open_settings_section(scr, _SECTION)
            await pilot.pause()
            scr._notice("итог операции")     # плашка внизу секции, живёт до пересборки
            await pilot.pause()
            for _ in range(20):
                await pilot.press("down")
            await _assert_revealed(pilot, scr, ".models-row.selected")


@pytest.mark.parametrize("new_h", [30, 16, 12])
async def test_canon_survives_live_resize(tmp_path, new_h):
    """Окно уменьшили НА ХОДУ — канон обязан устоять.

    Единственный путь, который не проверял ни один тест: экран открылся просторным,
    потолок рамки посчитался под ту высоту, а потом человек потянул угол терминала.
    Без пересчёта рамка остаётся прежней и футер снова уезжает за край — тот самый
    дефект, только отложенный до ресайза.

    Сторож проверяет КАНОН, а не способ его удержать: как именно рамка узнаёт новую
    высоту (пересчёт в `on_resize` или чистый CSS), тесту знать незачем."""
    from tui.app import TuiApp
    from tui.screens.settings import SettingsScreen
    app = TuiApp(config=_cfg())
    async with app.run_test(size=(80, 40)) as pilot:
        scr = SettingsScreen(config=_cfg(), config_path=str(tmp_path / "config.yml"),
                             on_apply=lambda: None, probe_fn=ok_probe())
        await pilot.app.push_screen(scr)
        await pilot.pause()
        open_settings_section(scr, "Подключение")     # секция заведомо выше любого окна
        await pilot.pause()
        await pilot.resize_terminal(80, new_h)
        await pilot.pause()
        footer = scr.query_one("#set-footer")
        scroll = scr.query_one("#set-scroll")
        assert scr.region.contains_region(footer.region), (
            f"после ресайза 40→{new_h} футер за краем: {footer.region} ⊄ {scr.region}")
        assert scr.region.contains_region(scroll.region), (
            f"после ресайза 40→{new_h} рамка за краем: {scroll.region} ⊄ {scr.region}")
