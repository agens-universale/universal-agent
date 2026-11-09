from gc_tui.services.path_display import fit_path


def test_fit_path_keeps_a_path_that_fits():
    assert fit_path("~/Desktop/shot.png", 40) == "~/Desktop/shot.png"


def test_fit_path_collapses_the_middle():
    out = fit_path("~/Desktop/screens/2026/september/shot.png", 24)
    assert out.startswith("~/") and out.endswith("/shot.png") and "…" in out
    assert len(out) <= 24


def test_fit_path_falls_back_to_the_file_name():
    assert fit_path("~/Desktop/screens/shot.png", 9) == "shot.png"
    assert fit_path("~/Desktop/screens/shot.png", 3) == "shot.png"
    assert fit_path("a-very-long-name.png", 5) == "a-very-long-name.png"
