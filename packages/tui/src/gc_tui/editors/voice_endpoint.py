"""Редактор своего STT-endpoint: URL/токен/модель (+ язык — только в /settings).

Канон-рамки полей — ConnectionEditor: подпись `.onb-hint` + `PasteInput` в
скруглённой amber-рамке (у одинокого поля её рисует класс `set-framed`, у поля
со списком — группа `.field-group`), воздух между полями —
`.set-group-gap`. Набор полей задаёт вызывающий: мастер спрашивает три,
/settings — четыре (язык мастер не спрашивает, спека §3). Префилл приходит
снаружи (мастер — из своего черновика, /settings — из сырого config.yml).

Поля «Язык» и «Модель» несут раскрывающиеся списки — язык (все языки whisper,
константа) и `ModelListController` (модели самого сервера, загрузка в воркере);
общая механика обоих — в `tui.editors.field_list.FieldListController`."""
from __future__ import annotations

import re

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.widgets import Input, Static

from gc_tui.editors.field_list import (FieldListController, LIST_EMPTY, LIST_FAILED,
                                    LIST_LOADING,
                                    MODEL_RELOAD_HINT as _MODEL_RELOAD_HINT,
                                    write_field as _write_field)
from gc_tui.editors.preset_list import PRESET_HINT, preset_list_controller
from gc_tui.palette import ACCENT, DEEP_DIM
from gc_tui.widgets.paste_input import PasteInput

from gc_core.onboarding_fields import URL_EMPTY as _URL_EMPTY, URL_ERROR as _URL_ERROR
# Имя `LIST_EMPTY` уже занято статусом списка моделей («сервер не вернул моделей»,
# `field_list`) — та же формула ошибки submit'а (§4.2), но своё имя, иначе объявление
# здесь молча переопределило бы импортированную константу списка (обе строки
# исполняются на уровне модуля).
_MODEL_REQUIRED = "выберите модель"
# Адреса нет — ни явный Alt+↓ на «Модели» (`ModelListController._load`), ни ручная
# проба (`settings._voice_probe`) не знают, куда идти; строка одна на обоих
# потребителей, чтобы совет не разошёлся (находка финального ревью 2026-08-06:
# `_load` на явную клавишу молчал, а проба на тот же вопрос отвечала другим текстом).
_URL_REQUIRED = "сначала укажите адрес endpoint"

# key → (подпись, id поля, placeholder, password)
_FIELDS = {
    "url": ("URL", "voice-url", "https://…/v1/audio/transcriptions", False),
    "token": ("Токен", "voice-token", "", True),
    "model": ("Модель", "voice-model", "whisper-1", False),
    "language": ("Язык", "voice-lang", "ru", False),
}

# Экспортов id полей здесь нет: MODEL_INPUT_ID/URL_INPUT_ID заводились планом «наружу,
# для хозяина ленты», но ни один хозяин их так и не спросил — экраны сверяют id полей
# своими средствами. Мёртвый экспорт хуже отсутствующего: он выглядит контрактом.
# Фильтр пресетов не оставил строк: у соседних списков этого поля (язык/модель) хинт
# рамки в этот момент дословно совпадает с футером — «Enter — сохранить как есть»
# (находка финального ревью 2026-08-05: канон-хинт пресет-листа звал ту же клавишу
# «оставить свой адрес», и рамка спорила с футером о значении одного Enter'а). Формула
# та же, что у `_LANG_HINT_NO_MATCH`/`_MODEL_HINT_NO_MATCH`.
_URL_HINT_NO_MATCH = "свой адрес · Enter — сохранить как есть · Esc — свернуть"

# ВСЕ языки whisper (`whisper.tokenizer.LANGUAGES` модели large-v3: 99 базовых кодов +
# добавленный в ней `yue`) с русскими названиями: сначала частые (их ищут чаще всего и
# листать за ними никто не станет), затем остальные по алфавиту кода. Список не запирает
# поле — свой код можно вписать руками (напр. диалект, которого у модели нет), см.
# normalized_language.
VOICE_LANGUAGES = (
    ("ru", "русский"), ("en", "английский"), ("uk", "украинский"),
    ("de", "немецкий"), ("fr", "французский"), ("es", "испанский"),
    ("it", "итальянский"), ("pt", "португальский"), ("zh", "китайский"),
    ("ja", "японский"), ("tr", "турецкий"), ("kk", "казахский"),
    ("af", "африкаанс"), ("am", "амхарский"), ("ar", "арабский"),
    ("as", "ассамский"), ("az", "азербайджанский"), ("ba", "башкирский"),
    ("be", "белорусский"), ("bg", "болгарский"), ("bn", "бенгальский"),
    ("bo", "тибетский"), ("br", "бретонский"), ("bs", "боснийский"),
    ("ca", "каталанский"), ("cs", "чешский"), ("cy", "валлийский"),
    ("da", "датский"), ("el", "греческий"), ("et", "эстонский"),
    ("eu", "баскский"), ("fa", "персидский"), ("fi", "финский"),
    ("fo", "фарерский"), ("gl", "галисийский"), ("gu", "гуджарати"),
    ("ha", "хауса"), ("haw", "гавайский"), ("he", "иврит"),
    ("hi", "хинди"), ("hr", "хорватский"), ("ht", "гаитянский креольский"),
    ("hu", "венгерский"), ("hy", "армянский"), ("id", "индонезийский"),
    ("is", "исландский"), ("jw", "яванский"), ("ka", "грузинский"),
    ("km", "кхмерский"), ("kn", "каннада"), ("ko", "корейский"),
    ("la", "латынь"), ("lb", "люксембургский"), ("ln", "лингала"),
    ("lo", "лаосский"), ("lt", "литовский"), ("lv", "латышский"),
    ("mg", "малагасийский"), ("mi", "маори"), ("mk", "македонский"),
    ("ml", "малаялам"), ("mn", "монгольский"), ("mr", "маратхи"),
    ("ms", "малайский"), ("mt", "мальтийский"), ("my", "бирманский"),
    ("ne", "непальский"), ("nl", "нидерландский"), ("nn", "нюнорск"),
    ("no", "норвежский"), ("oc", "окситанский"), ("pa", "панджаби"),
    ("pl", "польский"), ("ps", "пушту"), ("ro", "румынский"),
    ("sa", "санскрит"), ("sd", "синдхи"), ("si", "сингальский"),
    ("sk", "словацкий"), ("sl", "словенский"), ("sn", "шона"),
    ("so", "сомалийский"), ("sq", "албанский"), ("sr", "сербский"),
    ("su", "сунданский"), ("sv", "шведский"), ("sw", "суахили"),
    ("ta", "тамильский"), ("te", "телугу"), ("tg", "таджикский"),
    ("th", "тайский"), ("tk", "туркменский"), ("tl", "тагальский"),
    ("tt", "татарский"), ("ur", "урду"), ("uz", "узбекский"),
    ("vi", "вьетнамский"), ("yi", "идиш"), ("yo", "йоруба"),
    ("yue", "кантонский"),
)
# Хинт списка — канон пресет-листа адресов (та же формула «что это · печать · Enter · Esc»).
_LANG_HINT = "языки · печать — фильтр/свой код · Enter — подставить · Esc — свернуть"
# Фильтр не оставил строк: обещать «подставить» нечего — Enter уйдёт про введённое
# (канон _LIST_NO_MATCH списка моделей — честная строка вместо залипшей).
_LANG_HINT_NO_MATCH = "свой код · Enter — сохранить как есть · Esc — свернуть"
_LANG_ROW_CLASS = "voice-lang-row"
_LANG_HINT_CLASS = "voice-lang-hint"
# Рамка-скролл вокруг строк: канон-класс `.dropdown-scroll` + свой маркер (по нему список
# находит и сносит ИМЕННО свой контейнер — id нельзя: remove() у Textual отложенный, и
# повторное раскрытие в том же такте упало бы DuplicateIds).
_LANG_SCROLL_CLASS = "voice-lang-scroll"
_LANG_CODE_W = 6          # колонка кода: 2-3 символа + воздух до названия

# Список моделей endpoint'а — та же формула хинта, что у языка (это один визуальный язык).
# «Перечитать», а не «обновить»: словарь разведён (§5.4) — «обновить версию» закреплено
# за Ctrl+U (ставит пакет заново), Alt+↓ только читает. Хинт и футер стоят на одном
# экране, и одна клавиша не может зваться в них разными глаголами (канон-словарь
# `Alt+↓` — спека 2026-08-06 §2.1: список с сервера — «перечитать список»).
# Вынесена отдельно: тот же глагол называет и свёрнутое состояние списка
# (`ModelListController` передаёт её как `closed_hint` — находка финального ревью
# 2026-08-06, C: раньше свёрнутый список молчал канон-словарём `LIST_CLOSED_HINT`
# («показать список»), а раскрытый — этой строкой («перечитать список»), и клавиша на
# одном поле звалась двумя глаголами в зависимости от состояния, хотя делает одно и то
# же — идёт на сервер).
_MODEL_HINT = ("модели · печать — фильтр/свой id · Enter — подставить · "
               f"Esc — свернуть · {_MODEL_RELOAD_HINT}")
_MODEL_HINT_NO_MATCH = "свой id · Enter — сохранить как есть · Esc — свернуть"
# LIST_LOADING/LIST_FAILED/LIST_EMPTY — статусы сетевого списка, общие с `model`
# (`field_list.LIST_*`, спека 2026-08-06): своих формулировок здесь больше нет.
_MODEL_ROW_CLASS = "voice-model-row"
_MODEL_HINT_CLASS = "voice-model-hint"
_MODEL_SCROLL_CLASS = "voice-model-scroll"
# Что на STT-сервере вообще может расшифровывать речь: id этих семейств у всех
# известных серверов содержат whisper (whisper-1, whisper-large-v3-turbo,
# Systran/faster-whisper-small), transcribe (gpt-4o-transcribe, voxtral-mini-transcribe)
# либо voxtral отдельно: остальная линейка Voxtral (voxtral-small-latest и т.п.) слова
# transcribe в имени не несёт вовсе и без своего маркера пряталась бы как чат-модель.
_STT_MARKERS = ("whisper", "transcribe", "voxtral")


def stt_models_first(model_ids) -> list:
    """Только STT-модели сервера — либо, если таких не нашлось, ВЕСЬ его список.

    На общем OpenAI-совместимом сервере рядом с whisper лежат сотни чат-моделей, и
    показывать их в поле «Модель» голосового ввода значит прятать нужное. Но
    нестандартный сервер (своё имя модели без маркеров) прятать нельзя — тогда список
    оказался бы пустым там, где сервер честно ответил."""
    ids = [str(m) for m in (model_ids or [])]
    stt = [m for m in ids if any(mark in m.lower() for mark in _STT_MARKERS)]
    return stt or ids


def _key_of_input(widget_id) -> str | None:
    """Ключ поля по id виджета (или None — поле не наше)."""
    for key, (_label, wid, _ph, _pw) in _FIELDS.items():
        if wid == widget_id:
            return key
    return None


def normalized_language(code) -> str | None:
    """Код языка для записи в конфиг (нижний регистр) или None — код не годится.

    Правило — 2-3 латинские буквы (ISO-639-1/2, как ждёт whisper). Проверка обязательна
    именно здесь: в поле «Язык» человек печатает ФИЛЬТР списка, и «нем» слишком легко
    доезжает до сохранения (живой смок) — молча писать это в конфиг нельзя."""
    code = (code or "").strip()
    return code.lower() if re.fullmatch(r"[A-Za-z]{2,3}", code) else None


def _lang_row(item, *, selected: bool) -> Text:
    code, name = item
    t = Text()
    t.append("▸ " if selected else "  ", style=ACCENT)
    t.append(code.ljust(_LANG_CODE_W))
    t.append(name, style=DEEP_DIM)
    return t


def _lang_initial_index(items, value: str) -> int | None:
    """Позиция кода среди строк или None (кода в списке нет). Пустое поле — ru: именно им
    сохранение и подставляет дефолт, и подсветка не должна врать.

    Отличие от пресетов адреса: подсвечена строка ДЕЙСТВУЮЩЕГО кода, а не первая. Enter
    на входе в поле — штатное сохранение этой секции, и подменять чужой язык первой
    строкой он не имеет права; своего кода (не из списка) в строках нет вовсе —
    подсвеченной строки тогда нет, и Enter сохраняет введённое."""
    if not value:
        return 0
    value = value.lower()
    return next((i for i, (code, _name) in enumerate(items) if code == value), None)


def lang_list_controller(host, *, input_id: str, langs=VOICE_LANGUAGES,
                         on_pick=None) -> FieldListController:
    """Раскрывающийся список кодов языка под полем «Язык» — общий контроллер со своими
    параметрами: строки-константа, две колонки (код и название), фильтр по обоим
    («нем» → de), подсветка действующего кода.

    Раскрытие — по общему правилу панели (`can_open`): сам только на пустом поле,
    поверх значения — `Alt+↓` или печать. Правило одно на все три списка панели, и
    заводить исключение «язык всегда раскрывается» нельзя: тогда одно и то же
    действие (вход курсора на поле) вело бы себя по-разному в соседних строках."""
    return FieldListController(
        host, input_id=input_id, row_class=_LANG_ROW_CLASS,
        hint_class=_LANG_HINT_CLASS, scroll_class=_LANG_SCROLL_CLASS,
        rows_source=tuple(langs), render_row=_lang_row,
        matches=lambda item, q: q in item[0] or q in item[1],
        value_of=lambda item: item[0], initial_index=_lang_initial_index,
        hint=lambda ok: _LANG_HINT if ok else _LANG_HINT_NO_MATCH,
        can_open=lambda value: not value,
        on_pick=on_pick,
    )


class ModelListController(FieldListController):
    """Список моделей СВОЕГО endpoint под полем «Модель» (только облачная панель).

    Отличие от языка: строки не константа, а ответ сервера по адресу из поля URL —
    значит раскрытие уходит в воркер-поток (канон `ModelEntryForm` ветки профилей:
    `run_worker(thread=True, exclusive=True)` + `call_from_thread` + generation-guard,
    чтобы протухший ответ не лёг на уже закрытый/переоткрытый список). Пока летит
    запрос, под полем висит одна строка-статус: пустая рамка-скролл читалась бы как
    поломка.

    Сбой загрузки список не закрывает и секцию не роняет: остаётся статус «можно
    вписать вручную», ↑/↓ возвращаются ленте (ходить нечем), Enter сохраняет набранное
    руками — ровно как «свой код» у языка."""

    def __init__(self, host, *, input_id: str, fetch,
                 config_proxy: str | None = None, on_pick=None) -> None:
        super().__init__(
            host, input_id=input_id, row_class=_MODEL_ROW_CLASS,
            hint_class=_MODEL_HINT_CLASS, scroll_class=_MODEL_SCROLL_CLASS,
            render_row=self._model_row, matches=lambda item, q: q in item.lower(),
            initial_index=self._model_initial_index, hint=self._model_hint,
            on_pick=on_pick, closed_hint=_MODEL_RELOAD_HINT,
            # Сам раскрывается только на ПУСТОМ поле — то же правило, что у пресет-листа
            # адреса. Модель уже выбрана — раскрывать список поверх неё незачем, а цена
            # автораскрытия здесь выше, чем у локальных списков: каждый вход курсора на
            # поле уходил бы в сеть. Гейт держит ТОЛЬКО автовход: явная просьба
            # (`reload` → `open(forced=True)`) и печать (`reopen_for_typing`) идут мимо.
            # У модели АГЕНТА правило другое, потому что и виджет другой: там список не
            # сворачивается вовсе (`model.py` §9.1), выбор помечается ● в самих строках.
            can_open=lambda value: not value,
            # Строки сетевого списка приносит `_load` (потому и `_begin_open` свой), но
            # объявить источник всё равно обязаны: база спрашивает его, когда решает,
            # есть ли что обещать свёрнутым состоянием («не звать раскрыть список,
            # которого нет»). Без объявления она считает список вечно пустым и молча
            # оставляет под полем подсказку РАСКРЫТОГО — после Esc текст не менялся.
            rows_source=lambda: self._items,
        )
        self._fetch = fetch
        self._config_proxy = config_proxy
        self._status = ""          # чем занят список, пока строк нет
        self._gen = 0              # поколение загрузки: протухший ответ отбрасывается

    def _begin_open(self, field: Input, filter_text: str = "", *, forced: bool = False) -> None:
        self._visible = []
        self._idx = None
        self._mount_hint(field)
        # Вход на поле показывает ВЕСЬ список сервера (канон языка), а не отфильтрованный
        # сохранённым id: своей модели среди чужих может не быть вовсе, и пустой список
        # там читался бы как «сервер ничего не отдал». Фильтр приходит извне только от
        # перечитывания уже раскрытого списка (`reload`), где в поле лежит именно он.
        self._load(filter_text, forced=forced)

    def _reopen(self, field: Input, text: str) -> None:
        """Печать по свёрнутому списку: раскрываем тем же путём, что и вход курсора —
        загруженного списка у нас нет (`close` его уносит), фильтровать пока нечего."""
        self._mount_hint(field)
        self._load(text)

    def _can_promise(self) -> bool:
        """Строка под свёрнутым списком стоит ВСЕГДА, даже когда строк ещё нет.

        База молчит, когда показывать нечего, — у статического списка это честно. Здесь
        нет: `Alt+↓` не показывает уже добытое, а идёт за ним на сервер, и обещание
        уместно и до первой загрузки, и после того, как закрытие унесло строки."""
        return True

    def _on_close(self) -> None:
        self._gen += 1             # летящая загрузка этого раскрытия больше не наша
        # `_items` НЕ обнуляем: это последний известный ответ сервера, и по нему база
        # решает, обещать ли что-нибудь под свёрнутым списком. Обещать надо — `Alt+↓`
        # у сетевого списка уходит в сеть, а не показывает уже добытое; обнулив строки,
        # мы говорили базе «показывать нечего», и она молчала, оставляя под полем
        # подсказку РАСКРЫТОГО списка. Показать их снова нельзя: `_visible`/`_idx`
        # сброшены, а следующее раскрытие всё равно идёт за свежими (`_begin_open`).
        self._visible = []
        self._idx = None
        self._status = ""

    def reload(self) -> bool:
        """Alt+↓ на поле: перечитать список с сервера принудительно (раскрыт он или
        нет) — сервер мог ожить, адрес или токен — смениться (спека 2026-08-06 §2, §4.1:
        роль ушла с Ctrl+Space). Тем и отличается от списка языка, где перечитывать нечего.

        По РАСКРЫТОМУ списку в поле может лежать фильтр: baseline не трогаем (иначе Esc
        вернул бы остаток фильтра, а Enter записал бы его как id), а сам фильтр переносим
        в новое раскрытие — список и поле обязаны показывать одно и то же.

        Фильтр — это ровно то, что РАЗОШЛОСЬ с «значением до фильтра»: пока человек
        ничего не напечатал (в т.ч. по свёрнутому списку, после Esc), в поле лежит
        настоящий id, и сужать по нему список нельзя — своей модели среди чужих может
        не быть вовсе, и перечитывание показало бы пусто."""
        was_open = self.is_open
        current = self._current_value()
        filter_text = current if was_open and current != self._value0 else ""
        self.close(restore=False)   # no-op, если и так свёрнут
        self.open(keep_baseline=was_open, filter_text=filter_text, forced=True)
        return True

    def _load(self, filter_text: str, *, forced: bool = False) -> None:
        url = self._host.list_source_url()
        if not url:
            if forced:
                # Явная клавиша (Alt+↓) на пустом адресе — риск §6 спеки 2026-08-06,
                # находка B финального ревью: молча сворачиваться в ответ на прямую
                # просьбу нельзя, человек так и не узнает, чего не хватает. Строка та
                # же, что пишет проба на тот же вопрос (`settings._voice_probe`).
                self._status = _URL_REQUIRED
                self._refresh_rows()
                return
            # Автовход курсора на поле — идти некуда, но и не повод отчитывать:
            # человек ещё только заполняет форму (тихо сворачиваемся).
            self.close(restore=False)
            return
        token, verify = self._host.list_source_auth()
        self._gen += 1
        gen = self._gen
        self._status = LIST_LOADING
        self._refresh_rows()

        def _work() -> None:
            try:
                models = self._fetch(url, token, verify,
                                     config_proxy=self._config_proxy)
            except Exception:
                models = None       # сбой загрузки секцию не роняет — см. док-строку
            try:
                self._host.app.call_from_thread(self._on_loaded, models, gen, filter_text)
            except Exception:
                pass                # приложение уже закрылось, пока запрос летел

        self._host.run_worker(_work, thread=True, exclusive=True, name="voice-model-list")

    def _on_loaded(self, models, gen: int, filter_text: str) -> None:
        # Поколение протухло (список закрыли/переоткрыли, пока запрос летел) — коллбэк
        # не про этот диалог: не трогаем ни состояние, ни DOM.
        if gen != self._gen or not self.is_open or not self._host.is_mounted:
            return
        if models is None:
            self._status = LIST_FAILED
            self._refresh_rows()
            return
        self._items = tuple(stt_models_first(models))
        if not self._items:
            self._status = LIST_EMPTY
            self._refresh_rows()
            return
        self._status = ""
        self._mount_scroll()
        self.apply_filter(filter_text)
        if not (filter_text or "").strip():
            self._idx = self._pick_index(self._current_value().strip())
            self._refresh_rows()

    @staticmethod
    def _model_initial_index(items, value: str) -> int | None:
        """Позиция ДЕЙСТВУЮЩЕЙ модели среди строк (канон языка): Enter сразу после входа
        на поле не имеет права подменить чужой id первой строкой сервера. Своего id в
        списке нет — подсвеченной строки нет вовсе, и Enter уйдёт про введённое; поле
        пустое — защищать нечего, подсвечиваем первую."""
        if not value:
            return 0 if items else None
        value = value.lower()
        return next((i for i, model_id in enumerate(items)
                     if model_id.lower() == value), None)

    def _model_hint(self, has_selection: bool) -> str:
        if self._status:
            return self._status
        return _MODEL_HINT if has_selection else _MODEL_HINT_NO_MATCH

    @staticmethod
    def _model_row(item, *, selected: bool) -> Text:
        t = Text()
        t.append("▸ " if selected else "  ", style=ACCENT)
        t.append(item)
        return t


class VoiceEndpointEditor(Vertical):
    def __init__(self, *, values: dict, fields=("url", "token", "model"),
                 gap: bool = False,
                 autofocus: bool | str = True, placeholders: dict | None = None,
                 readonly: tuple[str, ...] = (), fetch=None,
                 saved_token: str = "", verify_ssl=True,
                 config_proxy: str | None = None,
                 url_presets: list | None = None) -> None:
        # gap — зазор группы над первой подписью: в мастере воздух дают строки опций
        # (set-spaced), в /settings редактор идёт вплотную к подсказке действия.
        # autofocus — забирать ли фокус при монтировании: True — первое поле (мастер:
        # поля появляются ТОЛЬКО под курсором на «Своём endpoint»), False — не забирать,
        # ключ поля — забрать именно им (/settings: поля видны всегда, и куда встать,
        # знает курсорная лента секции; ставим на монтировании — раньше полей ещё нет).
        # placeholders — переопределение подсказок полей: про уже СОХРАНЁННЫЕ значения
        # знает вызывающий (мастер — нет, /settings читает config.yml).
        # readonly — поля-факты: показывают чужие (не правимые здесь) значения, например
        # адрес и модель локального сервиса. Правит их не человек, а установка сервиса.
        # fetch(url, token, verify) — чем брать список моделей сервера. Не задан — списка
        # у поля «Модель» нет вовсе: мастер спрашивает адрес и токен ВПЕРВЫЕ, ходить с
        # ними в сеть на каждый вход в поле там незачем (и никто не просил).
        # saved_token/verify_ssl — чем дополнять запрос: пустое поле токена значит
        # «сохранённый» (то же правило, что при сохранении), verify — из voice-конфига.
        # config_proxy — прокси агента (backend.proxy): облачный STT ходит наружу
        # по тем же правилам, что backend, локальный сервис — всегда напрямую.
        # url_presets — пресеты СТТ-адресов (спека 2026-08-05 §3). None/пустой — списка
        # у поля «URL» нет: мастер и /settings решают это сами (см. url_list ниже).
        super().__init__(classes="onb-dyn set-gap" if gap else "onb-dyn")
        self._values = values or {}
        self._fields = tuple(fields)
        self._autofocus = autofocus
        self._placeholders = placeholders or {}
        self._readonly = tuple(readonly)
        self._saved_token = saved_token or ""
        self._verify_ssl = verify_ssl
        self._inputs: dict = {}         # ключ → поле, заполняет compose (см. focus_field)
        # Сохранённый токен помечен к удалению (Ctrl+U). Пустое поле само по себе значит
        # «оставить как было» — стереть секрет иначе нечем (канон ConnectionEditor).
        self._token_cleared = False
        # Поле последней ошибки submit() — хозяин ленты (`/settings._voice_cloud_save`)
        # ставит курсор/фокус по нему, а не всегда на URL (находка финального ревью
        # 2026-08-05: пустая «Модель» на чужом пресете уводила прочь от названного поля).
        self.last_error_field: str | None = None
        # Списки живут с редактором (их поля — его DOM), а раскрывает/сворачивает их
        # хозяин курсорной ленты: он один знает, где курсор.
        self.lang_list = lang_list_controller(
            self, input_id=_FIELDS["language"][1],
            on_pick=lambda value: self._write("language", value))
        # Поле-факт списка не получает: в локальной панели модель задаёт установка сервиса.
        self.model_list = (
            ModelListController(self, input_id=_FIELDS["model"][1], fetch=fetch,
                                config_proxy=config_proxy,
                                on_pick=lambda value: self._write("model", value))
            if fetch is not None and "model" in self._fields
            and "model" not in self._readonly else None)
        # Пресеты СТТ-адресов (спека 2026-08-05 §3): тот же контроллер, что у адреса
        # подключения, — печать это и фильтр, и свой адрес (restore_on_close=False
        # внутри фабрики). Поле-факт (readonly) списка не получает — его пишет
        # установка сервиса, не человек.
        presets = list(url_presets or [])
        self.url_list = (
            preset_list_controller(
                self, presets=presets, row_class="voice-url-preset-row",
                input_id=_FIELDS["url"][1], hint=PRESET_HINT,
                no_match_hint=_URL_HINT_NO_MATCH,
                on_pick=self._apply_url_preset)
            if presets and "url" in self._fields and "url" not in self._readonly
            else None)

    def _write(self, key: str, value: str) -> None:
        """Подстановка выбранного в поле — действие Enter'а списков (`on_pick`)."""
        _write_field(self.query_one(f"#{_FIELDS[key][1]}", Input), value)

    def _apply_url_preset(self, preset) -> None:
        """Enter по строке пресета: подставить адрес; ЧУЖОЙ адрес очищает поле
        «Модель» — у нового сервера свои имена, и старое с этого момента
        непроверенное утверждение (спека §4.1). Тот же адрес — модель цела."""
        changed = self.query_one(f"#{_FIELDS['url'][1]}", Input).value.strip() != preset.url
        self._write("url", preset.url)
        if changed and "model" in self._fields and "model" not in self._readonly:
            self._write("model", "")

    def list_for(self, key: str | None):
        """Контроллер списка поля или None — у этого поля списка нет."""
        if key == "language":
            return self.lang_list
        if key == "model":
            return self.model_list
        if key == "url":
            return self.url_list
        return None

    def list_source_url(self) -> str:
        """Адрес, по которому список моделей идёт за данными: то, что человек ВИДИТ в
        поле URL (а не сохранённое) — список обязан быть про правимый сейчас endpoint."""
        try:
            return self.query_one(f"#{_FIELDS['url'][1]}", Input).value.strip()
        except NoMatches:
            return ""

    def list_source_auth(self) -> tuple[str, object]:
        """(токен, verify) для запроса списка. Пустое поле токена = сохранённый —
        то же правило, что при сохранении секции (поле-секрет не префиллится)."""
        try:
            token = self.query_one(f"#{_FIELDS['token'][1]}", Input).value.strip()
        except NoMatches:
            token = ""
        return token or self._saved_token, self._verify_ssl

    def compose(self) -> ComposeResult:
        for i, key in enumerate(self._fields):
            label, wid, placeholder, password = _FIELDS[key]
            placeholder = self._placeholders.get(key, placeholder)
            # Зазор группы — у всех подписей, кроме первой: поля читаются парами
            # «подпись + поле», без него они слипаются в столбик (канон ConnectionEditor).
            lcls = "onb-hint onb-dyn" + ("" if i == 0 else " set-group-gap")
            yield Static(label, classes=lcls)
            # Нередактируемое поле — disabled (read_only у Input в Textual нет) плюс
            # приглушающий класс. Маску с него снимаем: она прячет секрет, а здесь
            # показан ФАКТ («не требуется») — звёздочки спрятали бы то, ради чего
            # поле и показано, а спрятать от правки его уже нечего.
            ro = key in self._readonly
            # has-saved: подсказку полю переопределяет вызывающий ровно тогда, когда
            # показывает маску уже СОХРАНЁННОГО значения — её красим цветом значения,
            # иначе она неотличима от подсказки-объяснения (спека §3.4).
            saved = " has-saved" if key in self._placeholders else ""
            # Рамка одна и она всегда (канон §3.2.1), но рисует её разное: у поля со
            # списком — группа (класс set-framed поле теряет, иначе рамок две и список
            # читается отдельным блоком), у одинокого — оно само. Группу заводим ровно
            # там, где есть что объединять: у поля-факта локальной панели списка нет
            # (его модель задаёт установка сервиса) — и группы тоже.
            grouped = self.list_for(key) is not None
            icls = "onb-dyn " + ("group-field" if grouped else "set-framed")
            field = PasteInput(value=str(self._values.get(key) or ""), id=wid,
                               placeholder=placeholder, password=password and not ro,
                               disabled=ro,
                               classes=icls + (" set-readonly" if ro else "") + saved)
            # Ссылки на поля держим сами: поле в группе — ВНУК редактора, а внуки
            # монтируются отдельным проходом, и query_one на своём on_mount ещё
            # гоночный (канон ConnectionEditor).
            self._inputs[key] = field
            if grouped:
                # Поле — ПРЯМОЙ ребёнок группы: и признак `set-disabled` (PasteInput),
                # и правила `.field-group > …` смотрят ровно на непосредственного
                # родителя — обёртка вокруг поля молча съела бы рамку и воздух.
                yield Vertical(field, id=f"{wid}-group", classes="field-group onb-dyn")
            else:
                yield field

    def on_mount(self) -> None:
        if isinstance(self._autofocus, str):
            self.focus_field(self._autofocus)
        elif self._autofocus:
            self.focus_first()
        # Строки под полями со списком — с ПЕРВОГО кадра формы, а не после первого
        # раскрытия: полей со списком здесь три, курсор стартует на строке режима, и
        # без этого подсказка была только под тем полем, куда он успел зайти (живой
        # смок 2026-08-20). `call_after_refresh` — поля в группе монтируются вторым
        # проходом, и на своём `on_mount` контроллер их ещё не находит.
        self.call_after_refresh(self._sync_list_hints)

    def _sync_list_hints(self) -> None:
        for ctrl in (self.url_list, self.model_list, self.lang_list):
            if ctrl is not None:
                ctrl.sync_hint()

    def focus_first(self) -> None:
        """Поля монтируются в уже активный экран — Textual не фокусирует их сама
        (канон ConnectionEditor.on_mount)."""
        self.focus_field(self._fields[0])

    @property
    def token_cleared(self) -> bool:
        """Сохранённый токен помечен к удалению: пустое поле теперь значит «стереть»,
        а не «оставить как было» — хозяин обязан спросить это перед записью."""
        return self._token_cleared

    def token_is_mask(self) -> bool:
        """Под полем «Токен» лежит СОХРАНЁННЫЙ секрет (показан маской-placeholder) и его
        ещё не стирали — значит `Ctrl+U` есть что делать, и футер вправе её обещать."""
        return "token" in self._placeholders and not self._token_cleared

    def clear_token(self, field_key: str | None) -> bool:
        """`Ctrl+U` на поле «Токен»: стереть СОХРАНЁННЫЙ токен. True — обработали.

        Тот же канон, что у «Подключения» (`ConnectionEditor.clear_token`): маска уходит
        сразу, из `config.yml` секрет пропадёт при сохранении, передумать можно, просто
        набрав новый. До этой правки стереть голосовой токен через интерфейс было нечем
        вовсе — поле пустое, а пустое значит «оставить сохранённый».

        Гейт держим внутри, а не у хозяев: клавишу зовут два экрана (мастер и
        /settings), и помнить условие каждой копии — верный способ разъехаться."""
        if field_key != "token" or not self.token_is_mask():
            return False        # не то поле или стирать нечего — клавиша не наша
        self._token_cleared = True
        inp = self._inputs.get("token")
        if inp is None:
            return False
        inp.value = ""
        inp.placeholder = ""        # маски сохранённого больше нет — стирать нечего
        inp.set_class(False, "has-saved")
        return True

    def focus_field(self, key: str) -> None:
        """Фокус на поле по ключу — курсорная лента /settings ходит по всем полям
        (канон ConnectionEditor.move, где курсор строки сам ставит фокус).

        По ссылке из compose, а не запросом: поле со списком лежит в группе, то есть
        ВНУКОМ, и на своём on_mount (autofocus) query_one его ещё не находит. Запрос
        остаётся фолбэком — редактор ещё не собран, и NoMatches здесь контрактный:
        хозяин ленты по нему понимает, что фокус поставит on_mount редактора."""
        field = self._inputs.get(key)
        if field is None:
            field = self.query_one(f"#{_FIELDS[key][1]}", Input)
        field.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Печать в поле со списком («Язык», «Модель» облака): раскрытый список —
        фильтрует. Свёрнутый, но поле в фокусе (человек печатает/вставляет, а не
        программа переписывает поле — restore/pick/пересборка редактора значений сами
        не рождают этот Changed, см. их `set_reactive`) — раскрывает список заново с
        этим же текстом как фильтром: после Esc список свёрнут, а раскрыть его больше
        нечем (см. reopen_for_typing)."""
        ctrl = self.list_for(_key_of_input(event.input.id))
        if ctrl is None:
            return
        if ctrl.is_open:
            ctrl.apply_filter(event.input.value)
        elif event.input.has_focus:
            ctrl.reopen_for_typing(event.input.value)

    def pick_in(self, key: str) -> bool:
        """Enter при раскрытом списке поля: подставить подсвеченное значение в поле и
        свернуть список. False — подставлять нечего (списка у поля нет, он свёрнут либо
        подсвеченной строки нет, то есть в поле своё значение): Enter тогда про
        введённое вручную."""
        ctrl = self.list_for(key)
        if ctrl is None:
            return False
        return ctrl.pick() is not None       # подстановку делает `on_pick` контроллера

    def pick_language(self) -> bool:
        return self.pick_in("language")

    def values(self) -> dict:
        """Введённые значения по ключам полей (обрезанные), без сайд-эффектов."""
        return {key: self.query_one(f"#{_FIELDS[key][1]}", Input).value.strip()
                for key in self._fields}

    def submit(self) -> tuple[bool, dict | None]:
        """(ok, значения|None). Пустой/невалидный URL или пустая «Модель» →
        (False, None), ошибка показана внутри редактора (канон EndpointEditor);
        какое поле виновато — `last_error_field` (см. его комментарий)."""
        from gc_core.onboarding import _valid_endpoint_url
        values = self.values()
        url = values["url"]
        if not url or not _valid_endpoint_url(url):
            self.last_error_field = "url"
            self._show_error(_URL_EMPTY if not url else _URL_ERROR)
            return False, None
        if "model" in self._fields and "model" not in self._readonly \
                and not values.get("model"):
            self.last_error_field = "model"
            self._show_error(_MODEL_REQUIRED)
            return False, None
        self.last_error_field = None
        for e in self.query("#voice-error"):
            e.remove()
        return True, values

    def _show_error(self, text: str) -> None:
        """Строка ошибки — одна на редактор: уже смонтированную ОБНОВЛЯЕМ, не сносим
        и не монтируем вторую (remove() отложенный → второй mount с тем же id упал бы
        DuplicateIds)."""
        for e in self.query("#voice-error"):
            e.update(text)
            return
        self.mount(Static(text, id="voice-error", classes="onb-warn onb-dyn"))
