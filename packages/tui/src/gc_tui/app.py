"""TuiApp — Textual application."""

from __future__ import annotations

import asyncio
import atexit
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, List, NamedTuple, Optional

if TYPE_CHECKING:
    from gc_core.model_router import ModelRouter

from gc_core.attachments.store import ImageStore
from gc_core.config_modules.field_meta import leaf_meta
from gc_core.runtime_notices import (
    collect_runtime_notices,
    collect_unread_runtime_notices,
    push_runtime_notice,
    register_runtime_notices_listener,
    unregister_runtime_notices_listener,
)
from gc_core.security.confirm_port import call_with_optional
from gc_core.settings import AppConfig, LoadedSettings, settings_of
from gc_core.startup_notices import (
    Notice,
    auto_routing_unavailable_notice,
    collect_startup_notices,
)
from gc_core.tools.registry import all_builtin_tool_names
from textual.app import App
from textual.binding import Binding
from textual.screen import Screen

from gc_tui import branding, palette, textual_compat
from gc_tui.screens.main import TraceScreen
from gc_tui.services.agent_runner import STOP_SETTLE_SEC
from gc_tui.services.clipboard_ops import copy_out, with_source_cite
from gc_tui.services.session_lock import is_held_by_other
from gc_tui.services.stream_sink import StreamSink
from gc_tui.services.trace_note_sink import TraceNoteSink
from gc_tui.services.voice_notices import voice_notices

textual_compat.apply_textual_patches()

_AUTO_UPDATE_NOTICE = "Доступна новая версия — /settings → Обновление"

# Ключи схемы с флагом ``restart`` (``gc_meta(restart=True)``, спека §3.2). Перечитка
# конфига и сессионный профиль их НЕ замораживают: новое значение попадает в
# действующие настройки сразу — каталог тредов окна (`_threads_dir`) перечитка
# переводит немедленно, executor берёт новые пути при следующей пересборке (логгер
# рана — при её создании). Notice `boot_frozen_pending` при смене значения сохранён
# в прежней редакции («вступит в силу со следующего запуска») — как предупреждение,
# что часть уже открытого (живой ран, его лог) остаётся на прежнем пути.
# Порядок — порядок полей схемы; путь через точку — из схемы AppConfig, по нему
# же и сравниваются значения настроек.
BOOT_FROZEN_KEYS = tuple(
    path for path, meta in leaf_meta().items() if meta.restart
)


def _setting_at(settings, dotted: str):
    """Значение поля настроек по пути через точку (None — поля нет)."""
    current = settings_of(settings)
    for name in dotted.split("."):
        current = getattr(current, name, None)
        if current is None:
            return None
    return current


def _maybe_auto_check(
    settings,
    state: dict,
    now: float,
    installed_commit,
    *,
    check,
    record_result,
    on_available,
    on_result=None,
) -> None:
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
    `on_result`), поэтому функция зовётся из фонового воркер-треда, не с UI-потока.
    `settings` — настройки окна (всё, что принимает `settings_of`)."""
    from gc_core.updater.check import (
        CheckResult,
        auto_check_enabled,
        update_available_from_state,
    )
    from gc_core.updater.source import resolved_source

    resolved = resolved_source()
    if resolved is None:
        return  # dev/неизвестный источник — обновлений нет
    if update_available_from_state(state, installed_commit):
        on_available(False)  # персист-вердикт: строка без toast
    if not auto_check_enabled(settings):
        return
    try:
        result = check(resolved)
    except Exception:
        result = None
    if result is None:
        record_result(
            now, None, installed_commit
        )  # только стамп; прошлый remote сохраняется
        if on_result is not None:
            on_result(CheckResult("unknown", None, "не удалось проверить"))
        return
    record_result(now, result.remote_commit, installed_commit)
    if on_result is not None:
        on_result(result)
    if result.status == "update":
        on_available(True)  # свежая находка → toast + строка


def _run_auto_check_safely(
    settings,
    state: dict,
    now: float,
    installed_commit,
    *,
    check,
    record_result,
    on_available,
    on_result=None,
) -> None:
    """Best-effort обёртка воркера: авто-чек обновления НИКОГДА не роняет старт.

    Воркер стартует при каждом запуске TUI с дефолтным `exit_on_error=True`, а
    внутренний try в `_maybe_auto_check` покрывает только `check()`. Любое иное
    исключение (гейты, `record_result`, `on_available`-мост `call_from_thread` при
    shutdown-гонке) иначе дошло бы до Textual `_handle_exception` и уронило
    приложение — поэтому глотаем всё здесь."""
    try:
        _maybe_auto_check(
            settings,
            state,
            now,
            installed_commit,
            check=check,
            record_result=record_result,
            on_available=on_available,
            on_result=on_result,
        )
    except Exception:
        pass


def _release_runner(runner) -> None:
    """Отдать логгер прогона снимаемого runner'а (см. `AgentRunner.release`).

    Через getattr: тесты подставляют в ``active_runner`` двойников без него.
    """
    release = getattr(runner, "release", None)
    if callable(release):
        release()


def _same_effective_key(cached_key: tuple, key: tuple) -> bool:
    """Ключ кэша действующих настроек: база — по ИДЕНТИЧНОСТИ (ключ держит сам
    объект, а не `id()`: освобождённый id переиспользуется, и чужая база
    совпала бы с ключом), остальное — по значению."""
    return cached_key[0] is key[0] and cached_key[1:] == key[1:]


@dataclass(frozen=True)
class _ReloadPlan:
    """Всё, что перечитка конфига применяет к окну, — собрано и проверено ДО
    применения (`TuiApp._plan_reload`). Применение (`_commit_reload`) — только
    присваивания, сбоить там нечему: перечитка либо меняет всё, либо ничего."""

    settings: LoadedSettings  # новая база окна
    session_overrides: dict  # сессионный слой после санации под новую базу
    effective: tuple  # (ключ, LoadedSettings) — кэш effective_settings
    profile_cache: Optional[
        tuple
    ]  # пере-мерж сессионного профиля под новую базу
    mtime_set: dict
    threads_dir: Optional[str]
    notices: tuple  # что снято санацией (уходит в /notices после применения)
    profile_dropped: bool
    activated_profile: Optional[tuple]  # (имя, LoadedSettings) для notice §3.5


class _ConfigSnapshot(NamedTuple):
    """Состояние конфига окна, снятое одним заходом под замком."""

    settings: object
    overrides: dict
    mtime_set: dict
    profile_cache: Optional[tuple]
    effective: Optional[tuple]
    generation: int


def _validation_reason(exc: Exception) -> str:
    """Короткая причина отказа проверки: первое сообщение pydantic, иначе текст."""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            first = errors()[0]
            where = ".".join(str(part) for part in first.get("loc", ()))
            return f"{where}: {first['msg']}" if where else str(first["msg"])
        except Exception:
            pass
    return str(exc)


def _stale_profile_notice(name: str, exc: Exception, base_name) -> Notice:
    """Сессионный профиль снят санацией (§9.4). Причина, а не один её частный
    случай: «не найден» посылало искать пропажу, когда файл лежит на месте и не
    читается (профиль в UTF-16, неразбираемый YAML). Причина самодостаточна
    («<путь>: …»), тот же приём, что у `session_profile_remerge_failed`."""
    acting = base_name or "без профиля"
    return Notice(
        "warning",
        "session_profile_stale",
        f'профиль "{name}" из сессии снят ({exc}) — действует "{acting}"',
        (),
    )


def _remerge_failed_notice(name: str, exc: Exception) -> Notice:
    return Notice(
        "warning",
        "session_profile_remerge_failed",
        f'профиль "{name}" из сессии временно недоступен ({exc}) — используется прежняя база',
        (),
    )


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
        Binding(
            "f6", "settings", "/settings", priority=True
        ),  # у TextArea свой f6=select_line — без priority хоткей мёртв при фокусе в инпуте
        Binding(
            "f7", "buffer", "Буфер сессии", priority=True
        ),  # TextArea f7=select_all — без priority F7 в инпуте не откроет буфер
        Binding("ctrl+slash", "help", "Help"),
        Binding("ctrl+s", "sessions", "/sessions"),
        # priority: TextArea перехватывает Ctrl+D как delete-right.
        Binding("ctrl+d", "diff", "/diff", priority=True),
        Binding("ctrl+o", "notices", "/notices"),
        Binding("ctrl+n", "new", "/new"),
        Binding("ctrl+g", "config", "/config"),
        # Ctrl+C: есть выделение → копировать; нет → отменить текущий run.
        # priority — перебивает копирование TextArea и системный help_quit.
        Binding(
            "ctrl+c", "interrupt", "копировать/стоп", show=False, priority=True
        ),
        # priority: без него Textual забирает Shift+Tab под focus_previous.
        Binding(
            "shift+tab",
            "cycle_permission_mode",
            "режим",
            show=False,
            priority=True,
        ),
        # Страховка: терминал часто съедает хоткей — основной UX = copy-on-select.
        Binding(
            "ctrl+shift+c",
            "copy_selection",
            "копировать",
            show=False,
            priority=True,
        ),
        Binding(
            "super+c",
            "copy_selection",
            "копировать",
            show=False,
            priority=True,
        ),
    ]

    def __init__(
        self,
        *,
        threads_dir: Optional[str] = None,
        system: str = "cli",
        settings: Optional[LoadedSettings] = None,
        profile_override: Optional[str] = None,
        model_override: Optional[str] = None,
    ) -> None:
        super().__init__()
        self._threads_dir = threads_dir
        self._system = system
        # База окна — `LoadedSettings` (файлы + CLI + рантайм TUI). Без настроек
        # (встраивание, тесты) — умолчания схемы.
        if settings is None:
            settings = LoadedSettings(settings=AppConfig())
        elif isinstance(settings, AppConfig):
            settings = LoadedSettings(settings=settings)
        elif not isinstance(settings, LoadedSettings):
            raise TypeError(
                "TuiApp ждёт настройки (LoadedSettings или AppConfig), а не "
                f"{type(settings).__name__}"
            )
        self._settings: LoadedSettings = settings
        # (ключ, LoadedSettings): действующие настройки читаются каждый кадр, а
        # наложение сессии — это валидация модели.
        self._effective_cache = None
        # Замок состояния конфига: перечитка (UI-поток) подменяет базу, сессионный
        # слой, кэши и снимки ОДНИМ присваиванием под ним, а сборка раннера
        # (worker-поток) берёт под ним же снимок — смесь старого и нового не
        # видит никто. Под замком только чтение/присваивание полей: диск,
        # валидация и notice (мост `call_from_thread` ждёт UI-поток) — снаружи.
        self._config_lock = threading.RLock()
        # Поколение состояния: растёт на каждой перечитке. Читатель, собравший
        # кэш по снимку ДО неё, не затирает им уже применённый новый.
        self._config_generation = 0
        # Кеш имён встроенных тулов для уведомлений: (settings, names) по
        # identity объекта настроек — сборка реестра дорогая, а бейдж
        # опрашивается каждый тик.
        self._builtin_names_cache: (
            tuple[LoadedSettings, frozenset[str]] | None
        ) = None
        # Профиль, выбранный флагом запуска `--profile` (`""` — «без профиля»), либо
        # None — флага не было. Тот же оверрайд, что main() отдал загрузчику на
        # старте: каждое перечитывание (`reload_config`) обязано отдавать его снова,
        # иначе первое же сохранение из /settings молча возвращает указатель из файла
        # — и совет «--profile "", чтобы обойти сломанный профиль» живёт до первой
        # записи. Хранится ОТДЕЛЬНО от сессионного выбора, а не кладётся в него:
        # сессионный выбор персистится в мету треда и переживает перезапуск, а флаг
        # запуска — свойство ЭТОГО процесса (`gc-ua --profile ""` не имеет права
        # прилипнуть к треду и обходить уже починенный профиль на следующем старте).
        # Выбор из приложения (`/profile`) остаётся сильнее: он ложится поверх
        # базы, собранной под флагом (`_session_base_settings`).
        self._profile_override = profile_override
        # Флаг запуска `--model` — тот же оверрайд, что main() отдаёт загрузчику на
        # старте: каждая пересборка базы (reload_config, _profile_remerge) обязана
        # накладывать его снова, иначе выбор живёт только до первого
        # перечитывания конфига. «Выбор или сырой id» решается один раз, по
        # реестру старта: профиль без алиаса не превращает выбор в легаси-форс
        # — такой выбор санируется, как `model:` конфига (спека §3.2).
        from gc_core.config import split_model_flag

        self._model_flag = (
            split_model_flag({"model": model_override}, settings)
            if model_override
            else None
        )
        # Сессионные переопределения конфига (спека 2026-07-20-session-scoped-settings):
        # база — self._settings (файлы), это — то, что переключено в текущем треде.
        # Хранятся по dotted-именам; накладываются в effective_settings().
        self._session_overrides: dict = {}
        self._executor_factory = (
            None  # callable returning an executor; for tests
        )
        self._stream_sink = StreamSink()
        # Пометки ленты «изображение не просмотрено» (дизайн картинок §10): ядро
        # зовёт on_trace_note из рабочего потока, экран снимает очередь на тике опроса.
        self._trace_note_sink = TraceNoteSink()
        self._subagent_progress_sink = (
            None  # QueueSubagentProgressSink; set in executor build
        )
        self._mcp_progress = None
        self._mcp_snapshot = (
            None  # boot-дискавери для /mcp (сервер→статус/тулы)
        )
        self._mcp_status = None  # per-server status dict — для startup_notices
        self._registered_tool_names = (
            None  # имена тулов до stop_tools — валидатор notices
        )
        self.active_runner = None  # AgentRunner | None
        self._runner_stale = False  # конфиг изменился при живом run → пересборка после завершения
        # Режим разрешений, выбранный в сессии (Shift+Tab, флаг запуска, сохранённый
        # в сессии). None — выбора не было, действует конфиг. Единственный
        # писатель — set_permission_mode.
        self._permission_mode: Optional[str] = None
        self._permission_mode_locked = False  # режим задан флагом запуска
        self.last_runner_error = None
        self._session_log_path: Optional[str] = None
        self.last_terminal_outcome: Optional[str] = None
        self.last_stop_requested: bool = (
            False  # юзер /stop по живому прогону → терминал «stopped»
        )
        self._onboarding_mode = (
            None  # None | "auto" | "review"; ставит tui/__init__.py
        )
        # Лестница загрузки приземлилась на ступень «без профиля» — финал мастера
        # обязан снять указатель (ставит tui/__init__.py). Один признак, а не весь
        # `RepairLoad`: приложению от лестницы нужен ровно этот ответ.
        self._onboarding_deactivate_profile = False
        # Текст деградации загрузки для экрана мастера (ставит tui/__init__.py;
        # None — деградации не было). Тем же приёмом и по той же причине, что и
        # признак выше: рантайм-уведомление под мастером недостижимо.
        self._onboarding_degraded = None
        self._config_path = (
            None  # путь к конфигу на диске; задаётся снаружи после __init__
        )
        # Мтайм-набор активного указателя (спека §3.3): база/managed/проектный конфиг +
        # оба кандидатных пути активного профиля. Расширение прежнего одиночного
        # _config_mtime — тот же снимок, что строит загрузчик, но снятый дёшево
        # (без парсинга) там, где полная загрузка не нужна (set_config_path).
        self._config_mtime_set: dict = {}
        # Кэш пере-мержа сессионного профиля (спека §9.1): (ключ, cfg). Ключ — снимок
        # mtime-набора + имя оверрайда; effective_settings() зовётся каждый кадр и не
        # должен гонять загрузчик, пока ни файлы, ни выбор не изменились.
        self._session_profile_cache = None
        # Профили, уже отдавшие notice активации в этой сессии (спека §3.5) — не спамить
        # повторно при каждом пере-мерже одного и того же выбора.
        self._profile_notice_seen: set = set()
        # Источник действующего профиля на момент последней сборки базы (спека §3.1):
        # (имя, путь, корень). Нужен ИМЕННО снимком — `active_profile_ref()` резолвит
        # по диску, и после появления тёзки «прежнее» значение уже не восстановить.
        self._profile_source: Optional[tuple] = None
        from gc_core.model_router import CircuitBreaker

        # Сессионный предохранитель моделей (спека §8) — владелец: сессия-тред;
        # персист/restore — persist_circuit_breaker()/load_circuit_breaker() (meta треда).
        self._circuit_breaker = CircuitBreaker()
        self._onboarding_config_path = (
            None  # куда писать конфиг (user_config_path)
        )
        self._upgrade_info = (
            None  # upgrade_gate.UpgradeInfo | None; ставит tui/__init__.py
        )
        self._voice_autostart_pending = False  # спавн voice-сервиса уже пошёл, вердикт ждём фоном; ставит tui/__init__.py
        self._voice_autostart_failed = False  # вердикт фонового подъёма — не поднялся; ставит _on_voice_autostart_done
        self._update_available = False  # async авто-чек нашёл новую версию (brand-строка + /notices)
        self._update_check_result = (
            None  # CheckResult сессии: питает глиф хаба и секцию «Обновление»
        )
        self._update_checking = (
            False  # стартовый авто-чек в полёте (глиф хаба — «…»)
        )
        from gc_tui.services.file_view import ViewPositionStore
        from gc_tui.state import TuiState

        self.tui_state = TuiState()
        self.view_positions = ViewPositionStore()
        # Один id на процесс TUI — для exclusive session lock (session-lock SPEC).
        self.tui_id = uuid.uuid4().hex
        self._clipboard_warn_shown = (
            False  # native-fail hint раз за сессию (clipboard_ops)
        )
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
        from gc_tui.services.terminal_title import (
            format_terminal_title,
            set_terminal_title,
        )

        set_terminal_title(format_terminal_title())
        self._start_image_staging_sweep()
        # Аварийный выход (смерть терминала, `terminal_guard`) идёт мимо on_unmount,
        # но хуки atexit выполняет: карман этого процесса снимает и он.
        atexit.register(self._clear_image_pocket)
        if self._voice_autostart_pending:
            self._start_voice_autostart()
        self._start_auto_update_check()
        if not self._needs_onboarding():
            self._show_post_update_if_needed()

    def _start_image_staging_sweep(self) -> None:
        """Карманы картинок мёртвых процессов (спека §3) — убрать на старте. Обход диска,
        поэтому daemon-потоком: сетевой диск может висеть, а выход приложения ждать его
        не должен (см. _start_voice_autostart). Уборка best-effort — сбой повторит
        следующий старт."""
        store = self.image_store

        def _work() -> None:
            try:
                store.sweep_dead_staging()
            except OSError:
                pass

        self._image_sweep = threading.Thread(
            target=_work, daemon=True, name="image-staging-sweep"
        )
        self._image_sweep.start()

    def _start_voice_autostart(self) -> None:
        """Довести подъём voice-сервиса до вердикта — ФОНОМ, отдельным daemon-потоком,
        а не через `run_worker(thread=True)`: тот идёт через дефолтный executor
        event-loop'а, и `asyncio.run()` на выходе из `app.run()` дожидается его
        завершения — для 90-секундного бюджета `ensure_voice_service` это заново
        подвесило бы терминал, только уже на выход, а не на старт (тот же баг, что
        чинили, в другом моменте). Daemon-поток (как `_preheat_bg` в
        voice_runtime.autostart) процесс не держит.

        Спавн (`start_if_needed`) делает сам `ensure_voice_service` — вот этот вызов,
        в этом потоке; main() только считает флаг `_voice_autostart_pending` и ничего
        не поднимает. Раньше main() ждал СИНХРОННО перед `app.run()`, и пустой терминал перед
        первым кадром рос вместе с бюджетом (было 15 с, стало 90 — живая приёмка
        поймала «программа зависла»). Ждать тут незачем: сервис поднимается сам, TUI
        открывается сразу, а вердикт (и честный toast «не запустился») приходит,
        когда будет готов — с полным бюджетом `ensure_voice_service`, без урезания:
        иначе тост мог бы соврать «не запустился» про сервис, который просто ещё
        поднимается."""
        from gc_tui.voice_runtime.autostart import ensure_voice_service

        cfg = self.base_settings()

        def _work() -> None:
            try:
                ok = ensure_voice_service(cfg)
            except Exception:
                ok = False
            try:
                self.call_from_thread(self._on_voice_autostart_done, ok)
            except Exception:
                pass  # приложение уже закрылось — некому показывать тост

        threading.Thread(
            target=_work, daemon=True, name="voice-autostart"
        ).start()

    def _on_voice_autostart_done(self, ok: bool) -> None:
        """Вердикт фонового подъёма — на UI-потоке. Гейт онбордингом, как у соседних
        сигналов: технический toast поверх мастера первого запуска сбивает с толку."""
        self._voice_autostart_failed = not ok
        if not ok and not self._needs_onboarding():
            self.notify(
                "voice-сервис не поднялся — подробности: gc-voice status",
                severity="warning",
            )

    def _start_auto_update_check(self) -> None:
        """Тонкая обёртка над `_maybe_auto_check`: реальные зависимости
        (check_for_update/record_check_result/upgrade_gate.read_state/install_source),
        запуск в фоновом воркер-треде — сеть не блокирует старт TUI, ошибки тихи."""
        import time

        from gc_core import app_version, upgrade_gate
        from gc_core.updater.check import check_for_update, record_check_result

        settings = self._settings
        state = upgrade_gate.read_state()
        src = app_version.install_source()
        installed = src.commit if src else None

        def _on_available(fresh: bool) -> None:
            self._update_available = (
                True  # постоянный сигнал: brand-строка + запись в /notices
            )
            if self.is_running:
                if fresh:
                    self.notify(
                        _AUTO_UPDATE_NOTICE, severity="information"
                    )  # toast только при свежей находке
                # M3: главный экран, а не активный (self.screen) — иначе, если авто-чек
                # приходит, пока сверху онбординг/PostUpdate/Settings, строка в карточке
                # так и не появится за всю сессию.
                scr = self._main_screen()
                if scr is not None and hasattr(scr, "refresh_update_notice"):
                    scr.refresh_update_notice()  # дорисовать строку в brand-блоке

        def _on_result(result) -> None:
            self._update_check_result = (
                result  # вердикт сессии: хаб настроек и секция берут его
            )
            self._refresh_update_glyph()

        def _done() -> None:
            self._update_checking = (
                False  # проверка завершилась (в т.ч. по гейту/ошибке)
            )
            self._refresh_update_glyph()

        self._update_checking = True
        self._refresh_update_glyph()

        def _work() -> None:
            try:
                _run_auto_check_safely(
                    settings,
                    state,
                    time.time(),
                    installed,
                    check=check_for_update,
                    record_result=record_check_result,
                    on_available=lambda fresh: self.call_from_thread(
                        _on_available, fresh
                    ),
                    on_result=lambda r: self.call_from_thread(_on_result, r),
                )
            finally:
                try:
                    self.call_from_thread(_done)
                except Exception:
                    pass  # выход из приложения во время чека: event-loop уже закрыт,
                    # а поток жив — call_from_thread бросил бы RuntimeError

        self.run_worker(_work, thread=True, name="auto-update-check")

    def _refresh_update_glyph(self) -> None:
        """Перерисовать строку «Обновление» в хабе настроек, если он открыт: вердикт
        стартового чека приходит асинхронно, юзер может уже сидеть в настройках."""
        scr = self.screen if self.is_running else None
        if scr is not None and hasattr(scr, "refresh_update_glyph"):
            scr.refresh_update_glyph()

    def _needs_onboarding(self) -> bool:
        from gc_core import onboarding

        return self._onboarding_mode == "review" or (
            self._onboarding_mode == "auto"
            and onboarding.is_unconfigured(self._settings)
        )

    def get_default_screen(self) -> Screen:
        from gc_tui.screens.onboarding import OnboardingScreen

        if self._needs_onboarding():
            return OnboardingScreen(
                settings=self._settings,
                config_path=self._onboarding_config_path,
                review_all=self._onboarding_mode == "review",
                on_commit=self._finish_onboarding,
                deactivate_profile=self._onboarding_deactivate_profile,
                degraded_note=self._onboarding_degraded,
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
        from gc_tui.screens.post_update import PostUpdateScreen

        if any(isinstance(s, PostUpdateScreen) for s in self.screen_stack):
            return
        self.push_screen(
            PostUpdateScreen(
                upgrade=self._upgrade_info,
                settings=self._settings,
                config_path=self._config_path,
            )
        )

    def set_config_path(self, path: Optional[str | Path]) -> None:
        """Единая точка присвоения `_config_path`: путь + снимок мтайм-набора (спека
        §3.3) одним движением. Вызывать ровно тогда, когда база окна уже фактически
        соответствует файлу по этому пути (main() — конфиг только что загружен
        из него; финал онбординга — следом сразу идёт reload_config()).

        Без немедленного снимка первая же проверка reload_config_if_changed()
        в новом окне видит файл как «изменившийся» (снимка ещё нет) и зря
        перечитывает конфиг и инвалидирует раннер — хотя окно только что само
        прочитало этот же файл.

        Хранится строка: онбординг отдаёт `Path`, обычный запуск — `str`, а
        экраны (/config) читают путь как строку."""
        self._config_path = str(path) if path else None
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
        return (
            None if ref is None else (ref.name, str(ref.path), ref.root_kind)
        )

    def _active_profile_name_for_mtime(self) -> Optional[str]:
        """Действующее имя профиля для мтайм-набора (спека §3.3 — «выбор, не
        снимок»): сессионный оверрайд, если задан, ИНАЧЕ базовый указатель.

        Набор обязан следить за файлом ТОГО профиля, что реально мержится сейчас
        (effective), а не только за пойнтером из файла — иначе правка файла
        сессионно выбранного профиля никогда не всплывёт в
        `reload_config_if_changed` (ревью Task 11: до этой правки набор всегда
        брал имя только из профиля базы окна, игнорируя сессионный
        выбор — редактирование выбранного в сессии профиля было невидимо
        бессрочно)."""
        override = self._session_overrides.get("profile")
        if override:
            return override
        return self.base_settings().runtime.active_profile or None

    def _compute_mtime_set(self) -> dict:
        """Снимок мтайм-набора ДЕЙСТВУЮЩЕГО указателя (спека §3.3) — только
        `stat()`, без парсинга/валидации конфига (используется в set_config_path/
        reload_config/reload_config_if_changed — связывание пути и проверка
        свежести не обязаны гонять полную загрузку ради снимка)."""
        if not self._config_path:
            return {}
        return self._mtime_set_for(self._active_profile_name_for_mtime())

    def _mtime_set_for(self, profile_name: Optional[str]) -> dict:
        """Мтайм-набор под ЯВНО названным профилем — перечитка снимает его для
        состояния, которое ещё только собирает (`_plan_reload`)."""
        if not self._config_path:
            return {}
        from gc_core.config import profile_mtime_set

        try:
            return profile_mtime_set(self._config_path, profile_name)
        except OSError:
            return {}

    def _load_base_settings(self):
        """`load_settings` под флагом запуска `--profile` — база окна.

        Стек под флагом не собрался — отказ уходит наверх, к тому же гейту, что
        держит окно на битом config.yml (`reload_config_if_changed`): прежняя база,
        причина в `/notices`. Флаг при этом не гасится и профиль не подменяется
        указателем из файла — ни того, ни другого отказ не заслуживает:

        • отличить «профиль исчез навсегда» от мгновенной гонки записи (соседнее
          окно или редактор дописывает файл профиля ровно сейчас) одной неудачной
          попыткой нечем — а цена ошибки несимметрична: снятый флаг не возвращается
          до перезапуска, и `--profile ""` молча воскрешает сломанный профиль на
          первом же сохранении из /settings (ровно та регрессия, ради которой флаг
          и заведён), тогда как лишний раз оставшаяся прежней база живёт до
          следующей проверки свежести;
        • подмена профиля указателем поднимает В РАБОТУ тот самый профиль, от
          которого человек ушёл флагом, — со своими адресами и моделями;
        • `reload_config` до этого места ничего не меняет, поэтому снимок
          мтайм-набора отказ не двигает: следующий заход в Настройки пробует снова,
          и дописанный (или возвращённый) файл чинит окно сам.

        Ловим только классы, которыми загрузчик докладывает о СОСТОЯНИИ файлов
        (`ValueError` — битый YAML/схема/нерезолвимый указатель, `OSError` — файл не
        читается), и лишь затем, чтобы назвать виновником флаг, а не config.yml.
        `TypeError`/`KeyError` — дефект кода, и под видом «профиль больше не
        собирается» он жил бы до первого чтения логов."""
        from gc_core.config import load_settings

        name = self._profile_override
        if name is None:
            return load_settings(self._config_path)
        try:
            return load_settings(self._config_path, profile_override=name)
        except (ValueError, OSError) as exc:
            what = (
                f'профиль "{name}" из флага --profile'
                if name
                else 'флаг --profile ""'
            )
            raise ValueError(f"{what}: {exc}") from exc

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
        профиля, ставший битым под новой базой, — санация (§9.4); ключи с флагом
        `restart` (BOOT_FROZEN_KEYS), разошедшиеся между старой и новой
        эффективной базой, — notice (§3.2). Применяются они при этом сразу:
        `_threads_dir` — новый каталог, executor — новые пути на пересборке.

        База собирается под флагом запуска `--profile` (`_profile_override`) — тем
        же оверрайдом, что и на старте: перечитывание без него молча возвращало
        указатель из файла.

        Атомарно: всё новое состояние (база, сессионный слой под ней, действующие
        настройки, мтайм-набор, пере-мерж сессионного профиля) собирается и
        проверяется в `_plan_reload`, ничего не трогая; применяет его одним
        движением `_commit_reload`. Сбой на любом шаге сборки — окно целиком на
        прежнем состоянии (исключение уходит вызывающему: `_reload_config_or_notice`
        превращает его в notice `config_reload_failed`)."""
        try:
            old_effective = self.effective_settings()
        except ValueError:
            # Сессионный слой не проходит схему уже сейчас (мета треда старой
            # версии) — его снимет `_plan_reload`; для сравнения restart-ключей
            # хватит базы: сессия путей не задаёт (`SESSION_KEYS`).
            old_effective = self._settings
        old_mtime_set = self._config_mtime_set
        old_source = self._profile_source
        old_overrides = dict(self._session_overrides)
        plan = self._plan_reload()
        self._commit_reload(plan)
        # Дальше — только следствия уже применённого состояния.
        self.invalidate_runner()
        if plan.profile_dropped or old_mtime_set != plan.mtime_set:
            self._circuit_breaker.reset()
        if plan.session_overrides != old_overrides:
            try:
                self.persist_session_overrides()
            except OSError:
                pass  # тред недоступен — в памяти уже чисто, персист догонит позже
        for notice in plan.notices:
            push_runtime_notice(notice)
        if plan.activated_profile is not None:
            name, loaded = plan.activated_profile
            self._maybe_notice_profile_activation(
                name, loaded.profile_meta, loaded
            )
        self._notice_profile_source_change(old_source)
        self._notice_boot_frozen_diff(old_effective, self.effective_settings())
        self._notice_auto_routing_unavailable()
        self._notify_screen_config_changed()

    def _plan_reload(self) -> _ReloadPlan:
        """Собрать новое состояние окна в локальных переменных — без единой
        записи в `self`. Порядок: база (файлы + CLI + рантайм TUI) → сессионный
        профиль под ней (не собирается — снять, как `sanitize_session_profile`) →
        мтайм-набор ДЕЙСТВУЮЩИМ именем (сессионный оверрайд, если он пережил
        санацию, иначе новый указатель: `meta.mtime_set` base-only загрузки не
        включает кандидатные пути сессионно выбранного профиля — ревью Task 11) →
        сессионный слой (не проходит проверку под новой базой — снять с notice) →
        действующие настройки → каталог тредов новой базы."""
        from gc_core.thread_store import resolve_threads_dir

        base = self._with_launch_flags(self._load_base_settings())
        snap = self._config_snapshot()
        overrides = dict(snap.overrides)
        notices: list = []
        profile_dropped = False
        activated = None
        base_name = base.runtime.active_profile or None
        name = overrides.get("profile")
        session_base = base
        remerged = None
        if name is not None and name != (base_name or ""):
            try:
                remerged = self._assemble_session_profile(name)
            except Exception as exc:
                if not name:
                    # Сессионное снятие профиля — дверь наружу, его не снимают:
                    # деградация на базу, как у `_profile_remerge_settings`.
                    remerged = base
                    notices.append(_remerge_failed_notice(name, exc))
                else:
                    overrides.pop("profile")
                    profile_dropped = True
                    notices.append(_stale_profile_notice(name, exc, base_name))
            else:
                activated = (name, remerged)
        if remerged is not None:
            session_base = remerged
        mtime_set = self._mtime_set_for(overrides.get("profile") or base_name)
        profile_cache = (
            ((frozenset(mtime_set.items()), name), remerged)
            if remerged is not None
            else None
        )
        try:
            effective = self._build_effective_entry(session_base, overrides)
        except ValueError:
            overrides, dropped = self._drop_invalid_session_keys(
                session_base, overrides
            )
            notices.extend(dropped)
            effective = self._build_effective_entry(session_base, overrides)
        return _ReloadPlan(
            settings=base,
            session_overrides=overrides,
            effective=effective,
            profile_cache=profile_cache,
            mtime_set=mtime_set,
            threads_dir=resolve_threads_dir(base),
            notices=tuple(notices),
            profile_dropped=profile_dropped,
            activated_profile=activated,
        )

    def _commit_reload(self, plan: _ReloadPlan) -> None:
        """Применить собранное `_plan_reload` — единственное место, где перечитка
        пишет состояние окна. Только присваивания под замком: читатель из
        worker-потока (сборка раннера) видит либо всё прежнее, либо всё новое."""
        with self._config_lock:
            self._config_generation += 1
            self._settings = plan.settings
            self._session_overrides = plan.session_overrides
            self._session_profile_cache = plan.profile_cache
            self._effective_cache = plan.effective
            self._config_mtime_set = plan.mtime_set
            self._threads_dir = plan.threads_dir

    def _config_snapshot(self) -> _ConfigSnapshot:
        """Согласованный снимок состояния конфига (одним заходом под замком)."""
        with self._config_lock:
            return _ConfigSnapshot(
                settings=self._settings,
                overrides=dict(self._session_overrides),
                mtime_set=dict(self._config_mtime_set or {}),
                profile_cache=self._session_profile_cache,
                effective=self._effective_cache,
                generation=self._config_generation,
            )

    @staticmethod
    def _effective_key(base, overrides: dict) -> tuple:
        from gc_tui.session_settings import SESSION_KEYS

        layer = tuple(
            sorted(
                (k, v)
                for k, v in overrides.items()
                if k != "profile" and k in SESSION_KEYS
            )
        )
        return (base, layer)

    @classmethod
    def _build_effective_entry(cls, base, overrides: dict) -> tuple:
        """(ключ, LoadedSettings): база + сессионный слой.
        `ValueError` — слой не проходит проверку схемы."""
        key = cls._effective_key(base, overrides)
        loaded = base.with_overrides(dict(key[1]), layer="session")
        return (key, loaded)

    @staticmethod
    def _drop_invalid_session_keys(base, overrides: dict) -> tuple:
        """Сессионные ключи, не проходящие проверку под новой базой, — снять
        (как санация снимает несобираемый профиль): `(уцелевший слой, notices)`.
        Слой мог прийти из меты треда старой версии или опереться на то, чего в
        новом конфиге нет, — окно без перечитки из-за него оставаться не должно."""
        from gc_tui.session_settings import SESSION_KEYS

        kept = dict(overrides)
        notices = []
        for key, value in overrides.items():
            if key == "profile" or key not in SESSION_KEYS:
                continue
            try:
                base.with_overrides({key: value}, layer="session")
            except ValueError as exc:
                kept.pop(key)
                reason = _validation_reason(exc)
                notices.append(
                    Notice(
                        "warning",
                        "session_override_dropped",
                        f'сессионное "{key}" = {value!r} снято: не проходит '
                        f"проверку под перечитанным конфигом ({reason})",
                        (),
                    )
                )
        return kept, notices

    def _with_tui_runtime(self, loaded):
        """Рантайм TUI поверх свежей загрузки. `launch_root` — снапшот старта: без
        переноса он съезжал бы на живой cwd при первом же сохранении настроек —
        /diff перестаёт работать, а периметр path_policy уходит со снапшота."""
        from gc_tui.runtime_config import apply_tui_runtime

        previous = self._settings.runtime.launch_root
        return apply_tui_runtime(loaded, launch_root=previous)

    def _notify_screen_config_changed(self) -> None:
        """Сказать главному экрану, что действующий конфиг сменился.

        Экран кэширует куски конфига один раз на секцию (голосовой flow строится
        на voice-секцию), поэтому сигнал обязателен КАЖДОМУ пути смены: и
        перечитке с диска, и сессионному выбору профиля. Точка одна — иначе у
        второго пути заводится свой канал и расходится с первым (сессионный
        профиль со своей секцией `voice` оставлял Ctrl+T на прежнем endpoint)."""
        if not self.is_running:
            return
        main = self._main_screen()
        if main is not None and hasattr(main, "config_reloaded"):
            main.config_reloaded()

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
        self._reload_config_or_notice()

    def _reload_config_or_notice(self) -> None:
        """Перечитка ПОД ГЕЙТОМ: отказ загрузчика (битый файл, непрочитанный слой,
        не собравшийся стек флага `--profile`) — уведомление, а не крэш Textual.

        Точка одна на всех вызывающих `reload_config` из UI: своя редакция того же
        перехвата разъехалась бы с первой — а окно, роняющее себя из-за диагностики
        чужого файла, теряет несохранённое у человека, который в этом файле не
        виноват."""
        try:
            self.reload_config()
        except Exception as exc:
            push_runtime_notice(
                Notice(
                    "warning",
                    "config_reload_failed",
                    f"config.yml не перечитан, окно осталось на прежней базе: {exc}",
                    ("config.yml",),
                )
            )

    def base_settings(self) -> LoadedSettings:
        """База окна (`LoadedSettings`) без сессионного слоя — то, что правит
        /settings и что получают экраны при ОТКРЫТИИ (/config, /mcp, /diff,
        /skills, /settings — и вход в секцию /settings), БЕЗ автоперечитки.
        Свежесть config.yml нужна только там, где он редактируется (Settings), и
        вызывается ЯВНО на пути его открытия (main._cmd_settings,
        settings._sync_config_from_app) — иначе два окна правили бы файл вслепую
        (дизайн 2026-07-21 §4).

        НЕ для рендера — effective_settings() (база + сессионные
        переопределения) зовётся на каждый кадр; здесь — только база."""
        return self._settings

    @property
    def last_runner_error(self) -> Optional[str]:
        """Ошибка прогона активного раннера (текст для строки «└»)."""
        return self._last_runner_error

    @last_runner_error.setter
    def last_runner_error(self, err: Optional[str]) -> None:
        # Ошибка принадлежит прогону, в котором случилась: пара «раннер и
        # run_seq». Следующий прогон того же раннера её не наследует.
        self._last_runner_error = err
        runner = self.active_runner
        self.last_runner_error_run = (runner, getattr(runner, "run_seq", None))

    @property
    def image_store(self) -> ImageStore:
        """Хранилище картинок сессии: корень — по правилу ядра (`ImageStore.for_config`),
        тот же, что у контекста запуска и охраны путей. На диске объект ничего не
        создаёт, поэтому собирается на каждое обращение."""
        return ImageStore.for_config(self.base_settings())

    def effective_settings(self) -> LoadedSettings:
        """Действующие настройки окна: база + сессионные переопределения — то,
        что видят и UI, и агент.

        Единая точка чтения: витрины (палитра, секция настроек) и сборка
        executor обязаны брать настройки отсюда, а не из базы — иначе
        переключённое в сессии значение не доедет. `profile` — особый ключ
        (спека §9.1): база не всегда `base_settings()` — сессия могла выбрать
        другой профиль, и тогда база — его пере-сборка (`_session_base_settings`,
        закэшированная).

        Неизменяемый объект из ОДНОГО снимка состояния: worker-поток, собирающий
        раннер во время перечитки, получает либо прежние настройки целиком, либо
        новые целиком."""
        return self._effective_entry()[1]

    def _effective_entry(self) -> tuple:
        """(ключ, LoadedSettings) по согласованному снимку. Сборка — вне замка
        (пере-мерж профиля ходит на диск и пишет notice, а мост notice на
        UI-поток под замком был бы взаимоблокировкой с перечиткой); кэш
        пишется, только если состояние за это время не перечитали."""
        snap = self._config_snapshot()
        base = self._session_base_settings(snap)
        key = self._effective_key(base, snap.overrides)
        cached = snap.effective
        if cached is not None and _same_effective_key(cached[0], key):
            return cached
        entry = self._build_effective_entry(base, snap.overrides)
        with self._config_lock:
            if self._config_generation == snap.generation:
                self._effective_cache = entry
        return entry

    def _session_base_settings(self, snap: Optional[_ConfigSnapshot] = None):
        """База для effective_settings(): база окна, либо, если сессия выбрала
        ДРУГОЙ профиль (§9.1), — пере-сборка базы под ним
        (`_profile_remerge_settings`, закэшированная по мтайм-набору+имени —
        рендер не должен гонять загрузчик каждый кадр). override "" —
        сессионное снятие (§9.1, ревизия 2026-07-28): база задаёт профиль,
        сессия работает чистым конфигом — пере-сборка БЕЗ профиля."""
        if snap is None:
            snap = self._config_snapshot()
        override_name = snap.overrides.get("profile")
        base_name = snap.settings.runtime.active_profile or None
        if override_name is None or override_name == (base_name or ""):
            return snap.settings
        return self._profile_remerge_settings(override_name, snap)

    def _profile_remerge_settings(
        self, name: str, snap: Optional[_ConfigSnapshot] = None
    ):
        """Пере-прогон загрузки с `profile_override=name` — то же, что делает
        CLI `--profile` (спека §9.1). Кэш-ключ — (мтайм-набор, имя): любое
        реальное изменение файлов ИЛИ смена сессионного выбора инвалидирует
        кэш; иначе то же имя при том же наборе отдаётся из кэша без похода на
        диск. Кэш читается из снимка `snap` и пишется, только если окно за время
        сборки не перечитали (перечитка уже положила свой пере-мерж).

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
        if snap is None:
            snap = self._config_snapshot()
        key = (frozenset(snap.mtime_set.items()), name)
        cached = snap.profile_cache
        if cached is not None and cached[0] == key:
            return cached[1]
        try:
            loaded = self._assemble_session_profile(name)
        except Exception as exc:
            fallback = snap.settings
            self._store_profile_cache(snap.generation, (key, fallback))
            push_runtime_notice(_remerge_failed_notice(name, exc))
            return fallback
        self._store_profile_cache(snap.generation, (key, loaded))
        self._maybe_notice_profile_activation(
            name, loaded.profile_meta, loaded
        )
        return loaded

    def _store_profile_cache(self, generation: int, entry: tuple) -> None:
        with self._config_lock:
            if self._config_generation == generation:
                self._session_profile_cache = entry

    def _assemble_session_profile(self, name: str):
        """Стек под профилем `name` — тем же путём, что база окна: файлы с
        `--profile name`, CLI, рантайм TUI. Исключение — профиль не собирается."""
        from gc_core.config import load_settings

        loaded = load_settings(self._config_path, profile_override=name)
        return self._with_launch_flags(loaded)

    def _with_launch_flags(self, loaded):
        """Флаги запуска поверх свежей загрузки: слой CLI и путь `--model`,
        решённый на старте (без повторного решения по новому реестру)."""
        from gc_core.config import apply_cli_overrides

        loaded = apply_cli_overrides(loaded)
        if self._model_flag:
            loaded = loaded.with_overrides(self._model_flag, layer="cli")
        return self._with_tui_runtime(loaded)

    def _maybe_notice_profile_activation(self, name: str, meta, cfg) -> None:
        """Спека §3.5: первая активация ПРОЕКТНОГО профиля в сессии — notice с
        файлом и урлами моделей: файл приезжает с клоном репозитория и уводит
        запросы на свои адреса — пользователь должен видеть это сам, не только в
        логах провайдера. `cfg` — настройки собранного стека (`LoadedSettings`)."""
        ref = meta.ref
        if (
            ref is None
            or ref.root_kind != "project"
            or name in self._profile_notice_seen
        ):
            return
        self._profile_notice_seen.add(name)
        from gc_core.settings import settings_of

        models = settings_of(cfg).models
        urls = sorted(
            {str(entry.url) for entry in models.values() if entry.url}
        )
        urls_text = ", ".join(urls) if urls else "—"
        push_runtime_notice(
            Notice(
                "info",
                "project_profile_activated",
                f'профиль "{name}" — проектный ({ref.path}); модели: {urls_text}',
                (str(ref.path),),
            )
        )

    def sanitize_session_profile(self) -> None:
        """§9.4 (ветка «выбран сессией»): сессионный оверрайд профиля, ставший
        битым (файл удалили/переименовали под новой базой), — снять + notice +
        пересборка + сброс предохранителя. Санация приравнена к смене профиля.

        Ветка «выбран конфигом» (файл базового указателя исчез/невалиден) сюда
        не относится — её покрывает reload_config_if_changed: загрузчик
        бросает исключение, окно остаётся на прежней базе + notice
        `config_reload_failed` (существующий канон, без изменений).

        Точка вызова — attach треда (main.py). Перечитка (`reload_config`)
        санирует тем же правилом и тем же notice, но в собираемом состоянии
        (`_plan_reload`), а не по живому окну."""
        name = self._session_overrides.get("profile")
        base_name = self.base_settings().runtime.active_profile or None
        if name and name != base_name:
            try:
                self.probe_profile_assembly(name)
            except Exception as exc:
                with self._config_lock:
                    self._session_overrides = {
                        k: v
                        for k, v in self._session_overrides.items()
                        if k != "profile"
                    }
                    self._session_profile_cache = None
                self._circuit_breaker.reset()
                try:
                    self.persist_session_overrides()
                except OSError:
                    pass  # тред недоступен — в памяти уже чисто, персист догонит позже
                push_runtime_notice(
                    _stale_profile_notice(name, exc, base_name)
                )
                self.invalidate_runner()
        self._notice_auto_routing_unavailable()

    def sanitize_session_model(self) -> None:
        """Сессионный выбор модели, которого нет в реестре действующего
        профиля (смена профиля, мета треда другого профиля), — снять и
        сказать об этом: иначе выбор молча уходил бы в `default`.
        Слово уровня и `auto` допустимы в любом реестре.

        Пока идёт ран, профиль для него ещё не действует (пересборка — после
        хода), и снятие сменило бы модель посреди хода: оно откладывается до
        конца рана или пересборки раннера — оба места зовут санацию снова."""
        from gc_core.config_modules.model_registry import choice_in_registry

        runner = self.active_runner
        if runner is not None and runner.is_running():
            return
        choice = self._session_overrides.get("model")
        loaded = self.effective_settings()
        if not choice or choice_in_registry(choice, loaded.settings):
            return
        with self._config_lock:
            self._session_overrides = {
                k: v
                for k, v in self._session_overrides.items()
                if k != "model"
            }
        try:
            self.persist_session_overrides()
        except OSError:
            pass  # тред недоступен — в памяти уже чисто, персист догонит позже
        profile = loaded.runtime.active_profile or "основной"
        push_runtime_notice(
            Notice(
                "info",
                "session_model_dropped",
                f"модели {choice} нет в профиле {profile} — выбор снят",
                (choice, profile),
            )
        )

    def apply_model_choice(self, choice: str) -> None:
        """Сессионный выбор модели: `auto`, слово уровня или алиас.
        Executor не пересобирается — роутер читает выбор через
        `_choice_provider` на каждом такте."""
        self.apply_session_override("model", choice)

    def probe_profile_assembly(self, name: str) -> None:
        """Собирается ли конфиг с профилем `name` — проба, а не применение: ответ
        выбрасывается, спрашивается лишь «собирается ли», исключение уходит
        вызывающему как есть (`ValueError` с путём и причиной).

        Побочки сбора (конвейер проекций, предупреждения TLS) глобальны и
        необратимы — их несёт `_profile_remerge`, который профиль действительно
        применяет. Проба, идущая другим поводом (санация, гейт выбора, витрина
        пикера), не должна двигать рантайм: сорвавшийся следом персист оставил бы
        процесс перепривязанным к профилю, которого нет ни в сессии, ни в мете.

        Имя публичное: судья «годен ли профиль» ОДИН на всех, и метку пикера
        (`toggles.profile_picker_options`) ставит он же. Вторая редакция правила
        разошлась бы с первой — витрина звала бы Enter туда, где Enter отказывает."""
        from gc_core.config import load_settings

        load_settings(self._config_path, profile_override=name)

    def switch_session_profile(self, name: str) -> None:
        """Переключить профиль сессии (спека §9.1/§9.3): применяет как обычный
        сессионный оверрайд (персист + restore на attach — существующим
        механизмом), но с эффектами, специфичными для профиля — предохранитель
        и кэш пере-мержа сбрасывает `set_session_override`; здесь — то, что
        специфично для МОМЕНТА выбора: boot-frozen-diff notice и, при живом ране,
        notice отложенного применения (пересборка сама откладывается существующим
        `invalidate_runner` — единый механизм с обычной перечиткой конфига).

        Профиль, с которым конфиг не собирается, отклоняется ДО применения:
        `_profile_remerge` деградирует на прежнюю базу молча для рендера, и без
        гейта выбор доезжал до футера и до меты треда — витрина называла профиль,
        которого нет ни в одном запросе, а негодное имя восстанавливалось на
        каждом attach. Проба идёт первой: после `apply_session_override` имя уже
        в мете, и снимать его пришлось бы вторым действием, по дороге к которому
        есть свои способы не доехать.

        Снятие (`name == ""`) пробы не знает — это дверь наружу, и судить её по
        сборке снимаемого значило бы запереть человека внутри битого выбора."""
        base_name = self.base_settings().runtime.active_profile or None
        if name and name != base_name:
            self.probe_profile_assembly(name)
        old_effective = self.effective_settings()
        self.apply_session_override("profile", name)
        self.sanitize_session_model()
        self._notice_boot_frozen_diff(old_effective, self.effective_settings())
        self._notice_auto_routing_unavailable()
        self._notify_screen_config_changed()
        runner = self.active_runner
        live = runner is not None and runner.is_running()
        self.invalidate_runner()
        if live:
            what = f'профиль "{name}"' if name else "отключение профиля"
            push_runtime_notice(
                Notice(
                    "info",
                    "profile_switch_deferred",
                    f"{what}: подключения и реестр моделей применятся после завершения текущей задачи",
                    (),
                )
            )

    def _notice_profile_source_change(
        self, old_source: Optional[tuple]
    ) -> None:
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
        push_runtime_notice(
            Notice(
                "warning",
                "profile_source_changed",
                f'профиль "{new_name}" теперь берётся из другого файла: '
                f"{new_path} ({new_ru}) вместо {old_path} ({old_ru})",
                (new_path, old_path),
            )
        )

    def _notice_auto_routing_unavailable(self) -> None:
        """Живой уровень — `auto`, а для действующего конфига автовыбор недоступен
        (сменился config.yml, профиль сессии, открыт тред с `auto` в мете): один
        runtime-нотис с причиной — тем же текстом, что у стартового. Базовый
        конфиг сам хранит `level: auto` — причину уже называет карточка
        стартовых уведомлений, второй раз не шлём; повтор того же текста гасит
        `push_runtime_notice`. При восстановлении профильного выбора проверяем
        уведомление после санации, чтобы битый профиль не давал два предупреждения."""
        if self._choice_provider() != "auto":
            return
        notice = auto_routing_unavailable_notice(self.effective_settings())
        if notice is None or notice == auto_routing_unavailable_notice(
            self.base_settings()
        ):
            return
        push_runtime_notice(notice)

    def _notice_boot_frozen_diff(
        self, old: LoadedSettings, new: LoadedSettings
    ) -> None:
        """Спека §3.2: ключи с флагом `restart` разошлись между старой и новой
        эффективной базой при пересборке. Значение уже действует (перечитка
        переводит `_threads_dir`, executor берёт новые пути на пересборке), но
        живой ран и его лог остаются на прежних — notice, не молчание. Текст
        notice — прежний («вступит в силу со следующего запуска»).

        Сравниваем, только если старое значение реально известно (не None): `None`
        типично значит «настройки ещё не резолвили значение по умолчанию»
        (например, свежесозданный `TuiApp` в тестах без путей) — это не смена
        значения пользователем, шуметь не о чем."""
        changed = [
            dotted
            for dotted in BOOT_FROZEN_KEYS
            if _setting_at(old, dotted) is not None
            and _setting_at(old, dotted) != _setting_at(new, dotted)
        ]
        if not changed:
            return
        push_runtime_notice(
            Notice(
                "info",
                "boot_frozen_pending",
                f"{', '.join(changed)} — вступит в силу со следующего запуска",
                tuple(changed),
            )
        )

    def _run_choice_provider(self) -> Callable[[], str]:
        """Выбор для Router'а собираемого рана: сессионный `/model` — живой
        (смена без пересборки), выбор конфига — снимок сборки executor.
        Смена профиля действует со следующего рана: посреди хода `model:`
        нового профиля не подменяет выбор идущего."""
        from gc_core.config_modules.model_registry import configured_choice

        configured = configured_choice(self._session_base_settings().settings)

        def provider() -> str:
            with self._config_lock:
                return self._session_overrides.get("model", configured)

        return provider

    def _choice_provider(self) -> str:
        """Выбор модели для `ModelRouter` (спека §6.4): читает сессионный слой
        напрямую, БЕЗ `effective_settings()` — выбор модели переключается
        без `invalidate_runner` (единственный тоггл с таким свойством), и это
        единственный потребитель, обязанный видеть смену немедленно, без
        пересборки executor."""
        from gc_core.config_modules.model_registry import configured_choice

        snap = self._config_snapshot()
        if "model" in snap.overrides:
            return snap.overrides["model"]
        return configured_choice(self._session_base_settings(snap).settings)

    def active_profile_ref(self):
        """`ProfileRef` действующего (эффективного, сессионно-осознанного) профиля
        или `None` — витрины `/config`/`/status` (спека §12.3a) хотят не только
        имя, но и корень/путь файла, а `effective_settings()` знает только имя.
        Единая точка резолва — `config_modules.profiles.find_active_ref`."""
        import os
        from pathlib import Path

        from gc_core.config_modules.profiles import find_active_ref

        name = self.effective_settings().runtime.active_profile or None
        return find_active_ref(
            name, Path(os.getcwd()), Path(self._config_path or "")
        )

    def display_router(self) -> ModelRouter:
        """Дешёвый Router для витрин (подсказка `/level`, футер, `/status` — спека
        §12.1/§12.2): резолвит из `effective_settings()` БЕЗ сборки executor —
        `resolve()`/`resolve_level()` не ходят в транспорт, только читают снимок
        конфига в памяти. Не путать с `ctx.model_router` активного рана — тот
        собран из снимка на момент СБОРКИ executor (может быть stale между
        пересборками); витрины обязаны видеть смену уровня/профиля сразу
        (§6.4), поэтому строят свой Router от текущего effective_settings()
        на каждый вызов, а не читают executor.

        `choice_provider` переводит сырой `_choice_provider()` (может быть
        литералом `auto`) через `effective_target` (спека auto-routing §5.1) —
        без этого `_resolve_level` падает `ValueError` на `auto`. Вне хода
        `current_level` неизвестен (`None` → `effective_target` отдаёт
        `default`); `live_choice_provider` остаётся сырым, чтобы `live_choice()`
        этого Router'а отвечал режимом, как у Router'а рана. Витрины его сейчас
        не читают: `/status` берёт отсюда модель и предохранитель, а уровень —
        через `toggle_state` (то же правило, что у палитры: `auto`, только пока
        автовыбор доступен)."""
        from gc_core.auto_routing.levels import effective_target
        from gc_core.config import cli_forces_legacy_model
        from gc_core.config_modules.model_registry import effective_choice
        from gc_core.model_router import ModelRouter

        from gc_tui.toggles import auto_available_cfg

        loaded = self.effective_settings()
        available = auto_available_cfg(loaded)
        return ModelRouter(
            loaded,
            choice_provider=lambda: effective_target(
                effective_choice(self._choice_provider(), loaded.settings),
                None,
                available,
            ),
            live_choice_provider=self._choice_provider,
            breaker=self._circuit_breaker,
            force_legacy=cli_forces_legacy_model(loaded),
        )

    def auto_mode_active(self) -> bool:
        """Идёт ли сейчас режим `auto` (спека §7.1): сырой живой уровень —
        литерал `auto`, и фича доступна для эффективного конфига. Футер решает
        по этому флагу, показывать ли `● auto` / `● → ● модель` вместо
        обычной точки уровня."""
        from gc_tui.toggles import auto_available_cfg

        return self._choice_provider() == "auto" and auto_available_cfg(
            self.effective_settings()
        )

    def persist_circuit_breaker(
        self,
        task_id: Optional[str] = None,
        snapshot: Optional[dict] = None,
    ) -> None:
        """Снимок предохранителя моделей (спека §8) в meta треда — переживает
        пересборки executor внутри сессии (владелец состояния — сессия-тред).
        По умолчанию — открытый тред и текущее состояние предохранителя."""
        task_id = task_id or getattr(self.tui_state, "active_task_id", None)
        if not task_id or not self._threads_dir:
            return
        from gc_tui.services.stores.file_store import FileTaskStore
        from gc_tui.session_settings import to_breaker_meta_patch

        if snapshot is None:
            snapshot = self._circuit_breaker.snapshot()
        store = FileTaskStore(
            threads_dir=self._threads_dir, system=self._system
        )
        store.update_tui_meta(task_id, to_breaker_meta_patch(snapshot))

    def thread_run_is_live(self, thread_id: str) -> bool:
        """Ход треда держит живой процесс: этот — своим раннером, другой
        TUI — свежим замком сессии. Иначе «running» в мете — остаток
        процесса, умершего посреди хода."""
        runner = self.active_runner
        if (
            runner is not None
            and runner.is_running()
            and self.run_thread_id(runner) == thread_id
        ):
            return True
        tui_id = getattr(self, "tui_id", "") or ""
        return bool(self._threads_dir and tui_id) and is_held_by_other(
            self._threads_dir, self._system, thread_id, tui_id
        )

    def session_run_is_live(self, thread_id: str) -> bool:
        """Ход сессии жив — для показа статуса. Открытая сессия — признаком
        главного экрана: он знает и ход, который тик ещё не закрыл.
        Прочие — живой процесс (`thread_run_is_live`)."""
        main = self._main_screen()
        if main is not None and thread_id == self.tui_state.active_task_id:
            return main.open_run_is_live()
        return self.thread_run_is_live(thread_id)

    def run_thread_id(self, runner) -> Optional[str]:
        """Тред прогона раннера; None — работа не прогон (сжатие, прогрев)
        или цели нет: тогда она принадлежит открытому треду."""
        target = getattr(runner, "_last_target", None)
        if (
            getattr(runner, "_thread_kind", "run") == "run"
            and target
            and target[1] == self._system
        ):
            return target[0]
        return None

    def _persist_breaker_after_worker(self, runner) -> None:
        """`persist_circuit_breaker`, вызванный ИЗ worker-потока раннера.

        Мост на UI-поток обязателен: persist — полный read-modify-write meta.json
        (`FileTaskStore.update_tui_meta`), а UI-поток на спаде `is_running` пишет в
        тот же документ (статус треда, `persist_session_overrides`). Две полные
        перезаписи от разных потоков теряют одну правку, а прерванная запись
        оставляет обрезанный файл, который потом читается как пустой.

        Тред и снимок берутся здесь, на воркере: снимок принадлежит треду
        прогона, а не открытому. Иначе трип хода сессии B, пока открыта A,
        лёг бы в meta A, а запись, дошедшая до UI-потока после смены сессии,
        унесла бы туда уже загруженное состояние новой.

        RuntimeError из `call_from_thread` — приложение не запущено (тесты,
        headless) или мы уже на UI-потоке: гонки в этом случае нет, пишем
        напрямую, потому что потерять трип хуже."""
        task_id = self.run_thread_id(runner)
        # Отвязанный (остановленный) прогон, чей тред уже не открыт: его
        # предохранитель записан при стопе, а общий объект теперь держит
        # состояние открытого треда — писать его в тред прогона нельзя.
        if runner is not self.active_runner and task_id != getattr(
            self.tui_state, "active_task_id", None
        ):
            return
        snapshot = self._circuit_breaker.snapshot()
        try:
            self.call_from_thread(
                self.persist_circuit_breaker, task_id, snapshot
            )
        except RuntimeError:
            self.persist_circuit_breaker(task_id, snapshot)

    def detach_run_sinks(self) -> None:
        """Остановленный прогон дорабатывает в фоне, отвязанный от окна: его
        стрим, прогресс субагентов и предохранитель окну больше не
        принадлежат. Следующая сборка исполнителя заводит свои; у окна —
        свой предохранитель с тем же состоянием, старый объект остаётся
        отвязанному исполнителю (его поздние трипы — только его)."""
        from gc_core.model_router import CircuitBreaker

        self._circuit_breaker = CircuitBreaker.restore(
            self._circuit_breaker.snapshot()
        )
        pump = getattr(self, "_stream_pump", None)
        if pump is not None:
            pump.close()
        self._stream_pump = None
        self._subagent_progress_sink = None

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
        if (
            dotted == "profile"
            and self._session_overrides.get(dotted) != value
        ):
            self._session_profile_cache = None
            self._circuit_breaker.reset()
        self._session_overrides[dotted] = value

    def apply_session_override(self, dotted: str, value) -> None:
        """Переопределить ключ и сохранить набор в метаданные треда — атомарно.

        Не записалось (каталог тредов недоступен, диск полон, отвалившийся сетевой
        путь) — откатываем: вызывающий показывает «✗ не сохранено» и оставляет на
        экране прежнее значение, поэтому effective_settings() обязан отдавать ровно
        его, иначе следующий ход агента пойдёт в необъявленном пользователю режиме."""
        prev = dict(self._session_overrides)
        self.set_session_override(dotted, value)
        try:
            self.persist_session_overrides()
        except OSError:
            self._session_overrides = prev
            raise

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

    def load_session_overrides(self, overrides: dict) -> bool:
        """Заменить набор переопределений целиком — при открытии треда.
        Пустой набор возвращает сессию к умолчаниям конфига.

        Возвращает True, если набор реально изменился: пересборка executor стоит
        MCP-дискавери в worker-потоке (пользователь ждёт её до начала хода), и
        платить за неё, когда у треда те же значения, незачем."""
        new = dict(overrides or {})
        # Мета треда старой версии или ручная правка: ключ, не проходящий
        # схему, снимается с уведомлением — иначе рендер окна падал бы на
        # каждой сборке действующих настроек.
        new, dropped = self._drop_invalid_session_keys(
            self.base_settings(), new
        )
        for notice in dropped:
            push_runtime_notice(notice)
        changed = new != self._session_overrides
        self._session_overrides = new
        # Профиль из меты сначала санируется на attach: чтение его уровня до
        # санации дало бы второе предупреждение о временно битом выборе.
        if "profile" not in new:
            self._notice_auto_routing_unavailable()
        return changed

    def persist_session_overrides(self) -> None:
        """Сохранить переопределения в метаданные текущего треда (meta.tui).

        Тред может быть ещё не создан (пустая сессия) — тогда тихо выходим:
        значения живут в памяти и запишутся при следующем персисте, когда тред
        появится. Ошибки записи не гасим — вызывающий показывает флеш."""
        task_id = getattr(self.tui_state, "active_task_id", None)
        if not task_id or not self._threads_dir:
            return
        from gc_tui.services.stores.file_store import FileTaskStore
        from gc_tui.session_settings import to_meta_patch

        store = FileTaskStore(
            threads_dir=self._threads_dir, system=self._system
        )
        store.update_tui_meta(task_id, to_meta_patch(self._session_overrides))

    def invalidate_runner(self) -> None:
        """Конфиг сессии изменился — следующий run должен собраться заново.
        Живой run не трогаем: помечаем stale, пересборка после завершения.
        Единая точка сброса раннера: сюда же делегирует reload_config."""
        runner = self.active_runner
        if runner is not None and runner.is_running():
            self._runner_stale = True
            return
        _release_runner(runner)
        self.active_runner = None
        self._runner_stale = False

    async def on_unmount(self) -> None:
        """Выход приложения — отпустить всё, что держит его из модулей ядра.

        Сначала `stop()`, и это не вежливость. Пока агент ждёт ответа человека,
        его поток сидит в `queue.get()` внутри `asyncio.to_thread`, то есть в
        потоке пула `concurrent.futures`, а такие потоки интерпретатор ждёт на
        выходе. `/quit` зовёт `app.exit()` без остановки прогона, и процесс
        оставался жить, пока в очередь не придёт строка. `stop()` кладёт туда
        пустую (`cancel_ask_human`) и снимает ожидание подтверждения.

        После стопа — короткое ожидание потока хода (`STOP_SETTLE_SEC`):
        ядро закрывает ход записью итога, иначе он остался бы без неё. Ждём
        в потоке пула, не на loop: поток хода ходит на UI-поток мостом
        `call_from_thread` (дельта стрима, предохранитель, подтверждение), и
        заблокированный loop держал бы его до таймаута.

        Затем логгер прогона (см. `AgentRunner.release`) и подписка на
        runtime-уведомления: и то и другое живёт в модульных реестрах до конца
        процесса и держит приложение целиком, если его не снять.
        """
        runner = self.active_runner
        stop = getattr(runner, "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception:
                pass
        wait = getattr(runner, "wait", None)
        if callable(wait):
            await asyncio.to_thread(wait, STOP_SETTLE_SEC)
        _release_runner(runner)
        unregister_runtime_notices_listener(self._on_runtime_notice)
        self._clear_image_pocket()
        atexit.unregister(self._clear_image_pocket)

    def watch_app_focus(self, focused: bool) -> None:
        """Окно терминала потеряло фокус — погасить подсветку картинок в репликах
        всех экранов стека (лента под модальным окном, превью): «указатель ушёл»
        терминал уже не пришлёт. Одна подписка на приложение, а не на каждую
        реплику; пустой стек (выход) — нечего гасить."""
        if focused:
            return
        for screen in self.screen_stack:
            for reply in screen.query("UserReplyContainer, NestedUserReply"):
                reply.clear_image_hover()

    def _clear_image_pocket(self) -> None:
        """Карман картинок этого процесса (спека §3) — только на выходе: штатном
        (on_unmount) и аварийном (хук atexit). Не на отправке и не на двойном Esc:
        сообщение, отправленное посреди запуска, ядро коммитит в тред позже, на
        границе шага, и берёт файл из кармана."""
        try:
            self.image_store.clear_own_staging()
        except OSError:
            pass

    def _finish_onboarding(self) -> None:
        """Финал онбординга (auto и review одинаково): reload_config + переход
        на главный экран — «завершил настройку → стартует агент»."""
        self.set_config_path(self._onboarding_config_path)
        # Тем же гейтом, что и остальные перечитки из UI: файл могли сломать в зазоре
        # между записью визарда и финалом, и трейсбек поверх только что законченной
        # настройки — цена, несоразмерная поводу. Причина уходит в /notices.
        self._reload_config_or_notice()
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
        if (
            self.active_runner is not None
            and self._runner_stale
            and not self.active_runner.is_running()
        ):
            _release_runner(self.active_runner)
            self.active_runner = None
            self._runner_stale = False
            # Снятие выбора, отложенное сменой профиля посреди хода.
            self.sanitize_session_model()
        if self.active_runner is not None:
            return self.active_runner
        from gc_tui.services.agent_runner import AgentRunner

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
            runner = runner_ref[0] if runner_ref else None
            self._persist_breaker_after_worker(runner)
            if runner is None or runner is not self.active_runner:
                return
            if getattr(runner, "_marked_stopped", False):
                return
            from gc_core.turn_error import describe_turn_error

            self.last_runner_error = describe_turn_error(exc)

        def _on_runner_complete(outcome: Optional[str]) -> None:
            runner = runner_ref[0] if runner_ref else None
            self._persist_breaker_after_worker(runner)
            if runner is None or runner is not self.active_runner:
                return
            self.last_terminal_outcome = outcome

        self.active_runner = AgentRunner(
            executor_factory=factory,
            on_error=_on_runner_error,
            on_complete=_on_runner_complete,
        )
        runner_ref.append(self.active_runner)
        self._runner_stale = False
        # Раннер пересоздаётся (смена настроек сессии, /stop): выбранный режим
        # принадлежит окну, а не раннеру, и едет в каждый новый.
        if self._permission_mode is not None:
            self.active_runner.set_permission_mode(self._permission_mode)
        return self.active_runner

    def _build_real_executor(self):
        """Build the real AgentExecutor from config. Called when no factory injected."""
        from gc_core.agent_executor import AgentExecutor
        from gc_core.config import (
            cli_forces_legacy_model,
        )
        from gc_core.config_modules.auth import build_auth_connector
        from gc_core.run_context import acreate_run_context
        from gc_core.settings import settings_of

        from gc_tui.services.mcp_progress import McpProgress
        from gc_tui.toggles import tui_agent_settings

        # Своё у TUI поверх действующих настроек: промпт-ассеты, адаптивный bounce
        # текстового ответа и режим судьи /gate (CLI/server/swarm их не ставят).
        loaded = tui_agent_settings(self.effective_settings())
        choice_provider = self._run_choice_provider()
        auth = build_auth_connector(loaded)
        progress = McpProgress()
        progress.begin(list(settings_of(loaded).mcp.servers))
        self._mcp_progress = progress
        from gc_tui.services.stream_pump import STREAM_REFRESH_MS, StreamPump

        self._stream_pump = StreamPump(
            self._stream_sink,
            signal=lambda: self.call_from_thread(self._stream_signal),
            throttle_s=STREAM_REFRESH_MS / 1000,
        )
        # Своя `asyncio.run` на сборку — как и у хода ниже. Раньше здесь
        # стояла ВТОРАЯ нативная сборка контекста (синхронная `create_run_context`),
        # и docs объясняли её привязкой MCP-сессий к loop'у. Обоснование было
        # неверным: синхронная сборка уводила дискавери в `run_coro_blocking`,
        # то есть на такой же одноразовый loop, умиравший до первого хода.
        # Настоящая причина была проще — эта функция синхронная и своего loop
        # не имеет; фабрика зовётся в рабочем потоке раннера, где живого loop
        # ещё нет, поэтому `asyncio.run` здесь законен.
        #
        # Отложенное решение (не в этой ветке): один долгоживущий loop в
        # рабочем потоке на всё приложение. Он же убрал бы TLS-рукопожатие,
        # которое TUI платит на каждое сообщение, закрывая пул после хода.
        import asyncio

        async def _abuild_ctx():
            # Пул httpx ЭТОГО loop закрываем здесь же: `asyncio.run` ниже убьёт
            # loop, а реестр клиентов ключуется по нему. Ход и `/compact` так и
            # делают, а сборка — нет: клиент легаси http/sse MCP оставался в
            # реестре с открытыми сокетами, и так на каждой пересборке (смена
            # конфига сессии, `/profile`).
            from gc_core.utils.async_http import aclose_clients

            try:
                return await acreate_run_context(
                    loaded,
                    auth_connector=auth,
                    on_progress=progress.update,
                    on_stream_delta=self._stream_pump.on_delta,
                    is_tui=True,
                    choice_provider=choice_provider,
                    circuit_breaker=self._circuit_breaker,
                    # Легаси-форс (спека §2/§12.1): CLI --model/--completions_url
                    # поверх непустого реестра — тот же детект, что уже
                    # используют CLI/server (config.cli_forces_legacy_model); без
                    # него ран резолвил бы через реестр, расходясь с честным
                    # хинтом /level (display_router).
                    force_legacy=cli_forces_legacy_model(loaded),
                )
            finally:
                await aclose_clients()

        ctx = asyncio.run(_abuild_ctx())
        # Пометки ленты «изображение не просмотрено» (дизайн картинок §10) — с тредом
        # прогона: хук нужен уже собранному контексту, поэтому ставится здесь.
        ctx.on_trace_note = self._trace_note_sink.hook_for(ctx)
        ctx.on_model_choice_applied = self._trace_note_sink.hook_for_choice(
            ctx
        )
        self._session_log_path = ctx.logging_path
        # Live nested progress субагентов: очередь на app, копия на ctx → child.
        from gc_core.execution.progress import QueueSubagentProgressSink

        self._subagent_progress_sink = QueueSubagentProgressSink()
        ctx.subagent_progress_sink = self._subagent_progress_sink
        # Снапшот MCP «сервер→статус/тулы» app-уровнево — чтобы /mcp показывал
        # результат boot-дискавери сразу, не дожидаясь первого сообщения.
        from gc_core.tools.mcp_snapshot import build_mcp_tools_snapshot

        self._mcp_snapshot = build_mcp_tools_snapshot(
            loaded, ctx.tools_data, ctx.mcp_status
        )
        self._mcp_status = ctx.mcp_status
        self._registered_tool_names = getattr(
            ctx, "registered_tool_names", None
        )
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
        decision_key: str | None = None,
        call_id: str | None = None,
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
                call_with_optional(
                    fn,
                    title,
                    question,
                    fields,
                    via=via,
                    decision_key=decision_key,
                    call_id=call_id,
                )
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

    def _builtin_tool_names(self, settings: LoadedSettings) -> frozenset[str]:
        """Имена встроенных тулов.

        Пересборка только при новом объекте настроек.
        """
        cached = self._builtin_names_cache
        if cached is not None and cached[0] is settings:
            return cached[1]
        names = frozenset(all_builtin_tool_names(settings))
        self._builtin_names_cache = (settings, names)
        return names

    def _startup_notices(self) -> List[Notice]:
        """Стартовые уведомления — единая сборка для полного списка и для бейджей."""
        from gc_core.settings import is_explicit

        loaded = self.base_settings()
        return collect_startup_notices(
            loaded,
            self._builtin_tool_names(loaded),
            self._mcp_status,
            is_tui=True,
            known_tool_names=getattr(self, "_registered_tool_names", None),
            extra=voice_notices(loaded),
            deprecated_keys_used=(
                ["backend.stream"]
                if is_explicit(loaded.settings, "backend.stream")
                else []
            ),
            upgrade_info=self._upgrade_info,
            compat_notices=list(loaded.runtime.notices),
        )

    def _update_notice(self) -> List[Notice]:
        """«Доступна новая версия» как Notice — один владелец текста.

        Без ведущего ↑: NoticesScreen._notice_text сам подставляет глиф уровня (ℹ)."""
        if not self._update_available:
            return []
        return [
            Notice(
                "info",
                "update_available",
                "доступна новая версия · /settings → Обновление",
            )
        ]

    def collect_notices(self) -> List[Notice]:
        """Стартовые + runtime-уведомления (эфемерные, не сбрасываются при просмотре)."""
        return (
            self._update_notice()
            + self._startup_notices()
            + collect_runtime_notices()
        )

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

    # --- режим разрешений -------------------------------------------------

    def _permission_mode_and_source(self):
        """Действующий режим и его источник: выбор сессии → конфиг → встроенный.

        Правило — ядра (``mode_with_source``), с выбором этого окна: оно живёт
        и до первого прогона, когда контекста рана со стором ещё нет.
        """
        from gc_core.security.permission_mode import mode_with_source

        return mode_with_source(
            self._permission_mode, self.effective_settings().settings
        )

    def effective_permission_mode(self) -> str:
        return self._permission_mode_and_source()[0].value

    def permission_mode_source(self) -> str:
        """Откуда взят действующий режим: session | config | default."""
        return self._permission_mode_and_source()[1]

    def set_permission_mode(self, mode: str) -> None:
        """Сменить режим сессии и донести до движка.

        Писатели ``_permission_mode`` — этот метод и
        ``clear_permission_mode``: пилюля, ``/config`` и движок расходятся
        ровно тогда, когда кто-то пишет поле мимо них.
        Раннера может ещё не быть — выбор донесёт ``get_runner`` при создании.
        """
        self._permission_mode = mode
        runner = self.active_runner
        if runner is not None and hasattr(runner, "set_permission_mode"):
            runner.set_permission_mode(mode)

    def lock_permission_mode(self, mode: str) -> None:
        """Режим из флага запуска: сохранённый в сессии его не перебивает."""
        self.set_permission_mode(mode)
        self._permission_mode_locked = True

    def clear_permission_mode(self) -> None:
        """Снять выбор окна: действует ``default_mode`` конфига.

        Сброс, а не запись ``default_mode`` в сессию: иначе смена конфига
        потом не действовала бы на эту сессию.
        """
        self._permission_mode = None
        runner = self.active_runner
        if runner is not None and hasattr(runner, "clear_permission_mode"):
            runner.clear_permission_mode()

    def adopt_thread_permission_mode(
        self, meta: dict | None, *, resumed: bool = False
    ) -> None:
        """Принять режим, сохранённый в открываемой сессии.

        ``resumed`` — сессию открыли (пикер), а не создали в этом окне: без
        сохранённого режима у неё действует ``default_mode`` конфига, а не
        режим окна. Новый тред окна (первое сообщение, ``/new``) режим окна
        наследует. Флаг запуска сильнее и того и другого.
        """
        if self._permission_mode_locked:
            return
        from gc_core.security.permission_mode import coerce_mode

        # Режим из другой сборки или убранный из списка — как его нет:
        # движок отвергает неизвестный режим исключением.
        mode = coerce_mode(
            ((meta or {}).get("shared") or {}).get("permission_mode")
        )
        if mode is not None:
            self.set_permission_mode(mode.value)
        elif resumed:
            self.clear_permission_mode()

    def action_cycle_permission_mode(self) -> None:
        """Shift+Tab: следующий режим в цикле (гейт — check_action)."""
        from gc_core.security.permission_mode import coerce_mode, next_mode

        current = coerce_mode(self.effective_permission_mode())
        if current is None:
            return
        new = next_mode(current)
        self.set_permission_mode(new.value)
        screen = self._main_screen()
        if screen is not None and hasattr(
            screen, "on_permission_mode_changed"
        ):
            screen.on_permission_mode_changed(new.value)

    def _main_screen(self):
        """Базовый (главный) экран — низ стека; None если это не TraceScreen."""
        base = self.screen_stack[0] if self.screen_stack else None
        return base if isinstance(base, TraceScreen) else None

    def _can_open_tool(self) -> bool:
        """config/diff открываются с главного экрана И с экрана уведомлений. Сами
        config/diff/справка — конечные: с них только F1-справка и Esc назад."""
        from gc_tui.screens.notices import NoticesScreen

        return isinstance(self.screen, (TraceScreen, NoticesScreen))

    def check_action(
        self, action: str, parameters: tuple[object, ...]
    ) -> bool:
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
            return action in (
                "back",
                "interrupt",
                "focus_next",
                "focus_previous",
            )
        if action == "diff":
            from gc_tui.screens.sessions import SessionsScreen

            # На /sessions Ctrl+D — массовое удаление; action_diff это учитывает.
            return self._can_open_tool() or isinstance(
                self.screen, SessionsScreen
            )
        if action == "cycle_permission_mode":
            # Пилюля режима видна только на главном экране: на прочих хоткей
            # выключен (иначе режим менялся бы молча, без индикатора), и
            # Shift+Tab остаётся их собственной навигацией по формам.
            screen = self._main_screen()
            if screen is None or self.screen is not screen:
                return False
            # На вопросе подтверждения — тоже выключен: смысл вопроса зависит от
            # режима, сменить режим посреди него — ответить на вопрос, которого
            # уже нет. Вопрос агента (ask_human) режима не касается, там хоткей
            # работает намеренно: человек ужесточает режим ПЕРЕД ответом.
            if getattr(screen, "_confirm_active", False):
                return False
            # Баннер одобрения плана — тот же случай: его варианты читаются в
            # терминах режима.
            return getattr(screen, "_plan_banner", None) is None
        return True

    def action_help(self) -> None:
        from gc_tui.screens.help import HelpScreen

        # Справка доступна с любого экрана, КРОМЕ самой справки (без help-на-help).
        if isinstance(self.screen, HelpScreen):
            return
        main = self._main_screen()
        if main is not None:
            main.spend_help_hint()
        self.push_screen(HelpScreen())

    def action_sessions(self) -> None:
        if not self._on_main():
            return
        if not self._threads_dir:
            self.notify("threads_dir не задан", severity="warning")
            return
        from gc_tui.screens.sessions import SessionsScreen

        self.push_screen(
            SessionsScreen(
                threads_dir=self._threads_dir,
                system=self._system,
            )
        )

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
        from gc_tui.screens.copy_buffer import CopyBufferScreen

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
        from gc_tui.screens.notices import NoticesScreen

        if isinstance(self.screen, NoticesScreen):
            return
        self.push_screen(NoticesScreen())

    def action_diff(self) -> None:
        from gc_tui.screens.diff import DiffScreen
        from gc_tui.screens.sessions import SessionsScreen

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
            from gc_tui.services.copy_buffer import push_copy_buffer

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
            self._push_session_copy_buffer(
                payload
            )  # B2: тем же payload сверху
            return
        scr = self.screen
        runner = getattr(self, "active_runner", None)
        if runner is not None and runner.is_running():
            if hasattr(scr, "_cmd_stop"):
                scr._cmd_stop()
        elif hasattr(scr, "_request_exit"):
            scr._request_exit()
