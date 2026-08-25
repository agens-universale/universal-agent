"""State-файл установки voice-батарейки — источник истины для автостарта/status."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from voice_runtime.paths import voice_state_path

SPEACHES_REPO = "https://github.com/speaches-ai/speaches.git"
SPEACHES_PIN = "v0.8.3"
# Коммит, на который указывает тег пина. Не украшение: по нему `install` решает, на
# той ли версии стоит уже существующий клон (тег на стороне upstream могут и
# передвинуть). БУДЕТЕ БАМПИТЬ SPEACHES_PIN — обязательно бампните и SHA: иначе
# сверка не сойдётся никогда и каждый install будет переклонировать заново.
SPEACHES_PIN_SHA = "c78b77d5ce56f8c1042f6bc11209697cca2b2445"
DEFAULT_PORT = 8735
DEFAULT_MODEL = "Systran/faster-whisper-small"


@dataclass(frozen=True)
class VoiceState:
    version: str
    root: str
    port: int
    model: str


def load_state() -> VoiceState | None:
    path = voice_state_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return VoiceState(version=str(data["version"]), root=str(data["root"]),
                          port=int(data["port"]), model=str(data["model"]))
    except (ValueError, KeyError, TypeError):
        return None


def available_update(state: VoiceState | None) -> str | None:
    """Версия, до которой можно обновить установленный движок, или None (нечего).

    Правило живёт здесь, рядом с пином и состоянием, а не в TUI: сверку читают и
    экраны (`voice_setup.engine_update`), и `gc-voice status` — рантайму нельзя тянуть
    в себя интерфейс. Чистая функция от уже прочитанного состояния: тем, у кого оно на
    руках, второй раз читать файл незачем."""
    if state is None or state.version == SPEACHES_PIN:
        return None
    return SPEACHES_PIN


def current_model_short() -> str:
    """Короткое имя модели голосовой батарейки для UI: state.model, если сервис
    установлен, иначе DEFAULT_MODEL; часть после последнего "/"."""
    st = load_state()
    model = st.model if st is not None else DEFAULT_MODEL
    return model.rsplit("/", 1)[-1]


def save_state(state: VoiceState) -> None:
    path = voice_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2),
                    encoding="utf-8")


def clear_state() -> None:
    path = voice_state_path()
    if path.is_file():
        path.unlink()
