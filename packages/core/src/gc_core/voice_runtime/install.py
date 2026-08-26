"""Оркестрация gc-voice install: клон пина → uv sync → CPU-only → модель → wire."""

from __future__ import annotations

import shutil
import subprocess
from typing import Optional

from gc_core.voice_runtime.fs import force_rmtree
from gc_core.voice_runtime.net import with_system_trust_retry
from gc_core.voice_runtime.paths import speaches_python, speaches_root
from gc_core.voice_runtime.state import (
    DEFAULT_MODEL, DEFAULT_PORT, SPEACHES_PIN, SPEACHES_PIN_SHA, SPEACHES_REPO,
    VoiceState, save_state,
)
from gc_core.voice_runtime.wire import managed_language, user_voice_conflict, wire_voice


class VoiceInstallError(RuntimeError):
    """Установка не удалась; message — человекочитаемый следующий шаг."""


def _run(runner, cmd, *, step: str):
    result = runner(cmd, capture_output=True, text=True)
    if getattr(result, "returncode", 1) != 0:
        detail = (getattr(result, "stderr", "") or "").strip()[-400:]
        raise VoiceInstallError(f"{step} завершился с ошибкой. {detail}")
    return result


def _model_info(model: str):
    import huggingface_hub

    return huggingface_hub.model_info(model)


def _download_model(model: str) -> None:
    from huggingface_hub import snapshot_download

    with_system_trust_retry(lambda: snapshot_download(repo_id=model))


_ASR_TASK = "automatic-speech-recognition"


def validate_model_card(model: str) -> Optional[str]:
    """None — модель пройдёт гейт speaches; иначе текст причины.

    Критерий тот же, что hf_model_filter speaches: library_name == "ctranslate2"
    и ASR-задача (pipeline_tag или тег). Иначе сервис ответит 404 на первой
    же транскрипции — ловим на install, а не в рантайме."""
    card = getattr(_model_info(model), "card_data", None)
    lib = getattr(card, "library_name", None) or getattr(card, "library", None)
    tags = set(getattr(card, "tags", None) or [])
    pipeline = getattr(card, "pipeline_tag", None)
    if lib == "ctranslate2" and (pipeline == _ASR_TASK or _ASR_TASK in tags):
        return None
    return (f"library_name={lib!r}, ASR-тег {'есть' if _ASR_TASK in tags else 'нет'} "
            "— нужна CTranslate2-карточка (library_name: ctranslate2)")


def _clone_mismatch(runner, git: str, root) -> Optional[str]:
    """Причина, по которой существующий клон нельзя брать как есть, или None.

    Без этой сверки повторный install после бампа SPEACHES_PIN молча оставлял на диске
    СТАРЫЙ код (каталог есть — значит «уже склонировано»), а в state.json писал НОВУЮ
    версию. `gc-voice status` и карточка /settings после этого хором показывали версию,
    которой на машине нет, — и разбор любой жалобы уходил не туда.

    Лечение у всех причин одно — переклон: репозиторий лёгкий (тяжёлая модель лежит
    отдельно, в общем HF-кэше, и её это не трогает), а переключение shallow-клона на
    другой тег тянет свои краевые случаи. Не смогли спросить git (битый .git, чужой
    владелец каталога) — тоже переклон: непроверяемый клон и есть повод его заменить,
    а объявить его годным «на всякий случай» значит вернуть ту же ложь о версии."""
    if not (root / "pyproject.toml").is_file():
        return "каталог неполный (нет pyproject.toml)"      # огрызок прошлой установки
    try:
        # safe.directory обязателен и не «на всякий случай»: клон, поставленный из
        # консоли с правами администратора, принадлежит BUILTIN\Администраторы, и git
        # отказывается его читать («detected dubious ownership»). Поймано живой
        # проверкой на реальной установке: сверка версии не срабатывала НИКОГДА, каждый
        # install уходил в переклон, а на занятом каталоге падал бы с «каталог занят».
        # Послабление точечное: только этот вызов, только чтение HEAD, и каталог наш —
        # мы его сами и клонировали.
        result = runner([git, "-c", f"safe.directory={root}",
                         "-C", str(root), "rev-parse", "HEAD"],
                        capture_output=True, text=True)
    except OSError as exc:
        return f"версию клона не прочитать ({exc})"
    if getattr(result, "returncode", 1) != 0:
        return "версию клона не прочитать (git rev-parse не отработал)"
    head = (getattr(result, "stdout", "") or "").strip()
    if head != SPEACHES_PIN_SHA:
        return f"клон не на {SPEACHES_PIN} (HEAD {head[:12] or '?'})"
    return None


def install(port: int = DEFAULT_PORT, model: str = DEFAULT_MODEL, *,
            base_raw: dict | None = None,
            runner=subprocess.run, echo=print) -> VoiceState:
    git = shutil.which("git")
    if git is None:
        raise VoiceInstallError("git не найден в PATH — установите git и повторите.")
    uv = shutil.which("uv")
    if uv is None:
        raise VoiceInstallError("uv не найден в PATH — установите uv (агент им ставится).")

    root = speaches_root()
    if root.is_dir():
        reason = _clone_mismatch(runner, git, root)
        if reason is not None:
            echo(f"[voice] {reason} — переклонирую {root}")
            if not force_rmtree(root):
                raise VoiceInstallError(
                    f"каталог {root} занят и не удаляется — сервис ещё работает? "
                    "Остановите его (gc-voice stop; форграунд serve — Ctrl+C) и повторите."
                )
    if not root.is_dir():
        echo(f"[voice] клонирую speaches {SPEACHES_PIN} → {root}")
        root.parent.mkdir(parents=True, exist_ok=True)
        _run(runner, [git, "clone", "--depth", "1", "--branch", SPEACHES_PIN,
                      SPEACHES_REPO, str(root)], step="git clone")
    else:
        echo(f"[voice] клон уже на {SPEACHES_PIN}: {root}")

    echo("[voice] uv sync (окружение speaches; uv сам поставит Python 3.12)")
    _run(runner, [uv, "sync", "--project", str(root), "--no-dev"],
         step="uv sync")

    # CPU-only: их deps тянут kokoro-onnx[gpu] → onnxruntime-gpu; заменяем на CPU.
    py = str(speaches_python())
    echo("[voice] нейтрализую GPU-зависимости (onnxruntime-gpu → onnxruntime)")
    runner([uv, "pip", "uninstall", "--python", py, "onnxruntime-gpu"],
           capture_output=True, text=True)   # может отсутствовать — не ошибка
    _run(runner, [uv, "pip", "install", "--python", py, "onnxruntime"],
         step="установка CPU onnxruntime")

    echo(f"[voice] проверяю модель {model} (карточка HF)")
    try:
        reason = validate_model_card(model)
    except Exception as exc:                     # оффлайн/зеркало — не блокируем
        echo(f"[voice] не удалось проверить карточку модели ({exc}) — продолжаю")
        reason = None
    if reason is not None:
        raise VoiceInstallError(
            f"модель {model} не поддерживается сервисом: {reason}. "
            "Рабочие примеры: Systran/faster-whisper-small | tiny | medium."
        )
    echo(f"[voice] скачиваю модель {model} (HF-кэш, ~0.5 ГБ)")
    try:
        _download_model(model)
    except Exception as exc:
        raise VoiceInstallError(f"скачивание модели завершилось с ошибкой: {exc}") from exc

    conflict = user_voice_conflict(base_raw or {}, port)
    wire_voice(port, model, managed_language())    # переустановка не сбрасывает язык
    state = VoiceState(version=SPEACHES_PIN, root=str(root), port=port, model=model)
    save_state(state)

    if conflict:
        echo(f"[voice] ВНИМАНИЕ: voice уже настроен на {conflict} в config.yml — "
             "локальный сервис перекроет его (config.local.yml главнее). "
             "Вернуть как было: gc-voice remove.")
    echo(f"[voice] готово: порт {port}, модель {model}. Запустите gc-ua — "
         "сервис поднимется сам.")
    return state
