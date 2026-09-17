"""Тесты LRU-ограничения кэша fetch_url (SharedStateData.fetch_url_cache).

Покрывает: нормализацию dict→OrderedDict, лимит 10, эвикцию самого старого
(popitem(last=False)), продление жизни при cache-hit (move_to_end), round-trip
через serialize_shared_block (Order сохраняется в JSON).
"""
from __future__ import annotations

import json
from collections import OrderedDict

from gc_core.states import SharedStateData
from gc_core.persistence.thread_meta_mapper import ThreadMetaMapper
from gc_core.tools.exec.fetch_url import FetchUrl
from gc_core.tools.exec.simple_fetch_url import SimpleFetchUrl


# ── Стаб shared/state для _get_cache ──────────────────────────────────────────

class _Shared:
    def __init__(self) -> None:
        self.fetch_url_cache: dict = {}


class _State:
    def __init__(self) -> None:
        self.shared = _Shared()


def _make_fetch() -> FetchUrl:
    return FetchUrl(state=_State())


def _make_simple_fetch() -> SimpleFetchUrl:
    return SimpleFetchUrl(state=_State())


# ── Нормализация dict→OrderedDict ─────────────────────────────────────────────

def test_get_cache_normalizes_plain_dict_to_ordereddict():
    f = _make_fetch()
    f._state.shared.fetch_url_cache = {"https://a": {"content": "A"}}
    cache = f._get_cache()
    assert isinstance(cache, OrderedDict)
    assert cache["https://a"] == {"content": "A"}
    # Подменный атрибут state.shared обновлён на месте.
    assert isinstance(f._state.shared.fetch_url_cache, OrderedDict)


def test_get_cache_creates_empty_ordereddict_when_missing():
    f = _make_fetch()
    f._state.shared.fetch_url_cache = None  # type: ignore[assignment]
    cache = f._get_cache()
    assert isinstance(cache, OrderedDict)
    assert len(cache) == 0


def test_get_cache_returns_none_when_no_shared():
    f = FetchUrl(state=None)
    assert f._get_cache() is None


# ── Эвикция: лимит 10, popitem(last=False) ────────────────────────────────────

def _fill_cache(f: FetchUrl, n: int) -> None:
    """Записывает n URL через публичный метод записи кэша."""
    for i in range(n):
        url = f"https://example.com/{i}"
        f._put_to_cache(url, f"page-{i}", {"title": None, "description": None, "lang": None})


def test_cache_evicts_oldest_when_limit_exceeded():
    f = _make_fetch()
    _fill_cache(f, 15)
    cache = f._get_cache()
    assert len(cache) == 10
    # Вытеснены самые старые (0..4), остались 5..14.
    keys = list(cache.keys())
    assert keys[0] == "https://example.com/5"
    assert keys[-1] == "https://example.com/14"


def test_cache_keeps_exactly_limit_at_boundary():
    f = _make_fetch()
    _fill_cache(f, 10)
    assert len(f._get_cache()) == 10


# ── LRU: move_to_end на cache-hit ────────────────────────────────────────────

def test_cache_hit_moves_to_end():
    f = _make_fetch()
    _fill_cache(f, 10)
    # Читаем URL 5 через _get_from_cache → он становится последним,
    # остальные сохраняют относительный порядок.
    result = f._get_from_cache("https://example.com/5")
    assert result is not None
    cache = f._get_cache()
    assert list(cache.keys())[-1] == "https://example.com/5"
    assert list(cache.keys())[0] == "https://example.com/0"


def test_repeatedly_read_url_survives_eviction():
    """URL, который читают, не вытесняется новыми записями (LRU-семантика)."""
    f = _make_fetch()
    _fill_cache(f, 10)
    # Читаем URL 5 → порядок: [0,1,2,3,4,6,7,8,9,5].
    result = f._get_from_cache("https://example.com/5")
    assert result is not None
    # Добавляем 5 новых (15..19) → эвикция 5 first (0..4); остаются
    # [6,7,8,9,5,15,16,17,18,19]. URL 5 пережил, 0..4 — вытеснены.
    for i in range(15, 20):
        f._put_to_cache(
            f"https://example.com/{i}",
            f"page-{i}",
            {"title": None, "description": None, "lang": None},
        )
    cache = f._get_cache()
    assert "https://example.com/5" in cache  # пережил эвикцию
    assert "https://example.com/0" not in cache  # вытеснен
    assert len(cache) == 10


# ── Round-trip через meta: OrderedDict → JSON → SharedStateData ───────────────

def test_cache_order_survives_serialize_roundtrip():
    """Порядок ключей (LRU-возраст) сохраняется при персистентности в meta."""
    shared = SharedStateData()
    for i in range(10):
        shared.fetch_url_cache[f"https://example.com/{i}"] = {"content": f"p{i}"}
    # merge_shared_into_meta ставит state_schema_version, иначе shared_from_meta упадёт.
    meta = ThreadMetaMapper.merge_shared_into_meta({}, shared)
    raw_json = json.dumps(meta, ensure_ascii=False)
    restored_meta = json.loads(raw_json)

    restored_shared = ThreadMetaMapper.shared_from_meta(restored_meta)
    assert restored_shared is not None
    # Содержимое и порядок сохранены.
    assert list(restored_shared.fetch_url_cache.keys()) == [
        f"https://example.com/{i}" for i in range(10)
    ]


def test_cache_ordereddict_equals_plain_dict_for_compatibility():
    """Существующие тесты сравнивают fetch_url_cache через == с dict."""
    shared = SharedStateData()
    shared.fetch_url_cache["https://x"] = {"content": "y"}
    # После нормализации через _get_cache — тоже OrderedDict.
    f = _make_fetch()
    f._state.shared = shared
    cache = f._get_cache()
    assert cache == {"https://x": {"content": "y"}}


def test_max_size_constant_is_ten():
    assert FetchUrl.fetch_url_cache_max_size == 10



# ── Прямые тесты методов _get_from_cache / _put_to_cache ──────────────────────

def test_get_from_cache_returns_content_and_meta():
    f = _make_fetch()
    f._put_to_cache("https://x.com", "body text", {"title": "T", "description": None, "lang": "en"})
    result = f._get_from_cache("https://x.com")
    assert result is not None
    content, meta = result
    assert content == "body text"
    assert meta == {"title": "T", "description": None, "lang": "en"}


def test_get_from_cache_returns_none_for_missing_url():
    f = _make_fetch()
    f._put_to_cache("https://x.com", "body", {"title": None, "description": None, "lang": None})
    assert f._get_from_cache("https://y.com") is None


def test_get_from_cache_returns_none_when_no_state():
    f = FetchUrl(state=None)
    assert f._get_from_cache("https://x.com") is None


def test_put_to_cache_noop_when_no_state():
    f = FetchUrl(state=None)
    # Не должен падать — просто no-op.
    f._put_to_cache("https://x.com", "body", {"title": None, "description": None, "lang": None})


def test_put_to_cache_evicts_on_overflow():
    f = _make_fetch()
    for i in range(15):
        f._put_to_cache(
            f"https://example.com/{i}",
            f"page-{i}",
            {"title": None, "description": None, "lang": None},
        )
    cache = f._get_cache()
    assert len(cache) == 10
    assert "https://example.com/0" not in cache
    assert "https://example.com/14" in cache


def test_get_from_cache_updates_lru_order():
    f = _make_fetch()
    _fill_cache(f, 10)
    # Читаем URL 3 — он становится последним.
    f._get_from_cache("https://example.com/3")
    cache = f._get_cache()
    assert list(cache.keys())[-1] == "https://example.com/3"


# ── SimpleFetchUrl: тот же LRU API (общий shared.fetch_url_cache) ─────────────

def test_simple_fetch_max_size_matches_fetch_url():
    assert SimpleFetchUrl.fetch_url_cache_max_size == FetchUrl.fetch_url_cache_max_size == 10


def test_simple_fetch_get_cache_normalizes_plain_dict_to_ordereddict():
    f = _make_simple_fetch()
    f._state.shared.fetch_url_cache = {"https://a": {"content": "A"}}
    cache = f._get_cache()
    assert isinstance(cache, OrderedDict)
    assert isinstance(f._state.shared.fetch_url_cache, OrderedDict)


def test_simple_fetch_cache_evicts_oldest_when_limit_exceeded():
    f = _make_simple_fetch()
    for i in range(15):
        f._put_to_cache(
            f"https://example.com/{i}",
            f"page-{i}",
            {"title": None, "description": None, "lang": None},
        )
    cache = f._get_cache()
    assert len(cache) == 10
    assert list(cache.keys())[0] == "https://example.com/5"
    assert list(cache.keys())[-1] == "https://example.com/14"


def test_simple_fetch_cache_hit_moves_to_end():
    f = _make_simple_fetch()
    for i in range(10):
        f._put_to_cache(
            f"https://example.com/{i}",
            f"page-{i}",
            {"title": None, "description": None, "lang": None},
        )
    result = f._get_from_cache("https://example.com/5")
    assert result is not None
    assert list(f._get_cache().keys())[-1] == "https://example.com/5"


def test_simple_and_full_fetch_share_lru_cache():
    """Оба тула пишут в один shared.fetch_url_cache с общей эвикцией."""
    shared = _Shared()
    state = _State()
    state.shared = shared
    full = FetchUrl(state=state)
    simple = SimpleFetchUrl(state=state)
    for i in range(8):
        full._put_to_cache(
            f"https://example.com/{i}",
            f"page-{i}",
            {"title": None, "description": None, "lang": None},
        )
    for i in range(8, 12):
        simple._put_to_cache(
            f"https://example.com/{i}",
            f"page-{i}",
            {"title": None, "description": None, "lang": None},
        )
    cache = simple._get_cache()
    assert len(cache) == 10
    assert "https://example.com/0" not in cache
    assert "https://example.com/1" not in cache
    assert "https://example.com/11" in cache
    # Cache-hit через SimpleFetchUrl обновляет LRU для общей записи.
    hit = simple._get_from_cache("https://example.com/2")
    assert hit is not None
    assert list(cache.keys())[-1] == "https://example.com/2"
