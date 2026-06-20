import os
import re
import sys
from typing import Any

import arg_parser
import onboarding
from agent_executor import AgentExecutor
from config import (
    apply_cli_overrides,
    build_auth_connector,
    load_config,
    user_config_path,
    _bootstrap_user_config,
)
from config_modules.env_vars import project_gc_dir
from message_policy.user_message_policy import (
    wrap_skill_resolution_technical_instruction,
    wrap_technical_user_content,
)
from run_context import RunContext, create_run_context
from mcp_daemon_autostart import ensure_mcp_daemons, shutdown_mcp_daemons
from utils.json_text import format_json_for_display


def _ensure_project_gc_dir() -> None:
    """Создаёт <cwd>/.gc/ при старте gc-cli. Делегирует общему хелперу
    (тот же вызывает gc-ua) — см. env_vars.ensure_project_gc_dir."""
    from config_modules.env_vars import ensure_project_gc_dir
    ensure_project_gc_dir()


def _ensure_user_skills_dir() -> None:
    """
    Создаёт <gc_data_dir>/skills/ при старте gc-cli (идемпотентно).
    В отличие от project-слоя, user-слой создаём заранее: пользователь точно
    рано или поздно захочет добавить свой скилл, и видеть готовую папку
    в `~/.local/share/gc/` (или `~/.gc/` на Windows) — это discoverability.
    На PermissionError/OSError тихо пропускает.
    """
    try:
        from config_modules.env_vars import gc_paths
        os.makedirs(gc_paths()["data"] / "skills", exist_ok=True)
    except (PermissionError, OSError):
        pass


def run(
    messages: list[str],
    ctx: RunContext,
    *,
    thread_id: str | None = None,
    start_new_thread: bool = False,
    verbose: bool | None = None,
) -> Any:
    """
    Основная функция одного запуска агента.
    Может вызываться как из CLI, так и из REST‑обёрток.

    messages: непустой список строк — каждая уходит отдельным user-сообщением
    (один запрос передавайте как ``[текст]``).
    """
    return AgentExecutor(ctx).run(
        messages,
        thread_id=thread_id,
        start_new_thread=start_new_thread,
        verbose=verbose,
    )


def _resolve_forced_skill(skill_name: str, config_dict: dict) -> str | None:
    """Resolve the -s/--skill flag deterministically (pack-aware).

    Returns the exact canonical skill id when resolved; the raw name as a
    soft fallback when it can't be resolved (unresolvable / ambiguous, or the
    catalog can't be built); or None when no -s was given. The agent always
    starts — an unresolved -s is a hint, not a hard contract — but the user
    is loudly warned on stderr so an explicitly requested -s is never
    silently dropped.
    """
    name = (skill_name or "").strip()
    if not name:
        return None
    from tools.skills.skill_arg_resolver import resolve_skill_arg, Resolved, Failed
    from tools.skills.registry import get_skills_registry

    try:
        catalog = get_skills_registry(config_dict).skill_catalog()
    except Exception as exc:  # noqa: BLE001 - surface as warning, not traceback
        print(
            f"warning: не удалось построить каталог скиллов для -s ({exc}); "
            f"агент стартует, '-s {name}' передан агенту как есть.",
            file=sys.stderr,
        )
        return name

    res = resolve_skill_arg(name, catalog)
    if isinstance(res, Failed):
        msg = (
            f"warning: -s '{name}' не разрешён детерминированно "
            f"({res.reason}); агент стартует, имя передано агенту как есть."
        )
        if res.suggestions:
            msg += " Возможно: " + ", ".join(res.suggestions) + "."
        print(msg, file=sys.stderr)
        return name
    if res.note:
        print(f"note: {res.note}", file=sys.stderr)
    return res.canonical_id


def _run_init() -> int:
    """`gc-cli --init-config`: создать ~/.gc/config.yml из шаблона и выйти, агент не
    запускается. Существующий конфиг не перетираем."""
    path = user_config_path()
    created = _bootstrap_user_config()
    if created:
        config_dict = load_config(None)
        print(onboarding.setup_guide(str(path), config_dict), file=sys.stderr)
    else:
        print(f"[gc] конфиг уже есть: {path} — не меняю", file=sys.stderr)
    return 0


_AGENT_ERROR_PREFIXES = (
    "Agent executed ",
    "Agent tools call errors.",
)


def _strip_code_fence(text: str) -> str:
    """Strip a wrapping ```[lang]\n...\n``` fence. Returns inner content or original."""
    stripped = text.strip()
    m = re.match(r"^```[\w]*\n(.+)\n```$", stripped, re.DOTALL)
    if m:
        return m.group(1)
    return stripped


def _extract_markdown(agent_result: Any) -> str:
    """Extract markdown from AgentExecutor.run() result.

    Fallback chain:
      (a) task_report field from finish_arguments JSON
      (b) handoff field from finish_arguments JSON
      (c) raw text response (text_response mode)
    Strips wrapping code fences.
    Returns "" for None, empty, or known agent error strings.
    """
    import json as _json

    if not agent_result:
        return ""

    raw = str(agent_result).strip()
    if not raw:
        return ""

    try:
        parsed = _json.loads(raw)
    except (_json.JSONDecodeError, TypeError):
        parsed = None

    if isinstance(parsed, dict):
        for key in ("task_report", "handoff"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return _strip_code_fence(value)
        return ""

    if any(raw.startswith(prefix) for prefix in _AGENT_ERROR_PREFIXES):
        return ""

    return _strip_code_fence(raw)


def _run_init_agents_md() -> int:
    """`gc-cli --init`: сгенерировать AGENTS.md сканом репозитория через агента."""
    from instructions.init_agents_md import run_init_agents_md
    import types

    config_dict = load_config(arg_parser.run_args.config)
    auth_connector = build_auth_connector(config_dict)
    ctx = create_run_context(config_dict, auth_connector=auth_connector)

    # ctx для confirm_port (CLI, не TUI)
    confirm_ctx = types.SimpleNamespace(
        is_tui=False,
        config_dict=config_dict,
        tui_confirm=None,
    )

    def generate_fn(prompt: str) -> str:
        executor = AgentExecutor(ctx)
        response = executor.run([prompt])
        return _extract_markdown(response)

    result = run_init_agents_md(cwd=os.getcwd(), ctx=confirm_ctx, generate_fn=generate_fn)
    if result["status"] == "cancelled":
        print("[gc] --init отменён", file=sys.stderr)
    else:
        print(f"[gc] AGENTS.md записан: {result['path']} (режим: {result['mode']})", file=sys.stderr)
    return 0


def main() -> int:
    """
    Входная точка CLI: парсинг аргументов, загрузка конфига,
    создание RunContext и запуск агента.

    Возвращает код выхода (0). Финальный ответ агента печатается в stdout
    при ``console_trace: true``; сырое значение доступно через ``cli.run()``.
    """
    _ensure_project_gc_dir()
    _ensure_user_skills_dir()

    if getattr(arg_parser.run_args, "init_config", False):
        return _run_init()

    if getattr(arg_parser.run_args, "init", False):
        return _run_init_agents_md()

    config_dict = load_config(arg_parser.run_args.config)

    # Единая точка применения CLI-override поверх config.yml.
    apply_cli_overrides(
        config_dict,
        extra_overrides=getattr(arg_parser, "extra_config_overrides", None),
        write_guard_enabled=arg_parser.run_args.write_guard,
        no_write_guard=arg_parser.run_args.no_write_guard,
        command_moderation_mode=arg_parser.run_args.command_moderation,
        read_guard_enabled=getattr(arg_parser.run_args, "read_guard", None),
        no_read_guard=getattr(arg_parser.run_args, "no_read_guard", False),
        read_guard_mode=getattr(arg_parser.run_args, "read_guard_mode", None),
        instructions_overwrite=getattr(arg_parser.run_args, "instructions_overwrite", False),
    )

    # Онбординг: дефолтный путь (без --config) и конфиг ещё не настроен
    # (остался плейсхолдер) → показать гайд и выйти, бэкенд не дёргаем.
    if arg_parser.run_args.config is None and onboarding.is_unconfigured(config_dict):
        print(
            onboarding.setup_guide(str(user_config_path()), config_dict),
            file=sys.stderr,
        )
        return 0
    forced_skill_id = _resolve_forced_skill(
        arg_parser.run_args.skill, config_dict
    )

    auth_connector = build_auth_connector(config_dict)
    auto_mcp = getattr(arg_parser.run_args, "auto_mcp", False)
    if auto_mcp:
        ensure_mcp_daemons(config_dict)
    ctx = create_run_context(config_dict, auth_connector=auth_connector)

    user_parts: list[str] = [arg_parser.run_args.query]
    if arg_parser.run_args.wrap_technical:
        user_parts[0] = wrap_technical_user_content(user_parts[0])
    if forced_skill_id:
        user_parts.append(
            wrap_skill_resolution_technical_instruction(forced_skill_id)
        )

    verbose = arg_parser.run_args.verbose
    thread_id = arg_parser.run_args.thread_id
    start_new_thread = arg_parser.run_args.new

    executor = AgentExecutor(ctx)
    try:
        response = executor.run(
            user_parts,
            thread_id=thread_id,
            start_new_thread=start_new_thread,
            verbose=verbose,
        )
    finally:
        if auto_mcp:
            shutdown_mcp_daemons()
    if config_dict.get("console_trace", True):
        formatted = format_json_for_display(response)
        if formatted:
            print(formatted)
    return 0


if __name__ == "__main__":
    # -q "создай подпапку test_project. в ней напиши на python модуль вычисления чисел фиббоначи. там же в отдельном файле напиши тесты. как прогонишь все тесты, запусти посчитай первые 1000е числео фиббоначи и сложи результат в json файл там же. Пиши быстрый код, чтобы кешировало расчеты и долго не считало. Скажи мне чему будет равно 1000-е числов фиббоначи, умноженное на 3."
    main()
