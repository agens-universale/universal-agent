"""gc-ua — entrypoint.

Тонкий парсинг startup-аргументов (--config, --task-root) + сборка
стека: load_config → threads_dir → TuiApp.
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from typing import Optional, Sequence


# Одна строка — не косметика: packages/tui/tests/test_runner_no_textual.py
# требует err.count("\n") <= 1, чтобы падение точки входа оставалось
# однострочным. Команда собирается из PROFILE_SUBDIRS: подкаталог здесь уже
# один раз протух при переименовании каталога пакетов.
def _missing_textual_msg() -> str:
    from gc_core.app_version import install_hint

    return (
        "TUI не установлен (нет пакета textual). Поставьте профиль с TUI: "
        + install_hint("gc-tui")
    )


def _interactive() -> bool:
    """TTY на входе и выходе → можно запускать интерактивный визард."""
    import sys as _sys

    return _sys.stdin.isatty() and _sys.stdout.isatty()


def _force_truecolor_if_supported() -> None:
    """Подсказать Textual использовать 24-bit на терминалах, где truecolor точно
    есть (Windows Terminal / VS Code / COLORTERM). Иначе авто-детект может выбрать
    256 цветов, и поколоночный градиент логотипа квантуется в видимые «ступеньки»
    (наблюдалось на Windows; на macOS truecolor определяется сам). `setdefault` не
    перетирает явный пользовательский TEXTUAL_COLOR_SYSTEM."""
    if (
        os.environ.get("WT_SESSION")
        or os.environ.get("TERM_PROGRAM") == "vscode"
        or os.environ.get("COLORTERM") in ("truecolor", "24bit")
    ):
        os.environ.setdefault("TEXTUAL_COLOR_SYSTEM", "truecolor")


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entrypoint для `gc-ua = "tui:main"`."""
    args_list = list(argv) if argv is not None else sys.argv[1:]
    # `gc-ua config …` — до TUI: без textual, агента, MCP и сети.
    if args_list[:1] == ["config"]:
        from gc_core.config_command import main as config_main

        return config_main(args_list[1:], program="gc-ua")
    _force_truecolor_if_supported()
    try:
        # importlib.import_module goes through sys.modules, not the package
        # attribute cache, so evicting tui.app in tests forces a real re-import.
        _app_mod = importlib.import_module("gc_tui.app")
    except ImportError:
        print(_missing_textual_msg(), file=sys.stderr)
        return 1

    parser = argparse.ArgumentParser(prog="gc-ua", add_help=False)
    parser.add_argument("--config", default=None)
    parser.add_argument("--task-root", default=None)
    parser.add_argument(
        "--profile",
        type=str,
        default=None,
        help=(
            "Активировать профиль <имя> поверх указателя profile: из config.yml "
            "(~/.gc/profiles/ или <project>/.gc/profiles/)"
        ),
    )
    parser.add_argument(
        "--init-config",
        action="store_true",
        help="Создать ~/.gc/config.yml из шаблона и выйти (TUI не запускается)",
    )
    # По умолчанию поднимаем HTTP/SSE-daemon'ы для MCP из конфига
    # (пересечение с манифестом recommended_mcp). Отключить: --no-auto-mcp.
    # -a/--auto-mcp оставлены для совместимости (no-op при default=True).
    parser.set_defaults(auto_mcp=True)
    parser.add_argument(
        "-a",
        "--auto-mcp",
        action="store_true",
        dest="auto_mcp",
        help=(
            "Поднять HTTP/SSE MCP-daemon'ы для серверов из mcp-servers "
            "(по умолчанию включено; пересечение с манифестом recommended_mcp); "
            "по завершении гасятся только процессы этого запуска"
        ),
    )
    parser.add_argument(
        "--no-auto-mcp",
        action="store_false",
        dest="auto_mcp",
        help="Не поднимать HTTP/SSE MCP-daemon'ы при старте",
    )
    args, _unknown = parser.parse_known_args(args_list)

    # Паритет с gc-cli: создать <cwd>/.gc/ (project-local слой: config overlay,
    # project skills). Тихо игнорит отсутствие прав на запись в cwd.
    from gc_core.config_modules.env_vars import (
        ensure_project_gc_dir,
        project_local_config_path,
    )

    ensure_project_gc_dir()

    from gc_core import onboarding
    from gc_core.config import (
        _bootstrap_user_config,
        apply_cli_overrides,
        flat_config,
        load_settings,
        resolve_config_path,
        user_config_path,
    )

    onb_write_path = user_config_path()
    onboarding_mode = None
    onboarding_deactivate_profile = False
    onboarding_degraded = None

    if getattr(args, "init_config", False):
        just_created = _bootstrap_user_config()
        # «Чинилка» обязана работать именно тогда, когда конфиг сломан: лестница
        # ступеней и текст деградации — общие с gc-cli (onboarding, один дом).
        # Целится она в ПОЛЬЗОВАТЕЛЬСКИЙ файл (`onb_write_path`) — тот же, в
        # который ниже пишет мастер, поэтому --config/--profile здесь не в счёт:
        # читать один файл, а писать в другой значило бы показать человеку поля
        # не того конфига, который он чинит.
        try:
            loaded = onboarding.load_config_for_repair()
        except onboarding.RepairRefused as exc:
            # M10: ступень шаблона при живом указателе профиля — запись мастера
            # обречена (гейт судит стек следующего старта): человек прошёл бы
            # весь экран мастера с вводом токена и упёрся в отказ записи.
            # Отказ ДО создания TuiApp, текст готов у лестницы.
            print(str(exc), file=sys.stderr)
            return 1
        except ValueError as exc:
            # Лестница исчерпана: не собирается и шаблон — сломан слой, в
            # который мастер не пишет (оверлей). Чинить его мастером нечем: то
            # же объяснение, что на обычном старте, и код 1 — не трейсбек.
            print(
                onboarding.explain_broken_config(
                    str(onb_write_path), exc, program="gc-ua"
                ),
                file=sys.stderr,
            )
            return 1
        settings, profile_meta = loaded.settings, loaded.meta
        config_dict = loaded.config
        if not _interactive():
            # TUI не стартует — stderr доезжает до человека и остаётся на экране.
            # Гайд знает ступень лестницы: на деградировавшей «настроен — можно
            # запускать» врало бы человеку, чей следующий старт падает.
            if loaded.message:
                print(f"[gc] {loaded.message}", file=sys.stderr)
            print(
                onboarding.setup_guide(
                    str(onb_write_path),
                    config_dict,
                    program="gc-ua",
                    just_created=just_created,
                    repair=loaded,
                ),
                file=sys.stderr,
            )
            return 0
        if loaded.message:
            # А здесь stderr не живёт: через мгновение Textual занимает экран
            # альтернативным буфером и стирает строку — человек не видит НИЧЕГО
            # и не понимает, почему поля мастера ведут себя странно (живой смок
            # это подтвердил). Показываем НА САМОМ ЭКРАНЕ мастера, плашкой рядом с
            # вердиктом о перекрывающем слое: рантайм-уведомления под мастером
            # недостижимы — `OnboardingScreen.BLOCKS_GLOBAL_NAV` гасит F4 намеренно
            # (справка с неработающими клавишами и уведомления по ещё не записанному
            # конфигу), и первый раз текст всплывал уже ПОСЛЕ финала, когда устаревал,
            # а при выходе по Esc — не всплывал никогда.
            onboarding_degraded = loaded.message
            # Уведомление остаётся: после финала навигация разблокирована, и там
            # это уместная история запуска — почему конфиг собрался не целиком.
            from gc_core.runtime_notices import push_runtime_notice
            from gc_core.startup_notices import Notice

            push_runtime_notice(
                Notice(
                    "warning",
                    "config_load_degraded",
                    loaded.message,
                    ("config.yml",),
                )
            )
        # Ступень «без профиля» значит, что конфиг собрался только со снятым
        # профилем: финал мастера обязан снять и указатель, иначе гейт записи
        # отвергнет собранное (стек следующего старта поднимет тот же профиль) —
        # чинилка дойдёт до конца и не запишет свою починку. Но снять его мастер
        # может не всегда (указатель в managed-слое), и судит об этом лестница:
        # своя редакция признака обещала бы на экране подтверждения деактивацию,
        # которую писатель не сделает, — а пустой якорь в базу уже уехал бы (D3).
        onboarding_deactivate_profile = loaded.deactivate_profile
        onboarding_mode = "review"
    else:
        just_created = (
            _bootstrap_user_config() if args.config is None else False
        )
        try:
            settings = load_settings(
                args.config, profile_override=args.profile
            )
        except ValueError as exc:
            # Нерезолвимый конфиг — объяснение в stderr и выход, а не голый
            # traceback: падение здесь случается ДО создания TuiApp, так что
            # собственный crash-репорт Textual не включится (см. explain_broken_config).
            # Профиль — тот же, что грузили: по указателю из файла объяснение
            # описывало бы чужой стек.
            print(
                onboarding.explain_broken_config(
                    resolve_config_path(args.config),
                    exc,
                    program="gc-ua",
                    profile_override=args.profile,
                ),
                file=sys.stderr,
            )
            return 1
        settings = apply_cli_overrides(settings)
        config_dict, profile_meta = (
            flat_config(settings),
            settings.profile_meta,
        )

        if args.config is None and onboarding.is_unconfigured(config_dict):
            if not _interactive():
                print(
                    onboarding.setup_guide(
                        str(onb_write_path),
                        config_dict,
                        program="gc-ua",
                        just_created=just_created,
                    ),
                    file=sys.stderr,
                )
                return 0
            onboarding_mode = "auto"

    # Поля, вычисляемые рантаймом TUI (в файле конфига их нет) — владелец
    # tui/runtime_config; тот же модуль переносит их через hot-reload настроек.
    from gc_tui.runtime_config import apply_tui_runtime

    settings = apply_tui_runtime(settings)
    config_dict = flat_config(settings)

    from gc_core.thread_store import resolve_threads_dir

    threads_dir = resolve_threads_dir(config_dict)

    from recommended_mcp.runtime.daemon_autostart import (
        ensure_mcp_daemons,
        kill_mcp_daemons_now,
        shutdown_mcp_daemons,
    )

    auto_mcp = args.auto_mcp
    if auto_mcp:
        ensure_mcp_daemons(
            config_dict,
            disable_hint="отключить автозапуск целиком: `--no-auto-mcp`",
        )

    voice_expected = False
    _release_voice = None
    try:
        from gc_tui.voice_runtime.autostart import (
            release_voice_service,
            url_is_ours,
        )
        from gc_tui.voice_runtime.state import load_state as _voice_state

        _release_voice = release_voice_service
        _vs = _voice_state()
        voice_expected = _vs is not None and url_is_ours(settings, _vs.port)
    except Exception:
        voice_expected = False

    # Флаг запуска доезжает до приложения: `reload_config` отдаёт его КАЖДОМУ
    # перечитыванию, иначе выбор живёт до первого сохранения из /settings —
    # и совет «запустить без профиля, ничего не потеряв» переставал работать
    # ровно там, где человек начинал чинить. У чинилки флага нет: она читает и
    # пишет ПОЛЬЗОВАТЕЛЬСКИЙ файл, и `--profile` там не в счёт (см. выше).
    app = _app_mod.TuiApp(
        threads_dir=threads_dir,
        system="cli",
        settings=settings,
        profile_override=None if args.init_config else args.profile,
    )
    app.set_config_path(
        onb_write_path if onboarding_mode else resolve_config_path(args.config)
    )
    app._config_project_local = project_local_config_path()  # str | None
    # Подъём (`ensure_voice_service`: spawn + ожидание готовности, до 90 с на холодном
    # старте) идёт ЦЕЛИКОМ фоном: TuiApp запускает его после первого кадра
    # (on_mount → _start_voice_autostart), а main() только считает флаг ожидания —
    # спавна здесь нет вовсе. Раньше вызов стоял тут СИНХРОННО и держал пустой терминал
    # перед `app.run()` весь бюджет ожидания (было 15 с, стало 90 — живая приёмка
    # поймала «программа зависла»).
    app._voice_autostart_pending = voice_expected
    # Спека §3.5: холодный старт с проектным профилем, выбранным самим config.yml
    # (не сессионным переключением) — тот же notice, что и сессионный путь
    # (`TuiApp._profile_remerge`), но здесь ProfileMeta есть только на этом
    # единственном старте загрузки, до создания app/треда.
    app._maybe_notice_profile_activation(
        profile_meta.active_name or "", profile_meta, settings
    )
    app._onboarding_mode = onboarding_mode
    app._onboarding_deactivate_profile = onboarding_deactivate_profile
    app._onboarding_degraded = onboarding_degraded
    app._onboarding_config_path = onb_write_path
    from gc_core import upgrade_gate

    app._upgrade_info = upgrade_gate.detect_upgrade()
    if app._upgrade_info is None:
        # Нет апгрейда в этом запуске — стампим сразу (база для будущего
        # detect_upgrade: первый запуск / повтор той же версии).
        upgrade_gate.mark_current_run()
    # Если апгрейд ЕСТЬ — стамп откладывается до закрытия PostUpdateScreen
    # (спека D, Task 10): иначе крэш до показа экрана «съедает» уведомление
    # об апгрейде на следующем запуске.

    def _shutdown_runtime() -> None:
        # Процессы индекса путей (git ls-files) — первыми: поток сборки индекса — демон
        # и умрёт вместе с процессом, а сам git пережил бы программу, дочитывая
        # репозиторий без хозяина. Быстро и не бросает: ошибки гашения глотаются внутри.
        from gc_tui.services import file_index

        file_index.kill_running_processes()
        # Осознанно две РАЗНЫЕ модели гашения демонов: voice — light-switch
        # (lease-файлы, гасит последний живой клиент, безопасно при N TUI);
        # MCP — per-process («гашу только то, что сам поднял»). Не унифицировано:
        # lease-модель для MCP — кандидат в апстрим (develop).
        # MCP раньше голоса: зависни голосовой шаг, демоны уже получили бы SIGTERM и
        # свой SIGKILL — аварийный выход ограничен бюджетом, и шаги после зависшего
        # не наступают. Голос — в finally: упавшее гашение MCP его не отменяет.
        try:
            if auto_mcp:
                shutdown_mcp_daemons()
        finally:
            # release всегда (не только при voice_ok): прибирает «опоздавший» сервис,
            # поднявшийся после таймаута, и мёртвые lease прошлых запусков.
            # Безусловность безопасна ровно потому, что гасит он только ПОМЕЧЕННЫЙ
            # агентский сервис (`leases.is_agent_owned`): форграундный `gc-voice serve`
            # человека переживёт выход из TUI, который голоса и не касался.
            if _release_voice is not None:
                try:
                    _release_voice()
                except Exception:
                    pass

    # Уборка одна на оба выхода: штатный и аварийный — когда терминал умер, а
    # Textual навсегда встал на записи в него (см. gc_tui.services.terminal_guard).
    # Не успеет аварийная уборка в бюджет — сторож добьёт MCP-демонов этого запуска
    # перед выходом. Голос сюда не входит: сервис общий для нескольких TUI.
    from gc_tui.services.terminal_guard import TerminalGuard

    guard = TerminalGuard(
        _shutdown_runtime,
        force_kill=kill_mcp_daemons_now if auto_mcp else None,
    )
    try:
        guard.install()
        app.run()
    finally:
        guard.close()
    return 0
