from gc_tui.services.path_display import display_path, fit_path

# ---- путь картинки под репликой: от проекта / через ~ / полный ----

ROOT = "/home/dmitry/work/project"
HOME = "/home/dmitry"


def test_display_path_inside_the_project_is_relative():
    assert display_path(f"{ROOT}/docs/shot.png", ROOT, HOME) == "docs/shot.png"


def test_display_path_inside_home_uses_tilde():
    assert (
        display_path(f"{HOME}/Desktop/shot.png", ROOT, HOME)
        == "~/Desktop/shot.png"
    )


def test_display_path_elsewhere_is_absolute():
    assert display_path("/tmp/shot.png", ROOT, HOME) == "/tmp/shot.png"


def test_display_path_keeps_a_bare_name():
    """Старые сессии хранят только имя файла — показываем его как есть."""
    assert display_path("shot.png", ROOT, HOME) == "shot.png"


def test_display_path_sibling_with_the_same_prefix_is_not_inside():
    assert (
        display_path(f"{ROOT}-old/shot.png", ROOT, HOME)
        == "~/work/project-old/shot.png"
    )


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
