"""Сервис «Обновить config.yml до текущей версии» (спека A, A5).

Скелет = свежий config.default.yml (полный, с комментами). Значения юзера
переносятся line-copy по dotted-путям (path-aware, форматирование юзера
сохраняется). Старый файл — в бэкап. Только явный вызов; личные комментарии
юзера не переносятся (остаются в бэкапе) — механика «шаблон-скелет + бэкап».

Секции юзера, отсутствующие в шаблоне (напр. working_memory, agent_auth и др.),
переносятся verbatim-блоком при условии, что поддерево валидно по схеме
(AppConfig.model_validate). Невалидное — в unknown.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, List, NamedTuple, Optional

import yaml

from gc_core.config import _default_config_path, validate_config_text
from gc_core.config_modules import compat
from gc_core.onboarding_config_writer import find_anchor, insert_missing, section_end
from gc_core.utils.atomic_io import atomic_replace
from gc_core.utils.user_text import read_user_text


class ResetReport(NamedTuple):
    backup_path: str        # старый файл, в бэкап


class SyncReport(NamedTuple):
    transferred: int        # перенесено значений/блоков юзера
    renamed: int            # из них — под новым именем (RENAMES)
    removed: List[str]      # устаревшие ключи (REMOVED) — только в бэкапе
    unknown: List[str]      # неизвестные ключи — только в бэкапе
    reenter: List[str]      # значение не переносимо (конвертер) — задать заново
    backup_path: str


def _user_paths(node: Any, prefix: str = "") -> List[str]:
    """Dotted-пути значений юзера. В непустые dict рекурсируем; скаляр/список/
    пустой dict — лист."""
    out: List[str] = []
    if isinstance(node, dict) and node:
        for key, value in node.items():
            p = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict) and value:
                out.extend(_user_paths(value, p))
            else:
                out.append(p)
    return out


def _transfer(dst_text: str, src_text: str, src_key: str, dst_key: str) -> str:
    """Заменить в dst блок dst_key строками юзера (блок src_key из src).

    Line-copy: сохраняет форматирование юзера; при rename первая строка
    получает новое имя ключа. KeyError, если якоря нет в dst."""
    s_start, s_end, _s_indent, _ = find_anchor(src_text, src_key)
    s_block_end = section_end(src_text, s_end, _s_indent)
    d_start, d_end, d_indent, _ = find_anchor(dst_text, dst_key)
    d_block_end = section_end(dst_text, d_end, d_indent)
    block = src_text[s_start:s_block_end]
    if src_key.split(".")[-1] != dst_key.split(".")[-1]:
        first, sep, rest = block.partition("\n")
        old_leaf = src_key.split(".")[-1]
        new_leaf = dst_key.split(".")[-1]
        first = first.replace(f"{old_leaf}:", f"{new_leaf}:", 1)
        block = first + sep + rest
    return dst_text[:d_start] + block + dst_text[d_block_end:]


def _restore_template_keys(result: str, template_text: str, template_raw: Any,
                           user_paths: set, ancestor: str) -> str:
    """Вернуть в перенесённую секцию ключи шаблона, которых у юзера нет.

    Collapse-перенос предка (см. sync_config) кладёт блок юзера ПОВЕРХ блока шаблона:
    вместе с ним пропали бы новые ключи шаблона внутри той же секции. Без этого файл
    после успешного sync снова требует обновления — needs_sync остаётся True навсегда,
    и агент предлагает обновить конфиг на каждом запуске, плодя бэкапы.
    """
    for path in _user_paths(template_raw):
        if path != ancestor and not path.startswith(ancestor + "."):
            continue
        if any(up == path or up.startswith(path + ".") for up in user_paths):
            continue                     # юзер этот путь задал (сам или потомком) — уже перенесён
        try:
            result = insert_missing(result, path, template_text)
        except KeyError:
            continue                     # якорь в шаблоне не нашёлся — sync из-за этого не роняем
    return result


def needs_sync(user_text: str, template_text: str) -> bool:
    """Есть ли файлу что обновлять: новые ключи шаблона, renames или removed.

    Путь шаблона считается «отсутствующим» у юзера только если ни один
    user-path не равен ему и не начинается с него+«.» (т.е. юзер с
    непустым chat_completion_kwargs не требует обновления по этому пути).
    """
    try:
        user_raw = yaml.safe_load(user_text) or {}
        tmpl_raw = yaml.safe_load(template_text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"невалидный YAML: {exc}") from exc
    user_paths = set(_user_paths(user_raw))
    tmpl_paths = set(_user_paths(tmpl_raw))
    # Шаблонный путь «новый», только если юзер не покрывает его сам или
    # его потомком (chat_completion_kwargs: {} vs {temperature: 0.2}).
    new_in_tmpl = {
        tp for tp in tmpl_paths
        if not any(up == tp or up.startswith(tp + ".") for up in user_paths)
    }
    if new_in_tmpl:
        return True
    legacy = set(compat.RENAMES) | set(compat.REMOVED)
    return bool(legacy & user_paths)


def _validate_subtree(top_key: str, subtree: Any) -> bool:
    """Проверить поддерево юзера {top_key: subtree} через AppConfig.

    Применяет apply_compat и model_validate на изолированном dict.
    Возвращает True если схема принимает такую конфигурацию.
    """
    from gc_core.config_modules.root import AppConfig
    probe = {top_key: subtree}
    compat.apply_compat(probe)
    try:
        AppConfig.model_validate(probe)
        return True
    except Exception:
        return False


def _verbatim_block(user_text: str, top_key: str) -> Optional[str]:
    """Извлечь сырой текстовый блок top_key из user_text (включая заголовок).

    Возвращает None если ключ не найден.
    """
    try:
        start, end, indent, _ = find_anchor(user_text, top_key)
        block_end = section_end(user_text, end, indent)
        return user_text[start:block_end]
    except KeyError:
        return None


def prune_backups(backup_dir: Path, name: str, *, keep: int = 10) -> None:
    """Оставить последние `keep` бэкапов `<name>.bak-*`, старые удалить (bounded рост).

    Публичная: конвенцию бэкапов держит этот модуль, и правило роста — её
    часть. Зовут и сброс конфига, и удаление профиля из /settings; своя
    редакция у второго и приводила к каталогу без предела."""
    baks = sorted(backup_dir.glob(name + ".bak-*"))
    for old in baks[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass


def reset_config(path, template_path=None, *, timestamp: Optional[str] = None) -> ResetReport:
    """«Сбросить к умолчанию» (спека §11): подменить `path` актуальным пакетным
    `config.default.yml` целиком (не line-copy, как `sync_config`) — старый файл
    уходит в бэкап той же конвенцией (`backups/<name>.bak-<ts>`, prune keep=10).

    Файлы профилей и сессионные оверрайды это действие не трогает — оно
    заменяет ровно один файл (`path`)."""
    path = Path(path)
    template_path = Path(template_path or _default_config_path())
    # Тем же переводом, что и `sync_config`: вызывающий («Сбросить к умолчанию»
    # в /settings) ловит `ValueError`. Голый `read_text` отдавал ему `OSError`
    # трейсбеком, а на шаблоне в чужой кодировке (корп-сборка, правленный ассет) —
    # `UnicodeDecodeError` без имени файла.
    template_text = read_user_text(template_path)
    validate_config_text(template_text)

    ts = timestamp or time.strftime("%Y%m%d-%H%M%S")
    backup_dir = path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / (path.name + f".bak-{ts}")
    shutil.copyfile(path, backup)
    prune_backups(backup_dir, path.name)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(template_text, encoding="utf-8")
        atomic_replace(str(tmp), str(path))
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return ResetReport(str(backup))


def sync_config(path, template_path=None, *, timestamp: Optional[str] = None) -> SyncReport:
    path = Path(path)
    template_path = Path(template_path or _default_config_path())
    # Оба чтения — под общим переводом в `ValueError`: вызывающий (`gc-cli
    # --sync-config`) ловит именно его и печатает «config.yml НЕ обновлён: …».
    # `OSError` мимо этого перехвата уходил трейсбеком, а `UnicodeDecodeError`
    # хоть и подкласс `ValueError`, но без имени файла: на конфиге в чужой
    # кодировке человек читал голое «codec can't decode» и не знал, о чём речь.
    user_text = read_user_text(path)
    template_text = read_user_text(template_path)
    try:
        user_raw = yaml.safe_load(user_text) or {}
        template_raw = yaml.safe_load(template_text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"невалидный YAML: {exc}") from exc
    if not isinstance(user_raw, dict):
        raise ValueError("config.yml должен быть YAML-объектом")

    all_user_paths = set(_user_paths(user_raw))
    result = template_text
    transferred = renamed = 0
    removed: List[str] = []
    unknown: List[str] = []
    reenter: List[str] = []
    done: List[str] = []   # dotted-пути уже перенесённых блоков
    # verbatim-блоки секций вне шаблона — добавляются в конец результата
    extra_blocks: List[str] = []

    def _covered(p: str) -> bool:
        return any(p == d or p.startswith(d + ".") for d in done)

    for dotted in _user_paths(user_raw):
        if _covered(dotted):
            continue
        if dotted in compat.REMOVED:
            removed.append(dotted)
            continue
        target = compat.RENAMES.get(dotted, dotted)
        if dotted in compat.RENAME_CONVERTERS:
            reenter.append(dotted)   # line-copy не умеет преобразовать значение
            continue
        try:
            result = _transfer(result, user_text, dotted, target)
            transferred += 1
            if target != dotted:
                renamed += 1
            done.append(dotted)  # исходный путь: дети старого ключа считаются покрытыми
            continue
        except KeyError:
            pass
        # Нет прямого якоря в шаблоне. Поднимаемся по предкам (multi-level walk):
        # ищем ближайшего предка с якорем в шаблоне.
        parts = dotted.split(".")
        parent_transferred = False
        for depth in range(len(parts) - 1, 0, -1):
            ancestor = ".".join(parts[:depth])
            if _covered(ancestor) or ancestor in compat.RENAMES:
                parent_transferred = True
                break
            try:
                result = _transfer(result, user_text, ancestor, ancestor)
                transferred += 1
                done.append(ancestor)
                # Блок юзера лёг ПОВЕРХ блока шаблона — вернём ключи шаблона,
                # которых у юзера нет (иначе они молча теряются, см. _restore_template_keys).
                result = _restore_template_keys(result, template_text, template_raw,
                                                all_user_paths, ancestor)
                parent_transferred = True
                break
            except KeyError:
                continue
        if parent_transferred:
            continue

        # Якоря нет ни на одном уровне — top-level секция юзера отсутствует
        # в шаблоне (напр. working_memory, agent_auth, chat_completions и др.).
        # Переносим verbatim-блок если схема его принимает.
        top_key = parts[0]
        if _covered(top_key):
            continue
        subtree = user_raw.get(top_key)
        if not _covered(top_key) and _validate_subtree(top_key, subtree):
            block = _verbatim_block(user_text, top_key)
            if block is not None:
                extra_blocks.append(block)
                transferred += 1
                done.append(top_key)
                continue
        unknown.append(dotted)

    if extra_blocks:
        if not result.endswith("\n"):
            result += "\n"
        result += "\n" + "\n".join(extra_blocks)
        if not result.endswith("\n"):
            result += "\n"

    validate_config_text(result)

    ts = timestamp or time.strftime("%Y%m%d-%H%M%S")
    backup_dir = path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / (path.name + f".bak-{ts}")
    shutil.copyfile(path, backup)
    prune_backups(backup_dir, path.name)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(result, encoding="utf-8")
        atomic_replace(str(tmp), str(path))
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return SyncReport(transferred, renamed, removed, unknown, reenter, str(backup))
