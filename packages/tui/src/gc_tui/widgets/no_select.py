"""Служебная надпись строки ленты, которой нет в выделении мышью."""

from __future__ import annotations

from textual.widgets import Static


class NoSelectStatic(Static):
    """Надпись вне выделения: `ALLOW_SELECT = False` убирает её из обхода при
    протяжке, `get_selection() -> None` — из `text_select_all` родителя, который
    потомков не фильтрует (`Screen._select_all_in_widget` берёт `widget.query("*")`).
    """

    ALLOW_SELECT = False

    def get_selection(self, selection):
        return None
