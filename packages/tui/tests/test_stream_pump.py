from gc_tui.services.stream_pump import StreamPump, STREAM_REFRESH_MS


class _Sink:
    def __init__(self):
        self.pushed = []

    def push(self, kind, text):
        self.pushed.append((kind, text))


def test_pushes_every_delta_but_throttles_signal():
    t = [100.0]
    sink = _Sink()
    sigs = []
    pump = StreamPump(sink, lambda: sigs.append(t[0]),
                      throttle_s=0.08, clock=lambda: t[0])
    pump.on_delta("answer", "a")     # first → signal (leading-edge)
    pump.on_delta("answer", "b")     # same time → throttled
    t[0] = 100.05
    pump.on_delta("answer", "c")     # +50ms < 80ms → throttled
    t[0] = 100.20
    pump.on_delta("answer", "d")     # +200ms since last signal → signal
    assert sink.pushed == [("answer", "a"), ("answer", "b"),
                           ("answer", "c"), ("answer", "d")]
    assert len(sigs) == 2            # 4 deltas, 2 signals


def test_signal_exception_does_not_propagate():
    pump = StreamPump(_Sink(),
                      lambda: (_ for _ in ()).throw(RuntimeError("boom")),
                      throttle_s=0.0, clock=lambda: 1.0)
    pump.on_delta("answer", "x")     # must not raise


def test_default_constant_is_350ms():
    assert STREAM_REFRESH_MS == 350
