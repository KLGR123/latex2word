#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
chunk.py

Chunk one or more LaTeX .tex files into paragraph-like blocks and export JSON.

What it does
------------
For each input .tex file:
1) Read file content (best-effort unless --strict).
2) Optionally extract \\title{...} (when --include-title).
3) Extract the document body between:
     \\begin{document} ... \\end{document}
4) Chunk the document into paragraphs using blank lines as separators, with extra
   paragraph breaks inserted around:
   - Sectioning commands (\\section, \\subsection, ...)
   - Some block environments (abstract/figure/table/equation/itemize/...)
5) Remove non-content chunks (e.g., pure \\cite{...} / \\label{...} lines).
6) Enforce: "\\label{...}" must NOT start a paragraph.
   If a chunk begins with one or more leading \\label{...}, those label commands
   are merged into the previous chunk.
7) Chapter is derived from the parent folder name of each --tex path.
   Example:
     /Users/.../inputs/7/iclr2022_conference.tex  -> chapter = 7
   The parent folder name MUST be a number; otherwise error.

Output JSON schema
------------------
{
  "documents": [
    {
      "tex": "path/to/file.tex",
      "paragraphs": [
        {"id": 1, "text": "..."},
        {"id": 2, "text": "..."}
      ],
      "chapter": 7,
      "title": "..."          # only if --include-title
    }
  ]
}

Usage
-----
  python3 chunk.py --tex a.tex b.tex --out chunks.json
  python3 chunk.py --tex a.tex --out chunks.json --include-title
  python3 chunk.py --tex a.tex --out chunks.json --keep-commands
  python3 chunk.py --tex a.tex --out chunks.json --split-on-forced-linebreak
  python3 chunk.py --tex a.tex --out chunks.json --strict

Notes / limitations
-------------------
- This is a pragmatic regex-based chunker, not a full TeX parser.
- It does not expand \\input/\\include; run your preprocessor first if needed.
"""

import argparse
import json
import os
import re
import sys
from typing import Dict, List, Optional, Any


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


# -----------------------------
# IO helpers
# -----------------------------

def read_text(path: str, encoding: str, strict: bool) -> str:
    try:
        with open(path, "r", encoding=encoding) as f:
            return f.read()
    except FileNotFoundError:
        eprint(f"[ERROR] Input file not found: {path}")
        if strict:
            raise
        return ""
    except UnicodeDecodeError as ex:
        eprint(f"[ERROR] Failed to decode file as {encoding}: {path} ({ex})")
        if strict:
            raise
        return ""
    except Exception as ex:
        eprint(f"[ERROR] Failed to read file: {path} ({ex})")
        if strict:
            raise
        return ""

def atomic_write_json(path: str, data: Any, encoding: str = "utf-8", strict: bool = False) -> bool:
    out_dir = os.path.dirname(os.path.abspath(path)) or "."
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception as ex:
        eprint(f"[ERROR] Cannot create output directory: {out_dir} ({ex})")
        if strict:
            raise
        return False

    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding=encoding) as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
        return True
    except Exception as ex:
        eprint(f"[ERROR] Failed to write output JSON: {path} ({ex})")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        if strict:
            raise
        return False


# -----------------------------
# Label merging (paragraph hygiene)
# -----------------------------

LABEL_LINE_RE = re.compile(r"^\s*\\label\{[^}]*\}\s*(.*)$", flags=re.DOTALL)

def merge_leading_labels(chunks: List[str]) -> List[str]:
    """
    If a chunk begins with \\label{...}, merge that \\label{...} into the previous chunk.
    Supports multiple leading labels in a row.
    This prevents \\label from being the first token of a paragraph.
    """
    merged: List[str] = []

    for c in chunks:
        s = c.lstrip()

        while True:
            m = LABEL_LINE_RE.match(s)
            if not m:
                break

            m2 = re.match(r"^\s*(\\label\{[^}]*\})\s*", s)
            label_cmd = m2.group(1) if m2 else "\\label{}"
            rest = s[m2.end():] if m2 else m.group(1)

            if merged:
                merged[-1] = merged[-1].rstrip() + "\n" + label_cmd
            else:
                merged.append(label_cmd)

            s = rest.lstrip()

        if s.strip():
            merged.append(s.strip())

    return merged


# -----------------------------
# LaTeX extraction / chunking
# -----------------------------

TITLE_PATTERNS = [
    r"\\title\{(.+?)\}",
    r"\\title\s*\[(.*?)\]\s*\{(.+?)\}",  # \title[short]{long}
]
DOC_PATTERN = r"\\begin\{document\}(.*?)\\end\{document\}"

SECTION_CMD_RE = re.compile(
    r"^(\\(part|chapter|section|subsection|subsubsection|paragraph|subparagraph)\*?\{.*?\})\s*$",
    flags=re.MULTILINE
)
ENV_BEGIN_RE = re.compile(r"\\begin\{([a-zA-Z*]+)\}")
ENV_END_RE = re.compile(r"\\end\{([a-zA-Z*]+)\}")

def normalize_newlines(s: str) -> str:
    return s.replace("\r\n", "\n").replace("\r", "\n")

def extract_title(tex: str) -> Optional[str]:
    for pat in TITLE_PATTERNS:
        m = re.search(pat, tex, flags=re.DOTALL)
        if m:
            grp = m.group(m.lastindex or 1)
            return grp.strip()
    return None

def extract_document(tex: str) -> Optional[str]:
    m = re.search(DOC_PATTERN, tex, flags=re.DOTALL)
    if not m:
        return None
    return m.group(1)

def remove_comments(tex: str) -> str:
    """Remove LaTeX comments starting with unescaped %; keep \\%."""
    out_lines = []
    for line in tex.splitlines():
        m = re.search(r"(?<!\\)%", line)
        if m:
            line = line[:m.start()]
        out_lines.append(line)
    return "\n".join(out_lines)

def strip_outer_space(s: str) -> str:
    return "\n".join([ln.rstrip() for ln in s.splitlines()]).strip()

def drop_common_preamble_commands(doc: str) -> str:
    doc = re.sub(r"^\s*\\(maketitle|tableofcontents)\s*$", "", doc, flags=re.MULTILINE)
    return doc

def insert_breaks_before_sections(doc: str) -> str:
    lines = doc.splitlines()
    out = []
    for ln in lines:
        if SECTION_CMD_RE.match(ln.strip()):
            if out and out[-1].strip() != "":
                out.append("")
            out.append(ln)
            out.append("")
        else:
            out.append(ln)
    return "\n".join(out)

def insert_breaks_for_some_environments(doc: str) -> str:
    block_envs = {
        "abstract", "figure", "table", "equation", "align", "align*", "gather", "gather*",
        "itemize", "enumerate", "description", "algorithm", "algorithm*", "lstlisting", "verbatim"
    }
    lines = doc.splitlines()
    out = []
    for ln in lines:
        b = ENV_BEGIN_RE.search(ln)
        e = ENV_END_RE.search(ln)
        if b:
            env = b.group(1)
            if env in block_envs:
                if out and out[-1].strip() != "":
                    out.append("")
                out.append(ln)
                continue
        if e:
            env = e.group(1)
            if env in block_envs:
                out.append(ln)
                out.append("")
                continue
        out.append(ln)
    return "\n".join(out)

def collapse_blank_lines(doc: str) -> str:
    return re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", doc)

def is_noncontent_chunk(chunk: str) -> bool:
    s = chunk.strip()
    if not s:
        return True
    if re.fullmatch(r"\\(label|ref|cite|citep|citet|vspace|hspace|smallskip|medskip|bigskip)\b.*", s):
        return True
    return False

def chunk_document(
    doc: str,
    keep_commands: bool = False,
    split_on_forced_linebreak: bool = False
) -> List[str]:
    doc = normalize_newlines(doc)
    doc = remove_comments(doc)
    doc = drop_common_preamble_commands(doc)

    doc = insert_breaks_before_sections(doc)
    doc = insert_breaks_for_some_environments(doc)

    if split_on_forced_linebreak:
        doc = re.sub(r"\\\\\s*\n", "\n\n", doc)

    doc = collapse_blank_lines(doc)
    doc = strip_outer_space(doc)

    raw_chunks = [c.strip() for c in re.split(r"\n\s*\n", doc) if c.strip()]

    chunks: List[str] = []
    for c in raw_chunks:
        if not keep_commands:
            if re.fullmatch(r"\\(part|chapter|section|subsection|subsubsection|paragraph|subparagraph)\*?\{.*?\}", c.strip()):
                continue
        if is_noncontent_chunk(c):
            continue
        chunks.append(c)

    return merge_leading_labels(chunks)


# -----------------------------
# Chapter from parent folder
# -----------------------------

def resolve_chapter_from_parent(tex_path: str) -> int:
    """
    Chapter is the parent folder name of the tex file, interpreted as int.
    Example: /.../inputs/7/iclr2022_conference.tex -> 7
    """
    parent = os.path.basename(os.path.dirname(os.path.abspath(tex_path)))
    if not parent.isdigit():
        raise ValueError(f"Parent folder name is not numeric: {parent}")
    return int(parent)


# -----------------------------
# Main
# -----------------------------

def main():
    ap = argparse.ArgumentParser(description="Chunk LaTeX .tex files and output JSON.")
    ap.add_argument("--tex", required=True, nargs="+", help="One or more input .tex file paths")
    ap.add_argument("--out", required=True, help="Output JSON file path")
    ap.add_argument("--encoding", default="utf-8", help="Input/output encoding (default: utf-8)")
    ap.add_argument("--include-title", action="store_true", help="Include extracted title in output JSON")
    ap.add_argument("--keep-commands", action="store_true", help="Keep pure command chunks like \\section{...}")
    ap.add_argument(
        "--split-on-forced-linebreak",
        action="store_true",
        help="Treat LaTeX '\\\\' as paragraph breaks (useful for some templates)",
    )
    ap.add_argument("--strict", action="store_true", help="Stop on first error")
    args = ap.parse_args()

    results: List[Dict[str, Any]] = []
    total_chunks = 0
    failed = 0

    for tex_path in args.tex:
        tex = read_text(tex_path, args.encoding, strict=args.strict)
        if not tex:
            failed += 1
            continue

        title = ""
        if args.include_title:
            try:
                title = extract_title(tex) or ""
            except Exception as ex:
                eprint(f"[WARNING] Failed to extract title from {tex_path}: {ex}")
                if args.strict:
                    raise
                title = ""

        try:
            doc = extract_document(tex)
        except Exception as ex:
            eprint(f"[ERROR] Failed to extract document env from {tex_path}: {ex}")
            if args.strict:
                raise
            failed += 1
            continue

        if not doc:
            eprint(f"[ERROR] No document content found in {tex_path} (missing \\begin{{document}}...\\end{{document}})")
            failed += 1
            if args.strict:
                sys.exit(1)
            continue

        try:
            chunks = chunk_document(
                doc,
                keep_commands=args.keep_commands,
                split_on_forced_linebreak=args.split_on_forced_linebreak,
            )
        except Exception as ex:
            eprint(f"[ERROR] Chunking failed for {tex_path}: {ex}")
            failed += 1
            if args.strict:
                raise
            continue

        try:
            chapter_val = resolve_chapter_from_parent(tex_path)
        except Exception as ex:
            eprint(f"[ERROR] Invalid chapter folder for {tex_path}: {ex}")
            failed += 1
            if args.strict:
                sys.exit(1)
            continue

        paragraphs = [{"id": i + 1, "text": c} for i, c in enumerate(chunks)]
        total_chunks += len(paragraphs)

        record: Dict[str, Any] = {
            "tex": tex_path,
            "paragraphs": paragraphs,
            "chapter": chapter_val,
        }
        if args.include_title:
            record["title"] = title

        results.append(record)

    payload = {"documents": results}

    ok = atomic_write_json(args.out, payload, encoding=args.encoding, strict=args.strict)
    if not ok:
        sys.exit(1)

    print(f"[INFO] Wrote {len(results)} document(s), {total_chunks} chunk(s) -> {args.out}")
    if failed:
        print(f"[WARNING] Failed documents: {failed}", file=sys.stderr)
        sys.exit(1 if args.strict else 0)


if __name__ == "__main__":
    main()