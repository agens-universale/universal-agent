"""Cross-package integration: remote TUI depends on the optional Temporal adapter."""


async def test_remote_tui_disconnect_does_not_stop_or_lock_files():
    from gc_tui.temporal_app import TemporalApp

    class Provider:
        closed = False
        sends = []

        async def status(self):
            return {"phase": "running", "turn_id": "turn-1"}

        async def history(self, after=0):
            return {"messages": [], "after": after}

        async def events(self, after=0):
            return []

        async def send(self, message_id, text):
            self.sends.append((message_id, text))

        async def stop(self, *_):
            raise AssertionError("Disconnect must not stop the workflow")

        async def aclose(self):
            self.closed = True

    provider = Provider()
    app = TemporalApp(provider)
    async with app.run_test() as pilot:
        await pilot.press("h", "i", "enter")
        await pilot.pause()
        assert provider.sends[0][1] == "hi"
    assert provider.closed
