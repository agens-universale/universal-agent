"""TDD: MessageProjectionPipeline — порядок projectors; default = strip_think only."""

from message_projection import (
    MessageProjectionPipeline,
    default_for_model_pipeline,
    project_assistant_text,
    project_text_for_model,
)
from message_projection.projectors.strip_think import strip_think_blocks


def test_default_pipeline_is_strip_think_only():
    pipeline = default_for_model_pipeline()
    assert pipeline.projectors == (strip_think_blocks,)


def test_project_text_for_model_uses_default_strip():
    assert project_text_for_model("<think>x</think>Y") == "Y"


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
