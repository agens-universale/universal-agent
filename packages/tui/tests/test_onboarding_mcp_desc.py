import pytest


class _FakeMcp:
    def installable_names(self): return ["duckduckgo_search", "office"]
    def wired_names(self): return []
    def descriptions(self): return {"duckduckgo_search": "веб-поиск в интернете",
                                     "office": "чтение/запись Word · Excel · PDF"}
    def install(self, name=None): return 0


def _screen():
    from gc_tui.screens.onboarding import OnboardingScreen
    cfg = {"completions_url": "http://your-backend:PORT/v1/chat/completions",
           "chat_completion_headers": {"Authorization": "Bearer <ВПИШИТЕ_ТОКЕН>"},
           "model": "<ВПИШИТЕ_МОДЕЛЬ>"}
    return OnboardingScreen(config=cfg, config_path="cfg.yml", review_all=False,
                             on_commit=lambda: None, fetch=lambda *a, **k: None,
                             mcp=_FakeMcp())


@pytest.mark.asyncio
async def test_onboarding_mcp_row_shows_description(tmp_path):
    # строка MCP на онбординге показывает приглушённое описание (тот же источник, что /settings)
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={})
    async with app.run_test() as pilot:
        scr = _screen()
        await pilot.app.push_screen(scr)
        await pilot.pause()
        scr._step = 3
        scr._render_step()
        await pilot.pause()
        blob = " ".join(str(w.render()) for w in scr.query(".onb-option"))
        assert "веб-поиск" in blob
