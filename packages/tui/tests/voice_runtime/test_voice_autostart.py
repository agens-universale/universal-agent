"""Гейт автостарта, подъём через супервизор, lease, release-гасит-последний."""
from gc_tui.voice_runtime import autostart as auto
from gc_tui.voice_runtime.state import VoiceState

ST = VoiceState(version="v0.8.3", root="/data/voice/speaches",
                port=8735, model="m")
CFG_OURS = {"voice": {"url": "http://127.0.0.1:8735/v1/audio/transcriptions"}}
CFG_GROQ = {"voice": {"url": "https://api.groq.com/openai/v1/audio/transcriptions"}}


class FakeSupervisor:
    def __init__(self, pids_dir=None):
        self.started = []

    def start_if_needed(self, name, run, *, cwd=None, extra_env=None):
        self.started.append(name)
        return True


def _wire(monkeypatch, *, state=ST, port_ok=True, ready=True):
    monkeypatch.setattr(auto, "load_state", lambda: state)
    monkeypatch.setattr(auto, "port_alive", lambda st: port_ok)
    monkeypatch.setattr(auto, "wait_daemon_url_ready", lambda url, **kw: ready)
    monkeypatch.setattr(auto, "preheat", lambda st: True)
    acquired = []
    monkeypatch.setattr(auto, "acquire", lambda pid: acquired.append(pid))
    # Пометка владельца пишет в voice_pids_dir() — в тестах на реальный каталог не ходим.
    monkeypatch.setattr(auto, "mark_agent_owned", lambda name: None)
    return acquired


def test_url_is_ours_parses_address_not_substrings():
    """Гейт разбирает адрес, а не сканирует строку. Подстрочная проверка промахивалась
    в обе стороны: адрес без пути и IPv6-форма считались чужими (автостарт молчал,
    /settings показывал «облако» для своей же машины), а хост, в имени которого просто
    встречается «localhost», — своим."""
    ours = auto.url_is_ours
    assert ours(CFG_OURS, 8735) is True
    assert ours({"voice": {"url": "http://127.0.0.1:8735"}}, 8735) is True      # без пути
    assert ours({"voice": {"url": "http://localhost:8735/v1/x"}}, 8735) is True
    assert ours({"voice": {"url": "http://[::1]:8735/v1/x"}}, 8735) is True     # IPv6
    assert ours({"voice": {"url": "http://localhost.evil.example:8735/v1/x"}},
                8735) is False                                                 # чужой хост
    assert ours({"voice": {"url": "http://127.0.0.1:9999/v1/x"}}, 8735) is False  # чужой порт
    assert ours(CFG_GROQ, 8735) is False
    assert ours({}, 8735) is False
    assert ours({"voice": {"url": "http://127.0.0.1:notaport/x"}}, 8735) is False


def test_gate_no_state(monkeypatch):
    _wire(monkeypatch, state=None)
    assert auto.ensure_voice_service(CFG_OURS, supervisor_factory=FakeSupervisor,
                                     preheat_async=False) is False


def test_gate_foreign_url(monkeypatch):
    acquired = _wire(monkeypatch)
    assert auto.ensure_voice_service(CFG_GROQ, supervisor_factory=FakeSupervisor,
                                     preheat_async=False) is False
    assert acquired == []          # чужой провайдер — мы вообще ни при чём


def test_port_alive_only_lease_and_preheat(monkeypatch):
    acquired = _wire(monkeypatch, port_ok=True)
    sup = FakeSupervisor()
    ok = auto.ensure_voice_service(CFG_OURS, supervisor_factory=lambda **kw: sup,
                                   preheat_async=False)
    assert ok is True
    assert sup.started == []       # уже жив — не поднимаем
    assert len(acquired) == 1


def test_spawn_when_port_silent(monkeypatch):
    acquired = _wire(monkeypatch, port_ok=False, ready=True)
    sup = FakeSupervisor()
    ok = auto.ensure_voice_service(CFG_OURS, supervisor_factory=lambda **kw: sup,
                                   preheat_async=False)
    assert ok is True
    assert sup.started == ["speaches"]
    assert len(acquired) == 1


def test_spawn_fail_returns_false(monkeypatch):
    _wire(monkeypatch, port_ok=False, ready=False)   # порт так и не ожил
    ok = auto.ensure_voice_service(CFG_OURS, supervisor_factory=FakeSupervisor,
                                   preheat_async=False)
    assert ok is False


def test_wait_uses_own_timeout_not_mcp_default(monkeypatch):
    """Голос ждёт готовности по своему бюджету (_VOICE_READY_TIMEOUT_SEC), а не по
    дефолту recommended_mcp.autostart, рассчитанному на мгновенные MCP-демоны."""
    _wire(monkeypatch, port_ok=False, ready=True)
    captured = {}

    def fake_wait(url, **kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(auto, "wait_daemon_url_ready", fake_wait)
    sup = FakeSupervisor()
    ok = auto.ensure_voice_service(CFG_OURS, supervisor_factory=lambda **kw: sup,
                                   preheat_async=False)
    assert ok is True
    assert captured.get("timeout") == auto._VOICE_READY_TIMEOUT_SEC


def test_race_port_alive_after_timeout_is_success(monkeypatch):
    """Ожидание истекло, но порт к моменту перепроверки уже ожил (гонка холодного
    старта) — вердикт обязан быть успехом со всеми последствиями: lease и прогрев."""
    calls = {"n": 0}

    def port_alive_seq(state):
        calls["n"] += 1
        return calls["n"] > 1   # мёртв до спавна, жив к моменту перепроверки после таймаута

    monkeypatch.setattr(auto, "load_state", lambda: ST)
    monkeypatch.setattr(auto, "port_alive", port_alive_seq)
    monkeypatch.setattr(auto, "wait_daemon_url_ready", lambda url, **kw: False)
    monkeypatch.setattr(auto, "preheat", lambda st: True)
    acquired = []
    monkeypatch.setattr(auto, "acquire", lambda pid: acquired.append(pid))
    # Пометка владельца пишет в voice_pids_dir() — без подмены этот тест лез бы в
    # ЖИВОЙ ~/.gc машины (моки тут собраны вручную, мимо _wire).
    marked = []
    monkeypatch.setattr(auto, "mark_agent_owned", lambda name: marked.append(name))
    sup = FakeSupervisor()
    ok = auto.ensure_voice_service(CFG_OURS, supervisor_factory=lambda **kw: sup,
                                   preheat_async=False)
    assert ok is True
    assert sup.started == ["speaches"]
    assert len(acquired) == 1
    assert marked == ["speaches"]      # спавнили мы — сервис наш, гасить его можно


def test_spawn_marks_service_as_ours(monkeypatch):
    """Спавнили мы → пометка владельца. Только помеченный сервис light-switch гасит."""
    _wire(monkeypatch, port_ok=False, ready=True)
    marked = []
    monkeypatch.setattr(auto, "mark_agent_owned", lambda name: marked.append(name))
    auto.ensure_voice_service(CFG_OURS, supervisor_factory=FakeSupervisor,
                              preheat_async=False)
    assert marked == ["speaches"]


def test_alive_service_is_not_claimed(monkeypatch):
    """Порт уже отвечает — сервис поднял кто-то другой (человек или прошлый TUI).
    Пользоваться им можно (lease), приписывать себе — нет."""
    _wire(monkeypatch, port_ok=True)
    marked = []
    monkeypatch.setattr(auto, "mark_agent_owned", lambda name: marked.append(name))
    auto.ensure_voice_service(CFG_OURS, supervisor_factory=FakeSupervisor,
                              preheat_async=False)
    assert marked == []


def test_not_claimed_when_supervisor_did_not_spawn(monkeypatch):
    """Порт молчит, но pid-файл принадлежит живому процессу — это чужой форграундный
    `gc-voice serve` на взлёте. start_if_needed вернёт False, и сервис остаётся чужим."""
    class BusySupervisor(FakeSupervisor):
        def start_if_needed(self, name, run, *, cwd=None, extra_env=None):
            return False

    _wire(monkeypatch, port_ok=False, ready=True)
    marked = []
    monkeypatch.setattr(auto, "mark_agent_owned", lambda name: marked.append(name))
    auto.ensure_voice_service(CFG_OURS, supervisor_factory=BusySupervisor,
                              preheat_async=False)
    assert marked == []


def test_late_service_stays_ours(monkeypatch):
    """Готовности не дождались, вердикт False — но спавнили МЫ, значит «опоздавший»
    сервис наш: пометка стоит до ожидания, иначе он висел бы вечно (гасить некому)."""
    _wire(monkeypatch, port_ok=False, ready=False)
    marked = []
    monkeypatch.setattr(auto, "mark_agent_owned", lambda name: marked.append(name))
    assert auto.ensure_voice_service(CFG_OURS, supervisor_factory=FakeSupervisor,
                                     preheat_async=False) is False
    assert marked == ["speaches"]


def test_release_last_stops_service(monkeypatch):
    monkeypatch.setattr(auto, "release", lambda pid, **kw: True)   # мы последние
    monkeypatch.setattr(auto, "is_agent_owned", lambda name: True)  # сервис наш
    monkeypatch.setattr(auto, "read_pid", lambda name, pids_dir=None: 4242)
    monkeypatch.setattr(auto, "is_pid_alive", lambda pid: True)
    killed = []
    cleared = []
    monkeypatch.setattr(auto, "terminate_pid", lambda pid, **kw: killed.append(pid))
    monkeypatch.setattr(auto, "remove_pid", lambda name, pids_dir=None: None)
    monkeypatch.setattr(auto, "clear_agent_owned", lambda name: cleared.append(name))
    # Без этой подмены тест читал НАСТОЯЩИЙ каталог lease на машине разработчика:
    # release_voice_service перечитывает его перед самым гашением (autostart.py:114).
    # Стоило держать открытым живой gc-ua — и его lease делал ветку «кто-то стартовал,
    # пока мы шли гасить», terminate_pid не вызывался, тест падал. В одиночку и в
    # наборе TUI проходил, в полном прогоне мигал: зависимость не от порядка тестов,
    # а от состояния машины. Сосед test_release_rereads_leases_right_before_killing
    # подменял live_leases с самого начала — здесь просто забыли.
    monkeypatch.setattr(auto, "live_leases", lambda: [])   # других клиентов нет
    auto.release_voice_service()
    assert killed == [4242]
    assert cleared == ["speaches"]                 # пометка уходит вместе с сервисом


def test_release_never_kills_foreign_service(monkeypatch):
    """Ключевой инвариант: TUI выходит, своего lease у него нет (голосом не пользовался),
    release честно говорит «живых клиентов нет» — но сервис поднял человек
    (`gc-voice serve`), пометки владельца нет. Чужой процесс не трогаем."""
    monkeypatch.setattr(auto, "release", lambda pid, **kw: True)
    monkeypatch.setattr(auto, "is_agent_owned", lambda name: False)
    monkeypatch.setattr(auto, "read_pid", lambda name, pids_dir=None: 4242)
    monkeypatch.setattr(auto, "is_pid_alive", lambda pid: True)
    killed = []
    monkeypatch.setattr(auto, "terminate_pid", lambda pid, **kw: killed.append(pid))
    auto.release_voice_service()
    assert killed == []


def test_release_rereads_leases_right_before_killing(monkeypatch):
    """Между «я последний» и гашением успел стартовать другой TUI — сервис не трогаем.

    Находка ревью коллеги: выходящий снимал свой lease, пересканировал каталог и шёл
    гасить; параллельно стартующее окно видело живой порт, подъём пропускало и клало
    свой lease — и оставалось с мёртвым сервисом. Перечитываем каталог перед самым
    `terminate_pid`: окно сужается до пары инструкций (полностью убрать его без
    файловой блокировки нельзя)."""
    monkeypatch.setattr(auto, "release", lambda pid, **kw: True)    # на момент решения — последние
    monkeypatch.setattr(auto, "is_agent_owned", lambda name: True)
    monkeypatch.setattr(auto, "read_pid", lambda name, pids_dir=None: 4242)
    monkeypatch.setattr(auto, "is_pid_alive", lambda pid: True)
    monkeypatch.setattr(auto, "live_leases", lambda: [777])          # новичок успел
    killed, cleared = [], []
    monkeypatch.setattr(auto, "terminate_pid", lambda pid, **kw: killed.append(pid))
    monkeypatch.setattr(auto, "remove_pid", lambda name, pids_dir=None: None)
    monkeypatch.setattr(auto, "clear_agent_owned", lambda name: cleared.append(name))

    auto.release_voice_service()

    assert killed == [], "сервис погашен под живым клиентом"
    assert cleared == [], "пометка владельца снята — сервис перестал быть нашим"


def test_release_not_last_keeps_service(monkeypatch):
    monkeypatch.setattr(auto, "release", lambda pid, **kw: False)  # есть ещё живые
    killed = []
    monkeypatch.setattr(auto, "terminate_pid", lambda pid, **kw: killed.append(pid))
    auto.release_voice_service()
    assert killed == []
