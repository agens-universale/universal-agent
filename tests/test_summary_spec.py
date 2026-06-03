import unittest

from summarize.prompt_loading import default_prompt_path, load_prompt_template
from summarize.summary_spec import (
    MIN_SECTION_HITS,
    SUMMARY_SECTIONS,
    SUMMARY_SECTIONS_PLACEHOLDER,
    format_section_titles_for_repair,
    render_sections_for_prompt,
    section_titles,
)
from summarize.summary_validation import (
    REQUIRED_SECTION_MARKERS,
    section_hit_count,
    validate_summary_text,
)
from tests.helpers.summary_sample import valid_summary_text_for_tests


class SummarySpecTest(unittest.TestCase):
    def test_section_titles_match_required_markers(self) -> None:
        self.assertEqual(section_titles(), REQUIRED_SECTION_MARKERS)
        self.assertEqual(len(SUMMARY_SECTIONS), 6)
        self.assertEqual(len(SUMMARY_SECTIONS), len(REQUIRED_SECTION_MARKERS))

    def test_prompt_template_injects_sections(self) -> None:
        raw = open(default_prompt_path(), encoding="utf-8").read()
        self.assertIn(SUMMARY_SECTIONS_PLACEHOLDER, raw)
        loaded = load_prompt_template(default_prompt_path())
        self.assertNotIn(SUMMARY_SECTIONS_PLACEHOLDER, loaded)
        for title in section_titles():
            self.assertIn(f"{title}:", loaded)

    def test_render_matches_spec_titles(self) -> None:
        block = render_sections_for_prompt()
        for section in SUMMARY_SECTIONS:
            self.assertIn(f"{section.title}:", block)
            self.assertIn(section.description.strip().splitlines()[0], block)

    def test_repair_lists_same_titles(self) -> None:
        repair_titles = format_section_titles_for_repair().split(", ")
        self.assertEqual(tuple(repair_titles), section_titles())

    def test_spec_includes_do_not_redo_and_open_tasks(self) -> None:
        titles = section_titles()
        self.assertIn("DO_NOT_REDO", titles)
        self.assertIn("OPEN_TASKS", titles)

    def test_validate_min_section_hits(self) -> None:
        text = valid_summary_text_for_tests()
        ok, reason = validate_summary_text(text)
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "ok")
        self.assertGreaterEqual(section_hit_count(text), MIN_SECTION_HITS)

        one_section = "TASK_AND_CONTEXT:\n" + "\n".join(f"- line {i}" for i in range(20))
        ok2, reason2 = validate_summary_text(one_section)
        self.assertFalse(ok2)
        self.assertEqual(reason2, "missing_structure")
        self.assertEqual(section_hit_count(one_section), 1)

        four_sections = (
            "TASK_AND_CONTEXT:\n"
            + "\n".join(f"- ctx {i}" for i in range(8))
            + "\n\nOPEN_TASKS:\n- mode: planning-only\n"
            + "\n".join(f"- [TODO] step {i}" for i in range(8))
            + "\n\nDO_NOT_REDO:\n- n/a\n\nLAST_VERIFIED_FACTS:\n"
            + "\n".join(f"- fact {i}" for i in range(8))
        )
        ok3, reason3 = validate_summary_text(four_sections)
        self.assertTrue(ok3, reason3)
        self.assertGreaterEqual(section_hit_count(four_sections), MIN_SECTION_HITS)


if __name__ == "__main__":
    unittest.main()
