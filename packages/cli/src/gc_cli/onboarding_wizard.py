"""Интерактивный визард первого запуска (презентация + оркестрация).
IO абстрагирован через WizardIO → тестируется без реального TTY."""
import getpass
import re
import sys
from dataclasses import dataclass
from typing import Callable

from gc_core import onboarding
from gc_core import onboarding_config_writer
from gc_core import onboarding_fields as _of
from gc_core.onboarding import authorization_header, mask_authorization, _valid_endpoint_url


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


def _collect_model(io: WizardIO, config: dict, values: dict, *, fetch=None) -> None:
    from gc_core.model_picker import fetch_models
    fetch = fetch or fetch_models
    endpoint = _effective(values, config, "completions_url") or ""
    auth = _effective(values, config, "Authorization", header=True)
    if auth == onboarding.TOKEN_SENTINEL:
        auth = None
    verify = bool(config.get("use_ssl", True))
    models = None
    if endpoint:
        try:
            models = fetch(endpoint, auth, verify=verify)
        except Exception:
            models = None
    io.printer("")
    io.printer(f"  {_of.ONBOARDING_FIELDS[2].label}")
    if not models:
        io.printer(f"  {_of.MODEL_NO_LIST}")
        raw = io.reader("  > ", False).strip()
        if raw:
            values["model"] = raw
        return
    has_warn = any(m.tool_capable is False for m in models)
    for i, m in enumerate(models, 1):
        mark = " ⚠" if m.tool_capable is False else ""
        io.printer(f"  {i}) {m.id}{mark}")
    io.printer(f"  {len(models) + 1}) {_of.MANUAL_ENTRY}")
    if has_warn:
        io.printer(f"  ⚠ {_of.MODEL_WARN}")
    choice = _select_single(io, f"введите номер 1–{len(models) + 1}", len(models) + 1, default=len(models) + 1)
    if choice == len(models) + 1:
        raw = io.reader("  > ", False).strip()
        if raw:
            values["model"] = raw
    else:
        values["model"] = models[choice - 1].id


def collect_fields(io: WizardIO, config: dict, *, review_all: bool) -> dict:
    pending = onboarding.pending_field_keys(config)
    values: dict = {}
    for field in _FIELDS:
        if not (review_all or field.leaf in pending):
            continue
        if field.kind == "auth":
            _collect_auth(io, values)
            continue
        if field.kind == "model":
            _collect_model(io, config, values)
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
        for idx in _select_multi(io, "номера через запятую, напр. 1,3", len(names)):
            mcp.install(names[idx - 1])


def run(config: dict, config_path, *, review_all: bool, io: WizardIO, mcp=None) -> bool:
    _rule_header(io)
    try:
        values = collect_fields(io, config, review_all=review_all)
        if values:
            from gc_core.config import _default_config_path, validate_config_text
            from gc_core.onboarding import FIELD_ANCHOR_PATHS
            dotted = {FIELD_ANCHOR_PATHS.get(k, k): v for k, v in values.items()}
            for missing in onboarding_config_writer.write_values(
                config_path, dotted,
                template_path=_default_config_path(),
                validate=validate_config_text,
            ):
                io.printer(f"  [warn] поле {missing} не найдено в конфиге — пропущено")
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
    io.printer("")
    return True
