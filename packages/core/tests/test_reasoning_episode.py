"""Эпизод мысли главного вызова: текст из готового ответа, время фазы
размышления и метка попытки — из потока."""

from gc_core.execution.reasoning_episode import ReasoningEpisode, episode_text
from gc_core.message_format.serialization import _clip_reasoning
from gc_core.persistence.reasoning_record import ReasoningRecord


def _clock(*moments):
    it = iter(moments)
    return lambda: next(it)


def test_episode_measures_think_phase_and_tags_attempt():
    ep = ReasoningEpisode(mono=_clock(10.0, 13.5), new_token=lambda: "att_1")
    assert ep.observe("reset", "") == "att_1"
    assert ep.observe("think", "a") == "a"
    assert ep.observe("think", "b") == "b"
    assert ep.observe("tool", "{") == "{"
    assert ep.observe("answer", "x") == "x"
    ep.capture({"reasoning_content": "план"})
    assert ep.take() == ReasoningRecord(
        text="план", duration_sec=3.5, attempt="att_1"
    )


def test_answer_ends_the_phase():
    ep = ReasoningEpisode(mono=_clock(1.0, 3.0), new_token=lambda: "t")
    ep.observe("reset", "")
    ep.observe("think", "a")
    ep.observe("answer", "x")
    ep.capture({"reasoning_content": "r"})
    assert ep.take() == ReasoningRecord(
        text="r", duration_sec=2.0, attempt="t"
    )


def test_reset_starts_new_attempt():
    tokens = iter(["att_1", "att_2"])
    ep = ReasoningEpisode(
        mono=_clock(1.0, 2.0, 10.0, 14.0), new_token=lambda: next(tokens)
    )
    assert ep.observe("reset", "") == "att_1"
    ep.observe("think", "a")
    ep.observe("tool", "{")
    assert ep.observe("reset", "") == "att_2"
    ep.observe("think", "b")
    ep.observe("answer", "x")
    ep.capture({"reasoning_content": "второй"})
    assert ep.take() == ReasoningRecord(
        text="второй", duration_sec=4.0, attempt="att_2"
    )


def test_open_phase_closes_on_capture():
    ep = ReasoningEpisode(mono=_clock(5.0, 6.5), new_token=lambda: "t")
    ep.observe("reset", "")
    ep.observe("think", "a")
    ep.capture({"reasoning_content": "только мысль"})
    assert ep.take() == ReasoningRecord(
        text="только мысль", duration_sec=1.5, attempt="t"
    )


def test_attempt_without_think_has_no_duration():
    ep = ReasoningEpisode(mono=_clock(), new_token=lambda: "t")
    ep.observe("reset", "")
    ep.observe("answer", "x")
    ep.capture({"content": "<think>шаг</think>x"})
    assert ep.take() == ReasoningRecord(
        text="шаг", duration_sec=None, attempt="t"
    )


def test_no_stream_gives_no_duration():
    ep = ReasoningEpisode(mono=lambda: 0.0, new_token=lambda: "t")
    ep.capture({"reasoning_content": "x"})
    assert ep.take() == ReasoningRecord(
        text="x", duration_sec=None, attempt=None
    )


def test_no_text_gives_no_record():
    ep = ReasoningEpisode(mono=lambda: 0.0, new_token=lambda: "t")
    ep.observe("reset", "")
    ep.capture({"content": "ответ"})
    assert ep.take() is None


def test_later_capture_replaces_earlier():
    ep = ReasoningEpisode(mono=lambda: 0.0, new_token=lambda: "t")
    ep.capture({"reasoning_content": "слабая"})
    ep.capture({"content": "без мысли"})
    assert ep.take() is None


def test_episode_text_variants():
    assert episode_text({"reasoning_content": "  "}) is None
    assert episode_text({"content": "<think>шаг</think>ответ"}) == "шаг"
    both = {"reasoning": "r", "content": "<think>i</think>x"}
    assert episode_text(both) == "r"
    assert episode_text({"reasoning_content": "", "reasoning": "r2"}) == "r2"
    assert episode_text({"content": "<think> </think>ответ"}) is None
    two = {"content": "<think>a</think>x<think>b</think>"}
    assert episode_text(two) == "a\nb"
    assert episode_text({"content": None}) is None
    assert episode_text({}) is None


def test_episode_text_is_clipped():
    long = "x" * 20000
    assert episode_text({"reasoning_content": long}) == _clip_reasoning(long)
