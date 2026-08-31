"""Рецепт ``find`` для обхода и для списка файлов — один источник.

Оба билдера делят голову ``find -H PATH -mindepth 1`` (``-H`` — паритет с
``os.walk``, который разыменовывает свою вершину) и группу prune. Обход
печатает pruned-каталог ДО ``-prune`` (листинг видит его, поиск по имени
находит), список файлов для grep — нет (``-type f``).
"""

import shlex

from backends.capabilities import parse_find_printf_entries
from backends.harbor import _build_find_walk_command
from backends.protocol import FIND_PRINTF_FORMAT
from utils.native_fs_search import _build_find_prune_argv, find_prune_group, find_walk_head


def test_walk_head_resolves_a_symlinked_start_path():
    assert find_walk_head("/w") == ["find", "-H", "/w", "-mindepth", "1"]


def test_prune_group_is_shared_by_both_builders():
    group = find_prune_group(exclude_dir_names=("node_modules", ""), exclude_nested_dot_dirs=True)
    assert group == ["(", "-type", "d", "-name", "node_modules", "-o", "-type", "d", "-name", ".*", ")"]
    assert find_prune_group(exclude_dir_names=(), exclude_nested_dot_dirs=False) == []

    files_argv = _build_find_prune_argv(
        "/w", exclude_dir_names=("node_modules",), exclude_nested_dot_dirs=True, file_pattern=None
    )
    assert files_argv[:5] == ["find", "-H", "/w", "-mindepth", "1"]
    assert files_argv[5:16] == group
    # Ссылки перечисляются наравне с обычными файлами.
    assert files_argv[16:] == [
        "-prune", "-o", "(", "-type", "f", "-o", "-type", "l", ")", "-print0",
    ]


def test_walk_command_prints_the_pruned_dir_before_pruning_it():
    cmd = _build_find_walk_command("/w", prune_names=("node_modules",), prune_dot_dirs=True)
    printf = f"-printf {shlex.quote(FIND_PRINTF_FORMAT)}"
    assert cmd.startswith("find -H /w -mindepth 1 '(' -type d -name node_modules -o -type d -name '.*' ')' ")
    assert f"')' {printf} -prune -o {printf}" in cmd
    assert cmd.count("-printf") == 2


def test_walk_command_without_prunes_is_a_plain_listing():
    cmd = _build_find_walk_command("/w")
    assert cmd == f"find -H /w -mindepth 1 -printf {shlex.quote(FIND_PRINTF_FORMAT)}"


def test_parse_symlink_size_is_unknown():
    rows = parse_find_printf_entries("link\tl\t7\nreal\tf\t7\n")
    assert [(e.rel, e.is_dir, e.size) for e in rows] == [("link", False, None), ("real", False, 7)]
