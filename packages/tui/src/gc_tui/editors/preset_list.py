"""Пресет-лист адресов провайдеров (спека умных форм §3.1) — общий
`FieldListController` со своими параметрами: строки name/url монтируются внутрь
рамки-группы поля, подсказка — под неё (единый язык форм 2026-08-03 §3.1: рамка
одна, подсказка снаружи). Данные локальные (объекты с .name/.url, напр.
EndpointPreset) — без воркеров/fetch. Хозяева — ConnectionEditor и VoiceEndpointEditor
(поле «URL» голосовой панели, спека 2026-08-05 §3)."""

from __future__ import annotations

from rich.text import Text

from gc_tui.editors.field_list import LIST_CLOSED_HINT, FieldListController
from gc_tui.palette import ACCENT, DEEP_DIM

# Канон-хинт пресет-листа адреса (спека §2.1, дословно).
PRESET_HINT = "провайдеры · печать — фильтр/свой адрес · Enter — подставить · Esc — свернуть"
# Список раскрыт, но НИЧЕГО НЕ ПОДСВЕЧЕНО — живой случай ровно один: `Alt+↓` поверх
# своего, не каталожного адреса (строки видны все, `initial_index` вернул None). Тогда
# `pick()` вернёт None и просто свернёт список, оставив набранное; обещать «Enter —
# подставить» значило бы врать на самом видном месте, под полем адреса.
# Печать сюда больше не приводит: набранное без совпадений сворачивает список
# (`connection.PRESET_NO_MATCH_CLOSED`) — канон в редакции 2026-08-18.
# Глагол — тот же, что в футере шага (`onboarding._FOOTER_PRESET_LIST_NO_MATCH`): обе
# строки видны одновременно, и звать одну клавишу двумя словами нельзя.
PRESET_HINT_NO_MATCH = (
    "в списке такого нет · Enter — оставить свой адрес · Esc — свернуть"
)


def preset_list_controller(
    host,
    *,
    presets: list,
    row_class: str,
    input_id: str,
    hint: str,
    on_pick=None,
    no_match_hint: str = PRESET_HINT_NO_MATCH,
    closed_hint: str = LIST_CLOSED_HINT,
) -> FieldListController:
    """Контроллер пресет-листа адреса под полем `#{input_id}`.

    Параметры-различия против соседних списков формы:

    * `can_open` — самораскрытие, когда курсор ПРИШЁЛ в поле (за печать отвечает
      `ConnectionEditor.on_input_changed` через `reopen_for_typing`, минуя этот
      предикат). Канон 2026-08-17 в редакции 2026-08-18: список раскрыт, пока
      значение складывается и есть из чего выбирать. Пусто — раскрываем: выбор
      сейчас единственное занятие, стрелки списка. Есть значение — любое, из
      каталога или своё — НЕ раскрываем: стрелки водят по форме, а способ вызвать
      список назван постоянной строкой под полем («Alt+↓ — показать список»).
      Раньше пресетное значение
      раскрывало список, и поведение экрана зависело от того, попал ли адрес
      пользователя в наш каталог: убрали провайдера — та же форма с тем же адресом
      повела себя иначе. Плюс раскрытый список забирал ↑/↓ насовсем — до поля токена
      было не дойти без Esc. Пустой каталог не раскрываем никогда: рамка без единой
      строки только заперла бы ↑/↓ формы.
      Подсветка совпавшего пресета (`initial_index`) при этом жива — она работает на
      форсированном раскрытии (`Alt+↓`), где список нужен поверх готового значения.
    * `restore_on_close=False` — печать в поле адреса это не только фильтр, но и ввод
      СВОЕГО адреса, и откатывать её по Esc нельзя (у языка и моделей — наоборот).
    * `value_of` по умолчанию — сам пресет: хост решает, подставлять ли `.url`
      (пункт-наследование с duck-атрибутом `.inherit` означает «осознанно оставить поле
      пустым», подстановка убила бы живую связь с основным конфигом).
    * `no_match_hint` — свой глагол для «подставлять нечего»: у «Подключения» это
      «оставить свой адрес» (канон, `_FOOTER_PRESET_LIST_NO_MATCH`), а у голосовой
      панели тот же Enter ещё и сохраняет секцию (`_voice_cloud_save`) — там футер
      зовёт его «сохранить как есть» (§5.4: одна клавиша — одна формулировка), и
      хозяин ленты передаёт ту же строку сюда, иначе рамка и футер спорили бы о
      значении одной клавиши.
    """
    presets = list(presets)
    # Колонка имени — по самому длинному (короткий фикс ширины резал бы выравнивание
    # url, когда имя длиннее: «раздел «Подключение»» и т.п.).
    name_w = max((len(p.name) for p in presets), default=0) + 3

    def render(preset, *, selected: bool) -> Text:
        t = Text()
        t.append("▸ " if selected else "  ", style=ACCENT)
        t.append(preset.name.ljust(name_w))
        t.append(preset.url, style=DEEP_DIM)
        return t

    def can_open(value: str) -> bool:
        return bool(presets) and not value

    def initial_index(items, value: str) -> int | None:
        """Позиция пресета с этим адресом или None (адреса в списке нет).

        Пустое поле — первая строка (первичная настройка, предложить все варианты).
        None, а не 0 при несовпадении: список раскрывается и поверх СВОЕГО адреса
        (форсированное раскрытие, спека 2026-08-06 §2.4), и подсветка первой строки
        означала бы, что Enter заменит рабочий адрес чужим. Канон языка и моделей
        (`_lang_initial_index`) — тот же.

        Пункт-наследование (`.inherit`) из поиска исключён: он означает ровно одно
        состояние — поле ПУСТОЕ, — а url несёт тот же, что унаследован, и часто
        совпадает с обычным пресетом. Стоя первым, он забирал бы совпадение себе, и
        курсор над своим, набранным адресом говорил бы «наследуете» (находка
        владельца по смоку)."""
        if not value:
            return 0
        return next(
            (
                i
                for i, p in enumerate(items)
                if p.url == value and not getattr(p, "inherit", False)
            ),
            None,
        )

    return FieldListController(
        host,
        input_id=input_id,
        row_class=row_class,
        hint_class=f"{input_id}-preset-hint",
        scroll_class=f"{input_id}-preset-scroll",
        rows_source=tuple(presets),
        render_row=render,
        matches=lambda p, q: q in p.name.lower() or q in p.url.lower(),
        initial_index=initial_index,
        can_open=can_open,
        hint=lambda ok: hint if ok else no_match_hint,
        on_pick=on_pick,
        restore_on_close=False,
        closed_hint=closed_hint,
    )
