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
    push_runtime_notice,
    register_runtime_notices_listener,
)
from startup_notices import Notice, collect_startup_notices
from tui import branding, palette
from tui.screens.main import TraceScreen
from tui.services.clipboard_ops import copy_out, with_source_cite
from tui.services.stream_sink import StreamSink
from tools.registry import all_builtin_tool_names

_AUTO_UPDATE_NOTICE = "Доступна новая версия — /settings → Обновление"

# Ключи схемы, применяемые только на старте приложения (спека §3.2): живая пересборка
# executor их видит (профиль/reload могут их менять), но эффект — только со следующего
# запуска (paths.threads_data_path привязывается при старте TUI, logs_dump_dir — при
# создании логгера рана). dotted-имя — из схемы AppConfig (для notice); справа — плоский
# ключ config_dict (`flatten_app_config`, src/config.py), по которому идёт сравнение.
BOOT_FROZEN_KEYS = ("paths.threads_data_path", "paths.logs_dump_dir")
_BOOT_FROZEN_FLAT = {
    "paths.threads_data_path": "threads_data_path",
    "paths.logs_dump_dir": "logs_dump_dir",
}


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
        Binding("f7", "buffer", "Буфер сессии", priority=True),  # TextArea f7=select_all — без priority F7 в инпуте не откроет буфер
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
        # Сессионные переопределения конфига (спека 2026-07-20-session-scoped-settings):
        # база — self._config (файл), это — то, что переключено в текущем треде.
        # Хранятся по dotted-именам; накладываются в effective_config().
        self._session_overrides: dict = {}
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
        # Мтайм-набор активного указателя (спека §3.3): база/managed/проектный конфиг +
        # оба кандидатных пути активного профиля. Расширение прежнего одиночного
        # _config_mtime — тот же снимок, что строит load_config_ex, но снятый дёшево
        # (без парсинга) там, где полная загрузка не нужна (set_config_path).
        self._config_mtime_set: dict = {}
        # Кэш пере-мержа сессионного профиля (спека §9.1): (ключ, cfg). Ключ — снимок
        # mtime-набора + имя оверрайда; effective_config() зовётся каждый кадр и не
        # должен гонять load_config_ex, пока ни файлы, ни выбор не изменились.
        self._session_profile_cache = None
        # Профили, уже отдавшие notice активации в этой сессии (спека §3.5) — не спамить
        # повторно при каждом пере-мерже одного и того же выбора.
        self._profile_notice_seen: set = set()
        # Источник действующего профиля на момент последней сборки базы (спека §3.1):
        # (имя, путь, корень). Нужен ИМЕННО снимком — `active_profile_ref()` резолвит
        # по диску, и после появления тёзки «прежнее» значение уже не восстановить.
        self._profile_source: Optional[tuple] = None
        from model_router import CircuitBreaker
        # Сессионный предохранитель моделей (спека §8) — владелец: сессия-тред;
        # персист/restore — persist_circuit_breaker()/load_circuit_breaker() (meta треда).
        self._circuit_breaker = CircuitBreaker()
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
        self._select_copy_timer = None  # O9: debounce copy-on-select
        self._pending_select_copy: Optional[str] = None

    # Debounce copy-on-select: не дергать subprocess на каждый MouseUp.
    _COPY_ON_SELECT_DEBOUNCE_S = 0.15

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

    def set_config_path(self, path: Optional[str]) -> None:
        """Единая точка присвоения `_config_path`: путь + снимок мтайм-набора (спека
        §3.3) одним движением. Вызывать ровно тогда, когда self._config уже фактически
        соответствует файлу по этому пути (main() — конфиг только что загружен
        из него; финал онбординга — следом сразу идёт reload_config()).

        Без немедленного снимка первая же проверка reload_config_if_changed()
        в новом окне видит файл как «изменившийся» (снимка ещё нет) и зря
        перечитывает конфиг и инвалидирует раннер — хотя окно только что само
        прочитало этот же файл."""
        self._config_path = path
        self._config_mtime_set = self._compute_mtime_set()
        self._profile_source = self._current_profile_source()

    def _current_profile_source(self) -> Optional[tuple]:
        """`(имя, путь, корень)` действующего профиля или None. Снимок для
        сравнения «до/после» в `reload_config` — резолв идёт по диску, поэтому
        прежнее значение обязано быть сохранено заранее, а не пере-вычислено."""
        try:
            ref = self.active_profile_ref()
        except Exception:
            return None
        return None if ref is None else (ref.name, str(ref.path), ref.root_kind)

    def _active_profile_name_for_mtime(self) -> Optional[str]:
        """Действующее имя профиля для мтайм-набора (спека §3.3 — «выбор, не
        снимок»): сессионный оверрайд, если задан, ИНАЧЕ базовый указатель.

        Набор обязан следить за файлом ТОГО профиля, что реально мержится сейчас
        (effective), а не только за пойнтером из файла — иначе правка файла
        сессионно выбранного профиля никогда не всплывёт в
        `reload_config_if_changed` (ревью Task 11: до этой правки набор всегда
        брал имя только из `self._config["profile"]`, игнорируя сессионный
        выбор — редактирование выбранного в сессии профиля было невидимо
        бессрочно)."""
        override = self._session_overrides.get("profile")
        if override:
            return override
        return (self._config or {}).get("profile") or None

    def _compute_mtime_set(self) -> dict:
        """Снимок мтайм-набора ДЕЙСТВУЮЩЕГО указателя (спека §3.3) — только
        `stat()`, без парсинга/валидации конфига (используется в set_config_path/
        reload_config/reload_config_if_changed — связывание пути и проверка
        свежести не обязаны гонять полную загрузку ради снимка)."""
        if not self._config_path:
            return {}
        from config import profile_mtime_set
        try:
            return profile_mtime_set(self._config_path, self._active_profile_name_for_mtime())
        except OSError:
            return {}

    def reload_config(self) -> None:
        """Hot-apply (спека B, B3): перечитать конфиг с диска и применить к app.

        Executor в TUI кэшируется (get_runner: one per app) — без сброса
        active_runner следующий run продолжил бы жить на старом RunContext.
        Живой run не трогаем: помечаем stale, пересборка при следующем get_runner
        после завершения.

        Профили v2 (спека §3.3/§5/§8): мтайм-набор расширен с одного файла до
        ProfileMeta.mtime_set действующего указателя (сессионный оверрайд, если он
        есть, — не только базового); реальное изменение набора (не только базового
        файла) сбрасывает предохранитель моделей (починка мёртвой модели не должна
        пережить починку — «сброс: изменение конфиг-набора»); сессионный выбор
        профиля, ставший битым под новой базой, — санация (§9.4); boot-frozen-ключи,
        разошедшиеся между старой и новой эффективной базой, — notice (§3.2)."""
        from config import load_config_ex, apply_cli_overrides
        from thread_store import resolve_threads_dir
        from tui.runtime_config import carry_over_runtime_fields
        cfg, _meta = load_config_ex(self._config_path)
        apply_cli_overrides(cfg)
        # launch_root и прочие поля рантайма TUI в файле конфига отсутствуют: без переноса
        # они исчезают при первом же сохранении настроек — /diff перестаёт работать, а
        # периметр path_policy съезжает со снапшота cwd на живой os.getcwd().
        carry_over_runtime_fields(self._config or {}, cfg)
        old_effective = self.effective_config()
        old_mtime_set = self._config_mtime_set
        old_source = self._profile_source
        self._config = cfg
        self._threads_dir = resolve_threads_dir(cfg)
        self._session_profile_cache = None   # база сменилась — пере-мерж под сессионный профиль устарел
        self.sanitize_session_profile()
        # Снимок — ПОСЛЕ санации и ДЕЙСТВУЮЩИМ именем (`_compute_mtime_set` →
        # `_active_profile_name_for_mtime`: сессионный оверрайд, если он пережил
        # санацию, иначе новый базовый указатель). Брать `meta.mtime_set` из
        # base-only load_config_ex выше было бы неверно — этот набор никогда не
        # включает кандидатные пути сессионно выбранного профиля (ревью Task 11).
        self._config_mtime_set = self._compute_mtime_set()
        if old_mtime_set != self._config_mtime_set:
            self._circuit_breaker.reset()
        self._notice_profile_source_change(old_source)
        self._notice_boot_frozen_diff(old_effective, self.effective_config())
        self.invalidate_runner()

    def reload_config_if_changed(self) -> None:
        """Гейт свежести окна: перечитать базу (reload_config), только если мтайм-набор
        текущего указателя (спека §3.3 — база/managed/проектный конфиг + оба кандидатных
        пути активного профиля) реально изменился с последнего известного снимка —
        другое окно могло сохранить своё умолчание или сменить профиль, пока это окно
        живёт на конфиге, прочитанном при старте.

        Зовётся ЯВНО и только на пути открытия Settings (main._cmd_settings при
        открытии экрана, settings._sync_config_from_app при входе в секцию) —
        там, где редактируется глобальный дефолт; больше ниоткуда
        (дизайн 2026-07-21 §4). Поток вызова — только UI, поэтому замок не нужен.

        Набор не менялся — не делаем НИЧЕГО: ни чтения, ни invalidate_runner.

        Битый/пропавший файл (юзер сохранил config.yml в промежуточном
        состоянии, либо файл профиля-указателя конфига исчез/невалиден, §9.4)
        не должен ронять окно — остаёмся на прежней базе, а причину
        кладём в runtime-уведомления (/notices)."""
        mtime_set = self._compute_mtime_set()
        if mtime_set == self._config_mtime_set:
            return
        try:
            self.reload_config()
        except Exception as exc:
            push_runtime_notice(Notice(
                "warning", "config_reload_failed",
                f"config.yml не перечитан, окно осталось на прежней базе: {exc}",
                ("config.yml",),
            ))

    def screen_config(self) -> dict:
        """Единая точка доступа к базе для ОТКРЫТИЯ экранов (/config, /mcp, /diff,
        /skills, /settings — и вход в секцию /settings): отдаёт актуальную базу
        БЕЗ автоперечитки. Свежесть config.yml нужна только там, где он
        редактируется (Settings), и вызывается ЯВНО на пути его открытия
        (main._cmd_settings, settings._sync_config_from_app) — иначе два окна
        правили бы файл вслепую (дизайн 2026-07-21 §4).

        НЕ для рендера — effective_config() (база + сессионные переопределения)
        зовётся на каждый кадр; здесь — только выдача базы при открытии экрана."""
        return self._config or {}

    def effective_config(self) -> dict:
        """Конфиг, который видят и UI, и агент: база + сессионные переопределения.

        Единая точка чтения: витрины (палитра, секция настроек) и сборка
        executor обязаны брать конфиг отсюда, а не из `self._config` — иначе
        переключённое в сессии значение не доедет.

        `profile` — особый ключ (спека §9.1): база не всегда `self._config` —
        сессия могла выбрать другой профиль, и тогда база — его пере-мерж
        (`_session_base_config`, закэшированный)."""
        from tui.session_settings import apply_overrides
        cfg = dict(self._session_base_config())
        apply_overrides(cfg, self._session_overrides)
        return cfg

    def _session_base_config(self) -> dict:
        """База для effective_config(): `self._config`, либо, если сессия выбрала
        ДРУГОЙ профиль (§9.1), — пере-смерженная под ним база (`_profile_remerge`,
        закэшированная по мтайм-набору+имени — рендер не должен гонять
        load_config_ex каждый кадр)."""
        override_name = self._session_overrides.get("profile")
        base_name = (self._config or {}).get("profile") or None
        if override_name is None or override_name == (base_name or ""):
            return self._config or {}
        # override "" = сессионное снятие (§9.1, ревизия 2026-07-28): база задаёт
        # профиль, сессия работает чистым конфигом — пере-мерж БЕЗ профиля.
        return self._profile_remerge(override_name)

    def _profile_remerge(self, name: str) -> dict:
        """Пере-прогон `load_config_ex(profile_override=name)` — то же, что делает
        CLI `--profile` (спека §9.1). Кэш-ключ — (мтайм-набор, имя): любое реальное
        изменение файлов ИЛИ смена сессионного выбора инвалидирует кэш; иначе то же
        имя при том же наборе отдаётся из кэша без похода на диск.

        Битый профиль (файл исчез/невалиден под ногами между санацией и этим
        вызовом — редкая гонка: `_active_profile_name_for_mtime` заводит его в
        мтайм-набор, поэтому обычный путь — reload_config_if_changed замечает
        изменение и sanitize_session_profile санирует ДО того, как сюда дошли;
        здесь — страховка на случай, если рендер всё же попал на битый файл
        МЕЖДУ санацией и этим вызовом) не должен ронять рендер на каждом кадре —
        деградируем на прежнюю базу, но НЕ молча: notice один раз на эпизод
        (кэшируем фоллбэк под тем же ключом — повтор с тем же ключом отдаётся из
        кэша без повторного notice; смена набора/имени — новый шанс и новый
        notice, если всё ещё битый)."""
        key = (frozenset((self._config_mtime_set or {}).items()), name)
        cached = self._session_profile_cache
        if cached is not None and cached[0] == key:
            return cached[1]
        from config import load_config_ex, apply_cli_overrides
        from tui.runtime_config import carry_over_runtime_fields
        try:
            cfg, meta = load_config_ex(self._config_path, profile_override=name)
        except Exception as exc:
            fallback = self._config or {}
            self._session_profile_cache = (key, fallback)
            push_runtime_notice(Notice(
                "warning", "session_profile_remerge_failed",
                f'профиль "{name}" из сессии временно недоступен ({exc}) — используется прежняя база',
                (),
            ))
            return fallback
        apply_cli_overrides(cfg)
        carry_over_runtime_fields(self._config or {}, cfg)
        self._session_profile_cache = (key, cfg)
        self._maybe_notice_profile_activation(name, meta, cfg)
        return cfg

    def _maybe_notice_profile_activation(self, name: str, meta, cfg: dict) -> None:
        """Спека §3.5: первая активация ПРОЕКТНОГО профиля в сессии — notice с
        файлом и урлами моделей: файл приезжает с клоном репозитория и уводит
        запросы на свои адреса — пользователь должен видеть это сам, не только в
        логах провайдера."""
        ref = meta.ref
        if ref is None or ref.root_kind != "project" or name in self._profile_notice_seen:
            return
        self._profile_notice_seen.add(name)
        models = cfg.get("models") or {}
        urls = sorted({str(entry.get("url")) for entry in models.values() if entry.get("url")})
        urls_text = ", ".join(urls) if urls else "—"
        push_runtime_notice(Notice(
            "info", "project_profile_activated",
            f'профиль "{name}" — проектный ({ref.path}); модели: {urls_text}',
            (str(ref.path),),
        ))

    def sanitize_session_profile(self) -> None:
        """§9.4 (ветка «выбран сессией»): сессионный оверрайд профиля, ставший
        битым (файл удалили/переименовали под новой базой), — снять + notice +
        пересборка + сброс предохранителя. Санация приравнена к смене профиля.

        Ветка «выбран конфигом» (файл базового указателя исчез/невалиден) сюда
        не относится — её покрывает reload_config_if_changed: load_config_ex
        бросает исключение, окно остаётся на прежней базе + notice
        `config_reload_failed` (существующий канон, без изменений).

        Точки вызова: attach треда (main.py) и reload_config (здесь)."""
        name = self._session_overrides.get("profile")
        if not name:
            return
        base_name = (self._config or {}).get("profile") or None
        if name == base_name:
            return
        from config import load_config_ex
        try:
            load_config_ex(self._config_path, profile_override=name)
        except Exception:
            acting = base_name or "без профиля"
            self._session_overrides.pop("profile", None)
            self._session_profile_cache = None
            self._circuit_breaker.reset()
            try:
                self.persist_session_overrides()
            except OSError:
                pass   # тред недоступен — в памяти уже чисто, персист догонит позже
            push_runtime_notice(Notice(
                "warning", "session_profile_stale",
                f'профиль "{name}" из сессии не найден — действует "{acting}"', ()))
            self.invalidate_runner()

    def switch_session_profile(self, name: str) -> None:
        """Переключить профиль сессии (спека §9.1/§9.3): применяет как обычный
        сессионный оверрайд (персист + restore на attach — существующим
        механизмом), но с эффектами, специфичными для профиля — предохранитель
        и кэш пере-мержа сбрасывает `set_session_override`; здесь — то, что
        специфично для МОМЕНТА выбора: boot-frozen-diff notice и, при живом ране,
        notice отложенного применения (пересборка сама откладывается существующим
        `invalidate_runner` — единый механизм с обычной перечиткой конфига)."""
        old_effective = self.effective_config()
        self.apply_session_override("profile", name)
        self._notice_boot_frozen_diff(old_effective, self.effective_config())
        runner = self.active_runner
        live = runner is not None and runner.is_running()
        self.invalidate_runner()
        if live:
            what = (f'профиль "{name}"' if name else "отключение профиля")
            push_runtime_notice(Notice(
                "info", "profile_switch_deferred",
                f"{what} применится после завершения текущей задачи", ()))

    def _notice_profile_source_change(self, old_source: Optional[tuple]) -> None:
        """Спека §3.1: у активного профиля сменился ФАЙЛ-источник при том же имени.

        Типовой случай — `git pull` принёс `<проект>/.gc/profiles/<имя>.yml`:
        проектный корень выигрывает у базового, и профиль того же имени начинает
        приносить другие модели, адреса и токены. Имя в футере при этом не
        меняется, поэтому без уведомления подмену не по чему заметить (обратный
        переезд, когда проектный файл удалили, — ровно так же).

        Молчим, когда сменилось само ИМЯ профиля (это осознанное переключение, о
        нём говорят свои каналы) и когда путь тот же — правка файла источником не
        является, иначе notice сыпался бы на каждое сохранение из /settings.
        """
        self._profile_source = self._current_profile_source()
        new_source = self._profile_source
        if old_source is None or new_source is None:
            return
        old_name, old_path, old_kind = old_source
        new_name, new_path, new_kind = new_source
        if old_name != new_name or old_path == new_path:
            return
        # Подпись корня — той же формулой, что у соседних витрин
        # (`main.py:4025`, `settings.py:2214`): своего словаря здесь не заводим.
        old_ru = "проектный" if old_kind == "project" else "глобальный"
        new_ru = "проектный" if new_kind == "project" else "глобальный"
        push_runtime_notice(Notice(
            "warning", "profile_source_changed",
            f'профиль "{new_name}" теперь берётся из другого файла: '
            f"{new_path} ({new_ru}) вместо {old_path} ({old_ru})",
            (new_path, old_path),
        ))

    def _notice_boot_frozen_diff(self, old_cfg: dict, new_cfg: dict) -> None:
        """Спека §3.2: boot-frozen-ключи разошлись между старой и новой эффективной
        базой при пересборке — живое переключение их не подхватывает, только
        следующий запуск. Notice, не молчание.

        Сравниваем, только если старое значение реально известно (не None): `None`
        типично значит «конфиг ещё не резолвил значение по умолчанию» (например,
        свежесозданный `TuiApp` в тестах с частичным словарём) — это не смена
        значения пользователем, шуметь не о чем."""
        changed = [
            dotted for dotted in BOOT_FROZEN_KEYS
            if old_cfg.get(_BOOT_FROZEN_FLAT[dotted]) is not None
            and old_cfg.get(_BOOT_FROZEN_FLAT[dotted]) != new_cfg.get(_BOOT_FROZEN_FLAT[dotted])
        ]
        if not changed:
            return
        push_runtime_notice(Notice(
            "info", "boot_frozen_pending",
            f'{", ".join(changed)} — вступит в силу со следующего запуска',
            tuple(changed),
        ))

    def _notice_level_switch_deferred(self, value: str) -> None:
        """/level посреди хода (спека Task 15, симметрично `switch_session_profile`,
        §9.1): сессионный оверрайд применяется в конфиг сразу (палитра — мгновенно,
        не выглядит несработавшей), но модель фиксируется на ход (`state.turn_level`,
        `turn_boundary.begin_turn`) — запросы текущего хода всё ещё идут на прежнем
        уровне. Тот же тон и механизм, что у профиля, своё имя события."""
        runner = self.active_runner
        if runner is not None and runner.is_running():
            push_runtime_notice(Notice(
                "info", "level_switch_deferred",
                f'уровень "{value}" применится после завершения текущей задачи', ()))

    def _level_provider(self) -> str:
        """Активный уровень для `ModelRouter` (спека §6.4): читает сессионный слой
        напрямую, БЕЗ `effective_config()`/`apply_overrides` — `/level` переключается
        без `invalidate_runner` (единственный тоггл с таким свойством), и это
        единственный потребитель, обязанный видеть смену немедленно, без
        пересборки executor."""
        if "level" in self._session_overrides:
            return self._session_overrides["level"]
        return (self._config or {}).get("level") or "default"

    def active_profile_ref(self):
        """`ProfileRef` действующего (эффективного, сессионно-осознанного) профиля
        или `None` — витрины `/config`/`/status` (спека §12.3a) хотят не только
        имя, но и корень/путь файла, а `effective_config()` отдаёт только имя.
        Единая точка резолва — `config_modules.profiles.find_active_ref`."""
        import os
        from pathlib import Path
        from config_modules.profiles import find_active_ref
        name = self.effective_config().get("profile") or None
        return find_active_ref(name, Path(os.getcwd()), Path(self._config_path or ""))

    def display_router(self) -> "ModelRouter":
        """Дешёвый Router для витрин (палитра `/level`/`/profiles`, футер — спека
        §12.1/§12.2): резолвит из `effective_config()` БЕЗ сборки executor —
        `resolve()`/`resolve_level()` не ходят в транспорт, только читают снимок
        конфига в памяти. Не путать с `ctx.model_router` активного рана — тот
        собран из снимка на момент СБОРКИ executor (может быть stale между
        пересборками); витрины обязаны видеть смену уровня/профиля сразу
        (§6.4), поэтому строят свой Router от текущего effective_config()
        на каждый вызов, а не читают executor."""
        from config import cli_forces_legacy_model
        from model_router import ModelRouter
        cfg = self.effective_config()
        return ModelRouter(
            cfg,
            level_provider=self._level_provider,
            breaker=self._circuit_breaker,
            force_legacy=cli_forces_legacy_model(cfg),
        )

    def persist_circuit_breaker(self) -> None:
        """Снимок предохранителя моделей (спека §8) в meta треда — переживает
        пересборки executor внутри сессии (владелец состояния — сессия-тред)."""
        task_id = getattr(self.tui_state, "active_task_id", None)
        if not task_id or not self._threads_dir:
            return
        from tui.services.stores.file_store import FileTaskStore
        from tui.session_settings import to_breaker_meta_patch
        store = FileTaskStore(threads_dir=self._threads_dir, system=self._system)
        store.update_tui_meta(task_id, to_breaker_meta_patch(self._circuit_breaker.snapshot()))

    def _persist_breaker_after_worker(self) -> None:
        """`persist_circuit_breaker`, вызванный ИЗ worker-потока раннера.

        Мост на UI-поток обязателен: persist — полный read-modify-write meta.json
        (`FileTaskStore.update_tui_meta`), а UI-поток на спаде `is_running` пишет в
        тот же документ (статус треда, `persist_session_overrides`). Две полные
        перезаписи от разных потоков теряют одну правку, а прерванная запись
        оставляет обрезанный файл, который потом читается как пустой.

        RuntimeError из `call_from_thread` — приложение не запущено (тесты,
        headless) или мы уже на UI-потоке: гонки в этом случае нет, пишем
        напрямую, потому что потерять трип хуже."""
        try:
            self.call_from_thread(self.persist_circuit_breaker)
        except RuntimeError:
            self.persist_circuit_breaker()

    def load_circuit_breaker(self, snapshot: dict) -> None:
        """Восстановить предохранитель из meta треда при attach (спека §8):
        заменяет СОДЕРЖИМОЕ предохранителя окна под тред, на который переключились —
        состояние предыдущего треда не должно течь в новый (новая сессия — сброс).

        Restore-in-place (не подмена объекта): прогретый executor держит ссылку на
        ЭТОТ объект (передан в `ctx.model_router` при сборке). Подмена ссылки
        осиротила бы его — трипы рана уходили бы в старый breaker, а
        `persist_circuit_breaker` писал бы новый пустой; витрины и executor видели
        бы разные предохранители."""
        self._circuit_breaker.replace_from(snapshot or {})

    def set_session_override(self, dotted: str, value) -> None:
        """Переопределить ключ в текущей сессии (в файл конфига НЕ пишет).

        `profile` — особый случай (спека §8/§9.1): реальная смена значения сбрасывает
        кэш пере-мержа И предохранитель моделей — починка недоступной модели прежнего
        профиля не должна пережить переключение."""
        if dotted == "profile" and self._session_overrides.get(dotted) != value:
            self._session_profile_cache = None
            self._circuit_breaker.reset()
        self._session_overrides[dotted] = value

    def apply_session_override(self, dotted: str, value) -> None:
        """Переопределить ключ и сохранить набор в метаданные треда — атомарно.

        Не записалось (каталог тредов недоступен, диск полон, отвалившийся сетевой
        путь) — откатываем: вызывающий показывает «✗ не сохранено» и оставляет на
        экране прежнее значение, поэтому effective_config() обязан отдавать ровно
        его, иначе следующий ход агента пойдёт в необъявленном пользователю режиме."""
        prev = dict(self._session_overrides)
        self.set_session_override(dotted, value)
        try:
            self.persist_session_overrides()
        except OSError:
            self._session_overrides = prev
            raise
        if dotted == "level":
            self._notice_level_switch_deferred(value)

    def apply_session_overrides(self, overrides: dict) -> None:
        """Переопределить НЕСКОЛЬКО ключей разом и сохранить в метаданные треда —
        атомарно (для пары /rigor). Не записалось — откатываем ВЕСЬ набор к
        прежнему, как одиночный apply_session_override: объявленное пользователю и
        то, с чем пойдёт следующий ход, обязаны совпасть."""
        prev = dict(self._session_overrides)
        self._session_overrides.update(overrides or {})
        try:
            self.persist_session_overrides()
        except OSError:
            self._session_overrides = prev
            raise

    def clear_session_overrides(self, keys) -> None:
        """Снять НЕСКОЛЬКО ключей сессии разом — generic primitive для отката
        целой группы оверрайдов (парой и больше) к глобальному дефолту.
        Нечего снимать — тихий no-op без persist. Ошибка записи — откат, как в
        apply_session_overrides."""
        prev = dict(self._session_overrides)
        for k in keys:
            self._session_overrides.pop(k, None)
        if self._session_overrides == prev:
            return
        try:
            self.persist_session_overrides()
        except OSError:
            self._session_overrides = prev
            raise

    def load_session_overrides(self, overrides: dict) -> bool:
        """Заменить набор переопределений целиком — при открытии треда.
        Пустой набор возвращает сессию к умолчаниям конфига.

        Возвращает True, если набор реально изменился: пересборка executor стоит
        MCP-дискавери в worker-потоке (пользователь ждёт её до начала хода), и
        платить за неё, когда у треда те же значения, незачем."""
        new = dict(overrides or {})
        changed = new != self._session_overrides
        self._session_overrides = new
        return changed

    def persist_session_overrides(self) -> None:
        """Сохранить переопределения в метаданные текущего треда (meta.tui).

        Тред может быть ещё не создан (пустая сессия) — тогда тихо выходим:
        значения живут в памяти и запишутся при следующем персисте, когда тред
        появится. Ошибки записи не гасим — вызывающий показывает флеш."""
        task_id = getattr(self.tui_state, "active_task_id", None)
        if not task_id or not self._threads_dir:
            return
        from tui.services.stores.file_store import FileTaskStore
        from tui.session_settings import to_meta_patch
        store = FileTaskStore(threads_dir=self._threads_dir, system=self._system)
        store.update_tui_meta(task_id, to_meta_patch(self._session_overrides))

    def invalidate_runner(self) -> None:
        """Конфиг сессии изменился — следующий run должен собраться заново.
        Живой run не трогаем: помечаем stale, пересборка после завершения.
        Единая точка сброса раннера: сюда же делегирует reload_config."""
        runner = self.active_runner
        if runner is not None and runner.is_running():
            self._runner_stale = True
            return
        self.active_runner = None
        self._runner_stale = False

    def _finish_onboarding(self) -> None:
        """Финал онбординга (auto и review одинаково): reload_config + переход
        на главный экран — «завершил настройку → стартует агент»."""
        self.set_config_path(self._onboarding_config_path)
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

        # Снимок предохранителя — ДО проверки владельца и на обоих исходах работы
        # воркера (спека §8): breaker один на приложение, и трипы уже случившегося
        # прогона нужны следующему ходу, даже если ран упал (сюда приходит
        # `_emit_error`, а не `_on_complete`) или active_runner подменили на
        # Ctrl+C. Проверка владельца отсекает UI-поля ЧУЖОГО раннера — предохранитель
        # к ним не относится.
        def _on_runner_error(exc: BaseException) -> None:
            self._persist_breaker_after_worker()
            runner = runner_ref[0] if runner_ref else None
            if runner is None or runner is not self.active_runner:
                return
            if getattr(runner, "_marked_stopped", False):
                return
            self.last_runner_error = str(exc) or exc.__class__.__name__

        def _on_runner_complete(outcome: Optional[str]) -> None:
            self._persist_breaker_after_worker()
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
        from config import build_auth_connector, cli_forces_legacy_model
        from run_context import create_run_context
        from tui.services.mcp_progress import McpProgress
        cfg = self.effective_config()
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
            level_provider=self._level_provider,
            circuit_breaker=self._circuit_breaker,
            # Легаси-форс (спека §2/§12.1): CLI --model/--completions_url поверх
            # непустого реестра — тот же детект, что уже используют CLI/server
            # (config.cli_forces_legacy_model); без него ран резолвил бы через
            # реестр, расходясь с честным хинтом /level (display_router).
            force_legacy=cli_forces_legacy_model(cfg),
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
        по гейту он не видит. Так пропадал Ctrl+D в секции MCP экрана /settings, пока
        удаление жило на нём (сейчас там Del, §5.3 единого языка форм) — хотя /diff
        с настроек всё равно не открывается.

        Экран, помеченный BLOCKS_GLOBAL_NAV, модален: мастер настройки и пост-апдейт
        живут до/вокруг записи конфига, и глобальная навигация поверх них показывала бы
        справку с неработающими клавишами и уведомления по ещё не записанному конфигу.
        """
        if getattr(self.screen, "BLOCKS_GLOBAL_NAV", False):
            # Esc и Ctrl+C остаются, а с ними — ходьба фокуса ВНУТРИ экрана: гейт
            # заведён против глобальной НАВИГАЦИИ поверх модального экрана, а не против
            # неё. Прежнее обоснование («без focus_next до полей за первым не
            # добраться») больше не действует: мастер перехватывает Tab сам и водит им
            # курсор группами (§4.1 единого языка форм), до app-биндинга клавиша не
            # доходит; у пост-апдейта фокусируемых виджетов нет вовсе. Разрешение
            # осталось как правило про ГРАНИЦУ гейта — фокус внутри модального экрана
            # его собственное дело, — а не как условие работоспособности полей.
            return action in ("back", "interrupt", "focus_next", "focus_previous")
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

    def action_buffer(self) -> None:
        """B5: открыть буфер сессии (F7 / /buffer). No-op если уже открыт.

        Нет активной сессии — короткое сообщение, экран не открываем.
        """
        from tui.screens.copy_buffer import CopyBufferScreen

        if isinstance(self.screen, CopyBufferScreen):
            return  # повторный F7 на открытом буфере — no-op
        main = self._main_screen()
        if main is None:
            return
        store = getattr(main, "_store", None)
        task_id = getattr(main, "_active_task_id", None)
        if store is None or not task_id:
            self.notify("Нет активной сессии — буфер недоступен")
            return
        self.push_screen(CopyBufferScreen(store=store, task_id=task_id))

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
            copy_out(self, payload, toast=True, blocking=False)

    def on_text_selected(self, event) -> None:
        """Copy-on-select: drag без Shift → внешний буфер (тихий успех).

        TextSelected не шлётся из Input/TextArea — дубля с focused.selected_text нет.
        Debounce: последний payload за окно; native write — в worker (O9).
        """
        payload = self._clipboard_payload()
        if not payload:
            return
        self._pending_select_copy = payload
        timer = getattr(self, "_select_copy_timer", None)
        if timer is not None:
            try:
                timer.stop()
            except Exception:
                pass
        self._select_copy_timer = self.set_timer(
            self._COPY_ON_SELECT_DEBOUNCE_S,
            self._flush_select_copy,
        )

    def _flush_select_copy(self) -> None:
        """Сработал debounce copy-on-select — копируем последний payload."""
        self._select_copy_timer = None
        text = self._pending_select_copy
        self._pending_select_copy = None
        if text:
            copy_out(self, text, toast=False, blocking=False)

    def cancel_pending_select_copy(self) -> None:
        """Отменить отложенную автокопию выделения (copy-on-select).

        Нужен для явных жестов копирования блока (кнопка ❐, двойной клик):
        двойной клик по таблице сначала рождает TextSelected (drag == выделение
        всего тела) и ставит в очередь автокопию видимого — псевдографики, а
        затем CopyBlockRequest кладёт в буфер переносимую форму — markdown.
        Без отмены debounce 0.15с срабатывает позже и затирает markdown тем,
        что видно на экране. Вызывать в момент обработки CopyBlockRequest."""
        self._pending_select_copy = None
        timer = getattr(self, "_select_copy_timer", None)
        if timer is not None:
            try:
                timer.stop()
            except Exception:
                pass
        self._select_copy_timer = None

    def _push_session_copy_buffer(self, payload: str) -> None:
        """B2: дописать payload сверху в буфер сессии (tui.copy_buffer) через meta.

        Тот же payload, что ушёл наружу (включая cite, если он был). Не вызываем
        на экране буфера (B8 — иначе буфер дописывал бы сам в себя). Best-effort:
        без store/active task — no-op. Только из UI-потока Textual.
        """
        # B8: на экране буфера не self-push (иначе буфер дописывал бы сам в себя)
        if getattr(self.screen, "_is_copy_buffer_screen", False):
            return
        # Store/task_id — из базового TraceScreen (как action_buffer): у diff/view
        # экранов своих _store/_active_task_id нет, без этого фикс push молча no-op.
        main = self._main_screen()
        if main is None:
            return
        store = getattr(main, "_store", None)
        task_id = getattr(main, "_active_task_id", None)
        if store is None or not task_id:
            return
        try:
            from tui.services.copy_buffer import push_copy_buffer

            meta = store.get_task_meta(task_id) or {}
            existing = (meta.get("tui") or {}).get("copy_buffer") or ""
            new_buf = push_copy_buffer(existing, payload)
            store.update_tui_meta(task_id, {"copy_buffer": new_buf})
        except Exception:
            pass  # best-effort: буфер сессии не должен ронять copy-жест

    def action_interrupt(self) -> None:
        """Ctrl+C: выделение → копировать (+ буфер сессии); активный run → стоп; вхолостую → exit-модалка (как Esc)."""
        payload = self._clipboard_payload()
        if payload:
            copy_out(self, payload, toast=True, blocking=False)
            self._push_session_copy_buffer(payload)  # B2: тем же payload сверху
            return
        scr = self.screen
        runner = getattr(self, "active_runner", None)
        if runner is not None and runner.is_running():
            if hasattr(scr, "_cmd_stop"):
                scr._cmd_stop()
        elif hasattr(scr, "_request_exit"):
            scr._request_exit()
