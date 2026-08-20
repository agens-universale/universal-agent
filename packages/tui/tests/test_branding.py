"""branding — единый источник бренд-контента TUI GIGACODE."""
from gc_tui import branding


def test_logo_shape():
    lines = branding.LOGO.split("\n")
    assert len(lines) == 6
    assert max(len(line) for line in lines) == 62


def test_gradient_color_stops():
    # амбер (RUN_COLOR) -> олива (OLIVE) -> teal (ACCENT)
    assert branding.gradient_color(0.0) == "#D4A574"
    assert branding.gradient_color(0.5) == "#8FA05A"
    assert branding.gradient_color(1.0) == "#54B3A6"


def test_gradient_color_clamps():
    assert branding.gradient_color(-1.0) == "#D4A574"
    assert branding.gradient_color(2.0) == "#54B3A6"


def test_logo_text_plain_matches_and_spaces_unstyled():
    t = branding.logo_text()
    assert t.plain == branding.LOGO
    # каждый видимый глиф получает свой span; пробелы и переводы строк — нет.
    glyphs = sum(1 for ch in branding.LOGO if ch not in (" ", "\n"))
    assert len(t.spans) == glyphs


def test_tagline_display():
    assert branding.tagline_display() == "U N I V E R S A L   A G E N T"


def test_version_under_logo_is_centered_like_the_tagline(monkeypatch):
    """Версия центрируется по ширине лого ведущими пробелами — тем же приёмом, что и
    тэглайн: рамка карточки шире бренда, и CSS-выравнивание увело бы строку по ней."""
    from gc_core import app_version
    monkeypatch.setattr(app_version, "install_source",
                        lambda: app_version.InstallSource("u", "master", "c", False))
    monkeypatch.setattr(app_version, "agent_version", lambda: "1.2.3")
    line = branding.version_under_logo()
    assert line.strip() == "v1.2.3"
    logo_w = max(len(l) for l in branding.LOGO.splitlines())
    assert len(line) - len(line.strip()) == (logo_w - len("v1.2.3")) // 2
    # центры строки версии и тэглайна совпадают (с точностью до нечётной ширины)
    tag = branding.tagline_under_logo()
    center = lambda s: (len(s) - len(s.strip())) + len(s.strip()) / 2
    assert abs(center(line) - center(tag)) <= 1


def test_version_shows_dev_when_running_from_sources(monkeypatch):
    """Из исходников метаданные пакета отстают от кода — номер соврал бы, поэтому dev."""
    from gc_core import app_version
    monkeypatch.setattr(app_version, "install_source",
                        lambda: app_version.InstallSource("u", None, None, True))
    assert branding.version_under_logo().strip() == app_version.DEV_LABEL
    assert "из исходников" in app_version.DEV_LABEL        # не голое «dev»: сухо и неясно
    monkeypatch.setattr(app_version, "install_source", lambda: None)   # нет метаданных
    assert branding.version_under_logo().strip() == app_version.DEV_LABEL


def test_version_forms_follow_one_rule(monkeypatch):
    """Короткая форма — для тесных строк (шапка /settings), длинная — где есть место
    (карточка, секция «Обновление»). Правило одно: из исходников номер не показываем,
    он остался от установки пакета и коду уже не соответствует."""
    from gc_core import app_version

    monkeypatch.setattr(app_version, "install_source",
                        lambda: app_version.InstallSource("u", None, None, True))
    assert app_version.display_version() == "dev"
    assert app_version.display_version(long=True) == app_version.DEV_LABEL

    monkeypatch.setattr(app_version, "install_source",
                        lambda: app_version.InstallSource("u", "master", "c", False))
    monkeypatch.setattr(app_version, "agent_version", lambda: "1.2.3")
    # установлено штатно — обе формы одинаковы, пояснять нечего
    assert app_version.display_version() == "v1.2.3"
    assert app_version.display_version(long=True) == "v1.2.3"
