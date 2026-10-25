"""ConfigScreen — снимок конфига сессии (read-only) + пути к файлам."""

from __future__ import annotations

from gc_core.security.permission_mode import mode_label
from gc_core.settings import runtime_of, settings_of
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Input, Static

from gc_tui.palette import DIM, MUTED
from gc_tui.services.redact import redact_settings
from gc_tui.services.yaml_view import (
    safe_yaml_dump,
    yaml_syntax_renderable,
    yaml_syntax_widget,
)

_FOOTER = "[Esc] back  [↑↓ / PgUp PgDn] прокрутка"
_SNAPSHOT_HINT = (
    "снимок на момент запуска · правки применятся после перезапуска"
)
_NO_MATCH = "(нет совпадений)"


def _filter_config_by_key(obj, q: str):
    """Рекурсивно оставить ключи, чьё имя содержит `q` (на любой глубине): при
    совпадении ключа — всё его поддерево; при совпадении вложенного ключа —
    путь к нему. `q` — уже в нижнем регистре. Не-dict значения без совпавшего
    ключа отбрасываются."""
    if not isinstance(obj, dict):
        return obj
    out = {}
    for k, v in obj.items():
        if q in str(k).lower():
            out[k] = v  # ключ совпал → всё поддерево
        elif isinstance(v, dict):
            sub = _filter_config_by_key(v, q)
            if sub:
                out[k] = sub  # вложенный совпал → путь
        elif isinstance(v, list):
            kept = [
                _filter_config_by_key(it, q)
                for it in v
                if isinstance(it, dict)
            ]
            kept = [it for it in kept if it]
            if kept:
                out[k] = kept
    return out


def _paths_block(
    settings,
    config_path: str | None,
    project_local: str | None,
    profile_path: str | None = None,
) -> Text:
    """Строки путей: метка (dim) + значение (ink); '(не задано)' — muted.
    Заголовок блока живёт в border_title панели, не в тексте. `settings` — всё,
    что принимает `settings_of`."""
    from gc_core.project_memory import resolve_project_memory_path

    paths = settings_of(settings).paths

    def path_or_unset(key):  # резолвнутый путь или метка
        v = getattr(paths, key, None)
        return str(v) if v else "(не задано)"

    rows = [("config.yml:", config_path or "(не определён)")]
    if project_local:
        rows.append(("  + project-local:", str(project_local)))
    if profile_path:
        rows.append(("  + профиль:", profile_path))
    rows += [
        ("agent.md:", path_or_unset("agent_md_path")),
        ("project_memory.md:", str(resolve_project_memory_path())),
        ("threads:", path_or_unset("threads_data_path")),
        ("logs:", path_or_unset("logs_dump_dir")),
    ]
    width = max(len(label) for label, _ in rows)
    t = Text()
    for i, (label, value) in enumerate(rows):
        if i:
            t.append("\n")
        t.append(label.ljust(width + 1), style=DIM)
        absent = value.startswith("(")  # "(не задано)" / "(не определён)"
        t.append(value, style=MUTED if absent else None)
    return t


def _effective_model_name(config) -> str:
    """Действующая модель снимка — резолв `ModelRouter` (§6/§7), а не плоский ключ:
    тот же канон, что у футера (`main.py::_footer_model_name`) и `/status`. С волны B
    мастер `agent.model` не пишет вовсе, а на легаси-конфигах ключ может быть просто
    устаревшим — обе беды показывали бы моделью не то, чем идёт работа.

    Уровень берём из того же снимка, что и строка «уровень:» рядом, — шапка обязана
    быть внутренне согласованной. Резолв не удался (реестр нерезолвим, алиаса нет) —
    честный фоллбэк на плоский ключ, шапка не место для отказа.

    Имя записи (алиас) главнее провайдерского id — язык слотов и привязок (§4).

    `level: auto` (спека auto-routing §5.1) переводится через `effective_target`
    ДО `ModelRouter` — сырой литерал `auto` иначе роняет `_resolve_level`
    (`ValueError`), и шапка показала бы фоллбэк вместо модели default-слота.
    Доступность фичи — через `gc_tui.toggles.auto_available_cfg` (единая точка
    TUI, спека §3), не пересчитывается своим композитом `auto_available` +
    `cli_forces_legacy_model`; core-аналог с той же логикой — только на
    core-функциях (`gc_core.onboarding.effective_model`, слой не позволяет
    ему знать про `gc_tui`)."""
    from gc_core.auto_routing.levels import effective_target
    from gc_core.config import cli_forces_legacy_model
    from gc_core.config_modules.model_registry import (
        configured_choice,
        effective_choice,
    )
    from gc_core.model_router import ModelRouter
    from gc_core.onboarding import MODEL_SENTINEL

    from gc_tui.toggles import auto_available_cfg

    settings = settings_of(config)
    name = settings.agent.model
    try:
        available = auto_available_cfg(config)
        resolved = ModelRouter(
            config,
            choice_provider=lambda: effective_target(
                effective_choice(configured_choice(settings), settings),
                None,
                available,
            ),
            force_legacy=cli_forces_legacy_model(config),
        ).resolve("main", check_breaker=False)
        name = resolved.alias or resolved.model
    except Exception:
        pass
    return name if name and name != MODEL_SENTINEL else "—"


def _profile_header_text(config, profile_source: str | None) -> Text:
    """Спека §12.3a: «профиль: work (проектный) · уровень: default · модель: glm-local».
    Выбор — действующий, тем же правилом, что у `/model`: `auto`, только
    когда автовыбор доступен, иначе то, чем идёт работа."""
    from gc_tui.toggles import shown_choice

    profile = runtime_of(config).active_profile or None
    level = shown_choice(config)
    model = _effective_model_name(config)
    t = Text()
    t.append("профиль: ", style=DIM)
    if profile:
        t.append(profile, style=MUTED)
        if profile_source:
            t.append(f" ({profile_source})", style=DIM)
    else:
        t.append("—", style=DIM)
    t.append("  ·  уровень: ", style=DIM)
    t.append(str(level), style=MUTED)
    t.append("  ·  модель: ", style=DIM)
    t.append(str(model), style=MUTED)
    return t


#: Откуда взят действующий режим разрешений — словами для человека.
_MODE_SOURCE_LABELS = {
    "session": "выбран в этой сессии",
    "config": "из конфига",
    "default": "по умолчанию",
}


def _permission_mode_text(mode: str, source: str | None) -> Text:
    """«режим разрешений: manual mode (выбран в этой сессии)» — в стиле шапки
    профиля. В отличие от снимка ниже, режим меняется на лету (Shift+Tab)."""
    t = Text()
    t.append("режим разрешений: ", style=DIM)
    t.append(mode_label(mode), style=MUTED)
    label = _MODE_SOURCE_LABELS.get(source or "")
    if label:
        t.append(f" ({label})", style=DIM)
    return t


class ConfigScreen(Screen):
    def __init__(
        self,
        *,
        settings=None,
        config_path: str | None = None,
        project_local: str | None = None,
        profile_source: str | None = None,
        profile_path: str | None = None,
        permission_mode: str | None = None,
        permission_mode_source: str | None = None,
    ) -> None:
        super().__init__()
        # Снимок базы окна (`LoadedSettings`) на момент открытия экрана.
        self._settings = settings
        self._config_path = config_path
        self._project_local = project_local
        self._profile_source = profile_source
        self._profile_path = profile_path
        self._permission_mode = permission_mode
        self._permission_mode_source = permission_mode_source
        self._filter = ""
        self._yaml_w: Static | None = None

    def _profile_header(self) -> Text:
        return _profile_header_text(self._settings, self._profile_source)

    def _redacted(self) -> dict:
        """Настройки документом (секции как в config.yml), секреты скрыты."""
        return redact_settings(settings_of(self._settings))

    def _yaml_text(self) -> str:
        return safe_yaml_dump(self._redacted())

    def _filtered_yaml_text(self) -> str:
        """YAML по текущему фильтру ключей; пустой фильтр → полный конфиг,
        нет совпадений → метка `(нет совпадений)`."""
        cfg = self._redacted()
        q = self._filter.strip().lower()
        if not q:
            return safe_yaml_dump(cfg)
        filtered = _filter_config_by_key(cfg, q)
        return safe_yaml_dump(filtered) if filtered else _NO_MATCH

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            with Horizontal(id="header"):
                yield Static(
                    "config · снимок сессии", id="header-left", markup=False
                )
            with VerticalScroll(id="trace", classes="quiet-focus"):
                yield Static(self._profile_header(), classes="config-caption")
                if self._permission_mode is not None:
                    yield Static(
                        _permission_mode_text(
                            self._permission_mode, self._permission_mode_source
                        ),
                        id="config-permission-mode",
                        classes="config-caption",
                    )
                yield Static(
                    _SNAPSHOT_HINT, classes="config-caption", markup=False
                )
                paths = Static(
                    _paths_block(
                        self._settings,
                        self._config_path,
                        self._project_local,
                        self._profile_path,
                    ),
                )
                paths.add_class("config-panel")
                paths.border_title = "пути"
                paths.border_subtitle = "правка внешним редактором"
                yield paths
                self._yaml_w = yaml_syntax_widget(self._yaml_text())
                self._yaml_w.add_class("config-panel")
                self._yaml_w.border_title = "config.yml"
                self._yaml_w.border_subtitle = "секреты скрыты"
                yield self._yaml_w
            with Horizontal(id="config-filter-row"):
                yield Static("> ", id="config-filter-prompt", markup=False)
                yield Input(placeholder="фильтр ключей…", id="config-filter")
            yield Static(_FOOTER, id="footer", markup=False)

    def on_mount(self) -> None:
        self.query_one("#config-filter", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "config-filter":
            self._filter = event.value
            if self._yaml_w is not None:
                self._yaml_w.update(
                    yaml_syntax_renderable(self._filtered_yaml_text())
                )

    # Фильтр-Input в фокусе (как на скиллах) → клавиатурную прокрутку #trace
    # форвардим вручную. home/end оставляем Input'у (курсор в строке фильтра).
    _SCROLL = {
        "up": "scroll_up",
        "down": "scroll_down",
        "pageup": "scroll_page_up",
        "pagedown": "scroll_page_down",
    }

    def on_key(self, event: events.Key) -> None:
        action = self._SCROLL.get(event.key)
        if action is not None:
            getattr(self.query_one("#trace", VerticalScroll), action)()
            event.stop()
