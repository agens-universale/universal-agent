"""Открытый профиль: ЭФФЕКТИВНЫЙ вид верхней половины (Task A плана 2026-08-13).

Профиль — это его файл, а экран показывает файл ПЛЮС тень основного конфига
(§2 спеки): записи основного конфига стоят строками реестра — приглушённые и
ПОДПИСАННЫЕ СЛОВОМ («основной конфиг») — рядом с записями самого профиля.
До этого профиль без своего реестра показывал пустое состояние и ноль строк,
хотя «те же модели, другая раскладка уровней» — главный сценарий профиля.

Тень читается из ДОКУМЕНТА базового config.yml (§3), а не из merged runtime
экрана: тот собран с уже активным профилем, и тень стала бы гибридной.
"""
from contextlib import asynccontextmanager

import pytest

pytest.importorskip("textual")

from textual.widgets import Static

from config_modules.model_registry import LEVELS
from tests.tui.conftest import _rgb, _row_style_set, _value_color, wait_until
from tests.tui.test_profile_editor import _fill_entry
from tests.tui.test_profiles_crud import (_flash, _notice, _profiles_screen,
                                          _put_cursor, _write)
from tui.editors import models_section as m
from tui.editors.models_section import value_style
from tui.palette import DEEP_DIM, DIM, INK

_BASE_URL = "http://base-host/v1/chat/completions"
_MERGED_URL = "http://merged-host/v1/chat/completions"


@pytest.fixture(autouse=True)
def _no_managed_overlay(tmp_path, monkeypatch):
    """Без этого происхождение полей читает НАСТОЯЩИЙ ~/.gc/config.local.yml
    разработчика (тот же приём, что в tests/tui/test_profiles_crud.py)."""
    monkeypatch.setattr(
        "mcp_runtime.paths.managed_config_path",
        lambda: tmp_path / "no-managed-overlay-here.yml",
    )


def _base_config_text(*, registry: bool = True, assign: bool = False) -> str:
    """Базовый config.yml — цель ОСНОВНОЙ секции и источник тени.

    `flash` со своим адресом, `opus` — без него: вторая запись и делает
    проверяемым то, ОТКУДА берётся адрес унаследованной строки (§3: `base_url`
    из файла, а не действующий адрес экрана) и что хвост «· из основного
    конфига» у такой строки подавлен (§4). Реестр из двух записей с адресами
    (буква брифа) этих двух фактов не выражает вовсе.

    `assign=True` дописывает привязку основного конфига: без неё третье
    состояние строки привязок («основной → light», §8) не выражается вовсе —
    у потребителя без своего ключа и без ключа тени значение одно, «активная».
    """
    text = ('profile: ""\n'
            "level: default\n"
            "backend:\n"
            f'  completions_url: "{_BASE_URL}"\n'
            "agent:\n"
            '  model: "test-model"\n')
    if registry:
        text += ("models:\n"
                 "  flash:\n"
                 '    model: "google/gemini-2.5-flash"\n'
                 '    url: "http://flash-host/v1/chat/completions"\n'
                 "  opus:\n"
                 '    model: "anthropic/claude-opus"\n'
                 "slots:\n"
                 '  default: "flash"\n'
                 '  light: "opus"\n')
    if assign:
        text += ("assign:\n"
                 '  summary: "light"\n')
    return text


_BASE_ONE_MODEL = ('profile: ""\n'
                   "level: default\n"
                   "backend:\n"
                   f'  completions_url: "{_BASE_URL}"\n'
                   "agent:\n"
                   '  model: "test-model"\n'
                   "models:\n"
                   "  flash:\n"
                   '    model: "google/gemini-2.5-flash"\n')

# Длинная унаследованная ПРИВЯЗКА при коротких слотах — единственная конфигурация,
# в которой колонку значения задаёт именно половина привязок: подписи слотов здесь
# короче («основной → flash», «не задан → default»), а сам длинный алиас в колонку
# и так входит списком реестра — длиннее его делает ровно приставка «основной → ».
_LONG_ALIAS = "very-long-alias-name"
_BASE_LONG_ASSIGN = ('profile: ""\n'
                     "level: default\n"
                     "backend:\n"
                     f'  completions_url: "{_BASE_URL}"\n'
                     "agent:\n"
                     '  model: "test-model"\n'
                     "models:\n"
                     "  flash:\n"
                     '    model: "google/gemini-2.5-flash"\n'
                     f"  {_LONG_ALIAS}:\n"
                     '    model: "zhipu/glm-4"\n'
                     "slots:\n"
                     '  default: "flash"\n'
                     "assign:\n"
                     f'  summary: "{_LONG_ALIAS}"\n')


def _screen_config(*, file_view: bool = True) -> dict:
    """Merged runtime экрана — с ДРУГИМ адресом, чем базовый файл: ровно так
    выглядит запуск с активным профилем, перенацелившим `backend`.

    `file_view=False` — выключенный просмотрщик (`tui.file_view.enabled: false`,
    ключ конфига `tui_file_view`): под-вид профиля обязан замолчать про `v`
    (§11a). Ключа нет вовсе — просмотрщик включён (дефолт `FileViewSettings`)."""
    config = {"completions_url": _MERGED_URL, "model": "test-model"}
    if not file_view:
        config["tui_file_view"] = {"enabled": False}
    return config


def _editor(scr):
    from tui.editors.models_section import ModelsSectionEditor
    return scr.query(ModelsSectionEditor).last()


def _texts(scr) -> str:
    return " ".join(str(w.render()) for w in scr.query(Static))


def _entry_row(ed, alias: str):
    return ed._row_text({"kind": "entry", "alias": alias}, False)


def _entry_aliases(ed) -> list:
    return [row["alias"] for row in ed._rows() if row["kind"] == "entry"]


def _profile_path(tmp_path):
    return tmp_path / "base" / "profiles" / "work.yml"


def _cursor_to(ed, kind: str, **match) -> None:
    """Курсор редактора на строку нужного рода: индексы строк знает он сам."""
    for i, row in enumerate(ed._rows()):
        if row["kind"] == kind and all(row.get(k) == v for k, v in match.items()):
            ed._idx = i
            ed._refresh_rows()
            return
    raise AssertionError(f"строки {kind}/{match} нет: {ed._rows()}")


@asynccontextmanager
async def _profile_view(tmp_path, monkeypatch, *, profile: str | None = None,
                        registry: bool = True, assign: bool = False,
                        base: str | None = None, size=None,
                        file_view: bool = True):
    """Открытый профиль «work» над базовым конфигом. Отдаёт `(pilot, scr, ed)`.

    `profile=None` — бланк создания (`PROFILE_SKELETON`, один комментарий-шапка).
    `base` — базовый файл целиком, когда его форма и есть предмет стража
    (`_BASE_ONE_MODEL`); иначе собирается `_base_config_text`.

    Работа из временного проектного каталога: дискавери профилей смотрит и в
    `<cwd>/.gc/profiles`, и каталог репозитория сдвинул бы состав строк.
    `exist_ok` — страж §11 открывает ДВЕ конфигурации подряд в одном tmp_path:
    их пара и есть его предмет, и разводить их по тестам значило бы разорвать
    сравнение."""
    import tui.app as app_mod
    from config_modules.profiles import PROFILE_SKELETON
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    monkeypatch.chdir(project)
    base_cfg = _write(tmp_path / "base" / "config.yml",
                      base if base is not None
                      else _base_config_text(registry=registry, assign=assign))
    _write(tmp_path / "base" / "profiles" / "work.yml",
           PROFILE_SKELETON if profile is None else profile)

    config = _screen_config(file_view=file_view)
    app = app_mod.TuiApp(config=config)
    async with app.run_test(**({"size": size} if size else {})) as pilot:
        scr = await _profiles_screen(pilot, app, base_cfg, config=config)
        await _put_cursor(pilot, scr, "profile", "work")
        await pilot.press("enter")
        await pilot.pause()
        yield pilot, scr, _editor(scr)


# --- строки: реестр основного конфига виден из профиля ------------------------


async def test_fresh_profile_shows_the_full_effective_view(tmp_path, monkeypatch):
    """Свежесозданный профиль (бланк — один комментарий) над базой с реестром:
    записи ОСНОВНОГО конфига стоят строками (§2), приглушённые и подписанные
    словом «основной конфиг», а указатель уровня у живой записи остаётся
    ЦВЕТНЫМ — уровень работает, происхождение записи этого не отменяет (§4).

    Пустого состояния здесь нет, и «также переопределяет» — тоже: секции
    моделей показаны формами, а больше в бланке ничего и нет."""
    async with _profile_view(tmp_path, monkeypatch) as (_pilot, scr, ed):
        ed._row_width = lambda: 100          # грамматика строки, а не геометрия окна
        assert _entry_aliases(ed) == ["flash", "opus"]
        flash, opus = _entry_row(ed, "flash"), _entry_row(ed, "opus")
        assert _value_color(flash, "flash") == _rgb(DEEP_DIM)
        assert _value_color(opus, "opus") == _rgb(DEEP_DIM)
        assert _value_color(flash, "основной конфиг") == _rgb(DEEP_DIM)
        assert _value_color(opus, "основной конфиг") == _rgb(DEEP_DIM)
        assert flash.plain.rstrip().endswith("← default")
        assert opus.plain.rstrip().endswith("← light")
        assert _value_color(flash, "default") == _rgb(value_style("default"))
        assert _value_color(opus, "light") == _rgb(value_style("light"))
        texts = _texts(scr)
        assert "также переопределяет" not in texts
        assert m._PROFILE_EMPTY_HINT not in texts


async def test_shadow_reads_the_base_file_not_the_merged_runtime(tmp_path, monkeypatch):
    """Тень — документ БАЗОВОГО файла, а не merged-конфиг экрана (§3, страж 14).

    Экран знает адрес `merged-host` (так выглядит запуск с активным ДРУГИМ
    профилем, перенацелившим `backend`), файл — `base-host`. Унаследованная
    запись без своего `url` работает адресом из ФАЙЛА: иначе открытый профиль A
    показывал бы «основной конфиг» через оверлей активного профиля B."""
    async with _profile_view(tmp_path, monkeypatch) as (_pilot, scr, ed):
        ed._row_width = lambda: 100
        assert scr._probe_args_from_config()[0] == _MERGED_URL
        assert ed._entry_address("opus") == _BASE_URL
        opus = _entry_row(ed, "opus")
        assert "base-host" in opus.plain
        assert "merged-host" not in opus.plain
        assert "flash-host" in _entry_row(ed, "flash").plain   # свой url записи цел


async def test_an_own_entry_under_a_base_alias_stands_in_its_position(tmp_path, monkeypatch):
    """Порядок §4: строки — записи основного файла и профиля, слитые по алиасу;
    унаследованные идут в порядке основного файла, свои-новые — после, в порядке
    файла профиля. Своя запись под ИМЕНЕМ записи основного конфига (файл профиля
    правится и руками) стоит на месте той, которую заслоняет, а не в конце.

    Происхождение двоичное: строка есть в файле профиля — она своя, и колонка
    молчит; стоит одной лишь тенью — «основной конфиг»."""
    profile = ("models:\n"
               "  glm:\n"
               '    model: "zhipu/glm-4"\n'
               '    url: "http://glm-host/v1/chat/completions"\n'
               "  opus:\n"
               '    model: "anthropic/claude-opus-4"\n')
    async with _profile_view(tmp_path, monkeypatch, profile=profile) as (_p, _scr, ed):
        ed._row_width = lambda: 100
        assert _entry_aliases(ed) == ["flash", "opus", "glm"]
        flash, opus, glm = (_entry_row(ed, a) for a in ("flash", "opus", "glm"))
        # Унаследованная строка приглушена ЦЕЛИКОМ; ярче прочего в ней ровно
        # один кусок — работающий указатель уровня (стрелка — общий DIM).
        assert _row_style_set(flash) == {_rgb(DEEP_DIM), _rgb(DIM),
                                         _rgb(value_style("default"))}
        assert "основной конфиг" in flash.plain
        assert _value_color(opus, "opus") == _rgb(INK)
        assert ed._origin_cell("opus") == ""          # своя — колонка молчит
        assert "claude-opus-4" in opus.plain          # показана СВОЯ запись профиля
        assert _value_color(glm, "glm") == _rgb(INK)
        assert "основной конфиг" not in glm.plain
        assert ed._origin_cell("glm") == ""


async def test_an_own_entry_shows_the_keys_it_inherits_per_key(tmp_path, monkeypatch):
    """Строки слиты по алиасу ПОКЛЮЧНО — тем же `deep_merge`, каким собирает
    запись загрузчик, и витрина обязана показывать то же, что будет работать.
    Профиль задал только `model` — адрес продолжает доставаться от записи
    основного конфига, а хвост ячейки честно говорит, что он не свой."""
    profile = ("models:\n"
               "  flash:\n"
               '    model: "google/gemini-3.0-flash"\n')
    async with _profile_view(tmp_path, monkeypatch, profile=profile) as (_p, _scr, ed):
        ed._row_width = lambda: 100
        assert ed._address_cell("flash") == "flash-host · адрес из «Подключения»"
        flash = _entry_row(ed, "flash")
        assert "google/gemini-3.0-flash" in flash.plain    # id — свой, он заслоняет
        assert ed._origin_cell("flash") == ""


async def test_an_own_address_keeps_the_inherited_model_id(tmp_path, monkeypatch):
    """Обратная половина того же: профиль задал только `url` — провайдерский id
    остаётся от записи основного конфига, а хвоста про адрес из «Подключения» у
    ячейки адреса больше нет (адрес стал своим)."""
    profile = ("models:\n"
               "  flash:\n"
               '    url: "http://own-host/v1/chat/completions"\n')
    async with _profile_view(tmp_path, monkeypatch, profile=profile) as (_p, _scr, ed):
        ed._row_width = lambda: 100
        assert ed._entry_model("flash") == "google/gemini-2.5-flash"
        flash = _entry_row(ed, "flash")
        assert "google/gemini-2.5-flash" in flash.plain
        assert "own-host" in flash.plain
        assert "из основного" not in flash.plain
        assert ed._origin_cell("flash") == ""


async def test_picker_mirrors_origin_brightness(tmp_path, monkeypatch):
    """Своя запись, белая в реестре, не имеет права сереть в пикере (ревизия
    владельца по смоку A+B): пикер зеркалит реестр не только колонками id+адрес,
    но и происхождением — свои ярко, унаследованные приглушены. Основной секции
    это не касается (без тени label_style не ставится — прежний DIM)."""
    body = 'models:\n  delta:\n    model: "d/delta"\n'
    async with _profile_view(tmp_path, monkeypatch, profile=body) as (pilot, scr, ed):
        _cursor_to(ed, "slot", level="default")
        await pilot.press("enter")
        await pilot.pause()
        by = {r["label"]: r for r in ed._picker["rows"][1:]}
        assert by["delta"]["label_style"] == INK        # своя — ярко, как в реестре
        assert by["flash"]["label_style"] == DEEP_DIM   # унаследованная — приглушена
        assert _value_color(by["delta"]["desc"], "d/delta") == _rgb(DIM)
        assert _value_color(by["flash"]["desc"],
                            by["flash"]["desc"].plain.split()[0]) == _rgb(DEEP_DIM)


async def test_del_on_an_inherited_row_refuses_instead_of_reporting_success(
        tmp_path, monkeypatch):
    """Del на унаследованной строке — отказ СРАЗУ, без confirm-баннера (ревизия
    владельца по смоку A+B): баннер «точно удалить?» обещал бы удаление, которого
    не случится ни при каком ответе — вопрос ложен, а не преждевременен. Отказ —
    канона «✗ не <глагол>: <причина>», файл байт-в-байт, строка на месте."""
    async with _profile_view(tmp_path, monkeypatch) as (pilot, scr, ed):
        before = _profile_path(tmp_path).read_bytes()
        _cursor_to(ed, "entry", alias="flash")
        await pilot.press("delete")
        await pilot.pause()
        assert not ed.awaiting_confirm              # ложного вопроса нет
        assert not list(ed.query(".models-confirm"))
        assert "✗ не удалено" in _notice(scr)
        # Удача у секции идёт ПЛАШКОЙ (`_models_notice`: ok=True → `_flash`), и
        # ложное «удалена» пришло бы именно туда.
        assert "удалена" not in _flash(scr)
        assert "flash" in _entry_aliases(ed)               # строка на месте
    assert _profile_path(tmp_path).read_bytes() == before


# --- действия по строкам реестра профиля (§5) ---------------------------------


def _footer(scr) -> str:
    from textual.widgets import Static as _Static
    return str(scr.query_one("#set-footer", _Static).render())


def _confirm_banner(ed) -> str:
    return " ".join(str(w.render()) for w in ed.query(".models-confirm"))


async def test_enter_on_an_inherited_row_refuses_instead_of_opening_a_form(
        tmp_path, monkeypatch):
    """Enter на унаследованной строке — отказ с причиной, а не форма.

    Править в этом файле нечего: запись живёт в основном конфиге, а копия под тем
    же именем была бы отдельной записью, отличающейся от своей ровно совпадением
    имени (решение владельца по смоку — переопределение снято). Молчаливый no-op
    был бы тупиком: Enter — клавиша открытия у всех прочих строк, и на отказ он
    обязан отвечать словами. Файлы — байт-в-байт, формы на экране нет."""
    from tui.editors.model_entry_form import ModelEntryForm
    async with _profile_view(tmp_path, monkeypatch) as (pilot, scr, ed):
        base_file = tmp_path / "base" / "config.yml"
        base_before, before = base_file.read_bytes(), _profile_path(tmp_path).read_bytes()
        _cursor_to(ed, "entry", alias="flash")
        await pilot.press("enter")
        await pilot.pause()
        assert not ed.form_open
        assert not list(scr.query(ModelEntryForm))
        assert "✗ не изменено" in _notice(scr)
        assert "flash" in _entry_aliases(ed)               # строка на месте
        assert base_file.read_bytes() == base_before
    assert _profile_path(tmp_path).read_bytes() == before


async def test_a_removal_that_could_not_touch_the_file_reports_a_refusal(
        tmp_path, monkeypatch):
    """Удаление, которое ничего не удалило, не смеет рапортовать успех.

    Рукописная flow-форма секции (`models: {glm: {...}}`): запись в документе
    есть, СТРОКИ под неё нет — писатель отказывает, и секция обязана донести
    именно отказ. Живой смок ревью показывал обратное: строка оставалась на
    месте, а плашка говорила об успехе."""
    profile = 'models: {glm: {model: "my/own"}}\n'
    async with _profile_view(tmp_path, monkeypatch, profile=profile) as (pilot, scr, ed):
        before = _profile_path(tmp_path).read_bytes()
        assert ed._entry_origin("glm") == "own"
        _cursor_to(ed, "entry", alias="glm")
        await pilot.press("delete")
        await pilot.pause()
        assert ed.awaiting_confirm
        await pilot.press("enter")
        await pilot.pause()
        assert "✗ не удалено" in _notice(scr)
        assert "удалена" not in _flash(scr)         # удача идёт плашкой — её нет
        ed = _editor(scr)
        ed._row_width = lambda: 100
        assert "glm" in _entry_aliases(ed)          # строка на месте
    assert _profile_path(tmp_path).read_bytes() == before


async def test_the_last_own_entry_does_not_promise_an_empty_registry(
        tmp_path, monkeypatch):
    """`last`-логика баннера («реестр станет пустым — уровни выключатся») считается
    по ЭФФЕКТИВНОМУ реестру (§5): своя запись бывает последней в файле профиля, а
    уровни после её удаления продолжат работать записями основного конфига —
    обещать их выключение значило бы пугать несуществующим."""
    profile = 'models:\n  glm:\n    model: "zhipu/glm-4"\n'
    async with _profile_view(tmp_path, monkeypatch, profile=profile) as (pilot, _s, ed):
        assert ed._own_aliases == ["glm"]              # единственная СВОЯ запись
        _cursor_to(ed, "entry", alias="glm")
        await pilot.press("delete")
        await pilot.pause()
        banner = _confirm_banner(ed)
        assert "Удалить запись «glm» из реестра?" in banner
        assert "Реестр станет пустым" not in banner
        assert "Enter — удалить · Esc — отмена" in banner


async def test_the_registry_footer_promises_by_row_origin(tmp_path, monkeypatch):
    """Страж 5: футер позиционный по ПРОИСХОЖДЕНИЮ строки. На унаследованной не
    работают ни Del, ни Enter — футер их и не обещает (канон `_FOOTER_RESERVE`:
    недоступное не обещаем). Своя строка — как была."""
    profile = ("models:\n"
               "  glm:\n"
               '    model: "zhipu/glm-4"\n')
    async with _profile_view(tmp_path, monkeypatch, profile=profile) as (pilot, scr, ed):
        # Курсор водим клавишами: футер обновляется вместе с ним, и обещание,
        # посчитанное мимо этого пути, ничего бы не доказало.
        assert _entry_aliases(ed) == ["flash", "opus", "glm"]
        footer = _footer(scr)                       # вход — курсор на унаследованной
        assert "Del" not in footer and "Enter" not in footer
        assert "n — добавить" in footer             # то, что работает, обещано
        for _ in range(2):
            await pilot.press("down")
            await pilot.pause()
        footer = _footer(scr)                       # своя
        assert "Del — удалить" in footer and "Enter — изменить" in footer


async def test_creating_an_entry_named_like_a_base_one_is_refused(
        tmp_path, monkeypatch):
    """Страж 12: создание с именем унаследованной записи — ошибка формы; файл
    профиля не тронут. Дублем это не является (своей записи с таким именем в
    профиле нет), но и занять имя, которым уже зовётся строка реестра, нельзя:
    две строки под одним алиасом читались бы как одна.

    Обещать переопределение отказ не смеет — такой двери больше нет."""
    from textual.widgets import Input, Static as _Static
    from tui.editors.model_entry_form import ModelEntryForm
    async with _profile_view(tmp_path, monkeypatch) as (pilot, scr, ed):
        before = _profile_path(tmp_path).read_bytes()
        await pilot.press("n")
        await pilot.pause()
        form = scr.query(ModelEntryForm).last()
        form.query_one("#mef-alias", Input).value = "flash"
        form.query_one("#mef-model", Input).value = "zhipu/glm-4"
        form._presets_ctrl.close()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        error = str(form.query_one("#mef-error", _Static).render())
        assert "алиас занят записью основного конфига" in error
        assert "переопред" not in error
        assert _editor(scr).form_open, "форма закрылась, будто запись прошла"
    assert _profile_path(tmp_path).read_bytes() == before


# --- лестница: колонка происхождения живёт дольше указателя -------------------


async def test_the_origin_column_survives_longer_than_the_pointer(tmp_path, monkeypatch):
    """Свип ширин 12…139 живым ресайзом (замер — `region.height` смонтированной
    строки, длина текста тут не показатель: хвостовые пробелы Rich не считает).

    Три факта разом: строка не переносится НИ НА КАКОЙ ширине; порядок жертв
    §4 — происхождение переживает указатель (существует ширина, где указателя
    уже нет, а слово «основной конфиг» ещё стоит); у унаследованной строки
    хвост про адрес из «Подключения» подавлен — двух происхождений в одной
    строке не бывает ни на какой ширине."""
    async with _profile_view(tmp_path, monkeypatch, size=(80, 40)) as (pilot, _scr, ed):
        origin_outlived_pointer = False
        for width in range(12, 140):
            await pilot.resize_terminal(width, 40)
            await pilot.pause()
            # Замер сходится не с первого прохода: появившийся скроллбар уносит
            # регион строки уже ПОСЛЕ раскладки, и секция пересчитывает себя сама
            # (`_confirm_width`). Мерить перенос до схождения значит мерить
            # предыдущую ширину.
            await wait_until(pilot, lambda: ed._measured_width == ed._row_width(),
                             f"замер ширины сошёлся на {width}", steps=20, step_s=0.01)
            if ed._row_width() < 2:
                continue
            wrapped = [str(w.render()) for w in ed.query(".models-row")
                       if w.region.height != 1]
            assert not wrapped, (width, ed._row_width(), wrapped[:2])
            base = _entry_row(ed, "opus").plain
            assert "адрес из" not in base, (width, base)
            if "основной конфиг" in base and "←" not in base:
                origin_outlived_pointer = True
        assert origin_outlived_pointer, "колонка происхождения ушла раньше указателя"


# --- нижняя половина: слоты и привязки на эффективных данных (Task B) ---------


def _slot_row(ed, level: str):
    return ed._row_text({"kind": "slot", "level": level}, False)


def _assign_row(ed, consumer: str):
    return ed._row_text({"kind": "assign", "consumer": consumer}, False)


def _doc(path) -> dict:
    """Документ файла, а не байты: снятие ключа оставляет пустой якорь секции —
    писатель так работает, и байтовое сравнение цели меряло бы не тот факт (§15
    стража 2). Байт-в-байт остаётся у ОСНОВНОГО файла: его секция не трогает."""
    import yaml
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


async def test_slot_value_shows_three_states(tmp_path, monkeypatch):
    """§7 дословно, три состояния значения строки уровня:

    * ключ в профиле (`light: flash`) — ЯРКО (INK, ревизия владельца по смоку:
      канонный DIM рядом с DEEP_DIM унаследованного неразличим — контраст 1.26,
      и различие держалось на одном слове-приставке; в основной секции, где
      контрастировать не с чем, канон §12.4 в силе);
    * ключа нет, слот основного задан (`default: flash`) — «основной → flash»;
    * не задан нигде (`max`) — «не задан → default», БЕЗ слова «основной»:
      источника нет, и двухстрелочная форма запрещена (+11 колонок всей нижней
      половине ни за что).

    Фоллбэк последней строки считается по MERGED слотам: свой файл знает только
    `light`, и по нему матрица `LEVEL_FALLBACK` привела бы к «не задан → light»
    — а работает уровень max через `default` основного конфига."""
    profile = 'slots:\n  light: "flash"\n'
    async with _profile_view(tmp_path, monkeypatch, profile=profile) as (_p, _s, ed):
        ed._row_width = lambda: 100          # грамматика строки, а не геометрия окна
        own, inherited, unset = (_slot_row(ed, lvl) for lvl in ("light", "default", "max"))
        assert _value_color(own, "flash") == _rgb(INK)          # свой ключ — ярко
        assert "основной" not in own.plain
        assert "основной → flash" in inherited.plain
        assert _value_color(inherited, "основной → flash") == _rgb(DEEP_DIM)
        assert "не задан → default" in unset.plain
        assert "основной → не задан" not in unset.plain
        # Ревью A→B: строка, уровень которой ЭФФЕКТИВНО работает, не смеет
        # говорить «не задан» — это и был промежуточный дефект между задачами.
        for level in LEVELS:
            row = _slot_row(ed, level)
            if ed._effective_slot_value(level):
                assert "не задан" not in row.plain, (level, row.plain)


async def test_assign_three_states_and_picker_first_row(tmp_path, monkeypatch):
    """§8: та же тройка у привязок — свой ключ ярко, привязка основного конфига
    словом «основной → light», пусто нигде — «активная». Первая строка пикера в
    обоих случаях снимает СВОЙ ключ (`value is None`), а подпись у неё — первое
    слово значения строки (канон тождества «подпись строки ↔ первый пункт»)."""
    profile = 'assign:\n  finish_gate: "max"\n'
    async with _profile_view(tmp_path, monkeypatch, profile=profile,
                             assign=True) as (pilot, _s, ed):
        ed._row_width = lambda: 100
        own = _assign_row(ed, "finish_gate")
        inherited = _assign_row(ed, "summary")
        nowhere = _assign_row(ed, "intent_scope")
        assert _value_color(own, "max") == _rgb(value_style("max"))
        assert "основной → light" in inherited.plain
        assert _value_color(inherited, "основной → light") == _rgb(DEEP_DIM)
        assert "активная" in nowhere.plain
        # «Активная» не имеет права стоять там, где значение эффективно есть:
        # оракул считается ЗДЕСЬ (свой ключ либо ключ тени), а не берётся из
        # метода секции — иначе страж сверял бы витрину с самой собой.
        for consumer in ("finish_gate", "summary", "intent_scope"):
            row = _assign_row(ed, consumer)
            if ed._assign_value(consumer) or ed._base_assign_value(consumer):
                assert "активная" not in row.plain, (consumer, row.plain)

        _cursor_to(ed, "assign", consumer="summary")
        await pilot.press("enter")
        await pilot.pause()
        first = ed._picker["rows"][0]
        assert first["label"] == "основной"
        assert first["desc"].plain == "→ light"
        assert first["value"] is None
        await pilot.press("escape")
        await pilot.pause()

        _cursor_to(ed, "assign", consumer="intent_scope")
        await pilot.press("enter")
        await pilot.pause()
        first = ed._picker["rows"][0]
        assert first["label"] == "активная"
        assert first["value"] is None
        assert "работает активным уровнем" in first["desc"].plain


async def test_the_value_column_counts_the_inherited_binding_label(tmp_path, monkeypatch):
    """Сетка нижней половины считает колонку значения по подписям СТРОК, а строка
    привязки — такой же её житель, как строка слота.

    Конфигурация подобрана так, что длину задаёт именно привязка: слоты здесь
    короткие, а «основной → very-long-alias-name» длиннее и их подписей, и голого
    алиаса в списке реестра — ровно на приставку. Не учтя её, сетка обрежет
    единственное, ради чего строка и написана, — имя модели, которой потребитель
    работает."""
    async with _profile_view(tmp_path, monkeypatch,
                             base=_BASE_LONG_ASSIGN) as (_p, _s, ed):
        ed._row_width = lambda: 120          # грамматика строки, а не геометрия окна
        label = f"основной → {_LONG_ALIAS}"
        # Посылка фикстуры: колонку подпирает привязка, а не слоты и не реестр —
        # иначе страж мерил бы не ту половину расчёта.
        assert max(len(ed._slot_unset_label(lvl)) for lvl in LEVELS) < len(label)
        assert max(len(a) for a in ed._effective_aliases) < len(label)
        assert label in _assign_row(ed, "summary").plain


async def test_slot_write_goes_to_the_profile_and_unset_returns_to_base(
        tmp_path, monkeypatch):
    """Выбор в пикере пишет `slots.<уровень>` в ФАЙЛ ПРОФИЛЯ, снятие первой
    строкой возвращает уровень основному конфигу — а его файл секция не трогает
    ни в ту, ни в другую сторону (страж 2).

    Список моделей — эффективный реестр В ПОРЯДКЕ реестра секции (§7): своего
    реестра у профиля нет вовсе, и по своим алиасам выбирать было бы не из
    чего."""
    async with _profile_view(tmp_path, monkeypatch) as (pilot, scr, ed):
        base_file = tmp_path / "base" / "config.yml"
        base_before = base_file.read_bytes()
        profile = _profile_path(tmp_path)
        _cursor_to(ed, "slot", level="default")
        await pilot.press("enter")
        await pilot.pause()
        rows = ed._picker["rows"]
        assert [r["label"] for r in rows] == ["основной", "flash", "opus"]
        assert rows[0]["desc"].plain == "→ flash"
        assert rows[0]["value"] is None
        assert ed._picker["idx"] == 0          # своего ключа нет → курсор на первой
        # Происхождение в пикере зеркалит реестр (ревизия владельца по смоку):
        # унаследованные строки приглушены целиком — и подпись, и id.
        for r in rows[1:]:
            assert r["label_style"] == DEEP_DIM
            assert _value_color(r["desc"], r["desc"].plain.split()[0]) == _rgb(DEEP_DIM)
        await pilot.press("down")
        await pilot.press("down")              # основной → flash → opus
        await pilot.press("enter")
        await pilot.pause()
        assert _doc(profile)["slots"]["default"] == "opus"
        assert base_file.read_bytes() == base_before

        ed = _editor(scr)
        _cursor_to(ed, "slot", level="default")
        await pilot.press("enter")
        await pilot.pause()
        assert ed._picker["idx"] == 2          # курсор на текущем значении (opus)
        await pilot.press("up")
        await pilot.press("up")                # opus → flash → «основной»
        await pilot.press("enter")
        await pilot.pause()
        assert "default" not in (_doc(profile).get("slots") or {})
        assert "основной → flash" in _slot_row(_editor(scr), "default").plain
        assert base_file.read_bytes() == base_before


async def test_needs_default_slot_on_the_effective_config(tmp_path, monkeypatch):
    """Страж 11 (§7): вопрос «какая по умолчанию» задаётся, когда неоднозначным
    станет ЭФФЕКТИВНЫЙ конфиг, — своего реестра для этого мало.

    Три случая формулы: база со слотом — вопроса нет; база с ОДНОЙ моделью без
    слотов плюс вторая запись в профиль — вопрос есть, слот уходит в профиль и
    merged валиден; легаси-база без реестра плюс первая запись профиля — вопроса
    нет (одна модель резолвится сама)."""
    async with _profile_view(tmp_path, monkeypatch) as (_p, _s, ed):
        # база задаёт default
        assert ed._needs_default_slot([("upsert_model", "glm", {})]) is False

    async with _profile_view(tmp_path, monkeypatch, registry=False) as (_p, _s, ed):
        # легаси-база, одна модель
        assert ed._needs_default_slot([("upsert_model", "glm", {})]) is False

    async with _profile_view(tmp_path, monkeypatch,
                             base=_BASE_ONE_MODEL) as (pilot, scr, ed):
        assert ed._needs_default_slot([("upsert_model", "glm", {})]) is True
        await pilot.press("n")
        await pilot.pause()
        await _fill_entry(pilot, scr, "glm", "zhipu/glm-4")
        ed = _editor(scr)
        assert ed.picker_open, "вопрос «какая модель по умолчанию» не задан"
        assert [r["label"] for r in ed._picker["rows"]] == ["flash", "glm"]
        await pilot.press("enter")             # default — на запись ОСНОВНОГО конфига
        await pilot.pause()
        document = _doc(_profile_path(tmp_path))
        assert document["models"]["glm"]["model"] == "zhipu/glm-4"
        assert document["slots"]["default"] == "flash"
    _validate_merged(_BASE_ONE_MODEL, document)


def _validate_merged(base_text: str, profile: dict) -> None:
    """Фаза 2 на РУЧНОМ мерже базы и профиля: файл профиля валидируется только
    фазой 1 (`validate_profile_text`), а ссылочную целостность конфиг проходит
    уже собранным — и без записанного слота такой merged невалиден."""
    import yaml
    from config import validate_config_text_with_sections
    merged = yaml.safe_load(base_text) or {}
    for key, value in profile.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    validate_config_text_with_sections(yaml.safe_dump(merged, allow_unicode=True))


async def test_empty_effective_registry_keeps_the_empty_state(tmp_path, monkeypatch):
    """Страж 8 (§10): гейт пустого состояния — ЭФФЕКТИВНЫЙ реестр. Легаси-база
    без реестра и пустой профиль — показывать нечем, слоты скрыты (легаси
    резолвится мимо реестра).

    Текст — ОДНА фраза (ревью A→C): две прежние спорили друг с другом — первая
    говорила, что реестра нет и в основном конфиге, вторая тут же обещала
    «модели действуют из основного конфига», то есть ровно то, чего по первой
    не существует. Состояние здесь всегда одно («реестра нет нигде»), и
    объяснять его двумя константами не за что."""
    async with _profile_view(tmp_path, monkeypatch, registry=False) as (_p, scr, ed):
        assert ed.registry_empty
        assert ed._rows() == []
        assert [str(w.render()) for w in ed.query(Static)] == [m._PROFILE_EMPTY_HINT]
        assert "модели действуют из основного конфига" not in _texts(scr)


async def test_slots_over_a_base_registry_are_shown_not_named_as_overrides(
        tmp_path, monkeypatch):
    """§11 в согласии с гейтом §10: «показаны формами» — тот же предикат, что и
    полный экран (эффективный реестр непуст), а не наличие `models` в файле.

    Профиль с ОДНИМИ `slots:` над базой С реестром показывает их строками — и
    называть их «также переопределяет» значит дублировать видимое. Та же
    конфигурация над легаси-базой строк не даёт вовсе — и тогда превью её
    единственный носитель."""
    profile = 'slots:\n  light: "flash"\n'
    async with _profile_view(tmp_path, monkeypatch, profile=profile) as (_p, scr, ed):
        assert [row["level"] for row in ed._rows() if row["kind"] == "slot"] == list(LEVELS)
        assert scr._profile_overlay_names() == []
        assert "также переопределяет" not in _texts(scr)

    async with _profile_view(tmp_path, monkeypatch, profile=profile,
                             registry=False) as (_p, scr, ed):
        assert ed._rows() == []
        assert scr._profile_overlay_names() == ["slots"]
        assert "профиль также переопределяет: slots" in _texts(scr)


async def test_picker_rows_never_wrap(tmp_path, monkeypatch):
    """Страж 15: строки раскрытого списка не переносятся НИ НА КАКОЙ ширине —
    тот же свип живым ресайзом, что у строк реестра, и тот же замер (`region.
    height`: хвостовые пробелы Rich не считает).

    Пикер привязки — самый широкий из двух: у него и подпись, и описание
    («основной → уровень», «id + адрес»), и уровней в списке больше.

    Свип заодно фиксирует ДЕГРАДАЦИЮ на рамке уже курсора (замеренная ширина
    0-1 колонки, терминал 8…14): строки печатаются пустыми — ни огрызка глифа,
    ни переноса, — а сам список остаётся раскрытым. Ветка не косметическая:
    двухколоночный курсор, перенесённый на вторую строку, надувает высоту
    списка, тот перерастает `max-height` и заводит скроллбар, скроллбар отнимает
    колонку — и замер не сходится никогда."""
    async with _profile_view(tmp_path, monkeypatch, assign=True,
                             size=(80, 40)) as (pilot, _scr, ed):
        _cursor_to(ed, "assign", consumer="summary")
        await pilot.press("enter")
        await pilot.pause()
        assert ed.picker_open
        narrow_seen = False
        for width in range(10, 140):
            await pilot.resize_terminal(width, 40)
            await pilot.pause()
            await wait_until(pilot,
                             lambda: ed._picker["width"] == ed._picker_width(),
                             f"замер ширины пикера сошёлся на {width}",
                             steps=20, step_s=0.01)
            measured = ed._picker_width()
            rows = list(ed.query(".models-picker-row"))
            assert ed.picker_open, width          # список пережил ресайз
            if measured is not None and measured < 2:
                narrow_seen = True
                heights = [w.region.height for w in rows]
                assert max(heights) <= 1, (width, measured, heights)
                shown = [str(w.render()).strip() for w in rows]
                assert not any(shown), (width, measured, shown[:2])
                continue
            if measured is None:
                continue        # региона нет вовсе — мерить нечего
            wrapped = [str(w.render()) for w in rows if w.region.height != 1]
            assert not wrapped, (width, measured, wrapped[:2])
        assert narrow_seen, "зона «рамка уже курсора» в свипе не встретилась"


# --- превью свёрнутых блоков: грамматика по роли (Task D, §9) -----------------
#
# Свёрнутый блок отвечает одной строкой на вопрос своего вида. У профиля вопрос
# ДРУГОЙ: не «что в реестре», а «что здесь моё» — поэтому реестр называет долю
# профиля вместо хостов, уровни показывают ЭФФЕКТИВНУЮ раскладку (превью — не
# место различать происхождение), а назначения — сколько их в профиле.


def _preview(ed, kind: str) -> str:
    return ed._block_preview(kind).plain


async def test_profile_previews_count_what_belongs_to_the_profile(tmp_path, monkeypatch):
    """Страж 7, ветка K>0: реестр — «N моделей · K в профиле» (K — записи файла
    профиля), назначения — «N · в профиле K».

    Хостов в профильном превью нет вовсе: главный вопрос профиля — «что моё»,
    а хост записи виден в её строке.

    Уровни — раскладка ЭФФЕКТИВНАЯ: `light` профиль не трогал, и превью на
    своих данных писало бы «light→—» при работающем «light→opus»."""
    profile = ('models:\n'
               '  glm:\n'
               '    model: "zhipu/glm-4"\n'
               'slots:\n'
               '  default: "glm"\n'
               'assign:\n'
               '  finish_gate: "max"\n')
    async with _profile_view(tmp_path, monkeypatch, profile=profile,
                             assign=True) as (_p, _scr, ed):
        assert _preview(ed, "entry") == "3 модели · 1 в профиле"
        assert "flash-host" not in _preview(ed, "entry")
        assert _preview(ed, "slot") == "light→opus · default→glm · max→—"
        assert _preview(ed, "assign") == "15 · в профиле 1"
        # Уровень в превью красится своим цветом — как везде на экране (§12.4).
        assert _value_color(ed._block_preview("slot"), "light") == _rgb(value_style("light"))


async def test_a_profile_that_owns_nothing_says_only_the_count(tmp_path, monkeypatch):
    """Страж 7, ветка K=0: хвоста нет вовсе — ни «0 в профиле», ни «все
    активная». Свежий профиль ничего своего не несёт, и приписка про ноль была
    бы шумом на каждой шапке."""
    async with _profile_view(tmp_path, monkeypatch, assign=True) as (_p, _scr, ed):
        assert _preview(ed, "entry") == "2 модели"
        assert _preview(ed, "assign") == "15"
        assert _preview(ed, "slot") == "light→opus · default→flash · max→—"


async def test_an_entry_under_a_base_alias_counts_as_the_profile_share(tmp_path, monkeypatch):
    """K — записи ФАЙЛА профиля, а не «те, которых нет в основном»: запись под
    именем записи основного конфига живёт в профиле, и не считать её значило бы
    писать «2 модели» о профиле, своя половина у которого есть."""
    profile = 'models:\n  opus:\n    model: "anthropic/claude-opus-4"\n'
    async with _profile_view(tmp_path, monkeypatch, profile=profile) as (_p, _scr, ed):
        assert ed._entry_origin("opus") == "own"
        assert _preview(ed, "entry") == "2 модели · 1 в профиле"


async def test_the_main_section_previews_keep_their_own_grammar(tmp_path):
    """Вторая половина стража 7: ветка по роли — значит у ОСНОВНОЙ секции
    грамматика прежняя. Тени под ней нет, «в профиле» ей сказать не о чем, а
    хосты в её превью — единственный ответ на «куда уйдут запросы»."""
    from tests.tui.test_settings_models_section import _REGISTRY, _section
    async with _section(tmp_path, _REGISTRY) as (_pilot, scr, _cfg_file):
        ed = _editor(scr)
        assert _preview(ed, "entry") == "2 модели · host"
        assert _preview(ed, "slot") == "light→— · default→flash · max→—"
        assert _preview(ed, "assign") == "15 · все активная"


# --- пустые якоря секций (Task D, §11) ----------------------------------------


def test_the_writer_leaves_exactly_the_empty_anchor_the_showcase_must_ignore():
    """Связь стража с реальностью: `slots: {}` — не выдуманная форма файла, а
    ровно то, что остаётся после снятия последнего ключа секции."""
    import yaml

    import onboarding_config_writer as ocw
    text = ocw.remove_mapping_entry('slots:\n  light: "flash"\n', "slots", "light")
    assert yaml.safe_load(text) == {"slots": {}}


async def test_empty_anchor_sections_are_not_overrides(tmp_path, monkeypatch):
    """Страж 13: пустой якорь — не переопределение. Профиль над легаси-базой
    (модельные секции формами не показаны, и превью оверлеев — их единственный
    носитель) с одним лишь `slots: {}` не переопределяет НИЧЕГО: витрина обязана
    читать пустую секцию как её отсутствие, иначе снятие последнего слота
    оставляло бы вечную строку «профиль также переопределяет: slots»."""
    async with _profile_view(tmp_path, monkeypatch, profile="slots: {}\n",
                             registry=False) as (_p, scr, _ed):
        assert scr._profile_overlay_names() == []
        assert "также переопределяет" not in _texts(scr)

    # Голый якорь без значения (`slots:` — рукописная форма того же) читается
    # так же: секция есть, переопределять ей нечем.
    async with _profile_view(tmp_path, monkeypatch, profile="slots:\nassign:\n",
                             registry=False) as (_p, scr, _ed):
        assert scr._profile_overlay_names() == []

    # А непустая секция остаётся переопределением — иначе страж зеленел бы и на
    # витрине, замолчавшей вообще обо всём.
    async with _profile_view(tmp_path, monkeypatch, profile='level: "max"\n',
                             registry=False) as (_p, scr, _ed):
        assert scr._profile_overlay_names() == ["level"]


# --- просмотр файла профиля по `v` (Task D, §11a) -----------------------------


async def test_v_opens_the_profile_file_in_the_viewer(tmp_path, monkeypatch):
    """§11a: немодельные секции профиля правятся только в YAML, но СМОТРЕТЬ их
    можно из хаба — `v` открывает файл профиля во встроенном просмотрщике
    (read-only, полным путём). Esc возвращает в под-вид, а не в список
    профилей: просмотрщик лёг ПОВЕРХ экрана настроек."""
    from tui.screens.file_view import FileViewerScreen
    async with _profile_view(tmp_path, monkeypatch) as (pilot, scr, _ed):
        assert "v — просмотр файла" in _footer(scr)
        await pilot.press("v")
        await pilot.pause()
        viewer = pilot.app.screen
        assert isinstance(viewer, FileViewerScreen)
        assert viewer._path.resolve() == _profile_path(tmp_path).resolve()
        await pilot.press("escape")
        await pilot.pause()
        assert pilot.app.screen is scr
        assert scr._profiles_open == "work"          # под-вид пережил просмотр


async def test_the_cyrillic_twin_opens_the_viewer_too(tmp_path, monkeypatch):
    """`м` — та же роль в кириллической раскладке (канон `n`/`т`, `r`/`к`): одна
    клавиша в двух написаниях, а не две разные."""
    from tui.screens.file_view import FileViewerScreen
    async with _profile_view(tmp_path, monkeypatch) as (pilot, _scr, _ed):
        await pilot.press("м")
        await pilot.pause()
        assert isinstance(pilot.app.screen, FileViewerScreen)


async def test_a_disabled_file_viewer_is_never_promised(tmp_path, monkeypatch):
    """`tui.file_view.enabled: false` — просмотрщика в этой сборке нет: футер
    молчит про `v`, подсказка оверлеев тоже, и клавиша ничего не делает.

    Канон «недоступное не обещаем»: отказ-нотисом было бы хуже — он появляется
    в ответ на обещание, которого экран не давал."""
    async with _profile_view(tmp_path, monkeypatch, profile='level: "max"\n',
                             file_view=False) as (pilot, scr, _ed):
        assert "v — просмотр файла" not in _footer(scr)
        assert "v — просмотр файла" not in scr._profile_overlay_hint()
        await pilot.press("v")
        await pilot.pause()
        assert pilot.app.screen is scr
        assert _notice(scr) == "" and _flash(scr) == ""


async def test_the_overlay_hint_names_the_way_to_see_those_sections(tmp_path, monkeypatch):
    """Подсказка строки «также переопределяет» — единственное место, где названы
    секции, форм для которых нет. Она же говорит, чем их посмотреть: иначе факт
    «правятся в YAML» оставлял бы человека без всякого хода."""
    async with _profile_view(tmp_path, monkeypatch, profile='level: "max"\n',
                             registry=False) as (_p, scr, _ed):
        texts = _texts(scr)
        assert "профиль также переопределяет: level" in texts
        assert "v — просмотр файла" in scr._profile_overlay_hint()


async def test_v_typed_into_a_form_field_stays_in_the_field(tmp_path, monkeypatch):
    """В поле формы `v` печатается, а не открывает просмотрщик.

    Меряет РЕЗУЛЬТАТ, а не механизм: держит его сегодня `showing_list` (открытая
    форма — не список), а `not isinstance(focused, Input)` рядом с ним защитный
    (финальное ревью: мутация его снятия не краснит ни одного стража). Страж
    останется честным, если гейты поменяются местами."""
    from textual.widgets import Input
    from tui.editors.model_entry_form import ModelEntryForm
    async with _profile_view(tmp_path, monkeypatch) as (pilot, scr, _ed):
        await pilot.press("n")
        await pilot.pause()
        form = scr.query(ModelEntryForm).last()
        alias = form.query_one("#mef-alias", Input)
        alias.focus()
        await pilot.pause()
        await pilot.press("v")
        await pilot.pause()
        assert pilot.app.screen is scr
        assert alias.value == "v"


async def test_the_open_picker_neither_promises_nor_takes_the_viewer_key(
        tmp_path, monkeypatch):
    """Обещание и действие держатся ОДНОГО состояния: пока раскрыт список
    значений, футер про `v` молчит — и клавиша молчит вместе с ним, иначе
    просмотрщик лёг бы поверх висящего выбора."""
    async with _profile_view(tmp_path, monkeypatch) as (pilot, scr, ed):
        _cursor_to(ed, "slot", level="light")
        await pilot.press("enter")
        await pilot.pause()
        assert ed.picker_open
        assert "v — просмотр файла" not in _footer(scr)
        await pilot.press("v")
        await pilot.pause()
        assert pilot.app.screen is scr
        assert ed.picker_open


async def test_the_main_models_section_has_no_profile_file_to_show(tmp_path):
    """`v` — клавиша ПОД-ВИДА (решение владельца): у основной секции цель записи
    и так названа строкой «запись: <полный путь>», а просмотрщик здесь про
    немодельные секции профиля. Ни футера, ни действия."""
    from tests.tui.test_settings_models_section import _REGISTRY, _section
    async with _section(tmp_path, _REGISTRY) as (pilot, scr, _cfg_file):
        assert "просмотр файла" not in _footer(scr)
        await pilot.press("v")
        await pilot.pause()
        assert pilot.app.screen is scr


# --- строка-действие и notice записи (Task D, хвосты ревью) -------------------


async def test_the_add_row_never_promises_del(tmp_path, monkeypatch):
    """Del на строке «+ добавить модель…» не работает — удалять на ней нечего.
    Футеры позиционные, и обещание прежнего футера записи было ложью В ОБЕИХ
    ролях (дефект и основной секции): чиним одной веткой."""
    async with _profile_view(tmp_path, monkeypatch) as (pilot, scr, ed):
        await pilot.press("down")
        await pilot.press("down")
        await pilot.pause()
        assert ed._row_at_cursor()["kind"] == "add"
        footer = _footer(scr)
        assert "Del" not in footer
        assert "добавить" in footer


async def test_the_add_row_of_the_main_section_never_promises_del(tmp_path):
    """Тот же дефект и та же ветка в основной секции — роль строки одна."""
    from tests.tui.test_settings_models_section import _REGISTRY, _section
    async with _section(tmp_path, _REGISTRY) as (pilot, scr, _cfg_file):
        ed = _editor(scr)
        await pilot.press("down")
        await pilot.press("down")
        await pilot.pause()
        assert ed._row_at_cursor()["kind"] == "add"
        assert "Del" not in _footer(scr)


async def test_saving_an_own_entry_reports_a_saved_entry(tmp_path, monkeypatch):
    """Итог записи один и тот же на обе роли секции: пишется всегда СВОЙ файл
    цели, и второго смысла у сохранения больше нет."""
    async with _profile_view(tmp_path, monkeypatch) as (pilot, scr, _ed):
        await pilot.press("n")
        await pilot.pause()
        await _fill_entry(pilot, scr, "glm", "zhipu/glm-4")
        assert "Запись «glm» сохранена" in _flash(scr)
