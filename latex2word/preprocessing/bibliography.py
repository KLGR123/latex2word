import argparse
import io
import json
import logging
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from typing import List, Dict, Set, Tuple, Optional, Any

# -----------------------------
# Reporting utilities
# -----------------------------

@dataclass
class Issue:
    level: str               # "ERROR" or "WARNING"
    stage: str               # e.g. "read_bib", "format_entry", "write_json"
    message: str
    context: Dict[str, Any]

class IssueCollector:
    def __init__(self):
        self.issues: List[Issue] = []

    def warn(self, stage: str, message: str, **context):
        self.issues.append(Issue("WARNING", stage, message, dict(context)))

    def error(self, stage: str, message: str, **context):
        self.issues.append(Issue("ERROR", stage, message, dict(context)))

    def demote(self, stage: str, reason: Optional[str] = None) -> int:
        changed = 0
        for issue in self.issues:
            if issue.stage != stage or issue.level != "ERROR":
                continue
            issue.level = "WARNING"
            if reason:
                existing = issue.context.get("note")
                issue.context["note"] = f"{existing}; {reason}" if existing else reason
            changed += 1
        return changed

    def has_errors(self) -> bool:
        return any(i.level == "ERROR" for i in self.issues)

    def counts(self) -> Tuple[int, int]:
        e = sum(1 for i in self.issues if i.level == "ERROR")
        w = sum(1 for i in self.issues if i.level == "WARNING")
        return e, w

    def print_summary(self, logger: logging.Logger):
        e, w = self.counts()
        if e == 0 and w == 0:
            logger.info("No issues detected.")
            return
        logger.info("Issue summary: %d error(s), %d warning(s).", e, w)
        for it in self.issues:
            ctx = ", ".join(f"{k}={v}" for k, v in it.context.items() if v not in ("", None, [], {}, set()))
            logger.log(logging.ERROR if it.level == "ERROR" else logging.WARNING,
                       "[%s] %s: %s%s",
                       it.level, it.stage, it.message, (f" ({ctx})" if ctx else ""))

class ForwardToIssuesHandler(logging.Handler):
    """Forward selected log records into IssueCollector."""
    def __init__(self, issues: IssueCollector, stage: str = "bibtexparser"):
        super().__init__()
        self.issues = issues
        self.stage = stage

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            level = record.levelno
            if level >= logging.ERROR:
                self.issues.error(self.stage, msg, logger=record.name)
            elif level >= logging.WARNING:
                self.issues.warn(self.stage, msg, logger=record.name)
        except Exception:
            pass


# -----------------------------
# Core formatting helpers
# -----------------------------

LANG_TAIL = "et al"  # overridden by CLI

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
    r"""Extract URL from \url{https://...} or http(s)://..."""
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


def normalize_bibtex_month_literals(text: str) -> str:
    """
    Convert bare BibTeX month literals like `month = October,` into braced
    values so parsers that reject symbolic month constants can still load the
    file.
    """
    month_names = {
        "jan", "january",
        "feb", "february",
        "mar", "march",
        "apr", "april",
        "may",
        "jun", "june",
        "jul", "july",
        "aug", "august",
        "sep", "sept", "september",
        "oct", "october",
        "nov", "november",
        "dec", "december",
    }

    def repl(m: re.Match[str]) -> str:
        raw = m.group(2)
        if raw.lower() not in month_names:
            return m.group(0)
        return f"{m.group(1)}{{{raw}}}{m.group(3)}"

    return re.sub(
        r"(\bmonth\s*=\s*)([A-Za-z]+)(\s*,)",
        repl,
        text,
        flags=re.IGNORECASE,
    )

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
    if not text:
        return False
    return any('\u4e00' <= ch <= '\u9fff' for ch in text)

def format_authors(author_field: str, max_authors: int = 3) -> str:
    authors = [format_one_author(a) for a in split_authors(author_field)]
    if not authors:
        return ""
    if len(authors) <= max_authors:
        return ", ".join(authors)
    shown = ", ".join(authors[:max_authors])

    has_chinese = any(contains_chinese(author) for author in authors)
    if has_chinese and LANG_TAIL == "et al":
        tail = "等"
    else:
        tail = LANG_TAIL
    return f"{shown}, {tail}"

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
    elif url and doc_tag == "[EB/OL]":
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
    if how and not extract_url(how):
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
    if "__raw_citation__" in entry:
        return normalize_spaces(str(entry["__raw_citation__"]))
    # BBL-parsed entries carry a special key; route to the BBL formatter.
    if "__bbl_authors__" in entry:
        return _format_bbl_entry(entry)
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


# ============================================================
# BBL parsing
# ============================================================

# Compiled patterns used throughout BBL parsing
_BBL_BIBITEM_RE   = re.compile(r"\\bibitem(?:\[[^\]]*\])?\{([^}]+)\}")
_BBL_NATEXLAB_RE  = re.compile(r"\{?\\natexlab\{[^}]*\}\}?")
_BBL_PENALTY_RE   = re.compile(r"\\penalty0\s*")
_BBL_EMPH_RE      = re.compile(r"\\emph\{([^}]+)\}")
_BBL_DOI_CMD_RE   = re.compile(r"\\doi\{([^}]+)\}")
_BBL_URL_CMD_RE   = re.compile(r"\\url\{([^}]+)\}")
_BBL_YEAR_RE      = re.compile(r"\b(1[89]\d\d|20[0-3]\d)\b")
_BBL_ETAL_RE      = re.compile(r",?\s*\bet[\s~]al\.?", re.IGNORECASE)
_BBL_AND_RE       = re.compile(r"\s+and\s+", re.IGNORECASE)


def _bbl_clean(s: str) -> str:
    """Remove common BBL rendering artifacts from a text fragment."""
    s = _BBL_NATEXLAB_RE.sub("", s)     # \natexlab{a} -> ""
    s = _BBL_PENALTY_RE.sub("", s)      # \penalty0 -> ""
    s = s.replace("~", " ")             # non-breaking space -> regular space
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _bbl_strip_latex(s: str) -> str:
    """Aggressively strip LaTeX markup from a BBL field (title, journal, etc.)."""
    s = _bbl_clean(s)
    return latex_to_plain(s)


def _bbl_extract_doi(text: str) -> str:
    """Extract DOI from \\doi{...}, stripping any https://doi.org/ prefix."""
    m = _BBL_DOI_CMD_RE.search(text)
    if m:
        doi = m.group(1).strip()
        doi = re.sub(r"^https?://doi\.org/", "", doi)
        return doi
    return ""


def _bbl_extract_url(text: str) -> str:
    """Extract the first URL from \\url{...} or a bare http(s):// string."""
    m = _BBL_URL_CMD_RE.search(text)
    if m:
        return m.group(1).strip()
    m = re.search(r"(https?://\S+)", text)
    if m:
        return m.group(1).rstrip(".,;)}]")
    return ""


def _bbl_extract_year(text: str) -> str:
    """Extract 4-digit year from BBL text, handling \\natexlab suffixes."""
    cleaned = _BBL_NATEXLAB_RE.sub("", text)
    m = _BBL_YEAR_RE.search(cleaned)
    return m.group(1) if m else ""


def _bbl_parse_authors(author_text: str) -> Tuple[List[str], bool]:
    """
    Parse a BBL author block like "F.~Last, A.~Last, and C.~Last." into
    a list of individual name strings.

    Returns (authors_list, has_et_al).
    """
    text = _bbl_clean(author_text).rstrip(".")

    # Detect and strip "et al." / "et~al."
    has_etal = bool(_BBL_ETAL_RE.search(text))
    text = _BBL_ETAL_RE.sub("", text)

    # Replace " and " with a temporary sentinel so we can split on commas safely
    text = _BBL_AND_RE.sub("##AND##", text)

    # Split by commas; then restore the sentinel within each token
    raw_parts = [p.strip() for p in text.split(",")]
    authors: List[str] = []
    for part in raw_parts:
        for token in part.split("##AND##"):
            token = token.strip().rstrip(",").strip()
            if token:
                authors.append(token)

    return authors, has_etal


def _format_bbl_authors(author_text: str, max_authors: int = 3) -> str:
    """
    Format a BBL-style author block into a GB/T 7714-2015 author string.
    Names are already in "F. Last" format; we clean and abbreviate.
    """
    authors, has_etal = _bbl_parse_authors(author_text)
    cleaned = [normalize_spaces(latex_to_plain(a)) for a in authors if a.strip()]
    if not cleaned:
        return ""

    has_chinese = any(contains_chinese(a) for a in cleaned)
    tail = "等" if has_chinese else LANG_TAIL

    if has_etal or len(cleaned) > max_authors:
        shown = ", ".join(cleaned[:max_authors])
        return f"{shown}, {tail}"
    return ", ".join(cleaned)


def _bbl_classify_venue(
    venue_blocks: List[str],
) -> Tuple[str, str, str, str, str, str]:
    """
    Infer entry type and extract structured fields from the BBL venue blocks
    (every \\newblock after the title block).

    Returns:
        (entrytype, journal, booktitle, volume, number, pages)
    """
    full_raw  = " ".join(venue_blocks)
    # Remove \penalty0 before numeric parsing; keep \emph intact for now
    stripped  = re.sub(r"\\penalty0", "", full_raw).replace("~", " ")
    clean     = _bbl_clean(full_raw)

    # --- Extract \emph{...} contents (journal or booktitle candidates) ---
    emphs = [_bbl_strip_latex(m) for m in _BBL_EMPH_RE.findall(full_raw)]

    # --- Pages / volume / number extraction ---
    pages, volume, number = "", "", ""

    # Pattern A: 39 (3/4): 324--345  or  97 (3): 327--351
    vnp = re.search(
        r"(\d+)\s*\(([^)]+)\)\s*:\s*(\d+)\s*(?:--|–|—)\s*(\d+)",
        stripped,
    )
    if vnp:
        volume = vnp.group(1)
        number = vnp.group(2)
        pages  = f"{vnp.group(3)}--{vnp.group(4)}"
    else:
        # Pattern B: 33: 1877--1901  (volume only, no parenthesised number)
        vp = re.search(
            r"(\d+)\s*:\s*(\d+)\s*(?:--|–|—)\s*(\d+)",
            stripped,
        )
        if vp:
            volume = vp.group(1)
            pages  = f"{vp.group(2)}--{vp.group(3)}"
        else:
            # Pattern C: "pages 563--587" or "page 567–577"
            pp = re.search(
                r"pages?\s+(\d+)\s*(?:--|–|—|\\penalty0\s*--)\s*(\d+)",
                stripped, re.IGNORECASE,
            )
            if pp:
                pages = f"{pp.group(1)}--{pp.group(2)}"
            else:
                pp2 = re.search(r"page\s+(\d+)[–—]+(\d+)", stripped, re.IGNORECASE)
                if pp2:
                    pages = f"{pp2.group(1)}--{pp2.group(2)}"

    # Volume from "volume~N" / "volume N" (supplement for inproceedings)
    vm = re.search(r"volume\s*~?\s*(\d+)", clean, re.IGNORECASE)
    if vm and not volume:
        volume = vm.group(1)

    # --- Entry type detection ---
    # "In " at the start of the block signals an inproceedings entry.
    if re.match(r"\s*In\b", clean):
        booktitle = emphs[0] if emphs else ""
        return "inproceedings", "", booktitle, volume, number, pages

    # Has \emph{...} -> treat as article (journal, arXiv preprint, etc.)
    if emphs:
        journal = emphs[0]
        return "article", journal, "", volume, number, pages

    return "misc", "", "", volume, number, pages


def _parse_bbl_entry(citekey: str, body: str) -> Dict:
    """
    Parse a single BBL entry body (the text that follows \\bibitem{key}) into a
    bibtex-style dict that is compatible with format_entry / build_lookup.

    The dict carries "__bbl_authors__" so that format_entry routes it to
    _format_bbl_entry instead of the BibTeX formatters.
    """
    # Split body on \newblock; first segment = authors, second = title, rest = venue
    blocks = re.split(r"\\newblock\s*", body)
    blocks = [b.strip() for b in blocks if b.strip()]

    author_text  = _bbl_clean(blocks[0]) if blocks else ""
    raw_title    = _bbl_strip_latex(blocks[1]).rstrip(".") if len(blocks) > 1 else ""
    venue_blocks = blocks[2:] if len(blocks) > 2 else []

    full_raw = " ".join(blocks)  # used for field extraction across all blocks

    doi  = _bbl_extract_doi(full_raw)
    url  = _bbl_extract_url(full_raw)
    year = _bbl_extract_year(" ".join(venue_blocks)) if venue_blocks else ""

    # Some BBL entries (preprints, tech-reports with no venue block) embed the
    # year at the very end of the title block: "...with RLHF, 2022."
    # Strip it out and use it as the year when no venue year was found.
    _TRAILING_YEAR = re.compile(r",?\s*(1[89]\d\d|20[0-3]\d)\s*$")
    ty_m = _TRAILING_YEAR.search(raw_title)
    if ty_m:
        if not year:
            year = ty_m.group(1)
        raw_title = raw_title[:ty_m.start()].rstrip(",").strip()

    # Fall back to searching the entire entry if still no year
    if not year:
        year = _bbl_extract_year(full_raw)

    title_text = raw_title

    etype, journal, booktitle, volume, number, pages = _bbl_classify_venue(venue_blocks)

    return {
        "ENTRYTYPE":        etype,
        "ID":               citekey,
        "__bbl_authors__":  author_text,
        "title":            title_text,
        "journal":          journal,
        "booktitle":        booktitle,
        "volume":           volume,
        "number":           number,
        "pages":            pages,
        "year":             year,
        "doi":              doi,
        "url":              url,
    }


def _format_bbl_entry(entry: Dict) -> str:
    """
    Produce a GB/T 7714-2015 formatted citation string from a BBL-parsed entry
    dict.  Mirrors the logic of format_article / format_inproceedings / etc.
    but uses the BBL-style author string stored in "__bbl_authors__".
    """
    authors   = _format_bbl_authors(entry.get("__bbl_authors__", ""))
    title     = entry.get("title", "")
    etype     = (entry.get("ENTRYTYPE") or "misc").lower()
    year      = entry.get("year", "")
    doi       = entry.get("doi", "")
    url       = entry.get("url", "")

    if etype == "article":
        journal = entry.get("journal", "")
        volume  = entry.get("volume", "")
        number  = entry.get("number", "")
        pages   = pages_normalize(entry.get("pages", ""))

        j_lower = journal.lower()
        doc_tag = "[EB/OL]" if ("arxiv" in j_lower or "preprint" in j_lower or "corr" == j_lower) else "[J]"

        out = (f"{authors}. " if authors else "")
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
        if doi:
            out += f" DOI: {doi}."
        elif url and doc_tag == "[EB/OL]":
            out += f" Available: {url}."
        return normalize_spaces(out)

    if etype in ("inproceedings", "conference"):
        booktitle = entry.get("booktitle", "")
        pages     = pages_normalize(entry.get("pages", ""))

        out = (f"{authors}. " if authors else "")
        out += f"{title}[C]//" if title else "[C]//"
        if booktitle:
            out += f"{booktitle}. "
        if year:
            out += year
        if pages:
            out += f": {pages}"
        out = out.rstrip(", ") + "."
        if doi:
            out += f" DOI: {doi}."
        elif url:
            out += f" Available: {url}."
        return normalize_spaces(out)

    # misc / unknown
    tag = "[EB/OL]" if url else "[M]"
    out = (f"{authors}. " if authors else "")
    out += f"{title}{tag}. " if title else f"{tag}. "
    if year:
        out += f"{year}."
    if url:
        out += f" Available: {url}."
    return normalize_spaces(out)


def _load_bbl_entries(
    paths: List[str],
    issues: IssueCollector,
    strict: bool,
    logger: logging.Logger,
) -> List[Dict]:
    """Parse one or more .bbl files and return a list of entry dicts."""
    all_entries: List[Dict] = []

    for path in paths:
        try:
            if not os.path.exists(path):
                raise FileNotFoundError(path)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
            except UnicodeDecodeError:
                with open(path, "r", encoding="latin-1") as f:
                    content = f.read()

            entries = _split_bbl_content(content, path, issues, strict)
            logger.info("Loaded %d entries from %s", len(entries), path)
            all_entries.extend(entries)

        except Exception as e:
            issues.error("read_bbl", "Failed to read/parse .bbl file", file=path, error=str(e))
            if strict:
                raise

    return all_entries


def _split_bbl_content(
    content: str,
    path: str,
    issues: IssueCollector,
    strict: bool,
) -> List[Dict]:
    """Split raw BBL file content into individual entry dicts."""
    matches = list(_BBL_BIBITEM_RE.finditer(content))
    entries: List[Dict] = []

    for i, m in enumerate(matches):
        citekey   = m.group(1).strip()
        body_start = m.end()
        body_end   = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body = content[body_start:body_end].strip()

        try:
            entry = _parse_bbl_entry(citekey, body)
            entries.append(entry)
        except Exception as e:
            issues.warn("parse_bbl", "Failed to parse BBL entry", key=citekey, file=path, error=str(e))
            if strict:
                raise

    return entries


def _extract_thebibliography_blocks(content: str) -> List[str]:
    pattern = re.compile(
        r"\\begin\{thebibliography\}\{[^}]*\}(.*?)\\end\{thebibliography\}",
        re.DOTALL,
    )
    return [m.group(1) for m in pattern.finditer(content)]


def _tex_bibitem_to_raw_entry(citekey: str, body: str) -> Dict[str, str]:
    text = body
    text = re.sub(r"\\newblock\s*", " ", text)
    text = re.sub(r"\\em\b\s*", "", text)
    text = re.sub(r"\\bibliographystyle\{[^}]*\}", " ", text)
    text = re.sub(r"\\bibliography\{[^}]*\}", " ", text)
    text = latex_to_plain(text)
    text = re.sub(r"\s+\.", ".", text)
    text = normalize_spaces(text).strip(" .")
    if text:
        text += "."
    return {
        "ENTRYTYPE": "rawtex",
        "ID": citekey,
        "title": text,
        "__raw_citation__": text,
    }


def _load_tex_bibliography_entries(
    paths: List[str],
    issues: IssueCollector,
    strict: bool,
    logger: logging.Logger,
) -> List[Dict]:
    """Load bibliography entries embedded in LaTeX thebibliography environments."""
    all_entries: List[Dict] = []

    for path in paths:
        try:
            if not os.path.exists(path):
                raise FileNotFoundError(path)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
            except UnicodeDecodeError:
                with open(path, "r", encoding="latin-1") as f:
                    content = f.read()

            block_count = 0
            entry_count = 0
            for block in _extract_thebibliography_blocks(content):
                block_count += 1
                matches = list(_BBL_BIBITEM_RE.finditer(block))
                for i, m in enumerate(matches):
                    citekey = m.group(1).strip()
                    body_start = m.end()
                    body_end = (
                        matches[i + 1].start()
                        if i + 1 < len(matches)
                        else len(block)
                    )
                    body = block[body_start:body_end].strip()
                    if not body:
                        issues.warn(
                            "parse_tex_bibliography",
                            "Embedded bibliography entry has empty body",
                            key=citekey,
                            file=path,
                        )
                        continue
                    all_entries.append(_tex_bibitem_to_raw_entry(citekey, body))
                    entry_count += 1
            if block_count:
                logger.info(
                    "Loaded %d embedded bibliography entries from %s",
                    entry_count,
                    path,
                )
        except Exception as e:
            issues.error(
                "read_tex_bibliography",
                "Failed to read/parse embedded bibliography from .tex file",
                file=path,
                error=str(e),
            )
            if strict:
                raise

    return all_entries


# ============================================================
# IO + pipeline steps (BibTeX)
# ============================================================

def _load_bibtex_entries(
    paths: List[str],
    issues: IssueCollector,
    strict: bool,
    logger: logging.Logger,
) -> List[Dict]:
    """Load and merge entries from one or more .bib files."""
    all_entries: List[Dict] = []
    try:
        import bibtexparser
        from bibtexparser.bparser import BibTexParser
    except ImportError as exc:
        issues.error("read_bib", "bibtexparser package is not installed", error=str(exc))
        if strict:
            raise
        return all_entries

    for path in paths:
        try:
            if not os.path.exists(path):
                raise FileNotFoundError(path)
            with open(path, "r", encoding="utf-8") as f:
                raw_text = f.read()
            normalized_text = normalize_bibtex_month_literals(raw_text)
            try:
                parser = BibTexParser(ignore_nonstandard_types=False)
                bibdb = bibtexparser.load(io.StringIO(normalized_text), parser=parser)
            except Exception as e:
                raise ValueError(f"BibTeX parse failed: {e}") from e

            entries = getattr(bibdb, "entries", None)
            if entries is None:
                issues.error("read_bib", "bibtexparser returned no entries attribute", file=path)
                if strict:
                    raise RuntimeError("No entries returned from bibtexparser.")
                continue

            logger.info("Loaded %d entries from %s", len(entries), path)
            all_entries.extend(entries)

        except UnicodeDecodeError as e:
            issues.error("read_bib", "Failed to decode file as UTF-8", file=path, error=str(e))
            if strict:
                raise
        except Exception as e:
            issues.error("read_bib", "Failed to read/parse .bib file", file=path, error=str(e))
            if strict:
                raise
    return all_entries


def load_bib_entries(
    paths: List[str],
    issues: IssueCollector,
    strict: bool,
    logger: logging.Logger,
) -> List[Dict]:
    """
    Load bibliography entries from a mixed list of .bib and .bbl file paths.

    Strategy:
      1. Parse all .bib files with bibtexparser.
      2. Parse all .bbl files with the BBL parser.
      3. If no .bib files were given (or all failed) AND .bbl files exist,
         load from .bbl automatically — the caller need not distinguish.
    """
    bib_paths = [p for p in paths if p.lower().endswith(".bib")]
    bbl_paths = [p for p in paths if p.lower().endswith(".bbl")]
    tex_paths = [p for p in paths if p.lower().endswith(".tex")]

    all_entries: List[Dict] = []
    bib_entries: List[Dict] = []
    bbl_entries: List[Dict] = []
    tex_entries: List[Dict] = []

    if bib_paths:
        bib_entries = _load_bibtex_entries(bib_paths, issues, strict, logger)
        all_entries.extend(bib_entries)
        if not bib_entries:
            logger.info("No entries loaded from .bib files; will try .bbl files if provided.")

    if bbl_paths:
        bbl_entries = _load_bbl_entries(bbl_paths, issues, strict, logger)
        all_entries.extend(bbl_entries)
        if bbl_entries and not bib_entries:
            demoted = issues.demote("read_bib", reason="using .bbl fallback")
            if demoted:
                logger.info(
                    "Demoted %d .bib parse error(s) to warning(s) because .bbl fallback succeeded.",
                    demoted,
                )

    if tex_paths:
        tex_entries = _load_tex_bibliography_entries(tex_paths, issues, strict, logger)
        all_entries.extend(tex_entries)

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
    raw = entry.get("title", "") or ""
    return strip_braces(raw).lower().strip()

def safe_entry_id(entry: Dict, issues: IssueCollector, idx: int) -> str:
    key = entry.get("ID", "") or ""
    if not key:
        gen = f"__missing_key_{idx}__"
        issues.warn("validate_entry", "Entry has no BibTeX key (ID); generated placeholder key", generated_key=gen)
        return gen
    return key

def deduplicate_entries(entries: List[Dict], issues: IssueCollector) -> List[Dict]:
    """
    Deduplicate by normalised title.  Keep the first occurrence; record all
    keys in '__aliases__' (a set).
    """
    seen: Dict[str, int] = {}
    unique_entries: List[Dict] = []

    for i, e in enumerate(entries):
        bib_key = e.get("ID", "") or ""
        norm_key = normalize_title_key(e)
        if not norm_key:
            try:
                norm_key = format_entry(e)
            except Exception:
                norm_key = f"__unformattable_{i}__"
                issues.warn("dedup", "Entry missing title and could not be formatted; using placeholder dedup key",
                            bib_key=bib_key, index=i)

        if norm_key in seen:
            idx = seen[norm_key]
            if bib_key:
                unique_entries[idx]["__aliases__"].add(bib_key)
        else:
            new_entry = dict(e)
            new_entry["__aliases__"] = {bib_key} if bib_key else set()
            seen[norm_key] = len(unique_entries)
            unique_entries.append(new_entry)

    return unique_entries

def populate_aliases(entries: List[Dict], issues: IssueCollector):
    """Ensure every entry has __aliases__ set populated."""
    for i, e in enumerate(entries):
        key = e.get("ID", "") or ""
        if "__aliases__" in e and isinstance(e["__aliases__"], set):
            continue
        if not key:
            e["__aliases__"] = set()
            issues.warn("validate_entry", "Entry has no BibTeX key (ID); it will get a placeholder during lookup build",
                        index=i)
        else:
            e["__aliases__"] = {key}

def build_lookup(entries: List[Dict], start: int, issues: IssueCollector, strict: bool) -> Dict[str, Dict[str, Any]]:
    """Build {citekey: {id, citation}} mapping."""
    lookup: Dict[str, Dict[str, Any]] = {}
    used_keys: Set[str] = set()

    for n, entry in enumerate(entries, start=start):
        try:
            citation = format_entry(entry)
            if not citation:
                issues.warn("format_entry", "Formatted citation is empty",
                            entry_type=entry.get("ENTRYTYPE", ""), key=entry.get("ID", ""))
        except Exception as e:
            key_for_ctx = entry.get("ID", "") or ""
            issues.error("format_entry", "Failed to format entry; emitting error placeholder citation",
                         key=key_for_ctx, entry_type=entry.get("ENTRYTYPE", ""), error=str(e))
            if strict:
                raise
            citation = f"[FORMAT_ERROR] key={key_for_ctx or '<missing>'} type={entry.get('ENTRYTYPE','')} error={str(e)}"

        aliases: Set[str] = entry.get("__aliases__", set()) or set()

        if not aliases:
            gen = safe_entry_id(entry, issues, idx=n)
            aliases = {gen}

        record = {"id": n, "citation": citation}

        for key in aliases:
            if key in used_keys:
                issues.warn("build_lookup", "Duplicate cite key encountered; later mapping overwrites earlier",
                            key=key, id=n)
            used_keys.add(key)
            lookup[key] = record

    return lookup

def atomic_write_json(path: str, data: Dict[str, Any], issues: IssueCollector, strict: bool):
    """Write JSON via temp file then atomic replace."""
    out_dir = os.path.dirname(os.path.abspath(path)) or "."
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception as e:
        issues.error("write_json", "Failed to create output directory", dir=out_dir, error=str(e))
        if strict:
            raise
        return

    try:
        fd, tmp_path = tempfile.mkstemp(prefix=".tmp_citations_", suffix=".json", dir=out_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
    except Exception as e:
        issues.error("write_json", "Failed to write JSON output", out=path, error=str(e))
        if strict:
            raise


# -----------------------------
# CLI + main
# -----------------------------

def setup_logger(level: str) -> logging.Logger:
    logger = logging.getLogger("bib7714")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.handlers = [handler]
    logger.propagate = False
    return logger

def main():
    global LANG_TAIL

    ap = argparse.ArgumentParser(
        description=(
            "Convert .bib / .bbl files to a GB/T 7714-2015 JSON lookup dictionary. "
            "If no .bib files are provided (or all fail), the tool falls back to "
            "any .bbl files given via --bib."
        )
    )
    ap.add_argument(
        "--bib", required=True, nargs="+",
        help="One or more .bib and/or .bbl file paths",
    )
    ap.add_argument("--out", default="citations.json", help="Output JSON file path (default: citations.json)")
    ap.add_argument("--sort", choices=["file", "year", "key"], default="file",
                    help="Sorting mode applied before numbering")
    ap.add_argument("--dedup", action="store_true",
                    help="Deduplicate entries by normalised title")
    ap.add_argument("--start", type=int, default=1,
                    help="Starting numeric citation index (default: 1)")
    ap.add_argument("--lang", default="en", choices=["en", "zh", "ja", "fr", "de"],
                    help="Language for 'et al.' tail")
    ap.add_argument("--strict", action="store_true",
                    help="Stop on first error (default: continue best-effort)")
    ap.add_argument("--log", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                    help="Log level")

    args = ap.parse_args()
    logger = setup_logger(args.log)
    issues = IssueCollector()

    # Capture bibtexparser warnings/errors
    btp_logger = logging.getLogger("bibtexparser")
    btp_logger.setLevel(logging.WARNING)
    btp_logger.propagate = False
    btp_logger.handlers = []
    btp_logger.addHandler(ForwardToIssuesHandler(issues, stage="bibtexparser"))

    if args.start < 1:
        issues.error("args", "--start must be >= 1", start=args.start)
        issues.print_summary(logger)
        sys.exit(2)

    lang_map = {"en": "et al", "zh": "等", "ja": "他", "fr": "et al", "de": "u. a."}
    LANG_TAIL = lang_map.get(args.lang, "et al")

    # 1) Load — auto-detects .bib vs .bbl by extension
    try:
        entries = load_bib_entries(args.bib, issues=issues, strict=args.strict, logger=logger)
    except Exception:
        issues.print_summary(logger)
        sys.exit(1)

    if not entries:
        issues.error("pipeline", "No entries loaded from input files", files=args.bib)
        issues.print_summary(logger)
        sys.exit(1)

    # 2) Dedup / aliases
    try:
        if args.dedup:
            entries = deduplicate_entries(entries, issues=issues)
        else:
            populate_aliases(entries, issues=issues)
    except Exception as e:
        issues.error("pipeline", "Failed during dedup/alias stage", error=str(e))
        if args.strict:
            issues.print_summary(logger)
            sys.exit(1)

    # 3) Sort
    try:
        entries = sort_entries(entries, args.sort)
    except Exception as e:
        issues.error("pipeline", "Failed during sort stage", mode=args.sort, error=str(e))
        if args.strict:
            issues.print_summary(logger)
            sys.exit(1)

    # 4) Build lookup
    try:
        lookup = build_lookup(entries, start=args.start, issues=issues, strict=args.strict)
    except Exception:
        issues.print_summary(logger)
        sys.exit(1)

    # 5) Write output
    try:
        atomic_write_json(args.out, lookup, issues=issues, strict=args.strict)
    except Exception:
        issues.print_summary(logger)
        sys.exit(1)

    logger.info(
        "Wrote %d citation keys (%d unique entries) -> %s",
        len(lookup), len(entries), args.out,
    )
    issues.print_summary(logger)
    sys.exit(1 if issues.has_errors() else 0)


if __name__ == "__main__":
    main()
