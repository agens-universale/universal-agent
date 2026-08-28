"""Каждый Input с id обязан быть перечислен в CSS — иначе Textual рисует дефолтную
рамку глифами, которых нет в консольных шрифтах (баг поля «Прокси», смок 2026-08-02)."""
from pathlib import Path

_STYLES = Path(__file__).resolve().parents[2] / "src" / "tui" / "styles.tcss"

# id полей, которые обязаны иметь и плоский, и framed-стиль
_FRAMED_FIELD_IDS = ("conn-url", "conn-token", "conn-proxy", "onb-model",
                     "onb-level-model", "mef-alias", "mef-model", "mef-url",
                     "mef-token", "set-profile-name",
                     "voice-url", "voice-token", "voice-model", "voice-lang")


def _css() -> str:
    return _STYLES.read_text(encoding="utf-8")


def test_every_framed_field_has_flat_rule():
    """Плоское правило: без него Input несёт дефолтную tall-рамку."""
    css = _css()
    flat = [ln for ln in css.splitlines()
            if "border: none" in ln and "height: 1" in ln]
    joined = " ".join(flat)
    missing = [fid for fid in _FRAMED_FIELD_IDS if f"#{fid}" not in joined]
    assert missing == [], f"нет плоского правила у: {missing}"


def test_every_framed_field_has_framed_rule():
    """Framed-правило: рамка round $run-color вместо дефолтной."""
    css = _css()
    framed = [ln for ln in css.splitlines() if ".set-framed" in ln]
    joined = " ".join(framed)
    missing = [fid for fid in _FRAMED_FIELD_IDS if f"#{fid}.set-framed" not in joined]
    assert missing == [], f"нет framed-правила у: {missing}"


def test_disabled_token_border_is_dimmed():
    """«Без авторизации» замораживает поле токена — рамка приглушается до $deep-dim,
    иначе замороженное поле читается как активное.

    Полей токена столько же, сколько форм с галкой «без авторизации», и сторож
    обходит их СПИСКОМ: правило, записанное для «Подключения», молча не
    распространяется на форму записи реестра — её пропажу прежний сторож не видел."""
    css = _css()
    for field in ("conn-token", "mef-token"):
        line = next((ln for ln in css.splitlines()
                     if f"#{field}.set-framed:disabled" in ln), None)
        assert line is not None, f"нет правила для замороженного #{field}"
        assert "$deep-dim" in line, f"рамка #{field} не приглушена: {line}"


def test_dropdown_scroll_rule_exists():
    """Потолок высоты выпадающих списков. Правило принесла голосовая фича, в develop
    его нет — при разносе на ветки оно обязано уехать в ux-polish, иначе список
    моделей там раскатает страницу (§3.4 спеки). Тест держит его на месте."""
    assert ".dropdown-scroll" in _css()
    assert "max-height: 14" in _css()


def test_auth_nested_is_gone_everywhere():
    """Класс ушёл вместе с радио-блоком авторизации. Проверяем И css, И применения:
    правило без применений — мёртвый код, применение без правила — потерянный отступ
    (так и вышло, когда правило удалили раньше времени).

    Обход файлов — средствами Python (Path.rglob), не внешним `grep`: на Windows
    (основная платформа проекта) его может не быть в PATH — тест не падал бы, а
    ERROR'ил; а с capture_output=True без check=True сломанный вызов дал бы пустой
    stdout и ассерт молча проходил бы вхолостую, на сломанном коде."""
    assert ".auth-nested" not in _css()
    src = Path(__file__).resolve().parents[2] / "src"
    hits = []
    for path in src.rglob("*"):
        if not path.is_file() or path.suffix in {".pyc", ".bak"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if "auth-nested" in line:
                hits.append(f"{path}:{i}:{line}")
    assert hits == [], "класс всё ещё применяется:\n" + "\n".join(hits)
