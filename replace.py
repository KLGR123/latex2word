#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
replace.py

Post-process labeled.json by resolving LaTeX cross-reference and citation
commands into their final display forms:

  Citation commands (\\cite, \\citep, \\citet, ...)  ->  [N]  (numeric, superscript-ready)
  Reference commands (\\ref, \\eqref, \\autoref, \\cref, \\Cref, \\pageref, \\nameref, ...)
                                                     ->  Chinese label from refmap
  \\hyperref[label]{text}                            ->  Chinese label from refmap

Replacements are applied to both the "text" and "translation" fields of every
paragraph.  Keys not found in citations.json or refmap.json are reported on
stderr and replaced with a configurable placeholder (default: [未找到]).

Lookup for refmap is scoped to the chapter of each document.  If the key is
absent from the chapter-level map, a global scan across all chapters is
attempted as a fallback, and a warning is emitted.

Input files
-----------
  labeled.json    -- output of label.py
  citations.json  -- output of bib.py   ({citekey: {"id": N, "citation": "..."}})
  refmap.json     -- output of refmap.py ({"chapter": {"label_key": "env_label"}})

Output
------
  replaced.json   -- same schema as labeled.json, with commands substituted

Usage
-----
  python3 replace.py
  python3 replace.py \\
      --labeled  outputs/labeled.json \\
      --citations outputs/citations.json \\
      --refmap   outputs/refmap.json \\
      --output   outputs/replaced.json
  python3 replace.py --placeholder "[?]"
  python3 replace.py --verbose
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Compiled regex patterns
# ---------------------------------------------------------------------------

# Citation commands.
# Handles optional pre/post note args:  \citep[pre][post]{key1, key2}
# Capture group 1: raw comma-separated key string (whitespace stripped by us).
_CITE_RE = re.compile(
    r"\\cite[a-zA-Z*]*"      # command name: \cite \citep \citet \citealt ...
    r"(?:\s*\[[^\]]*\])*"    # zero or more optional args [...]
    r"\s*\{\s*([^}]+?)\s*\}" # {key1, key2, ...}
)

# Standard single-argument reference commands.
# Capture group 1: command name (for context in warnings).
# Capture group 2: label key.
_REF_RE = re.compile(
    r"\\(ref|eqref|autoref|cref|Cref|pageref|nameref|vref|Vref|fref)\*?"
    r"\s*\{\s*([^}]+?)\s*\}"
)

# \hyperref[label]{display_text}  --  label is in [...], not {...}.
# Capture group 1: label key.
_HYPERREF_RE = re.compile(
    r"\\hyperref\s*\[\s*([^\]]+?)\s*\]\s*\{[^}]*\}"
)


# ---------------------------------------------------------------------------
# Missing-key tracker
# ---------------------------------------------------------------------------

class MissingKeyTracker:
    """Accumulates missing citation / reference key reports per document."""

    def __init__(self) -> None:
        # {(doc_tex, key_type, key): count}
        self._counts: Dict[Tuple[str, str, str], int] = defaultdict(int)

    def record(self, doc_tex: str, key_type: str, key: str) -> None:
        self._counts[(doc_tex, key_type, key)] += 1

    def print_summary(self) -> None:
        if not self._counts:
            return
        total = sum(self._counts.values())
        print(
            f"[WARN] {total} unreplaced reference(s) detected:",
            file=sys.stderr,
        )
        for (doc, key_type, key), count in sorted(self._counts.items()):
            suffix = f" (x{count})" if count > 1 else ""
            print(
                f"       [{key_type}] key={key!r}  in  {doc}{suffix}",
                file=sys.stderr,
            )


# ---------------------------------------------------------------------------
# Replacement helpers
# ---------------------------------------------------------------------------

def _replace_cite_keys(
    raw_keys: str,
    citations: Dict[str, Any],
    placeholder: str,
    doc_tex: str,
    tracker: MissingKeyTracker,
    verbose: bool,
) -> str:
    """
    Given a comma-separated string of BibTeX keys (as captured from the
    curly-brace argument), return the numeric citation string [N1,N2,...].
    """
    keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
    ids: List[str] = []
    for key in keys:
        entry = citations.get(key)
        if entry is None:
            tracker.record(doc_tex, "cite", key)
            if verbose:
                print(
                    f"[WARN] citation key not found: {key!r}  ({doc_tex})",
                    file=sys.stderr,
                )
            ids.append(placeholder)
        else:
            ids.append(str(entry["id"]))
    return "\\textsuperscript{[" + ",".join(ids) + "]}"


def _replace_ref_key(
    key: str,
    chapter_str: str,
    refmap: Dict[str, Dict[str, str]],
    placeholder: str,
    doc_tex: str,
    tracker: MissingKeyTracker,
    verbose: bool,
    cmd: str = "ref",
) -> str:
    """
    Look up a LaTeX label key in refmap, first in the document's own chapter,
    then falling back to a global scan across all chapters.

    Returns the Chinese env_label string, or placeholder on failure.
    """
    chapter_map = refmap.get(chapter_str, {})
    label = chapter_map.get(key)
    if label is not None:
        return label

    # Global fallback scan
    for other_chapter, mapping in refmap.items():
        if other_chapter == chapter_str:
            continue
        label = mapping.get(key)
        if label is not None:
            if verbose:
                print(
                    f"[WARN] \\{cmd}{{{key}}} replaced via fallback "
                    f"chapter {other_chapter} (expected {chapter_str})  ({doc_tex})",
                    file=sys.stderr,
                )
            return label

    tracker.record(doc_tex, cmd, key)
    if verbose:
        print(
            f"[WARN] reference key not found: {key!r}  (\\{cmd}, chapter {chapter_str}, {doc_tex})",
            file=sys.stderr,
        )
    return placeholder


# ---------------------------------------------------------------------------
# Per-field text processing
# ---------------------------------------------------------------------------

def replace_field(
    text: str,
    chapter_str: str,
    doc_tex: str,
    citations: Dict[str, Any],
    refmap: Dict[str, Dict[str, str]],
    placeholder: str,
    tracker: MissingKeyTracker,
    verbose: bool,
) -> str:
    """
    Replace all citation and reference commands in a single text field.

    Processing order:
      1. \\hyperref[label]{text}   (must come before generic ref pass)
      2. Standard \\ref-family commands
      3. Citation commands

    Surrounding whitespace / newlines adjacent to the matched command are
    collapsed: we replace the full match (which may already include
    whitespace inside the argument) and the regex patterns already strip
    inner whitespace via \\s* around the captured key.
    """
    if not text:
        return text

    # --- 1. \hyperref[label]{text} ---
    def _hyperref_sub(m: re.Match) -> str:
        key = m.group(1).strip()
        return _replace_ref_key(
            key, chapter_str, refmap, placeholder, doc_tex, tracker, verbose,
            cmd="hyperref",
        )

    text = _HYPERREF_RE.sub(_hyperref_sub, text)

    # --- 2. Standard \ref-family ---
    def _ref_sub(m: re.Match) -> str:
        cmd = m.group(1)
        raw = m.group(2).strip()
        # \Cref / \cref etc. support comma-separated multiple keys
        keys = [k.strip() for k in raw.split(",") if k.strip()]
        replaced_parts = [
            _replace_ref_key(
                k, chapter_str, refmap, placeholder, doc_tex, tracker, verbose,
                cmd=cmd,
            )
            for k in keys
        ]
        replaced = "、".join(replaced_parts)
        if cmd == "eqref" and not re.search(r"[（(【\[]", replaced):
            return f"（{replaced}）"
        return replaced

    text = _REF_RE.sub(_ref_sub, text)

    # --- 3. Citation commands ---
    def _cite_sub(m: re.Match) -> str:
        raw_keys = m.group(1)
        return _replace_cite_keys(
            raw_keys, citations, placeholder, doc_tex, tracker, verbose,
        )

    text = _CITE_RE.sub(_cite_sub, text)

    return text


# ---------------------------------------------------------------------------
# Document-level processing
# ---------------------------------------------------------------------------

def replace_documents(
    documents: List[Dict],
    citations: Dict[str, Any],
    refmap: Dict[str, Dict[str, str]],
    placeholder: str,
    verbose: bool,
) -> Tuple[List[Dict], MissingKeyTracker]:
    """
    Walk all documents/paragraphs and apply citation + reference resolution
    to both the "text" and "translation" fields.

    Returns a deep-copied, replaced document list and the missing-key tracker.
    """
    docs_out = deepcopy(documents)
    tracker = MissingKeyTracker()

    for doc in docs_out:
        chapter_str = str(doc.get("chapter", ""))
        doc_tex = doc.get("tex", "<unknown>")

        for para in doc.get("paragraphs", []):
            for field in ("text", "translation"):
                raw = para.get(field)
                if not isinstance(raw, str):
                    continue
                para[field] = replace_field(
                    raw,
                    chapter_str=chapter_str,
                    doc_tex=doc_tex,
                    citations=citations,
                    refmap=refmap,
                    placeholder=placeholder,
                    tracker=tracker,
                    verbose=verbose,
                )

    return docs_out, tracker


# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------

def load_json(path: str, label: str) -> Any:
    if not os.path.exists(path):
        print(f"[ERROR] {label} file not found: {path}", file=sys.stderr)
        sys.exit(1)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        print(f"[ERROR] Invalid JSON in {label} ({path}): {exc}", file=sys.stderr)
        sys.exit(1)


def write_json_atomic(path: str, data: Any) -> None:
    out_dir = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(out_dir, exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as exc:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        print(f"[ERROR] Failed to write output: {exc}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=(
            "Resolve LaTeX \\cite / \\ref commands in labeled.json into "
            "numeric citations [N] and Chinese cross-reference labels."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument(
        "--labeled",
        default=os.path.join("outputs", "labeled.json"),
        help="Path to labeled.json (default: outputs/labeled.json)",
    )
    ap.add_argument(
        "--citations",
        default=os.path.join("outputs", "citations.json"),
        help="Path to citations.json from bib.py (default: outputs/citations.json)",
    )
    ap.add_argument(
        "--refmap",
        default=os.path.join("outputs", "refmap.json"),
        help="Path to refmap.json from refmap.py (default: outputs/refmap.json)",
    )
    ap.add_argument(
        "--output",
        default=os.path.join("outputs", "replaced.json"),
        help="Path to output replaced.json (default: outputs/replaced.json)",
    )
    ap.add_argument(
        "--placeholder",
        default="[未找到]",
        help=(
            "Replacement string for keys not found in citations / refmap "
            "(default: [未找到])"
        ),
    )
    ap.add_argument(
        "--verbose",
        action="store_true",
        help="Print a warning for every individual missing key as it is encountered.",
    )
    return ap.parse_args()


def main() -> None:
    args = parse_args()

    # --- Load inputs ---
    data = load_json(args.labeled, "labeled")
    documents = data.get("documents", [])
    if not documents:
        print("[WARNING] No documents found in labeled.json.", file=sys.stderr)

    citations: Dict[str, Any] = load_json(args.citations, "citations")
    refmap: Dict[str, Dict[str, str]] = load_json(args.refmap, "refmap")

    total_paras = sum(len(d.get("paragraphs", [])) for d in documents)
    print(
        f"[INFO] Loaded {len(documents)} document(s), {total_paras} paragraph(s)."
    )
    print(f"[INFO] Citations index: {len(citations)} key(s).")
    total_ref_keys = sum(len(v) for v in refmap.values())
    print(
        f"[INFO] Refmap: {len(refmap)} chapter(s), {total_ref_keys} label key(s)."
    )

    # --- Resolve ---
    docs_out, tracker = replace_documents(
        documents,
        citations=citations,
        refmap=refmap,
        placeholder=args.placeholder,
        verbose=args.verbose,
    )

    # --- Write output ---
    write_json_atomic(args.output, {"documents": docs_out})
    print(f"[INFO] Output written to: {args.output}")

    # --- Report missing keys ---
    tracker.print_summary()


if __name__ == "__main__":
    main()