import pytest
pytest.importorskip("textual")


@pytest.mark.asyncio
async def test_ctrl_c_idle_requests_exit_not_stop(tmp_path, monkeypatch):
    from tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path / "t"), config={"model": "M"})
    async with app.run_test() as pilot:
        scr = app.screen
        called = {"exit": 0, "stop": 0}
        monkeypatch.setattr(scr, "_request_exit", lambda: called.__setitem__("exit", called["exit"] + 1))
        monkeypatch.setattr(scr, "_cmd_stop", lambda: called.__setitem__("stop", called["stop"] + 1))
        monkeypatch.setattr(app, "_current_selection", lambda: "")
        app.action_interrupt()                       # нет run, нет выделения
        assert called == {"exit": 1, "stop": 0}      # exit-модалка, НЕ стоп/спам


@pytest.mark.asyncio
async def test_brand_card_shows_the_version_under_the_tagline(monkeypatch):
    """Версия — в карточке под тэглайном: её видно при каждом запуске, и рядом же
    появляется «↑ доступна новая версия», так что номер и предложение обновиться
    читаются вместе."""
    import app_version
    monkeypatch.setattr(app_version, "install_source",
                        lambda: app_version.InstallSource("u", "master", "c", False))
    monkeypatch.setattr(app_version, "agent_version", lambda: "9.9.9")
    from tui.app import TuiApp

    app = TuiApp(config={"model": "M"})
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        rows = ["".join(seg.text for seg in strip).rstrip()
                for strip in app.screen._compositor.render_strips()]
        tag = next(i for i, r in enumerate(rows) if "U  N  I" in r)
        # через пустую строку под тэглайном; рамку карточки из строки убираем
        assert rows[tag + 1].replace(chr(9474), "").strip() == ""
        assert rows[tag + 2].replace(chr(9474), "").strip() == "v9.9.9"


@pytest.mark.asyncio
async def test_stacked_brand_card_keeps_air_before_the_status_column():
    """Узкое окно: статусы встают ПОД брендом, и им нужен воздух сверху — иначе первая
    строка липнет к бренду (сбоку, в широком режиме, отступ не нужен и его нет)."""
    from tui.app import TuiApp

    app = TuiApp(config={"model": "M"})
    async with app.run_test(size=(74, 34)) as pilot:      # колонка сбоку не влезает
        await pilot.pause()
        rows = ["".join(seg.text for seg in strip).rstrip().replace(chr(9474), "")
                for strip in app.screen._compositor.render_strips()]
        status = next(i for i, r in enumerate(rows) if "готов к работе" in r)
        assert rows[status - 1].strip() == ""             # пустая строка перед статусом


@pytest.mark.asyncio
async def test_brand_card_relayouts_when_the_window_is_resized():
    """Раскладка карточки выбиралась только при сборке: сузив окно уже после старта,
    человек получал колонку статусов в пару символов — слова рвались по буквам, а
    совсем узко она схлопывалась в ноль и статусы пропадали вовсе."""
    from tui.app import TuiApp

    def _status_line(app) -> str:
        rows = ["".join(s.text for s in strip).rstrip().replace(chr(9474), "")
                for strip in app.screen._compositor.render_strips()]
        found = [r for r in rows if "готов к работе" in r]
        assert found, "строка статуса не отрисована — колонка схлопнулась"
        return found[0]

    logo_parts = ("█", "╚")            # куски псевдографики логотипа
    app = TuiApp(config={"model": "M"})
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        # старт в широком окне: статусы сбоку — делят строку экрана с логотипом
        assert any(ch in _status_line(app) for ch in logo_parts)

        await pilot.resize_terminal(68, 34)         # сузили уже после старта
        await pilot.pause()
        await pilot.pause()
        narrow = _status_line(app)
        assert not any(ch in narrow for ch in logo_parts)      # ушли под логотип
        assert narrow.strip() == chr(10003) + " готов к работе"  # целиком, не по буквам

        await pilot.resize_terminal(110, 34)        # вернули ширину — снова сбоку
        await pilot.pause()
        await pilot.pause()
        assert any(ch in _status_line(app) for ch in logo_parts)


@pytest.mark.asyncio
async def test_status_column_goes_under_the_logo_when_a_line_does_not_fit():
    """Порог узости — по самой длинной строке статусов, а не по фиксированному числу:
    длинная MCP-строка уводит колонку вниз даже на окне, где короткая помещалась бы.
    Иначе слова рвались по буквам («подк / люче / ны»)."""
    from tui.app import TuiApp

    def _stacked(app) -> bool:
        rows = ["".join(s.text for s in strip).rstrip().replace(chr(9474), "")
                for strip in app.screen._compositor.render_strips()]
        line = next(r for r in rows if "готов к работе" in r)
        return not any(ch in line for ch in ("█", "╚"))   # нет логотипа рядом

    app = TuiApp(config={"model": "M"})
    async with app.run_test(size=(96, 30)) as pilot:
        await pilot.pause()
        assert not _stacked(app)                     # коротким строкам места хватает
        app.screen._refresh_notices_card(
            mcp_line="✓ MCP: все подключены · /mcp")  # строка стала длиннее
        await pilot.pause()
        await pilot.pause()
        assert _stacked(app)                         # и колонка ушла вниз целиком


@pytest.mark.asyncio
async def test_stacked_status_column_has_no_left_indent():
    """Под логотипом колонке не от чего отступать — отступ там читался как случайный
    сдвиг вправо (сбоку он отделяет её от логотипа и нужен)."""
    from tui.app import TuiApp

    app = TuiApp(config={"model": "M"})
    async with app.run_test(size=(70, 30)) as pilot:
        await pilot.pause()
        rows = ["".join(s.text for s in strip).rstrip().replace(chr(9474), "")
                for strip in app.screen._compositor.render_strips()]
        status = next(r for r in rows if "готов к работе" in r)
        logo = next(r for r in rows if "█" in r)
        assert len(status) - len(status.lstrip()) < len(logo) - len(logo.lstrip()) + 2
