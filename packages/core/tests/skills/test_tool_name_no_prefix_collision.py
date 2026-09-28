"""Регресс: ни одно skills-тул-имя не должно быть строгим префиксом другого.

Слабая модель при `tool_choice: auto`, набрав длинное имя тула в истории, не
может автогрессивно остановиться на более коротком имени-префиксе — она
дописывает виденный суффикс. `skills_load` ⊂ `skills_load_reference` — ровно эта
ловушка. См. docs/specs/2026-06-25-skills-tool-namespace-decollision-design.md.
"""

from gc_core.tools.skills.skills_tools import get_skills_tools_data


def test_skills_tool_names_have_no_prefix_collision():
    names = list(get_skills_tools_data(None).keys())
    collisions = [
        (a, b) for a in names for b in names if a != b and b.startswith(a)
    ]
    assert not collisions, (
        "tool name is a strict prefix of another (autoregressive tool-name trap): "
        + ", ".join(f"{a!r} prefix-of {b!r}" for a, b in collisions)
    )
