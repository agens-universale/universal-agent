"""ModelEntryForm — форма одной записи реестра `models.<alias>` (волна B, Task 2).

Контракт формы — `ConnectionEditor`: draft живёт в полях, наружу уходит только по
`submit()`, и `submit()` отдаёт ТОЛЬКО явно заданные поля — merge со старой записью
делает writer (`config_modules/registry_write.upsert_model`). Сама форма не пишет
никуда и цели записи не знает.

Две РАЗНЫЕ клавиши (контракт волны B, п.4): `Ctrl+Space` — проба соединения
(`tui/services/settings_probe`), `Alt+↓` — подбор списка моделей с эндпоинта. Их
путаница и есть находка A4 ревью плана, поэтому на разведение заведён отдельный
страж.
"""
from pathlib import Path

import pytest

pytest.importorskip("textual")

import yaml
from textual.app import App, ComposeResult
from textual.widgets import Input, Static

from config import _default_config_path
from config_modules.endpoint_presets import EndpointPreset
from model_picker import ModelInfo
from tui.services.settings_probe import ProbeResult


def _harness(editor):
    class _App(App):
        def compose(self) -> ComposeResult:
            yield editor
    return _App()


def _form(**kwargs):
    """Форма с инъекцией сети-заглушки: тесты, которым сеть не нужна, всё равно не
    должны иметь ни малейшего шанса в неё сходить."""
    from tui.editors.model_entry_form import ModelEntryForm
    kwargs.setdefault("fetch", lambda *a, **k: None)
    kwargs.setdefault("probe", lambda *a, **k: ProbeResult("unknown", None, None, 0, "нет сети"))
    return ModelEntryForm(**kwargs)


def _models():
    return [ModelInfo(id="google/gemini-2.5-flash", tool_capable=True),
            ModelInfo(id="zhipu/glm-4.6", tool_capable=True)]


def _rows(form) -> list:
    """Строки списка моделей НА ЭКРАНЕ (фильтр гасит их через `display`)."""
    return [w for w in form.query(".mef-model-row") if w.display]


_REGISTRY_WITH_PRICING = (
    'profile: ""\n'
    "level: default\n"
    "models:\n"
    "  flash:\n"
    '    model: "google/gemini-2.5-flash"\n'
    '    url: "https://openrouter.ai/api/v1/chat/completions"\n'
    '    token: "sk-secret"\n'
    "    pricing:\n"
    "      input: 0.3\n"
    "      output: 1.2\n"
    "slots:\n"
    '  default: "flash"\n'
    "backend:\n"
    '  completions_url: "http://hub/v1/chat/completions"\n'
)


# --- draft: наружу только через submit() --------------------------------------

async def test_draft_does_not_leak_before_submit(tmp_path):
    """Форма не пишет никуда: правки живут в полях, файл цели не меняется даже
    после submit() — записывает writer, которого зовёт хозяин формы."""
    target = tmp_path / "config.yml"
    target.write_text(_REGISTRY_WITH_PRICING, encoding="utf-8")
    before = target.read_bytes()
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                          "url": "https://openrouter.ai/api/v1/chat/completions",
                          "token": "sk-secret"},
                 existing_aliases={"flash"})
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.query_one("#mef-url", Input).value = "https://new/v1/chat/completions"
        assert form.is_dirty() is True
        assert target.read_bytes() == before
        ok, alias, fields = form.submit()
        assert (ok, alias) == (True, "flash")
        assert fields["url"] == "https://new/v1/chat/completions"
        assert target.read_bytes() == before


async def test_escape_collapses_the_list_and_then_gives_the_key_back(tmp_path):
    """Esc — откат: сперва сворачивает раскрытый список (своё под-состояние формы),
    и только когда сворачивать нечего, отдаёт клавишу хозяину (тот закроет форму —
    и черновик умрёт вместе с ней, файл не тронут)."""
    target = tmp_path / "config.yml"
    target.write_text(_REGISTRY_WITH_PRICING, encoding="utf-8")
    before = target.read_bytes()
    form = _form(initial=None, existing_aliases={"flash"},
                 fetch=lambda *a, **k: _models(),
                 effective_url="http://hub/v1/chat/completions")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.move(1)                                  # алиас → id модели
        await pilot.pause()
        assert form.reload_models() is True
        for _ in range(3):
            await pilot.pause()
        assert _rows(form), "список не раскрылся"
        assert form.escape() is True                  # свернуть список
        await pilot.pause()
        assert not _rows(form)
        assert form.escape() is False                 # сворачивать нечего — клавиша хозяину
        assert target.read_bytes() == before


async def test_is_dirty_is_honest():
    """Вход без правок — не dirty; правка поля и переключение галки — dirty."""
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash"},
                 existing_aliases={"flash"})
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        assert form.is_dirty() is False
        form.query_one("#mef-model", Input).value = "zhipu/glm-4.6"
        assert form.is_dirty() is True
        form.query_one("#mef-model", Input).value = "google/gemini-2.5-flash"
        assert form.is_dirty() is False
        form.move(-1)                                  # курсор → галка (последняя позиция)
        await pilot.pause()
        assert form.toggle_no_auth() is True
        assert form.is_dirty() is True                 # «явно без авторизации» — правка


# --- наследование основного конфига (решение 7) --------------------------------

async def test_empty_url_inherits_and_no_key_is_emitted():
    """Пустое поле адреса = наследование от основного конфига: ключ `url` не
    отдаётся вовсе (writer сохранит отсутствие ключа), а placeholder честно
    показывает, ЧТО именно унаследуется."""
    form = _form(initial=None, existing_aliases=set(),
                 effective_url="http://hub/v1/chat/completions")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.query_one("#mef-alias", Input).value = "flash"
        form.query_one("#mef-model", Input).value = "google/gemini-2.5-flash"
        ok, alias, fields = form.submit()
        assert (ok, alias) == (True, "flash")
        assert "url" not in fields
        assert form.query_one("#mef-url", Input).placeholder == (
            "наследуется: http://hub/v1/chat/completions")


async def test_clearing_a_prefilled_url_removes_the_key(tmp_path):
    """Стёртое ПРЕДЗАПОЛНЕННОЕ поле — это «снять», а не «промолчать»: ключ уходит
    из записи (`None` — контракт писателя), и адрес снова наследуется от основного
    конфига. Иначе форма показывала бы «изменено», Enter отвечал бы «сохранено», а
    в файле оставался бы прежний адрес."""
    from config_modules.registry_write import upsert_model
    target = tmp_path / "config.yml"
    target.write_text(_REGISTRY_WITH_PRICING, encoding="utf-8")
    template = Path(_default_config_path()).read_text(encoding="utf-8")
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                          "url": "https://openrouter.ai/api/v1/chat/completions",
                          "token": "sk-secret"},
                 existing_aliases={"flash"},
                 effective_url="http://hub/v1/chat/completions")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.query_one("#mef-url", Input).value = ""
        await pilot.pause()
        assert form.is_dirty() is True
        # Placeholder виден ровно в этот момент — когда поле стёрли: он обязан
        # называть, ЧТО теперь будет действовать.
        assert form.query_one("#mef-url", Input).placeholder == (
            "наследуется: http://hub/v1/chat/completions")
        ok, alias, fields = form.submit()
        assert ok is True and fields["url"] is None
    upsert_model(target, alias, fields, template)
    entry = yaml.safe_load(target.read_text(encoding="utf-8"))["models"]["flash"]
    assert "url" not in entry, entry                    # ключ снят — наследование
    assert entry["pricing"] == {"input": 0.3, "output": 1.2}
    assert entry["token"] == "sk-secret"                # соседей это не задело


async def test_clearing_a_prefilled_model_id_removes_the_key(tmp_path):
    """То же для провайдерского id: стёрли — ключ снят, и алиас снова служит id
    (`model_router`: `entry.get("model") or alias`). Без этого «пусто = алиас и
    есть id» было бы недостижимо на правке."""
    from config_modules.registry_write import upsert_model
    target = tmp_path / "config.yml"
    target.write_text(_REGISTRY_WITH_PRICING, encoding="utf-8")
    template = Path(_default_config_path()).read_text(encoding="utf-8")
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                          "url": "https://openrouter.ai/api/v1/chat/completions"},
                 existing_aliases={"flash"})
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.query_one("#mef-model", Input).value = ""
        await pilot.pause()
        ok, alias, fields = form.submit()
        assert ok is True and fields["model"] is None
    upsert_model(target, alias, fields, template)
    entry = yaml.safe_load(target.read_text(encoding="utf-8"))["models"]["flash"]
    assert "model" not in entry, entry


async def test_inherit_preset_is_offered_when_editing_a_record_with_its_own_url():
    """Пункт «основной конфиг» нужен именно тем записям, у которых свой адрес ЕСТЬ:
    это единственный явный путь вернуть их к наследованию (решение 7). Список поверх
    своего адреса сам не раскрывается (канон `can_open`) — раскрывает Alt+↓."""
    presets = [EndpointPreset(name="OpenRouter",
                              url="https://openrouter.ai/api/v1/chat/completions")]
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                          "url": "https://my-own.internal/v1/chat/completions"},
                 existing_aliases={"flash"}, presets=presets,
                 effective_url="http://hub/v1/chat/completions")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.move(1)                                   # id → адрес
        await pilot.pause()
        assert form.presets_open is False              # поверх своего адреса — не сам
        assert form.reload_presets() is True           # Alt+↓ раскрывает явно
        await pilot.pause()
        rows = [str(w.render()) for w in form.query(".mef-preset-row") if w.display]
        assert rows and "основной конфиг" in rows[0], rows
        # Раскрытие поверх СВОЕГО адреса никого не подсвечивает (канон §2.4) — иначе
        # Enter молча затёр бы рабочий адрес первой строкой. Наследование выбирается
        # явно: ↓ (стрелка модального списка) и только потом Enter.
        assert form.presets_have_selection is False
        form.move(1)
        await pilot.pause()
        assert form.presets_have_selection is True
        assert form.enter() is True
        await pilot.pause()
        assert form.query_one("#mef-url", Input).value == ""
        ok, _alias, fields = form.submit()
        assert ok is True and fields["url"] is None


async def test_inherit_preset_leaves_the_field_empty():
    """Пункт «основной конфиг» в пресет-листе — осознанно оставить поле ПУСТЫМ:
    подстановка адреса убила бы живую связь с основным конфигом."""
    presets = [EndpointPreset(name="OpenRouter",
                              url="https://openrouter.ai/api/v1/chat/completions")]
    form = _form(initial=None, existing_aliases=set(), presets=presets,
                 effective_url="http://hub/v1/chat/completions")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.move(1); form.move(1)                     # алиас → id → адрес
        await pilot.pause()
        rows = [str(w.render()) for w in form.query(".mef-preset-row") if w.display]
        assert rows and "основной конфиг" in rows[0], rows
        assert form.enter() is True                    # Enter по пункту наследования
        await pilot.pause()
        assert form.query_one("#mef-url", Input).value == ""


async def test_untouched_token_field_keeps_the_saved_secret():
    """Заморозка токена: нетронутое поле не отдаёт ключ `token` — сохранённый
    секрет переживает правку соседнего поля; сам секрет в значение не заезжает."""
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                          "token": "sk-SECRETVALUE"},
                 existing_aliases={"flash"})
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        token = form.query_one("#mef-token", Input)
        assert token.value == ""                       # секрет не предзаполняется
        assert "SECRETVALUE" not in token.placeholder  # только маска
        assert "***" in token.placeholder
        assert token.has_class("has-saved")            # маска красится как значение
        form.query_one("#mef-url", Input).value = "https://new/v1/chat/completions"
        ok, _alias, fields = form.submit()
        assert ok is True
        assert "token" not in fields


async def test_no_auth_toggle_freezes_the_field_and_writes_empty_token():
    """Галка ● «без авторизации» — поле замораживается (раскладка не прыгает), а
    submit отдаёт явное `token: ""` — это НЕ то же самое, что «не задан»."""
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                          "token": "sk-secret"},
                 existing_aliases={"flash"})
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.move(-1)                                  # курсор → галка
        await pilot.pause()
        assert form.toggle_no_auth() is True
        await pilot.pause()
        token = form.query_one("#mef-token", Input)
        assert token.display is True and token.disabled is True
        assert token.value == "" and token.placeholder == "не используется"
        ok, _alias, fields = form.submit()
        assert ok is True and fields["token"] == ""


async def test_no_auth_switched_off_returns_the_record_to_inheritance():
    """Галку сняли, своего токена не ввели → ключ `token` УДАЛЯЕТСЯ (`None` —
    контракт писателя): иначе `token: ""` остался бы в файле навсегда, и
    переключатель работал бы только в одну сторону."""
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                          "token": ""},
                 existing_aliases={"flash"},
                 effective_auth="Bearer sk-HUB")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.move(-1)                                  # курсор → галка (стоит ●)
        await pilot.pause()
        assert form.toggle_no_auth() is True           # ● → ○
        ok, _alias, fields = form.submit()
        assert ok is True and fields["token"] is None


async def test_no_auth_already_active_and_untouched_writes_nothing():
    """Запись уже была `token: ""`, галку не трогали → ключ не переписывается
    заново («не изменилось — не пишем»)."""
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                          "token": ""},
                 existing_aliases={"flash"})
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        assert form.is_dirty() is False
        ok, _alias, fields = form.submit()
        assert ok is True and "token" not in fields


# --- алиас: валидация на месте -------------------------------------------------

async def test_reserved_alias_is_rejected_with_an_honest_error():
    form = _form(initial=None, existing_aliases=set())
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.query_one("#mef-alias", Input).value = "default"
        form.query_one("#mef-model", Input).value = "google/gemini-2.5-flash"
        ok, alias, fields = form.submit()
        assert (ok, alias, fields) == (False, None, None)
        error = str(form.query_one("#mef-error", Static).render())
        assert "зарезервировано" in error, error


async def test_duplicate_alias_is_rejected_with_an_honest_error():
    form = _form(initial=None, existing_aliases={"flash"})
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.query_one("#mef-alias", Input).value = "flash"
        form.query_one("#mef-model", Input).value = "google/gemini-2.5-flash"
        ok, _alias, _fields = form.submit()
        assert ok is False
        assert "уже используется" in str(form.query_one("#mef-error", Static).render())


async def test_empty_alias_is_rejected_with_an_honest_error():
    """Пустое имя — честная ошибка, а не молчаливый отказ и не выдумка имени."""
    form = _form(initial=None, existing_aliases=set())
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.query_one("#mef-model", Input).value = "google/gemini-2.5-flash"
        ok, _alias, _fields = form.submit()
        assert ok is False
        assert str(form.query_one("#mef-error", Static).render())


async def test_own_alias_is_not_its_own_duplicate_and_is_read_only_on_edit():
    """Редактирование: алиас — read-only заголовок (поля для ввода нет), и запись
    не считает саму себя дублем."""
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash"},
                 existing_aliases={"flash", "glm"})
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        assert not form.query("#mef-alias")            # поля ввода нет
        assert "flash" in str(form.query_one("#mef-alias-head", Static).render())
        ok, alias, _fields = form.submit()
        assert (ok, alias) == (True, "flash")


async def test_empty_model_id_means_the_alias_is_the_id():
    """Пусто в поле id — легально: алиас и есть провайдерский id (`model_router`:
    `entry.get("model") or alias`), ключ `model` не отдаётся."""
    form = _form(initial=None, existing_aliases=set())
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.query_one("#mef-alias", Input).value = "gigacode"
        ok, alias, fields = form.submit()
        assert (ok, alias, fields) == (True, "gigacode", {})


async def test_malformed_url_is_rejected():
    form = _form(initial=None, existing_aliases=set())
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.query_one("#mef-alias", Input).value = "flash"
        form.query_one("#mef-url", Input).value = "notaurl"
        ok, _alias, _fields = form.submit()
        assert ok is False
        assert "http(s)://" in str(form.query_one("#mef-error", Static).render())


# --- Alt+↓ (подбор) и Ctrl+Space (проба) — РАЗНЫЕ клавиши -----------------------

async def test_alt_down_fills_the_model_list():
    """`Alt+↓` на поле id идёт на эндпоинт и раскрывает список; Enter подставляет
    выделенную строку в поле."""
    calls = []

    def fetch(url, auth, **kwargs):
        calls.append((url, auth))
        return _models()

    form = _form(initial=None, existing_aliases=set(), fetch=fetch,
                 effective_url="http://hub/v1/chat/completions",
                 effective_auth="Bearer sk-HUB")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.move(1)                                   # курсор → id модели
        await pilot.pause()
        assert form.reload_models() is True
        for _ in range(3):
            await pilot.pause()
        assert calls == [("http://hub/v1/chat/completions", "Bearer sk-HUB")]
        rows = [str(w.render()) for w in _rows(form)]
        assert len(rows) == 2 and "google/gemini-2.5-flash" in rows[0], rows
        assert form.enter() is True                    # подставить выделенную
        await pilot.pause()
        assert form.query_one("#mef-model", Input).value == "google/gemini-2.5-flash"


async def test_alt_down_reports_a_dead_endpoint_instead_of_staying_silent():
    form = _form(initial=None, existing_aliases=set(), fetch=lambda *a, **k: None,
                 effective_url="http://hub/v1/chat/completions")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.move(1)
        await pilot.pause()
        assert form.reload_models() is True
        for _ in range(3):
            await pilot.pause()
        from tui.editors.field_list import LIST_FAILED
        assert LIST_FAILED in str(form.query_one("#mef-model-status", Static).render())


async def test_ctrl_space_probes_and_alt_down_fetches_and_they_never_swap():
    """Разведение клавиш (находка A4): проба зовёт ТОЛЬКО probe, подбор — ТОЛЬКО
    fetch. Вердикт пробы — differential, как в «Подключении»: контрольный запрос
    без токена + повтор с токеном при 401."""
    probes: list = []
    fetches: list = []

    def probe(endpoint, auth, verify, model, **kwargs):
        probes.append((endpoint, auth, model))
        if auth is None:
            return ProbeResult("rejected", 401, None, 5, "токен отклонён (401)")
        return ProbeResult("ok", 200, None, 7, "доступен")

    form = _form(initial=None, existing_aliases=set(),
                 fetch=lambda *a, **k: fetches.append(a) or _models(),
                 probe=probe,
                 effective_url="http://hub/v1/chat/completions",
                 effective_auth="Bearer sk-HUB")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.query_one("#mef-model", Input).value = "google/gemini-2.5-flash"
        # Правка поля обесценивает вердикт (и летящую пробу) — канон базы
        # (`SettingsScreen._reset_draft_state`): даём событию дойти ДО пробы,
        # иначе оно обесценило бы её же результат.
        await pilot.pause()
        assert form.probe_connection() is True
        for _ in range(3):
            await pilot.pause()
        assert fetches == [], "проба сходила за списком моделей"
        assert [p[1] for p in probes] == [None, "Bearer sk-HUB"], probes
        assert all(p[2] == "google/gemini-2.5-flash" for p in probes)
        line = str(form.query_one("#mef-probe-line", Static).render())
        assert "токен принят" in line, line

        probes.clear()
        form.move(1)                                   # курсор → id модели
        await pilot.pause()
        assert form.reload_models() is True
        for _ in range(3):
            await pilot.pause()
        assert probes == [], "подбор списка постучался пробой"
        assert fetches, "подбор списка не сходил на эндпоинт"


async def test_probe_without_any_address_is_honest():
    form = _form(initial=None, existing_aliases=set())
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        assert form.probe_connection() is True
        await pilot.pause()
        assert "адрес" in str(form.query_one("#mef-probe-line", Static).render())


# --- состояние наружу: хозяину нужен позиционный футер ---------------------------

async def test_form_tells_the_host_where_the_cursor_is_and_what_is_open():
    """Футер секции позиционный (Enter значит разное на разных строках), а обещания
    клавиш зависят от того, раскрыт ли список и есть ли в нём подсвеченная строка.
    Спрашивать это ДЕЙСТВИЯМИ нельзя (`reload_models()` ходит в сеть) — форма отдаёт
    read-only признаки, как `ConnectionEditor`."""
    presets = [EndpointPreset(name="OpenRouter",
                              url="https://openrouter.ai/api/v1/chat/completions")]
    form = _form(initial=None, existing_aliases=set(), presets=presets,
                 fetch=lambda *a, **k: _models(),
                 effective_url="http://hub/v1/chat/completions")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        assert (form.on_url_field, form.on_model_field) == (False, False)
        assert (form.presets_open, form.list_open) == (False, False)
        form.move(1)                                   # → id модели
        await pilot.pause()
        assert (form.on_model_field, form.list_open) == (True, False)
        assert form.list_has_selection is False
        assert form.reload_models() is True
        for _ in range(3):
            await pilot.pause()
        assert (form.list_open, form.list_has_selection) == (True, True)
        assert form.escape() is True
        await pilot.pause()
        assert form.list_open is False
        form.move(1)                                   # → адрес
        await pilot.pause()
        assert (form.on_url_field, form.on_model_field) == (True, False)
        assert (form.presets_open, form.presets_have_selection) == (True, True)
        form.query_one("#mef-url", Input).value = "https://my-own.internal/v1"
        await pilot.pause()
        # Строки видны все, но ни одна не подсвечена: Enter здесь оставит свой адрес,
        # и футер обязан читать именно этот признак, а не «список не пуст».
        assert (form.presets_open, form.presets_have_selection) == (True, False)


# --- вёрстка: рамка одна на поле и его список -----------------------------------

async def test_both_lists_live_inside_their_field_frames():
    """Канон §3.1 (эталон — `ConnectionEditor`): поле со списком и его строки — под
    ОДНОЙ рамкой-группой, а подсказка про клавиши остаётся снаружи (§3.2.2:
    внутри рамки только выбор). Оба списка формы играют по одному правилу."""
    from textual.containers import VerticalScroll
    presets = [EndpointPreset(name="OpenRouter",
                              url="https://openrouter.ai/api/v1/chat/completions")]
    form = _form(initial=None, existing_aliases=set(), presets=presets,
                 fetch=lambda *a, **k: _models(),
                 effective_url="http://hub/v1/chat/completions")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.move(1)                                   # курсор → id модели
        await pilot.pause()
        assert form.reload_models() is True
        for _ in range(3):
            await pilot.pause()
        model_group = form.query_one("#mef-model-group")
        assert form.query_one("#mef-model", Input).parent is model_group
        assert list(model_group.query(VerticalScroll)), "строки моделей — внутрь группы"
        assert not list(model_group.query(".mef-model-hint")), "подсказка — снаружи рамки"
        assert list(form.query(".mef-model-hint")), "подсказка списка обязана быть"
        # Раскрытый список модальный: пока в нём есть строки, ↑/↓ водят ПО НЕМУ —
        # уйти с поля можно только свернув его (канон §4.2.2).
        assert form.escape() is True
        await pilot.pause()
        form.move(1)                                   # курсор → адрес: пресеты раскрылись
        await pilot.pause()
        url_group = form.query_one("#mef-url-group")
        assert form.query_one("#mef-url", Input).parent is url_group
        assert list(url_group.query(VerticalScroll)), "строки пресетов — внутрь группы"
        assert not list(url_group.query(".mef-url-preset-hint")), "подсказка — снаружи рамки"


# --- сквозной с Task 1: merge писателя ------------------------------------------

async def test_submit_after_url_edit_keeps_pricing(tmp_path):
    """Форма отдаёт только явно заданные поля, merge делает writer: правка адреса
    не теряет `pricing`, о котором форма не знает вовсе."""
    from config_modules.registry_write import upsert_model
    target = tmp_path / "config.yml"
    target.write_text(_REGISTRY_WITH_PRICING, encoding="utf-8")
    template = Path(_default_config_path()).read_text(encoding="utf-8")
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                          "url": "https://openrouter.ai/api/v1/chat/completions",
                          "token": "sk-secret"},
                 existing_aliases={"flash"})
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.query_one("#mef-url", Input).value = "https://new/v1/chat/completions"
        ok, alias, fields = form.submit()
        assert ok is True
    upsert_model(target, alias, fields, template)
    entry = yaml.safe_load(target.read_text(encoding="utf-8"))["models"]["flash"]
    assert entry["url"] == "https://new/v1/chat/completions"
    assert entry["pricing"] == {"input": 0.3, "output": 1.2}
    assert entry["token"] == "sk-secret"               # нетронутое поле не стёрло секрет
