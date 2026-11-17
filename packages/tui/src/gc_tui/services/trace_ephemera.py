"""Память эфемерного ленты (спека шага 2 окна ленты, §5.4).

Эфемерное — то, что лента показывает, а модель истории так же не
вернёт: строки мыслей, блоки «!», вывод команд, строки без события
(реплики «не отправлено» из meta, строки ``runtime: …``, копии рамки
плана, строка итога без записи), пометки трейса под строкой-владельцем,
«прервано» под вопросом, — и снимок вида раскрываемых виджетов
(ключ плюс флаг).

Пока ход смонтирован, память держит ссылки на живые виджеты и их место:
позицию модели на момент монтажа (число её элементов — строка встаёт за
последним из них) плюс порядковый номер. «Забыть ход» (`freeze`)
заменяет снятые виджеты их копиями (`respawn` виджета), пометки — ключом
владельца (`anchor_key`); отрисовщик кусков возвращает их на свои места
(`for_turn`, `restore_decorations`). Модуль без Textual: виджеты для
него — непрозрачные объекты.

Блоки «!» живут здесь как единственный реестр: `! clear` удаляет и
снятые, «последние N» считают и их.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gc_tui.services.history_model import HistoryModel

#: Класс эфемерного виджета ленты (его же видит дамп ленты в тестах).
EPHEMERAL_CLASS = "ephemeral"


def anchor_key(widget: object) -> str | None:
    """Ключ владельца пометки, одинаковый у живого виджета и у
    нарисованного заново: строка вызова и вопрос — по call_id, прочее —
    по номеру события. Без номера (реплика ещё не в треде) — None."""
    call_id = getattr(widget, "call_id", None)
    if call_id:
        return f"call:{call_id}"
    no = getattr(widget, "event_no", 0)
    return f"event:{no}" if no else None


@dataclass(eq=False)
class _Entry:
    """Эфемерный виджет: живой или (``frozen``) копия снятого."""

    at: int
    seq: int
    widget: object
    frozen: bool = False


@dataclass(eq=False)
class _Decor:
    """Пометка под владельцем: живым (``owner``) или снятым — по ключу
    (``key``), а владелец без ключа (эфемерный: реплика «не отправлено»)
    — по своей копии (``copy``)."""

    at: int
    body: object
    owner: object = None
    key: str | None = None
    copy: object = None


def _freeze_entry(entry: _Entry, removed, respawn, gone, copies) -> bool:
    """Живой эфемерный виджет при снятии хода: снят с ходом — копией
    (``respawn`` None — ход за окном удержания, не копируется; копия —
    в ``copies`` по id снятого); False — забыть."""
    if entry.widget not in removed:
        return not gone(entry.widget)
    copy = respawn(entry.widget) if respawn is not None else None
    if copy is None:
        return False
    copies[id(entry.widget)] = copy
    entry.widget, entry.frozen = copy, True
    return True


def _freeze_decor(
    decor: _Decor, removed, at: int, kept: bool, gone, copies
) -> bool:
    """Пометка живого владельца при снятии хода: снят — по ключу
    владельца, без ключа — по его копии; False — забыть."""
    owner = decor.owner
    if owner not in removed:
        return not gone(owner)
    decor.owner, decor.key, decor.at = None, anchor_key(owner), at
    if decor.key is None:
        decor.copy = copies.get(id(owner))
    return kept and (decor.key is not None or decor.copy is not None)


class Ephemera:
    """Память эфемерного открытой сессии."""

    def __init__(self) -> None:
        self._entries: list[_Entry] = []
        self._decor: list[_Decor] = []
        self._views: dict[str, tuple[int, bool]] = {}
        self._bang: list[list[_Entry]] = []
        self._bang_open: list[_Entry] | None = None
        self._seq = 0
        # Позиции до неё — за окном удержания: их не запоминаем.
        self._horizon = 0

    def clear(self) -> None:
        """Смена сессии, пересборка ленты."""
        self._entries.clear()
        self._decor.clear()
        self._views.clear()
        self._bang.clear()
        self._bang_open = None
        self._horizon = 0

    # ---- захват ----------------------------------------------------------

    def capture(self, widget: object, *, after: int) -> None:
        """Эфемерный виджет смонтирован за ``after`` элементами модели."""
        self._entries.append(_Entry(after, self._seq, widget))
        self._seq += 1

    def decorate(self, owner: object, body: object, *, at: int) -> None:
        """Пометка ``body`` под живым владельцем."""
        self._decor.append(_Decor(at, body, owner=owner))

    def decorate_key(self, key: str, body: object, *, at: int) -> None:
        """Пометка владельцу, который уже снят с ленты (по ключу)."""
        self._decor.append(_Decor(at, body, key=key))

    def note_view(self, key: str, flag: bool, *, at: int = 0) -> None:
        """Снимок вида раскрываемого виджета ``key``; ``at`` — позиция
        его хода в модели (для окна удержания)."""
        if at >= self._horizon:
            self._views[key] = (at, flag)

    def view(self, key: str) -> bool | None:
        found = self._views.get(key)
        return None if found is None else found[1]

    # ---- блоки «!» -------------------------------------------------------

    def bang_row(self, widget: object) -> None:
        """Строка «!» (уже захваченная) — в открытый блок."""
        entry = next(
            (e for e in reversed(self._entries) if e.widget is widget), None
        )
        if entry is None:
            return
        if self._bang_open is None:
            self._bang_open = []
            self._bang.append(self._bang_open)
        self._bang_open.append(entry)

    def bang_close(self) -> None:
        self._bang_open = None

    def bang_open_widgets(self) -> list:
        return [e.widget for e in self._bang_open or ()]

    def bang_clear(self, count: int | None) -> list:
        """Забыть последние ``count`` блоков «!» (None — все), снятые тоже;
        возвращает их живые виджеты — их снимает экран."""
        self._bang_open = None
        if count is None or count >= len(self._bang):
            blocks, self._bang = self._bang, []
        else:
            blocks = self._bang[-count:]
            del self._bang[-count:]
        gone = {id(e) for block in blocks for e in block}
        self._entries = [e for e in self._entries if id(e) not in gone]
        return [e.widget for b in blocks for e in b if not e.frozen]

    # ---- снятие и возврат ------------------------------------------------

    def freeze(
        self,
        removed: Collection,
        *,
        at: int,
        gone: Callable[[object], bool],
        respawn: Callable[[object], object | None],
    ) -> None:
        """Ход снят: его эфемерные виджеты (``removed``) — копиями,
        пометки — ключом владельца, ``at`` — позиция хода в модели.
        Виджеты, которые экран снял сам (``gone``: замена записью,
        откат, `! clear`), память забывает."""
        kept = at >= self._horizon
        copy = respawn if kept else None
        copies: dict[int, object] = {}
        self._drop_entries(
            {
                id(e)
                for e in self._entries
                if not e.frozen
                and not _freeze_entry(e, removed, copy, gone, copies)
            }
        )
        self._decor = [
            d
            for d in self._decor
            if d.owner is None
            or _freeze_decor(d, removed, at, kept, gone, copies)
        ]

    def for_turn(self, model: HistoryModel, turn: int) -> list:
        """Снятое эфемерное хода ``turn`` по порядку — ``(позиция,
        виджет)``; вызывающий монтирует его, память снова держит живые
        виджеты. Позиция — число элементов модели перед строкой: строка
        хода встаёт за его элементом ``позиция - 1``."""
        starts = model.turn_starts
        out = []
        for e in self._entries:
            if not e.frozen or e.at <= 0:
                continue
            if bisect_right(starts, e.at - 1) - 1 != turn:
                continue
            e.frozen = False
            out.append(e)
        out.sort(key=lambda e: (e.at, e.seq))
        return [(e.at, e.widget) for e in out]

    def restore_decorations(self, key: str | None, owner: object) -> list:
        """Пометки снятого владельца ``key`` (или того, чья копия —
        ``owner``) — теперь под ``owner``."""
        bodies = []
        for d in self._decor:
            if d.owner is not None:
                continue
            if (key is not None and d.key == key) or (
                d.copy is not None and d.copy is owner
            ):
                d.owner, d.key, d.copy = owner, None, None
                bodies.append(d.body)
        return bodies

    # ---- окно удержания и сброс ------------------------------------------

    def drop_before(self, at: int) -> None:
        """Ходы до позиции ``at`` ушли за окно удержания: вернутся как
        после переоткрытия — их снятое и снимок вида забыты, смонтированное
        при снятии не запоминается."""
        self._horizon = max(self._horizon, at)
        self._drop_entries(
            {id(e) for e in self._entries if e.at < at and e.frozen}
        )
        self._decor = [d for d in self._decor if d.at >= at]
        self._views = {k: v for k, v in self._views.items() if v[0] >= at}

    def _drop_entries(self, drop: set[int]) -> None:
        if not drop:
            return
        self._entries = [e for e in self._entries if id(e) not in drop]
        for block in self._bang:
            block[:] = [e for e in block if id(e) not in drop]
        self._bang = [b for b in self._bang if b or b is self._bang_open]
