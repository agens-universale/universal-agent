"""TuiApp — Textual application."""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import List, Optional

from textual.app import App
from textual.binding import Binding
from textual.screen import Screen

from runtime_notices import (
    collect_runtime_notices,
    collect_unread_runtime_notices,
    register_runtime_notices_listener,
)
from startup_notices import Notice, collect_startup_notices
from tui import branding, palette
from tui.screens.main import TraceScreen
from tui.services.clipboard_ops import copy_out, with_source_cite
from tui.services.stream_sink import StreamSink
from tools.registry import all_builtin_tool_names

_AUTO_UPDATE_NOTICE = "Доступна новая версия — /settings → Обновление"


def _maybe_auto_check(config: dict, state: dict, now: float, installed_commit,
                      *, check, record_result, on_available, on_result=None) -> None:
    """Ядро авто-проверки обновления при старте (спека D, Task 8; персист-вердикт).

    Два сигнала:
    1. **Персистентный вердикт** (КАЖДЫЙ старт, без сети): если сохранённый remote-коммит
       последней проверки ≠ установленный → обновление доступно → `on_available(fresh=False)`
       (строка держится до обновления, а не только в день чека).
    2. **Свежая сетевая перепроверка** (КАЖДЫЙ старт, если `update.check: auto`): обновляет
       сохранённый remote; при найденном обновлении → `on_available(fresh=True)` (свежая
       находка → можно и toast). Сам вердикт уходит наружу через `on_result(CheckResult)` —
       из него живут глиф «Обновление» в хабе настроек и сама секция (второй раз в сеть
       ходить незачем). Сеть недоступна → вердикт `unknown`.

    Сеть/UI/время/персист — только через инъекцию (`check`, `record_result`, `on_available`,
    `on_result`), поэтому функция зовётся из фонового воркер-треда, не с UI-потока."""
    from updater.check import CheckResult, auto_check_enabled, update_available_from_state
    from updater.source import resolved_source

    resolved = resolved_source()
    if resolved is None:
        return                                   # dev/неизвестный источник — обновлений нет
    if update_available_from_state(state, installed_commit):
        on_available(False)                      # персист-вердикт: строка без toast
    if not auto_check_enabled(config):
        return
    try:
        result = check(resolved)
    except Exception:
        result = None
    if result is None:
        record_result(now, None, installed_commit)   # только стамп; прошлый remote сохраняется
        if on_result is not None:
            on_result(CheckResult("unknown", None, "не удалось проверить"))
        return
    record_result(now, result.remote_commit, installed_commit)
    if on_result is not None:
        on_result(result)
    if result.status == "update":
        on_available(True)                       # свежая находка → toast + строка


def _run_auto_check_safely(config: dict, state: dict, now: float, installed_commit,
                           *, check, record_result, on_available, on_result=None) -> None:
    """Best-effort обёртка воркера: авто-чек обновления НИКОГДА не роняет старт.

    Воркер стартует при каждом запуске TUI с дефолтным `exit_on_error=True`, а
    внутренний try в `_maybe_auto_check` покрывает только `check()`. Любое иное
    исключение (гейты, `record_result`, `on_available`-мост `call_from_thread` при
    shutdown-гонке) иначе дошло бы до Textual `_handle_exception` и уронило
    приложение — поэтому глотаем всё здесь."""
    try:
        _maybe_auto_check(config, state, now, installed_commit,
                          check=check, record_result=record_result,
                          on_available=on_available, on_result=on_result)
    except Exception:
        pass


class TuiApp(App):
    """GigaCode — основное приложение."""

    CSS_PATH = str(Path(__file__).parent / "styles.tcss")
    TITLE = branding.APP_NAME
    # У продукта своя палитра команд (Ctrl+K / ввод «/»). Встроенную палитру
    # Textual (Ctrl+P: Keys/Maximize/Quit/Screenshot/Theme) отключаем — она не
    # часть UX и перехватывает Ctrl+P раньше обработчиков экранов (нужен под превью).
    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        Binding("escape", "back", "Назад", show=False),
        Binding("f1", "help", "Help"),
        Binding("f2", "sessions", "/sessions"),
        Binding("f3", "diff", "/diff"),
        Binding("f4", "notices", "/notices"),
        Binding("f5", "config", "/config"),
        Binding("f6", "settings", "/settings", priority=True),  # у TextArea свой f6=select_line — без priority хоткей мёртв при фокусе в инпуте
        Binding("ctrl+slash", "help", "Help"),
        Binding("ctrl+s", "sessions", "/sessions"),
        # priority: TextArea перехватывает Ctrl+D как delete-right.
        Binding("ctrl+d", "diff", "/diff", priority=True),
        Binding("ctrl+o", "notices", "/notices"),
        Binding("ctrl+n", "new", "/new"),
        Binding("ctrl+g", "config", "/config"),
        # Ctrl+C: есть выделение → копировать; нет → отменить текущий run.
        # priority — перебивает копирование TextArea и системный help_quit.
        Binding("ctrl+c", "interrupt", "копировать/стоп", show=False, priority=True),
        # Страховка: терминал часто съедает хоткей — основной UX = copy-on-select.
        Binding("ctrl+shift+c", "copy_selection", "копировать", show=False, priority=True),
        Binding("super+c", "copy_selection", "копировать", show=False, priority=True),
    ]

    def __init__(
        self,
        *,
        threads_dir: Optional[str] = None,
        system: str = "cli",
        config: Optional[dict] = None,
    ) -> None:
        super().__init__()
        self._threads_dir = threads_dir
        self._system = system
        self._config = config or {}
        self._executor_factory = None  # callable returning an executor; for tests
        self._stream_sink = StreamSink()
        self._subagent_progress_sink = None  # QueueSubagentProgressSink; set in executor build
        self._mcp_progress = None
        self._mcp_snapshot = None      # boot-дискавери для /mcp (сервер→статус/тулы)
        self._mcp_status = None        # per-server status dict — для startup_notices
        self.active_runner = None      # AgentRunner | None
        self._runner_stale = False   # конфиг изменился при живом run → пересборка после завершения
        self.last_runner_error: Optional[str] = None
        self._session_log_path: Optional[str] = None
        self.last_terminal_outcome: Optional[str] = None
        self.last_stop_requested: bool = False   # юзер /stop по живому прогону → терминал «stopped»
        self._onboarding_mode = None          # None | "auto" | "review"; ставит tui/__init__.py
        self._config_path = None              # путь к конфигу на диске; задаётся снаружи после __init__
        self._onboarding_config_path = None   # куда писать конфиг (user_config_path)
        self._upgrade_info = None             # upgrade_gate.UpgradeInfo | None; ставит tui/__init__.py
        self._update_available = False        # async авто-чек нашёл новую версию (brand-строка + /notices)
        self._update_check_result = None      # CheckResult сессии: питает глиф хаба и секцию «Обновление»
        self._update_checking = False         # стартовый авто-чек в полёте (глиф хаба — «…»)
        from tui.services.file_view import ViewPositionStore
        from tui.state import TuiState
        self.tui_state = TuiState()
        self.view_positions = ViewPositionStore()
        # Один id на процесс TUI — для exclusive session lock (session-lock SPEC).
        self.tui_id = uuid.uuid4().hex
        self._clipboard_warn_shown = False  # native-fail hint раз за сессию (clipboard_ops)

    def get_css_variables(self) -> dict:
        """Питаем TCSS палитрой из tui.palette — единый источник цветов для CSS и
        Python-рендера. `$code`, `$ok`, … в styles.tcss резолвятся отсюда."""
        variables = super().get_css_variables()
        variables.update(palette.css_variables())
        return variables

    def on_mount(self) -> None:
        from tui.services.terminal_title import format_terminal_title, set_terminal_title
        set_terminal_title(format_terminal_title())
        self._start_auto_update_check()
        if not self._needs_onboarding():
            self._show_post_update_if_needed()

    def _start_auto_update_check(self) -> None:
        """Тонкая обёртка над `_maybe_auto_check`: реальные зависимости
        (check_for_update/record_check_result/upgrade_gate.read_state/install_source),
        запуск в фоновом воркер-треде — сеть не блокирует старт TUI, ошибки тихи."""
        import time
        import upgrade_gate
        import app_version
        from updater.check import check_for_update, record_check_result

        config = self._config or {}
        state = upgrade_gate.read_state()
        src = app_version.install_source()
        installed = src.commit if src else None

        def _on_available(fresh: bool) -> None:
            self._update_available = True     # постоянный сигнал: brand-строка + запись в /notices
            if self.is_running:
                if fresh:
                    self.notify(_AUTO_UPDATE_NOTICE, severity="information")   # toast только при свежей находке
                # M3: главный экран, а не активный (self.screen) — иначе, если авто-чек
                # приходит, пока сверху онбординг/PostUpdate/Settings, строка в карточке
                # так и не появится за всю сессию.
                scr = self._main_screen()
                if scr is not None and hasattr(scr, "refresh_update_notice"):
                    scr.refresh_update_notice()                # дорисовать строку в brand-блоке

        def _on_result(result) -> None:
            self._update_check_result = result   # вердикт сессии: хаб настроек и секция берут его
            self._refresh_update_glyph()

        def _done() -> None:
            self._update_checking = False        # проверка завершилась (в т.ч. по гейту/ошибке)
            self._refresh_update_glyph()

        self._update_checking = True
        self._refresh_update_glyph()

        def _work() -> None:
            try:
                _run_auto_check_safely(
                    config, state, time.time(), installed,
                    check=check_for_update,
                    record_result=record_check_result,
                    on_available=lambda fresh: self.call_from_thread(_on_available, fresh),
                    on_result=lambda r: self.call_from_thread(_on_result, r),
                )
            finally:
                try:
                    self.call_from_thread(_done)
                except Exception:
                    pass       # выход из приложения во время чека: event-loop уже закрыт,
                               # а поток жив — call_from_thread бросил бы RuntimeError

        self.run_worker(_work, thread=True, name="auto-update-check")

    def _refresh_update_glyph(self) -> None:
        """Перерисовать строку «Обновление» в хабе настроек, если он открыт: вердикт
        стартового чека приходит асинхронно, юзер может уже сидеть в настройках."""
        scr = self.screen if self.is_running else None
        if scr is not None and hasattr(scr, "refresh_update_glyph"):
            scr.refresh_update_glyph()

    def _needs_onboarding(self) -> bool:
        import onboarding
        return self._onboarding_mode == "review" or (
            self._onboarding_mode == "auto" and onboarding.is_unconfigured(self._config or {})
        )

    def get_default_screen(self) -> Screen:
        from tui.screens.onboarding import OnboardingScreen
        if self._needs_onboarding():
            return OnboardingScreen(
                config=self._config or {},
                config_path=self._onboarding_config_path,
                review_all=self._onboarding_mode == "review",
                on_commit=self._finish_onboarding,
            )
        return TraceScreen()

    def _show_post_update_if_needed(self) -> None:
        """Пуш пост-апдейт экрана (спека D, D4/Task 10) поверх базового экрана,
        если этот запуск — первый после апгрейда версии (`_upgrade_info`).

        Порядок: если требовался онбординг (обязательные поля не заполнены),
        сначала он — этот метод зовётся из `on_mount` только когда онбординг
        НЕ нужен, и из `_finish_onboarding` после его завершения. Идемпотентно:
        повторный вызов при уже смонтированном экране — no-op (защита от
        двойного push при повторном прогоне onboarding-финала)."""
        if self._upgrade_info is None:
            return
        from tui.screens.post_update import PostUpdateScreen
        if any(isinstance(s, PostUpdateScreen) for s in self.screen_stack):
            return
        self.push_screen(PostUpdateScreen(
            upgrade=self._upgrade_info,
            config=self._config or {},
            config_path=self._config_path,
        ))

    def reload_config(self) -> None:
        """Hot-apply (спека B, B3): перечитать конфиг с диска и применить к app.

        Executor в TUI кэшируется (get_runner: one per app) — без сброса
        active_runner следующий run продолжил бы жить на старом RunContext.
        Живой run не трогаем: помечаем stale, пересборка при следующем get_runner
        после завершения."""
        from config import load_config, apply_cli_overrides
        from thread_store import resolve_threads_dir
        from tui.runtime_config import carry_over_runtime_fields
        cfg = load_config(self._config_path)
        apply_cli_overrides(cfg)
        # launch_root и прочие поля рантайма TUI в файле конфига отсутствуют: без переноса
        # они исчезают при первом же сохранении настроек — /diff перестаёт работать, а
        # периметр path_policy съезжает со снапшота cwd на живой os.getcwd().
        carry_over_runtime_fields(self._config or {}, cfg)
        self._config = cfg
        self._threads_dir = resolve_threads_dir(cfg)
        runner = self.active_runner
        if runner is None:
            return
        if runner.is_running():
            self._runner_stale = True
        else:
            self.active_runner = None
            self._runner_stale = False

    def _finish_onboarding(self) -> None:
        """Финал онбординга (auto и review одинаково): reload_config + переход
        на главный экран — «завершил настройку → стартует агент»."""
        self._config_path = self._onboarding_config_path
        self.reload_config()
        self._onboarding_mode = None
        # OnboardingScreen — базовый экран (get_default_screen), у него нет result-callback,
        # из-за чего switch_screen падает на _pop_result_callback (IndexError: pop from empty).
        # Кладём заглушку — switch_screen штатно заменяет базовый экран свежим TraceScreen,
        # который прогреет executor уже на ПРАВИЛЬНОМ конфиге (при unconfigured-базе он бы
        # прогрелся на плейсхолдерах). Textual 8.2.7 (pinned).
        scr = self.screen
        if not scr._result_callbacks:
            scr._result_callbacks.append(None)
        self.switch_screen(TraceScreen())
        # Онбординг только что закрыл дорогу обязательным полям — теперь можно
        # показать пост-апдейт экран, если это первый запуск после апгрейда.
        self._show_post_update_if_needed()

    def get_runner(self):
        """Lazy-build the AgentRunner (one per app instance for MVP).

        Важно: executor (incl. MCP-дискавери с сетевыми вызовами) строится НЕ
        здесь — get_runner зовётся из submit-хендлера на UI-потоке, и синхронная
        сборка заморозила бы event-loop Textual. Передаём фабрику; AgentRunner
        построит executor в своём worker-потоке при первом start().
        """
        if (self.active_runner is not None and self._runner_stale
                and not self.active_runner.is_running()):
            self.active_runner = None
            self._runner_stale = False
        if self.active_runner is not None:
            return self.active_runner
        from tui.services.agent_runner import AgentRunner
        factory = self._executor_factory or self._build_real_executor

        # runner_ref заполняется сразу после конструктора: callback должен
        # игнорировать ошибки чужого/заменённого runner (см. AgentRunner._emit_error).
        runner_ref: list = []

        def _on_runner_error(exc: BaseException) -> None:
            runner = runner_ref[0] if runner_ref else None
            if runner is None or runner is not self.active_runner:
                return
            if getattr(runner, "_marked_stopped", False):
                return
            self.last_runner_error = str(exc) or exc.__class__.__name__

        def _on_runner_complete(outcome: Optional[str]) -> None:
            runner = runner_ref[0] if runner_ref else None
            if runner is None or runner is not self.active_runner:
                return
            self.last_terminal_outcome = outcome

        self.active_runner = AgentRunner(
            executor_factory=factory, on_error=_on_runner_error,
            on_complete=_on_runner_complete,
        )
        runner_ref.append(self.active_runner)
        self._runner_stale = False
        return self.active_runner

    def _build_real_executor(self):
        """Build the real AgentExecutor from config. Called when no factory injected."""
        from agent_executor import AgentExecutor
        from config import build_auth_connector
        from run_context import create_run_context
        from tui.services.mcp_progress import McpProgress
        cfg = dict(self._config) if self._config else {}
        cfg["console_trace"] = False
        from tui.services.prompt_assets import tui_special_instructions_path
        cfg["chat_completions_special_instructions_path"] = tui_special_instructions_path()
        # TUI adaptive bounce: сигнал в общий run_agent (только TUI ставит его).
        # CLI/server/swarm не ставят → строгий контракт. Управляется tui.text_answer_mode.
        cfg["text_answer_mode"] = cfg.get("tui_text_answer_mode", "judge")
        from tui.toggles import apply_finish_gate_mode
        apply_finish_gate_mode(cfg)   # /gate: off глушит судью только в TUI (спека §3)
        auth = build_auth_connector(cfg)
        progress = McpProgress()
        progress.begin(list((cfg.get("mcp-servers") or {}).keys()))
        self._mcp_progress = progress
        from tui.services.stream_pump import StreamPump, STREAM_REFRESH_MS
        self._stream_pump = StreamPump(
            self._stream_sink,
            signal=lambda: self.call_from_thread(self._stream_signal),
            throttle_s=STREAM_REFRESH_MS / 1000,
        )
        ctx = create_run_context(
            cfg, auth_connector=auth, on_progress=progress.update,
            on_stream_delta=self._stream_pump.on_delta,
            is_tui=True,
        )
        self._session_log_path = ctx.logging_path
        # Live nested progress субагентов: очередь на app, копия на ctx → child.
        from tools.subagents.progress import QueueSubagentProgressSink
        self._subagent_progress_sink = QueueSubagentProgressSink()
        ctx.subagent_progress_sink = self._subagent_progress_sink
        # Снапшот MCP «сервер→статус/тулы» app-уровнево — чтобы /mcp показывал
        # результат boot-дискавери сразу, не дожидаясь первого сообщения.
        from tools.mcp_snapshot import build_mcp_tools_snapshot
        self._mcp_snapshot = build_mcp_tools_snapshot(cfg, ctx.tools_data, ctx.mcp_status)
        self._mcp_status = ctx.mcp_status
        # Проводка confirm-канала: TUI-раннер умеет показывать баннер; CLI/server — нет.
        runner = self.active_runner
        if runner is not None and hasattr(runner, "make_tui_confirm"):
            ctx.tui_confirm = runner.make_tui_confirm(self._present_confirm)
        register_runtime_notices_listener(self._on_runtime_notice)
        return AgentExecutor(ctx)

    def _stream_signal(self) -> None:
        """Вызывается на UI-потоке (StreamPump → call_from_thread); событийная
        перерисовка контента стрима на активном экране (no-op без метода)."""
        fn = getattr(self.screen, "_refresh_stream_content", None)
        if callable(fn):
            fn()

    def _present_confirm(
        self,
        title: str,
        question: str,
        fields: list,
        via: str | None = None,
    ) -> bool:
        """Агент-поток → UI: баннер на TraceScreen. True = mounted, False = fail.

        Цель — базовый TraceScreen (не top-modal). Sync ack через return
        ``call_from_thread``: без mounted нельзя звать wait() (I2).
        """
        def _show() -> bool:
            screen = self._main_screen()
            if screen is None:
                return False
            fn = getattr(screen, "show_confirm_banner", None)
            if not callable(fn):
                return False
            try:
                try:
                    fn(title, question, fields, via=via)
                except TypeError:
                    fn(title, question, fields)
            except Exception:
                return False
            return True

        try:
            return bool(self.call_from_thread(_show))
        except Exception:
            return False

    # --- actions ------------------------------------------------------

    def action_back(self) -> None:
        if len(self.screen_stack) > 1:
            self.pop_screen()
        else:
            self.exit()

    def _on_main(self) -> bool:
        """Текущий экран — главный (TraceScreen). sessions открываются только отсюда."""
        return isinstance(self.screen, TraceScreen)

    def _startup_notices(self) -> List[Notice]:
        """Стартовые уведомления — единая сборка для полного списка и для бейджей."""
        cfg = self._config or {}
        return collect_startup_notices(
            cfg,
            all_builtin_tool_names(cfg),
            self._mcp_status,
            is_tui=True,
            deprecated_keys_used=cfg.get("deprecated_keys_used"),
            upgrade_info=self._upgrade_info,
            compat_notices=cfg.get("compat_notices"),
        )

    def _update_notice(self) -> List[Notice]:
        """«Доступна новая версия» как Notice — один владелец текста.

        Без ведущего ↑: NoticesScreen._notice_text сам подставляет глиф уровня (ℹ)."""
        if not self._update_available:
            return []
        return [Notice("info", "update_available",
                       "доступна новая версия · /settings → Обновление")]

    def collect_notices(self) -> List[Notice]:
        """Стартовые + runtime-уведомления (эфемерные, не сбрасываются при просмотре)."""
        return self._update_notice() + self._startup_notices() + collect_runtime_notices()

    def collect_unread_notices(self) -> List[Notice]:
        """Бейдж КАРТОЧКИ: стартовые + unread runtime.

        Обновления здесь нет намеренно: в карточке оно показано отдельной строкой
        (_compose_card_lines), и счётчик утверждал бы то же самое второй раз."""
        return self._startup_notices() + collect_unread_runtime_notices()

    def collect_footer_notices(self) -> List[Notice]:
        """Бейдж ФУТЕРА: то же, что в карточке, ПЛЮС обновление.

        В футере отдельной строки про обновление нет — без счётчика признак исчезал бы
        вместе с карточкой, как только трейс уедет вверх, и до перезапуска не вернулся."""
        return self._update_notice() + self.collect_unread_notices()

    def _main_screen(self):
        """Базовый (главный) экран — низ стека; None если это не TraceScreen."""
        base = self.screen_stack[0] if self.screen_stack else None
        return base if isinstance(base, TraceScreen) else None

    def _can_open_tool(self) -> bool:
        """config/diff открываются с главного экрана И с экрана уведомлений. Сами
        config/diff/справка — конечные: с них только F1-справка и Esc назад."""
        from tui.screens.notices import NoticesScreen
        return isinstance(self.screen, (TraceScreen, NoticesScreen))

    def check_action(self, action: str, parameters: "tuple[object, ...]") -> bool:
        """Гейт app-биндингов: Textual спрашивает его перед применением привязки и на
        False пропускает клавишу дальше — в `on_key` активного экрана.

        Без гейта priority-биндинг съедает клавишу даже там, где его action ничего не
        делает: priority-привязки проверяются ДО форварда события экрану, а Textual
        считает действие выполненным по факту существования метода — ранний `return`
        по гейту он не видит. Так пропадал Ctrl+D в секции MCP экрана /settings (там
        он удаляет пакет), хотя /diff с настроек всё равно не открывается.

        Экран, помеченный BLOCKS_GLOBAL_NAV, модален: мастер настройки и пост-апдейт
        живут до/вокруг записи конфига, и глобальная навигация поверх них показывала бы
        справку с неработающими клавишами и уведомления по ещё не записанному конфигу.
        """
        if getattr(self.screen, "BLOCKS_GLOBAL_NAV", False):
            return action in ("back", "interrupt")   # Esc и Ctrl+C остаются
        if action == "diff":
            from tui.screens.sessions import SessionsScreen
            # На /sessions Ctrl+D — массовое удаление; action_diff это учитывает.
            return self._can_open_tool() or isinstance(self.screen, SessionsScreen)
        return True

    def action_help(self) -> None:
        from tui.screens.help import HelpScreen
        # Справка доступна с любого экрана, КРОМЕ самой справки (без help-на-help).
        if isinstance(self.screen, HelpScreen):
            return
        self.push_screen(HelpScreen())

    def action_sessions(self) -> None:
        if not self._on_main():
            return
        if not self._threads_dir:
            self.notify("threads_dir не задан", severity="warning")
            return
        from tui.screens.sessions import SessionsScreen
        self.push_screen(SessionsScreen(
            threads_dir=self._threads_dir, system=self._system,
        ))

    def action_config(self) -> None:
        main = self._main_screen()
        if main is None or not self._can_open_tool():
            return
        main._cmd_config()

    def action_settings(self) -> None:
        main = self._main_screen()
        if main is None or not self._can_open_tool():
            return
        main._cmd_settings()

    def action_notices(self) -> None:
        from tui.screens.notices import NoticesScreen
        if isinstance(self.screen, NoticesScreen):
            return
        self.push_screen(NoticesScreen())

    def action_diff(self) -> None:
        from tui.screens.diff import DiffScreen
        from tui.screens.sessions import SessionsScreen

        # На /sessions Ctrl+D — массовое удаление (не перехватываем в /diff).
        if isinstance(self.screen, SessionsScreen):
            self.screen._open_mass_delete()
            return
        if isinstance(self.screen, DiffScreen):
            return
        main = self._main_screen()
        if main is None or not self._can_open_tool():
            return
        main._cmd_diff()

    def refresh_notices_indicators(self) -> None:
        """Обновить счётчики уведомлений в карточке главного экрана и футере активного."""
        main = self._main_screen()
        if main is not None and hasattr(main, "_refresh_notices_card"):
            main._refresh_notices_card()
        fn = getattr(self.screen, "_refresh_notices_footer", None)
        if callable(fn):
            fn()

    def _on_runtime_notice(self) -> None:
        """Callback из worker-потока: обновить счётчики уведомлений."""
        self.call_from_thread(self.refresh_notices_indicators)

    def action_new(self) -> None:
        if not self._on_main():
            return
        self.screen._cmd_new()

    def _current_selection(self) -> str:
        """Текст текущего выделения: сначала клавиатурное в фокусном вводе,
        затем мышиное по экрану (история вывода)."""
        focused = self.focused
        sel = getattr(focused, "selected_text", "") or ""
        if sel:
            return sel
        try:
            return self.screen.get_selected_text() or ""
        except Exception:
            return ""

    def _clipboard_payload(self) -> str:
        """Выделение + опциональный суффикс источника с view/diff экранов."""
        sel = self._current_selection()
        if not sel:
            return ""
        cite = getattr(self.screen, "clipboard_source_cite", None)
        if not callable(cite):
            return sel
        try:
            info = cite()
        except Exception:
            return sel
        if not info:
            return sel
        kind, path = info
        return with_source_cite(sel, kind=kind, path=path)

    def action_copy_selection(self) -> None:
        """Ctrl+Shift+C / Super+C: copy selection; пусто → no-op (не stop)."""
        payload = self._clipboard_payload()
        if payload:
            copy_out(self, payload, toast=True)

    def on_text_selected(self, event) -> None:
        """Copy-on-select: drag без Shift → внешний буфер (тихий успех).

        TextSelected не шлётся из Input/TextArea — дубля с focused.selected_text нет.
        """
        payload = self._clipboard_payload()
        if payload:
            copy_out(self, payload, toast=False)

    def action_interrupt(self) -> None:
        """Ctrl+C: выделение → копировать; активный run → стоп; вхолостую → exit-модалка (как Esc)."""
        payload = self._clipboard_payload()
        if payload:
            copy_out(self, payload, toast=True)
            return
        scr = self.screen
        runner = getattr(self, "active_runner", None)
        if runner is not None and runner.is_running():
            if hasattr(scr, "_cmd_stop"):
                scr._cmd_stop()
        elif hasattr(scr, "_request_exit"):
            scr._request_exit()
