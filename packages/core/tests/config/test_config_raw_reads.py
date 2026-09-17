"""Сырое чтение документа конфига: чем оно отвечает на нечитаемый файл.

`config_raw.read_document` — best-effort читатель витрин: он не грузит конфиг, а
показывает, что лежит в файле, и потому на любой отказ чтения обязан отдать
пустой документ, а не бросить. Витрина профилей читает им ВСЕ файлы корня,
включая неактивные, — а неактивный профиль в загрузке не участвует, и его
кодировку никто до этого не проверял.
"""
import pathlib

from gc_core import config_raw


def test_undecodable_document_reads_as_empty(tmp_path):
    """Файл в UTF-16 — не «битый YAML», а нечитаемые байты: `UnicodeDecodeError`
    подкласс `ValueError`, а не `OSError`, и мимо `except OSError` он уходил
    наружу. Цена: экран настроек падал на чужом профиле, который в этом запуске
    даже не действует."""
    path = tmp_path / "other.yml"
    path.write_text("slots:\n  default: x\n", encoding="utf-16")
    assert config_raw.read_document(path) == {}


def test_missing_document_reads_as_empty(tmp_path):
    """Сторож соседнего исхода: пропавший файл отвечал пусто и обязан отвечать
    так же — правка кодировки не должна была подменить обработку `OSError`."""
    assert config_raw.read_document(tmp_path / "nowhere.yml") == {}


def test_readable_document_still_parses(tmp_path):
    """Сторож: расширение перехвата не глотает нормальное чтение."""
    path = tmp_path / "ok.yml"
    path.write_text("slots:\n  default: x\n", encoding="utf-8")
    assert config_raw.read_document(path) == {"slots": {"default": "x"}}
