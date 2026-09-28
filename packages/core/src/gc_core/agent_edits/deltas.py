"""Итог по файлу: снимок «до» против того, что лежит на диске сейчас.

Счётчики отдельных записей журнала отвечают на вопрос «сколько работы
проделано»: агент правил файл fs-тулом, потом дважды переписал его целиком
через шелл — записи сложатся в «+293 −22» на файле в 133 строки. Читают же эти
цифры как «насколько изменился файл», а это другое число: разница между
состоянием до первой правки и текущим содержимым. Здесь живёт второе.

Расчёт общий для всех потребителей: сводка хода в статусной строке
(``tui.services.edits_lookup``) и экран обзора правок. Источник базы задаёт
вызывающий — ``SnapshotStore.counting_turn_baseline`` для «за ход»,
``SnapshotStore.counting_session_baseline`` для «за сессию»: для СЧЁТА годятся
обе породы баз, и точный снимок fs-тула, и счётная база шелл-канала. Точные
снимки (``turn_baseline``/``session_baseline``) отдельно спрашивает откат — им
подаётся предикат ``revertable_for``, а не эта база.

Слой данных: без UI. Дифф считает тот же ``build_diff``, что и рекордер
(единственная реализация на оба канала), контент декодируется тем же
best-effort способом — снимки хранятся байтами.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional, Tuple

from gc_core.agent_edits.journal import EditRecord
from gc_core.agent_edits.paths import is_external_key
from gc_core.config_modules.agent_edits import (
    DEFAULT_HUNK_CONTEXT_LINES,
    DEFAULT_MAX_DIFF_SOURCE_BYTES,
)
from gc_core.utils.file_text import (
    BINARY_SAMPLE_BYTES,
    decode_text_best_effort,
    sha256_hex_of_bytes,
    split_lines_lf,
)

# Байты, которые может дать кириллический текст cp1251: ASCII плюс сама
# кириллица — буквы (0xC0–0xFF) и Ё/ё (0xA8/0xB8). Проверка идёт вычитанием
# (``bytes.translate``), а не циклом по байтам: сюда доезжают файлы целиком, до
# ``max_diff_source_bytes``.
_CP1251_TEXT_BYTES = bytes(range(0x80)) + bytes(
    sorted({0xA8, 0xB8, *range(0xC0, 0x100)})
)


def decode_edit_text(data: bytes) -> str | None:
    """Байты правки → текст для диффа, или ``None`` — «это не текст» (Д2).

    Общий примитив ``decode_text_best_effort`` (BOM, utf-16, строгий utf-8,
    догадка chardet в пределах allowlist) идёт ПЕРВЫМ и решает всё, что решает
    сегодня; менять его нельзя — fs-тулы декодируют им же перед обратной
    записью файла, и молчаливая догадка «это cp1251» заставила бы их писать
    utf-8 поверх.

    Резерв ниже — только для диффа, наружу он ничего не пишет: короткая русская
    строка в однобайтовой кириллице (``git diff`` по cp1251-файлу — почти
    сплошной ASCII с горсткой высоких байт) уверенности chardet не набирает, и
    без резерва такой текст объявлялся бы бинарём, а хэнки шелл-канала
    превращались в U+FFFD прямо в журнале — навсегда.

    Гипотеза принимается, только если КАЖДЫЙ байт старше 0x7F укладывается в
    кириллический диапазон cp1251: латиница-1, koi8 и настоящий бинарь так не
    выглядят. Приговор «бинарь» примитива (NUL в пробе) резерв не пересматривает
    — иначе бинарный файл поехал бы в дифф текстом.
    """
    text = decode_text_best_effort(data)
    if text is not None:
        return text
    if b"\x00" in data[:BINARY_SAMPLE_BYTES]:
        return None
    if data.translate(None, _CP1251_TEXT_BYTES):
        return None  # остались байты, которых кириллица cp1251 не даёт
    return data.decode("cp1251")


@dataclass(frozen=True)
class FileDelta:
    """Итоговая разница по одному файлу.

    ``countable=False`` — сравнить стороны не удалось (кап, бинарь, ошибка
    чтения), и нули означают «не считали», а не «файл не изменился». Поле
    исключено из сравнения (``compare=False``): равенство дельт — про числа и
    хэнки, а отказ от них неотличим ИМЕННО этим, поэтому вызывающий обязан
    спросить признак явно.

    ``disk_sha256`` — хэш байтов диска, с которыми сравнивали. Заполнен только
    на обычном пути: у отказов сравнивать нечего, а быстрый путь «база = диск»
    и так называет состояние — это база. Исключён из сравнения по той же
    причине, что и ``countable``.
    """

    add: int
    del_: int
    hunks: str
    countable: bool = field(default=True, compare=False)
    disk_sha256: Optional[str] = field(default=None, compare=False)


EMPTY_DELTA = FileDelta(0, 0, "")  # различий нет — честный ноль
_UNCOUNTABLE_DELTA = FileDelta(0, 0, "", countable=False)  # сравнить нечем


def build_diff(old_text: str, new_text: str, n: int) -> Tuple[str, int, int]:
    """unified diff + add/del по строкам тела.

    ``difflib.unified_diff`` эмитит заголовки ``--- ``/``+++ `` ровно один раз —
    первыми двумя строками потока (если он непустой), не на каждый хэнк.
    Поэтому заголовки отбрасываются позиционно (``lines[2:]``), а не фильтром
    по префиксу: контентная строка вида ``++i;`` или ``---`` после ``+``/``-``
    diff-маркера сама даёт трёхсимвольный префикс ``+++``/``---`` и фильтр по
    префиксу молча терял её из счётчиков (текст ``hunks`` при этом не портится
    — только числа).
    """
    lines = [
        line if line.endswith("\n") else f"{line}\n"
        for line in difflib.unified_diff(
            split_lines_lf(old_text, keepends=True),
            split_lines_lf(new_text, keepends=True),
            n=n,
            lineterm="\n",
        )
    ]
    hunks = "".join(lines)
    body = lines[
        2:
    ]  # без заголовков --- / +++ (пусто, если lines пуст — различий нет)
    add = sum(1 for line in body if line.startswith("+"))
    del_ = sum(1 for line in body if line.startswith("-"))
    return hunks, add, del_


def abs_path_for_key(launch_root: str, path_key: str) -> Path:
    """``path_key`` журнала → абсолютный путь.

    Под ``launch_root`` ключ относительный, у файла ВНЕ проекта — уже
    абсолютный (см. ``agent_edits.paths.canonical_path_key``).
    """
    path = Path(path_key)
    return path if path.is_absolute() else Path(launch_root) / path


def delta_vs_disk(
    baseline: Optional[bytes],
    abs_path: Path,
    *,
    max_source_bytes: int = DEFAULT_MAX_DIFF_SOURCE_BYTES,
    hunk_context_lines: int = DEFAULT_HUNK_CONTEXT_LINES,
) -> FileDelta:
    """Дельта одного файла: ``baseline`` (снимок «до») против диска.

    Деградации честные, без выдуманных чисел:

    * ``baseline is None`` — база пуста, весь файл идёт в добавленные. Отвечает
      за легальность пустой базы ВЫЗЫВАЮЩИЙ: базы не бывает не только у
      созданного файла (см. ``file_entries``);
    * файла нет на диске — он удалён: весь снимок идёт в удалённые;
    * любая сторона больше ``max_source_bytes`` или не декодируется в текст
      (бинарь) — нули с ``countable=False``: считать нечем, и вызывающий обязан
      это различать. Кап проверяется по ``stat``, файл сверх него целиком не
      читается.

    Быстрый путь по равенству байтов идёт ДО декодирования: обе стороны не
    ``None`` и побайтово совпадают → различий нет, и это узнаваемо без
    декодирования вовсе. Без него короткий текст в редкой кодировке (chardet
    ниже порога уверенности) объявлялся бы несчитаемым, даже когда база и
    диск идентичны — дефект бьёт по разобранности, счётчикам и подтверждению
    сброса разом, поскольку все трое читают этот же примитив.
    """
    if baseline is not None and len(baseline) > max_source_bytes:
        return _UNCOUNTABLE_DELTA

    current: Optional[bytes] = None
    if abs_path.is_file():
        try:
            if abs_path.stat().st_size > max_source_bytes:
                return _UNCOUNTABLE_DELTA
            current = abs_path.read_bytes()
        except OSError:
            return _UNCOUNTABLE_DELTA

    if baseline is not None and current is not None and baseline == current:
        return EMPTY_DELTA

    old_text = "" if baseline is None else decode_edit_text(baseline)
    new_text = "" if current is None else decode_edit_text(current)
    if old_text is None or new_text is None:
        return _UNCOUNTABLE_DELTA

    hunks, add, del_ = build_diff(old_text, new_text, hunk_context_lines)
    disk = None if current is None else sha256_hex_of_bytes(current)
    return FileDelta(add, del_, hunks, disk_sha256=disk)


@dataclass(frozen=True)
class FileEntry:
    """Итог набора записей по одному файлу — то, что показывает экран.

    Счётчики кумулятивные (база представления против диска), пометки честные.
    Состояний счёта ТРИ, и сливать их нельзя — иначе неточное выглядит точным:

    * ``exact=True, countable=True`` — итог посчитан от базы, числа полные;
    * ``exact=False, countable=True`` — базы нет (Р12), числа взяты из журнала:
      это сумма пошаговых правок, а не разница «до → сейчас»;
    * ``countable=False`` — дельту считать было нечем: у пути нет ни одной
      записи с диффом (бинарь, файл сверх капа, шелл-удаление) либо сравнение
      сторон отказало по тому же капу. Нули здесь — «не считали», а не «файл не
      изменился»; если журнал что-то знает, счётчики берутся из него.

    ``revertable`` отвечает только за точные снимки и файлы, созданные здесь.
    """

    path: str  # canonical key журнала
    add: int
    del_: int
    kind: str  # "created" | "deleted" | "modified"
    exact: bool  # False → счётчики из журнала, а не от базы
    countable: bool  # False → дельта не считалась вовсе
    by_shell: bool  # среди записей пути есть сделанные командой
    external: bool  # разрыв before-цепочки: файл правился извне
    revertable: bool
    last_ts: float  # для сортировки «по последней правке»
    # Создан в этом наборе (``created_here`` с поправкой на ступень лестницы) —
    # независимо от того, есть ли файл на диске сейчас. ``kind`` этого не
    # несёт: созданный и удалённый путь — это ``deleted``, а для правила
    # «пусто — значит нет строки» он — исходное «файла нет».
    created: bool = False


def file_entries(
    records: Iterable[EditRecord],
    baseline_for: Callable[[str], Optional[bytes]],
    launch_root: str,
    *,
    revertable_for: Optional[Callable[[str], bool]] = None,
    stepped: Optional[Callable[[str], bool]] = None,
    absent_step: Optional[Callable[[str], bool]] = None,
    max_source_bytes: int = DEFAULT_MAX_DIFF_SOURCE_BYTES,
    hunk_context_lines: int = DEFAULT_HUNK_CONTEXT_LINES,
) -> list[FileEntry]:
    """Итог по каждому файлу набора. Порядок — по последней правке, убыванию.

    Единственный пофайловый расчёт на всех потребителей (Р11): ``net_summary``
    — тонкий агрегат над ним, второй реализации тех же чисел не заводится.

    Каждый путь считается одним из двух способов:

    * есть база ИЛИ файл создан в этом наборе (``created_here``) — итог по
      диску: база против текущего содержимого, ``exact=True``;
    * базы нет, и файл не создавался — счётчики ИЗ ЖУРНАЛА, сумма по записям
      пути, ``exact=False``. Считать дельту от пустой базы тут нельзя: базы нет
      не потому, что снимать было нечего (см. ``created_here``).

    **У созданного здесь файла база набора пуста, пока у пути нет ступени
    выше** (принятие или коммит, §3.2 спеки) — и для счётчиков, и для права
    отката, — а ``baseline_for`` для него не зовётся вовсе. Снимок у такого
    пути существовать МОЖЕТ, но он не «до»: рекордер снимает его, только
    когда старый контент был, то есть начиная со ВТОРОЙ правки (тул создал файл,
    потом дописал строку). Победи такой снимок пустоту — в кумулятиве осталась
    бы одна дописанная строка вместо всего сочинённого файла, а откат «созданного
    и удалённого» СОЗДАЛ бы файл с содержимым, которого у пользователя никогда
    не было.

    Ступень выше переворачивает это правило: ``stepped(path_key) is True``
    обходит created-ветку целиком — ``baseline_for`` зовётся и для такого
    пути, ``kind`` выходит ``modified``/``deleted`` вместо ``created``, а
    ``revertable`` — безусловно ``True`` (ступень сама по себе точная порода
    снимка). Без ступени файл, «созданный и принятый», откатывался бы
    удалением вместо восстановления принятого содержимого.

    Путь, все записи которого ``binary``/``no_diff`` (бинарь, файл сверх капа,
    шелл-удаление), в списке присутствует и даёт 0/0 с ``countable=False``:
    сравнивать нечем, а счётчики журнала тут отвечали бы на другой вопрос.
    ``baseline_for`` для такого пути тоже НЕ зовётся — чтение снимка целиком
    ради заведомых нулей это чистая трата диска.

    ``baseline_for`` — источник базы для СЧЁТА (``counting_*_baseline``);
    ``revertable_for(path_key)`` отвечает на вопрос «есть ли ТОЧНЫЙ снимок» и
    только он даёт право на откат чужого содержимого. ``None`` — предикат не
    задан (зовут ради одних счётчиков).

    ``stepped(path_key)`` — «у пути есть ступень выше начала сессии»; значение
    имеет только для created-путей (см. выше). ``None`` — предикат не задан,
    поведение прежнее: так зовёт ``net_summary`` — сводка хода ступеней не
    видит (Р27).

    ``absent_step(path_key)`` — «ступень пути это принятое ОТСУТСТВИЕ файла»
    (§3.1 спеки волны 4). База такого пути не «отсутствует», а ПУСТА, и
    разница принципиальная: ``baseline_for`` отдаёт по нему ``None``, и без
    предиката путь попадал в ветку «базы нет» — счётчики из журнала,
    ``exact=False``, ``kind`` из записей, — хотя ступень у него есть и §12
    требует точных чисел. Предикат включает ту же ветку, что у созданного
    файла: дельта от пустоты, ``kind`` ``created``, право отката по
    существованию на диске (откат такого пути — удаление, §3.1).
    """
    by_path: dict[str, list[EditRecord]] = {}
    for rec in records:
        # Внешние пути отсекаются ЗДЕСЬ, в единственном пофайловом расчёте
        # (Р11), а не на экране: сводка хода — тонкий агрегат над ним, и
        # второй фильтр во втором слое дал бы два разных числа об одном
        # наборе. Считать внешний путь нечем — базы у него нет.
        if is_external_key(rec.path):
            continue
        by_path.setdefault(rec.path, []).append(rec)

    entries: list[FileEntry] = []
    for path_key, unordered in by_path.items():
        # Сортировка стабильная: при равных ts порядок дописывания сохраняется.
        path_records = sorted(unordered, key=lambda rec: rec.ts)
        abs_path = abs_path_for_key(launch_root, path_key)
        on_disk = abs_path.is_file()
        created = created_here(path_records)
        # Ступень выше есть — база больше не пуста, created-обход снят.
        stepped_over_created = (
            created and stepped is not None and stepped(path_key)
        )
        if stepped_over_created:
            created = False
        # ...кроме ступени «принято отсутствие»: её база — ПУСТОТА, и путь
        # считается от неё ровно как созданный (§3.1), чем бы он ни был в
        # журнале до принятия.
        if absent_step is not None and absent_step(path_key):
            created = True
            stepped_over_created = False

        exact = countable = False
        add = del_ = 0
        disk_sha256: Optional[str] = None
        baseline_sha256: Optional[str] = None
        if not all(_is_no_diff(rec) for rec in path_records):
            baseline = None if created else baseline_for(path_key)
            if baseline is not None:
                baseline_sha256 = sha256_hex_of_bytes(baseline)
            if baseline is not None or created:
                delta = delta_vs_disk(
                    baseline,
                    abs_path,
                    max_source_bytes=max_source_bytes,
                    hunk_context_lines=hunk_context_lines,
                )
                countable = delta.countable
                exact = delta.countable
                add, del_ = delta.add, delta.del_
                disk_sha256 = delta.disk_sha256
            else:
                countable = (
                    True  # пошаговые хэнки в журнале всё-таки посчитаны
                )
            if not exact:
                add = sum(rec.add for rec in path_records)
                del_ = sum(rec.del_ for rec in path_records)

        if stepped_over_created:
            revertable = True  # ступень — точная порода, база под неё есть
        elif created:
            # Созданный файл откатывается удалением, и права на это ему даёт
            # только собственное существование: точного «до» у него нет и
            # быть не может.
            revertable = on_disk
        else:
            revertable = bool(revertable_for and revertable_for(path_key))

        entries.append(
            FileEntry(
                path=path_key,
                add=add,
                del_=del_,
                kind=_kind(path_records, created=created, on_disk=on_disk),
                exact=exact,
                countable=countable,
                by_shell=any(rec.call_id is None for rec in path_records),
                external=_edited_outside(
                    path_records,
                    disk_sha256=disk_sha256,
                    baseline_sha256=baseline_sha256,
                ),
                revertable=revertable,
                last_ts=path_records[-1].ts,
                created=created,
            )
        )

    entries.sort(key=lambda entry: entry.last_ts, reverse=True)
    return entries


def net_summary(
    records: Iterable[EditRecord],
    baseline_for: Callable[[str], Optional[bytes]],
    launch_root: str,
    *,
    max_source_bytes: int = DEFAULT_MAX_DIFF_SOURCE_BYTES,
    hunk_context_lines: int = DEFAULT_HUNK_CONTEXT_LINES,
) -> Tuple[int, int, int]:
    """(файлов, +, −) по набору записей — агрегат над ``file_entries``.

    Файлов — число РАЗЛИЧНЫХ путей в записях (включая те, что дают 0/0).
    Правила счёта и деградаций — там же, одним расчётом на всех потребителей
    (Р11): два разных расчёта одного числа в интерфейсе — это два разных числа
    на экране. ``stepped=None`` — сводка хода лестницу ступеней не видит (Р27).
    """
    entries = file_entries(
        records,
        baseline_for,
        launch_root,
        stepped=None,
        max_source_bytes=max_source_bytes,
        hunk_context_lines=hunk_context_lines,
    )
    return (
        len(entries),
        sum(entry.add for entry in entries),
        sum(entry.del_ for entry in entries),
    )


def _kind(
    path_records: list[EditRecord], *, created: bool, on_disk: bool
) -> str:
    """Вид правки файла. Порядок проверок важен.

    «Создан и удалён в наборе» — это ``deleted``: на диске файла нет, и
    показывать его как созданный значило бы врать про существующий файл.
    """
    if created and on_disk:
        return "created"
    if not on_disk or path_records[-1].op == "delete":
        return "deleted"
    return "modified"


def _since_baseline(
    path_records: list[EditRecord], baseline_sha256: Optional[str]
) -> tuple[list[EditRecord], bool]:
    """Хвост цепочки ПОСЛЕ базы и признак «база в цепочке нашлась».

    Отсчёт идёт от базы представления, и история ниже неё в показанную дельту
    не входит: приняли состояние — всё, что было до него, стало точкой отсчёта,
    а не содержимым. Обрезка идёт по ПОСЛЕДНЕМУ вхождению: файл мог возвращаться
    к этому состоянию не раз, и текущая точка — более поздняя.

    База не нашлась — обрезки нет, и это не отказ. Базы бывают синтетическими:
    байты ступени HEAD собирает ``git_ops.head_bytes_like_disk``, приводя концы
    строк к преобладающим на диске, и такого состояния на диске могло не быть
    ни разу. Тогда правило просто помнит больше нужного — лишнее
    предупреждение, а не вечное ложное.
    """
    if baseline_sha256 is None:
        return path_records, False
    cut = -1
    for idx, rec in enumerate(path_records):
        if rec.after_sha256 == baseline_sha256:
            cut = idx
    if cut < 0:
        return path_records, False
    return path_records[cut + 1 :], True


def _edited_outside(
    path_records: list[EditRecord],
    *,
    disk_sha256: Optional[str] = None,
    baseline_sha256: Optional[str] = None,
) -> bool:
    """В показанной дельте есть содержимое, которого агент не писал.

    Вопрос про ДЕЛЬТУ, а не про историю: метится то, что видно от базы до
    диска. Отсюда две опоры — цепочка записей журнала и состояние диска, — и
    обе отвечают на один вопрос, поэтому складываются.

    * **цепочка**: запись легла на состояние, которого мы не знаем, — её
      ``before`` не совпал с тем, чем кончила предыдущая, и в цепочке раньше не
      встречался. Возврат к ИЗВЕСТНОМУ состоянию разрывом не считается: откат
      пишет файл на диск, записи в журнал не делает, и следующая запись честно
      говорит «видел то, что было раньше»;
    * **диск**: то, что лежит там сейчас, мы не производили. Видно сразу, без
      следующей записи: счётчики строки идут от базы против ДИСКА, и чужая
      строка попадает в них немедленно.

    Признак ЛИПКИЙ в пределах дельты: раз чужое содержимое в неё вошло, оно
    там и остаётся, сколько бы правок агент ни сделал сверху. Прежняя форма
    решала по последней сравнимой записи и гасила пометку на первой же правке
    поверх чужой — при том что строки оставались в дельте, счётчики их
    включали, а предупреждение перед откатом исчезало: `r` уничтожал ручную
    работу молча (пятый раунд ревью, находка 3). Уходит пометка не от новой
    правки, а от сдвига БАЗЫ — принятия или коммита, — и это делает
    ``_since_baseline``.

    ``None`` — «сравнить не с чем», а не разрыв (§3 спеки): у записи сверх капа
    мутирующих тулов старый контент в память не поднимался, а у git-подканала
    ``before`` неизвестен в принципе — он видит файл только ПОСЛЕ команды
    (карманный подканал байты «до» знает и хэш заполняет). Без ``after``
    последней записи не судим и о диске: чем агент кончил — неизвестно.

    Начало отсчёта после обрезки — сама база: цепочка обязана продолжиться
    именно от неё. Если же базу в цепочке не нашли, первой записи сравнивать
    не с чем, и претензий к ней нет.

    **Названное ограничение.** Запись агента, не дошедшая до журнала (тул MCP,
    выключенный ``shell_tracking``, обрезка по капу путей), неотличима от
    чужой. Ослаблять предупреждение из-за этого нельзя: откат уничтожит и такую
    работу, то есть предупредить тут правильно — неточно лишь слово «агента».
    Сюда же два остатка поменьше: чужая правка, чьё содержимое агент потом
    полностью затёр, даёт лишнее предупреждение; а грязь, лежавшая на диске до
    начала сессии, не видна вовсе — журнал о ней ничего не знает.
    """
    records, anchored = _since_baseline(path_records, baseline_sha256)
    outside = False
    # База, от которой идёт отсчёт, — известное состояние: откат к ней пишет
    # диск без записи в журнал, и следующая запись честно говорит «видел
    # базу». Без неё в известных возврат к базе считался разрывом, когда её
    # хэш в цепочке не встречался ни разу — у записей git-подканала `before`
    # пустой (смок 2026-09-24: 100 путей из 500 получили «извне» за правки
    # самого агента).
    known: set[str] = (
        {baseline_sha256} if baseline_sha256 is not None else set()
    )
    previous_after: Optional[str] = baseline_sha256 if anchored else None
    for rec in records:
        if (
            rec.before_sha256 is not None
            and previous_after is not None
            and rec.before_sha256 != previous_after
            and rec.before_sha256 not in known
        ):
            outside = True
        for digest in (rec.before_sha256, rec.after_sha256):
            if digest is not None:
                known.add(digest)
        previous_after = rec.after_sha256
    if (
        disk_sha256 is not None
        and previous_after is not None
        and disk_sha256 not in known
    ):
        outside = True
    return outside


def created_here(path_records: list[EditRecord]) -> bool:
    """Файл создан в этом наборе записей — пустая база для него легальна.

    Предикат публичный: тело диффа во вьюере обязано выбирать базу по тому же
    правилу, что счётчики списка (``tui.services.edits_view.file_diff``), иначе
    одна и та же правка показала бы на экране два разных диффа.

    Базы «до» пишут два канала, и ни один не пишет её у созданного файла:
    ``recorder.record_edit`` снимает точный снимок, только когда старый контент
    вообще был, а шелл-канал поднимает счётную базу из ИНДЕКСА git — у файла,
    которого в индексе нет, брать её неоткуда. Но базы не бывает и по другим
    причинам: у правки чужого untracked-файла командой её нет ровно так же, а
    флаг ``partial`` (append сверх капа) съём глушит. Отличить эти случаи можно
    только по журналу: признак создания — ПЕРВАЯ запись пути с ``op="create"``.

    Такую запись ставят ОБА канала: fs-тулы — когда файла до правки не было;
    шелл-делта — когда пути не было в снимке «до», после команды он
    неотслеживаемый И файл на диске свежее этого снимка (см.
    ``shell_delta._created_by_command``). Запись ``create`` после любой другой
    по тому же пути создания в этом наборе не означает — до него файл был.
    """
    return path_records[0].op == "create"


def _is_no_diff(rec: EditRecord) -> bool:
    return rec.op == "binary" or "no_diff" in rec.flags


def truncate_hunks(hunks: str, max_chars: int) -> tuple[str, list[str]]:
    """Хэнки под потолком и флаги записи. `max_chars <= 0` — БЕЗ ЛИМИТА.

    Ноль как «без лимита» обещан обоими поставляемыми шаблонами конфига
    (`max_diff_chars: 0 = без лимита`). Без этой проверки ноль резал бы каждый
    хэнк в пустую строку и помечал запись усечённой — то есть журнал терял бы
    содержимое ровно там, где его просили не ограничивать.

    Владелец один на все три точки усечения (рекордер и оба места шелл-канала):
    порознь проверка уже однажды разъехалась — её потеряли при переписывании.
    """
    if max_chars <= 0 or len(hunks) <= max_chars:
        return hunks, []
    return hunks[:max_chars], ["truncated"]
