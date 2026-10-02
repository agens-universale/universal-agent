"""Интерактивный визард первого запуска (презентация + оркестрация).
IO абстрагирован через WizardIO → тестируется без реального TTY."""

import getpass
import re
import sys
from dataclasses import dataclass
from typing import Callable

from gc_core import onboarding
from gc_core import onboarding_fields as _of
from gc_core.onboarding import (
    _valid_endpoint_url,
    authorization_header,
    mask_authorization,
)  # re-export
from gc_core.settings import settings_of
from gc_core.utils.is_number import is_number


class InvalidChoice(ValueError):
    """Невалидный ввод в меню; текст — готовая подсказка пользователю."""


def _tokens(raw: str) -> list:
    return [t for t in re.split(r"[,\s]+", raw.strip()) if t]


def parse_single(raw: str, n: int):
    toks = _tokens(raw)
    if not toks:
        return None
    if len(toks) != 1:
        raise InvalidChoice(f"Выберите один пункт: 1–{n}.")
    t = toks[0]
    if not is_number(t):
        raise InvalidChoice(f"Введите номер 1–{n}.")
    v = int(t)
    if not (1 <= v <= n):
        raise InvalidChoice(f"{v} — вне диапазона 1–{n}.")
    return v


def parse_multi(raw: str, n: int) -> list:
    seen: list = []
    for t in _tokens(raw):
        if not is_number(t):
            raise InvalidChoice(f"«{t}» — не номер, доступны 1–{n}.")
        v = int(t)
        if not (1 <= v <= n):
            raise InvalidChoice(f"«{v}» — нет такого пункта, доступны 1–{n}.")
        if v not in seen:
            seen.append(v)
    return sorted(seen)


# Порог печати списка целиком и размер показываемого куска. Пятнадцать строк со
# шапкой шага и приглашением укладываются в окно 24 строк; порог чуть выше
# показа — чтобы не обрезать список ради двух строк.
_LIST_WHOLE_MAX = 20
_LIST_SHOWN = 15
# Подтверждение «да». Канон `[y/N]` — у `cli.py:232-241`; `д`/`да` добавлены
# потому, что на русской раскладке клавиша `y` печатает «н», и подтверждать
# вслепую «нетом» человек не должен.
_YES = frozenset({"y", "yes", "д", "да"})
# Явный отказ на приглашении «таких нет». `no` в набор не входит: пара букв
# встречается в реальных id (`nousresearch/...`), и отняв её у поиска, сломаем
# живой сценарий — а поиск по одной букве в каталоге из сотен моделей всё
# равно бессмысленен.
_NO = frozenset({"n", "н", "нет"})


def _matching(models, query: str) -> list:
    """Модели, чей id содержит `query` (регистр не важен)."""
    needle = query.casefold()
    return [m for m in models if needle in m.id.casefold()]


def _exact_model(models, query: str):
    """Модель с ровно таким id — или None. Точное совпадение выбирается сразу:
    кто знает имя, не должен искать его номер в собственном же результате."""
    needle = query.casefold()
    return next((m for m in models if m.id.casefold() == needle), None)


def _view_initial(models) -> tuple:
    """Что показать на входе в шаг: короткий список целиком и без счётчика,
    длинный — первым куском и со счётчиком. Молча резать нельзя: обрезанный
    список без объяснения читается как «это всё, что есть»."""
    if len(models) <= _LIST_WHOLE_MAX:
        return list(models), ""
    shown = list(models[:_LIST_SHOWN])
    return shown, f"показаны {len(shown)} из {len(models)} · * — показать все"


def _interpret(raw: str, models, shown) -> tuple:
    """Что значит ввод шага выбора модели. Порядок разбора — из спеки §5.3:
    пусто → оставить; `*` → весь список; номер показанной строки → выбор;
    точный id → выбор; иначе поиск подстрокой; ничего не нашлось → спросить,
    брать ли введённое как id.

    Отдельной функцией, а не ветками в цикле: правило разбора — единственное
    место, где решается судьба ввода, и проверять его надо без экрана."""
    if not raw:
        return "keep", ""
    if raw == "*":
        return "all", ""
    if is_number(raw) and 1 <= int(raw) <= len(shown):
        return "pick", shown[int(raw) - 1].id
    exact = _exact_model(models, raw)
    if exact is not None:
        return "pick", exact.id
    found = _matching(models, raw)
    if not found:
        return "ask", raw
    if len(found) <= _LIST_SHOWN:
        return "view", (
            found,
            f"найдено {len(found)} из {len(models)} · * — весь каталог",
        )
    return "view", (
        found[:_LIST_SHOWN],
        f"показаны {_LIST_SHOWN} из {len(found)} совпадений · уточните запрос · * — весь каталог",
    )


@dataclass
class WizardIO:
    reader: Callable[[str, bool], str]  # (prompt, secret) -> str
    printer: Callable[[str], None]


@dataclass(frozen=True)
class _Field:
    leaf: str  # ключ-якорь для config-writer
    hint: str
    kind: str  # "endpoint" | "auth" | "model"


_FIELDS = tuple(
    _Field(fs.key, fs.hint, kind=fs.kind) for fs in _of.ONBOARDING_FIELDS
)


def _collect_auth(io: WizardIO, values: dict) -> None:
    io.printer("")
    io.printer(f"  {_of.ONBOARDING_FIELDS[1].label}")
    io.printer(
        "\n".join(f"  {i}) {opt}" for i, opt in enumerate(_of.AUTH_OPTIONS, 1))
    )
    choice = _select_single(io, "введите номер 1–3", 3, default=3)
    if choice == 1:
        io.printer(f"  {_of.TOKEN_HINT}")
        raw = io.reader("  > ", True).strip()
        if raw:
            header = authorization_header(raw)
            values["Authorization"] = header
            io.printer(
                f"  записано: Authorization: {mask_authorization(header)}"
            )
    elif choice == 2:
        values["Authorization"] = ""  # чистит сентинел → политика довольна
        io.printer(f"  {_of.NO_AUTH_NOTE}")
    # 3 → пропуск (ничего не пишем, остаётся pending)


def _effective(values: dict, backend, key: str, header: bool = False):
    """Значение, введённое в этом прогоне, иначе — из секции ``backend``."""
    if header:
        v = values.get("Authorization")
        if v is None:
            v = (backend.chat_completion_headers or {}).get("Authorization")
        return v
    if values.get(key) is not None:
        return values.get(key)
    return getattr(backend, key)


def _print_models(io: WizardIO, shown, *, current: str) -> None:
    """Нумерованные строки показанного куска. Ширина номера — по длине куска:
    столбец id не должен прыгать между строками 9) и 10)."""
    width = len(str(len(shown)))
    for i, m in enumerate(shown, 1):
        mark = " ⚠" if m.tool_capable is False else ""
        here = "  ← сейчас" if current and m.id == current else ""
        io.printer(f"  {i:>{width}}) {m.id}{mark}{here}")
    if any(m.tool_capable is False for m in shown):
        io.printer(f"  ⚠ {_of.MODEL_WARN}")


def _current_line(
    config: dict, level: str, model_id: str, note: str = ""
) -> str:
    """Строка «сейчас» шага. Имя записи в скобках — только когда слот задан:
    модель из легаси `agent.model` имени не имеет, и выдумывать его нельзя.
    `note` — подпись об источнике модели фоллбэка (M12): слот тогда не задан,
    и с именем записи она не встречается."""
    if not model_id:
        return f"сейчас: {_of.LEVEL_UNSET} · Enter — оставить"
    alias = getattr(settings_of(config).slots, level)
    entry = f"  (имя записи «{alias}»)" if alias and alias != model_id else ""
    if note:
        entry = f"  {note}"
    return f"сейчас: {model_id}{entry} · Enter — оставить"


def _warn_if_tool_incapable(
    io: WizardIO, models, shown, model_id: str
) -> None:
    """Предупреждение в момент ВЫБОРА: точный ввод id минует печать каталога, а ⚠
    живёт только в ней (`_print_models`) и только у показанного куска — на длинном
    каталоге модель без инструментов выбиралась без предупреждения вовсе (ревью
    M13). Один перехват на оба шага: и модель, и уровни выбирают через
    `_choose_model`.

    Гейт «id не был в `shown`» (ревью #15): номер строки — это ВСЕГДА строка из
    `shown`, и `_print_models` уже дорисовал ей ⚠ и напечатал это же
    предупреждение под списком; печатать его здесь ещё раз на тот же id —
    дублировать то, что человек только что видел. Печати не было только у
    точного id — тот обходит каталог целиком (`_interpret`: `_exact_model` ищет
    по ВСЕМ моделям, не по `shown`)."""
    if any(m.id == model_id for m in shown):
        return
    m = next((m for m in models if m.id == model_id), None)
    if m is not None and m.tool_capable is False:
        io.printer(f"  {m.id} ⚠")
        io.printer(f"  ⚠ {_of.MODEL_WARN}")


def _ask_as_is(io: WizardIO, query: str) -> tuple:
    """Совпадений нет — предложить взять введённое как id.
    ('keep','') — Enter; ('take', query) — согласие; ('decline','') — явный
    отказ (`n`/«н»/«нет»); ('query', raw) — ответ, который сам является
    следующим запросом."""
    io.printer("")
    io.printer("  таких у провайдера нет.")
    io.printer(f"  взять «{query}» как есть?")
    raw = io.reader(
        "  > (y — да · n — нет · другой запрос · Enter — пропустить шаг): ",
        False,
    ).strip()
    if not raw:
        return "keep", ""
    folded = raw.casefold()
    if folded in _YES:
        return "take", query
    if folded in _NO:
        return "decline", ""
    return "query", raw


def _apply_choice(
    io: WizardIO, models, shown, kind: str, payload
) -> tuple[str, object]:
    """Что делать с распознанным вводом шага «Модель».

    Возвращает пару «действие, значение»: `("return", id|None)` — шаг закончен,
    `("view", (shown, note))` — сменить показанный кусок, `("pending", raw)` —
    ответ сам стал следующим запросом, `("again", None)` — перерисовать список.
    """
    if kind == "keep":
        return "return", None
    if kind == "pick":
        _warn_if_tool_incapable(io, models, shown, payload)
        return "return", payload
    if kind == "all":
        return "view", (list(models), "")
    if kind == "view":
        return "view", payload
    what, value = _ask_as_is(io, payload)
    if what == "keep":
        return "return", None
    if what == "take":
        return "return", value
    if what == "query":
        return "pending", value
    # "decline" — ничего не меняем: список и приглашение печатаются заново.
    return "again", None


def _choose_model(io: WizardIO, models, *, current: str) -> str | None:
    """Выбор модели из списка. Возвращает id либо None — «оставить как есть».

    Цикл тонкий: решение об вводе принимает `_interpret`, а здесь остаётся
    печать и удержание состояния показа."""
    shown, note = _view_initial(models)
    pending = None  # ввод, полученный ответом на «взять как есть»
    while True:
        if pending is None:
            io.printer("")
            _print_models(io, shown, current=current)
            if note:
                io.printer(f"  {note}")
            io.printer("")
            raw = io.reader(
                f"  > (номер 1–{len(shown)} или часть имени): ", False
            ).strip()
        else:
            raw, pending = pending, None
        action, value = _apply_choice(
            io, models, shown, *_interpret(raw, models, shown)
        )
        if action == "return":
            return value
        if action == "view":
            shown, note = value
        elif action == "pending":
            pending = value
            # "decline" — ничего не меняем: список и приглашение печатаются
            # заново на следующем витке (pending остаётся None)


def _fetch_models(config: dict, values: dict, fetch=None):
    """Список моделей у провайдера — по адресу и токену, УЖЕ введённым в этом
    прогоне (иначе список пришёл бы со старого сервера). Сбой сети — None:
    шаг обязан продолжаться свободным вводом.

    Заглушка адреса — не адрес (`configured_endpoint`): по ней запрос уходил на
    несуществующий `your-backend:PORT` и ждал таймаута, чтобы сказать «список не
    получен» — при пустом адресе шаг честно говорит это сразу."""
    from gc_core.model_picker import fetch_models

    fetch = fetch or fetch_models
    backend = settings_of(config).backend
    endpoint = onboarding.configured_endpoint(
        _effective(values, backend, "completions_url")
    )
    auth = _effective(values, backend, "Authorization", header=True)
    if auth == onboarding.TOKEN_SENTINEL:
        auth = None
    if not endpoint:
        return None
    try:
        return fetch(endpoint, auth, verify=backend.use_ssl)
    except Exception:
        return None


def _collect_model(
    io: WizardIO, config: dict, values: dict, *, fetch=None
) -> list | None:
    """Шаг «Модель». Возвращает полученный список моделей (или None) — шаг
    уровней переиспользует его, второй поход в сеть за тем же ответом лишний.

    Заголовок и строка «сейчас» печатаются ДО похода в сеть: на недоступном
    эндпоинте запрос молчит десятки секунд, и без этого порядка мастер
    выглядит зависшим (подтверждено живым замером владельца)."""
    io.printer("")
    io.printer(f"  {_of.ONBOARDING_FIELDS[2].label}")
    io.printer(f"  {_of.MODEL_STEP_HINT}")
    # Что считать текущим на этом шаге — правило общее с экраном TUI и живёт в ядре
    # (`onboarding.model_step_display`): вторая его редакция уже расходилась с
    # первой — тот же шаг в TUI показывал модель активного уровня. Подпись —
    # источник модели фоллбэка при незаданном default (M12): без неё строка
    # пустела, и «оставить как есть» было не на чем.
    current, fallback_note = onboarding.model_step_display(config)
    if current:
        io.printer(
            f"  {_current_line(config, 'default', current, note=fallback_note)}"
        )
    io.printer(f"  {_of.MODEL_FETCHING}")
    models = _fetch_models(config, values, fetch)
    if not models:
        io.printer(f"  {_of.MODEL_NO_LIST}")
        raw = io.reader("  > ", False).strip()
        if raw:
            values["model"] = raw
            io.printer(f"  выбрано для default: {raw}")
        return models
    chosen = _choose_model(io, models, current=current)
    if chosen:
        values["model"] = chosen
        io.printer(f"  выбрано для default: {chosen}")
    return models


_LEVELS = ("light", "max")


def _collect_levels(io: WizardIO, config: dict, values: dict, models) -> None:
    """Шаг «Уровни моделей»: две независимые необязательные под-формы. Пропуск не
    пишет НИЧЕГО — заданный ранее уровень переживает прогон, в котором его
    пропустили (ключа нет в `values` → операции нет → слот не тронут).

    Список моделей приходит от шага «Модель» и не перезапрашивается; `None`
    значит, что его не было и там, — уровни спрашиваются свободным вводом."""
    io.printer("")
    io.printer(f"  {_of.LEVELS_TITLE}")
    io.printer(f"  {_of.LEVELS_QUESTION}")
    raw = io.reader("  > (y — да · Enter — пропустить): ", False).strip()
    if raw.casefold() not in _YES:
        return
    for level in _LEVELS:
        io.printer("")
        io.printer(f"  {_of.LEVEL_FORM_LABEL[level]}")
        current = onboarding.assigned_level_model(config, level)
        io.printer(f"  {_current_line(config, level, current)}")
        key = onboarding.MODEL_SLOT_KEYS[level]
        if not models:
            io.printer(f"  {_of.MODEL_NO_LIST}")
            typed = io.reader("  > ", False).strip()
            if typed:
                values[key] = typed
                io.printer(f"  выбрано для {level}: {typed}")
            continue
        chosen = _choose_model(io, models, current=current)
        if chosen:
            values[key] = chosen
            io.printer(f"  выбрано для {level}: {chosen}")


def _collect_endpoint(
    io: WizardIO,
    config: dict,
    values: dict,
    field: "_Field",
    *,
    review_all: bool,
    pending,
) -> None:
    """Шаг «Адрес»: печать заголовка и ввод до валидного URL либо до Enter."""
    io.printer("")
    io.printer(f"  {_of.ONBOARDING_FIELDS[0].label}")
    if field.hint:
        io.printer(f"  {field.hint}")
    # Заглушка шаблона — не значение (тот же нормализатор, что у сетевого
    # пути ниже): показать её как действующий адрес значит соврать о том,
    # куда пойдёт запрос. Сюда она доходит, когда требование адреса закрыл
    # самодостаточный реестр, а плоский `backend` так и остался шаблонным.
    current = onboarding.configured_endpoint(
        settings_of(config).backend.completions_url
    )
    if review_all and field.leaf not in pending and current:
        io.printer(f"  [текущее: {current}]")
    while True:
        raw = io.reader("  > ", False).strip()
        if not raw:
            break  # Enter → пропустить (остаётся pending/текущее)
        if _valid_endpoint_url(raw):
            values[field.leaf] = raw
            break
        io.printer(
            f"  «{raw}» не похоже на URL (нужны http(s):// и хост). Введите ещё раз или Enter — пропустить."
        )


def collect_fields(io: WizardIO, config: dict, *, review_all: bool) -> dict:
    pending = onboarding.pending_field_keys(config)
    values: dict = {}
    models = None
    for field in _FIELDS:
        if not (review_all or field.leaf in pending):
            continue
        if field.kind == "auth":
            _collect_auth(io, values)
            continue
        if field.kind == "model":
            models = _collect_model(io, config, values)
            continue
        if field.kind == "endpoint":
            _collect_endpoint(
                io,
                config,
                values,
                field,
                review_all=review_all,
                pending=pending,
            )
            continue
    if review_all:
        # Уровни не входят в требования запуска (`_FIELD_CHECKS`), и удлинять ими
        # аварийную донастройку незачем: их спрашивает только режим сверки.
        _collect_levels(io, config, values, models)
    return values


def _rule_header(io: WizardIO) -> None:
    io.printer("")
    io.printer("─── Настройка GigaCode Universal Agent ───")
    io.printer("")
    io.printer("  Enter — пропустить · Ctrl-C — выйти")


def default_io() -> WizardIO:
    def reader(prompt: str, secret: bool) -> str:
        return getpass.getpass(prompt) if secret else input(prompt)

    return WizardIO(reader=reader, printer=lambda m: print(m, file=sys.stderr))


def _reload_config() -> dict:
    from gc_core.config import load_config

    return load_config(None)


def _select_single(io: WizardIO, hint: str, n: int, *, default):
    while True:
        raw = io.reader(f"  > ({hint}): ", False)
        try:
            value = parse_single(raw, n)
        except InvalidChoice as exc:
            io.printer(f"  {exc}")
            continue
        return default if value is None else value


def _select_multi(io: WizardIO, hint: str, n: int) -> list:
    while True:
        raw = io.reader(f"  > ({hint}): ", False)
        try:
            return parse_multi(raw, n)
        except InvalidChoice as exc:
            io.printer(f"  {exc}")


def _mcp_step(io: WizardIO, mcp) -> None:
    names = mcp.installable_names()
    if not names:
        return
    io.printer("MCP-серверы — что поставить?")
    io.printer("  1) все рекомендованные\n  2) выбрать\n  3) пропустить")
    choice = _select_single(io, "введите номер 1–3", 3, default=3)
    if choice == 1:
        mcp.install(None)
    elif choice == 2:
        for i, name in enumerate(names, 1):
            io.printer(f"   [{i}] {name}")
        for idx in _select_multi(
            io, "номера через запятую, напр. 1,3", len(names)
        ):
            mcp.install(names[idx - 1])


_SUMMARY_LABEL = {"default": "модель", "light": "light", "max": "max"}


def _summary_lines(config: dict) -> list[str]:
    """Итог прогона: три уровня СВЕЖЕГО конфига. Пропущенный шаг и пустой слот
    выглядят одинаково — и это правда: сводка говорит, что лежит в файле, а не
    что человек вводил."""
    lines = []
    slots = settings_of(config).slots
    for level in ("default", "light", "max"):
        model = onboarding.assigned_level_model(config, level)
        via_fallback = not model and level == "default"
        if via_fallback:
            # Модель и подпись — из ядра (переехали туда для M12): те же слова
            # печатает строка «сейчас» шага «Модель» обеих поверхностей.
            model, note = onboarding.default_fallback_model(config)
        if not model:
            lines.append(f"    {_SUMMARY_LABEL[level]:<12} не задан")
            continue
        if via_fallback:
            entry = f"  {note}"
        else:
            alias = getattr(slots, level)
            entry = (
                f"  (имя записи «{alias}»)" if alias and alias != model else ""
            )
        lines.append(f"    {_SUMMARY_LABEL[level]:<12} {model}{entry}")
    return lines


def _broken_config_fate(*, refused: bool, wrote: bool) -> str:
    """Судьба файла, если после всех ответов конфиг всё ещё не загружается.

    О ней говорим только то, что знаем, — исходов три, и общего текста у них
    нет. Запись СОСТОЯЛАСЬ: фаза 1 судит текст, а не собираемость стека
    (`slots.max` на несуществующий алиас её проходит), файл изменён — и
    «остался в прежнем виде» отправляло человека искать свои ответы в файле,
    где они уже лежат. Запись ОТКАЗАЛА: что легло, а что нет, ветка отказа
    сказала раньше — второй раз об этом не спорим. Писателя не звали (человек
    прошёл мастер, ничего не заполнив): файл и правда прежний.
    """
    if refused:
        return ""
    if wrote:
        return " — ваши ответы записаны, поломка мимо них осталась"
    return " — записывать было нечего, файл остался в прежнем виде"


def _report_outcome(
    io: WizardIO, config_path, *, refused: bool, wrote: bool
) -> bool:
    """Перечитать конфиг и напечатать итог мастера. True — напечатано «✓ Готово»."""
    try:
        fresh = _reload_config()
    except ValueError as exc:
        # Второе, необработанное падение здесь было бы хуже первого: человек уже
        # потратил время на все вопросы мастера.
        fate = _broken_config_fate(refused=refused, wrote=wrote)
        io.printer(
            f"\n  [ошибка] вы ответили на все вопросы, но конфиг всё ещё не загружается{fate}:"
        )
        io.printer(onboarding.explain_broken_config(str(config_path), exc))
        return False
    if onboarding.is_unconfigured(fresh):
        io.printer(onboarding.setup_guide(str(config_path), fresh))
        return False
    if refused:
        # Итог уже напечатан у отказа: что легло, что нет. Сводка «трёх уровней»
        # здесь читалась бы как отчёт о записанном.
        return False
    io.printer("")
    io.printer(f"  ✓ Готово. Конфиг: {config_path}")
    for line in _summary_lines(fresh):
        io.printer(line)
    io.printer("")
    return True


def run(
    config: dict,
    config_path,
    *,
    review_all: bool,
    io: WizardIO,
    mcp=None,
    deactivate_profile: bool = False,
) -> bool:
    _rule_header(io)
    if deactivate_profile:
        # Тот же текст, что на экране подтверждения TUI (`_of` — общий дом), и
        # ДО вопросов, а не перед самой записью: здесь нет шага подтверждения, и
        # после последнего ответа мастер пишет сразу — предупреждение, стоящее
        # там, человек читал бы уже как отчёт о случившемся.
        io.printer("")
        io.printer(f"  {_of.PROFILE_DEACTIVATION_WARNING}")
    # Отказ записи — любая из трёх веток ниже — не даёт печатать «✓ Готово» и
    # отдавать успех: отказ оставляет файл читаемым, проверка «конфиг не настроен»
    # его не ловит, и `gc-cli --init-config && …` шёл бы дальше, считая выбор
    # сохранённым (рулинг владельца у `cli._run_init`: код возврата отражает исход).
    refused = False
    wrote = False
    try:
        values = collect_fields(io, config, review_all=review_all)
        # Снятие указателя — само по себе повод к записи: человек прошёл мастер
        # ради починки, и оставить конфиг несобираемым потому, что все поля он
        # пропустил, значит не сделать ровно то, что обещано строкой выше.
        if values or deactivate_profile:
            missing = onboarding.write_wizard_values(
                config_path, values, deactivate_profile=deactivate_profile
            )
            wrote = True
            if missing:
                # Правило «всё или ничего» — про ПЛОСКУЮ запись; записи реестра ушли
                # раньше отдельной транзакцией, поэтому файл уже не нетронут. Говорим
                # о том, чего не хватает, а не о судьбе всего конфига.
                io.printer(
                    f"\n  [ошибка] не записаны поля: {', '.join(missing)} — впишите их в конфиг вручную"
                )
                refused = True
    except onboarding.PartialWriteError as exc:
        # Часть записи уже на диске — обещать нетронутый файл нельзя (то же
        # правило, что у ветки `missing` выше). Что именно легло, знает только
        # `write_wizard_values` — читаем её ответ (`written`), а не выводим состав
        # из её порядка записи: догадка разъедется с писателем на первой же правке
        # порядка, и человек прочтёт про снятый профиль там, где его не снимали.
        io.printer(
            f"\n  [ошибка] остальное не записано: {exc} — при этом уже: "
            + ", ".join(
                _of.LANDED_WORDS.get(name, name) for name in exc.written
            )
        )
        refused = True
    except (ValueError, OSError) as exc:
        # OSError — штатный отказ записи (диск, права, win-лок), не невозможный
        # случай: мимо перехвата человек получал голый трейсбек после всех
        # ответов (ревью L33).
        io.printer(f"\n  [ошибка] конфиг не записан: {exc}")
        refused = True
    except (KeyboardInterrupt, EOFError):
        # Прерывание — исход, а не пауза: дальше мастер перечитывал конфиг и на
        # настроенном печатал «✓ Готово» со сводкой и отдавал успех. Человек нажал
        # Ctrl-C ровно затем, чтобы этого не случилось, а `gc-cli --init-config && …`
        # шёл дальше, будто он всё подтвердил.
        io.printer("\nПрервано.")
        return False
    else:
        if mcp is not None:
            _mcp_step(io, mcp)
    return _report_outcome(io, config_path, refused=refused, wrote=wrote)
