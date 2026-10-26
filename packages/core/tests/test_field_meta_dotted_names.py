"""Имя записи словаря с точкой (алиас модели ``gpt-4.1``) не открывает секрет.

Строковый путь ``models.gpt-4.1.token`` разваливается на лишние сегменты и не
находит флаг ``secret`` — маскирование обязано идти по сегментам документа.
"""

from gc_core.config_modules.field_meta import MASK, meta_for_parts, redact


def test_dotted_model_alias_token_is_masked():
    document = {"models": {"gpt-4.1": {"token": "SECRET", "model": "m"}}}
    redacted = redact(document)
    assert redacted["models"]["gpt-4.1"]["token"] == MASK
    assert redacted["models"]["gpt-4.1"]["model"] == "m"
    assert "SECRET" not in repr(redacted)


def test_meta_for_parts_resolves_dotted_entry_name():
    assert meta_for_parts(["models", "gpt-4.1", "token"]).secret
    assert not meta_for_parts(["models", "gpt-4.1", "model"]).secret
