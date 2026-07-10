from tools.fs.edit_suggestions import build_edit_miss_suggestions


def test_extra_spaces_and_case_match_single_line():
    content = "KEY: value\nretry    count\n"
    suggestions, total = build_edit_miss_suggestions("key: value", content)
    assert total >= 1
    assert suggestions[0]["line"] == 1
    assert suggestions[0]["text"] == "KEY: value"
    assert suggestions[0]["reason"] == "single-line difflib similarity"
    assert suggestions[0]["score"] > 0.2

    suggestions, total = build_edit_miss_suggestions("retry count", content)
    assert total >= 1
    assert suggestions[0]["line"] == 2
    assert suggestions[0]["text"] == "retry    count"


def test_indent_difference_still_suggests():
    content = "    def foo():\n        return 1\n"
    suggestions, total = build_edit_miss_suggestions("def foo():", content)
    assert total >= 1
    assert suggestions[0]["line"] == 1
    assert suggestions[0]["score"] > 0.2


def test_typo_in_token_suggests_nearest_line():
    content = "alpha beta gamma\nunrelated line\n"
    suggestions, total = build_edit_miss_suggestions("alpha betta gamma", content)
    assert total >= 1
    assert suggestions[0]["line"] == 1
    assert suggestions[0]["score"] > 0.2


def test_multiline_near_miss():
    content = "foo bar\nbaz qux\nzap zot\n"
    suggestions, total = build_edit_miss_suggestions("foo baar\nbaz qxx", content)
    assert total >= 1
    assert suggestions[0]["line"] == 1
    assert suggestions[0]["end_line"] == 2
    assert suggestions[0]["reason"] == "multiline difflib similarity"
    assert 0.2 < suggestions[0]["score"] <= 1.0


def test_score_threshold_and_top_three():
    lines = [f"alpha beta line {i}" for i in range(20)]
    content = "\n".join(lines) + "\n"
    suggestions, total = build_edit_miss_suggestions("Alpha   beta", content)
    assert total == 20
    assert len(suggestions) == 3
    scores = [s["score"] for s in suggestions]
    assert scores == sorted(scores, reverse=True)
    assert all(s > 0.2 for s in scores)


def test_below_min_score_excluded():
    content = "zzzzzzzz\nyyyyyyyy\n"
    suggestions, total = build_edit_miss_suggestions("completely different", content, min_score=0.9)
    assert suggestions == []
    assert total == 0


def test_truncate_long_suggestion_text():
    long_line = "x" * 250
    content = long_line + "\n"
    suggestions, total = build_edit_miss_suggestions("x" * 40, content, max_text_chars=200)
    assert total >= 1
    assert suggestions[0]["text"] == ("x" * 200) + "…"
    assert len(suggestions[0]["text"]) == 201


def test_empty_search_or_content():
    assert build_edit_miss_suggestions("", "a\n") == ([], 0)
    assert build_edit_miss_suggestions("a", "") == ([], 0)
    assert build_edit_miss_suggestions("a", "\n\n") == ([], 0)


def test_single_token_search_can_suggest():
    content = "ALPHA beta gamma\n"
    suggestions, total = build_edit_miss_suggestions("ALPHA", content)
    assert total >= 1
    assert suggestions[0]["line"] == 1
    assert suggestions[0]["reason"] == "single-line difflib similarity"


def test_picks_nearest_among_distractors():
    content = (
        "completely unrelated header\n"
        "def compute_total(items):\n"
        "    return sum(items)\n"
        "another noise line here\n"
    )
    suggestions, total = build_edit_miss_suggestions(
        "def compute_totl(items):",
        content,
    )
    assert total >= 1
    assert suggestions[0]["line"] == 2
    assert "compute_total" in suggestions[0]["text"]
    assert suggestions[0]["score"] > 0.2


def test_mid_score_near_miss_not_dropped():
    """Scores in ~0.2–0.6 must survive (old SAR cutoff was 0.6)."""
    content = "hello world example\nzzzzzzzzzzzzzzzzzz\n"
    suggestions, total = build_edit_miss_suggestions(
        "hello wrld exampl",
        content,
    )
    assert total >= 1
    assert suggestions[0]["line"] == 1
    assert suggestions[0]["score"] > 0.2


def test_multiline_picks_correct_window_among_blocks():
    content = (
        "aaa aaa\n"
        "bbb bbb\n"
        "foo bar\n"
        "baz qux\n"
        "ccc ccc\n"
        "ddd ddd\n"
    )
    suggestions, total = build_edit_miss_suggestions(
        "foo baar\nbaz qxx",
        content,
    )
    assert total >= 1
    assert suggestions[0]["line"] == 3
    assert suggestions[0]["end_line"] == 4
    assert suggestions[0]["text"] == "foo bar\nbaz qux"


def test_raw_text_preserves_original_whitespace():
    content = "\tfoo   bar\n"
    suggestions, total = build_edit_miss_suggestions("foo bar", content)
    assert total >= 1
    assert suggestions[0]["text"] == "\tfoo   bar"


def test_ranking_prefers_closer_line():
    content = "alpha beta\nalpha beta gamma delta\nalpha\n"
    suggestions, total = build_edit_miss_suggestions(
        "alpha beta gamma delta",
        content,
    )
    assert total >= 2
    assert suggestions[0]["line"] == 2
    assert suggestions[0]["score"] >= suggestions[1]["score"]
