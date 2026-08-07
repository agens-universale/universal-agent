"""Редактор подключения: адрес + токен-блок на одном экране, единый курсор.
Токен-блок — канон умных форм (как «Модель», Task 5): password-поле #conn-token +
строка-галка «без авторизации» (Space — переключить) вместо радио-выбора. Секрет
НЕ предзаполняется в поле токена — текущий показан маской в placeholder. Пресеты
адреса (спека умных форм §3.1) — общий контроллер списка (`preset_list`)."""
from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.message import Message
from textual.widgets import Input, Static

import onboarding
from onboarding_fields import (
    NO_AUTH_FROZEN_PLACEHOLDER as _NO_AUTH_FROZEN_PLACEHOLDER,
    NO_AUTH_LABEL as _NO_AUTH_LABEL,
    NO_AUTH_TOGGLE_HINT as _NO_AUTH_TOGGLE_HINT,
)
from tui.editors.field_list import write_field
from tui.editors.preset_list import PRESET_HINT as _PRESET_HINT, preset_list_controller
from tui.palette import DEEP_DIM
from tui.widgets.paste_input import PasteInput

_URL_ERROR = "нужны http(s):// и хост"
_URL_EMPTY = "введите адрес"
_TOKEN_EMPTY = "введите токен"
_TOKEN_PH_CREATE = "если не указать — запрос пойдёт без токена (подходит, если сервер его не требует)"
_PRESET_HINT_EMPTY = "выберите провайдера или впишите свой адрес · Enter — подставить"


def _auth_active(header: str | None) -> int | None:
    """Действующий вариант авторизации из сырого Authorization: 0=Bearer, 1=без, None=не задано."""
    if header is None or header == onboarding.TOKEN_SENTINEL:
        return None
    return 1 if header == "" else 0


class ConnectionEditor(Vertical):
    class Ready(Message):
        """Редактор собран: адрес в поле, судьба пресет-листа решена (раскрыт по канону
        §2.1 — или нет, если в поле свой адрес). Хозяину пора пересчитать хром секции.

        Хозяин считает футер и dirty-маркер сразу после монтажа тела и повторяет расчёт
        `call_after_refresh`, но `on_mount` редактора идёт ЕЩЁ позже — в первом кадре
        секция обещала клавиши без списка. До сих пор это чинил побочный `Input.Changed`
        от поля, смонтированного со значением: хозяин принимал его за печать и обновлял
        и футер, и «изменено». Тем же событием список сужался по собственному значению
        поля (живой смок 2026-08-04), поэтому адрес теперь пишется программно
        (`write_field`), а молчаливую подпорку заменяет это сообщение.

        Сообщением, а не звонком в экран: редактор живёт на двух экранах и в голом
        харнесе тестов, где хозяина нет вовсе."""

    def __init__(self, *, url_current: str, auth_header: str | None,
                 proxy_current: str = "",
                 presets: list | None = None, create_mode: bool = False) -> None:
        super().__init__(classes="onb-dyn")
        self._url_current = url_current or ""
        self._auth_header = auth_header
        self._proxy_current = proxy_current or ""
        self._presets = list(presets) if presets is not None else []
        self.create_mode = create_mode
        self._no_auth = False if create_mode else (auth_header == "")
        # Позиции курсора: url/token/proxy всегда; галка — только вне create_mode
        # (None-элемент = «не Input», как в _POSITIONS ModelEntryForm).
        self._positions = (("conn-url", "conn-token", "conn-proxy") if create_mode
                           else ("conn-url", "conn-token", None, "conn-proxy"))
        self._idx = 0
        self._initial_url = self._url_current
        self._initial_proxy = self._proxy_current
        # Контроллер пресет-листа заводится в on_mount (ему нужны смонтированные поля),
        # но спросить о нём могут раньше: футер хоста считается сразу после mount(),
        # когда редактор в дереве уже есть, а своего on_mount ещё не прошёл.
        self._presets_ctrl = None

    # --- рендер ---
    def _token_is_mask(self) -> bool:
        """placeholder токена сейчас — маска сохранённого значения, а не подсказка."""
        return _auth_active(self._auth_header) == 0 and bool(self._auth_header)

    def _token_placeholder(self) -> str:
        """placeholder ≠ значение: действующий токен — маской (и НИКАКОГО хинта —
        маска сама говорит «текущее значение остаётся»); токена нет — хинт про
        пустоту прямо в поле, как у адреса (смок 2026-07-30). В файл НЕ пишется —
        печать заменяет, пустое поле = «не менять»/«не задан»."""
        if self._token_is_mask():
            return onboarding.display_secret(self._auth_header)
        return _TOKEN_PH_CREATE if self.create_mode else "введите токен"

    def _proxy_placeholder(self) -> str:
        """placeholder для поля прокси: действующее значение маской, пусто — подсказка."""
        if self._proxy_current:
            return onboarding.display_secret(self._proxy_current)
        return "напр. http://proxy:8080  или  socks5h://…"

    def _noauth_cls(self) -> str:
        sel = " selected" if self._positions[self._idx] is None else ""
        return "onb-option onb-dyn set-spaced" + sel

    def _noauth_text(self) -> Text:
        # Разбор подсказок в теле (§6.2): «Space — переключить» ОСТАЁТСЯ. Space —
        # клавиша ОДНОЙ этой строки: на адресе, токене и прокси он печатает пробел, и
        # футер, говорящий за всю секцию, обещал бы его всюду. Подсказка написана в
        # самой строке и живёт ровно столько, сколько живёт строка.
        t = Text()
        mark = "● " if self._no_auth else "○ "
        t.append(mark + _NO_AUTH_LABEL)
        t.append("   " + _NO_AUTH_TOGGLE_HINT, style=DEEP_DIM)
        return t

    def compose(self) -> ComposeResult:
        # Рамка одна и она всегда (канон §3.2.1): у поля со списком её рисует
        # группа, у одинокого поля — класс set-framed. Выбора «без рамки» нет.
        icls = "onb-dyn set-framed"
        yield Static("Адрес API-сервера", classes="onb-hint onb-dyn")
        # Ссылку на поле держим сами: внутри группы оно ВНУКА, а внуки монтируются
        # отдельным проходом — on_mount может застать группу ещё пустой (settings
        # монтирует редактор без await). query_one("#conn-url") там уже гоночный.
        # Поле монтируется ПУСТЫМ, адрес в него кладёт on_mount (`write_field`):
        # смонтированное со значением, оно постит на себя `Input.Changed`, а пресет-лист
        # к этому моменту уже раскрыт (канон §2.1) — хозяин принимал бы событие за печать
        # и сужал список до одной строки (живой смок 2026-08-04, см. on_mount).
        self._url_input = PasteInput(value="",
                                     placeholder="напр. https://host/v1/chat/completions",
                                     id="conn-url", classes="onb-dyn group-field")
        with Vertical(classes="field-group onb-dyn", id="conn-url-group"):
            # Без set-framed: рамку рисует группа, а класс поля перебил бы её по
            # специфичности (#conn-url.set-framed — id+класс) и дал бы вторую
            # рамку внутри первой.
            yield self._url_input
        yield Static("Токен", classes="onb-hint onb-dyn set-group-gap", id="conn-token-label")
        # has-saved: placeholder говорит одно из двух (маска сохранённого /
        # подсказка-объяснение), и различать их надо не вчитываясь — маркер красит
        # маску цветом значения (спека §3.4).
        tcls = icls + (" has-saved" if self._token_is_mask() else "")
        yield PasteInput(value="", placeholder=self._token_placeholder(), password=True,
                         id="conn-token", classes=tcls)
        if not self.create_mode:
            yield Static(self._noauth_text(), id="conn-noauth", classes=self._noauth_cls())
        yield Static("Прокси (необязательно)", classes="onb-hint onb-dyn set-group-gap")
        # Без has-saved: прокси префиллится значением, и его маска видна ровно в один
        # момент — когда поле стёрли, чтобы прокси СНЯТЬ. Ярко сказать там «сохранённое
        # есть» значило бы спорить с намерением.
        yield PasteInput(value=self._proxy_current, placeholder=self._proxy_placeholder(),
                         id="conn-proxy", classes=icls)

    def on_mount(self) -> None:
        self._sync_token_visibility()
        # Адрес — программной записью (`write_field`, канон «запись ≠ печать человека»):
        # обычное присваивание постит `Input.Changed`, и раскрытый пресет-лист сужался
        # бы по собственному значению поля — вход в секцию показывал одну строку вместо
        # всего списка, а уход с поля и возврат давали все (живой смок 2026-08-04).
        write_field(self._url_input, self._url_current)
        self._url_input.focus()
        # Онбординг (create_mode, поле ещё пустое) — первичный хинт зовёт выбрать
        # провайдера явно (спека §2.1); settings/edit — канонный хинт «пресеты · …».
        hint = _PRESET_HINT_EMPTY if self.create_mode and not self._url_current else _PRESET_HINT
        self._presets_ctrl = preset_list_controller(
            self, presets=self._presets, row_class="conn-preset-row",
            input_id="conn-url", hint=hint, on_pick=self._apply_preset,
        )
        # Канон §2.1: пресеты раскрыты сразу, как только курсор/фокус на адресе —
        # в т.ч. на mount (фокус стартует на url), независимо от того, задан ли
        # уже адрес (утверждённый мокап settings-секции показывает лист открытым
        # ПОД заполненным адресом).
        self._presets_ctrl.open()
        self.post_message(self.Ready())

    # --- навигация ---
    def _sync_token_visibility(self) -> None:
        """● — поле не исчезает (раскладка не прыгает), а замораживается: disabled,
        пустое, placeholder «не используется» (смок 2026-07-30 — вместо
        плашки-заглушки). ○ — поле оживает с прежним placeholder (маска
        действующего/хинт)."""
        inp = self.query_one("#conn-token", Input)
        inp.disabled = self._no_auth
        if self._no_auth:
            inp.value = ""
            inp.placeholder = _NO_AUTH_FROZEN_PLACEHOLDER
        else:
            inp.placeholder = self._token_placeholder()
        # Маркер живёт вместе с placeholder'ом: при ● текст «не используется» —
        # не маска, и поле не должно врать, что сохранённое значение есть.
        inp.set_class(not self._no_auth and self._token_is_mask(), "has-saved")

    def move(self, delta: int) -> None:
        if self._presets_ctrl.move(delta):
            # Пресет-лист модальный (как список моделей): пока по нему есть чем
            # ходить, ↑/↓ водят ПО НЕМУ — курсор формы остаётся на адресе до Enter/Esc.
            return
        if self._presets_ctrl.is_open:
            # Список раскрыт, но фильтр не оставил строк (набран свой адрес): водить
            # нечем, и держать клавишу себе значит запереть стрелки без всякой обратной
            # связи (контракт `FieldListController.move`). Отдаём их ленте — а список
            # сворачиваем: курсор уходит с поля адреса, и открытым он вводил бы в
            # заблуждение и хозяина (`presets_open` = «стрелка водила по списку»),
            # и человека. Набранное при этом остаётся: `restore_on_close=False`.
            self._presets_ctrl.close()
        self._idx = (self._idx + delta) % len(self._positions)
        if self._no_auth and self._positions[self._idx] == "conn-token":
            # Замороженное поле токена (● без авторизации) курсор пропускает —
            # фокусировать disabled-Input нельзя, делать там нечего.
            self._idx = (self._idx + delta) % len(self._positions)
        target = self._positions[self._idx]
        if target == "conn-url":
            self._presets_ctrl.open()          # канон §2.1: вход на адрес — всегда раскрыть
        else:
            # Курсор ушёл с адреса — подсказка возврата (`show_closed_hint`,
            # спека 2026-08-06 §4.4) не имеет права остаться висеть под чужим полем:
            # `Alt+↓` оттуда список адреса уже не раскрывает (`reload_presets` гейтует
            # именно позицию курсора): поле оставили — подсказка уходит с ним.
            self._presets_ctrl.hide_closed_hint()
        if not self.create_mode:
            w = self.query_one("#conn-noauth", Static)
            (w.add_class if target is None else w.remove_class)("selected")
        if target is None:
            # Галка — не Input: снять фокус явно, иначе прежний фокусный Input
            # остался бы сфокусирован и «съедал» бы Space/печать.
            self.screen.set_focus(None)
        else:
            self.query_one(f"#{target}", Input).focus()

    def toggle_no_auth(self) -> bool:
        """Space на позиции галки: переключить «без авторизации». True — курсор
        был на галке (переключено); False — курсор в другом месте, клавишу хост
        НЕ стопит (печать пробела в фокусный Input идёт как обычно)."""
        if self._positions[self._idx] is not None:
            return False
        self._no_auth = not self._no_auth
        self.query_one("#conn-noauth", Static).update(self._noauth_text())
        self._sync_token_visibility()
        return True

    def _apply_preset(self, preset) -> None:
        """Действие Enter'а по строке списка (`on_pick`): подставить адрес пресета.

        Через write_field, как и все прочие записи в поля этого редактора: обычное
        присваивание постит Input.Changed «как будто печатал человек». Сейчас это
        безвредно (список к моменту вызова уже свёрнут, и фильтр на событие не
        реагирует), но ровно от таких обходов и заводили общий шов."""
        write_field(self.query_one("#conn-url", Input), preset.url)

    def enter_presets(self) -> bool:
        """Список раскрыт (канон §2.1: он раскрыт сразу же на входе в поле
        адреса) — Enter подставляет выделенный пресет и сворачивает список
        (True, обработано — хост НЕ сабмитит форму); список УЖЕ свёрнут
        (Esc) — Enter здесь обычный submit секции (False), список сам собой
        второй раз не открывается — снова раскрыть можно, только уйдя с поля
        адреса и вернувшись на него."""
        if not self._presets_ctrl.is_open:
            return False
        self._presets_ctrl.pick()            # подстановку делает `_apply_preset`
        return True

    def escape_presets(self) -> bool:
        """Esc при открытом пресет-листе — свернуть ТОЛЬКО список, секция
        остаётся открытой. True — обработано; False — списка нет, хост решает
        сам (обычно — закрыть секцию).

        Свернув список, показываем хинт возврата (`show_closed_hint`, спека
        2026-08-06 §4.4): без него `Esc` уводил бы пресет-лист в один конец без
        обратного пути, поле остаётся в фокусе."""
        if not self._presets_ctrl.is_open:
            return False
        self._presets_ctrl.close()
        self._presets_ctrl.show_closed_hint()
        return True

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "conn-url":
            self._presets_ctrl.apply_filter(event.input.value)   # no-op, если список свёрнут

    # focus_first() здесь больше нет: он повторял приземление `move()` (раскрыть список
    # на адресе, снять подсветку галки, сфокусировать поле), только грубее — всегда на
    # первое поле. В проде его не звал никто: экраны водят ленту через `move`, а вход в
    # секцию фокусирует адрес сам `on_mount`. Голосовой редактор ту же форму давно
    # заменил точечным `focus_field(key)`.

    # --- контракт для экрана ---
    @property
    def presets_open(self) -> bool:
        """Пресет-лист адреса раскрыт: хост различает «уход с поля адреса»
        (список уже свёрнут) от навигации ПО списку (курсор не покидал поле)."""
        return self._presets_ctrl is not None and self._presets_ctrl.is_open

    @property
    def on_url_field(self) -> bool:
        """Курсор ленты стоит на поле адреса. Alt+↓ раскрывает список только там
        (`reload_presets` гейтит ровно эту позицию) — футер обязан обещать клавишу
        по тому же признаку, иначе на «Токене»/«Прокси» она молчала бы вопреки
        обещанному (находка финального ревью 2026-08-06, D)."""
        return self._positions[self._idx] == "conn-url"

    def reload_presets(self) -> bool:
        """`Alt+↓` на адресе: раскрыть свёрнутый список (спека 2026-08-06 §2, §4.1 —
        роль ушла с `Ctrl+Space`, та теперь безусловно проверяет связь, порядок клавиш
        не важен, §2.2).

        Адрес — поле СО списком, а не обычное (§5.1 канона, исправление по смоку
        2026-08-04): прежняя редакция спеки числила его обычным, и клавиша уходила в
        пробу безусловно — раскрыть список было нечем, хотя подсказка под полем это
        обещала.

        `False` — курсор не на адресе либо список уже раскрыт: у статических пресетов
        перечитывать нечего, и клавиша просто ничего не делает (падать ей больше
        некуда — `Ctrl+Space` про список у этой клавиши больше не спрашивает)."""
        if self._presets_ctrl is None or self._positions[self._idx] != "conn-url":
            return False
        return self._presets_ctrl.reload()

    @property
    def presets_have_selection(self) -> bool:
        """Есть ли в раскрытом списке подсвеченная строка — Enter подставит именно её.

        НЕ то же самое, что «список не пуст»: форсированное раскрытие (Alt+↓) поверх
        своего адреса, которого среди пресетов нет, показывает ВСЕ строки, но не
        подсвечивает ни одной (`initial_index`, спека §2.4) — Enter тогда лишь
        сворачивает список, значение остаётся набранным. Хинт под рамкой и футер
        обязаны читать именно этот признак (находка финального ревью 2026-08-06, A:
        раньше оба смотрели на «список не пуст» и в этом состоянии врали — обещали
        «подставить», хотя `pick()` вернул бы None)."""
        return self._presets_ctrl is not None and self._presets_ctrl.has_selection

    def peek(self) -> tuple[str, int, str, str]:
        """(url_field, auth_idx, token_raw, proxy) — без сайд-эффектов, для probe и dirty.
        auth_idx — производное от галки «без авторизации» (1=●, 0=○)."""
        url = self.query_one("#conn-url", Input).value.strip()
        token = self.query_one("#conn-token", Input).value.strip()
        proxy = self.query_one("#conn-proxy", Input).value.strip()
        auth_idx = 1 if self._no_auth else 0
        return url, auth_idx, token, proxy

    def is_dirty(self) -> bool:
        try:
            url, _auth_idx, token, proxy = self.peek()
        except NoMatches:
            return False
        if url != self._initial_url:
            return True
        if proxy != self._initial_proxy:
            return True
        active = _auth_active(self._auth_header)
        if self._no_auth != (active == 1):
            return True                      # галка отличается от действующего варианта
        if not self._no_auth and token:
            return True                      # галка ○ (Bearer) и введён новый токен
        return False

    def submit(self) -> tuple[str | None, str | None, str | None, str | None]:
        """(url|None, header|None, error|None, proxy|None). None-значение = не менять.
        proxy возвращается даже при ошибке (settings screen пишет его отдельно)."""
        url, _auth_idx, token, proxy = self.peek()
        # адрес
        out_url: str | None = None
        if url != self._initial_url:
            if not url:
                return None, None, _URL_EMPTY, proxy
            if not onboarding._valid_endpoint_url(url):
                return None, None, _URL_ERROR, proxy
            out_url = url
        elif not url:                        # был пуст и остался пуст, но адрес обязателен
            return None, None, _URL_EMPTY, proxy
        # авторизация — по галке «без авторизации»
        sel = 1 if self._no_auth else 0
        active = _auth_active(self._auth_header)
        header: str | None = None
        if sel == 1:                         # «без авторизации»
            if active != 1:
                header = ""                  # снять действующую авторизацию
        elif token:
            header = onboarding.authorization_header(token)
        elif active == 1 and not self.create_mode:
            # действовала «без авторизации», галку сняли (выбран Bearer), токена
            # нет ни в поле, ни действующего — честная ошибка (пользователь явно
            # ушёл с «без авторизации», токен не дал). active None/0 (авторизация
            # не настроена вовсе / уже действует Bearer) — не ошибка, header
            # остаётся None (нечего/незачем менять); create-режим — токен
            # искренне необязателен (Task 8).
            return None, None, _TOKEN_EMPTY, proxy
        # active in (0, None), поле пусто → ничего не менять (header None)
        out_proxy: str | None = None if proxy == self._initial_proxy else proxy
        return out_url, header, None, out_proxy
