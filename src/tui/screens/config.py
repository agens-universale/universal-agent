"""ConfigScreen — снимок конфига сессии (read-only) + пути к файлам."""
from __future__ import annotations
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Static
from rich.text import Text

from tui.services.redact import redact_secrets
from tui.services.yaml_view import safe_yaml_dump, yaml_syntax_widget
from tui.palette import DIM, MUTED

_FOOTER = "[Esc] back  [↑↓ / PgUp PgDn] прокрутка"
_SNAPSHOT_HINT = "снимок на момент запуска · правки применятся после перезапуска"


def _paths_block(config: dict, config_path: str | None,
                 project_local: str | None) -> Text:
    """Строки путей: метка (dim) + значение (ink); '(не задано)' — muted.
    Заголовок блока живёт в border_title панели, не в тексте."""
    from project_memory import resolve_project_memory_path

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

    def _yaml_text(self) -> str:
        return safe_yaml_dump(redact_secrets(self._config))

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
                yaml_w = yaml_syntax_widget(self._yaml_text())
                yaml_w.add_class("config-panel")
                yaml_w.border_title = "config.yml"
                yaml_w.border_subtitle = "секреты скрыты"
                yield yaml_w
            yield Static(_FOOTER, id="footer", markup=False)
