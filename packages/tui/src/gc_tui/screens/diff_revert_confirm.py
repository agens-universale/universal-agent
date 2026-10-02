"""Полноэкранное подтверждение отката: показать то, что исчезнет.

Модальный `ConfirmScreen` шириной 56 дифф не вмещает, а откат без превью — это
«поверьте на слово»: пользователь стирает СВОИ файлы и обязан видеть, что
именно уходит. Поэтому подтверждение занимает весь экран и говорит тем же
языком, что список и вьюер: тело рисует движок `render_hunks`, служебные
строки — `line-meta`, предупреждение — плашка блока (`tree-warn`).

Превью — ПРЯМОЙ дифф «точная база → диск»: не «как файл менялся», а «что в нём
пропадёт». Считает его `EditsView.revert_delta` — тот же снимок, которым откат
пишет; счётная база шелл-канала сюда не доезжает даже числами.

**Обрезок нет вовсе (Р22): все файлы набора и весь дифф каждого.** Экран
отдельный и прокручиваемый, а единственная его работа — честно показать, что
исчезнет; хвост «… и ещё N строк» под шапкой «будет отменено +221» эту работу
подрывал. Тело при этом собирается УЧАСТКАМИ и ЛЕНИВО (`split_body` + `LazyBody`,
тот же механизм, что у вьюера): место под каждый участок резервируется сразу —
полоса прокрутки честна, — а рисуются те, что попали в кадр (Р23). Показ
целиком от этого не страдает: до любого места доезжает прокрутка, а вот
открытие перестаёт стоить всего набора.

Исключение одно — набор больше `FULL_PREVIEW_MAX_FILES`: ленивое тело всё
равно держит виджет на каждый участок, и десять тысяч файлов открывались
секундами. Там тела диффов опускаются, а всё остальное остаётся — каждый файл
поимённо, его цель, предупреждения и пояснения (`_compact_revert_lines`,
`_compact_reset_lines`). Набор по-прежнему не режется.

**Курсора и выделения здесь нет — это документ (Е3):** прочитал, подтвердил.
Действия на отдельный файл на экране не существует, и курсор рождал бы
ожидание «Enter для выделенного», хотя `Enter` всегда подтверждал ВЕСЬ набор.
Поэтому `n`/`p` ушли из клавиш и из футера, ходить по телу осталось прокрутке
(`↑↓`, `PgUp`/`PgDn`), а объём набора переехал на само действие — как надпись
кнопки диалога: `Enter откатить 2 файла` (`confirm_footer`). Строка имени
файла несёт его счётчики (`calc.py · +185 −0`): при откате всего набора виден
вклад каждого файла в итог шапки.

**Тело — дерево блоков (Е3):** блок на файл, между блоками одна пустая строка,
внутри блока зазоров нет. Имя файла со счётчиками — голова блока, под ней цель
отката, предупреждения и сам дифф; у набора из одного файла головы нет — имя
уже в шапке. Пропущенные откатом пути (`skipped`, достоверной базы нет) —
такие же блоки с одной строкой причины (`SKIPPED_LINE`) вместо диффа: сводная
строка над телом называла их числом, а не именами.

Цель отката (задача 13, §4.4/§13 спеки) — первая строка блока: у безымянного
это «вернёт к <ступень>», у названного — стрелка `→ <ступень>`
(`revert_target_line`/`revert_target_arrow` — словарь ступеней и падежи
собирает `tui.screens.diff`, здесь только рендер). Свои `⚠`-строки
(`RevertTarget.warns`) идут следом тем же приёмом, что и `WARN_EXTERNAL`, —
плашка `tree-warn` внутри блока СВОЕГО файла; непустыми бывают только у
источника «Ход» (§4.4).

Результат отдаётся тем же протоколом, что у `ConfirmScreen`: `dismiss(bool)`,
вызов — `push_screen(RevertConfirmScreen(...), callback)`.

**Сброс принятия (`u`/`Ctrl+U`) занял этот же экран (задача 4 доп. волны 4
смоука, С7).** По опасности он сопоставим с откатом (теряет точку возврата
к принятому), а старое лёгкое подтверждение (общий `ConfirmScreen`) не
называло ни файл, ни то, откуда после сброса пойдёт счёт. Второй, почти
такой же экран ради этой разницы заводить не стали: `reset=True`
(конструктор — фабрика `for_reset`) переключает шапку (`build_reset_title`)
и слово действия в футере (`RESET_VERB`) на словарь сброса и подменяет тело —
диффа у сброса нет вовсе (диск не меняется), полотно несёт цену и цель
(`reset_loss_line`/`reset_target_line` на `ResetTarget` из
`EditsView.reset_targets`, тот же приём и та же граница сервис/UI, что и у
цели отката). Каркас (шапка/футер/resize/`LazyBody`) общий.
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from gc_core.agent_edits.deltas import FileDelta
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from gc_tui.palette import ACCENT, WARN
from gc_tui.screens.diff import (
    BODY_UNBUILT,
    CHROME_PADDING,
    FALLBACK_WIDTH,
    NOT_REVERTABLE,
    SCROLLBAR_COLUMN,
    TRACE_FRAME,
    VIEWER_NO_PREVIEW,
    build_reset_title,
    build_revert_title,
    meta_line,
    paint_counters,
    reset_loss_line,
    reset_target_line,
    revert_target_arrow,
    revert_target_line,
    revert_target_warnings,
    split_body,
)
from gc_tui.services.edits_view import ResetTarget, RevertTarget
from gc_tui.services.plural import files_word
from gc_tui.widgets.lazy_body import BodySlot, LazyBody
from gc_tui.widgets.no_select import NoSelectStatic

# Константы вместо повторяющихся литералов (Sonar S1192).
_SEL_TRACE = "#trace"

# Отступ тела слева (`RevertConfirmScreen #trace > Static` в `styles.tcss`) —
# ровно тот же воздух, что кромка курсора даёт участкам вьюера. Строки имён и
# вложенные строки дерева набирают его паддингом своих классов, участки диффа —
# правилом выше; левый край дерева тот же, поэтому расчёт заливки его не
# различает.
BODY_INDENT = 2

# Признак поднимают два независимых наблюдения: разрыв before-цепочки в журнале
# (о правке МЕЖДУ правками агента узнаём из следующей записи) и содержимое
# диска, которого мы не писали, — второе видно сразу, без следующей правки
# (смок 2026-09-10). Текст говорит ровно о том, что известно обоим.
WARN_EXTERNAL = (
    "После правки агента файл менялся извне — откат отменит и эти изменения."
)
# База и диск совпали: откат законен (снимок есть), но отменять в файле нечего.
# Пустое тело под шапкой «будет отменено» читалось бы как «дифф не собрался».
PREVIEW_NOTHING = "отменять нечего: файл совпадает с базой"
# Причина пропуска — строкой в блоке САМОГО файла (Е3): сводное «Пропущено: N
# файлов» стояло над телом и не называло никого поимённо. Причина одна на всех
# пропущенных подтверждения: сюда попадают только пути без достоверной базы,
# остальные отказы случаются уже при исполнении и живут в итоговой плашке.
SKIPPED_LINE = f"пропущен: {NOT_REVERTABLE}"
# Порог полного подтверждения. До него — дифф каждого файла (Р22); выше —
# компактный вид: итог, каждый файл поимённо со счётчиками и целью, его
# предупреждения, — плотным текстом. Ленивое тело держит виджет на каждый
# участок, и полный вид на больших наборах не открывается: 2000 файлов — 2,8 с
# открытия и ~1,5 с на PgDn, 10 000 — 15 с и 5 с (смок 2026-09-24). Набор при
# этом НЕ режется: каждый путь назван, дифф любого открывается на экране
# изменений; прочитать тысячи диффов подряд подтверждение всё равно не даёт.
FULL_PREVIEW_MAX_FILES = 300
COMPACT_NOTE = (
    "Файлов больше {limit} — дифф каждого здесь не показан; "
    "любой файл можно открыть на экране изменений."
)
# Строк компактного списка на один виджет: несколько виджетов вместо тысяч, и
# ни один не настолько велик, чтобы его отрисовка стала заметной.
_COMPACT_CHUNK_LINES = 500
_CONFIRM_SCROLL = "↑↓ PgUp PgDn"
_CONFIRM_CANCEL = "Esc отмена"
CONFIRM_VERB = "откатить"
# Слово действия у сброса своё (задача 4 доп., С7): сброс не пишет на диск, и
# «откатить» было бы неправдой.
RESET_VERB = "сбросить"


def confirm_action(count: int, *, reset: bool = False) -> str:
    """`Enter откатить 2 файла` — объём на самом действии, как кнопка диалога (Е3)."""
    verb = RESET_VERB if reset else CONFIRM_VERB
    return f"Enter {verb} {count} {files_word(count)}"


def confirm_footer(
    *, count: int, can_scroll: bool = True, reset: bool = False
) -> str:
    """Футер подтверждения; мёртвые клавиши не рекламируются — как везде.

    Курсора по файлам нет (Е3): `n`/`p` ушли, остались прокрутка (только когда
    тело не влезает — замер `max_scroll_y`), действие с числом и отмена.
    """
    parts = [confirm_action(count, reset=reset)]
    if can_scroll:
        parts.append(_CONFIRM_SCROLL)
    parts.append(_CONFIRM_CANCEL)
    return " · ".join(parts)


# Полная форма полосы на одном файле — все сегменты живы. Собрана тем же
# `confirm_footer`: это КАНОН словаря обоих экранов, на который ссылаются
# стражи, а не второй его источник.
CONFIRM_FOOTER = confirm_footer(count=1)
RESET_CONFIRM_FOOTER = confirm_footer(count=1, reset=True)


def file_caption(
    path: str, add: int, del_: int, *, countable: bool = True
) -> str:
    """Строка имени блока в дереве: имя и вклад файла в итог шапки (Р22).

    Обрамления тире нет (Е3): блок опознаётся отступом вложенных строк, а
    рамка из тире делала из документа таблицу.

    Нули несчётного пути (бинарь, файл сверх капа) не показываются — по тому же
    правилу, что в шапке: `+0 −0` прочиталось бы как «отменяется ноль строк»,
    хотя откат вернёт целый файл.
    """
    counters = f" · +{add} −{del_}" if countable else ""
    return f"{path}{counters}"


def _compact_slots(lines: Sequence[Text]) -> List[BodySlot]:
    """Пояснение и список компактного подтверждения — несколькими виджетами.

    Список режется на куски по `_COMPACT_CHUNK_LINES`: виджет на строку вернул
    бы ту же стену, от которой компактный вид и уходит.
    """
    slots = [
        BodySlot(
            meta_line(
                COMPACT_NOTE.format(limit=FULL_PREVIEW_MAX_FILES), "tree-line"
            )
        )
    ]
    for start in range(0, len(lines), _COMPACT_CHUNK_LINES):
        chunk = Text("\n").join(lines[start : start + _COMPACT_CHUNK_LINES])
        slots.append(BodySlot(meta_line(chunk, "tree-line")))
    return slots


def _accent_text(text: str) -> Text:
    """Строка цели отката акцентным цветом — тот же токен, которым экраны
    правок метят главное в строке."""
    result = Text()
    result.append(text, style=ACCENT)
    return result


class RevertConfirmScreen(Screen[bool]):
    """Подтверждение отката: шапка, предупреждения, превью отменяемого, футер.

    `preview` — `[(путь, дельта)]` из того же расчёта, которым откат пишет;
    `countable=False` означает «сравнить было нечем» (бинарь, файл сверх капа):
    такой путь откатывается штатно — право даёт точный снимок, а не наличие
    диффа, — но показать в нём нечего.

    `targets` (задача 13, §4.4/§13 спеки) — цель отката на путь, тот же расчёт,
    что исполнит `r`/`Ctrl+R`: у безымянного блока становится строкой
    «вернёт к …» перед превью, у названного — стрелкой под строкой имени. Путь
    без цели в словаре (не должно случаться при вызове из `_ask_revert` —
    превью там строится только для откатываемых) просто не получает ни строки,
    ни стрелки.

    `skipped` — пути набора, которым откат не подлежит (достоверной базы нет):
    они становятся блоками с одной строкой причины и в счёт шапки и действия
    не входят — откатывать их нечем. `external` — пути, менявшиеся извне после
    правки агента: предупреждение о них живёт в блоке своего файла. Оба
    приходят ПУТЯМИ: строки собирает это место, и собирает их там, где видно,
    к кому они относятся.

    Показывается ВСЁ: все файлы набора и весь дифф каждого (Р22). Ограничивать
    здесь нечего — экран прокручиваемый, а обрезка отняла бы у него
    единственную работу. Выше `FULL_PREVIEW_MAX_FILES` файлов опускаются
    только тела диффов (компактный вид), файлы и их пояснения — нет.

    Шапка приходит СОСТАВНЫМИ частями, а не готовой строкой: она зависит от
    ширины (длинный путь режется слева), и на смену ширины её обязано
    пересобирать это же место — иначе счётчики уезжают из кадра, а гейт
    запрещает жертвовать ими когда бы то ни было.

    `reset` (задача 4 доп. волны 4 смоука, С7) — тот же экран для подтверждения
    сброса принятия (`u`/`Ctrl+U`): те же клавиши, тот же жанр показа, но
    диффа не бывает вовсе (сброс не пишет на диск) — тело несёт цену и цель
    (`reset_targets`: `ResetTarget` на путь), собранные ПО `paths`, а не по
    `preview` (у сброса он всегда пуст). Строить второй почти такой же экран
    ради этой разницы значило бы дублировать шапку, футер, resize и
    `LazyBody` — здесь меняется только словарь и то, что попадает в полотно;
    используйте фабрику `for_reset`, а не голый конструктор с `reset=True`.
    """

    # priority: Enter/Esc решают судьбу действия и обязаны срабатывать раньше
    # прокрутки `#trace`, которая забирает фокус на старте.
    BINDINGS = [
        Binding("enter", "confirm", "подтвердить", show=False, priority=True),
        Binding("escape", "cancel", "отмена", show=False, priority=True),
    ]

    def __init__(
        self,
        *,
        paths: Sequence[str],
        add: int,
        del_: int,
        exact: bool,
        skipped: Sequence[str] = (),
        external: Sequence[str] = (),
        preview: Sequence[Tuple[str, FileDelta]] = (),
        targets: Sequence[RevertTarget] = (),
        reset_targets: Sequence[ResetTarget] = (),
        reset: bool = False,
        mass: bool = False,
    ) -> None:
        super().__init__()
        self._paths = list(paths)
        self._add = add
        self._del = del_
        self._exact = exact
        # Пропущенные (без достоверной базы) и правленные извне — ПУТЯМИ, а не
        # готовыми строками: строку каждого собирает тело, и собирает её В
        # БЛОКЕ своего файла (Е3). Общий список предупреждений над телом не
        # называл, к кому относится, и в наборе из нескольких файлов это был
        # вопрос без ответа.
        self._skipped = list(skipped)
        self._external = set(external)
        self._preview = list(preview)
        # По пути — цель отката (задача 13): словарь, а не параллельный
        # `targets`-список, — порядок `preview`/`targets` у вызывающего может
        # разойтись, а путь есть у обоих.
        self._targets_by_path = {t.path: t for t in targets}
        # Сброс несёт СВОЙ расчёт (`ResetTarget`, Д5): к цели там добавлены
        # цена (теряется ли точка отката) и час принятия, а откату эти поля
        # чужие — двух пород в одном словаре не держим.
        self._reset_by_path = {t.path: t for t in reset_targets}
        self._reset = reset
        self._mass = mass
        self._fill = BODY_UNBUILT
        # Объём набора для действия в футере (Е3): `Enter` подтверждает ВЕСЬ
        # набор, и число обязано совпасть с числом шапки. У сброса это все
        # пути, у отката — только те, у кого есть превью: пропущенные откату
        # не подлежат и в счёт не входят.
        self._count = len(self._paths) if reset else len(self._preview)

    @classmethod
    def for_reset(
        cls,
        *,
        paths: Sequence[str],
        targets: Sequence[ResetTarget],
        mass: bool,
    ) -> RevertConfirmScreen:
        """Подтверждение сброса (`u`/`Ctrl+U`, задача 4 доп. — С7): без диффа
        — сброс не пишет на диск, отменять в строках нечего, — поэтому
        `add`/`del_`/`exact`/`preview` и пропущенные откатом здесь не
        участвуют.

        `paths` — ВЕСЬ набор, у которого снимается принятие, а не одни только
        теряющие точку отката (Д5): тело называет каждого поимённо, шапка
        считает всех.
        """
        return cls(
            paths=paths,
            add=0,
            del_=0,
            exact=True,
            reset_targets=targets,
            reset=True,
            mass=mass,
        )

    # ---- каркас --------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            with Horizontal(id="header"):
                yield Static("", id="header-left", markup=False)
            yield LazyBody(id="trace")
            yield Static("", id="footer", markup=False)

    def on_mount(self) -> None:
        """Шапка и футер сразу, тело — первым же `Resize` (он придёт тем же тактом).

        Собирать тело здесь рано: раскладка ещё не назвала `#trace` ширину, а
        собранное вслепую пришлось бы собирать заново — двойная цена открытия
        на большом наборе (замер — во вьюере, там же причина).
        """
        self._render_chrome()

    def on_resize(self, event: events.Resize) -> None:
        """Ширина сменилась — пересобираются и шапка, и заливка строк тела.

        Ширина берётся ИЗ СОБЫТИЯ: экран получает `Resize` раньше, чем `#trace`
        примет новый размер, и замер контейнера отдал бы предыдущую ширину.
        Сверка по ширине заливки отсекает `Resize` на смену высоты — от неё не
        зависит ни тело, ни шапка. ПОЛОСА зависит: от высоты кадра зависит,
        живая ли прокрутка (Д5), — и её строка пересобирается и на такой
        `Resize` тоже.
        """
        if self._fill_width(event.size.width) == self._fill:
            self.call_after_refresh(self._render_footer)
            return
        self._render_screen(screen_width=event.size.width)

    def _header_width(self, screen_width: Optional[int] = None) -> int:
        width = (screen_width or self.size.width) - CHROME_PADDING
        return width if width > 0 else FALLBACK_WIDTH

    def _fill_width(self, screen_width: Optional[int] = None) -> Optional[int]:
        """Ширина заливки строки: тело `#trace` минус скроллбар и отступ слева.

        Разница между экраном и телом — рамка `#trace` (Р16), паддинга у тела в
        рамке нет; замером рамку вычитать не надо — `size` уже content-region.
        Скроллбар резервируется всегда: `size.width` его колонку не вычитает, а
        у превью отката он есть почти наверняка. Отступ слева (`BODY_INDENT`,
        правило в `styles.tcss`) — тот же воздух, что у участков вьюера: без
        него превью прилипало к рамке, а без вычитания заливка вылезла бы под
        скроллбар.
        """
        if screen_width is None:
            try:
                width = self.query_one(_SEL_TRACE, VerticalScroll).size.width
            except Exception:
                width = 0
        else:
            width = screen_width - TRACE_FRAME
        width -= SCROLLBAR_COLUMN + BODY_INDENT
        return width if width > 0 else None

    # ---- рендер --------------------------------------------------------
    # Имя `_render` занято Textual (`Widget._render`) — свой метод с таким
    # именем гасит отрисовку экрана целиком.

    def _render_screen(self, *, screen_width: Optional[int] = None) -> None:
        """Перерисовать экран. `screen_width` — ширина из события `Resize`.

        Тело — ПЕРВЫМ: футер говорит про прокрутку тела, и при обратном
        порядке её сегмент отставал бы на такт.

        Тело собирает первый же `Resize` (см. `on_mount`), а не монтаж экрана.
        """
        self._render_body(self._fill_width(screen_width))
        self._render_chrome(screen_width)
        # Полоса знает про прокрутку только ПОСЛЕ раскладки: до неё у полотна
        # нет ни виртуальной высоты, ни кадра, и `max_scroll_y` отдал бы ноль
        # на любом теле. Тот же приём, которым `LazyBody` уточняет замером
        # окно сборки, — и та же цена: один лишний рендер строки.
        self.call_after_refresh(self._render_footer)

    def _render_chrome(self, screen_width: Optional[int] = None) -> None:
        width = self._header_width(screen_width)
        title = (
            build_reset_title(self._paths, mass=self._mass, width=width)
            if self._reset
            else build_revert_title(
                self._paths,
                add=self._add,
                del_=self._del,
                exact=self._exact,
                width=width,
            )
        )
        self.query_one("#header-left", Static).update(paint_counters(title))
        self._render_footer()

    def _render_footer(self) -> None:
        """Полоса подсказок: действие с объёмом набора и живая прокрутка.

        Прокрутка спрашивается у самого полотна (`max_scroll_y`), а не
        считается по строкам: высоту служебных строк задаёт CSS (отступы,
        перенос длинного пути), и вторая арифметика того же разъехалась бы с
        первой на первом же узком терминале.
        """
        self.query_one("#footer", Static).update(
            confirm_footer(
                count=self._count,
                can_scroll=self.query_one(_SEL_TRACE, LazyBody).max_scroll_y
                > 0,
                reset=self._reset,
            )
        )

    def _render_body(self, fill: Optional[int]) -> None:
        trace = self.query_one(_SEL_TRACE, LazyBody)
        self._fill = fill
        trace.set_slots(self._body_slots(fill))

    def _body_slots(self, fill: Optional[int]) -> List[BodySlot]:
        """Полотно: у отката — дерево блоков с превью, у сброса своё.

        Сброс (`self._reset`, задача 4 доп.) в тело отката не заходит вовсе —
        у него нет ни диффа, ни предупреждений отката, только цена и цель.
        """
        if self._reset:
            return self._reset_slots()
        return self._revert_slots(fill)

    def _revert_slots(self, fill: Optional[int]) -> List[BodySlot]:
        """Тело отката — дерево блоков (Е3): имя файла со счётчиками, под ним
        цель, предупреждения и хэнки; пропущенные (без достоверной базы) —
        свои блоки с одной тусклой строкой причины, в счётчики шапки не входят.
        Одиночный `r` имя не повторяет (оно в шапке). Между блоками — одна
        пустая строка.

        Пустая строка — виджет `tree-gap`, а не отступ строки: строки блока
        носят классы дерева, и те гасят `margin-top` у `.line-meta` своим
        shorthand'ом (ловушка 2). Иначе выходило ровно наоборот — имя файла
        прилипало к чужому диффу и отрывалось от своей же цели.

        Тело файла режется на участки тем же `split_body`, что у вьюера: ширина
        колонки номеров, порог подсветки синтаксиса и внутристрочные акценты
        считаются движком по всему файлу разом, и режется уже готовый
        `rich.Text`. Куски здесь РОВНЫЕ (`by_edits=False`): выравнивать их на
        правки незачем — курсора по правкам тут нет, — а участок на каждую
        правку дал бы сотни виджетов на файле с частыми мелкими правками.

        Участки уходят в полотно МЕСТАМИ (Р23): высота каждого известна из
        разбора входа, а движок считает тело файла, лишь когда до него доехал
        кадр. Набор из дюжины файлов поэтому открывается за цену первого, а не
        всех сразу.

        Цель отката (задача 13) формой зависит от того, названо ли имя:
        безымянный блок получает «вернёт к <ступень>», названный — стрелку
        `→ <ступень>` сразу под именем. Свои `⚠`-строки (`RevertTarget.warns`)
        и `WARN_EXTERNAL` идут следом предупреждениями блока.
        """
        if len(self._preview) + len(self._skipped) > FULL_PREVIEW_MAX_FILES:
            return self._compact_revert_slots()
        slots: List[BodySlot] = []
        # Имена нужны, как только блоков больше одного: пропущенные — тоже
        # блоки, и безымянный блок рядом с названным читался бы их частью.
        named = len(self._preview) > 1 or bool(self._skipped)
        first = True

        def gap() -> None:
            nonlocal first
            if not first:
                slots.append(BodySlot(meta_line("", "tree-gap")))
            first = False

        for path, delta in self._preview:
            gap()
            if named:
                slots.append(
                    BodySlot(
                        meta_line(
                            paint_counters(
                                file_caption(
                                    path,
                                    delta.add,
                                    delta.del_,
                                    countable=delta.countable,
                                )
                            ),
                            "tree-head",
                        )
                    )
                )
            target = self._targets_by_path.get(path)
            if target is not None:
                line = (
                    revert_target_arrow(target)
                    if named
                    else revert_target_line(target)
                )
                slots.append(
                    BodySlot(meta_line(_accent_text(line), "tree-line"))
                )
                slots.extend(
                    BodySlot(
                        NoSelectStatic(warn, classes="tree-warn", markup=False)
                    )
                    for warn in revert_target_warnings(target)
                )
            if path in self._external:
                slots.append(
                    BodySlot(
                        NoSelectStatic(
                            WARN_EXTERNAL, classes="tree-warn", markup=False
                        )
                    )
                )
            if not delta.countable:
                slots.append(
                    BodySlot(meta_line(VIEWER_NO_PREVIEW, "tree-line"))
                )
                continue
            parts = split_body(
                delta.hunks, path=path, fill_width=fill, by_edits=False
            )
            if not parts:
                slots.append(BodySlot(meta_line(PREVIEW_NOTHING, "tree-line")))
                continue
            slots.extend(
                BodySlot(
                    Static("", classes="diff-hunk", markup=False),
                    part.lines,
                    part.render,
                )
                for part in parts
            )
        for path in self._skipped:
            gap()
            slots.append(BodySlot(meta_line(path, "tree-head")))
            slots.append(BodySlot(meta_line(SKIPPED_LINE, "tree-line")))
        return slots

    def _compact_revert_slots(self) -> List[BodySlot]:
        return _compact_slots(self._compact_revert_lines())

    def _compact_revert_lines(self) -> List[Text]:
        """Тело отката большого набора — блок на файл, без тел диффов.

        Форма общая с компактным сбросом: строка файла — имя со счётчиками и
        цель стрелкой; под ней с отступом всё, что полный вид говорит о файле
        кроме самого диффа, в том же порядке — предупреждения (ступени,
        правки извне) цветом предупреждения, пояснения («превью недоступно»,
        «отменять нечего») тусклым. Пропущенные — именем и причиной под ним.
        Всё поимённо: набор не режется, опускаются только тела.
        """
        lines: List[Text] = []
        for path, delta in self._preview:
            line = paint_counters(
                file_caption(
                    path, delta.add, delta.del_, countable=delta.countable
                )
            )
            target = self._targets_by_path.get(path)
            warns: List[str] = []
            if target is not None:
                line.append("  ")
                line.append_text(_accent_text(revert_target_arrow(target)))
                warns.extend(revert_target_warnings(target))
            if path in self._external:
                warns.append(WARN_EXTERNAL)
            lines.append(line)
            lines.extend(Text(f"  {warn}", style=WARN) for warn in warns)
            if not delta.countable:
                lines.append(Text(f"  {VIEWER_NO_PREVIEW}"))
            elif not delta.hunks.strip():
                lines.append(Text(f"  {PREVIEW_NOTHING}"))
        for path in self._skipped:
            lines.append(Text(path))
            lines.append(Text(f"  {SKIPPED_LINE}"))
        return lines

    def _compact_reset_slots(self) -> List[BodySlot]:
        return _compact_slots(self._compact_reset_lines())

    def _compact_reset_lines(self) -> List[Text]:
        """Тело сброса большого набора — той же формы, что компактный откат.

        Строка файла — имя и откуда пойдёт счёт; под ней с отступом цена:
        потеря точки отката — цветом предупреждения, «терять нечего» —
        тусклым, как в полном виде.
        """
        lines: List[Text] = []
        for path in self._paths:
            target = self._reset_by_path.get(path)
            line = Text(path)
            if target is None:
                lines.append(line)
                continue
            line.append("  ")
            line.append_text(_accent_text(reset_target_line(target.target)))
            lines.append(line)
            lines.append(
                Text(
                    f"  {reset_loss_line(target)}",
                    style=WARN if target.loses_point else "",
                )
            )
        return lines

    def _reset_slots(self) -> List[BodySlot]:
        """Тело сброса — дерево блоков (Е3): имя файла и под ним две строки —
        что теряется и откуда пойдёт счёт. Одиночный `u` имя не повторяет (оно
        в шапке), блок начинается сразу со строк. Между блоками — одна пустая
        строка (`tree-gap`), других зазоров нет: `.line-meta` носит `margin-top`,
        и классы дерева его гасят своим shorthand (ловушка 2).

        Порядок строк внутри блока — сперва цена (`reset_loss_line`), потом
        последствие (`reset_target_line`). Потеря — предупреждением
        (`tree-warn`, тот же приём, что у `⚠`-строк отката); «терять нечего» —
        обычная вложенная строка (тот же жанр, что `PREVIEW_NOTHING`),
        предупреждение на ней читалось бы как тревога на ровном месте.

        `Ctrl+U` (`self._mass=True`) называет файлы ВСЕГДА, даже на одном —
        тот же гейт, что у шапки: раз шапка не назвала файл по имени, тело
        обязано.
        """
        if len(self._paths) > FULL_PREVIEW_MAX_FILES:
            return self._compact_reset_slots()
        slots: List[BodySlot] = []
        for index, path in enumerate(self._paths):
            target = self._reset_by_path.get(path)
            if index:
                slots.append(BodySlot(meta_line("", "tree-gap")))
            if self._mass:
                slots.append(BodySlot(meta_line(path, "tree-head")))
            if target is None:
                continue
            loss = reset_loss_line(target)
            slots.append(
                BodySlot(
                    NoSelectStatic(loss, classes="tree-warn", markup=False)
                    if target.loses_point
                    else meta_line(loss, "tree-line")
                )
            )
            slots.append(
                BodySlot(
                    meta_line(
                        _accent_text(reset_target_line(target.target)),
                        "tree-sub",
                    )
                )
            )
        return slots

    # ---- решение -------------------------------------------------------

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)
