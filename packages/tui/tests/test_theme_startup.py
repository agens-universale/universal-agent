"""Тема при старте: выбор палитры от фона терминала и порядок main().

Токены палитры модули берут через `from gc_tui.palette import X` — значение
фиксируется при импорте. Поэтому `apply_background` обязан случиться раньше,
чем загрузится любой такой модуль; порядок проверяется в отдельном процессе,
где модули gc_tui ещё не загружены другими тестами.
"""

import json
import os
import subprocess
import sys
import textwrap

import pytest

from gc_tui.palette import select_palette

DARK = (0x21, 0x27, 0x33)

# Что main() вправе загрузить из gc_tui до выбора палитры: ни один из этих
# модулей не берёт токены палитры по имени. Новый модуль в списке —
# только после проверки, что он палитру по имени не импортирует.
ALLOWED_BEFORE_PALETTE = {
    "gc_tui",
    "gc_tui.palette",
    "gc_tui.runtime_config",
    "gc_tui.services",
    "gc_tui.services.terminal_caps",
}

_PROBE = textwrap.dedent(
    """
    import json, os, sys
    seen = {}

    class Spy:
        def find_spec(self, name, path=None, target=None):
            if name == "textual" and "textual_env" not in seen:
                seen["textual_env"] = os.environ.get("TEXTUAL_COLOR_SYSTEM")
            if name == "gc_tui.app":
                seen["applied_before_app"] = "applied" in seen
                print(json.dumps(seen))
                sys.stdout.flush()
                os._exit(0)
            return None

    sys.meta_path.insert(0, Spy())
    from gc_tui import main, palette
    from gc_tui.services import terminal_caps

    terminal_caps.query_background = lambda **kw: (0x21, 0x27, 0x33)

    def apply(rgb):
        seen["applied"] = list(rgb)
        seen["loaded"] = sorted(m for m in sys.modules if m == "gc_tui"
                                or m.startswith("gc_tui."))

    palette.apply_background = apply
    main(["--config", sys.argv[1]])
    """
)


def _run_main_until_app_import(tmp_path) -> dict:
    threads = tmp_path / "threads"
    threads.mkdir()
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        "backend:\n"
        "  api_mode: chat_completions\n"
        "  completions_url: http://127.0.0.1:9\n"
        "paths:\n"
        f'  threads_data_path: "{threads.as_posix()}"\n',
        encoding="utf-8",
    )
    env = {**os.environ, "HOME": str(tmp_path), "WT_SESSION": "x"}
    env.pop("TEXTUAL_COLOR_SYSTEM", None)
    done = subprocess.run(
        [sys.executable, "-c", _PROBE, str(cfg)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_gigacode_does_not_probe():
    def probe():
        raise AssertionError("в gigacode терминал не опрашивается")

    assert select_palette("gigacode", probe) is None


def test_auto_without_answer_is_gigacode():
    assert select_palette("auto", lambda: None) is None


def _rgb(hex_color):
    return tuple(int(hex_color[i : i + 2], 16) for i in (1, 3, 5))


@pytest.mark.parametrize(
    "bg", ["#224fbc", "#13773d", "#929292", "#ffffff", "#b0b0b0"]
)
def test_auto_background_unreadable_for_text_is_gigacode(bg):
    assert select_palette("auto", lambda: _rgb(bg)) is None


@pytest.mark.parametrize(
    "bg",
    [
        "#2e3440",
        "#3b4252",
        "#7a251e",
        "#212733",
        "#16191e",
        "#000000",
        "#282c34",
        "#002b36",
        "#0E1119",
        "#1e1e1e",
    ],
)
def test_auto_background_readable_for_text_is_applied(bg):
    assert select_palette("auto", lambda: _rgb(bg)) == _rgb(bg)


def test_auto_dark_background_is_applied():
    assert select_palette("auto", lambda: DARK) == DARK


def test_main_applies_palette_before_app_import(tmp_path):
    seen = _run_main_until_app_import(tmp_path)

    assert seen["applied"] == list(DARK)
    assert seen["applied_before_app"] is True
    assert set(seen["loaded"]) <= ALLOWED_BEFORE_PALETTE


def test_truecolor_is_set_before_first_textual_lookup(tmp_path):
    seen = _run_main_until_app_import(tmp_path)

    assert seen["textual_env"] == "truecolor"
