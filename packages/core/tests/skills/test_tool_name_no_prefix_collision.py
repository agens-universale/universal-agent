"""Регресс: ни одно skills-тул-имя не должно быть строгим префиксом другого.

Слабая модель при `tool_choice: auto`, набрав длинное имя тула в истории, не
может автогрессивно остановиться на более коротком имени-префиксе — она
дописывает виденный суффикс. `skills_load` ⊂ `skills_load_reference` — ровно эта
ловушка. См. docs/specs/2026-06-25-skills-tool-namespace-decollision-design.md.
"""

from gc_core.tools.skills.skills_tools import get_skills_tools_data
from gc_testkit.settings import make_settings


def test_skills_tool_names_have_no_prefix_collision():
    names = list(get_skills_tools_data(None).keys())
    collisions = [
        (a, b) for a in names for b in names if a != b and b.startswith(a)
    ]
    assert not collisions, (
        "tool name is a strict prefix of another (autoregressive tool-name trap): "
        + ", ".join(f"{a!r} prefix-of {b!r}" for a, b in collisions)
    )


def test_builtin_and_skills_tool_names_have_no_prefix_collision():
    """Та же ловушка у встроенных тулов: имя не должно быть префиксом соседнего."""
    from gc_core.constants.tool_names import FS_VIEW_IMAGE_NAME
    from gc_core.tools.registry import all_builtin_tool_names

    names = sorted(
        all_builtin_tool_names(
            make_settings({"backend.api_mode": "chat_completions"})
        )
        | set(get_skills_tools_data(None))
    )
    assert FS_VIEW_IMAGE_NAME in names
    collisions = [
        (a, b) for a in names for b in names if a != b and b.startswith(a)
    ]
    assert collisions == []
