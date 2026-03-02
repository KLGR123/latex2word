#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Convert BibTeX (.bib) to a GB/T 7714-2015 (numeric) reference list,
and export a structured JSON lookup dictionary for downstream use.

Install:
  pip install bibtexparser

Usage:
  python bib.py --bib refs1.bib refs2.bib --out refs.json
  python bib.py --bib refs1.bib refs2.bib --out refs.json --sort year --dedup
"""

import argparse
import json
import re
from typing import List, Dict, Set

import bibtexparser


# ----------------------------
# Helpers
# ----------------------------

def normalize_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def strip_braces(s: str) -> str:
    if not s:
        return ""
    s = s.replace("{", "").replace("}", "")
    return normalize_spaces(s)


def latex_to_plain(s: str) -> str:
    """Light LaTeX cleanup (best-effort)."""
    if not s:
        return ""
    s = strip_braces(s)
    s = s.replace(r"\&", "&")
    s = s.replace(r"---", "–").replace(r"--", "–")
    s = re.sub(r"\\(textit|emph|textbf)\s*", "", s)
    s = s.replace("\\", "")
    return normalize_spaces(s)


def extract_url(s: str) -> str:
    r"""
    Extract URL from:
        - \url{https://...}
        - http(s)://...
    """
    if not s:
        return ""
    raw = s.strip()
    m = re.search(r"\\url\{([^}]+)\}", raw)
    if m:
        return m.group(1).strip()
    m = re.search(r"(https?://\S+)", raw)
    if m:
        return m.group(1).rstrip(".,;)}]").strip()
    return ""


def pick(entry: Dict, *keys: str) -> str:
    for k in keys:
        if k in entry and entry[k]:
            return latex_to_plain(entry[k])
    return ""


def pick_url(entry: Dict) -> str:
    """Prefer explicit url field, else try howpublished/note."""
    url = pick(entry, "url")
    if url:
        return url
    hp = entry.get("howpublished", "") or ""
    nt = entry.get("note", "") or ""
    url = extract_url(hp) or extract_url(nt)
    return url.strip()


def year_from_entry(entry: Dict) -> str:
    y = pick(entry, "year", "date")
    if y and re.match(r"^\d{4}-", y):
        return y[:4]
    return y


def pages_normalize(p: str) -> str:
    p = latex_to_plain(p)
    p = p.replace("--", "-").replace("–", "-")
    return p


def split_authors(author_field: str) -> List[str]:
    if not author_field:
        return []
    parts = [normalize_spaces(p) for p in author_field.split(" and ") if normalize_spaces(p)]
    return parts


def format_one_author(name: str) -> str:
    name = latex_to_plain(name)
    if "," in name:
        last, first = [normalize_spaces(x) for x in name.split(",", 1)]
        return normalize_spaces(f"{last} {first}")
    return name


def contains_chinese(text: str) -> bool:
    """Check if text contains Chinese characters (CJK Unified Ideographs)."""
    if not text:
        return False
    for char in text:
        if '\u4e00' <= char <= '\u9fff':
            return True
    return False


def format_authors(author_field: str, max_authors: int = 3) -> str:
    authors = [format_one_author(a) for a in split_authors(author_field)]
    if not authors:
        return ""
    if len(authors) <= max_authors:
        return ", ".join(authors)
    shown = ", ".join(authors[:max_authors])

    # Auto-detect: use "等" if any author name contains Chinese characters
    has_chinese = any(contains_chinese(author) for author in authors)
    tail = "等" if has_chinese else "et al."
    return f"{shown}, {tail}"


def doc_type_tag(entry_type: str, entry: Dict) -> str:
    """GB/T type tags (pragmatic)."""
    t = (entry_type or "").lower()
    if t == "article":
        return "[J]"
    if t in ("inproceedings", "conference", "proceedings"):
        return "[C]"
    if t in ("book", "inbook"):
        return "[M]"
    if t in ("phdthesis", "mastersthesis", "thesis"):
        return "[D]"
    if t in ("techreport", "report"):
        return "[R]"
    # online-ish
    if pick_url(entry):
        return "[EB/OL]"
    return "[M]"


# ----------------------------
# Formatters
# ----------------------------

def format_article(entry: Dict) -> str:
    authors = format_authors(pick(entry, "author"))
    title = pick(entry, "title")
    journal = pick(entry, "journal", "journaltitle")
    year = year_from_entry(entry)
    volume = pick(entry, "volume")
    number = pick(entry, "number", "issue")
    pages = pages_normalize(pick(entry, "pages"))
    doi = pick(entry, "doi")
    url = pick_url(entry)

    # Determine document type: use [EB/OL] for arXiv/preprint
    doc_tag = "[J]"
    if "arxiv" in (journal or "").lower() or "preprint" in (journal or "").lower():
        doc_tag = "[EB/OL]"

    out = ""
    if authors:
        out += f"{authors}. "
    out += f"{title}{doc_tag}. " if title else f"{doc_tag}. "

    tail = []
    if journal:
        tail.append(journal)
    if year:
        tail.append(year)
    if volume:
        tail.append(f"{volume}({number})" if number else volume)

    if tail:
        out += ", ".join(tail)
        if pages:
            out += f": {pages}"
        out += "."
    else:
        out = out.strip()

    if doi:
        out += f" DOI: {doi}."
    elif url and ("arxiv" in (journal or "").lower() or "preprint" in (journal or "").lower()):
        # Optional: keep arXiv URL if present
        out += f" Available: {url}."
    return normalize_spaces(out)


def format_inproceedings(entry: Dict) -> str:
    authors = format_authors(pick(entry, "author"))
    title = pick(entry, "title")
    booktitle = pick(entry, "booktitle")
    year = year_from_entry(entry)
    pages = pages_normalize(pick(entry, "pages"))
    organization = pick(entry, "organization")
    address = pick(entry, "address", "location")
    doi = pick(entry, "doi")
    url = pick_url(entry)

    out = ""
    if authors:
        out += f"{authors}. "
    out += f"{title}[C]//" if title else "[C]//"
    if booktitle:
        out += f"{booktitle}. "
    if address:
        out += f"{address}: "
    if organization:
        out += f"{organization}, "
    if year:
        out += f"{year}"
    if pages:
        out += f": {pages}"
    out = out.rstrip(", ") + "."

    if doi:
        out += f" DOI: {doi}."
    elif url:
        out += f" Available: {url}."
    return normalize_spaces(out)


def format_book(entry: Dict) -> str:
    authors = format_authors(pick(entry, "author", "editor"))
    title = pick(entry, "title")
    year = year_from_entry(entry)
    publisher = pick(entry, "publisher")
    address = pick(entry, "address", "location")
    edition = pick(entry, "edition")
    url = pick_url(entry)

    out = ""
    if authors:
        out += f"{authors}. "
    out += f"{title}[M]. " if title else "[M]. "

    pub = []
    if address:
        pub.append(address)
    if publisher:
        pub.append(publisher)
    if pub:
        out += ": ".join(pub)
        if year:
            out += f", {year}"
        out += "."
    else:
        out += f"{year}." if year else ""
    if edition:
        out += f" {edition}."
    if url:
        out += f" Available: {url}."
    return normalize_spaces(out)


def format_thesis(entry: Dict) -> str:
    authors = format_authors(pick(entry, "author"), max_authors=99)
    title = pick(entry, "title")
    year = year_from_entry(entry)
    school = pick(entry, "school")
    address = pick(entry, "address", "location")
    ttype = pick(entry, "type")
    url = pick_url(entry)

    out = ""
    if authors:
        out += f"{authors}. "
    out += f"{title}[D]. " if title else "[D]. "

    if address and school:
        out += f"{address}: {school}"
    elif school:
        out += f"{school}"
    if year:
        out += f", {year}"
    out += "."
    if ttype:
        out += f" {ttype}."
    if url:
        out += f" Available: {url}."
    return normalize_spaces(out)


def format_techreport(entry: Dict) -> str:
    authors = format_authors(pick(entry, "author"))
    title = pick(entry, "title")
    year = year_from_entry(entry)
    inst = pick(entry, "institution")
    rtype = pick(entry, "type")
    number = pick(entry, "number")
    address = pick(entry, "address", "location")
    url = pick_url(entry)

    out = ""
    if authors:
        out += f"{authors}. "
    out += f"{title}[R]. " if title else "[R]. "

    tail = []
    if address:
        tail.append(address)
    if inst:
        tail.append(inst)
    if tail:
        out += ": ".join(tail)
        if year:
            out += f", {year}"
        out += "."
    else:
        out += f"{year}." if year else ""

    if rtype:
        out += f" {rtype}."
    if number:
        out += f" No.{number}."
    if url:
        out += f" Available: {url}."
    return normalize_spaces(out)


def format_misc_or_online(entry: Dict) -> str:
    authors = format_authors(pick(entry, "author"))
    title = pick(entry, "title")
    year = year_from_entry(entry)
    how = latex_to_plain(entry.get("howpublished", "") or "")
    note = pick(entry, "note")
    url = pick_url(entry)

    tag = "[EB/OL]" if url else "[M]"

    out = ""
    if authors:
        out += f"{authors}. "
    out += f"{title}{tag}. " if title else f"{tag}. "

    tail = []
    if how and not extract_url(how):  # avoid duplicating raw \url{...}
        tail.append(how)
    if year:
        tail.append(year)
    if tail:
        out += ", ".join(tail) + "."
    else:
        out = out.strip()

    if note:
        out += f" {note}."
    if url:
        out += f" Available: {url}."
    return normalize_spaces(out)


def format_entry(entry: Dict) -> str:
    etype = (entry.get("ENTRYTYPE") or "").lower()
    if etype == "article":
        return format_article(entry)
    if etype in ("inproceedings", "conference", "proceedings"):
        return format_inproceedings(entry)
    if etype in ("book", "inbook"):
        return format_book(entry)
    if etype in ("phdthesis", "mastersthesis", "thesis"):
        return format_thesis(entry)
    if etype in ("techreport", "report"):
        return format_techreport(entry)
    return format_misc_or_online(entry)


def load_bib_entries(paths: List[str]) -> List[Dict]:
    """Load and merge entries from one or more .bib files."""
    all_entries = []
    for path in paths:
        with open(path, "r", encoding="utf-8") as f:
            bibdb = bibtexparser.load(f)
        all_entries.extend(bibdb.entries)
    return all_entries


def sort_entries(entries: List[Dict], mode: str) -> List[Dict]:
    if mode == "file":
        return entries
    if mode == "year":
        def y(e):
            yy = year_from_entry(e)
            return int(yy) if yy.isdigit() else -1
        return sorted(entries, key=lambda e: (-y(e), (e.get("ID") or "")))
    if mode == "key":
        return sorted(entries, key=lambda e: (e.get("ID") or ""))
    return entries


def normalize_title_key(entry: Dict) -> str:
    """
    Produce a normalized title string for deduplication:
    strips LaTeX braces, lowercases, collapses whitespace.
    Falls back to empty string if no title field.
    """
    raw = entry.get("title", "") or ""
    return strip_braces(raw).lower().strip()


def deduplicate_entries(entries: List[Dict]) -> List[Dict]:
    """
    Remove duplicate entries based on normalized title (lowercase + strip).
    Keeps the first occurrence; all bib keys (including duplicates) are
    recorded in the '__aliases__' field (a set) of the retained entry.
    Entries without a title fall back to the formatted reference string.
    """
    # norm_key -> index in unique_entries
    seen: Dict[str, int] = {}
    unique_entries: List[Dict] = []

    for e in entries:
        bib_key = e.get("ID", "")
        norm_key = normalize_title_key(e)

        # Fall back to formatted reference when title is absent
        if not norm_key:
            norm_key = format_entry(e)

        if norm_key in seen:
            # Duplicate: register this bib key as an alias of the first occurrence
            idx = seen[norm_key]
            if bib_key:
                unique_entries[idx]["__aliases__"].add(bib_key)
        else:
            new_entry = dict(e)
            new_entry["__aliases__"] = {bib_key} if bib_key else set()
            seen[norm_key] = len(unique_entries)
            unique_entries.append(new_entry)

    return unique_entries


def build_lookup(entries: List[Dict], start: int = 1) -> Dict:
    """
    Build the output lookup dictionary.

    Structure:
        {
            "<bib_key>": {
                "id":       <int>,          # numeric citation index
                "citation": "<str>"         # formatted GB/T 7714-2015 string
            },
            ...
        }

    Every bib key (including duplicate aliases) is registered as a separate
    top-level key pointing to the same id / citation value, so any downstream
    lookup by original cite key works directly.
    """
    lookup: Dict = {}
    for numeric_id, entry in enumerate(entries, start=start):
        citation = format_entry(entry)
        aliases: Set[str] = entry.get("__aliases__", set())
        record = {"id": numeric_id, "citation": citation}
        for key in aliases:
            lookup[key] = record
    return lookup


def main():
    ap = argparse.ArgumentParser(
        description="Convert .bib files to a GB/T 7714-2015 JSON lookup dictionary."
    )
    ap.add_argument(
        "--bib", required=True, nargs="+",
        help="One or more input .bib file paths"
    )
    ap.add_argument(
        "--out", default="refs.json",
        help="Output JSON file path (default: refs.json)"
    )
    ap.add_argument(
        "--sort", choices=["file", "year", "key"], default="file",
        help="Sorting mode applied before numbering"
    )
    ap.add_argument(
        "--dedup", action="store_true",
        help="Deduplicate entries by normalized title"
    )
    ap.add_argument(
        "--start", type=int, default=1,
        help="Starting numeric citation index (default: 1)"
    )
    args = ap.parse_args()

    entries = load_bib_entries(args.bib)

    if args.dedup:
        entries = deduplicate_entries(entries)
    else:
        # Even without dedup, populate __aliases__ so build_lookup works uniformly
        for e in entries:
            bib_key = e.get("ID", "")
            e["__aliases__"] = {bib_key} if bib_key else set()

    entries = sort_entries(entries, args.sort)
    lookup = build_lookup(entries, start=args.start)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(lookup, f, ensure_ascii=False, indent=2)

    print(f"Wrote {len(lookup)} citation keys ({len(entries)} unique entries) -> {args.out}")


if __name__ == "__main__":
    main()
