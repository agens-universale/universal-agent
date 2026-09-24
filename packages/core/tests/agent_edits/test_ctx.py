"""Чтение лимитов секции `diff_tui` с ctx: мусор из конфига до записи не доезжает.

Схема (`DiffTuiConfig`) объявляет эти ключи простым `int` и пропускает и
отрицательное, и — через слой оверрайдов — нечисловое. Читатели на стороне TUI
клампят такое сами; здесь страж того, что запись правок делает то же самое.
"""
from __future__ import annotations

from types import SimpleNamespace

from gc_core.agent_edits.ctx import limit
from gc_core.config_modules.diff_tui import (
    DEFAULT_HUNK_CONTEXT_LINES,
    DEFAULT_MAX_DIFF_CHARS,
    DEFAULT_MAX_DIFF_SOURCE_BYTES,
    DEFAULT_SNAPSHOTS_MAX_FILES,
)


def _ctx(**diff_tui):
    return SimpleNamespace(config_dict={"diff_tui": dict(diff_tui)})


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
