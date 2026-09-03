"""Интерактивный визард первого запуска (презентация + оркестрация).
IO абстрагирован через WizardIO → тестируется без реального TTY."""
import getpass
import re
import sys
from dataclasses import dataclass
from typing import Callable

import onboarding
import onboarding_fields as _of
from onboarding import authorization_header, mask_authorization, _valid_endpoint_url  # re-export


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
    if not t.isdigit():
        raise InvalidChoice(f"Введите номер 1–{n}.")
    v = int(t)
    if not (1 <= v <= n):
        raise InvalidChoice(f"{v} — вне диапазона 1–{n}.")
    return v


def parse_multi(raw: str, n: int) -> list:
    seen: list = []
    for t in _tokens(raw):
        if not t.isdigit():
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
    if raw.isdigit() and 1 <= int(raw) <= len(shown):
        return "pick", shown[int(raw) - 1].id
    exact = _exact_model(models, raw)
    if exact is not None:
        return "pick", exact.id
    found = _matching(models, raw)
    if not found:
        return "ask", raw
    if len(found) <= _LIST_SHOWN:
        return "view", (found, f"найдено {len(found)} из {len(models)}")
    return "view", (found[:_LIST_SHOWN],
                    f"показаны {_LIST_SHOWN} из {len(found)} совпадений · уточните запрос")


@dataclass
class WizardIO:
    reader: Callable[[str, bool], str]   # (prompt, secret) -> str
    printer: Callable[[str], None]


@dataclass(frozen=True)
class _Field:
    leaf: str          # ключ-якорь для config-writer
    label: str
    hint: str
    secret: bool
    kind: str          # "endpoint" | "auth" | "model"


_FIELDS = tuple(
    _Field(fs.key, fs.label, fs.hint, secret=(fs.kind == "auth"), kind=fs.kind)
    for fs in _of.ONBOARDING_FIELDS
)


def _current_value(config: dict, field: _Field) -> str:
    if field.kind == "auth":
        return (config.get("chat_completion_headers") or {}).get("Authorization") or ""
    if field.kind == "endpoint":
        return config.get("completions_url") or ""
    return config.get("model") or ""


def _field_prompt(field: _Field, current: str, review_all: bool) -> str:
    parts = [field.label]
    if review_all and current:
        shown = mask_authorization(current) if field.kind == "auth" else current
        parts.append(f"[текущее: {shown}]")
    if field.hint:
        parts.append(f"({field.hint})")
    return " ".join(parts) + ": "


def _collect_auth(io: WizardIO, values: dict) -> None:
    io.printer("")
    io.printer(f"  {_of.ONBOARDING_FIELDS[1].label}")
    io.printer("\n".join(f"  {i}) {opt}" for i, opt in enumerate(_of.AUTH_OPTIONS, 1)))
    choice = _select_single(io, "введите номер 1–3", 3, default=3)
    if choice == 1:
        io.printer(f"  {_of.TOKEN_HINT}")
        raw = io.reader("  > ", True).strip()
        if raw:
            header = authorization_header(raw)
            values["Authorization"] = header
            io.printer(f"  записано: Authorization: {mask_authorization(header)}")
    elif choice == 2:
        values["Authorization"] = ""      # чистит сентинел → политика довольна
        io.printer(f"  {_of.NO_AUTH_NOTE}")
    # 3 → пропуск (ничего не пишем, остаётся pending)


def _effective(values: dict, config: dict, key: str, header: bool = False):
    if header:
        v = values.get("Authorization")
        if v is None:
            v = (config.get("chat_completion_headers") or {}).get("Authorization")
        return v
    return values.get(key) if values.get(key) is not None else config.get(key)


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


def _current_line(config: dict, level: str, model_id: str) -> str:
    """Строка «сейчас» шага. Имя записи в скобках — только когда слот задан:
    модель из легаси `agent.model` имени не имеет, и выдумывать его нельзя."""
    if not model_id:
        return f"сейчас: {_of.LEVEL_UNSET} · Enter — оставить"
    alias = (config.get("slots") or {}).get(level)
    entry = f"  (запись «{alias}»)" if alias and alias != model_id else ""
    return f"сейчас: {model_id}{entry} · Enter — оставить"


def _ask_as_is(io: WizardIO, query: str) -> tuple:
    """Совпадений нет — предложить взять введённое как id.
    ('keep','') — Enter; ('take', query) — согласие; ('query', raw) — ответ,
    который сам является следующим запросом."""
    io.printer("")
    io.printer("  таких у провайдера нет.")
    io.printer(f"  взять «{query}» как есть?")
    raw = io.reader("  > (y — да · другой запрос · Enter — оставить): ", False).strip()
    if not raw:
        return "keep", ""
    if raw.casefold() in _YES:
        return "take", query
    return "query", raw


def _choose_model(io: WizardIO, models, *, current: str) -> str | None:
    """Выбор модели из списка. Возвращает id либо None — «оставить как есть».

    Цикл тонкий: решение об вводе принимает `_interpret`, а здесь остаётся
    печать и удержание состояния показа."""
    shown, note = _view_initial(models)
    pending = None                  # ввод, полученный ответом на «взять как есть»
    while True:
        if pending is None:
            _print_models(io, shown, current=current)
            if note:
                io.printer(f"  {note}")
            raw = io.reader(f"  > (номер 1–{len(shown)} или часть имени): ",
                            False).strip()
        else:
            raw, pending = pending, None
        kind, payload = _interpret(raw, models, shown)
        if kind == "keep":
            return None
        if kind == "pick":
            return payload
        if kind == "all":
            shown, note = list(models), ""
        elif kind == "view":
            shown, note = payload
        else:
            what, value = _ask_as_is(io, payload)
            if what == "keep":
                return None
            if what == "take":
                return value
            pending = value


def _fetch_models(config: dict, values: dict, fetch=None):
    """Список моделей у провайдера — по адресу и токену, УЖЕ введённым в этом
    прогоне (иначе список пришёл бы со старого сервера). Сбой сети — None:
    шаг обязан продолжаться свободным вводом."""
    from model_picker import fetch_models
    fetch = fetch or fetch_models
    endpoint = _effective(values, config, "completions_url") or ""
    auth = _effective(values, config, "Authorization", header=True)
    if auth == onboarding.TOKEN_SENTINEL:
        auth = None
    if not endpoint:
        return None
    try:
        return fetch(endpoint, auth, verify=bool(config.get("use_ssl", True)))
    except Exception:
        return None


def _collect_model(io: WizardIO, config: dict, values: dict, *, fetch=None) -> list | None:
    """Шаг «Модель». Возвращает полученный список моделей (или None) — шаг
    уровней переиспользует его, второй поход в сеть за тем же ответом лишний."""
    models = _fetch_models(config, values, fetch)
    io.printer("")
    io.printer(f"  {_of.ONBOARDING_FIELDS[2].label}")
    io.printer(f"  {_of.MODEL_STEP_HINT}")
    current = onboarding.effective_model(config)
    if current:
        io.printer(f"  {_current_line(config, 'default', current)}")
    if not models:
        io.printer(f"  {_of.MODEL_NO_LIST}")
        raw = io.reader("  > ", False).strip()
        if raw:
            values["model"] = raw
        return models
    chosen = _choose_model(io, models, current=current)
    if chosen:
        values["model"] = chosen
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
            continue
        chosen = _choose_model(io, models, current=current)
        if chosen:
            values[key] = chosen


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
            io.printer("")
            io.printer(f"  {_of.ONBOARDING_FIELDS[0].label}")
            if field.hint:
                io.printer(f"  {field.hint}")
            if review_all and field.leaf not in pending and _current_value(config, field):
                io.printer(f"  [текущее: {_current_value(config, field)}]")
            while True:
                raw = io.reader("  > ", False).strip()
                if not raw:
                    break                        # Enter → пропустить (остаётся pending/текущее)
                if _valid_endpoint_url(raw):
                    values[field.leaf] = raw
                    break
                io.printer(f"  «{raw}» не похоже на URL (нужны http(s):// и хост). "
                           f"Введите ещё раз или Enter — пропустить.")
            continue
        raw = io.reader(_field_prompt(field, _current_value(config, field), review_all),
                        field.secret).strip()
        if not raw:
            continue   # пропуск: авто → остаётся pending; review → оставить текущее
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
    from config import load_config
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
        for idx in _select_multi(io, "номера через запятую, напр. 1,3", len(names)):
            mcp.install(names[idx - 1])


_SUMMARY_LABEL = {"default": "модель", "light": "light", "max": "max"}


def _summary_lines(config: dict) -> list[str]:
    """Итог прогона: три уровня СВЕЖЕГО конфига. Пропущенный шаг и пустой слот
    выглядят одинаково — и это правда: сводка говорит, что лежит в файле, а не
    что человек вводил."""
    lines = []
    for level in ("default", "light", "max"):
        model = onboarding.assigned_level_model(config, level)
        if not model and level == "default":
            model = onboarding.effective_model(config)
        if not model:
            lines.append(f"    {_SUMMARY_LABEL[level]:<12} не задан")
            continue
        alias = (config.get("slots") or {}).get(level)
        entry = f"  (запись «{alias}»)" if alias and alias != model else ""
        lines.append(f"    {_SUMMARY_LABEL[level]:<12} {model}{entry}")
    return lines


def run(config: dict, config_path, *, review_all: bool, io: WizardIO, mcp=None) -> bool:
    _rule_header(io)
    try:
        values = collect_fields(io, config, review_all=review_all)
        if values:
            missing = onboarding.write_wizard_values(config_path, values)
            if missing:
                # Правило «всё или ничего» — про ПЛОСКУЮ запись; записи реестра ушли
                # раньше отдельной транзакцией, поэтому файл уже не нетронут. Говорим
                # о том, чего не хватает, а не о судьбе всего конфига.
                io.printer("\n  [ошибка] не записаны поля: "
                           f"{', '.join(missing)} — впишите их в конфиг вручную")
    except onboarding.PartialWriteError as exc:
        # Записи реестра ушли первой транзакцией и уже на диске — обещать
        # нетронутый файл нельзя (то же правило, что у ветки `missing` выше).
        io.printer(f"\n  [ошибка] поля не записаны: {exc} — записи моделей "
                   "при этом уже в конфиге")
    except ValueError as exc:
        io.printer(f"\n  [ошибка] конфиг не записан: {exc}")
    except (KeyboardInterrupt, EOFError):
        io.printer("\nПрервано.")
    else:
        if mcp is not None:
            _mcp_step(io, mcp)
    fresh = _reload_config()
    if onboarding.is_unconfigured(fresh):
        io.printer(onboarding.setup_guide(str(config_path), fresh))
        return False
    io.printer("")
    io.printer(f"  ✓ Готово. Конфиг: {config_path}")
    for line in _summary_lines(fresh):
        io.printer(line)
    io.printer("")
    return True
