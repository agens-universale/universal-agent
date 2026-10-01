"""Детерминированный tripwire для shell/python-команд: ловит ОЧЕВИДНЫЕ
обращения к protected-путям (rm -rf .gc и т.п.). Это НЕ гарантия — обфускация
не ловится; второй рубеж — LLM-судья (command_moderation), страховка — бэкап
памяти (Спека B §7)."""

from __future__ import annotations

import inspect
import json
import os
import re
import shlex
from typing import Any, Callable

from gc_core.security.command_moderation import (
    NO_WORKAROUND_NOTICE,
    effective_command_cwd,
)
from gc_core.security.confirm_port import confirmation_step
from gc_core.security.dual_wrapper import dual_wrap
from gc_core.security.guard_probe import GuardProbeRequest, probe_step
from gc_core.security.path_policy import (
    _extract_config_dict,
    resolve_tool_context,
)
from gc_core.security.protected_paths import (
    classify_path,
    protected_paths,
    protection_enabled,
)

_TOKEN_RE = re.compile(r"[\s=;&|()<>]+")


#: Символы, которые shlex с punctuation_chars=True отдаёт отдельными токенами:
#: разделители команд, перенаправления, группировка. Путями они не бывают —
#: в проверку они не идут. Границы подкоманд при этом теряются: сейчас
#: scan_command_for_protected идёт по плоскому списку и они ему не нужны, но
#: если понадобится разбор по подкомандам — выбрасывать их будет нельзя.
_PUNCTUATION_CHARS = "();<>|&"


def _tokens(command: str) -> list[str]:
    r"""Токены команды для проверки путей.

    Разбор — один проход `shlex` с `punctuation_chars=True`: он снимает кавычки
    и одновременно отделяет `; & | < > ( )` от соседних слов. Раньше здесь
    резали СЫРУЮ строку регуляркой `[;&|\n\r]+` ДО shlex, потому что штатный
    `shlex.split` разделителями их не считает и `rm -rf .gc; echo x` давал
    токен `.gc;` — с точкой с запятой внутри, которую проверка пути не узнавала
    (найдено 18.08.2026: агент удалил .gc, приписав `; echo "exit=$?"`).

    Резка по сырой строке рвала кавычки и породила две поломки, которые снял
    этот разбор:
      * ложный запрет — `git commit -m "fix: .gc; cleanup"`: `;` внутри кавычек
        рвал строку, обе половины падали в грубый фолбэк, и голый `.gc`
        читался как защищённый путь;
      * ложный пропуск — `rm -rf ".gc" "note; done"`: разделитель в соседнем
        кавыченном аргументе ломал кавычки, и защищённый путь оставался
        токеном `".gc"` ВМЕСТЕ с кавычками.
    Заодно закрылась унаследованная дыра `rm -rf .gc>/dev/null`: `>` в список
    разделителей не входил и путь склеивался с перенаправлением. По той же
    причине теперь видны `(cd x && rm -rf .gc)` и `rm -rf $(echo .gc)` —
    скобка больше не прилипает к пути.

    Две настройки обязательны, и обе держат ЛОЖНЫЙ ЗАПРЕТ — сырой
    `shlex.shlex` умолчаниями отличается от `shlex.split`, которым пользовались
    раньше:
      * `commenters = ""` — иначе решётка обрывает слово, и `rm -rf .gc#backup`
        даёт токен `.gc`: удаление постороннего файла читается как удаление
        защищённого каталога;
      * `whitespace_split = True` — иначе `,` и `:` не входят в wordchars,
        слово распадается, и `rm -rf .gc:old` тоже даёт голый `.gc`.
    Обе закреплены в test_command_guard_separators.py именно этими командами.
    """
    # posix=True on POSIX (backslash as escape char); posix=False on Windows
    # (backslash is path separator, not escape — shlex would strip it otherwise).
    posix = os.name != "nt"
    lex = shlex.shlex(command, posix=posix, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    try:
        raw: list[str] = list(lex)
        # В non-posix кавычки остаются частью токена: снимаем сами, иначе
        # защищённый путь приезжает как '".gc"' и классификатор его не узнаёт.
        strip_quotes = not posix
        # shlex уже отработал escape-последовательности сам.
        drop_escapes = False
    except ValueError:
        # Незакрытая кавычка (или обрыв на escape) — разбирать нечем; режем
        # грубо и снимаем кавычки: для tripwire лишний токен безопаснее
        # спрятанного пути. Такая команда всё равно не выполнится — backend
        # запускает её через shell=True, и shell падает на синтаксисе; фолбэк
        # нужен, чтобы отказ был осознанным, а не случайным.
        raw = _TOKEN_RE.split(command)
        strip_quotes = True
        # Экранирование грубая резка не разбирает, а posix-shlex разобрал бы:
        # без этого `rm -rf ./.gc\/x "unclosed` давал токен с бэкслешем внутри,
        # и защищённый путь переставал узнаваться (фуззинг 200 000 строк — три
        # таких случая). На Windows бэкслеш — разделитель пути, там не трогаем.
        drop_escapes = posix
    out: list[str] = []
    for tok in raw:
        if strip_quotes:
            tok = tok.strip("\"'")
        if drop_escapes:
            tok = tok.replace("\\", "")
        if not tok or all(ch in _PUNCTUATION_CHARS for ch in tok):
            continue
        out.append(tok)
    return out


def scan_command_for_protected(command: str, *, cwd: str, extra=None) -> str:
    ps = protected_paths(cwd=cwd, extra=extra)
    worst = "allow"
    rank = {"allow": 0, "ask": 1, "deny": 2}
    for tok in _tokens(command):
        cand = tok if os.path.isabs(tok) else os.path.join(cwd, tok)
        cls = classify_path(cand, ps)
        decision = {
            "skills": "allow",
            "none": "allow",
            "instructions": "ask",
            "operational": "deny",
        }[cls]
        if rank[decision] > rank[worst]:
            worst = decision
    return worst


def _blocked_command_result(
    self_obj: Any,
    effective_tool_name: str,
    err: dict,
    args: tuple,
    kwargs: dict,
) -> str:
    # Use tool's own blocked-result method if available (mirrors moderation pattern).
    blocked_method = getattr(
        self_obj, "_command_moderation_blocked_result", None
    )
    if callable(blocked_method):
        return blocked_method(err, *args[1:], **kwargs)
    return json.dumps(
        {"tool": effective_tool_name, "ok": False, "error": err},
        ensure_ascii=False,
    )


def _extract_payload(
    func: Callable[..., str], payload_arg: str, args: tuple, kwargs: dict
) -> str | None:
    """Строка команды/кода из аргументов вызова; None — проверять нечего."""
    try:
        bound = inspect.signature(func).bind_partial(*args, **kwargs)
        payload = bound.arguments.get(payload_arg)
    except Exception:
        return None
    return payload if isinstance(payload, str) else None


def _command_guard_flow(
    func: Callable[..., str],
    payload_arg: str,
    tool_name: str,
    args: tuple,
    kwargs: dict,
):
    # Тело — ГЕНЕРАТОР, а не обычная функция: `dual_wrap` строит по нему и
    # синхронную, и асинхронную обёртку. Обычный декоратор здесь ломался
    # молча: на `async def acall` он в ветке отказа возвращал СТРОКУ вместо
    # корутины, и вызывающий получал «a coroutine was expected».
    # `return (yield)` — точка, где вызывается сам тул.
    self_obj = args[0] if args else None
    ctx_obj = kwargs.get("ctx") or getattr(self_obj, "_ctx", None)
    cfg = _extract_config_dict(self_obj, ctx_obj, kwargs)

    if not protection_enabled(cfg):
        return (yield)

    # Extract the payload (command string or code string).
    payload = _extract_payload(func, payload_arg, args, kwargs)
    if payload is None:
        return (yield)

    # For the security scan we need the project root (launch_root) so that
    # project_gc_dir resolves the correct .gc directory.  Relative path
    # tokens in commands are also resolved against launch_root — the
    # project root is the canonical base for policy checks.
    context = resolve_tool_context(*args, **kwargs)
    effective_tool_name = tool_name or getattr(func, "__name__", "")
    launch_root = context.launch_root
    if not launch_root:
        # Fallback: use effective command cwd
        launch_root = effective_command_cwd(
            effective_tool_name, kwargs, context
        )

    extra = list(context.write_guard.extra_paths) or None
    if callable(getattr(ctx_obj, "guard_probe", None)):
        facts = yield probe_step(
            ctx_obj,
            GuardProbeRequest(
                "command",
                effective_tool_name,
                payload,
                launch_root,
            ),
        )
        decision = facts.protected_decision
    else:
        decision = scan_command_for_protected(
            payload, cwd=launch_root, extra=extra
        )

    if decision == "allow":
        return (yield)

    if decision == "ask":
        from gc_core.security.confirm_port import request_confirm

        confirmed = yield confirmation_step(
            local_request=request_confirm,
            title="Защищённый путь",
            question="Команда затрагивает защищённый путь. Выполнить?",
            fields=[("command", payload)],
            ctx=ctx_obj,
            decision_key=f"command:{payload}",
        )
        if confirmed:
            return (yield)
        # ask not confirmed → instructive blocked result
        err = {
            "type": "protected_path_denied",
            "message": "User declined the command.",
            # Та же форма, что и у ветки deny ниже: запрет на обход —
            # отдельным полем, а не приклейкой к message. Иначе один и
            # тот же гвард отдаёт модели два разных конверта.
            "guidance": NO_WORKAROUND_NOTICE,
        }
        return _blocked_command_result(
            self_obj, effective_tool_name, err, args, kwargs
        )

    # deny → static blocked result
    err = {
        "type": "protected_path_denied",
        "message": "Command targets a protected path (.gc / config).",
        # Тот же класс обхода, что и у судьи: заблокированную команду
        # модель переписывает в эквивалент. Запрет — одной формулировкой
        # с модерацией (NO_WORKAROUND_NOTICE).
        "guidance": NO_WORKAROUND_NOTICE,
    }
    return _blocked_command_result(
        self_obj, effective_tool_name, err, args, kwargs
    )


def enforce_command_guard(
    payload_arg: str = "command",
    tool_name: str = "",
) -> Callable[[Callable[..., str]], Callable[..., str]]:
    """Deterministic pre-check decorator: runs BEFORE the LLM judge.

    deny  → returns a blocked result immediately (no subprocess spawned).
    ask   → prompts via confirm_port; if not confirmed → blocked.
    allow → falls through to the decorated function unchanged.

    Activated only when protection_enabled(config_dict) is True.
    """

    def decorator(func: Callable[..., str]) -> Callable[..., str]:
        # Тело — ГЕНЕРАТОР, а не обычная функция: `dual_wrap` строит по нему и
        # синхронную, и асинхронную обёртку. Обычный декоратор здесь ломался
        # молча: на `async def acall` он в ветке отказа возвращал СТРОКУ вместо
        # корутины, и вызывающий получал «a coroutine was expected».
        # `return (yield)` — точка, где вызывается сам тул.
        def _flow(*args: Any, **kwargs: Any):
            return (
                yield from _command_guard_flow(
                    func, payload_arg, tool_name, args, kwargs
                )
            )

        return dual_wrap(func, _flow)

    return decorator
