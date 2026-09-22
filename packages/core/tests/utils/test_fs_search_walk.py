"""Hard walk floor for FS search tools."""

from gc_core.utils.fs_search_walk import (
    HARD_EXCLUDE_DIR_NAMES,
    HARD_EXCLUDE_NESTED_DOT_DIRS,
    effective_exclude_dir_names,
    hard_exclude_dir_names_for_prompt,
    should_skip_nested_dir_name,
)


def test_hard_floor_includes_common_junk():
    assert "venv" in HARD_EXCLUDE_DIR_NAMES
    assert "node_modules" in HARD_EXCLUDE_DIR_NAMES
    assert "__pycache__" in HARD_EXCLUDE_DIR_NAMES
    assert ".git" in HARD_EXCLUDE_DIR_NAMES
    assert HARD_EXCLUDE_NESTED_DOT_DIRS is True


def test_effective_names_union_extra_and_ignores_empty_config():
    assert effective_exclude_dir_names(None) == HARD_EXCLUDE_DIR_NAMES
    assert effective_exclude_dir_names([]) == HARD_EXCLUDE_DIR_NAMES
    assert effective_exclude_dir_names(["venv"]) == HARD_EXCLUDE_DIR_NAMES
    extra = effective_exclude_dir_names(["build", "dist", ""])
    assert "build" in extra and "dist" in extra
    assert extra >= HARD_EXCLUDE_DIR_NAMES


def test_should_skip_reasons():
    names = effective_exclude_dir_names()
    assert should_skip_nested_dir_name("venv", exclude_names=names) == (
        "excluded_directory_name"
    )
    assert should_skip_nested_dir_name(".cache", exclude_names=names) == (
        "hidden_dot_directory"
    )
    assert should_skip_nested_dir_name("src", exclude_names=names) is None
    assert (
        should_skip_nested_dir_name(
            ".cache", exclude_names=names, exclude_dot_dirs=False
        )
        is None
    )


def test_prompt_list_sorted_and_stable():
    s = hard_exclude_dir_names_for_prompt()
    assert s == ", ".join(sorted(HARD_EXCLUDE_DIR_NAMES))
