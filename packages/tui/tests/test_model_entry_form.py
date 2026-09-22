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
from gc_core.config import _default_config_path
from gc_core.config_modules.endpoint_presets import EndpointPreset
from gc_core.model_picker import ModelInfo
from gc_tui.services.settings_probe import ProbeResult
from textual.app import App, ComposeResult
from textual.widgets import Input, Static


def _harness(editor):
    class _App(App):
        def compose(self) -> ComposeResult:
            yield editor
    return _App()


def _form(**kwargs):
    """Форма с инъекцией сети-заглушки: тесты, которым сеть не нужна, всё равно не
    должны иметь ни малейшего шанса в неё сходить."""
    from gc_tui.editors.model_entry_form import ModelEntryForm
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
        form.escape()                                 # свернуть пресеты входа
        form.move(-1); form.move(-1)                  # адрес → алиас → id модели
        for _ in range(3):
            await pilot.pause()                       # автоподгрузка раскрыла список
        assert _rows(form), "список не раскрылся"
        assert form.escape() is True                  # свернуть список
        await pilot.pause()
        assert not _rows(form)
        assert form.escape() is False                 # сворачивать нечего — клавиша хозяину
        assert target.read_bytes() == before


async def test_is_dirty_is_honest():
    """Вход без правок — не dirty; правка поля и переключение галки — dirty.

    Пара у записи своя: галка ● живёт только в этом состоянии (§4), а половину пары
    (адрес без токена) правило вообще запрещает."""
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                          "url": "https://openrouter.ai/api/v1/chat/completions",
                          "token": "sk-secret"},
                 existing_aliases={"flash"})
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        assert form.is_dirty() is False
        form.query_one("#mef-model", Input).value = "zhipu/glm-4.6"
        assert form.is_dirty() is True
        form.query_one("#mef-model", Input).value = "google/gemini-2.5-flash"
        assert form.is_dirty() is False
        form.move(1); form.move(1)                     # адрес → токен → галка
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


async def test_the_url_placeholder_names_the_address_that_really_acts():
    """Адрес записи может жить во ВТОРОМ написании (`backend.completions_url`) — поле
    `url` при этом пусто, и placeholder говорил «наследуется: <общий адрес>». Это ложь:
    у `backend.completions_url` приоритет над общим (`model_router._resolve_alias`), и
    названный призраком общий адрес не действует.

    Поле обязано называть ДЕЙСТВУЮЩИЙ адрес — тем же правилом, каким поле токена
    показывает маску своего секрета записи, а не унаследованного."""
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                          "token": "sk-OWN",
                          "backend": {"completions_url": "https://my-host/v1"}},
                 existing_aliases={"flash"},
                 effective_url="http://hub/v1/chat/completions",
                 effective_auth="Bearer sk-HUB")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        assert form.query_one("#mef-url", Input).value == ""
        assert form.query_one("#mef-url", Input).placeholder == "https://my-host/v1"
        # Токен той же записи и так честен — маска СВОЕГО секрета, не общего.
        assert "sk-HUB" not in form.query_one("#mef-token", Input).placeholder


async def test_clearing_a_prefilled_url_removes_the_key(tmp_path):
    """Стёртое ПРЕДЗАПОЛНЕННОЕ поле — это «снять», а не «промолчать»: ключ уходит
    из записи (`None` — контракт писателя), и адрес снова наследуется от основного
    конфига. Иначе форма показывала бы «изменено», Enter отвечал бы «сохранено», а
    в файле оставался бы прежний адрес.

    Одним адресом это больше не выражается (§2): он уходит ВМЕСТЕ с токеном — пара
    возвращается к общей целиком, и в файле не остаётся ни того, ни другого."""
    from gc_core.config_modules.registry_write import upsert_model
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
        # Половину пары форма не сохранит и скажет почему — совет исполним отсюда же:
        # поле токена открыто, курсор на него встаёт.
        assert form.submit()[0] is False
        form.escape()                                   # опустевшее поле раскрыло пресеты
        form.move(1)                                    # адрес → токен
        await pilot.pause()
        assert form.clear_token() is True
        ok, alias, fields = form.submit()
        assert ok is True and fields["url"] is None and fields["token"] is None
    upsert_model(target, alias, fields, template)
    entry = yaml.safe_load(target.read_text(encoding="utf-8"))["models"]["flash"]
    assert "url" not in entry, entry                    # ключи сняты — наследование
    assert "token" not in entry, entry
    assert entry["pricing"] == {"input": 0.3, "output": 1.2}


async def test_clearing_a_prefilled_model_id_removes_the_key(tmp_path):
    """То же для провайдерского id: стёрли — ключ снят, и алиас снова служит id
    (`model_router`: `entry.get("model") or alias`). Без этого «пусто = алиас и
    есть id» было бы недостижимо на правке."""
    from gc_core.config_modules.registry_write import upsert_model
    target = tmp_path / "config.yml"
    target.write_text(_REGISTRY_WITH_PRICING, encoding="utf-8")
    template = Path(_default_config_path()).read_text(encoding="utf-8")
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                          "url": "https://openrouter.ai/api/v1/chat/completions",
                          "token": "sk-secret"},
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
        await pilot.pause()                            # курсор уже на адресе (позиция 1)
        assert form.presets_open is False              # поверх своего адреса — не сам
        assert form.reload_presets() is True           # Alt+↓ раскрывает явно
        await pilot.pause()
        rows = [str(w.render()) for w in form.query(".mef-preset-row") if w.display]
        assert rows and "раздел «Подключение» · наследовать" in rows[0], rows
        # Слово «наследовать» несущее: адрес пункта часто совпадает с
        # обычным пресетом, и без него строка читалась бы дублем.
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
        await pilot.pause()                            # курсор на адресе, список раскрыт
        rows = [str(w.render()) for w in form.query(".mef-preset-row") if w.display]
        assert rows and "раздел «Подключение» · наследовать" in rows[0], rows
        # Слово «наследовать» несущее: адрес пункта часто совпадает с
        # обычным пресетом, и без него строка читалась бы дублем.
        assert form.enter() is True                    # Enter по пункту наследования
        await pilot.pause()
        assert form.query_one("#mef-url", Input).value == ""


# --- адрес и токен — одно состояние (§4) ---------------------------------------

async def test_token_is_read_only_while_the_connection_is_shared():
    """§4: токен и галка доступны только при СВОЁМ адресе. Пока адрес общий, решать про
    токен нечего — он придёт вместе с адресом, а набранный над общим адресом токен был бы
    ровно запрещённой половиной."""
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash"},
                 existing_aliases={"flash"},
                 effective_url="https://openrouter.ai/api/v1/chat/completions",
                 effective_auth="Bearer sk-COMMON")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        token = form.query_one("#mef-token", Input)
        assert token.disabled is True
        assert "токен приходит из раздела «Подключение»" in str(
            form.query_one("#mef-noauth", Static).render())
        assert form.own_connection() is False


async def test_typing_an_address_opens_the_token_and_drops_the_inherited_mask():
    """Печать в адресе — вход в «своё»: маска унаследованного токена обещала бы чужой
    секрет для чужого теперь адреса."""
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash"},
                 existing_aliases={"flash"},
                 effective_url="https://openrouter.ai/api/v1/chat/completions",
                 effective_auth="Bearer sk-COMMON")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.query_one("#mef-url", Input).value = "https://my-host/v1/chat/completions"
        await pilot.pause()
        token = form.query_one("#mef-token", Input)
        assert token.disabled is False
        assert token.placeholder == "укажите токен для этого адреса"
        assert form.own_connection() is True


async def test_the_pair_row_says_where_the_address_and_token_come_from():
    """§4: состояние пары не переключается отдельным элементом — оно выводится из полей,
    и строка над ними называет, откуда пара взята."""
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash"},
                 existing_aliases={"flash"},
                 effective_url="https://openrouter.ai/api/v1/chat/completions",
                 effective_auth="Bearer sk-COMMON")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        row = form.query_one("#mef-pair", Static)
        assert str(row.render()) == "Адрес и токен: наследуются из раздела «Подключение»"
        form.query_one("#mef-url", Input).value = "https://my-host/v1/chat/completions"
        await pilot.pause()
        assert str(row.render()) == "Адрес и токен: свои"


async def test_empty_common_token_is_explained_not_masked():
    """§4: `Authorization: ""` приводится к None (`settings.py:854-856`) — маски быть не
    может. Наследование пустого общего токена законно: запись не задаёт ничего, значит
    наследует пару целиком и работает без авторизации."""
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash"},
                 existing_aliases={"flash"},
                 effective_url="https://openrouter.ai/api/v1/chat/completions",
                 effective_auth=None)
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        token = form.query_one("#mef-token", Input)
        assert token.placeholder == "общий токен не задан — запросы идут без авторизации"
        ok, _alias, fields = form.submit()
        assert ok is True and "token" not in fields and "url" not in fields


async def test_the_form_refuses_to_save_an_address_without_a_token():
    """§2: своё подключение задаётся ЦЕЛИКОМ, и форма обязана не давать собрать то, что
    валидатор конфига отвергнет, — иначе отказ придёт от писателя, когда правка уже
    сделана."""
    form = _form(initial=None, existing_aliases=set(),
                 effective_url="https://openrouter.ai/api/v1/chat/completions",
                 effective_auth="Bearer sk-COMMON")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.query_one("#mef-alias", Input).value = "flash"
        form.query_one("#mef-url", Input).value = "https://my-host/v1/chat/completions"
        await pilot.pause()
        ok, _alias, _fields = form.submit()
        assert ok is False
        assert str(form.query_one("#mef-error", Static).render()) == (
            "задан свой адрес, но не задан токен — укажите токен "
            "или отметьте «без авторизации»")
        form.query_one("#mef-token", Input).value = "sk-OWN"
        ok, alias, fields = form.submit()
        assert (ok, alias) == (True, "flash")
        assert fields["url"] == "https://my-host/v1/chat/completions"
        assert fields["token"] == "sk-OWN"


async def test_the_form_refuses_to_save_a_token_without_an_address():
    """Симметричный конец правила: стёртый адрес — «пусто» становится решением при
    сохранении (§4, краевые случаи), а свой токен записи никуда не делся. Поле токена при
    этом остаётся открытым: иначе снять оставшуюся половину было бы нечем."""
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                          "url": "https://my-host/v1/chat/completions",
                          "token": "sk-OWN"},
                 existing_aliases={"flash"},
                 effective_url="https://openrouter.ai/api/v1/chat/completions",
                 effective_auth="Bearer sk-COMMON")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.query_one("#mef-url", Input).value = ""
        await pilot.pause()
        assert form.query_one("#mef-token", Input).disabled is False
        ok, _alias, _fields = form.submit()
        assert ok is False
        assert str(form.query_one("#mef-error", Static).render()) == (
            "задан свой токен, но не задан адрес — укажите адрес "
            "или верните наследование")


async def test_the_checkbox_is_inert_while_the_connection_is_shared():
    """Галка неактивна при общей паре (§4), и знает это ФОРМА, а не навигация: сегодня
    курсор на строку не встаёт, но обработчик Task 3 переезжает на уровень формы, и
    гейт, оставленный на одной позиции курсора, там развалится."""
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash"},
                 existing_aliases={"flash"},
                 effective_url="https://openrouter.ai/api/v1/chat/completions",
                 effective_auth="Bearer sk-COMMON")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        # Ставим курсор на строку галки в обход навигации — она-то её и пропускает.
        form._idx = form._positions.index(None)
        assert form.toggle_no_auth() is False
        assert form.own_connection() is False
        assert "○" in str(form.query_one("#mef-noauth", Static).render())


async def test_erasing_the_address_leaves_the_own_token_reachable():
    """Тупик, который правило могло бы построить само: набрали свою пару, стёрли адрес.
    Состояние на лету не переключается (§4, краевые случаи) — иначе поле токена
    замёрзло бы вместе с набранным ключом, курсор на него не встаёт, и совет отказа
    («укажите адрес или верните наследование») стал бы неисполним."""
    form = _form(initial=None, existing_aliases=set(),
                 effective_url="https://openrouter.ai/api/v1/chat/completions",
                 effective_auth="Bearer sk-COMMON")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.query_one("#mef-alias", Input).value = "flash"
        form.query_one("#mef-url", Input).value = "https://my-host/v1/chat/completions"
        await pilot.pause()
        form.query_one("#mef-token", Input).value = "sk-OWN"
        await pilot.pause()
        form.query_one("#mef-url", Input).value = ""
        await pilot.pause()
        token = form.query_one("#mef-token", Input)
        assert form.own_connection() is True            # своё осталось — токен
        assert token.disabled is False and token.value == "sk-OWN"
        form.move(1)                                    # адрес → токен: позиция жива
        await pilot.pause()
        ok, _alias, _fields = form.submit()
        assert ok is False
        # Совет отказа исполним прямо здесь: стёрли набранный токен — пара снова общая.
        token.value = ""
        await pilot.pause()
        assert form.own_connection() is False
        ok, alias, fields = form.submit()
        assert (ok, alias) == (True, "flash")
        assert "url" not in fields and "token" not in fields


async def test_the_inherit_preset_returns_the_whole_pair_not_just_the_address():
    """Пункт «раздел «Подключение» · наследовать» — «вернуть общие», а не «очистить адрес»
    (§5): оставленный токен был бы ровно запрещённой половиной, снять которую в общей
    паре нечем. Уходят оба ключа разом."""
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                          "url": "https://my-host/v1/chat/completions",
                          "token": "sk-OWN"},
                 existing_aliases={"flash"},
                 effective_url="https://openrouter.ai/api/v1/chat/completions",
                 effective_auth="Bearer sk-COMMON")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        assert form.reload_presets() is True            # Alt+↓ поверх своего адреса
        await pilot.pause()
        form.move(1)                                    # подсветить пункт наследования
        await pilot.pause()
        assert form.enter() is True
        await pilot.pause()
        assert form.own_connection() is False
        assert form.query_one("#mef-token", Input).disabled is True
        assert str(form.query_one("#mef-pair", Static).render()) == (
            "Адрес и токен: наследуются из раздела «Подключение»")
        ok, _alias, fields = form.submit()
        assert ok is True
        assert fields["url"] is None and fields["token"] is None


async def test_the_second_writing_of_the_pair_is_read_as_own():
    """§2 знает два написания пары, и форма обязана считать её тем же ядровым
    определением (`model_registry.connection_parts`): иначе запись, задавшую своё
    подключение через `backend`, форма объявит общей и откажется сохранить БЕЗ единой
    правки — а починить это в ней нечем, `backend` она не правит."""
    by_backend_url = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                                    "token": "sk-OWN",
                                    "backend": {"completions_url": "https://my-host/v1"}},
                           existing_aliases={"flash"},
                           effective_url="https://openrouter.ai/api/v1/chat/completions",
                           effective_auth="Bearer sk-COMMON")
    async with _harness(by_backend_url).run_test() as pilot:
        await pilot.pause()
        assert by_backend_url.own_connection() is True
        assert by_backend_url.query_one("#mef-token", Input).disabled is False
        ok, _alias, fields = by_backend_url.submit()
        # Правок не было — отказывать не в чем, и ключей пары форма не трогает.
        assert ok is True
        assert "url" not in fields and "token" not in fields
    by_backend_auth = _form(
        initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                 "url": "https://my-host/v1/chat/completions",
                 "backend": {"chat_completion_headers": {"Authorization": "Bearer sk-OWN"}}},
        existing_aliases={"flash"},
        effective_url="https://openrouter.ai/api/v1/chat/completions",
        effective_auth="Bearer sk-COMMON")
    async with _harness(by_backend_auth).run_test() as pilot:
        await pilot.pause()
        assert by_backend_auth.own_connection() is True
        ok, _alias, fields = by_backend_auth.submit()
        assert ok is True
        assert "token" not in fields


async def test_probe_does_not_send_the_inherited_token_to_a_typed_address():
    """§4: в сеть уходит только СОБРАННАЯ пара. Иначе «напечатал чужой адрес → нажал
    Ctrl+Space» отправляет общий ключ на чужой хост ещё до сохранения.

    Смотрим на ВСЕ вызовы пробы, а не на первый: differential-танец
    (`settings_probe.probe_connection`) первым всегда шлёт контроль БЕЗ токена, и утечка
    видна только во втором — том, что идёт после 401."""
    seen = []

    def fake_probe(endpoint, auth, verify, model, **kwargs):
        seen.append((endpoint, auth))
        return ProbeResult("rejected", 401, None, 5, "токен отклонён (401)")

    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash"},
                 existing_aliases={"flash"},
                 effective_url="https://openrouter.ai/api/v1/chat/completions",
                 effective_auth="Bearer sk-COMMON", probe=fake_probe)
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.query_one("#mef-url", Input).value = "https://foreign/v1/chat/completions"
        await pilot.pause()
        assert form.probe_connection() is True
        for _ in range(3):
            await pilot.pause()
        assert seen and all(e.startswith("https://foreign") for e, _ in seen), seen
        assert [auth for _, auth in seen] == [None], seen   # общий ключ не ушёл


async def test_the_model_list_does_not_send_the_inherited_token_to_a_typed_address():
    """Тот же страж для АВТОПОДГРУЗКИ: список уходит на эндпоинт при простом переходе
    курсора на поле id — без единой сетевой клавиши и до всякого сохранения."""
    calls = []

    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash"},
                 existing_aliases={"flash"},
                 fetch=lambda url, auth, **k: calls.append((url, auth)) or None,
                 effective_url="https://openrouter.ai/api/v1/chat/completions",
                 effective_auth="Bearer sk-COMMON")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.query_one("#mef-url", Input).value = "https://foreign/v1/chat/completions"
        await pilot.pause()
        form.escape()                                  # свернуть пресеты входа
        form.move(-1); form.move(-1)                   # адрес → алиас → id модели
        for _ in range(3):
            await pilot.pause()
        assert calls == [("https://foreign/v1/chat/completions", None)], calls


async def test_untouched_token_field_keeps_the_saved_secret():
    """Заморозка токена: нетронутое поле не отдаёт ключ `token` — сохранённый
    секрет переживает правку соседнего поля; сам секрет в значение не заезжает."""
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                          "url": "https://openrouter.ai/api/v1/chat/completions",
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
    submit отдаёт явное `token: ""` — это НЕ то же самое, что «не задан». Пара при этом
    остаётся целой: `token: ""` — заданный токен своего адреса (§2)."""
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                          "url": "https://openrouter.ai/api/v1/chat/completions",
                          "token": "sk-secret"},
                 existing_aliases={"flash"})
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.move(1); form.move(1)                     # адрес → токен → галка
        await pilot.pause()
        assert form.toggle_no_auth() is True
        await pilot.pause()
        token = form.query_one("#mef-token", Input)
        assert token.display is True and token.disabled is True
        assert token.value == "" and token.placeholder == "не используется"
        ok, _alias, fields = form.submit()
        assert ok is True and fields["token"] == ""


async def test_no_auth_switched_off_returns_the_record_to_inheritance():
    """Снятая галка не создаёт запрещённое состояние МОЛЧА: над своим адресом ○ значит
    «токена нет» — ровно половина пары (§2), и форма отказывает, называя причину.
    Раньше здесь возвращалось наследование одного токена — при правиле §2 это и есть
    запрещённая половина, а возврат пары целиком делает Ctrl+U.

    Дорога назад при этом остаётся: стёртый адрес уводит пару в общую, и `token: ""`
    уходит из файла вместе с ним (`None` — контракт писателя). Иначе пустая строка
    осталась бы в записи навсегда, и переключатель работал бы в одну сторону."""
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                          "url": "https://my-host/v1/chat/completions",
                          "token": ""},
                 existing_aliases={"flash"},
                 effective_url="https://openrouter.ai/api/v1/chat/completions",
                 effective_auth="Bearer sk-HUB")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        # Один move: замороженное ● поле токена курсор пропускает сам.
        form.move(1)                                   # адрес → (токен ●) → галка
        await pilot.pause()
        assert form.toggle_no_auth() is True           # ● → ○
        await pilot.pause()
        assert form.submit()[0] is False
        assert str(form.query_one("#mef-error", Static).render()) == (
            "задан свой адрес, но не задан токен — укажите токен "
            "или отметьте «без авторизации»")
        form.query_one("#mef-url", Input).value = ""
        await pilot.pause()
        assert form.own_connection() is False
        ok, _alias, fields = form.submit()
        assert ok is True and fields["url"] is None and fields["token"] is None


async def test_no_auth_already_active_and_untouched_writes_nothing():
    """Запись уже была `token: ""`, галку не трогали → ключ не переписывается
    заново («не изменилось — не пишем»)."""
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                          "url": "https://openrouter.ai/api/v1/chat/completions",
                          "token": ""},
                 existing_aliases={"flash"})
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        assert form.is_dirty() is False
        ok, _alias, fields = form.submit()
        assert ok is True and "token" not in fields


# --- Ctrl+U: свой токен снят, запись наследует общий ---------------------------

async def test_ctrl_u_drops_the_own_token_mask_and_shows_the_inherited_one():
    """Ctrl+U на поле токена возвращает запись к общему токену «Подключения», и
    видно это СРАЗУ: маска своего секрета уходит, placeholder называет маской то, что
    теперь унаследуется. Результат клавиши обязан быть виден ДО сохранения — иначе
    она снова «невыводима из экрана» (смок владельца)."""
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                          "token": "sk-OWNSECRET"},
                 existing_aliases={"flash"},
                 effective_auth="Bearer sk-HUBSECRET")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        token = form.query_one("#mef-token", Input)
        assert "OWNSECRET" not in token.placeholder    # свой секрет — только маской
        assert not token.placeholder.startswith("наследуется")
        form.move(1)                                   # адрес → токен
        await pilot.pause()
        assert form.clear_token() is True
        await pilot.pause()
        assert form.token_cleared is True
        assert token.placeholder.startswith("наследуется")
        assert "HUBSECRET" not in token.placeholder    # унаследованный — тоже маской
        assert form.is_dirty() is True


@pytest.mark.parametrize("field_id", ["mef-url", "mef-token"])
async def test_ctrl_u_returns_the_whole_pair_from_either_field(field_id):
    """§5: клавиша живёт на уровне формы — под галкой ● поле токена disabled и курсором
    пропускается, поэтому «на любом из двух полей» иначе выродилось бы в «только на
    адресе». С обоих полей результат ОДИН: пара уходит к общей целиком."""
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                          "url": "https://my-host/v1/chat/completions",
                          "token": "sk-OWN"},
                 existing_aliases={"flash"},
                 effective_url="https://openrouter.ai/api/v1/chat/completions",
                 effective_auth="Bearer sk-HUB")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        for _ in form._positions:                      # дойти до поля своей ходьбой
            if form._positions[form._idx] == field_id:
                break
            form.move(1)
        await pilot.pause()
        assert form._positions[form._idx] == field_id, "позиция недостижима курсором"
        assert form.clear_token() is True
        await pilot.pause()
        assert form.own_connection() is False
        assert form.query_one("#mef-url", Input).value == ""
        assert form.query_one("#mef-token", Input).disabled is True
        assert str(form.query_one("#mef-pair", Static).render()) == (
            "Адрес и токен: наследуются из раздела «Подключение»")
        ok, _alias, fields = form.submit()
        assert ok is True and fields["url"] is None and fields["token"] is None


async def test_ctrl_u_from_the_token_field_leaves_the_cursor_on_a_live_field():
    """Клавиша замораживает поле ПОД курсором (общая пара → токен `disabled`), и Textual
    сам уводит фокус по focus chain — на алиас, где Input делает select-all. Курсор формы
    при этом остаётся на позиции токена: Alt+↓ и Space мертвы, а любая печать ЗАМЕНЯЕТ
    имя записи, и Enter молча переименовывает её с каскадом по слотам и привязкам.

    Форма — хозяин курсора: заморозив позицию под ним, она обязана сама перевести его на
    законную (та же дисциплина, что у `move`/`_skip_position`). Соседняя незамороженная
    и первая в паре — «Адрес». Пресет-лист при этом раскрываться НЕ должен: над опустевшим
    полем следующий Enter подставил бы строку списка вместо сохранения."""
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                          "url": "https://my-host/v1/chat/completions",
                          "token": "sk-OWN"},
                 existing_aliases={"flash"},
                 effective_url="https://openrouter.ai/api/v1/chat/completions",
                 effective_auth="Bearer sk-HUB")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.move(1)                                   # адрес → токен
        await pilot.pause()
        assert form._positions[form._idx] == "mef-token"
        assert form.clear_token() is True
        await pilot.pause()
        assert pilot.app.focused is form.query_one("#mef-url", Input)
        assert form.on_url_field is True               # футер обещает клавиши адреса
        assert form.presets_open is False              # Enter сохраняет, а не подставляет
        await pilot.press("g")
        await pilot.pause()
        assert form.query_one("#mef-alias", Input).value == "flash"
        assert form.query_one("#mef-url", Input).value == "g"   # печать ушла под курсор
        form.query_one("#mef-url", Input).value = ""
        await pilot.pause()
        ok, alias, _fields = form.submit()
        assert (ok, alias) == (True, "flash")          # Enter не переименовал запись


async def test_ctrl_u_takes_the_no_auth_checkbox_off_too():
    """Галка ● — часть пары (`token: ""` над своим адресом, §2), и Ctrl+U снимает её
    вместе с обоими полями. Оставь он галку — запись осталась бы «своей» с пустой
    половиной, а снять галку было бы нечем: при общей паре она неактивна.

    Курсор при этом стоит на адресе, и не случайно: под ● поле токена заморожено и
    курсором пропускается — вход в клавишу здесь ровно один."""
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                          "url": "https://my-host/v1/chat/completions",
                          "token": ""},
                 existing_aliases={"flash"},
                 effective_url="https://openrouter.ai/api/v1/chat/completions",
                 effective_auth="Bearer sk-HUB")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        assert "●" in str(form.query_one("#mef-noauth", Static).render())
        assert form.clear_token() is True
        await pilot.pause()
        assert "○" in str(form.query_one("#mef-noauth", Static).render())
        assert form.own_connection() is False
        ok, _alias, fields = form.submit()
        assert ok is True and fields["url"] is None and fields["token"] is None


async def test_ctrl_u_gets_the_record_out_of_half_a_pair():
    """Правило ПЕРЕВЕРНУЛО прежний случай: свой адрес без токена — половина пары,
    сохранить её нельзя (§2), и Ctrl+U здесь обязан работать — он и есть выход. Прежняя
    редакция клавиши отвечала на этот случай «не наша»: гейт требовал непустого своего
    токена, то есть молчал ровно там, где человек и застревает."""
    form = _form(initial=None, existing_aliases=set(),
                 effective_url="https://openrouter.ai/api/v1/chat/completions",
                 effective_auth="Bearer sk-HUB")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.query_one("#mef-alias", Input).value = "flash"
        form.query_one("#mef-url", Input).value = "https://my-host/v1/chat/completions"
        await pilot.pause()
        assert form.submit()[0] is False               # половина пары — не сохранить
        assert form.clear_token() is True              # курсор на адресе
        await pilot.pause()
        assert form.own_connection() is False
        assert form.query_one("#mef-url", Input).value == ""
        ok, alias, fields = form.submit()
        assert (ok, alias) == (True, "flash")
        assert "url" not in fields and "token" not in fields


async def test_the_refusal_goes_away_with_the_state_it_was_about():
    """Отказ живёт ровно столько, сколько состояние, о котором он сказан: человек
    послушался и нажал обещанную клавишу — красная строка про половину пары обязана
    уйти вместе с половиной. Иначе форма спорит сама с собой: строка над полями говорит
    «общие», отказ под ними — «задан свой адрес»."""
    form = _form(initial=None, existing_aliases=set(),
                 effective_url="https://openrouter.ai/api/v1/chat/completions",
                 effective_auth="Bearer sk-HUB")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.query_one("#mef-alias", Input).value = "flash"
        form.query_one("#mef-url", Input).value = "https://my-host/v1/chat/completions"
        await pilot.pause()
        assert form.submit()[0] is False
        error = form.query_one("#mef-error", Static)
        assert error.display is True
        assert form.clear_token() is True
        await pilot.pause()
        assert error.display is False
        assert str(error.render()) == ""


async def test_the_token_label_stays_silent_under_the_no_auth_checkbox():
    """Под ● поле токена заморожено и курсором пропускается — обещать в ЕГО подписи
    клавишу значит звать её туда, куда не встать. Выход при этом не теряется: клавиша
    работает с поля адреса, и там её называет футер."""
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                          "url": "https://my-host/v1/chat/completions",
                          "token": ""},
                 existing_aliases={"flash"},
                 effective_url="https://openrouter.ai/api/v1/chat/completions",
                 effective_auth="Bearer sk-HUB")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        assert "●" in str(form.query_one("#mef-noauth", Static).render())
        assert str(form.query_one("#mef-token-label", Static).render()) == "Токен"
        assert form.clear_token() is True               # с адреса клавиша всё равно наша


async def test_the_second_writing_says_where_the_pair_is_edited():
    """Пара во втором написании живёт в секции `backend`, а форма её не правит — и
    обязана это СКАЗАТЬ. Иначе строка «Адрес и токен: свои» обещает правку теми полями,
    которые к этой паре отношения не имеют, и человеку негде узнать, куда идти."""
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                          "token": "sk-OWN",
                          "backend": {"completions_url": "https://my-host/v1"}},
                 existing_aliases={"flash"},
                 effective_url="https://openrouter.ai/api/v1/chat/completions",
                 effective_auth="Bearer sk-HUB")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        assert str(form.query_one("#mef-pair", Static).render()) == (
            "Адрес и токен: свои — заданы секцией backend, правятся в файле")


async def test_ctrl_u_is_neither_promised_nor_taken_over_a_pair_in_backend():
    """Клавиша не обещается там, где не может доделать: пара (или её половина) задана
    секцией `backend`, форма её не правит — снятые `url`/`token` оставили бы запись
    своей, и нажатие выглядело бы сделанным, ничего не сделав.

    «Сделал» и «не сделал» не должны выглядеть одинаково: действие отвечает False, и
    клавиша уходит `Input` — второе нажатие подряд рапортовало бы успех над неизменным
    состоянием."""
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                          "token": "sk-OWN",
                          "backend": {"completions_url": "https://my-host/v1"}},
                 existing_aliases={"flash"},
                 effective_url="https://openrouter.ai/api/v1/chat/completions",
                 effective_auth="Bearer sk-HUB")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        assert form.own_connection() is True
        assert str(form.query_one("#mef-token-label", Static).render()) == "Токен"
        assert form.can_return_shared_pair is False
        assert form.clear_token() is False              # курсор на адресе
        form.move(1)                                    # адрес → токен (поле живое)
        await pilot.pause()
        assert form.can_return_shared_pair is False
        assert form.clear_token() is False
        assert form.clear_token() is False              # и второе нажатие — тот же ответ
        assert form.token_cleared is False
        assert form.query_one("#mef-token", Input).value == ""
        assert form.own_connection() is True            # состояние не тронуто


async def test_the_inherit_preset_is_gated_like_the_key_that_does_the_same():
    """§5: пункт «раздел «Подключение» · наследовать» и Ctrl+U — два входа в ОДНО действие,
    значит и гейт у них один. Пара во втором написании (секция `backend`) форме не
    подвластна: клавиша честно отказывает, а пункт проходил — снимал токен и оставлял
    запись с адресом из `backend` без своего токена, то есть запрещённой половиной,
    которую `submit()` отвергает. Тупик: строка пары говорит «правятся в файле», а
    форма требует токен, взять который ей неоткуда.

    Обратная половина обязательна: там, где действие исполнимо, пункт стоит на месте —
    он и есть явный вход в наследование, и убрать его заодно с тупиком значило бы
    лечить болезнь ампутацией."""
    in_backend = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                                "token": "sk-OWN",
                                "backend": {"completions_url": "https://my-host/v1"}},
                       existing_aliases={"flash"},
                       effective_url="http://hub/v1/chat/completions",
                       effective_auth="Bearer sk-HUB")
    async with _harness(in_backend).run_test() as pilot:
        await pilot.pause()
        in_backend.reload_presets()
        await pilot.pause()
        rows = [str(w.render()) for w in in_backend.query(".mef-preset-row")]
        assert not any("наследовать" in r for r in rows), rows
        assert in_backend.token_cleared is False        # и снимать было нечем

    own = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                         "url": "https://my-host/v1/chat/completions", "token": "sk-OWN"},
                existing_aliases={"flash"},
                effective_url="http://hub/v1/chat/completions",
                effective_auth="Bearer sk-HUB")
    async with _harness(own).run_test() as pilot:
        await pilot.pause()
        assert own.reload_presets() is True
        await pilot.pause()
        rows = [str(w.render()) for w in own.query(".mef-preset-row") if w.display]
        assert rows and "раздел «Подключение» · наследовать" in rows[0], rows


async def test_ctrl_u_is_not_ours_off_the_pair_fields_or_over_a_shared_pair():
    """Клавиша работает на полях ПАРЫ и только пока пара своя. Иначе False — и хозяин
    обязан отдать её `Input`: на алиасе и id модели у Ctrl+U своё штатное дело, и там
    оно настоящее — эти поля предзаполнены, стирать в них есть что."""
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                          "url": "https://openrouter.ai/api/v1/chat/completions",
                          "token": "sk-OWNSECRET"},
                 existing_aliases={"flash"})
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.move(-1)                                  # адрес → алиас (по кольцу)
        await pilot.pause()
        assert form.clear_token() is False             # поле алиаса — не поле пары
        assert form.token_cleared is False
        assert form.own_connection() is True           # пара цела, клавиша её не тронула
    # Вторая половина — про «возвращать нечего»: пара общая, и оба её поля пусты.
    shared = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash"},
                   existing_aliases={"flash"},
                   effective_url="https://openrouter.ai/api/v1/chat/completions",
                   effective_auth="Bearer sk-HUB")
    async with _harness(shared).run_test() as pilot:
        await pilot.pause()
        assert shared.own_connection() is False
        assert shared.clear_token() is False           # курсор на адресе — и всё равно нет
        assert shared.token_cleared is False


async def test_the_token_label_promises_ctrl_u_only_when_there_is_something_to_drop():
    """Обещание клавиши живёт в ПОДПИСИ ПОЛЯ (канон `ConnectionEditor._token_label`)
    и ровно в том состоянии, где клавиша работает: пара своя — подпись зовёт «вернуть
    общие»; пара общая (в том числе сразу после Ctrl+U) — возвращать нечего, подписи
    нет. Гейт именно ПАРА, а не свой токен: над своим адресом без токена клавиша тоже
    работает — она и есть выход из половины пары."""
    from textual.widgets import Static
    inherited = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash"},
                      existing_aliases={"flash"},
                      effective_auth="Bearer sk-HUB")
    async with _harness(inherited).run_test() as pilot:
        await pilot.pause()
        assert str(inherited.query_one("#mef-token-label", Static).render()) == "Токен"
    own = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                         "url": "https://openrouter.ai/api/v1/chat/completions",
                         "token": "sk-OWNSECRET"},
                existing_aliases={"flash"},
                effective_auth="Bearer sk-HUB")
    async with _harness(own).run_test() as pilot:
        await pilot.pause()
        label = own.query_one("#mef-token-label", Static)
        assert str(label.render()) == "Токен   Ctrl+U — вернуть наследование"
        own.move(1)
        await pilot.pause()
        assert own.clear_token() is True
        await pilot.pause()
        assert str(label.render()) == "Токен"          # сняли — обещать больше нечего
    # Половина пары — состояние, где клавиша нужнее всего: свой адрес набран, токена
    # нет, и сохранить такое правило не даст. Обещай подпись по СВОЕМУ ТОКЕНУ — выход
    # отсюда остался бы неназванным ровно там, где его ищут.
    half = _form(initial=None, existing_aliases=set(),
                 effective_url="https://openrouter.ai/api/v1/chat/completions",
                 effective_auth="Bearer sk-HUB")
    async with _harness(half).run_test() as pilot:
        await pilot.pause()
        half.query_one("#mef-url", Input).value = "https://my-host/v1/chat/completions"
        await pilot.pause()
        assert str(half.query_one("#mef-token-label", Static).render()) == (
            "Токен   Ctrl+U — вернуть наследование")


async def test_typing_after_ctrl_u_cancels_the_mark(tmp_path):
    """Передумали: набранная после Ctrl+U пара снимает пометку сама — в файл уедет
    НАБРАННОЕ, а не удаление ключей. Иначе submit пошёл бы по ветке «снять», и
    введённое молча пропало бы.

    Набирается именно ПАРА, и порядок не выбирают: клавиша вернула общую целиком, а над
    общим адресом поле токена заморожено — оживает оно только со своим адресом."""
    from textual.widgets import Static
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                          "url": "https://openrouter.ai/api/v1/chat/completions",
                          "token": "sk-OWNSECRET"},
                 existing_aliases={"flash"},
                 effective_url="http://hub/v1/chat/completions",
                 effective_auth="Bearer sk-HUB")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.move(1)
        await pilot.pause()
        assert form.clear_token() is True
        await pilot.pause()
        assert form.query_one("#mef-token", Input).disabled is True
        form.query_one("#mef-url", Input).value = "https://my-host/v1/chat/completions"
        await pilot.pause()
        assert form.query_one("#mef-token", Input).disabled is False
        form.query_one("#mef-token", Input).value = "sk-NEW"
        await pilot.pause()
        assert form.token_cleared is False
        # Клавиша снова работает — и подпись снова о ней говорит.
        assert str(form.query_one("#mef-token-label", Static).render()) == (
            "Токен   Ctrl+U — вернуть наследование")
        ok, _alias, fields = form.submit()
        assert ok is True and fields["token"] == "sk-NEW"
        assert fields["url"] == "https://my-host/v1/chat/completions"


async def test_saving_after_ctrl_u_removes_both_pair_keys_from_the_file(tmp_path):
    """Главное последствие: в ФАЙЛЕ не осталось ни `token`, ни `url` — запись вернулась
    к общей паре целиком (§5). Один снятый токен был бы ровно запрещённой половиной, и
    отказал бы уже валидатор конфига. Соседние ключи, о которых форма не знает
    (`pricing`), это не задело."""
    from gc_core.config_modules.registry_write import upsert_model
    target = tmp_path / "config.yml"
    target.write_text(_REGISTRY_WITH_PRICING, encoding="utf-8")
    template = Path(_default_config_path()).read_text(encoding="utf-8")
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                          "url": "https://openrouter.ai/api/v1/chat/completions",
                          "token": "sk-secret"},
                 existing_aliases={"flash"},
                 effective_url="http://hub/v1/chat/completions",
                 effective_auth="Bearer sk-HUB")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.move(1)
        await pilot.pause()
        assert form.clear_token() is True
        ok, alias, fields = form.submit()
        assert ok is True and fields["token"] is None and fields["url"] is None
    upsert_model(target, alias, fields, template)
    entry = yaml.safe_load(target.read_text(encoding="utf-8"))["models"]["flash"]
    assert "token" not in entry, entry                 # ключи сняты — наследование
    assert "url" not in entry, entry
    assert entry["pricing"] == {"input": 0.3, "output": 1.2}


async def test_the_checkbox_and_ctrl_u_write_different_things(tmp_path):
    """Главный страж различия смыслов: ● «без авторизации» пишет явное `token: ""`
    (осознанно без токена СВОЕГО адреса — пара остаётся своей и целой), Ctrl+U снимает
    пару ЦЕЛИКОМ (наследовать общую). Совпади они — возврат к наследованию снова стал
    бы недостижим с экрана, ради чего клавиша и заводилась."""
    from gc_core.config_modules.registry_write import upsert_model
    template = Path(_default_config_path()).read_text(encoding="utf-8")
    own_url = "https://openrouter.ai/api/v1/chat/completions"
    written = {}
    for case in ("checkbox", "ctrl+u"):
        target = tmp_path / f"config-{case.replace('+', '-')}.yml"
        target.write_text(_REGISTRY_WITH_PRICING, encoding="utf-8")
        form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                              "url": own_url, "token": "sk-secret"},
                     existing_aliases={"flash"},
                     effective_url="http://hub/v1/chat/completions",
                     effective_auth="Bearer sk-HUB")
        async with _harness(form).run_test() as pilot:
            await pilot.pause()
            form.move(1)                               # адрес → токен
            await pilot.pause()
            if case == "checkbox":
                form.move(1)                           # токен → галка
                await pilot.pause()
                assert form.toggle_no_auth() is True
            else:
                assert form.clear_token() is True
            ok, alias, fields = form.submit()
            assert ok is True
        upsert_model(target, alias, fields, template)
        written[case] = yaml.safe_load(target.read_text(encoding="utf-8"))["models"]["flash"]
    assert written["checkbox"]["token"] == ""          # ● — осознанно без токена
    assert written["checkbox"]["url"] == own_url       # свой адрес при этом остался
    assert "token" not in written["ctrl+u"], written["ctrl+u"]   # Ctrl+U — наследование
    assert "url" not in written["ctrl+u"], written["ctrl+u"]     # и адрес вместе с ним


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


@pytest.mark.parametrize("alias", ["[x]", "[/x]"])
async def test_an_alias_with_brackets_stays_readable_in_the_error(alias):
    """Строка ошибки — PLAIN-TEXT: в неё интерполируется то, что набрал человек, и
    квадратные скобки Rich читает разметкой. `[x]` съедало имя («алиас '' уже
    используется» — отказ про пустоту), `[/x]` (закрывающий тег без открытого) ронял
    `MarkupError` наружу через `enter()` в хозяина, и приложение закрывалось.

    Имена такие проходят и валидатор (`validate_model_alias` смотрит только
    зарезервированные слова), и запись в файл (`_yaml_key` их квотирует) — то есть
    повторный ввод СОХРАНЁННОГО имени и ронял TUI. Тот же класс ветка уже чинила
    `markup=False` (`settings._singleton`, `models_section._ask`)."""
    form = _form(initial=None, existing_aliases={alias})
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.query_one("#mef-alias", Input).value = alias
        form.query_one("#mef-model", Input).value = "google/gemini-2.5-flash"
        ok, _alias, _fields = form.submit()            # разметка здесь и роняла форму
        assert ok is False
        error = str(form.query_one("#mef-error", Static).render())
        assert f"'{alias}'" in error, error             # имя названо, а не проглочено


async def test_empty_alias_derives_the_name_from_the_model_id():
    """Алиас не обязателен (ревизия владельца): пустое поле показывает ПРИЗРАКОМ
    конкретное имя, что возьмётся из id (логика мастера: последний path-сегмент,
    занятое имя получает суффикс), placeholder следует за печатью в поле id, а
    Enter принимает призрак без ввода."""
    form = _form(initial=None, existing_aliases={"gemini-2.5-flash"})
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        alias_field = form.query_one("#mef-alias", Input)
        assert alias_field.placeholder == "имя из id модели"     # id пуст — принцип
        form.query_one("#mef-model", Input).value = "google/gemini-2.5-flash"
        await pilot.pause()
        assert alias_field.placeholder == "gemini-2.5-flash-2"   # призрак = что возьмётся
        # Выбор из списка пишется write_field'ом, который глушит Input.Changed, —
        # призрак обязан пересчитаться и на этом пути (живой смок: id из списка,
        # а в поле алиаса остался generic-текст).
        form._apply_model("zhipu/glm-4.6")
        await pilot.pause()
        assert alias_field.placeholder == "glm-4.6"
        form._apply_model("google/gemini-2.5-flash")
        await pilot.pause()
        ok, alias, fields = form.submit()
        assert (ok, alias) == (True, "gemini-2.5-flash-2")
        assert fields["model"] == "google/gemini-2.5-flash"


async def test_empty_alias_and_model_together_are_rejected():
    """Пусто ОБА — имени взять неоткуда: честная ошибка, а не молчаливый отказ и не
    выдумка имени."""
    form = _form(initial=None, existing_aliases=set())
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        ok, _alias, _fields = form.submit()
        assert ok is False
        assert "укажите id модели или алиас" in str(
            form.query_one("#mef-error", Static).render())


async def test_own_alias_is_editable_and_not_its_own_duplicate():
    """Редактирование: алиас — обычное предзаполненное поле (переименование — та же
    форма, транзакцию ведёт секция), запись не считает саму себя дублем, а СТЁРТЫЙ
    алиас — «переименуй в имя по умолчанию»: призрак-placeholder называет имя из
    id, Enter принимает его."""
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash"},
                 existing_aliases={"flash", "glm"})
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        field = form.query_one("#mef-alias", Input)
        assert field.value == "flash"                  # предзаполнен, редактируем
        assert form.original_alias == "flash"          # прежнее имя — секции, для rename
        ok, alias, _fields = form.submit()
        assert (ok, alias) == (True, "flash")          # сам себе не дубль
        field.value = "glm"
        ok, _alias, _fields = form.submit()
        assert ok is False                             # занятое имя — честная ошибка
        assert "уже используется" in str(form.query_one("#mef-error", Static).render())
        field.value = "fresh"
        ok, alias, _fields = form.submit()
        assert (ok, alias) == (True, "fresh")          # переименование уходит секции
        field.value = ""
        await pilot.pause()
        assert field.placeholder == "gemini-2.5-flash"     # призрак имени по умолчанию
        ok, alias, _fields = form.submit()
        assert (ok, alias) == (True, "gemini-2.5-flash")   # Enter принял призрак


async def test_creating_an_entry_named_like_a_base_one_is_refused():
    """§5: имя занято записью основного конфига — своя ошибка, а не общее «уже
    используется»: своей записи с таким именем в профиле нет, и человеку надо
    сказать, ЧЕМ имя занято. Призрак алиаса уникализируется против ЭФФЕКТИВНОГО
    набора — имя, занятое в основном конфиге, он не предложит."""
    from gc_tui.editors import model_entry_form as mef
    form = _form(initial=None, existing_aliases=set(),
                 base_aliases=frozenset({"flash", "gemini-2.5-flash"}))
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.query_one("#mef-alias", Input).value = "flash"
        form.query_one("#mef-model", Input).value = "zhipu/glm-4.6"
        ok, _alias, _fields = form.submit()
        assert ok is False
        assert str(form.query_one("#mef-error", Static).render()) == mef._ALIAS_TAKEN_BY_BASE
        form.query_one("#mef-alias", Input).value = ""
        form.query_one("#mef-model", Input).value = "google/gemini-2.5-flash"
        await pilot.pause()
        assert form.query_one("#mef-alias", Input).placeholder == "gemini-2.5-flash-2"


async def test_editing_an_own_entry_named_like_a_base_one_is_allowed():
    """Тёзка базовой — ЛЕГАЛЬНАЯ раскладка профиля (§4/§5 спеки эффективного вида):
    загрузчик её принимает, витрина считает её своей (`_entry_origin` → own) и обещает
    «Enter — изменить». Решение владельца запрещает СОЗДАНИЕ тёзки через UI, но правку
    существующей отказ убивал вместе с ней: выход был только Del или переименование, а
    оно меняет смысл — запись перестаёт перекрывать базовую.

    То же правило, каким форма не считает запись дублем самой себя (`_other_aliases`):
    из «занятых» исключается СОБСТВЕННОЕ имя записи, и только оно."""
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash"},
                 existing_aliases={"flash"},
                 base_aliases=frozenset({"flash", "opus"}))
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.query_one("#mef-model", Input).value = "zhipu/glm-4.6"
        ok, alias, fields = form.submit()
        assert (ok, alias) == (True, "flash"), str(
            form.query_one("#mef-error", Static).render())
        assert fields["model"] == "zhipu/glm-4.6"
        # Переименование В имя ДРУГОЙ базовой записи — это уже создание тёзки, только
        # из существующей: по-прежнему отказ.
        form.query_one("#mef-alias", Input).value = "opus"
        ok, _alias, _fields = form.submit()
        assert ok is False
        from gc_tui.editors import model_entry_form as mef
        assert str(form.query_one("#mef-error", Static).render()) == mef._ALIAS_TAKEN_BY_BASE


# --- тень: тёзка без своей пары наследует её у заслонённой записи ---------------
#
# Профиль перекрыл запись основного конфига под тем же именем частью ключей; пару,
# адрес и id тёзка берёт у ЗАСЛОНЁННОЙ записи (`base_entry`), а не у «Подключения»
# — так сливает запись загрузчик. Стенд с настоящими файлами и словами формы —
# `test_profile_effective_view`; здесь — то, что видно только с сетевой заглушкой
# и на втором написании тени.

_SHADOWED = {"model": "openai/gpt-5.5", "url": "https://base.example/v1/chat/completions",
             "token": "BASE-TOKEN"}


def _namesake_form(**kwargs):
    """Тёзка стенда smoke-shadow: в профиле у `gpt` один под-ключ `backend`."""
    kwargs.setdefault("base_entry", _SHADOWED)
    return _form(initial={"alias": "gpt", "backend": {"use_ssl": False}},
                 existing_aliases={"gpt"}, base_aliases=frozenset({"gpt"}),
                 effective_url="https://openrouter.ai/api/v1/chat/completions",
                 effective_auth="Bearer SMOKE", **kwargs)


async def test_the_probe_and_the_model_list_of_a_namesake_go_to_the_shadowed_pair():
    """Поле и проба обязаны говорить об одной связи: под адресом тёзки назван адрес
    заслонённой записи — значит и Ctrl+Space, и автоподгрузка списка идут туда и
    её токеном, а не на «Подключение» общим ключом. Иначе «✓ доступен» стоял бы
    под адресом, который никто не проверял."""
    seen, calls = [], []

    def fake_probe(endpoint, auth, verify, model, **kwargs):
        seen.append((endpoint, auth))
        return ProbeResult("rejected", 401, None, 5, "токен отклонён (401)")

    form = _namesake_form(probe=fake_probe,
                          fetch=lambda url, auth, **k: calls.append((url, auth)) or None)
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        assert form.probe_connection() is True
        for _ in range(3):
            await pilot.pause()
        assert seen and all(e == _SHADOWED["url"] for e, _ in seen), seen
        assert [auth for _, auth in seen] == [None, "Bearer BASE-TOKEN"], seen
        form.escape()                                  # свернуть пресеты входа
        form.move(-1); form.move(-1)                   # адрес → алиас → id модели
        for _ in range(3):
            await pilot.pause()
        assert calls == [(_SHADOWED["url"], "Bearer BASE-TOKEN")], calls


async def test_the_shadowed_record_is_read_in_its_second_writing_too():
    """Тень несёт пару вторым написанием (`backend.completions_url` /
    `chat_completion_headers.Authorization`) — форма читает её тем же ядровым
    определением §2, что и своё: иначе тёзка над такой записью звала бы
    «Подключение», показывая при этом её хост."""
    base = {"model": "x", "backend": {
        "completions_url": "https://hdr.example/v1/chat/completions",
        "chat_completion_headers": {"Authorization": "Bearer HDR-SECRET"}}}
    form = _namesake_form(base_entry=base)
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        assert str(form.query_one("#mef-pair", Static).render()) == (
            "Адрес и токен: наследуются из записи основного конфига")
        assert form.query_one("#mef-url", Input).placeholder == (
            "наследуется: https://hdr.example/v1/chat/completions")
        token = form.query_one("#mef-token", Input)
        assert token.placeholder == "наследуется: Bearer HDR***"
        assert token.has_class("has-saved")
        assert form._current_url() == "https://hdr.example/v1/chat/completions"
        assert form._current_auth_header() == "Bearer HDR-SECRET"


async def test_a_shadowed_record_without_a_token_is_explained_not_masked():
    """`token: ""` у заслонённой записи — «сервер не требует авторизации» (§2), и
    тёзка наследует ровно это: маски нет (маскировать нечего, и `***` обещал бы
    секрет), поле говорит, что будет, а проба идёт без токена."""
    seen = []

    def fake_probe(endpoint, auth, verify, model, **kwargs):
        seen.append((endpoint, auth))
        return ProbeResult("ok", 200, [], 5, "доступен")

    form = _namesake_form(base_entry={**_SHADOWED, "token": ""}, probe=fake_probe)
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        token = form.query_one("#mef-token", Input)
        assert token.placeholder == (
            "запись основного конфига без токена — запросы идут без авторизации")
        assert not token.has_class("has-saved")
        assert form.probe_connection() is True
        for _ in range(3):
            await pilot.pause()
        assert seen == [(_SHADOWED["url"], None)], seen


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


async def test_an_env_reference_in_the_address_is_not_a_malformed_url(monkeypatch):
    """Форма получает СЫРОЙ документ (`models_section._read_document` →
    `config_raw.read_document`, «без резолва ${ENV:VAR}»), и `${ENV:VAR}` в адресе —
    штатная документированная форма (`docs/configuration.md`: пример
    `models.flash.token: "${ENV:OPENROUTER_API_KEY}"`, «${ENV:VAR} работают и внутри
    профиля»), к которой ветка сама и толкает (`profiles.find_literal_tokens`).

    Структурная проверка её бракует, и запись с таким адресом нельзя было сохранить даже
    правкой одного id модели: тупик с выходом только через ручную правку YAML. То же
    исключение, что у пробы мастера (`screens/onboarding`: `not _valid_endpoint_url(url)
    and not config_raw.env_refs(url)`)."""
    monkeypatch.setenv("MEF_HUB", "https://hub/v1/chat/completions")
    monkeypatch.setenv("MEF_TOK", "sk-hub")
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                          "url": "${ENV:MEF_HUB}", "token": "${ENV:MEF_TOK}"},
                 existing_aliases={"flash"})
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.query_one("#mef-model", Input).value = "zhipu/glm-4.6"
        ok, alias, fields = form.submit()
        assert (ok, alias) == (True, "flash"), str(
            form.query_one("#mef-error", Static).render())
        assert fields["url"] == "${ENV:MEF_HUB}"       # ссылка уезжает ссылкой
        # Набрать плейсхолдер руками — тот же путь: структурная проверка смотрит на
        # ЗНАЧЕНИЕ, а не на то, трогали ли поле. С ЗАДАННОЙ переменной сохраняется.
        monkeypatch.setenv("MEF_OTHER", "https://other/v1/chat/completions")
        form.query_one("#mef-url", Input).value = "${ENV:MEF_OTHER}"
        await pilot.pause()
        assert form.submit()[0] is True, str(form.query_one("#mef-error", Static).render())
        # А НЕзаданная разрывает пару — и отказ обязан назвать переменную, а не
        # отправить человека искать опечатку в полях (решение владельца, вариант Б).
        monkeypatch.delenv("MEF_OTHER", raising=False)
        await pilot.pause()
        assert form.submit()[0] is False
        assert "MEF_OTHER" in str(form.query_one("#mef-error", Static).render())


# --- Alt+↓ (подбор) и Ctrl+Space (проба) — РАЗНЫЕ клавиши -----------------------

async def test_the_model_list_loads_itself_on_field_entry():
    """Вход в поле id подгружает список САМ (ревизия владельца: к этому полю курсор
    приходит после адреса и токена — идти есть по чему) и раскрывает его; Enter
    подставляет выделенную строку. `Alt+↓` остаётся принудительным перечитыванием."""
    calls = []

    def fetch(url, auth, **kwargs):
        calls.append((url, auth))
        return _models()

    form = _form(initial=None, existing_aliases=set(), fetch=fetch,
                 effective_url="http://hub/v1/chat/completions",
                 effective_auth="Bearer sk-HUB")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.escape()                                  # свернуть пресеты входа
        form.move(-1); form.move(-1)                   # адрес → алиас → id модели
        for _ in range(3):
            await pilot.pause()
        assert calls == [("http://hub/v1/chat/completions", "Bearer sk-HUB")]
        rows = [str(w.render()) for w in _rows(form)]
        assert len(rows) == 2 and "google/gemini-2.5-flash" in rows[0], rows
        assert form.enter() is True                    # подставить выделенную
        await pilot.pause()
        assert form.query_one("#mef-model", Input).value == "google/gemini-2.5-flash"
        assert form.reload_models() is True            # Alt+↓ — перечитать заново
        for _ in range(3):
            await pilot.pause()
        assert len(calls) == 2                         # автоподгрузка была ОДИН раз
        form.escape()                                  # свернуть — иначе ↑/↓ водят ПО списку
        form.move(1); form.move(-1)                    # ушли с поля и вернулись
        for _ in range(3):
            await pilot.pause()
        assert len(calls) == 2, "те же параметры перечитаны без нужды"
        # Канон 2026-08-17: САМ список раскрывается только над ПУСТЫМ полем — поверх
        # подставленного id он забрал бы ↑/↓ себе, и Enter молча затёр бы набранное.
        # Путь назад один, и он назван постоянной строкой под полем.
        assert form.list_open is False
        hint = str(form.query(".mef-model-hint").last().render())
        assert hint == "Alt+↓ — перечитать список", hint


async def test_changed_credentials_retry_the_autoload(tmp_path):
    """Сбой без токена, потом введённый токен — ОДИН сценарий, а не два (ревизия
    владельца по смоку): вход в поле id обязан сходить заново, когда изменились
    адрес или авторизация. Прежний флаг «автоподгрузка один раз на форму» держал
    форму на первой неудаче до ручного Alt+↓."""
    calls = []

    def fetch(url, auth, **kwargs):
        calls.append((url, auth))
        return _models() if auth else None            # без токена сервер молчит

    form = _form(initial=None, existing_aliases=set(), fetch=fetch,
                 effective_url="http://hub/v1/chat/completions")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        # Адрес — свой: над общим полем токена нет вовсе (§4), а сценарий именно про
        # токен, введённый после неудачи.
        form.query_one("#mef-url", Input).value = "http://hub/v1/chat/completions"
        await pilot.pause()
        form.escape()                                  # свернуть пресеты входа
        form.move(-1); form.move(-1)                   # адрес → алиас → id модели
        for _ in range(3):
            await pilot.pause()
        from gc_tui.editors.field_list import LIST_FAILED
        assert LIST_FAILED in str(form.query_one("#mef-model-status", Static).render())
        assert len(calls) == 1 and calls[0][1] is None
        form.move(1); form.move(1)                     # id → алиас → адрес
        await pilot.pause()
        form.escape()                                  # вход на адрес раскрыл пресеты
        form.move(1)                                   # адрес → токен
        form.query_one("#mef-token", Input).value = "sk-NEW"
        await pilot.pause()
        form.move(1); form.move(1)                     # токен → галка → id модели
        for _ in range(3):
            await pilot.pause()
        assert len(calls) == 2, "изменённый токен не перечитал список"
        assert calls[1][1] == "Bearer sk-NEW"
        assert [str(w.render()) for w in _rows(form)], "список не раскрылся"


async def test_the_autoload_answer_respects_can_open_over_a_typed_id():
    """Автоподгрузка подчиняется СОБСТВЕННОМУ правилу формы (`can_open`): её ответ
    над заполненным полем id не раскрывается — форсированное раскрытие поверх
    набранного забирало ↑/↓ списку, а один Enter молча подменял `gpt-4` на
    `gpt-4-turbo` (M6 ревью MR #80). Список даёт о себе знать счётчиком, путь к
    нему — явный Alt+↓."""
    from textual.widgets import Static
    fetch = lambda *a, **k: [ModelInfo(id="gpt-4-turbo", tool_capable=True),
                             ModelInfo(id="gpt-4", tool_capable=True)]
    form = _form(initial={"alias": "g", "model": "gpt-4"}, existing_aliases={"g"},
                 fetch=fetch, effective_url="http://hub/v1/chat/completions")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.escape()                                  # свернуть пресеты входа
        form.move(-1); form.move(-1)                   # адрес → алиас → id модели
        for _ in range(3):
            await pilot.pause()                        # ответ автоподгрузки приехал
        assert form.list_open is False, "ответ раскрылся поверх набранного id"
        assert not _rows(form)
        # Загрузка состоялась и о ней сказано — счётчиком, а не раскрытием.
        assert "2" in str(form.query_one("#mef-model-status", Static).render())
        # Enter — хозяину (сохранить), а не подстановка чужой строки.
        assert form.enter() is False
        assert form.query_one("#mef-model", Input).value == "gpt-4"


async def test_the_forced_list_highlights_the_row_equal_to_the_field():
    """`Alt+↓` поверх набранного id: подсвечена строка, РАВНАЯ значению поля
    (эталоны `preset_list._initial_index` / `voice_endpoint._model_initial_index`),
    а не первая уцелевшая под фильтром — там стоял сосед `gpt-4-turbo`, и Enter
    вписывал его вместо своего (второй дефект M6)."""
    fetch = lambda *a, **k: [ModelInfo(id="gpt-4-turbo", tool_capable=True),
                             ModelInfo(id="gpt-4", tool_capable=True)]
    form = _form(initial={"alias": "g", "model": "gpt-4"}, existing_aliases={"g"},
                 fetch=fetch, effective_url="http://hub/v1/chat/completions")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.escape()                                  # свернуть пресеты входа
        form.move(-1); form.move(-1)                   # адрес → алиас → id модели
        for _ in range(3):
            await pilot.pause()                        # автоподгрузка (список свёрнут)
        assert form.reload_models() is True            # явная просьба показать
        for _ in range(3):
            await pilot.pause()
        assert form.list_open is True
        assert form.list_has_selection is True
        selected = [r for r in (str(w.render()) for w in _rows(form)) if "▸" in r]
        assert selected and "gpt-4-turbo" not in selected[0], selected
        assert "gpt-4" in selected[0]
        assert form.enter() is True                    # подставить подсвеченную
        await pilot.pause()
        assert form.query_one("#mef-model", Input).value == "gpt-4"


async def test_the_forced_list_highlights_nothing_when_the_typed_id_has_no_exact_match():
    """`Alt+↓` поверх набранного id, которого на сервере нет ВООБЩЕ (только соседи
    по префиксу, `gpt-4-turbo`/`gpt-4o` — точного `gpt-4` нет): `_begin_open`
    ставит `_idx = None` верно (`_model_initial_index` не нашёл строку, равную
    полю), но тут же `apply_filter` тем же значением сужает список, и
    `_model_refilter_index` откатывал это решение фолбэком на первую уцелевшую
    строку — подсвечивала соседа, которого поле не называет. Один Enter тогда
    молча подставлял его вместо набранного (M6, находка #13, ревью MR #80)."""
    fetch = lambda *a, **k: [ModelInfo(id="gpt-4-turbo", tool_capable=True),
                             ModelInfo(id="gpt-4o", tool_capable=True)]
    form = _form(initial={"alias": "g", "model": "gpt-4"}, existing_aliases={"g"},
                 fetch=fetch, effective_url="http://hub/v1/chat/completions")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.escape()                                  # свернуть пресеты входа
        form.move(-1); form.move(-1)                   # адрес → алиас → id модели
        for _ in range(3):
            await pilot.pause()                        # автоподгрузка (список свёрнут)
        assert form.reload_models() is True            # явная просьба показать
        for _ in range(3):
            await pilot.pause()
        assert form.list_open is True
        assert form.list_has_selection is False, "точного совпадения нет — подсвечивать нечего"
        assert not [r for r in (str(w.render()) for w in _rows(form)) if "▸" in r]
        assert form.enter() is True                    # список раскрыт, клавиша его
        await pilot.pause()
        assert form.query_one("#mef-model", Input).value == "gpt-4", \
            "Enter подставил соседа вместо набранного id"


async def test_a_changed_address_invalidates_the_fetched_model_list():
    """Успешная загрузка с host-A → смена адреса на host-B → возврат на поле id:
    ОБЯЗАН пойти второй fetch с B (M7 ревью MR #80). Список принадлежит связи
    (адрес, авторизация), и показанный без сверки ключа список host-A подставлял
    Enter'ом id, которого на B нет; второго похода не было вовсе."""
    calls = []

    def fetch(url, auth, **kwargs):
        calls.append((url, auth))
        return _models()

    form = _form(initial=None, existing_aliases=set(), fetch=fetch,
                 effective_url="http://host-a/v1/chat/completions")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.escape()                                  # свернуть пресеты входа
        form.move(-1); form.move(-1)                   # адрес → алиас → id модели
        for _ in range(3):
            await pilot.pause()
        assert calls == [("http://host-a/v1/chat/completions", None)]
        form.escape()                                  # свернуть раскрытый список
        form.move(1); form.move(1)                     # id → алиас → адрес
        await pilot.pause()
        form.query_one("#mef-url", Input).value = "http://host-b/v1/chat/completions"
        await pilot.pause()                            # пресеты закрылись сами (нет совпадений)
        form.move(-1); form.move(-1)                   # адрес → алиас → id модели
        for _ in range(3):
            await pilot.pause()
        assert len(calls) == 2, "смена адреса не перечитала список"
        assert calls[1] == ("http://host-b/v1/chat/completions", None)


async def test_alt_down_reports_a_dead_endpoint_instead_of_staying_silent():
    form = _form(initial=None, existing_aliases=set(), fetch=lambda *a, **k: None,
                 effective_url="http://hub/v1/chat/completions")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.escape()                                  # свернуть пресеты входа
        form.move(-1); form.move(-1)                   # адрес → алиас → id модели
        for _ in range(3):
            await pilot.pause()                        # автоподгрузка при входе
        from gc_tui.editors.field_list import LIST_FAILED
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
        form.escape()                                  # свернуть пресеты входа
        form.move(-1); form.move(-1)                   # адрес → алиас → id модели
        for _ in range(3):
            await pilot.pause()                        # автоподгрузка при входе
        assert probes == [], "подбор списка постучался пробой"
        assert fetches, "подбор списка не сходил на эндпоинт"


async def test_probe_without_any_address_is_honest():
    form = _form(initial=None, existing_aliases=set())
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        assert form.probe_connection() is True
        await pilot.pause()
        assert "адрес" in str(form.query_one("#mef-probe-line", Static).render())


async def test_the_probe_resolves_env_references_before_the_request(monkeypatch):
    """`${ENV:VAR}` в полях — СЫРОЕ значение файла, а не адрес и не токен: в запрос
    обязано уйти раскрытое. Буквальный `Bearer ${ENV:MEF_TOK}` сервер отклоняет, и проба
    отвечала «✗ токен отклонён» на исправном конфиге.

    Раскрытие — тем же резолвером, что у `config.load_config`, и ВНУТРИ воркера: у
    `${FILE:...}` это чтение с диска, в UI-потоке оно подвесило бы интерфейс (эталон —
    `screens/onboarding._auto_probe`). Наружу (поле, submit) раскрытое не возвращается."""
    monkeypatch.setenv("MEF_HUB", "https://hub/v1/chat/completions")
    monkeypatch.setenv("MEF_TOK", "sk-REAL")
    seen = []

    def fake_probe(endpoint, auth, verify, model, **kwargs):
        seen.append((endpoint, auth))
        return ProbeResult("rejected", 401, None, 5, "токен отклонён (401)")

    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                          "url": "${ENV:MEF_HUB}", "token": "${ENV:MEF_TOK}"},
                 existing_aliases={"flash"}, probe=fake_probe)
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        assert form.probe_connection() is True
        for _ in range(3):
            await pilot.pause()
        assert seen, "проба не пошла"
        assert all(e == "https://hub/v1/chat/completions" for e, _ in seen), seen
        assert "Bearer sk-REAL" in [auth for _, auth in seen], seen
        # Поле остаётся сырым: раскрытое значение — только для запроса.
        assert form.query_one("#mef-url", Input).value == "${ENV:MEF_HUB}"


async def test_typing_in_the_form_does_not_read_a_file_backed_token(monkeypatch, tmp_path):
    """`_as_validator_sees` (правило пары, `_pair_parts`) резолвит поля СИНХРОННО на
    каждое нажатие клавиши в mef-url/mef-token (`on_input_changed` → `_sync_pair_state`
    → `own_connection` → `_pair_parts`) — и до фикса разворачивал вместе с `${ENV:…}`
    ещё и `${FILE:...}`, то есть читал файл-кред с диска на UI-потоке на каждый
    набранный символ, вопреки правилу соседней `_resolved_for_request` («Зовётся
    ТОЛЬКО из воркеров… в UI-потоке оно подвесило бы интерфейс»)."""
    from gc_core.config_modules import env_interpolation as env_interp

    reads: list = []
    real_read = env_interp._read_secret_file

    def counted(path_raw, cache):
        reads.append(path_raw)
        return real_read(path_raw, cache)

    monkeypatch.setattr(env_interp, "_read_secret_file", counted)

    cred = tmp_path / "cred.txt"
    cred.write_text("sk-FROM-FILE")
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                          "token": f"${{FILE:{cred}}}"},
                 existing_aliases={"flash"})
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        url_input = form.query_one("#mef-url", Input)
        url_input.value = "http://own/v1/chat/completions"     # печать одного символа
        await pilot.pause()
        assert reads == [], f"печать в #mef-url прочитала файл-кред с диска: {reads}"


async def test_the_model_list_resolves_env_references_before_the_request(monkeypatch):
    """Тот же страж для подбора списка: он уходит на эндпоинт САМ, при простом переходе
    курсора на поле id, — с буквальной ссылкой в адресе список молча не грузился бы
    никогда («✗ не удалось получить список»)."""
    monkeypatch.setenv("MEF_HUB", "https://hub/v1/chat/completions")
    monkeypatch.setenv("MEF_TOK", "sk-REAL")
    calls = []

    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                          "url": "${ENV:MEF_HUB}", "token": "${ENV:MEF_TOK}"},
                 existing_aliases={"flash"},
                 fetch=lambda url, auth, **k: calls.append((url, auth)) or _models())
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.escape()                                  # свернуть пресеты входа
        form.move(-1); form.move(-1)                   # адрес → алиас → id модели
        for _ in range(3):
            await pilot.pause()
        assert calls == [("https://hub/v1/chat/completions", "Bearer sk-REAL")], calls


async def test_a_missing_env_variable_is_named_instead_of_a_blind_request(monkeypatch):
    """Переменной нет — резолв даст "", и стучаться в пустой адрес нечестно: под
    визуально заполненным полем и проба, и список соврали бы «нет соединения», как будто
    сломан сам сервер. Называем переменную (эталон — `screens/onboarding._auto_probe`,
    `env_ref_status`/`ENV_REF_MISSING`)."""
    monkeypatch.delenv("MEF_ABSENT", raising=False)
    calls = []

    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                          "url": "${ENV:MEF_ABSENT}", "token": "sk-own"},
                 existing_aliases={"flash"},
                 fetch=lambda url, auth, **k: calls.append((url, auth)) or _models(),
                 probe=lambda *a, **k: calls.append(a) or ProbeResult(
                     "ok", 200, None, 1, "доступен"))
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        assert form.probe_connection() is True
        for _ in range(3):
            await pilot.pause()
        line = str(form.query_one("#mef-probe-line", Static).render())
        assert "MEF_ABSENT" in line and "переменной нет" in line, line
        form.escape()                                  # свернуть пресеты входа
        form.move(-1); form.move(-1)                   # адрес → алиас → id модели
        for _ in range(3):
            await pilot.pause()
        status = str(form.query_one("#mef-model-status", Static).render())
        assert "MEF_ABSENT" in status and "переменной нет" in status, status
        assert calls == [], calls                      # в сеть не ходили


async def test_a_missing_env_variable_in_the_token_stops_the_probe(monkeypatch):
    """Тот же гейт на ВТОРОМ конце связи — и он важнее адресного: `docs/configuration.md`
    сам толкает держать в `${ENV:…}` именно ТОКЕН, а `profiles.find_literal_tokens` в
    рантайме ругает литеральный.

    Незаданная переменная схлопывала ключ в пустой `Bearer `, differential-проба делала
    ДВА сетевых вызова и выносила вердикт «✓ сервер доступен · ✗ токен отклонён (401)»:
    форма винила токен там, где виновата ненайденная переменная, а пустой ключ при этом
    улетал на живой сервер.

    В строке — сама ССЫЛКА, а не собранный `_current_auth_header()`: «Bearer ${ENV:X} ·
    переменной нет» называет переменную вперемешку со схемой."""
    monkeypatch.delenv("MEF_NO_TOKEN", raising=False)
    calls = []

    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                          "url": "https://hub/v1/chat/completions",
                          "token": "${ENV:MEF_NO_TOKEN}"},
                 existing_aliases={"flash"},
                 probe=lambda *a, **k: calls.append(a) or ProbeResult(
                     "ok", 200, None, 1, "доступен"))
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        assert form.probe_connection() is True
        for _ in range(3):
            await pilot.pause()
        line = str(form.query_one("#mef-probe-line", Static).render())
        assert "MEF_NO_TOKEN" in line and "переменной нет" in line, line
        assert "Bearer" not in line, line              # ссылка, а не заголовок
        assert calls == [], calls                      # в сеть не ходили


async def test_a_missing_env_variable_in_the_token_stops_the_model_list(monkeypatch):
    """То же для подбора списка: он уходит на эндпоинт САМ, простым переходом курсора
    на поле id, — и уносил бы пустой ключ, отвечая «✗ не удалось получить список», как
    будто дело в сервере."""
    monkeypatch.delenv("MEF_NO_TOKEN", raising=False)
    calls = []

    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                          "url": "https://hub/v1/chat/completions",
                          "token": "${ENV:MEF_NO_TOKEN}"},
                 existing_aliases={"flash"},
                 fetch=lambda url, auth, **k: calls.append((url, auth)) or _models())
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.escape()                                  # свернуть пресеты входа
        form.move(-1); form.move(-1)                   # адрес → алиас → id модели
        for _ in range(3):
            await pilot.pause()
        status = str(form.query_one("#mef-model-status", Static).render())
        assert "MEF_NO_TOKEN" in status and "переменной нет" in status, status
        assert calls == [], calls                      # в сеть не ходили


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
        # Старт — поле адреса (порядок «куда → чем → что → как назвать»): вход
        # раскрыл пресет-лист сам, первая строка подсвечена.
        assert (form.on_url_field, form.on_model_field) == (True, False)
        assert (form.presets_open, form.presets_have_selection) == (True, True)
        form.query_one("#mef-url", Input).value = "https://my-own.internal/v1"
        await pilot.pause()
        # Канон 2026-08-18: набранного в каталоге нет — список СВОРАЧИВАЕТСЯ (обещать
        # ему нечего), а постоянная строка под полем говорит об этом и называет
        # клавишу возврата.
        assert form.presets_open is False
        hint = str(form.query(".mef-url-preset-hint").last().render())
        assert hint == "в списке такого нет · Alt+↓ — показать список", hint
        # `Alt+↓` поверх СВОЕГО адреса — единственное живое состояние «список раскрыт,
        # но ничего не подсвечено»: строки видны все, Enter оставит набранное. Футер
        # обязан читать именно этот признак, а не «список не пуст».
        assert form.reload_presets() is True
        await pilot.pause()
        assert (form.presets_open, form.presets_have_selection) == (True, False)
        assert form.escape() is True                   # свернуть — двигаться дальше
        await pilot.pause()
        form.move(-1); form.move(-1)                   # адрес → алиас → id модели
        for _ in range(3):
            await pilot.pause()
        # Автоподгрузка на входе (fetch-стаб): список раскрылся сам, строка выделена.
        assert (form.on_model_field, form.on_url_field) == (True, False)
        assert (form.list_open, form.list_has_selection) == (True, True)
        assert form.escape() is True
        await pilot.pause()
        assert (form.list_open, form.on_model_field) == (False, True)


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
        form.escape()                                  # свернуть пресеты входа
        form.move(-1); form.move(-1)                   # адрес → алиас → id модели
        for _ in range(3):
            await pilot.pause()                        # автоподгрузка раскрыла список
        model_group = form.query_one("#mef-model-group")
        assert form.query_one("#mef-model", Input).parent is model_group
        assert list(model_group.query(VerticalScroll)), "строки моделей — внутрь группы"
        assert not list(model_group.query(".mef-model-hint")), "подсказка — снаружи рамки"
        assert list(form.query(".mef-model-hint")), "подсказка списка обязана быть"
        # Раскрытый список модальный: пока в нём есть строки, ↑/↓ водят ПО НЕМУ —
        # уйти с поля можно только свернув его (канон §4.2.2).
        assert form.escape() is True
        await pilot.pause()
        form.move(1); form.move(1)                     # id → алиас → адрес: пресеты раскрылись
        await pilot.pause()
        url_group = form.query_one("#mef-url-group")
        assert form.query_one("#mef-url", Input).parent is url_group
        assert list(url_group.query(VerticalScroll)), "строки пресетов — внутрь группы"
        assert not list(url_group.query(".mef-url-preset-hint")), "подсказка — снаружи рамки"


# --- сквозной с Task 1: merge писателя ------------------------------------------

async def test_submit_after_url_edit_keeps_pricing(tmp_path):
    """Форма отдаёт только явно заданные поля, merge делает writer: правка адреса
    не теряет `pricing`, о котором форма не знает вовсе."""
    from gc_core.config_modules.registry_write import upsert_model
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


async def test_the_cursor_does_not_call_a_typed_address_inherited():
    """Пункт наследования и обычный пресет часто несут ОДИН url, а пункт стоит первым:
    поиск «первого совпадения» подсвечивал его и над своим, набранным адресом — курсор
    говорил «наследуете», когда запись задала адрес сама (находка владельца по смоку).

    Пункт наследования означает ровно одно состояние — поле ПУСТОЕ. При заполненном
    поле текущим может быть только обычный пресет."""
    url = "https://openrouter.ai/api/v1/chat/completions"
    presets = [EndpointPreset(name="OpenRouter", url=url)]
    form = _form(initial={"alias": "flash", "model": "google/gemini-2.5-flash",
                          "url": url, "token": "sk-own"},
                 existing_aliases={"flash"}, presets=presets,
                 effective_url=url)                    # общий адрес — тот же самый
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        assert form.reload_presets() is True           # Alt+↓ поверх своего адреса
        await pilot.pause()
        rows = [str(w.render()) for w in form.query(".mef-preset-row") if w.display]
        selected = [r for r in rows if r.startswith("▸")]
        assert selected and "OpenRouter" in selected[0], rows
        assert "наследовать" not in selected[0], rows


async def test_a_failure_while_the_cursor_was_away_is_reported_on_return():
    """Загрузка провалилась, пока курсор ушёл с поля id: показать ошибку было
    негде, а дребезг автоподгрузки не повторит запрос теми же параметрами — без
    памяти о провале вернувшийся курсор встречало бы молчание, неотличимое от
    «список ещё летит»."""
    calls: list = []
    form = _form(initial=None, existing_aliases=set(),
                 fetch=lambda url, auth, **k: calls.append((url, auth)) or None,
                 effective_url="http://hub/v1/chat/completions")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.escape()                                  # свернуть пресеты входа
        form.move(-1); form.move(-1)                   # адрес → алиас → id модели
        form.move(1)                                   # уйти с поля, не дождавшись
        for _ in range(3):
            await pilot.pause()                        # ответ приходит под чужим полем
        assert calls, "автоподгрузка не стартовала"
        assert str(form.query_one("#mef-model-status", Static).render()) == ""

        form.move(-1)                                  # вернуться на поле id
        for _ in range(3):
            await pilot.pause()
        from gc_tui.editors.field_list import LIST_FAILED
        assert len(calls) == 1, "дребезг сломан: повторный запрос теми же параметрами"
        assert LIST_FAILED in str(form.query_one("#mef-model-status", Static).render())


async def test_a_dangling_env_reference_is_judged_as_the_validator_judges_it(monkeypatch):
    """Правило пары судит РАСКРЫТОЕ значение (решение владельца 2026-08-26, вариант Б),
    и форма обязана судить так же — её докстринг это и обещает.

    Пока переменной нет, `${ENV:…}` в адресе для валидатора «своего адреса нет».
    Форма же считала его своим и требовала токен, а писатель, увидев пустую строку,
    требовал токен УБРАТЬ: запись не собиралась ни одним набором полей."""
    monkeypatch.delenv("MEF_PAIR_URL", raising=False)
    form = _form(initial={"alias": "corp", "model": "corp/model-x",
                          "url": "${ENV:MEF_PAIR_URL}"},
                 existing_aliases={"corp"})
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.query_one("#mef-model", Input).value = "corp/model-y"
        ok, alias, fields = form.submit()
        assert (ok, alias) == (True, "corp"), str(
            form.query_one("#mef-error", Static).render())
        assert fields["url"] == "${ENV:MEF_PAIR_URL}", "в файл обязана уехать ССЫЛКА"


async def test_a_resolved_env_reference_still_needs_its_half_of_the_pair(monkeypatch):
    """Контроль той же монеты: переменная задана — адрес свой, и правило пары снова
    требует токен. Иначе запись ушла бы на свой хост с общим ключом."""
    monkeypatch.setenv("MEF_PAIR_URL", "https://corp/v1/chat/completions")
    form = _form(initial={"alias": "corp", "model": "corp/model-x",
                          "url": "${ENV:MEF_PAIR_URL}"},
                 existing_aliases={"corp"})
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.query_one("#mef-model", Input).value = "corp/model-y"
        ok, _alias, _fields = form.submit()
        assert ok is False
        assert "не задан токен" in str(form.query_one("#mef-error", Static).render())


async def test_empty_server_answer_is_not_reported_as_a_failure():
    """Сервер ОТВЕТИЛ пустым списком — это не сбой запроса.

    Голосовая панель в той же ситуации говорит «сервер не вернул моделей»
    (`field_list.LIST_EMPTY`), и состояние это заведено ровно затем, чтобы
    отличать «с адресом и сетью всё в порядке, моделей там нет» от «не
    достучались». Форма записи реестра валила оба случая в LIST_FAILED и
    отправляла человека чинить связь, которая работает."""
    from gc_tui.editors.field_list import LIST_EMPTY
    form = _form(initial=None, existing_aliases=set(), fetch=lambda *a, **k: [],
                 effective_url="http://hub/v1/chat/completions",
                 effective_auth="Bearer sk-HUB")
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.escape()
        form.move(-1); form.move(-1)                   # адрес → алиас → id модели
        for _ in range(4):
            await pilot.pause()
        assert str(form.query_one("#mef-model-status", Static).render()) == LIST_EMPTY

@pytest.mark.asyncio
async def test_a_mouse_click_lands_the_cursor_like_the_arrows_do():
    """Клик мышью по полю — тоже перемещение курсора (спека §4.3, канон
    voice-редакторов): без приземления клик в поле id не запускал автоподгрузку
    списка, статуса не было, а Alt+↓ молчал — его гейт смотрит на позицию
    курсора, которую двигали только стрелки (живой смок 2026-09-04)."""
    calls = []

    def fetch(url, auth, **kw):
        calls.append(url)
        return _models()

    form = _form(initial={"alias": "op", "model": "",
                          "url": "https://openrouter.ai/api/v1/chat/completions",
                          "token": "sk-x"},
                 existing_aliases=set(), fetch=fetch)
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        assert form._positions[form._idx] == "mef-url"    # предусловие: курсор на адресе
        await pilot.click("#mef-model")
        await pilot.pause()
        assert form._positions[form._idx] == "mef-model", "клик не приземлил курсор"
        assert calls, "автоподгрузка не стартовала по клику"
        assert form.reload_models() is True               # Alt+↓ ожил

@pytest.mark.asyncio
async def test_the_id_field_promises_alt_down_before_the_first_load():
    """«Alt+↓ — перечитать список» обязана стоять под полем id с ПЕРВОГО кадра:
    список СЕТЕВОЙ — клавиша идёт на сервер, и обещание осмысленно даже без
    единой добытой строки (канон _can_promise, воплощённый у voice). До фикса
    строка появлялась только после первой загрузки — способ позвать список не
    был назван нигде (живой смок 2026-09-04)."""
    from gc_tui.editors.field_list import MODEL_RELOAD_HINT
    form = _form(initial={"alias": "op", "model": "",
                          "url": "https://openrouter.ai/api/v1/chat/completions",
                          "token": "sk-x"},
                 existing_aliases=set(), fetch=lambda *a, **k: None)
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        hints = [str(w.render()) for w in form.query(".mef-model-hint")]
        assert any(MODEL_RELOAD_HINT in h for h in hints), hints

@pytest.mark.asyncio
async def test_a_click_on_the_noauth_row_lands_the_cursor_too():
    """[ревью №7, находка 3] Галка «без авторизации» — единственная позиция, на
    которую мышь не приземлялась (её позиция в _positions — None, а id виджета в
    кортеже нет): после клика Space печатал пробел в прежнее поле вместо
    переключения."""
    form = _form(initial={"alias": "op", "model": "m",
                          "url": "https://openrouter.ai/api/v1/chat/completions",
                          "token": "sk-x"},
                 existing_aliases=set())
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        await pilot.click("#mef-noauth")
        await pilot.pause()
        assert form._positions[form._idx] is None, "клик по галке не приземлил курсор"
        assert form.toggle_no_auth() is True


@pytest.mark.asyncio
async def test_a_mouse_landing_refreshes_the_screen_footer():
    """[ревью №7, находка 2] Приземление мышью обязано обновить футер экрана —
    как канон voice (settings.on_descendant_focus): иначе до следующей клавиши
    футер обещает клавиши прошлой позиции."""
    calls = []
    form = _form(initial={"alias": "op", "model": "m",
                          "url": "https://openrouter.ai/api/v1/chat/completions",
                          "token": "sk-x"},
                 existing_aliases=set())
    app = _harness(form)
    async with app.run_test() as pilot:
        await pilot.pause()
        pilot.app.screen._refresh_footer = lambda: calls.append(1)
        await pilot.click("#mef-token")
        await pilot.pause()
        assert form._positions[form._idx] == "mef-token"
        assert calls, "футер не обновлён после мышиного приземления"

@pytest.mark.asyncio
async def test_typing_in_the_id_field_reopens_the_fetched_list_with_a_filter():
    """Печать в поле id при свёрнутом списке возвращает его с фильтром по
    набранному — как у адреса и голосового endpoint'а (reopen_for_typing,
    канон 2026-08-18). Поле id было единственным соседом без живого фильтра:
    человек стирал хвост id и ждал варианты, а список молчал до Alt+вниз
    (живой смок 2026-09-04). Без добытых моделей раскрывать нечего —
    поведение прежнее."""
    form = _form(initial={"alias": "op", "model": "",
                          "url": "https://openrouter.ai/api/v1/chat/completions",
                          "token": "sk-x"},
                 existing_aliases=set(), fetch=lambda *a, **k: _models())
    async with _harness(form).run_test() as pilot:
        await pilot.pause()
        form.move(1); form.move(1); form.move(1)      # адрес -> токен -> галка -> id
        await pilot.pause()
        assert form._positions[form._idx] == "mef-model"
        assert form._list.is_open                      # автоподгрузка раскрыла
        form.escape()                                  # свернуть, курсор в поле
        await pilot.pause()
        assert not form._list.is_open
        await pilot.press("z", "h")                    # печать возвращает список
        await pilot.pause()
        assert form._list.is_open, "печать не вернула свёрнутый список"
        shown = " ".join(str(w.render()) for w in _rows(form))
        assert "glm" in shown and "gemini" not in shown   # фильтр по набранному

