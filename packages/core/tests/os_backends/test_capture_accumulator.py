"""Bounded head+tail accumulator: настоящие края, точные счётчики, O(L) памяти."""

from gc_core.backends.capture import (
    BoundedStreamAccumulator,
    CapturedStream,
    captured_stream_from_text,
)


def _feed(text: str, cap: int, chunk: int = 7) -> CapturedStream:
    acc = BoundedStreamAccumulator(cap)
    for i in range(0, len(text), chunk):
        acc.feed(text[i : i + chunk])
    return acc.result()


def test_short_stream_kept_whole():
    cs = _feed("abcdef", 100)
    assert cs.truncated is False
    assert cs.content == "abcdef"
    assert cs.total_chars == 6
    assert cs.omitted_chars == 0


def test_exactly_cap_not_truncated():
    text = "x" * 40
    cs = _feed(text, 40)
    assert cs.truncated is False
    assert cs.content == text
    assert cs.omitted_chars == 0


def test_head_and_tail_are_real_edges():
    text = "".join(str(i % 10) for i in range(1000))
    cs = _feed(text, 40)
    assert cs.truncated is True
    assert cs.head == text[:20]
    assert cs.tail == text[-20:]
    assert cs.total_chars == 1000
    assert cs.omitted_chars == 960
    assert len(cs.head) + len(cs.tail) + cs.omitted_chars == cs.total_chars


def test_odd_cap_splits_floor_head_rest_tail():
    text = "y" * 100
    cs = _feed(text, 41)
    assert len(cs.head) == 20
    assert len(cs.tail) == 21


def test_single_chunk_larger_than_tail_keeps_real_end():
    acc = BoundedStreamAccumulator(10)
    acc.feed("a" * 5)
    acc.feed("b" * 1000 + "END")
    cs = acc.result()
    assert cs.head == "a" * 5
    assert cs.tail.endswith("END")
    assert len(cs.tail) == 5
    assert cs.total_chars == 1008


def test_zero_cap_counts_but_stores_nothing():
    cs = _feed("z" * 500, 0)
    assert cs.head == ""
    assert cs.tail == ""
    assert cs.total_chars == 500
    assert cs.truncated is True


def test_memory_is_bounded_on_huge_stream():
    acc = BoundedStreamAccumulator(1000)
    for _ in range(5000):
        acc.feed("q" * 1000)
    cs = acc.result()
    assert len(cs.head) + len(cs.tail) == 1000
    assert cs.total_chars == 5_000_000


def test_from_text_matches_incremental():
    text = "".join(str(i % 7) for i in range(3000))
    assert captured_stream_from_text(text, 64) == _feed(text, 64)
