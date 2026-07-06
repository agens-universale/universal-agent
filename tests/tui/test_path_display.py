from tui.services.path_display import truncate_path


def test_full_path_when_fits():
    p = r"D:\dev\proj"
    assert truncate_path(p, 40) == p


def test_middle_collapsed_keeps_root_and_tail():
    p = r"D:\dev\PycharmProjects\ai\ReAct-Agent"
    out = truncate_path(p, 22)
    assert out.startswith("D:") and out.endswith("ReAct-Agent") and "…" in out
    assert len(out) <= 22


def test_very_narrow_keeps_tail_with_ellipsis():
    p = r"D:\dev\PycharmProjects\ai\ReAct-Agent"
    out = truncate_path(p, 15)
    assert out.endswith("ReAct-Agent")
    assert out.startswith("…")
    assert len(out) <= 15


def test_tail_longer_than_max_truncates_tail():
    p = r"D:\very-long-final-directory-name"
    out = truncate_path(p, 10)
    assert len(out) <= 10


def test_posix_separator():
    p = "/home/dmitry/work/project"
    out = truncate_path(p, 16)
    assert out.endswith("project") and len(out) <= 16


def test_absolute_posix_preserves_leading_slash_narrow():
    p = "/home/dmitry/work/project"
    out = truncate_path(p, 18)
    assert out.startswith("/"), f"leading slash lost: {out!r}"
    assert out.endswith("project"), f"tail lost: {out!r}"
    assert len(out) <= 18


def test_absolute_posix_preserves_leading_slash_wide():
    p = "/home/dmitry/work/project"
    out = truncate_path(p, 40)
    assert out.startswith("/")
    assert out == p
