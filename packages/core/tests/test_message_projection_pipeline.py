"""TDD: MessageProjectionPipeline; default = effective strip tags; configure."""

from gc_core.message_projection import MessageProjectionPipeline, configure_for_model_tags, default_for_model_pipeline, project_assistant_text, project_text_for_model
from gc_core.message_projection.projectors.strip_tagged_blocks import strip_think_blocks


def test_default_pipeline_strips_think_only_without_configure():
    configure_for_model_tags([])
    pipeline = default_for_model_pipeline()
    assert pipeline.project("<think>x</think>Y") == "Y"
    assert (
        pipeline.project("<redacted_thinking>x</redacted_thinking>Y")
        == "<redacted_thinking>x</redacted_thinking>Y"
    )


def test_project_text_for_model_uses_default_strip():
    configure_for_model_tags([])
    assert project_text_for_model("<think>x</think>Y") == "Y"


def test_configure_extra_tag_strips_in_default_pipeline():
    configure_for_model_tags(["redacted_thinking"])
    try:
        assert (
            project_text_for_model(
                "<think>t</think><redacted_thinking>r</redacted_thinking>ok"
            )
            == "ok"
        )
    finally:
        configure_for_model_tags([])


def test_configure_think_in_extra_is_noop():
    configure_for_model_tags(["think", "THINK"])
    try:
        assert project_text_for_model("<think>x</think>Y") == "Y"
    finally:
        configure_for_model_tags([])


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
    configure_for_model_tags([])
    assert project_assistant_text("<think>t</think>ok") == "ok"
    content = [
        {"type": "output_text", "text": "<think>r</think>\n\nFinal."},
    ]
    assert project_assistant_text(content) == "Final."


def test_project_assistant_text_only_think_empty():
    configure_for_model_tags([])
    assert project_assistant_text("<think>only</think>") == ""


def test_non_greedy_in_default_pipeline():
    configure_for_model_tags([])
    assert (
        project_text_for_model("<think>a</think> prose <think>b</think> end")
        == "prose  end"
    )
