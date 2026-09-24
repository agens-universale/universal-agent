"""Редактор модели: thread-fetch списка (инъекция), ⚠ для не-tool-capable, метка
«текущая» («в файле», если поле перекрыто оверлеем) у модели из конфига,
строка-источник списка, ручной ввод.

Композиция — канон единого языка форм (спека 2026-08-03 §3.1): поле сверху, его
список под ним, обоих обнимает одна рамка-группа; счётчик, ⚠ и строка-источник —
снаружи рамки. Поле само себе фильтр: напечатанное сужает список, а не совпавшее
ни с одной строкой и есть ручной ввод.

Механика списка — общий `FieldListController` (спека §8). Своего у этого списка
ровно три вещи, и все они уехали в параметры контроллера: строки разнородные
(«текущая» / «вписать вручную» / модели, `rows_source` + `render_row`), фильтр
никогда не трогает две первые (`matches`), а курсор после сужения держится за
ВЫБРАННУЮ строку по identity, а не прыгает на первую уцелевшую (`refilter_index`).
Свёртывания у списка нет (§9.1, осознанное отклонение): рамка стоит всегда, значит
нет ни `Esc`-отката, ни префилла поля — `close()` зовётся только на пересборке."""
from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, Static

from gc_tui.editors.field_list import FieldListController, LIST_FAILED, LIST_LOADING
from gc_tui.form_frame import REVEAL_WITH_TAIL
from gc_tui.palette import ACCENT, DEEP_DIM
from gc_tui.widgets.paste_input import PasteInput

from typing import NamedTuple


class _Row(NamedTuple):
    kind: str              # "current" | "model"
    model: object = None   # ModelInfo для current/model; None для manual


_SOURCE_OK = "список получен с сервера"
# LIST_FAILED/LIST_LOADING — статусы сетевого списка, общие с `voice_endpoint`
# (`field_list.LIST_*`, спека 2026-08-06): своих формулировок здесь больше нет, чтобы
# следующая правка одного экрана не развела тексты со вторым снова.
_NO_MATCH = "ничего не найдено — впишите вручную"
# Поле одно на две роли (§3.2.6): печать сужает список, а не совпавшее ни с одной
# строкой — и есть ручной ввод. Прежние «поиск модели» и «имя модели» по
# отдельности врали бы про половину поля.
_FIELD_PLACEHOLDER = "поиск или имя модели"


class ModelEditor(Vertical):
    def __init__(self, *, current: str, endpoint: str, auth: str | None,
                 verify: bool, fetch, input_id: str = "onb-model",
                 active: str = "", on_loaded=None,
                 active_overridden: bool = False,
                 config_proxy: str | None = None) -> None:
        # Курсор подводится вместе с хвостом блока: под рамкой поля — счётчик,
        # источник списка и ⚠, а `Ctrl+↓` здесь перечитывает список, не прокручивая
        # (см. REVEAL_WITH_TAIL).
        super().__init__(classes=f"onb-dyn {REVEAL_WITH_TAIL}")
        self._active_overridden = active_overridden   # модель из файла перекрыта оверлеем
        self._current = current or ""
        self._endpoint = endpoint
        self._auth = auth
        self._verify = verify
        self._config_proxy = config_proxy
        self._fetch = fetch
        self._input_id = input_id
        self._active = active or ""
        self._on_loaded = on_loaded
        self._models = None
        self._gen = 0                  # поколение загрузки: протухший ответ отбрасывается
        self._synthetic = False        # псевдо-список из active (сервер список не отдал)
        self._active_offered = True    # действующая модель есть в ответе сервера
        self.models_loaded = False
        self._filter: str = ""          # напечатанное в поле — оно же фильтр (on_input_changed)
        self._marked = ""               # модель, зафиксированная в этой сессии (● на ней)
        self._prev_row: _Row | None = None   # строка под ▸ до пересбора (см. _list_initial_index)
        self._list = FieldListController(
            self, input_id=self._input_id, row_class="onb-model-row",
            scroll_class="onb-model-scroll",
            rows_source=self._all_rows, render_row=self._row_text_for,
            matches=self._row_matches, initial_index=self._list_initial_index,
            refilter_index=self._list_refilter_index,
            # hint=None: своей строки-подсказки у списка нет — под рамкой стоит
            # собственный блок статуса (счётчик, «ничего не найдено», ⚠, источник).
            # restore_on_close=False: свёртывания нет, откатывать по Esc нечего (§9.1).
            hint=None, restore_on_close=False,
        )

    def compose(self) -> ComposeResult:
        yield Static(LIST_LOADING, classes="onb-hint onb-dyn", id="onb-model-loading")

    def on_mount(self) -> None:
        self._start_fetch()

    def _start_fetch(self, *, refresh: bool = False) -> None:
        """Сходить за списком в воркер-потоке. Поколение бампаем ЗДЕСЬ: `exclusive=True`
        уже бегущий поток не останавливает (Textual этого не умеет), и без поколения
        ответ прошлой загрузки перемонтировал бы список поверх свежего.

        `refresh` — просьба к `fetch` не отвечать из кэша (см. `reload`)."""
        self._gen += 1
        gen = self._gen
        self.run_worker(lambda: self._fetch_blocking(gen, refresh), thread=True,
                        exclusive=True, name="model-fetch")

    def reload(self) -> bool:
        """Alt+↓ на поле моделей: перечитать список с сервера принудительно —
        сервер мог ожить, адрес или токен — смениться (спека 2026-08-06 §2, §4.1 —
        роль ушла с Ctrl+Space).

        Раскрытием тут не обойтись: свёртывания у этого списка нет вовсе (§9.1) —
        рамка стоит всегда, раскрывать нечего. Нужен настоящий поход на сервер.

        Идём с `refresh`: и /settings, и мастер дают редактору fetch, обёрнутый в
        сессионный кэш списков, а ключ кэша — (url, auth, verify, proxy) — заморожен в
        конструкторе и за жизнь редактора не меняется. Без обхода клавиша была бы
        детерминированным no-op: тот же ключ — тот же ответ, всегда.

        Пометка ● и напечатанный фильтр живут в полях объекта и перечитывание их не
        трогают (§7.1: пометка переезжает только явным действием). True — клавиша
        израсходована, хозяину есть смысл забрать её себе."""
        if self.models_loaded and not self.has_models:
            # Форма «сервер не отдал»: в поле лежит НЕ фильтр, а имя модели, набранное
            # руками (has_models гасит on_input_changed). Правило донора «фильтр — то,
            # что разошлось с baseline»: здесь baseline и есть само значение, значит
            # фильтра нет, а набранное едет дальше префиллом — на случай, если сервер
            # промолчит и во второй раз.
            self._current = self._manual_value() or ""
        self._start_fetch(refresh=True)
        self._set_source(LIST_LOADING)
        return True

    def _fetch_blocking(self, gen: int, refresh: bool = False) -> None:
        try:
            if self._endpoint:
                # refresh кладём в kwargs только когда он True: обычная загрузка обязана
                # звать fetch ровно как раньше, иначе TypeError-фолбэк ниже срабатывал бы
                # у всех, кто про refresh не знает, и молча ронял бы config_proxy.
                extra = {"refresh": True} if refresh else {}
                try:
                    models = self._fetch(
                        self._endpoint, self._auth,
                        verify=self._verify, config_proxy=self._config_proxy, **extra,
                    )
                except TypeError:
                    # старые/тестовые fetch без config_proxy (и без refresh)
                    models = self._fetch(
                        self._endpoint, self._auth, verify=self._verify,
                    )
            else:
                models = None
        except Exception:
            models = None
        self.app.call_from_thread(self._handle_fetch_result, models, gen)

    async def _handle_fetch_result(self, models, gen: int) -> None:
        # Поколение протухло — ответ не про эту загрузку: не трогаем ни состояние, ни DOM.
        if gen != self._gen or not self.is_mounted:
            return
        # Строка, на которой стоял курсор ДО пересбора (перечитывание), или None —
        # первая загрузка, возвращать неоткуда.
        self._prev_row = self._list.selected_item if self.models_loaded else None
        self._synthetic = False
        self._active_offered = True
        if not models and self._active:
            from gc_core.model_picker import ModelInfo
            models = [ModelInfo(id=self._active)]
            self._synthetic = True
        if models:
            models = self._ensure_active(models)
        self._models = models
        self.models_loaded = True
        # Выбранную строку держим по identity (`_reselect_idx`, он же правило и для
        # фильтрации) — её подхватит `_list_initial_index` при раскрытии. В /settings
        # пометки ● нет вовсе — выбор там И ЕСТЬ курсор, и молчаливый переезд на якорь
        # выбросил бы несохранённый выбор: секция перестала бы читаться «изменено», а
        # Enter не сохранил бы ничего.
        self._list.close()      # раскрытие прошлого монтажа больше не наше
        # Тело сносим ЦЕЛИКОМ и дожидаемся сноса (await): перечитывание может сменить
        # форму монтажа на любую из двух («сервер отдал» ↔ «сервер не отдал»), а
        # remove() у Textual отложенный — пересборка с теми же id (#onb-model, группа)
        # без await упала бы DuplicateIds. На первой загрузке снести нечего, кроме
        # строки «загружаю модели…», — тот же вызов.
        await self.remove_children()
        # Тот же гейт ПОВТОРНО. `call_from_thread` не кладёт колбэк в очередь виджета,
        # а запускает его отдельной корутиной на цикле: пока этот обработчик висит на
        # `remove_children`, цикл свободен и успевает прокрутить следующее нажатие —
        # обработчик нового поколения проходит целиком и монтирует своё. Проснувшись,
        # мы смонтировали бы поверх смонтированного: DuplicateIds → исключение в воркере
        # (`exit_on_error=True`) → падение приложения. Достижимо автоповтором
        # удерживаемого Alt+↓ при мгновенном ответе (спека 2026-08-06 §6: терминалы
        # репитят стрелку агрессивнее, чем репитили Ctrl+Space).
        if gen != self._gen or not self.is_mounted:
            return                  # тело соберёт обработчик свежего поколения
        await self._mount_list()
        if self._on_loaded is not None:
            self._on_loaded()

    @staticmethod
    def _first_choice_idx(rows) -> int:
        """Первая строка ВЫБОРА: действующая, иначе первая модель, иначе (моделей не
        осталось) ручной ввод.

        Это же и якорь после фильтрации. «Вписать вручную» якорем быть не может, пока
        есть из чего выбирать: на ней submit() отдаёт живой текст поля, то есть остаток
        набора, и Enter записал бы в конфиг обрывок фильтра («gpt») именем модели.
        """
        for kind in ("current", "model"):
            for i, row in enumerate(rows):
                if row.kind == kind:
                    return i
        return 0                                 # только manual

    def _ensure_active(self, models: list) -> list:
        """Инвариант: действующая модель всегда присутствует в списке выбора.

        Сервер может её не вернуть — сменили эндпоинт, модель снята с раздачи. Без неё
        предвыбор встаёт на чужую модель: секция читается как «изменено» при входе, а
        Enter молча подменяет модель в конфиге. _all_rows держит тот же инвариант
        отдельной строкой «текущая», которой фильтр не касается; здесь он
        же для случая «модели нет в ответе сервера».
        """
        if not self._active or self._active in {m.id for m in models}:
            return models
        from gc_core.model_picker import ModelInfo
        self._active_offered = False
        return [ModelInfo(id=self._active)] + list(models)

    def _active_model(self):
        """ModelInfo действующей модели (из _models), или None."""
        if not self._active or not self._models:
            return None
        return next((m for m in self._models if m.id == self._active), None)

    def _all_rows(self) -> list:
        """ВСЕ строки списка (`rows_source` контроллера) — фильтр их не режет, он гасит
        уже смонтированные через `display`. Список крутится в .dropdown-scroll, так что
        капа на количество нет."""
        rows: list[_Row] = []
        active = self._active_model()
        if active is not None:
            rows.append(_Row("current", active))
        rows.extend(_Row("model", m) for m in (self._models or [])
                    if m.id != self._active)
        return rows

    @staticmethod
    def _row_matches(row, q: str) -> bool:
        """Фильтр сужает только «остальные»: строка «текущая» — якорь, она видна всегда
        (иначе Enter остался бы вовсе без строки выбора)."""
        return row.kind != "model" or q in row.model.id.lower()

    def _list_initial_index(self, rows, _value: str) -> int:
        """Что подсвечено сразу после (пере)сборки списка: строка, стоявшая под ▸ до
        пересбора, — по identity; её нет (первая загрузка) — якорь `_first_choice_idx`.
        Значение поля здесь ни при чём: в нём лежит ФИЛЬТР, а не имя модели (§3.2.6)."""
        return self._reselect_idx(rows, self._prev_row)

    def _list_refilter_index(self, rows, visible, prev) -> int:
        """Куда встаёт ▸ после сужения: на ту же строку, если она уцелела (identity), —
        иначе на якорь. Соседние списки формы прыгают на первую уцелевшую; здесь так
        нельзя: выбор в /settings И ЕСТЬ курсор, и переезд выбросил бы его молча."""
        return self._reselect_idx([rows[i] for i in visible], prev)

    @property
    def _rows(self) -> list:
        """Строки, которые сейчас на экране (фильтр гасит остальные через `display`)."""
        return self._list.visible_items

    def _active_mark(self) -> str:
        """Подпись у модели из конфига.

        Слово выбирается по тому, ДЕЙСТВУЕТ ли она: перекрытую оверлеем звать «текущей»
        нельзя — экран рядом показывает, что действует другая (спека: поле — редактор
        файла, а не табло).

        Уточнение «нет на сервере» — про модель, которой нет в ответе сервера: она в
        списке (чтобы не подменить её молча), но помечена как не предлагаемая. Исключение —
        ссылка ${ENV:VAR}: её и не может быть в ответе сервера, это не отсутствие модели.
        """
        from gc_core import config_raw
        word = "в файле" if self._active_overridden else "текущая"
        if config_raw.env_refs(self._active):
            return f"  {word} · из переменной окружения"
        if self._active_offered:
            return f"  {word}"
        return f"  {word} · нет на сервере"

    def _marked_id(self) -> str:
        """Помеченная ● модель: зафиксированная в этой сессии (mark) или, пока выбора не
        было, действующая из конфига — она и есть уже зафиксированное значение."""
        return self._marked or self._active

    def _row_marked(self, row) -> bool:
        """Помечена ли строка ●. Пометка одна на список и сверяется со СНИМКОМ момента
        выбора (`_marked`), а не с живым текстом поля: поле стало фильтром (§3.2.6) и
        меняется на каждую букву — сверка по тексту гасила бы ● и заставляла бы
        обещание Enter в футере мигать «выбрать» ↔ «далее» по мере набора. Пометка
        переезжает только явным действием.

        Вписанное своё имя пометки не получает вовсе: строки-посредника «вписать
        вручную» больше нет (смок 2026-08-04), а `mark()` без подсвеченной строки
        возвращает None — Enter там сразу значит «далее», и значение уезжает через
        submit()."""
        return bool(self._marked_id()) and row.model.id == self._marked_id()

    def _row_text_for(self, row, *, selected: bool) -> Text:
        cursor = "▸ " if selected else "  "
        t = Text()
        t.append(cursor, style=ACCENT)
        m = row.model
        is_active = row.kind == "current"
        dot = "● " if self._row_marked(row) else "○ "
        warn = " ⚠" if m.tool_capable is False else ""
        t.append(f"{dot}{m.id}{warn}")
        if is_active:
            t.append(self._active_mark(), style=DEEP_DIM)
        return t

    async def _mount_list(self) -> None:
        if not self._models:
            # Списка нет — группировать поле не с чем: остаётся одинокий ручной ввод
            # со своей рамкой и префиллом действующей модели.
            inp = PasteInput(value=self._current, placeholder="имя модели",
                             id=self._input_id, classes="onb-dyn set-framed")
            self.mount(inp)
            self.mount(Static(LIST_FAILED, id=self._source_id(),
                              classes="onb-hint onb-dyn"))
            inp.focus()
            return
        # Поле и его список — под одной рамкой (спека §3.1): поле само себе фильтр,
        # строки идут ПОД ним.
        #
        # Поле пустое, а не префилленное действующей моделью (осознанное отклонение
        # от макета §3.1, спека §9). Причина не в том, что префилл технически нельзя
        # отличить от фильтра — пресет-лист адреса именно это и делает снимком
        # значения. Причина в том, что у списка моделей НЕТ свёртывания: рамка стоит
        # всегда, значит нет и момента «Esc — свернуть с откатом фильтра», которым
        # донор возвращает поле к исходному значению. Префилл здесь превратился бы в
        # мёртвый фильтр: список сужен до одной строки, а вернуть 30-символьный id
        # обратно нечем, кроме ручной перепечатки.
        #
        # Действующая модель при этом не потеряна — она первой строкой списка, с ●
        # и подписью «текущая».
        #
        # Исключение — перечитывание (reload): там в поле уже лежал ФИЛЬТР, и вернуть
        # надо именно его, иначе строки и поле показывали бы разное. На первой загрузке
        # _filter пуст, то есть поле, как и раньше, стартует пустым.
        inp = PasteInput(value=self._filter, placeholder=_FIELD_PLACEHOLDER,
                         id=self._input_id, classes="onb-dyn group-field")
        # Без set-framed: рамку рисует группа, а класс поля перебил бы её по
        # специфичности (#onb-model.set-framed — id+класс) и дал бы вторую рамку
        # внутри первой. Воздух внутри рамки — margin по .group-field.
        group = Vertical(id=f"{self._input_id}-group", classes="field-group onb-dyn")
        # Группу и поле монтируем ПООЧЕРЁДНО и с await: строки список ставит сам, а
        # найти поле он может только по id — дети, переданные конструктору контейнера,
        # до compose-прохода лежат в `_pending_children` и запросом не находятся.
        await self.mount(group)
        await group.mount(inp)
        # Строки — во вложенный скролл с потолком высоты (.dropdown-scroll): длинный
        # список крутится внутри, страница не раскатывается. Фильтр поля переносим в
        # раскрытие: после перечитывания (reload) строки и поле обязаны показывать одно
        # и то же.
        self._list.open(filter_text=self._filter)
        # Счётчик, «ничего не найдено», ⚠ и источник — ПОД рамкой (§3.2.2/§3.2.4:
        # внутри неё только выбор). Монтируются раз и навсегда, дальше меняется
        # только их текст: пересборка сносила бы поле, в котором печатают.
        self.mount(Static("", id=self._count_id(), classes="onb-hint onb-dyn"))
        self.mount(Static("", id=self._nomatch_id(), classes="onb-hint onb-dyn"))
        self.mount(Static("", id=self._warn_id(), classes="onb-warn onb-dyn"))
        self.mount(Static(LIST_FAILED if self._synthetic else _SOURCE_OK,
                          id=self._source_id(), classes="onb-hint onb-dyn"))
        self._refresh_status()
        # Поле — внук (лежит в группе), а внуки монтируются отдельным проходом:
        # query_one("#onb-model") здесь ещё гоночный, поэтому фокусим по ссылке.
        inp.focus()

    def _refresh_status(self) -> None:
        """Счётчик, «ничего не найдено» и ⚠ — обновляются ТЕКСТОМ, без ремонтажа."""
        from gc_core.onboarding_fields import MODEL_WARN
        n = sum(1 for r in self._rows if r.kind in ("current", "model"))
        from gc_tui.services.probe_presentation import plural_models
        self._set_status(self._count_id(), f"{n} {plural_models(n)}")
        no_match = bool(self._filter) and not any(r.kind == "model" for r in self._rows)
        self._set_status(self._nomatch_id(), _NO_MATCH if no_match else "")
        warn = any(r.kind in ("current", "model") and r.model is not None
                   and r.model.tool_capable is False for r in self._rows)
        self._set_status(self._warn_id(), f"⚠ {MODEL_WARN}" if warn else "")

    def _set_source(self, text: str) -> None:
        """Строка-источник списка. В отличие от _set_status, display не трогаем: строка
        есть в обеих формах монтажа и молчащей не бывает."""
        for w in self.query(f"#{self._source_id()}"):
            w.update(text)

    def _set_status(self, wid: str, text: str) -> None:
        """Пустой текст = строки нет: display гасит и место под неё, а сам текст
        стираем, чтобы виджет не отвечал старым содержимым на запрос рендера."""
        for w in self.query(f"#{wid}"):
            w.update(text)
            w.display = bool(text)

    def _manual_selector(self) -> str:
        return f"#{self._input_id}"

    def _count_id(self) -> str:
        return f"{self._input_id}-count"

    def _nomatch_id(self) -> str:
        return f"{self._input_id}-nomatch"

    def _warn_id(self) -> str:
        return f"{self._input_id}-warn"

    def _source_id(self) -> str:
        return f"{self._input_id}-source"

    def on_input_changed(self, event) -> None:
        """Поле само себе фильтр (§3.2.6) — отдельной строки «> поиск модели» нет.

        Строки не пересобираются: фильтр гасит уже смонтированные через `display`
        (общая механика контроллера). Поле не трогаем вовсе — в нём печатают, и его
        снос убивал бы фокус, позицию курсора и набранное на каждой букве; счётчик,
        «ничего не найдено» и ⚠ обновляются текстом по той же причине."""
        # has_models: без списка фильтровать нечего, то же поле там просто ручной ввод.
        if event.input.id != self._input_id or not self.has_models:
            return
        new = event.value.strip()
        if new == self._filter:
            return
        self._filter = new
        self._list.apply_filter(new)
        self._refresh_status()

    def _reselect_idx(self, rows, prev) -> int:
        """Сохранить выбранную строку по identity через пересбор; пропала → первая
        строка выбора (§4.4, _first_choice_idx).

        Клэмп старого числового индекса к новому списку молча подменял бы модель на ту,
        что заняла ту же позицию после фильтрации, — submit()/peek() вернули бы чужой выбор.
        """
        anchor = self._first_choice_idx(rows)
        if prev is None:
            return anchor
        if prev.kind == "current":
            return next((i for i, r in enumerate(rows) if r.kind == prev.kind), anchor)
        return next((i for i, r in enumerate(rows)
                     if r.kind == "model" and r.model.id == prev.model.id), anchor)

    @property
    def is_marked(self) -> bool:
        """Строка под курсором уже помечена ● — Enter на ней ведёт ДАЛЕЕ, а не выбирает.
        Списка нет (сервер его не отдал, на экране только ручной ввод) — помечать нечего
        и негде показать ●: Enter ведёт дальше, и футер шага обещает именно это."""
        row = self._list.selected_item
        if not self.has_models or row is None:
            return True
        return self._row_marked(row)

    def mark(self) -> str | None:
        """Зафиксировать модель под курсором: ● переезжает на неё, шаг НЕ продвигаем.
        None = помечать нечего (списка нет или ручной ввод пуст) — вызывающий трактует
        Enter как «далее», иначе клавиша выглядела бы мёртвой."""
        row = self._list.selected_item
        if not self.has_models or row is None:
            return None
        if self._typed_own_name():
            # Вписано своё имя: метить нечего — Enter здесь сразу «далее», и значение
            # уезжает через submit(). Иначе человеку пришлось бы жать Enter дважды
            # (сперва пометить набранное, потом идти дальше) — механика, которой
            # владелец не просил (смок 2026-08-04).
            return None
        value = self.submit()
        if value is None:
            return None
        self._marked = value
        self._list.refresh_rows()       # ● переехала — перерисовать, ничего не выбирая
        return value

    def move(self, delta: int) -> None:
        if not self._list.move(delta):
            return
        # Поле одно и всегда на месте (§3.1) — прятать/показывать нечего, фокус из
        # него не уходит: печать продолжает фильтровать, куда бы ни встал курсор.
        for f in self.query(self._manual_selector()):
            f.focus()

    @property
    def has_models(self) -> bool:
        return bool(self._models)

    def submit(self) -> str | None:
        """Имя модели или None (не менять)."""
        if self.has_models and not self._list.is_open:
            # Список сейчас пересобирают (перечитывание ждёт сноса тела): выбирать не из
            # чего, а живой текст поля здесь — ФИЛЬТР, а не имя модели. Отдать его
            # значило бы записать в конфиг обрывок набора.
            return None
        row = self._list.selected_item
        if row is None:
            return self._manual_value()          # сервера без списка → только своё имя
        if self._typed_own_name():
            # Набранное не оставило в списке ни одной модели: человек вписывает СВОЁ имя,
            # а не фильтрует. Отдельной строки-посредника «вписать вручную» для этого
            # больше нет (смок владельца 2026-08-04) — поле само себе фильтр.
            #
            # Различаем именно по «не осталось моделей», а не по «текст ≠ id строки»:
            # при живой фильтрации текст поля это обрывок набора («gpt»), и отдать его
            # значило бы записать в конфиг мусор вместо модели.
            return self._manual_value()
        return row.model.id

    def _typed_own_name(self) -> bool:
        """В поле набрано своё имя: текст непуст, а моделей под фильтром не осталось.
        Строка «текущая» — якорь, она видна всегда, поэтому по наличию подсвеченной
        строки это не определить."""
        if not self._manual_value():
            return False
        return not any(r.kind == "model" for r in self._list.visible_items)

    def _manual_value(self) -> str | None:
        try:
            return self.query_one(self._manual_selector(), Input).value.strip() or None
        except Exception:
            return None

    def peek(self) -> str | None:
        """Текущий выбор без сайд-эффектов (для dirty-проверки)."""
        return self.submit()
