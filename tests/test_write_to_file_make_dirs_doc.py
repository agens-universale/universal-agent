"""C2: описание make_dirs в fs_write_file — универсальное (создание родительских
папок), без привязки к конкретному кейсу. Цель — чтобы агент знал про параметр и
понимал, когда его звать (родительской папки нет)."""
from gc_core.tools.fs.write_to_file_tool import WriteToFile


def test_tool_description_documents_make_dirs():
    md = WriteToFile.tool_description()["parameters"]["properties"]["make_dirs"]["description"]
    assert "parent" in md.lower()                 # объясняет: создаёт родительские папки
    assert "missing" in md.lower() or "does not exist" in md.lower()
    assert "skill" not in md.lower()              # универсально, без костыля под кейс


def test_prompt_description_mentions_make_dirs():
    pd = WriteToFile.prompt_description()
    assert "make_dirs" in pd                       # параметр виден в инструкции
    assert "parent" in pd.lower()
    assert "skill" not in pd.lower()
