"""VoiceInputFlow: переходы, min-duration, авто-стоп, ошибки. Без Textual/сети."""

from gc_tui.services.voice_input import (
    IDLE,
    MIN_SECONDS,
    RECORDING,
    TRANSCRIBING,
    VoiceInputFlow,
)
from gc_tui.voice.errors import VoiceError


class FakeRecorder:
    def __init__(self):
        self.elapsed = 0.0
        self.cancelled = False

    def start(self):
        pass

    @property
    def elapsed_seconds(self):
        return self.elapsed

    def stop(self):
        return b"pcm-data"

    def cancel(self):
        self.cancelled = True


def make_flow(rec, transcribe, cfg=None, events=None):
    events = events if events is not None else []
    flow = VoiceInputFlow(
        cfg or {"max_seconds": 120},
        recorder_factory=lambda: rec,
        transcribe=transcribe,
        ui_call=lambda fn, *a: fn(*a),  # синхронно: тесты детерминированы
        on_state=lambda s: events.append(("state", s)),
        on_text=lambda t: events.append(("text", t)),
        on_error=lambda e: events.append(("error", e)),
        spawn=lambda fn: fn(),  # фоновый поток → синхронный вызов
    )
    return flow, events


def test_toggle_starts_then_stops_and_inserts_text():
    rec = FakeRecorder()
    flow, events = make_flow(rec, lambda pcm, cfg: "привет")
    flow.toggle()
    assert flow.state == RECORDING
    rec.elapsed = 2.0
    flow.toggle()
    assert flow.state == IDLE
    assert ("text", "привет") in events
    assert ("state", TRANSCRIBING) in events


def test_short_recording_cancels_silently():
    rec = FakeRecorder()
    flow, events = make_flow(rec, lambda pcm, cfg: "не должно вызваться")
    flow.toggle()
    rec.elapsed = MIN_SECONDS / 2
    flow.toggle()
    assert flow.state == IDLE
    assert rec.cancelled
    assert not any(kind in ("text", "error") for kind, _ in events)


def test_escape_cancels_recording():
    rec = FakeRecorder()
    flow, events = make_flow(rec, lambda pcm, cfg: "x")
    flow.toggle()
    flow.cancel_recording()
    assert flow.state == IDLE
    assert rec.cancelled
    assert not any(kind == "text" for kind, _ in events)


def test_tick_autostops_at_max_seconds():
    rec = FakeRecorder()
    flow, events = make_flow(rec, lambda pcm, cfg: "долгий текст", cfg={"max_seconds": 10})
    flow.toggle()
    rec.elapsed = 5.0
    flow.tick()
    assert flow.state == RECORDING  # рано
    rec.elapsed = 10.1
    flow.tick()
    assert flow.state == IDLE  # авто-стоп → распозналось (sync)
    assert ("text", "долгий текст") in events


def test_transcribe_error_reported():
    rec = FakeRecorder()

    def failing(pcm, cfg):
        raise VoiceError("STT: HTTP 500")

    flow, events = make_flow(rec, failing)
    flow.toggle()
    rec.elapsed = 3.0
    flow.toggle()
    assert flow.state == IDLE
    assert ("error", "STT: HTTP 500") in events


def test_recorder_start_error_reported():
    class BrokenRecorder(FakeRecorder):
        def start(self):
            raise VoiceError("микрофон недоступен: нет устройства")

    flow, events = make_flow(BrokenRecorder(), lambda pcm, cfg: "x")
    flow.toggle()
    assert flow.state == IDLE
    assert any(kind == "error" for kind, _ in events)


def test_toggle_ignored_while_transcribing():
    rec = FakeRecorder()
    pending = []
    events = []
    flow = VoiceInputFlow(
        {"max_seconds": 120},
        recorder_factory=lambda: rec,
        transcribe=lambda pcm, cfg: "текст",
        ui_call=lambda fn, *a: fn(*a),
        on_state=lambda s: events.append(("state", s)),
        on_text=lambda t: events.append(("text", t)),
        on_error=lambda e: events.append(("error", e)),
        spawn=pending.append,  # worker откладывается — TRANSCRIBING живёт до ручного вызова
    )
    flow.toggle()
    rec.elapsed = 2.0
    flow.toggle()
    assert flow.state == TRANSCRIBING
    flow.toggle()  # игнор: ни новый старт, ни стоп
    assert flow.state == TRANSCRIBING
    assert not any(kind == "text" for kind, _ in events)
    pending[0]()  # выполнить отложенный worker
    assert flow.state == IDLE
    assert ("text", "текст") in events


def test_input_level_only_while_recording():
    rec = FakeRecorder()
    rec.level = 0.7
    flow, _ = make_flow(rec, lambda pcm, cfg: "x")
    assert flow.input_level == 0.0  # idle — тишина
    flow.toggle()
    assert flow.input_level == 0.7  # запись — живой уровень рекордера
    rec.elapsed = 2.0
    flow.toggle()
    assert flow.input_level == 0.0  # transcribing/idle — снова 0


def _settle(flow, frames=12):
    """Прогнать несколько кадров: EMA-дисплей сходится к целевым уровням.

    Кадр двигает ТОЛЬКО tick(); чтение input_bands состояние не меняет (иначе футер,
    который собирает пилюлю дважды за кадр — нарисовать и померить, — крутил бы
    анимацию с двойной скоростью)."""
    for _ in range(frames):
        flow.tick()
    return flow.input_bands


def test_reading_bands_does_not_advance_animation():
    """Инвариант: рендер только читает. Сколько раз ни спроси — картинка та же."""
    rec = FakeRecorder()
    rec.band_levels = (0.5, 0, 0, 0, 0, 0, 0)
    flow, _ = make_flow(rec, lambda pcm, cfg: "x")
    flow.toggle()
    flow.tick()
    first = flow.input_bands
    assert flow.input_bands == first  # второе чтение за тот же кадр
    assert flow.input_bands == first  # и третье
    flow.tick()
    assert flow.input_bands != first  # кадр сменился — картинка поехала


def test_input_bands_agc_normalizes_to_peak():
    rec = FakeRecorder()
    rec.level = 0.4
    flow, _ = make_flow(rec, lambda pcm, cfg: "x")
    assert flow.input_bands == (0.0,) * 7  # idle — тишина
    flow.toggle()
    assert all(abs(b - 1.0) < 0.02 for b in _settle(flow))  # АРУ: пик у потолка

    rec2 = FakeRecorder()  # свежий flow: чистый пик
    rec2.band_levels = (0.05, 0.1, 0.02, 0.0, 0.0, 0.0, 0.0)
    flow2, _ = make_flow(rec2, lambda pcm, cfg: "x")
    flow2.toggle()
    bands = _settle(flow2)
    assert abs(bands[1] - 1.0) < 0.02  # громчайшая полоса — максимум
    assert abs(bands[0] - 0.5) < 0.02  # остальные — относительно неё
    assert abs(bands[2] - 0.4) < 0.02  # сосед пика подсвечен (0.4×)
    assert all(b >= 0.009 for b in bands)  # живой сигнал: лента дышит вся


def test_input_bands_noise_gate_peak_decay_and_ema():
    rec = FakeRecorder()
    flow, _ = make_flow(rec, lambda pcm, cfg: "x")
    flow.toggle()
    rec.band_levels = (0.002,) * 7  # чистый шум ниже гейта
    assert _settle(flow) == (0.0,) * 7  # АРУ шум не «взлетает»
    rec.band_levels = (0.5, 0, 0, 0, 0, 0, 0)  # громкий пик
    loud = _settle(flow)[0]
    assert loud > 0.95
    rec.band_levels = (0.05, 0, 0, 0, 0, 0, 0)  # стало тихо
    flow.tick()  # один кадр
    assert flow.input_bands[0] < loud  # EMA: столбик «стекает», не прыгает
    # 120 кадров ≈ 9.6 с при кадре 0.08 с. Счёт в кадрах, а не в секундах: когда кадр
    # ускорили с 0.15 до 0.08, коэффициент затухания пика пересчитали (0.95 → 0.975),
    # чтобы темп в секундах не менялся, — а прежние 60 кадров стали вдвое меньшим сроком.
    assert _settle(flow, frames=120)[0] > 0.9  # АРУ адаптировался к тихому
