from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any, Optional

from science_mcp.models import Author, WorkRecord


def invert_to_text(idx: Optional[dict[str, list[int]]]) -> Optional[str]:
    if not idx:
        return None
    max_pos = -1
    for positions in idx.values():
        for p in positions:
            if p > max_pos:
                max_pos = p
    if max_pos < 0:
        return None
    words: list[str] = [""] * (max_pos + 1)
    for word, positions in idx.items():
        for p in positions:
            words[p] = word
    return " ".join(w for w in words if w)


def openalex_to_work(raw: dict[str, Any]) -> WorkRecord:
    work_id = raw.get("id") or ""
    short_id = work_id.rsplit("/", 1)[-1] if work_id else ""
    authors = [
        Author(name=(au.get("author") or {}).get("display_name"))
        for au in (raw.get("authorships") or [])
        if (au.get("author") or {}).get("display_name")
    ]
    doi = raw.get("doi")
    if doi and doi.startswith("https://doi.org/"):
        doi = doi[len("https://doi.org/"):]
    abstract = invert_to_text(raw.get("abstract_inverted_index"))
    return WorkRecord(
        source="openalex",
        id=short_id or work_id,
        title=raw.get("title") or raw.get("display_name") or "(no title)",
        authors=authors,
        year=raw.get("publication_year"),
        abstract=abstract,
        doi=doi,
        url=raw.get("doi") or work_id or None,
        citation_count=raw.get("cited_by_count"),
        is_open_access=(raw.get("open_access") or {}).get("is_oa"),
        extra={
            "type": raw.get("type"),
            "language": raw.get("language"),
        },
    )


def _crossref_author(a: dict[str, Any]) -> Author:
    if "name" in a and a["name"]:
        return Author(name=a["name"])
    parts = [a.get("given"), a.get("family")]
    name = " ".join(p for p in parts if p) or "Unknown"
    return Author(name=name)


def crossref_to_work(raw: dict[str, Any]) -> WorkRecord:
    doi = raw.get("DOI")
    title_list = raw.get("title") or []
    title = title_list[0] if title_list else "(no title)"
    issued = (raw.get("issued") or {}).get("date-parts") or [[None]]
    year = issued[0][0] if issued and issued[0] else None
    authors = [_crossref_author(a) for a in (raw.get("author") or [])]
    return WorkRecord(
        source="crossref",
        id=doi or raw.get("URL") or "(no id)",
        title=title,
        authors=authors,
        year=year,
        doi=doi,
        url=raw.get("URL"),
        citation_count=raw.get("is-referenced-by-count"),
        extra={
            "type": raw.get("type"),
            "subtype": raw.get("subtype"),
            "container_title": (raw.get("container-title") or [None])[0],
            "publisher": raw.get("publisher"),
        },
    )


def pubmed_xml_to_works(xml_text: str) -> list[WorkRecord]:
    root = ET.fromstring(xml_text)
    out: list[WorkRecord] = []
    for art in root.findall(".//PubmedArticle"):
        pmid_el = art.find(".//PMID")
        pmid = pmid_el.text if pmid_el is not None else None
        title_el = art.find(".//ArticleTitle")
        title = "".join(title_el.itertext()).strip() if title_el is not None else "(no title)"
        abstract_text = " ".join(
            "".join(a.itertext()).strip()
            for a in art.findall(".//Abstract/AbstractText")
        ).strip() or None
        year_el = art.find(".//PubDate/Year")
        year = (
            int(year_el.text)
            if year_el is not None and year_el.text and year_el.text.isdigit()
            else None
        )
        authors: list[Author] = []
        for au in art.findall(".//AuthorList/Author"):
            last = au.findtext("LastName") or ""
            fore = au.findtext("ForeName") or ""
            coll = au.findtext("CollectiveName")
            name = coll or f"{fore} {last}".strip()
            if name:
                authors.append(Author(name=name))
        doi = None
        pmcid = None
        for aid in art.findall(".//ArticleId"):
            id_type = aid.get("IdType")
            if id_type == "doi" and doi is None:
                doi = aid.text
            elif id_type == "pmc" and pmcid is None:
                pmcid = aid.text
        out.append(WorkRecord(
            source="pubmed",
            id=pmid or "(no pmid)",
            title=title,
            authors=authors,
            year=year,
            abstract=abstract_text,
            doi=doi,
            pmid=pmid,
            pmcid=pmcid,
            url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else None,
        ))
    return out


ATOM_NS = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"


def arxiv_atom_to_works(xml_text: str) -> list[WorkRecord]:
    root = ET.fromstring(xml_text)
    out: list[WorkRecord] = []
    for entry in root.findall(f"{ATOM_NS}entry"):
        id_url = entry.findtext(f"{ATOM_NS}id") or ""
        arxiv_id = id_url.rsplit("/abs/", 1)[-1] if "/abs/" in id_url else id_url
        title = (entry.findtext(f"{ATOM_NS}title") or "(no title)").strip()
        summary = (entry.findtext(f"{ATOM_NS}summary") or "").strip() or None
        published = entry.findtext(f"{ATOM_NS}published") or ""
        year = int(published[:4]) if published[:4].isdigit() else None
        authors: list[Author] = []
        for au in entry.findall(f"{ATOM_NS}author"):
            n = au.findtext(f"{ATOM_NS}name")
            if n:
                authors.append(Author(name=n.strip()))
        doi_el = entry.find(f"{ARXIV_NS}doi")
        doi = doi_el.text if doi_el is not None else None
        pdf_url = None
        for link in entry.findall(f"{ATOM_NS}link"):
            if link.get("type") == "application/pdf":
                pdf_url = link.get("href")
                break
        out.append(WorkRecord(
            source="arxiv",
            id=arxiv_id,
            title=title,
            authors=authors,
            year=year,
            abstract=summary,
            doi=doi,
            arxiv_id=arxiv_id,
            url=id_url or pdf_url,
            is_open_access=True,
        ))
    return out


def dblp_hit_to_work(hit: dict[str, Any]) -> WorkRecord:
    info = hit.get("info") or {}
    authors_data = info.get("authors", {}).get("author", [])
    if isinstance(authors_data, dict):
        authors_data = [authors_data]
    authors: list[Author] = []
    for a in authors_data:
        n = a.get("text") if isinstance(a, dict) else a
        if n:
            authors.append(Author(name=str(n)))
    try:
        year = int(info.get("year")) if info.get("year") else None
    except (TypeError, ValueError):
        year = None
    return WorkRecord(
        source="dblp",
        id=info.get("key") or info.get("url") or "(no id)",
        title=info.get("title") or "(no title)",
        authors=authors,
        year=year,
        doi=info.get("doi"),
        url=info.get("ee") or info.get("url"),
        extra={"venue": info.get("venue"), "type": info.get("type")},
    )


def core_to_work(raw: dict[str, Any]) -> WorkRecord:
    authors: list[Author] = []
    for a in raw.get("authors") or []:
        name = a.get("name") if isinstance(a, dict) else None
        if name:
            authors.append(Author(name=name))
    url = raw.get("downloadUrl")
    if not url:
        links = raw.get("links") or []
        if links and isinstance(links[0], dict):
            url = links[0].get("url")
    return WorkRecord(
        source="core",
        id=str(raw.get("id") or raw.get("doi") or "(no id)"),
        title=raw.get("title") or "(no title)",
        authors=authors,
        year=raw.get("yearPublished"),
        abstract=raw.get("abstract"),
        doi=raw.get("doi"),
        url=url,
        is_open_access=True,
        citation_count=raw.get("citationCount"),
    )


def s2_to_work(raw: dict[str, Any]) -> WorkRecord:
    authors = [Author(name=a.get("name")) for a in (raw.get("authors") or []) if a.get("name")]
    ext = raw.get("externalIds") or {}
    return WorkRecord(
        source="semantic_scholar",
        id=raw.get("paperId") or "(no id)",
        title=raw.get("title") or "(no title)",
        authors=authors,
        year=raw.get("year"),
        abstract=raw.get("abstract"),
        doi=ext.get("DOI"),
        pmid=ext.get("PubMed"),
        arxiv_id=ext.get("ArXiv"),
        url=raw.get("url"),
        citation_count=raw.get("citationCount"),
    )
