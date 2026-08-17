"""ConfigScreen — снимок конфига сессии (read-only) + пути к файлам."""
from __future__ import annotations
from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Input, Static
from rich.text import Text

from gc_tui.services.redact import redact_secrets
from gc_tui.services.yaml_view import safe_yaml_dump, yaml_syntax_renderable, yaml_syntax_widget
from gc_tui.palette import DIM, MUTED

_FOOTER = "[Esc] back  [↑↓ / PgUp PgDn] прокрутка"
_SNAPSHOT_HINT = "снимок на момент запуска · правки применятся после перезапуска"
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
            out[k] = v                       # ключ совпал → всё поддерево
        elif isinstance(v, dict):
            sub = _filter_config_by_key(v, q)
            if sub:
                out[k] = sub                 # вложенный совпал → путь
        elif isinstance(v, list):
            kept = [_filter_config_by_key(it, q) for it in v if isinstance(it, dict)]
            kept = [it for it in kept if it]
            if kept:
                out[k] = kept
    return out


def _paths_block(config: dict, config_path: str | None,
                 project_local: str | None) -> Text:
    """Строки путей: метка (dim) + значение (ink); '(не задано)' — muted.
    Заголовок блока живёт в border_title панели, не в тексте."""
    from gc_core.project_memory import resolve_project_memory_path

    def path_or_unset(key):  # резолвнутый путь или метка
        v = config.get(key)
        return str(v) if v else "(не задано)"

    rows = [("config.yml:", config_path or "(не определён)")]
    if project_local:
        rows.append(("  + project-local:", str(project_local)))
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
        absent = value.startswith("(")   # "(не задано)" / "(не определён)"
        t.append(value, style=MUTED if absent else None)
    return t


class ConfigScreen(Screen):
    def __init__(self, *, config: dict | None = None,
                 config_path: str | None = None,
                 project_local: str | None = None) -> None:
        super().__init__()
        self._config = config or {}
        self._config_path = config_path
        self._project_local = project_local
        self._filter = ""
        self._yaml_w: Static | None = None

    def _yaml_text(self) -> str:
        return safe_yaml_dump(redact_secrets(self._config))

    def _filtered_yaml_text(self) -> str:
        """YAML по текущему фильтру ключей; пустой фильтр → полный конфиг,
        нет совпадений → метка `(нет совпадений)`."""
        cfg = redact_secrets(self._config)
        q = self._filter.strip().lower()
        if not q:
            return safe_yaml_dump(cfg)
        filtered = _filter_config_by_key(cfg, q)
        return safe_yaml_dump(filtered) if filtered else _NO_MATCH

    def rendered_text(self) -> str:
        """Для тестов: подсказка + пути + YAML одной строкой."""
        return (_SNAPSHOT_HINT + "\n"
                + _paths_block(self._config, self._config_path,
                               self._project_local).plain
                + "\n" + self._yaml_text())

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            with Horizontal(id="header"):
                yield Static("config · снимок сессии", id="header-left", markup=False)
            with VerticalScroll(id="trace", classes="quiet-focus"):
                yield Static(_SNAPSHOT_HINT, classes="config-caption", markup=False)
                paths = Static(
                    _paths_block(self._config, self._config_path, self._project_local),
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
                self._yaml_w.update(yaml_syntax_renderable(self._filtered_yaml_text()))

    # Фильтр-Input в фокусе (как на скиллах) → клавиатурную прокрутку #trace
    # форвардим вручную. home/end оставляем Input'у (курсор в строке фильтра).
    _SCROLL = {"up": "scroll_up", "down": "scroll_down",
               "pageup": "scroll_page_up", "pagedown": "scroll_page_down"}

    def on_key(self, event: events.Key) -> None:
        action = self._SCROLL.get(event.key)
        if action is not None:
            getattr(self.query_one("#trace", VerticalScroll), action)()
            event.stop()
