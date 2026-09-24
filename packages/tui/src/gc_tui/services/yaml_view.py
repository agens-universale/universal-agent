"""Общий YAML-виджет с подсветкой (Rich Syntax) — для /config и /mcp."""

import yaml
from rich.syntax import Syntax
from textual.widgets import Static

from gc_tui.palette import BAND_BG


class _SafeReprDumper(yaml.SafeDumper):
    """SafeDumper, не падающий на произвольных объектах — рендерит их через str()."""


# Catch-all по MRO: точные типы (dict/list/str/int/bool/None) идут штатными
# репрезентерами SafeDumper, всё прочее (pydantic-модели, Path, enum, …) —
# строкой. Так /config не падает на реальном app._config (RepresenterError).
_SafeReprDumper.add_multi_representer(
    object, lambda dumper, data: dumper.represent_str(str(data))
)


def safe_yaml_dump(data) -> str:
    """yaml-дамп, толерантный к не-сериализуемым значениям конфига."""
    return yaml.dump(
        data,
        Dumper=_SafeReprDumper,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


# Тема подсветки YAML — один источник, меняется в одну строку при ревью.
# Кандидаты: solarized-dark | nord | gruvbox-dark | native | github-dark |
#            one-dark | monokai | dracula | inkpot | fruity
_YAML_SYNTAX_THEME = "solarized-dark"


def yaml_syntax_renderable(text: str) -> Syntax:
    """Текст YAML → Rich Syntax с подсветкой на фоне BAND_BG (для in-place update)."""
    return Syntax(
        text or "",
        lexer="yaml",
        theme=_YAML_SYNTAX_THEME,
        background_color=BAND_BG,
        word_wrap=False,
    )


def yaml_syntax_widget(text: str) -> Static:
    """Текст YAML → Static(Syntax) с подсветкой на фоне BAND_BG."""
    return Static(yaml_syntax_renderable(text))
