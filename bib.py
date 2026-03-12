#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Convert BibTeX (.bib) to a GB/T 7714-2015 (numeric) reference list,
and export a structured JSON lookup dictionary for downstream use.

Install:
  pip install bibtexparser

Usage:
  python bib.py --bib citations1.bib citations2.bib --out citations.json
  python bib.py --bib citations1.bib citations2.bib --out citations.json --sort year --dedup

Extras:
  --strict   Stop on first error (default: best-effort, continue)
  --log      Log level: DEBUG/INFO/WARNING/ERROR (default: INFO)
"""

import argparse
import json
import logging
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from typing import List, Dict, Set, Tuple, Optional, Any

import bibtexparser
from bibtexparser.bparser import BibTexParser


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
            # Print compact, but with enough context to debug.
            ctx = ", ".join(f"{k}={v}" for k, v in it.context.items() if v not in ("", None, [], {}, set()))
            logger.log(logging.ERROR if it.level == "ERROR" else logging.WARNING,
                       "[%s] %s: %s%s",
                       it.level, it.stage, it.message, (f" ({ctx})" if ctx else ""))

class ForwardToIssuesHandler(logging.Handler):
    """
    Forward selected log records into IssueCollector, so we can show them in issue summary.
    """
    def __init__(self, issues: IssueCollector, stage: str = "bibtexparser"):
        super().__init__()
        self.issues = issues
        self.stage = stage

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            level = record.levelno
            # Only collect WARNING/ERROR/CRITICAL (INFO/DEBUG are usually too noisy)
            if level >= logging.ERROR:
                self.issues.error(self.stage, msg, logger=record.name)
            elif level >= logging.WARNING:
                self.issues.warn(self.stage, msg, logger=record.name)
        except Exception:
            # Never break pipeline because of logging issues
            pass


# -----------------------------
# Core formatting helpers
# -----------------------------

LANG_TAIL = "et al"  # will be overridden by CLI

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

    # Pragmatic: treat arXiv/preprint as [EB/OL]
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


# -----------------------------
# IO + pipeline steps
# -----------------------------

def load_bib_entries(paths: List[str], issues: IssueCollector, strict: bool, logger: logging.Logger) -> List[Dict]:
    """Load and merge entries from one or more .bib files with robust error reporting."""
    all_entries: List[Dict] = []
    for path in paths:
        try:
            if not os.path.exists(path):
                raise FileNotFoundError(path)
            with open(path, "r", encoding="utf-8") as f:
                try:
                    parser = BibTexParser(ignore_nonstandard_types=False)
                    bibdb = bibtexparser.load(f, parser=parser)
                except Exception as e:
                    # Parsing errors often happen here
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
    """Return entry ID; if missing, generate a stable placeholder and report."""
    key = entry.get("ID", "") or ""
    if not key:
        gen = f"__missing_key_{idx}__"
        issues.warn("validate_entry", "Entry has no BibTeX key (ID); generated placeholder key", generated_key=gen)
        return gen
    return key

def deduplicate_entries(entries: List[Dict], issues: IssueCollector) -> List[Dict]:
    """
    Deduplicate by normalized title (or by formatted reference if title missing).
    Keep the first occurrence; record all keys in '__aliases__' (a set).
    """
    seen: Dict[str, int] = {}
    unique_entries: List[Dict] = []

    for i, e in enumerate(entries):
        bib_key = e.get("ID", "") or ""
        norm_key = normalize_title_key(e)
        if not norm_key:
            # fallback to formatted reference; may still be empty if broken
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
    """Even without dedup, ensure every entry has __aliases__ set populated."""
    for i, e in enumerate(entries):
        key = e.get("ID", "") or ""
        if "__aliases__" in e and isinstance(e["__aliases__"], set):
            continue
        if not key:
            # don't fabricate here; safe_entry_id() will handle in build_lookup
            e["__aliases__"] = set()
            issues.warn("validate_entry", "Entry has no BibTeX key (ID); it will get a placeholder during lookup build",
                        index=i)
        else:
            e["__aliases__"] = {key}

def build_lookup(entries: List[Dict], start: int, issues: IssueCollector, strict: bool) -> Dict[str, Dict[str, Any]]:
    """
    Build {citekey: {id, citation}} mapping.
    Any missing citekey gets a generated placeholder so downstream lookup is still possible.
    """
    lookup: Dict[str, Dict[str, Any]] = {}
    used_keys: Set[str] = set()

    for n, entry in enumerate(entries, start=start):
        # Format entry safely
        try:
            citation = format_entry(entry)
            if not citation:
                issues.warn("format_entry", "Formatted citation is empty", entry_type=entry.get("ENTRYTYPE", ""), key=entry.get("ID", ""))
        except Exception as e:
            key_for_ctx = entry.get("ID", "") or ""
            issues.error("format_entry", "Failed to format entry; emitting error placeholder citation",
                         key=key_for_ctx, entry_type=entry.get("ENTRYTYPE", ""), error=str(e))
            if strict:
                raise
            # Best-effort placeholder
            citation = f"[FORMAT_ERROR] key={key_for_ctx or '<missing>'} type={entry.get('ENTRYTYPE','')} error={str(e)}"

        aliases: Set[str] = entry.get("__aliases__", set()) or set()

        # If aliases empty (e.g., missing ID), generate one
        if not aliases:
            gen = safe_entry_id(entry, issues, idx=n)
            aliases = {gen}

        record = {"id": n, "citation": citation}

        for key in aliases:
            if key in used_keys:
                # This is a real problem for downstream; report it.
                issues.warn("build_lookup", "Duplicate cite key encountered; later mapping overwrites earlier",
                            key=key, id=n)
            used_keys.add(key)
            lookup[key] = record

    return lookup

def atomic_write_json(path: str, data: Dict[str, Any], issues: IssueCollector, strict: bool):
    """Write JSON via temp file then atomic replace to avoid partial/corrupt outputs."""
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
            os.replace(tmp_path, path)  # atomic on most OS
        finally:
            # If replace failed, cleanup temp
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
        description="Convert .bib files to a GB/T 7714-2015 JSON lookup dictionary."
    )
    ap.add_argument("--bib", required=True, nargs="+", help="One or more input .bib file paths")
    ap.add_argument("--out", default="citations.json", help="Output JSON file path (default: citations.json)")
    ap.add_argument("--sort", choices=["file", "year", "key"], default="file", help="Sorting mode applied before numbering")
    ap.add_argument("--dedup", action="store_true", help="Deduplicate entries by normalized title")
    ap.add_argument("--start", type=int, default=1, help="Starting numeric citation index (default: 1)")
    ap.add_argument("--lang", default="en", choices=["en", "zh", "ja", "fr", "de"], help="Language for 'et al.' tail")
    ap.add_argument("--strict", action="store_true", help="Stop on first error (default: continue best-effort)")
    ap.add_argument("--log", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Log level")

    args = ap.parse_args()
    logger = setup_logger(args.log)
    issues = IssueCollector()

    # --- Capture bibtexparser warnings/errors into our IssueCollector ---
    btp_logger = logging.getLogger("bibtexparser")
    btp_logger.setLevel(logging.WARNING)      # only collect warnings+
    btp_logger.propagate = False              # stop it from going to root handlers
    btp_logger.handlers = []                  # remove default handlers (prevents naked prints)
    btp_logger.addHandler(ForwardToIssuesHandler(issues, stage="bibtexparser"))

    # Validate args early
    if args.start < 1:
        issues.error("args", "--start must be >= 1", start=args.start)
        issues.print_summary(logger)
        sys.exit(2)

    # Set language-specific 'et al'
    lang_map = {
        "en": "et al",
        "zh": "等",
        "ja": "他",
        "fr": "et al",
        "de": "u. a.",
    }
    LANG_TAIL = lang_map.get(args.lang, "et al")

    # 1) Load
    try:
        entries = load_bib_entries(args.bib, issues=issues, strict=args.strict, logger=logger)
    except Exception:
        # strict mode likely raised; summary first
        issues.print_summary(logger)
        sys.exit(1)

    if not entries:
        issues.error("pipeline", "No entries loaded from input .bib files", bib=args.bib)
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

    # Done
    logger.info("Wrote %d citation keys (%d unique entries) -> %s", len(lookup), len(entries), args.out)

    # Print issue summary and decide exit code
    issues.print_summary(logger)
    if issues.has_errors():
        # Non-zero to make CI/pipelines catch it
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()