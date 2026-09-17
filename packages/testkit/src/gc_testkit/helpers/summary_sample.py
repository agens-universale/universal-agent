"""Образцы сводки для тестов."""

from gc_core.summarize.summary_spec import assemble_summary_xml, primary_section_tag, section_tags


def valid_summary_xml_for_tests(
    *,
    seed: str | None = None,
    line_filler: str | None = None,
) -> str:
    filler = line_filler or "- test content for validation padding."
    bodies: dict[str, str] = {}
    for tag in section_tags():
        body = filler
        if tag == primary_section_tag() and seed:
            body = f"- {seed}\n{filler}"
        bodies[tag] = body
    return assemble_summary_xml(bodies)
