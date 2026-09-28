"""Чтение лимитов секции `agent_edits` с ctx: мусор из конфига до записи не доезжает.

Схема (`AgentEditsConfig`) объявляет эти ключи простым `int` и пропускает и
отрицательное, и — через слой оверрайдов — нечисловое. Читатели на стороне TUI
клампят такое сами; здесь страж того, что запись правок делает то же самое.
"""
from __future__ import annotations

from types import SimpleNamespace

from gc_core.agent_edits.ctx import limit
from gc_core.config_modules.agent_edits import (
    DEFAULT_HUNK_CONTEXT_LINES,
    DEFAULT_MAX_DIFF_CHARS,
    DEFAULT_MAX_DIFF_SOURCE_BYTES,
    DEFAULT_SNAPSHOTS_MAX_FILES,
)


def _ctx(**agent_edits):
    return SimpleNamespace(config_dict={"agent_edits": dict(agent_edits)})


def test_explicit_value_wins_over_config():
    ctx = _ctx(max_diff_chars=10)
    assert limit(7, ctx, "max_diff_chars", DEFAULT_MAX_DIFF_CHARS) == 7


def test_non_positive_source_cap_falls_back_to_default():
    """Кап размера ниже единицы обесценивает каждую правку в `no_diff`: дифф не
    считается, снимок не снимается, откат мёртв весь сеанс."""
    for raw in (-1, 0):
        ctx = _ctx(max_diff_source_bytes=raw)
        assert (
            limit(None, ctx, "max_diff_source_bytes", DEFAULT_MAX_DIFF_SOURCE_BYTES)
            == DEFAULT_MAX_DIFF_SOURCE_BYTES
        )


def test_non_numeric_value_falls_back_instead_of_raising():
    """`int("много")` раньше летел из `_record_edit` — правка пропадала целиком:
    внешний `except Exception` глотает её вместе со счётчиками."""
    ctx = _ctx(max_diff_source_bytes="много", hunk_context_lines=None)
    assert (
        limit(None, ctx, "max_diff_source_bytes", DEFAULT_MAX_DIFF_SOURCE_BYTES)
        == DEFAULT_MAX_DIFF_SOURCE_BYTES
    )
    assert (
        limit(None, ctx, "hunk_context_lines", DEFAULT_HUNK_CONTEXT_LINES)
        == DEFAULT_HUNK_CONTEXT_LINES
    )


def test_bool_is_not_a_limit():
    """`true` в yaml — это не «один»: для питона `True == 1`, для конфига — мусор."""
    ctx = _ctx(snapshots_max_files=True)
    assert (
        limit(None, ctx, "snapshots_max_files", DEFAULT_SNAPSHOTS_MAX_FILES)
        == DEFAULT_SNAPSHOTS_MAX_FILES
    )


def test_zero_is_legal_for_hunk_context_and_diff_chars():
    """Ноль — не всегда мусор: у контекста хэнков это дифф без контекстных строк,
    у `max_diff_chars` — обещанное шаблонами конфига «без лимита»."""
    assert limit(None, _ctx(hunk_context_lines=0), "hunk_context_lines",
                 DEFAULT_HUNK_CONTEXT_LINES) == 0
    assert limit(None, _ctx(max_diff_chars=0), "max_diff_chars",
                 DEFAULT_MAX_DIFF_CHARS) == 0


def test_zero_snapshots_max_files_means_do_not_evict():
    """Ноль у `snapshots_max_files` — «не вытеснять», а не мусор.

    В `snapshots.evict_lru` первая же строка — `if max_files <= 0: return`,
    то есть ноль выключает вытеснение целиком, а не сносит все снимки.
    Подменять его дефолтом значит отнимать у пользователя законную настройку.
    """
    assert limit(None, _ctx(snapshots_max_files=0), "snapshots_max_files",
                 DEFAULT_SNAPSHOTS_MAX_FILES) == 0


def test_negative_stays_garbage_even_where_zero_is_legal():
    assert limit(None, _ctx(hunk_context_lines=-3), "hunk_context_lines",
                 DEFAULT_HUNK_CONTEXT_LINES) == DEFAULT_HUNK_CONTEXT_LINES
    assert limit(None, _ctx(max_diff_chars=-1), "max_diff_chars",
                 DEFAULT_MAX_DIFF_CHARS) == DEFAULT_MAX_DIFF_CHARS
    assert limit(None, _ctx(snapshots_max_files=-1), "snapshots_max_files",
                 DEFAULT_SNAPSHOTS_MAX_FILES) == DEFAULT_SNAPSHOTS_MAX_FILES


def test_sane_values_pass_through():
    ctx = _ctx(max_diff_source_bytes=512, snapshots_max_files=42)
    assert limit(None, ctx, "max_diff_source_bytes",
                 DEFAULT_MAX_DIFF_SOURCE_BYTES) == 512
    assert limit(None, ctx, "snapshots_max_files",
                 DEFAULT_SNAPSHOTS_MAX_FILES) == 42


# --- запись правок по режимам запуска ------------------------------------------

import pytest  # noqa: E402

from gc_core.agent_edits.ctx import recording_enabled  # noqa: E402


@pytest.mark.parametrize(
    "is_tui, run_mode, record, expected",
    [
        (True, None, {}, True),                          # TUI — пишет по умолчанию
        (True, None, {"tui": False}, False),             # …и выключается явно
        (False, "cli", {}, False),                       # CLI — по умолчанию нет
        (False, "cli", {"cli": True}, True),
        (False, "server", {}, False),                    # сервер — по умолчанию нет
        (False, "server", {"server": True}, True),
        (False, None, {"cli": True, "server": True}, False),  # библиотека: режима нет
        (False, "что-то", {"cli": True}, False),         # неизвестный режим — нет
    ],
)
def test_recording_is_decided_by_the_run_mode(is_tui, run_mode, record, expected):
    """Журнал правок пишется там, где его смотрят: в TUI — всегда (по умолчанию),
    в `gc-cli` и на сервере — только если включено; библиотечное использование
    ядра режима не объявляет и не пишет. TUI узнаётся по признаку контекста, CLI и
    сервер — по служебному `run_mode`, который ставит сам фронтенд."""
    config = {"agent_edits": {"record": record}}
    if run_mode is not None:
        config["run_mode"] = run_mode
    ctx = SimpleNamespace(is_tui=is_tui, config_dict=config)

    assert recording_enabled(ctx) is expected
