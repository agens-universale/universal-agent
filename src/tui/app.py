"""TuiApp — Textual application."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from textual.app import App
from textual.binding import Binding
from textual.screen import Screen

from runtime_notices import register_runtime_notices_listener
from tui import branding, palette
from tui.screens.main import TraceScreen
from tui.services.stream_sink import StreamSink


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
        Binding("f2", "config", "/config"),
        Binding("f3", "mcp", "/mcp"),
        Binding("f4", "notices", "/notices"),
        Binding("ctrl+n", "new", "/new"),
        Binding("ctrl+s", "sessions", "/sessions"),
        # Ctrl+C: есть выделение → копировать; нет → отменить текущий run.
        # priority — перебивает копирование TextArea и системный help_quit.
        Binding("ctrl+c", "interrupt", "копировать/стоп", show=False, priority=True),
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
        self._mcp_progress = None
        self._mcp_snapshot = None      # boot-дискавери для /mcp (сервер→статус/тулы)
        self._mcp_status = None        # per-server status dict — для startup_notices
        self.active_runner = None      # AgentRunner | None
        self.last_runner_error: Optional[str] = None
        self.last_terminal_outcome: Optional[str] = None
        self.last_stop_requested: bool = False   # юзер /stop по живому прогону → терминал «stopped»
        from tui.state import TuiState
        self.tui_state = TuiState()

    def get_css_variables(self) -> dict:
        """Питаем TCSS палитрой из tui.palette — единый источник цветов для CSS и
        Python-рендера. `$code`, `$ok`, … в styles.tcss резолвятся отсюда."""
        variables = super().get_css_variables()
        variables.update(palette.css_variables())
        return variables

    def on_mount(self) -> None:
        from tui.services.terminal_title import format_terminal_title, set_terminal_title
        set_terminal_title(format_terminal_title())

    def get_default_screen(self) -> Screen:
        return TraceScreen()

    def get_runner(self):
        """Lazy-build the AgentRunner (one per app instance for MVP).

        Важно: executor (incl. MCP-дискавери с сетевыми вызовами) строится НЕ
        здесь — get_runner зовётся из submit-хендлера на UI-потоке, и синхронная
        сборка заморозила бы event-loop Textual. Передаём фабрику; AgentRunner
        построит executor в своём worker-потоке при первом start().
        """
        if self.active_runner is not None:
            return self.active_runner
        from tui.services.agent_runner import AgentRunner
        factory = self._executor_factory or self._build_real_executor

        def _on_runner_error(exc: BaseException) -> None:
            self.last_runner_error = str(exc) or exc.__class__.__name__

        def _on_runner_complete(outcome: Optional[str]) -> None:
            self.last_terminal_outcome = outcome

        self.active_runner = AgentRunner(
            executor_factory=factory, on_error=_on_runner_error,
            on_complete=_on_runner_complete,
        )
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
        )
        # Снапшот MCP «сервер→статус/тулы» app-уровнево — чтобы /mcp показывал
        # результат boot-дискавери сразу, не дожидаясь первого сообщения.
        from tools.mcp_snapshot import build_mcp_tools_snapshot
        self._mcp_snapshot = build_mcp_tools_snapshot(cfg, ctx.tools_data, ctx.mcp_status)
        self._mcp_status = ctx.mcp_status
        # Проводка confirm-канала: TUI-раннер умеет показывать баннер; CLI/server — нет.
        runner = self.active_runner
        if runner is not None and hasattr(runner, "make_tui_confirm"):
            ctx.is_tui = True
            ctx.tui_confirm = runner.make_tui_confirm(self._present_confirm)
        register_runtime_notices_listener(self._on_runtime_notice)
        return AgentExecutor(ctx)

    def _stream_signal(self) -> None:
        """Вызывается на UI-потоке (StreamPump → call_from_thread); событийная
        перерисовка контента стрима на активном экране (no-op без метода)."""
        fn = getattr(self.screen, "_refresh_stream_content", None)
        if callable(fn):
            fn()

    def _present_confirm(self, title: str, question: str, fields: list) -> None:
        """Вызывается на агент-потоке; маршаллит показ баннера в UI-поток."""
        def _show() -> None:
            fn = getattr(self.screen, "show_confirm_banner", None)
            if fn is not None:
                fn(title, question, fields)
        self.call_from_thread(_show)

    # --- actions ------------------------------------------------------

    def action_back(self) -> None:
        if len(self.screen_stack) > 1:
            self.pop_screen()
        else:
            self.exit()

    def _on_main(self) -> bool:
        """Текущий экран — главный (TraceScreen). notices/sessions открываются
        только отсюда и не стекаются."""
        return isinstance(self.screen, TraceScreen)

    def _main_screen(self):
        """Базовый (главный) экран — низ стека; None если это не TraceScreen."""
        base = self.screen_stack[0] if self.screen_stack else None
        return base if isinstance(base, TraceScreen) else None

    def _can_open_tool(self) -> bool:
        """config/mcp открываются с главного экрана И с экрана уведомлений. Сами
        config/mcp/справка — конечные: с них только F1-справка и Esc назад."""
        from tui.screens.notices import NoticesScreen
        return isinstance(self.screen, (TraceScreen, NoticesScreen))

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

    def action_mcp(self) -> None:
        main = self._main_screen()
        if main is None or not self._can_open_tool():
            return
        main._cmd_mcp()

    def action_notices(self) -> None:
        from tui.screens.notices import NoticesScreen
        if not self._on_main():
            return
        scr = self.screen
        notices = scr._collect_notices() if hasattr(scr, "_collect_notices") else []
        self.push_screen(NoticesScreen(notices=notices))

    def _on_runtime_notice(self) -> None:
        """Callback из worker-потока: обновить счётчик уведомлений в карточке."""
        def _refresh() -> None:
            scr = self.screen
            if isinstance(scr, TraceScreen) and hasattr(scr, "_refresh_notices_card"):
                scr._refresh_notices_card()
        self.call_from_thread(_refresh)

    def action_new(self) -> None:
        scr = self.screen
        if hasattr(scr, "_cmd_new"):
            scr._cmd_new()

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

    def action_interrupt(self) -> None:
        """Ctrl+C: есть выделение → копировать в буфер; иначе → остановить run."""
        sel = self._current_selection()
        if sel:
            self.copy_to_clipboard(sel)  # OSC 52 + внутренний app.clipboard (SSH/удалённые)
            from tui.services.os_ops import write_clipboard
            write_clipboard(sel)         # нативный ОС-буфер (локальные терминалы без OSC 52)
            return
        scr = self.screen
        if hasattr(scr, "_cmd_stop"):
            scr._cmd_stop()
