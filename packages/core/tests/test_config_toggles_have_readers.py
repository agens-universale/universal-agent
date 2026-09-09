"""Гейт: конфиг не обещает того, чего не делает.

``test_config_default_template`` следит, чтобы каждый ключ схемы был либо в
шаблоне, либо осознанно занесён в список экспертных. Но он не проверяет
главного: ЧИТАЕТ ли этот ключ хоть кто-нибудь. Ключ можно добавить в схему,
описать в ``demo_config.yml`` и не подключить — пользователь выставит его,
ничего не произойдёт, и узнать об этом неоткуда.

Такие ключи есть, и их тринадцать из ста шестидесяти трёх. Все, кроме двух,
описаны в demo-конфигах — то есть обещаны пользователю. Часть спроецирована в
плоский словарь ``config.py``, отчего выглядит подключённой.

Этот набор не требует их немедленно чинить: решение, дописать читателя или
убрать ключ из схемы, — не тестовое. Он требует другого: чтобы список НЕ РОС
молча. Новый неподключённый ключ покраснеет здесь в день своего появления, а
не через полгода в отчёте об инвентаризации.

Как метод отличает читателя. Ищется имя листа как отдельное слово в исходниках
вне схемы (``config_modules``) и вне проекции (``gc_core/config.py``): и то и
другое лишь ОБЪЯВЛЯЕТ ключ, а не пользуется им. Метод даёт ложноотрицательные
(ключ, прочитанный по вычисляемому имени, будет сочтён живым) и не даёт
ложноположительных — для гейта это правильный перекос: он не поднимает
ложных тревог.
"""
import re
from pathlib import Path

from pydantic import BaseModel

from gc_core.config_modules.root import AppConfig
from gc_testkit.paths import REPO

#: Ключи схемы, у которых сегодня нет ни одного читателя.
#:
#: Пополнять — только вместе с решением, что с ключом делать дальше. Пустая
#: строка в примечании значит «причина неизвестна»; такие и есть худший случай,
#: потому что пользователь их уже видит в demo-конфиге.
KNOWN_UNREAD: dict = {
    # --- forward-compat бинарного чтения: в v1 не enforce'ятся ---
    # Помечены как таковые и в security.py, и в списке экспертных ключей
    # test_config_default_template. Осознанное решение, не недосмотр.
    "security.read_guard.max_file_size_kb": "forward-compat, вне v1",
    "security.read_guard.binary_read_policy": "forward-compat, вне v1",
    "security.read_guard.binary_allowed_types": "forward-compat, вне v1",
    "security.read_guard.binary_allowed_extensions": "forward-compat, вне v1",

    # --- объявлено вне v1 ---
    "agent.finish_gate_use_rule_hints": "rule-layer без LLM, вне v1",

    # --- спроецированы в плоский словарь, но не читаются ---
    # Выглядят подключёнными: config.py кладёт их в config_dict рядом с
    # рабочими ключами. Описаны в demo-конфигах, то есть обещаны пользователю.
    "agent.client_name": "",
    "agent.model_steering": "",
    "agent.stored_tool_outputs_max_entries": "",
    "project_memory.enable_project_memory_notice": "",

    # --- не спроецированы и не читаются ---
    "todo.reminder_steps": "",
    "tui.stream_display": "",

    # --- не упомянуты нигде, даже в demo-конфигах ---
    "mcp.meta.recommended_mcp_manifest": "",
    "mcp.meta.tools_cache_ttl": "",
}


def _schema_leaves(model: type[BaseModel], prefix: str = "") -> list[str]:
    """Все листовые пути схемы в точечной записи."""
    out: list[str] = []
    for name, field in model.model_fields.items():
        annotation = field.annotation
        path = f"{prefix}{name}"
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            out.extend(_schema_leaves(annotation, path + "."))
        else:
            out.append(path)
    return out


def _source_text() -> str:
    """Исходники вне схемы и вне проекции — то есть места, где ключом ПОЛЬЗУЮТСЯ."""
    chunks: list[str] = []
    for path in (REPO / "packages").glob("*/src/**/*.py"):
        text = str(path)
        if "gc_core/config_modules/" in text or text.endswith("gc_core/config.py"):
            continue
        try:
            chunks.append(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
    return "\n".join(chunks)


def _unread_leaves() -> list[str]:
    """Ключи схемы, чьего имени нет ни в одном исходнике вне config_modules.

    Читателем считается и аксессор ``<ключ>_from_ctx``: `config_modules` держит
    его для ключей, которые нужны в местах без готового профиля (см.
    ``subagent_role_max_tokens_from_ctx``). Голый ``\b<ключ>\b`` такой вызов не
    видит — за именем идёт ``_``, — и подключённый ключ выглядел бы как
    забытый. Ровно этот ложный крик и получился при вливании веток.

    Суффиксов два, и второй добавлен по такому же ложному крику: у ключей,
    которые нужны там, где готового профиля нет, а плоский ``config_dict``
    есть, аксессор называется ``<ключ>_from_config_dict``
    (``exec_output_preview_edge_chars_from_config_dict``).
    """
    source = _source_text()
    return [
        leaf for leaf in _schema_leaves(AppConfig)
        if not re.search(
            r"\b" + re.escape(leaf.rsplit(".", 1)[-1])
            + r"(_from_ctx|_from_config_dict)?\b", source
        )
    ]


def test_no_new_unread_toggle_appears():
    """Список неподключённых ключей не вырос.

    Главный гейт файла. Новый ключ, добавленный в схему и не подключённый к
    коду, краснеет здесь сразу — а не всплывает через полгода при
    инвентаризации, когда его уже успели описать в документации.
    """
    appeared = sorted(set(_unread_leaves()) - set(KNOWN_UNREAD))
    assert appeared == [], (
        "в схеме появились ключи, которых никто не читает:\n  "
        + "\n  ".join(appeared)
        + "\nЛибо подключите их к коду, либо занесите в KNOWN_UNREAD вместе с "
        "решением,\nчто с ними делать. Молча оставлять нельзя: конфиг обещает "
        "пользователю то,\nчего не делает."
    )


def test_known_unread_list_does_not_go_stale():
    """В списке нет ключей, которые УЖЕ подключили.

    Обратная сторона: подключённый ключ обязан уйти из списка, иначе тот
    превращается в свалку и перестаёт что-либо значить.
    """
    unread = set(_unread_leaves())
    fixed = sorted(set(KNOWN_UNREAD) - unread)
    assert fixed == [], (
        "эти ключи уже читаются, но всё ещё числятся неподключёнными:\n  "
        + "\n  ".join(fixed)
        + "\nУберите их из KNOWN_UNREAD."
    )


def test_every_known_unread_key_exists_in_the_schema():
    """В списке нет ключей, которых в схеме уже нет.

    Иначе удалённый ключ остался бы висеть в списке и маскировал бы собой
    новый, случайно совпавший по имени.
    """
    leaves = set(_schema_leaves(AppConfig))
    ghosts = sorted(set(KNOWN_UNREAD) - leaves)
    assert ghosts == [], f"в KNOWN_UNREAD ключи, которых нет в схеме: {ghosts}"
