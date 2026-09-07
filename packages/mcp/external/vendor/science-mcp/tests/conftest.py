from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES_DIR


def _read(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


@pytest.fixture
def openalex_sample() -> str:
    return _read("openalex.json")


@pytest.fixture
def crossref_sample() -> str:
    return _read("crossref.json")


@pytest.fixture
def pubmed_efetch_xml() -> str:
    return _read("pubmed_efetch.xml")


@pytest.fixture
def arxiv_sample() -> str:
    return _read("arxiv.xml")


@pytest.fixture
def dblp_sample() -> str:
    return _read("dblp.json")


@pytest.fixture
def core_sample() -> str:
    return _read("core.json")
