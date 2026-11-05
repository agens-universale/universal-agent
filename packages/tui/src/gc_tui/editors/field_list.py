"""Общий контроллер раскрывающегося списка под полем формы (спека единого языка
форм 2026-08-03 §8).

До этой правки одно и то же поведение жило в трёх копиях: `PresetListController`
(пресеты адреса), приватный `_FieldListController` редактора голоса (язык и модели
endpoint'а) и собственный код `ModelEditor` (модели агента). Пока поведение
расходилось, объединять было нечего; после §3–§6 копии сошлись, и каждый новый
список формы был бы четвёртой копипастой.

Контроллер владеет МЕХАНИКОЙ:

* раскрыть/свернуть — строки уходят во вложенный `.dropdown-scroll` ВНУТРЬ рамки-группы
  своего поля (рамка одна на поле и список, спека §3.1), подсказка — ПОД группой (§3.2.3);
* фильтр — по УЖЕ смонтированным строкам через `display`: DOM не пересобирается на
  каждую букву, и поле, в котором печатают, не теряет фокус и позицию курсора;
* ↑/↓ — приоритет над лентой хозяина, пока список раскрыт (`move` возвращает «клавиша
  израсходована»), но не когда ходить нечем: тогда стрелки возвращаются ленте;
* `Enter` — подставить подсвеченное; `Esc` — свернуть с откатом фильтра;
* автоподскролл подсвеченной строки внутри скролла.

Различия хостов уезжают в ПАРАМЕТРЫ, а не в флаги «если модель — так, если адрес —
иначе»: источник строк (`rows_source`), рендер строки (`render_row`), правило фильтра
(`matches`), что отдаёт выбор (`value_of`), подсветка на входе (`initial_index`),
правило «можно ли раскрывать» (`can_open`), действие по `Enter` (`on_pick`), откат
фильтра по `Esc` (`restore_on_close`).

Асинхронный источник (модели сервера приезжают воркером) — подкласс: он переопределяет
`_begin_open`/`_reopen`/`_on_close`/`reload` и кладёт приехавшие строки сам, см.
`voice_endpoint.ModelListController`.
"""

from __future__ import annotations

from rich.text import Text
from textual.containers import VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Input, Static

from gc_tui.palette import DEEP_DIM
from gc_tui.widgets.column_static import ColumnStatic

# Свёрнутый список: без этой строки поле молчит о том, что список вообще есть, и
# свернувший его Esc'ом остаётся без пути назад. Глагол — из канон-словаря `Alt+↓`
# (спека 2026-08-06 §2.1, §4.2): раскрытие списка (свёрнутый локальный источник) ушло
# с `Ctrl+Space` на `Alt+↓` — та клавиша теперь занята пробой связи там, где проба
# есть, и молчит там, где её нет (§2.5 спеки). Дефолт контроллера (параметр
# `closed_hint`) — источник с сервера (список моделей endpoint'а) переопределяет её
# своим «перечитать список»: тот же глагол, что у него в открытом состоянии, иначе
# одна клавиша на одном поле звалась бы двумя словами в зависимости от того, свёрнут
# список или нет (находка финального ревью 2026-08-06, C).
LIST_CLOSED_HINT = "Enter — показать список"

# Статусы СЕТЕВОГО списка (модели агента `model.ModelEditor`, модели STT-endpoint'а
# `voice_endpoint.ModelListController`) — раньше каждый редактор описывал одни и те же
# три состояния своими словами, и после первой же самостоятельной правки одного из них
# тексты разошлись (находка владельца на живом смоке 2026-08-06). Общее место, а не
# нормализация после факта: следующая правка одного экрана не разведёт формулировки
# снова, потому что развести им попросту нечего — константа одна на оба.
#
# LIST_LOADING — не «перечитываю»: у списка моделей агента загрузка бывает не только
# повторной (Alt+↓), но и первой — вход в поле сам идёт на сервер, и «перечитываю» там
# было бы неправдой. «Загружаю модели» верно в обоих случаях: говорит, чего ждать, а не
# как называется механизм (первая загрузка это или перечитывание).
LIST_LOADING = "загружаю модели…"
# LIST_FAILED — говорит и что случилось, и что делать: голое «сервер не отдаёт список»
# оставляло бы человека без выхода, хотя выход есть — вписать модель руками.
LIST_FAILED = "не удалось получить список — можно вписать вручную"
# LIST_EMPTY — сервер ОТВЕТИЛ, но пустым списком: другое состояние, чем сбой запроса
# (LIST_FAILED) — с адресом и сетью всё в порядке, моделей на сервере действительно нет.
LIST_EMPTY = "сервер не вернул моделей"
# MODEL_RELOAD_HINT — постоянная строка под полем со списком С СЕРВЕРА: он ходит
# туда и раскрытым, и свёрнутым, поэтому «перечитать», а не «показать»
# (LIST_CLOSED_HINT — вариант локального источника). Дом общий по той же причине,
# что у статусов выше: две формы задают этой клавише один вопрос, и звать её
# разными глаголами им нечем.
MODEL_RELOAD_HINT = "Enter — показать список"


def write_field(field: Input, value: str) -> None:
    """Программная запись значения в поле (откат фильтра, подстановка выбранного).

    `set_reactive` — официальный способ Textual положить реактив БЕЗ вотчеров: `Changed`
    тогда не постится вовсе, и запись не читается как печать человека (иначе она сама
    раскрывала бы список заново, см. `reopen_for_typing`). Плата — вместе с вотчером
    пропущен и переклэмп выделения из `Input._watch_value`: курсор оставался за границей
    нового значения (и чинился только следующей клавишей). Ставим его в конец сами —
    заодно это и перерисовка поля, которой `set_reactive` тоже не делает."""
    field.set_reactive(Input.value, value)
    field.cursor_position = len(value)
    field.refresh()


class FieldListController:
    def __init__(  # noqa: PLR0913  # S107: won't fix, см. CONTRIBUTING.md
        self,
        host,
        *,
        input_id: str,
        row_class: str,
        scroll_class: str,
        render_row,
        hint=None,
        hint_class: str = "",
        row_classes: str = "",
        rows_source=(),
        matches=None,
        value_of=None,
        initial_index=None,
        refilter_index=None,
        can_open=None,
        on_pick=None,
        restore_on_close: bool = True,
        closed_hint: str = LIST_CLOSED_HINT,
        promise_empty: bool = False,
    ) -> None:
        # rows_source — последовательность либо функция без аргументов: её перечитывают
        # на каждом раскрытии (статический список от этого не меняется, живой — меняется).
        # render_row(item, *, selected) -> Text — колонки и метки строки.
        # hint(has_selection) -> str — подсказка под списком: обещания клавиш зависят от
        #   того, есть ли подсвеченная строка (Enter её подставит), а не от того, видны
        #   ли вообще строки — форсированное раскрытие (Alt+↓) поверх значения, которого
        #   в списке нет, показывает ВСЕ строки, но не подсвечивает ни одной (спека
        #   2026-08-06 §2.4, находка финального ревью: врал именно этот параметр, когда
        #   его путали с «есть ли строки под фильтром»). None — своей строки у списка
        #   нет: под рамкой хозяин рисует собственный блок статуса (счётчик, ⚠, источник —
        #   список моделей агента).
        # row_classes — доп. классы строк сверх `row_class` (воздух `set-spaced` и т.п.).
        # matches(item, q) -> bool — правило фильтра (по умолчанию — подстрока в str).
        # value_of(item) -> значение, которое отдаёт pick() (по умолчанию — сам item).
        # initial_index(items, value) -> int|None — что подсвечено на входе; None — ничего
        #   (значение поля в списке не нашлось, и Enter уйдёт про введённое вручную).
        #   `value` — значение поля как есть (обрезанное): нормализует его сам параметр —
        #   код языка ищут без регистра, адрес пресета сверяют посимвольно.
        # refilter_index(items, visible, prev) -> int|None — куда встаёт ▸ после фильтра
        #   (индекс В `visible`). По умолчанию — первая уцелевшая строка; список моделей
        #   агента вместо этого держит выбранную строку по identity, пока она видна.
        # can_open(value) -> bool — раскрывать ли список при таком значении поля.
        # on_pick(value) — что делает Enter после выбора (обычно подстановка в поле).
        # restore_on_close — возвращать ли по Esc значение, бывшее ДО фильтра.
        # closed_hint — что показать под свёрнутым списком (ставит `_sync_hint`), чем его
        #   раскрыть. По умолчанию — канон локального источника («показать список»);
        #   список с сервера (модели endpoint'а) всегда идёт в сеть, раскрытый он или
        #   нет, — глагол тот же, что у него в открытом состоянии («перечитать список»),
        #   иначе одна клавиша на одном поле звалась бы двумя словами в зависимости от
        #   состояния (находка финального ревью 2026-08-06, §2.1 спеки).
        self._host = host
        self._input_id = input_id
        self._row_class = row_class
        self._row_classes = row_classes
        self._hint_class = hint_class
        self._scroll_class = scroll_class
        self._render_row = render_row
        self._hint = hint
        self._rows_source = rows_source
        self._matches = matches
        self._value_of = value_of
        self._initial_index = initial_index
        self._refilter_index = refilter_index
        self._can_open = can_open
        self._on_pick = on_pick
        self._restore_on_close = restore_on_close
        self._closed_hint = closed_hint
        # promise_empty — сетевой источник: Alt+↓ идёт за строками на сервер, и
        # обещание под свёрнутым списком осмысленно даже без единой добытой
        # строки (см. _can_promise; канон voice_endpoint.ModelListController).
        self._promise_empty = promise_empty
        self._items: tuple = ()
        self.is_open = False
        self._visible: list[int] = []  # индексы _items, прошедшие фильтр
        self._idx: int | None = (
            None  # позиция В _visible (None — ничего не подсвечено)
        )
        self._value0 = ""  # значение поля ДО фильтра (см. close)

    # --- параметры-различия ---
    def _read_items(self) -> tuple:
        src = self._rows_source
        return tuple(src() if callable(src) else src)

    def _row_text(self, i: int, *, selected: bool) -> Text:
        return self._render_row(self._items[i], selected=selected)

    def _match(self, i: int, q: str) -> bool:
        if self._matches is not None:
            return self._matches(self._items[i], q)
        return q in str(self._items[i]).lower()

    def would_match(self, text: str) -> bool:
        """Нашлось бы хоть что-то под таким фильтром — БЕЗ раскрытия и монтажа строк.

        Нужна хозяину, чтобы решить, стоит ли вообще поднимать свёрнутый список на
        печать: поднять и тут же свернуть значило бы монтировать и сносить строки на
        каждую букву. `visible_items` на это не годятся — они про уже раскрытый список."""
        q = (text or "").strip().lower()
        items = self._read_items()
        if not q:
            return bool(items)
        return any(
            self._matches(it, q)
            if self._matches is not None
            else q in str(it).lower()
            for it in items
        )

    def _hint_text(self) -> str:
        return self._hint(self.has_selection)

    def _value_at(self, i: int):
        return (
            self._value_of(self._items[i])
            if self._value_of is not None
            else self._items[i]
        )

    def _pick_index(self, value: str) -> int | None:
        if self._initial_index is not None:
            return self._initial_index(self._items, value)
        return 0 if self._items else None

    def _filter_index(self, prev) -> int | None:
        if not self._visible:
            return None
        if self._refilter_index is not None:
            return self._refilter_index(self._items, list(self._visible), prev)
        return 0

    # --- что видно и что выбрано ---
    @property
    def visible_items(self) -> list:
        """Строки, которые сейчас на экране (в порядке показа) — по ним хозяин считает
        свой блок статуса под рамкой."""
        return [self._items[i] for i in self._visible]

    @property
    def selected_item(self):
        """Строка под ▸ или None (подсвеченной строки нет — своё значение / пусто)."""
        if self._visible and self._idx is not None:
            return self._items[self._visible[self._idx]]
        return None

    @property
    def has_selection(self) -> bool:
        """Есть ли подсвеченная строка — Enter её подставит. НЕ то же самое, что
        непустые `visible_items`: форсированное раскрытие (Alt+↓) поверх значения, которого в
        списке нет, показывает ВСЕ строки (`visible_items` непусты), но подсвеченной среди
        них нет (`initial_index` вернул None, спека §2.4) — по этому признаку, а не по
        видимости строк, различаются обещания хинта и футера («подставить» ↔ «оставить
        своё»; находка финального ревью 2026-08-06: раньше оба текста читали
        видимость строк и врали именно в этом состоянии)."""
        return self.selected_item is not None

    # --- хуки подкласса (асинхронный источник строк) ---
    def _begin_open(
        self, field: Input, filter_text: str = "", *, forced: bool = False
    ) -> None:
        """Чем оборачивается раскрытие: у статического источника — сразу показать
        строки, у живого — сперва уйти за ними в сеть.

        `forced` — раскрытие пришло с явной клавиши (Alt+↓), а не автовходом курсора:
        подклассу, которому решать «идти в сеть или тихо свернуться» (см.
        `voice_endpoint.ModelListController._load`), это важно — молчать в ответ на
        явную просьбу нельзя (спека 2026-08-06 §6), а на автовход — можно. База сама
        параметр не использует: у статического источника раскрытие не бывает пустым."""
        self._items = self._read_items()
        self._visible = list(range(len(self._items)))
        self._idx = self._pick_index(field.value.strip())
        self._mount_hint(field)
        self._mount_scroll()
        if filter_text.strip():
            self.apply_filter(filter_text)
        else:
            self._refresh_rows()

    def _reopen(self, field: Input, text: str) -> None:
        self._items = self._read_items()
        self._mount_hint(field)
        self._mount_scroll()
        self.apply_filter(text)

    def _on_close(self) -> None:
        """Хук подкласса: у живого источника закрытие обесценивает летящую загрузку."""

    def reload(self) -> bool:
        """Alt+↓ на поле: раскрыть список явно, ничего не печатая — второй путь
        назад после Esc (спека 2026-08-06 §2, §4.1: роль ушла с Ctrl+Space). Данные
        статического источника перечитывать нечего: уже раскрытый список это не
        трогает (и клавишу не ест — её не на что тратить).
        True — сделали что-то, хозяину есть смысл забрать клавишу себе."""
        if self.is_open:
            return False
        self.open(forced=True)
        return True

    # --- общая механика ---
    def _field(self) -> Input:
        return self._host.query_one(f"#{self._input_id}", Input)

    def _rows(self) -> list:
        """Строки ТЕКУЩЕГО раскрытия: последние N матчей — снесённые ещё в дереве
        (`remove()` у Textual отложенный, канон «последние N» /settings)."""
        if not self._items:
            return []
        return list(self._host.query(f".{self._row_class}"))[
            -len(self._items) :
        ]

    def open(
        self,
        *,
        keep_baseline: bool = False,
        filter_text: str = "",
        forced: bool = False,
    ) -> None:
        """Раскрыть список: значение поля становится «значением до фильтра» (см. close).

        `keep_baseline` — НЕ переписывать его: перечитывание уже раскрытого списка
        (`reload`) видит в поле ФИЛЬТР, и сделать фильтр baseline значит потерять
        прежнее значение — Esc вернул бы остаток фильтра, а доверчивый Enter записал бы
        его в конфиг. `filter_text` — чем сузить список сразу, чтобы после перечитывания
        строки и поле показывали одно и то же."""
        if self.is_open:
            return
        try:
            field = self._field()
        except NoMatches:
            return  # поля в этом наборе нет (мастер) — раскрывать нечего
        if (
            not forced
            and self._can_open is not None
            and not self._can_open(field.value.strip())
        ):
            # `can_open` защищает АВТОраскрытие (вход курсора на поле): раскрыть список
            # поверх чужого значения значит увести туда ↑/↓ и дать Enter молча затереть
            # набранное. На явную клавишу (`forced`) это правило не распространяется —
            # человек попросил список сам (смок 2026-08-04).
            self._sync_hint()  # отказ — тоже состояние: строка обязана назвать Alt+↓
            return
        self.is_open = True
        if not keep_baseline:
            self._value0 = field.value
        self._begin_open(field, filter_text, forced=forced)

    def reopen_for_typing(self, text: str) -> None:
        """Раскрыть список, когда печать застала его свёрнутым (курсор остался в поле
        после Esc — раскрыть было нечем, живой баг). В отличие от `open()`, НЕ трогает
        `_value0`: печать продолжает уже начатую правку, а не открывает новую сессию —
        `_value0` обязан остаться тем, что было в поле ДО неё (иначе Esc после раскрытия
        по печати вернул бы в поле только что напечатанное, а не исходное значение).

        Хозяин зовёт это только когда поле в фокусе — печать/вставка человека, а не
        программная запись (restore/pick/пересборка редактора сами не рождают Changed,
        см. `write_field`)."""
        if self.is_open:
            return
        try:
            field = self._field()
        except NoMatches:
            return
        self.is_open = True
        self._reopen(field, text)

    def _group(self):
        """Группа поля — общая рамка поля и его списка (спека §3.1) — или None: её уже
        снесли вместе с редактором (пересборка полей, уход с экрана)."""
        try:
            return self._host.query_one(f"#{self._input_id}-group")
        except NoMatches:
            return None

    def _anchor(self, field: Input):
        """Анкер монтирования подсказки — последняя СУЩЕСТВУЮЩАЯ (даже отложенно
        снесённая) подсказка этого списка, а не всегда группа: два `mount` подряд, пока
        прошлый close() ещё не снёс свои виджеты, иначе вставили бы новую ПЕРЕД старой,
        и `_hint_widget` («последняя живая») взял бы как раз обречённую."""
        existing = [
            w for w in self._host.children if self._hint_class in w.classes
        ]
        # `or field` — недостижимый хвост, а не мягкая деградация: у сгруппированного
        # поля `field` внук хоста, и mount(after=внук) упал бы MountError. Живого пути
        # сюда нет (при живом поле группа тоже находится), оставлен как явный маркер.
        return existing[-1] if existing else (self._group() or field)

    def _hint_widget(self):
        """Живая строка-подсказка поля или None. Помеченную на удаление (`_pruning`)
        существующей не считаем: она ещё в дереве, но обречена — запись в неё исчезла
        бы вместе с ней (канон `_singleton` /settings)."""
        if self._hint is None:
            return None  # своей строки у списка нет (см. параметр `hint`)
        live = [
            w
            for w in self._host.query(f".{self._hint_class}")
            if not w._pruning
        ]
        return live[-1] if live else None

    def _mount_hint(self, field: Input) -> None:
        """Строка-подсказка ПОД рамкой группы — ровно одна (спека §3.2.3: внутри рамки
        только выбор, всё, что комментирует, — снаружи). Существующую переиспользуем, а
        не сносим и монтируем заново: под свёрнутым списком висит подсказка «чем
        раскрыть», а `remove()` у Textual отложенный — в одном такте рядом оказались бы
        две строки, и раскрытие дописывало бы свою под старой вместо замены."""
        if self._hint is None:
            return
        if self._hint_widget() is None:
            self._host.mount(
                Static("", classes=f"onb-hint onb-dyn {self._hint_class}"),
                after=self._anchor(field),
            )

    def _mount_scroll(self) -> None:
        """Строки — во вложенный скролл с потолком высоты (канон `.dropdown-scroll`):
        строк бывают десятки, и без потолка список раскатывал бы секцию.

        Скролл монтируется ВНУТРЬ группы своего поля: рамка у поля и списка одна
        (спека §3.1), и рисует её группа. В хвост, а не перед прошлым (отложенно
        снесённым) скроллом — «последние N» (`_rows`) иначе взял бы обречённые."""
        group = self._group()
        if group is None:
            return  # группы уже нет — редактор пересобирают
        scroll = VerticalScroll(
            classes=f"dropdown-scroll onb-dyn {self._scroll_class}"
        )
        scroll.can_focus = (
            False  # фокус держит поле — список водят его клавиши
        )
        group.mount(scroll)
        if not scroll.is_attached:
            # Редактор уже сносят (пересборка полей, уход с экрана): `mount` в такой
            # момент тихо ничего не монтирует, и `scroll.mount` ниже упал бы MountError.
            # Ловит ответ живого источника, доехавший к самой разборке.
            return
        # Строки — ОТДЕЛЬНЫМ mount, не через конструктор контейнера: дети конструктора
        # у Textual лежат в `_pending_children` и монтируются лишь на compose-проходе,
        # а раскрытие обязано быть готовым к работе сразу (`_refresh_rows`/`apply_filter`
        # зовут `query` тем же вызовом). `scroll.mount` здесь легален: `mount` выше уже
        # приаттачил контейнер к дереву (регистрация синхронная).
        row_cls = f"onb-option onb-dyn {self._row_class} {self._row_classes}".rstrip()
        scroll.mount(*[ColumnStatic("", classes=row_cls) for _ in self._items])

    def close(
        self, *, restore: bool = True, closed_hint: str | None = None
    ) -> None:
        """Свернуть список. `restore` — вернуть в поле значение, которое было ДО фильтра:
        печать там, где она только фильтрует, не имеет права оставить в поле остаток
        набора («нем») — следующий Enter сохранил бы его (живой смок). Осознанный выбор
        (`pick`) откатывать, наоборот, нельзя; хост, у которого печать — ещё и ввод
        своего значения (адрес), выключает откат целиком (`restore_on_close=False`).

        Фокус не трогаем: курсор либо остался в поле (Esc/Enter), либо его уже увёл
        хозяин ленты."""
        if not self.is_open:
            return
        self.is_open = False
        self._on_close()
        for w in self._host.query(f".{self._scroll_class}"):
            w.remove()  # строки уходят вместе со своим контейнером
        self._sync_hint(
            closed_hint
        )  # подсказка остаётся — меняется только её текст
        if restore and self._restore_on_close:
            try:
                field = self._field()
            except NoMatches:
                return  # поля уже нет (редактор пересобирают) — восстанавливать некуда
            write_field(field, self._value0)

    def _can_promise(self) -> bool:
        """Есть ли что обещать строкой под СВЁРНУТЫМ списком.

        У статического источника это просто «есть ли строки»: звать раскрыть список,
        которого не существует, нельзя. Сетевой список правило переопределяет — его
        `Alt+↓` не показывает уже добытое, а идёт за строками на сервер, и обещание
        осмысленно даже с пустым списком (`voice_endpoint.ModelListController`,
        параметр `promise_empty` — то же исключение для контроллера формы)."""
        return self._promise_empty or bool(self._read_items())

    def sync_hint(self) -> None:
        """Привести строку под полем в согласие с состоянием списка — снаружи.

        Нужен хозяину формы: строка обязана стоять с ПЕРВОГО кадра, а не появляться
        после первого раскрытия. Живой смок 2026-08-20: в панели с тремя полями со
        списком подсказка была под теми, куда успел зайти курсор, и не появлялась под
        остальными — на «Подключении» этого не видно, там поле со списком одно и
        курсор стартует на нём."""
        self._sync_hint()

    def _sync_hint(self, closed_override: str | None = None) -> None:
        """Текст подсказки — функция состояния списка: раскрыт → хинт открытого (с
        учётом наличия подсветки), свёрнут → `closed_hint`. Виджет не сносим.

        Живёт здесь, а не у хозяина: строка принадлежит контроллеру — он её монтирует,
        он же владеет `is_open`. Пока согласование было хозяйским делом, каждый путь
        закрытия обязан был помнить о ремонте, и строка пропадала после Enter и после
        фильтра, сузившего список до нуля."""
        if self._hint is None:
            return  # своей строки у списка нет (см. параметр `hint`)
        if not self.is_open and not self._can_promise():
            # Показывать нечего — и обещать нечего: `closed_hint` звал бы раскрыть
            # список, которого не существует, а форсированное раскрытие дало бы
            # пустую рамку (находка ревью 2026-08-18). Молчим.
            return
        try:
            field = self._field()
        except NoMatches:
            return  # поля уже нет (редактор пересобирают)
        self._mount_hint(field)  # строка одна: существующую переиспользует
        w = self._hint_widget()
        if w is not None:
            text = (
                self._hint_text()
                if self.is_open
                else (closed_override or self._closed_hint)
            )
            w.update(Text(text, style=DEEP_DIM))

    def move(self, delta: int) -> bool:
        """↑/↓ по списку. True — клавиша израсходована списком: хозяин на неё ленту не
        двигает (канон: `is_open` проверяется в `move` первым — курсор не может «утечь»
        с поля, пока список не свёрнут явно).

        Исключение — фильтр без совпадений (в т.ч. список ещё грузится или не
        загрузился): ходить нечем, и забирать клавишу значит «запереть» стрелки без
        всякой обратной связи; лента снова водит курсор сама."""
        if not self.is_open or not self._visible:
            return False
        if self._idx is None:
            self._idx = 0 if delta > 0 else len(self._visible) - 1
        else:
            self._idx = (self._idx + delta) % len(self._visible)
        self._refresh_rows()
        return True

    def pick(self):
        """Значение подсвеченной строки — и свернуть список. None — подставлять нечего
        (список свёрнут либо в нём нет подсвеченной строки): Enter про введённое."""
        if not self.is_open:
            return None
        value = None
        if self._visible and self._idx is not None:
            value = self._value_at(self._visible[self._idx])
        if self._restore_on_close:
            # Enter фиксирует поле: дальше «значение до фильтра» — это ЕГО результат, а не
            # то, с чем вошли. Иначе «вошёл (ru) → Enter (сохранил en) → печать → Esc»
            # вернул бы в поле «ru», и следующий Enter молча затёр бы только что
            # сохранённый язык. Своего значения (value is None) это касается ровно так же —
            # его берут из поля.
            self._value0 = (
                value if value is not None else self._current_value()
            )
        self.close(
            restore=False
        )  # Enter — осознанный выбор, откату не подлежит
        if value is not None and self._on_pick is not None:
            self._on_pick(value)
        return value

    def refresh_rows(self) -> None:
        """Перерисовать строки, ничего не выбирая заново: хозяин сменил то, что рисуется
        В строке (пометка ● списка моделей), а не то, что выбрано."""
        if self.is_open:
            self._refresh_rows()

    def _current_value(self) -> str:
        try:
            return self._field().value
        except NoMatches:
            return self._value0

    def apply_filter(self, text: str) -> None:
        """Печать в поле — живой фильтр по уже смонтированным строкам: прячем/показываем
        через `display`, DOM не пересобираем."""
        if not self.is_open:
            return
        prev = self.selected_item
        q = (text or "").strip().lower()
        self._visible = [
            i for i in range(len(self._items)) if not q or self._match(i, q)
        ]
        self._idx = self._filter_index(prev)
        self._refresh_rows()

    def _refresh_rows(self) -> None:
        visible_set = set(self._visible)
        selected_i = (
            self._visible[self._idx]
            if self._visible and self._idx is not None
            else -1
        )
        selected_w = None
        for i, w in enumerate(self._rows()):
            w.display = i in visible_set
            w.update(self._row_text(i, selected=(i == selected_i)))
            (w.add_class if i == selected_i else w.remove_class)("selected")
            if i == selected_i:
                selected_w = w
        hint = self._hint_widget()
        if hint is not None:
            hint.update(Text(self._hint_text(), style=DEEP_DIM))
        # Подсвеченную строку подводим сами — фокус держит поле, и за курсором списка
        # Textual не следит; ↑/↓ по длинному списку крутят ВНУТРЕННИЙ скролл рамки
        # (scroll_visible идёт к ближайшему скроллящему предку). Подсвеченной строки нет
        # (своё значение / фильтр без совпадений) — подводим ХИНТ: иначе список
        # раскрывается за фолдом, и на невысоком терминале это «клавиши умерли».
        target = selected_w if selected_w is not None else hint
        if target is not None:
            self._host.call_after_refresh(target.scroll_visible, animate=False)
