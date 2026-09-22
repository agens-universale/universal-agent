"""TDD: MessageProjectionPipeline; default = effective strip tags via args."""

from gc_core.message_projection import (
    MessageProjectionPipeline,
    effective_for_model_tags,
    for_model_pipeline,
    project_assistant_text,
    project_text_for_model,
)
from gc_core.message_projection.projectors.strip_tagged_blocks import strip_think_blocks


def test_default_pipeline_strips_think_only_without_extra_tags():
    pipeline = for_model_pipeline()
    assert pipeline.project("<think>x</think>Y") == "Y"
    assert pipeline.project("<redacted_thinking>x</redacted_thinking>Y") == "<redacted_thinking>x</redacted_thinking>Y"


def test_project_text_for_model_uses_default_strip():
    assert project_text_for_model("<think>x</think>Y") == "Y"


def test_extra_tag_strips_in_pipeline():
    assert (
        project_text_for_model(
            "<think>t</think><redacted_thinking>r</redacted_thinking>ok",
            tags=["redacted_thinking"],
        )
        == "ok"
    )


def test_think_in_tags_with_strip_think_true_dedupes():
    assert project_text_for_model("<think>x</think>Y", tags=["think", "THINK"]) == "Y"


def test_effective_for_model_tags_puts_think_first():
    assert effective_for_model_tags(("redacted_thinking",)) == (
        "think",
        "redacted_thinking",
    )
    assert effective_for_model_tags(("redacted_thinking", "think")) == (
        "think",
        "redacted_thinking",
    )
    assert effective_for_model_tags(("think", "redacted_thinking")) == (
        "think",
        "redacted_thinking",
    )
    assert effective_for_model_tags(("redacted_thinking",), strip_think=False) == ("redacted_thinking",)


def test_unclosed_extra_tag_inside_think_does_not_eat_answer():
    text = "<think>let me use <redacted_thinking> here</think>Final answer to the user."
    assert project_text_for_model(text, tags=["redacted_thinking"]) == "Final answer to the user."
    assert (
        project_text_for_model(text, tags=["redacted_thinking", "think"], strip_think=True)
        == "Final answer to the user."
    )


def test_strip_think_false_empty_tags_keeps_think_prose():
    assert project_text_for_model("<think>x</think>Y", strip_think=False) == ("<think>x</think>Y")
    assert project_text_for_model("  keep spaces  ", strip_think=False) == ("  keep spaces  ")


def test_strip_think_false_think_in_tags_strips_prose_only():
    assert project_text_for_model("<think>x</think>Y", tags=["think"], strip_think=False) == "Y"
    assert (
        project_text_for_model(
            "<redacted_thinking>r</redacted_thinking>ok",
            tags=["think"],
            strip_think=False,
        )
        == "<redacted_thinking>r</redacted_thinking>ok"
    )


def test_strip_think_false_extra_tag_does_not_strip_think_prose():
    assert (
        project_text_for_model(
            "<think>t</think><redacted_thinking>r</redacted_thinking>ok",
            tags=["redacted_thinking"],
            strip_think=False,
        )
        == "<think>t</think>ok"
    )


def test_strip_think_true_plus_extra_still_strips_think():
    assert (
        project_text_for_model(
            "<think>t</think><redacted_thinking>r</redacted_thinking>ok",
            tags=["redacted_thinking"],
            strip_think=True,
        )
        == "ok"
    )


def test_pipeline_applies_projectors_in_order():
    def upper(text: str) -> str:
        return text.upper()

    def add_bang(text: str) -> str:
        return text + "!"

    pipeline = MessageProjectionPipeline([upper, add_bang])
    assert pipeline.project("ab") == "AB!"


def test_project_text_accepts_custom_pipeline():
    def wrap(text: str) -> str:
        return f"[{text}]"

    pipeline = MessageProjectionPipeline([strip_think_blocks, wrap])
    assert project_text_for_model("<think>t</think>hi", pipeline=pipeline) == "[hi]"


def test_project_assistant_text_string_and_list():
    assert project_assistant_text("<think>t</think>ok") == "ok"
    content = [
        {"type": "output_text", "text": "<think>r</think>\n\nFinal."},
    ]
    assert project_assistant_text(content) == "Final."


def test_project_assistant_text_only_think_empty():
    assert project_assistant_text("<think>only</think>") == ""


def test_non_greedy_in_default_pipeline():
    assert project_text_for_model("<think>a</think> prose <think>b</think> end") == "prose  end"
