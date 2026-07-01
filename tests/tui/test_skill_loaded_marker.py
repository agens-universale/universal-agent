import pytest


def _trace_texts(scr):
    from textual.widgets import Static
    return [str(w.render()) for w in scr.query("#trace Static")]


@pytest.mark.asyncio
async def test_inline_marker_loaded_after_reply(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    import tools.skills.skill_delivery as sd
    import tools.skills.registry as sreg
    monkeypatch.setattr(sreg, "get_skills_registry", lambda cfg: object())
    monkeypatch.setattr(sd, "build_skill_delivery", lambda skill_id, reg: "WIRE")
    monkeypatch.setattr(sd, "skill_is_available", lambda skill_id, reg: True)

    class _Fake:
        def run(self, messages, **kw):
            import threading
            self._evt = threading.Event(); self._evt.wait(timeout=2.0)
        def stop_run(self):
            if getattr(self, "_evt", None): self._evt.set()
        def add_message(self, m): pass

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    app._executor_factory = lambda: _Fake()
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [{"id": "coder"}]
        scr._handle_command("skill coder создай скилл")
        await pilot.pause(0.3)
        texts = _trace_texts(scr)
        joined = "\n".join(texts)
        assert "загружен" in joined
        assert "отправлен агенту" not in joined
        # маркер ПОСЛЕ реплики: порядок по mount-индексу виджетов в #trace
        from tui.widgets.user_reply import UserReplyContainer
        from textual.widgets import Static
        trace = scr.query_one("#trace")
        kids = list(trace.children)
        reply = scr.query_one(UserReplyContainer)
        reply_i = kids.index(reply)

        def _widget_text(w):
            """Рекурсивно собирает текст Static-потомков виджета."""
            parts = []
            try:
                parts.append(str(w.render()))
            except Exception:
                pass
            for child in w.query(Static):
                try:
                    parts.append(str(child.render()))
                except Exception:
                    pass
            return "\n".join(parts)

        marker_i = next(
            (i for i, w in enumerate(kids) if "загружен" in _widget_text(w)),
            None,
        )
        assert marker_i is not None, "Маркер 'загружен' не найден в #trace"
        assert marker_i > reply_i


@pytest.mark.asyncio
async def test_marker_not_found_when_unavailable(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    import tools.skills.skill_delivery as sd
    import tools.skills.registry as sreg
    monkeypatch.setattr(sreg, "get_skills_registry", lambda cfg: object())
    monkeypatch.setattr(sd, "build_skill_delivery", lambda skill_id, reg: "WIRE")
    monkeypatch.setattr(sd, "skill_is_available", lambda skill_id, reg: False)

    class _Fake:
        def run(self, messages, **kw):
            import threading
            self._evt = threading.Event(); self._evt.wait(timeout=2.0)
        def stop_run(self):
            if getattr(self, "_evt", None): self._evt.set()
        def add_message(self, m): pass

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    app._executor_factory = lambda: _Fake()
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [{"id": "foo"}]
        scr._handle_command("skill foo сделай")
        await pilot.pause(0.3)
        joined = "\n".join(_trace_texts(scr))
        assert "не найден" in joined
        assert "загружен" not in joined
