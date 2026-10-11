import unittest

from gc_core.summarize.prompt_loading import (
    default_prompt_path,
    load_prompt_template,
)
from gc_core.summarize.summary_spec import (
    _SECTION_BODY_PATTERNS,
    SUMMARY_OUTPUT_SHAPE_PLACEHOLDER,
    SUMMARY_SECTIONS,
    SUMMARY_SECTIONS_PLACEHOLDER,
    agent_summary_slice_mode,
    assemble_summary_xml,
    extract_summary_bodies,
    merge_summary_bodies,
    render_output_shape_for_prompt,
    render_sections_for_prompt,
    section_tags,
    slice_summary,
)
from gc_core.summarize.summary_validation import (
    FALLBACK_SUMMARY,
    merge_repaired_summary,
    resolve_after_repair_failure,
    validate_summary_text,
)
from gc_testkit.helpers.summary_sample import valid_summary_xml_for_tests


class SummarySpecTest(unittest.TestCase):
    def test_agent_summary_slice_mode_table(self) -> None:
        self.assertEqual(
            agent_summary_slice_mode(num_batches=1, patch_executed=True),
            "short",
        )
        self.assertEqual(
            agent_summary_slice_mode(num_batches=2, patch_executed=False),
            "full",
        )

    def test_section_tags_follow_summary_sections_canon(self) -> None:
        self.assertEqual(
            section_tags(), tuple(s.tag for s in SUMMARY_SECTIONS)
        )
        self.assertEqual(set(_SECTION_BODY_PATTERNS), set(section_tags()))

    def test_extract_roundtrip_all_canon_tags(self) -> None:
        pad = "- test content for validation padding."
        bodies = {tag: f"body-{tag}\n{pad}" for tag in section_tags()}
        xml = assemble_summary_xml(bodies)
        extracted = extract_summary_bodies(xml)
        for tag in section_tags():
            self.assertEqual(extracted.get(tag), bodies[tag])
        ok, reason = validate_summary_text(xml)
        self.assertTrue(ok, reason)

    def test_prompt_template_injects_xml_sections(self) -> None:
        raw = open(default_prompt_path(), encoding="utf-8").read()
        self.assertIn(SUMMARY_SECTIONS_PLACEHOLDER, raw)
        self.assertIn("one XML document", raw)
        loaded = load_prompt_template(default_prompt_path())
        self.assertIn(SUMMARY_OUTPUT_SHAPE_PLACEHOLDER, raw)
        self.assertNotIn(SUMMARY_SECTIONS_PLACEHOLDER, loaded)
        self.assertNotIn(SUMMARY_OUTPUT_SHAPE_PLACEHOLDER, loaded)
        self.assertNotIn("Plain text only", loaded)
        for tag in section_tags():
            self.assertIn(f"<{tag}>", loaded)
        shape = render_output_shape_for_prompt()
        self.assertIn(shape, loaded)
        self.assertTrue(
            loaded.index("<summary>") < loaded.index("<task_and_context>")
        )

    def test_render_matches_spec_tags(self) -> None:
        block = render_sections_for_prompt()
        for section in SUMMARY_SECTIONS:
            self.assertIn(f"<{section.tag}>", block)
            self.assertIn(section.description.strip().splitlines()[0], block)

    def test_merge_repair_keeps_preserved_tags(self) -> None:
        pad = "- test content for validation padding."
        invalid = assemble_summary_xml(
            {
                "task_and_context": f"keep-me\n{pad}",
                "open_tasks": f"mode: execution\n{pad}",
            }
        )
        repaired = assemble_summary_xml(
            {
                "do_not_redo": pad,
                "last_verified_facts": pad,
                "active_constraints": pad,
                "recent_artifacts": pad,
            },
            only_present=True,
        )
        merged = merge_repaired_summary(
            invalid_text=invalid, repaired_text=repaired
        )
        ok, reason = validate_summary_text(merged)
        self.assertTrue(ok, reason)
        self.assertIn("keep-me", merged)
        self.assertIn("<do_not_redo>", merged)

    def test_slice_short_only_agent_visible_tags(self) -> None:
        full = valid_summary_xml_for_tests()
        short = slice_summary(full, "short")
        for section in SUMMARY_SECTIONS:
            if section.agent_visible:
                self.assertIn(f"<{section.tag}>", short)
            else:
                self.assertNotIn(f"<{section.tag}>", short)

    def test_slice_full_all_canon_tags(self) -> None:
        full = assemble_summary_xml(
            {tag: f"body-{tag}" for tag in section_tags()}
        )
        sliced = slice_summary(full, "full")
        for tag in section_tags():
            self.assertIn(f"<{tag}>body-{tag}</{tag}>", sliced)

    def test_partial_merged_when_strict_validate_fails(self) -> None:
        pad = "- test content for validation padding."
        invalid = assemble_summary_xml({"task_and_context": f"keep\n{pad}"})
        repaired = "<summary></summary>"
        merged = merge_repaired_summary(
            invalid_text=invalid, repaired_text=repaired
        )
        text, resolution = resolve_after_repair_failure(
            invalid_text=invalid,
            merged_text=merged,
            repaired_text=repaired,
            accumulated_summary="",
        )
        self.assertEqual(resolution, "partial_merged")
        self.assertIn("keep", text)
        ok, reason = validate_summary_text(text)
        self.assertTrue(ok, reason)

    def test_fallback_only_when_no_tags_anywhere(self) -> None:
        text, resolution = resolve_after_repair_failure(
            invalid_text="not xml",
            merged_text="<summary></summary>",
            repaired_text="",
            accumulated_summary="",
        )
        self.assertEqual(resolution, "fallback")
        self.assertEqual(text, FALLBACK_SUMMARY)
        self.assertTrue(validate_summary_text(text)[0])

    def test_merge_summary_bodies_repair_overrides(self) -> None:
        merged = merge_summary_bodies(
            preserved={"task_and_context": "old"},
            repaired={"task_and_context": "new", "open_tasks": "t"},
        )
        self.assertEqual(merged["task_and_context"], "new")
        self.assertEqual(merged["open_tasks"], "t")

    def test_extract_summary_bodies_includes_empty_present_tag(self) -> None:
        xml = "<summary><open_tasks></open_tasks></summary>"
        bodies = extract_summary_bodies(xml)
        self.assertEqual(bodies, {"open_tasks": ""})

    def test_merge_summary_bodies_empty_repair_wipes_preserved(self) -> None:
        merged = merge_summary_bodies(
            preserved={"open_tasks": "mode: execution\n- old todo"},
            repaired={"open_tasks": ""},
        )
        self.assertEqual(merged["open_tasks"], "")

    def test_merge_repaired_summary_empty_repair_tag_wipes_section(
        self,
    ) -> None:
        pad = "- test content for validation padding."
        invalid = assemble_summary_xml(
            {
                "task_and_context": f"keep-me\n{pad}",
                "open_tasks": f"mode: execution\n{pad}",
            }
        )
        repaired = assemble_summary_xml({"open_tasks": ""}, only_present=True)
        merged = merge_repaired_summary(
            invalid_text=invalid, repaired_text=repaired
        )
        self.assertIn("keep-me", merged)
        self.assertNotIn("mode: execution", merged)
        self.assertEqual(extract_summary_bodies(merged).get("open_tasks"), "")

    def test_slice_rejects_bad_root(self) -> None:
        with self.assertRaises(ValueError):
            slice_summary("<dialog_history>x</dialog_history>", "short")

    def test_extract_unescaped_ampersand_in_body(self) -> None:
        pad = "- test content for validation padding."
        raw = (
            "<summary>"
            f"<task_and_context>path a&b ok\n{pad}</task_and_context>"
            f"<open_tasks>{pad}</open_tasks>"
            f"<do_not_redo>{pad}</do_not_redo>"
            f"<last_verified_facts>{pad}</last_verified_facts>"
            f"<active_constraints>{pad}</active_constraints>"
            f"<recent_artifacts>{pad}</recent_artifacts>"
            "</summary>"
        )
        bodies = extract_summary_bodies(raw)
        self.assertIn("a&b", bodies["task_and_context"])
        ok, reason = validate_summary_text(raw)
        self.assertTrue(ok, reason)

    def test_extract_inside_markdown_fences(self) -> None:
        pad = "- test content for validation padding."
        inner = (
            f"<task_and_context>fenced\n{pad}</task_and_context>"
            f"<open_tasks>{pad}</open_tasks>"
            f"<do_not_redo>{pad}</do_not_redo>"
            f"<last_verified_facts>{pad}</last_verified_facts>"
            f"<active_constraints>{pad}</active_constraints>"
            f"<recent_artifacts>{pad}</recent_artifacts>"
        )
        raw = f"```xml\n<summary>{inner}</summary>\n```"
        bodies = extract_summary_bodies(raw)
        self.assertIn("fenced", bodies["task_and_context"])

    def test_extract_duplicate_tag_uses_last_match(self) -> None:
        bodies = extract_summary_bodies(
            "<summary><open_tasks>first</open_tasks><open_tasks>second</open_tasks></summary>"
        )
        self.assertEqual(bodies["open_tasks"], "second")

    def test_validate_accepts_short_all_n_a_sections(self) -> None:
        bodies = dict.fromkeys(section_tags(), "- n/a")
        xml = assemble_summary_xml(bodies)
        ok, reason = validate_summary_text(xml)
        self.assertTrue(ok, reason)

    def test_validate_rejects_all_empty_tag_bodies(self) -> None:
        xml = assemble_summary_xml(dict.fromkeys(section_tags(), ""))
        ok, reason = validate_summary_text(xml)
        self.assertFalse(ok)
        self.assertEqual(reason, "empty_content")


if __name__ == "__main__":
    unittest.main()
