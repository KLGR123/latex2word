#!/usr/bin/env python3
r"""
tex.py

Given one or more folder paths, preprocess LaTeX projects that have multiple .tex files:
1) Detect TeX inclusion commands (e.g., \input{...}, \include{...}, \subfile{...}, \import{...}{...}, \subimport{...}{...})
2) Inline included .tex content into the including file (recursively).
3) Pick a "main" .tex file (prefer one containing both \title and \author; fallback to \begin{document}).
4) Warn about leftover .tex files that were never inlined/used.
5) Move all non-main .tex files into a newly created ./preprocessed/ folder, leaving only the main .tex in the folder root.

Notes / limitations:
- This is a pragmatic text-based preprocessor, not a full TeX parser.
- It skips lines where inclusion commands appear only in comments (best-effort).
- It will not resolve complex macro-generated filenames or conditional includes.

Usage:
  python3 tex.py --folders examples/WEPO examples/SMITH examples/CogniWeb
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from macro import expand_defined_macros

try:
    from strip import strip_formatting as _strip_formatting
    _STRIP_FMT_AVAILABLE = True
except ImportError:
    _STRIP_FMT_AVAILABLE = False

# ----------------------------
# LaTeX inclusion syntaxes supported
# ----------------------------
# Common ways TeX references other .tex files:
#   \input{file}         (TeX primitive)
#   \include{file}       (LaTeX; typically adds page breaks)
#   \subfile{file}       (subfiles package)
#   \import{dir/}{file}  (import package)
#   \subimport{dir/}{file} (import package)
#
# Other related syntaxes (not strictly tex includes) exist, e.g. \includeonly{...},
# but they do not directly include content at the point of use. We do not expand those.
#
# Pattern notes:
# - We accept optional spaces: \input { foo }
# - We accept with or without ".tex" extension.
# - For \import/\subimport we take two brace args.
SINGLE_ARG_CMDS = ("input", "include", "subfile")
DOUBLE_ARG_CMDS = ("import", "subimport")

SINGLE_ARG_RE = re.compile(
    r"""\\(?P<cmd>input|include|subfile)\s*\{\s*(?P<arg>[^}]+?)\s*\}""",
    re.VERBOSE,
)
DOUBLE_ARG_RE = re.compile(
    r"""\\(?P<cmd>import|subimport)\s*\{\s*(?P<dir>[^}]+?)\s*\}\s*\{\s*(?P<arg>[^}]+?)\s*\}""",
    re.VERBOSE,
)


@dataclass
class InlineResult:
    text: str
    used_files: Set[Path]  # files that were inlined (directly or indirectly)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Inline multi-tex LaTeX folders into a single main .tex file.")
    p.add_argument(
        "--folders",
        nargs="+",
        required=True,
        help="One or more folder paths containing .tex files.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not write or move files; only print what would happen.",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Verbose logging.",
    )
    p.add_argument(
        "--strip-formatting",
        action="store_true",
        dest="strip_formatting",
        help=(
            "After inlining, strip LaTeX formatting commands (\\textbf, \\textit, "
            "\\textcolor, \\hspace, etc.) from the merged main .tex file while "
            "preserving wrapped content. Requires strip.py alongside tex.py."
        ),
    )
    return p.parse_args()


# ----------------------------
# Utilities
# ----------------------------
def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def vget(verbose: bool, msg: str) -> None:
    if verbose:
        log(msg)


def find_unescaped_percent(line: str) -> int:
    """
    Return the index of the first unescaped '%' in the line, or -1 if none.
    This is a best-effort comment detector for LaTeX.
    """
    i = 0
    while i < len(line):
        if line[i] == "%":
            # Count backslashes immediately before '%'
            bs = 0
            j = i - 1
            while j >= 0 and line[j] == "\\":
                bs += 1
                j -= 1
            if bs % 2 == 0:  # even number means '%' is not escaped
                return i
        i += 1
    return -1


def normalize_tex_name(name: str) -> str:
    """
    Normalize an include argument to a file name:
    - Strip surrounding whitespace
    - Remove wrapping quotes if present
    - Ensure ends with .tex
    """
    n = name.strip().strip('"').strip("'")
    if not n.lower().endswith(".tex"):
        n += ".tex"
    return n


def resolve_include_path(
    including_file: Path,
    include_cmd: str,
    arg: str,
    dir_arg: Optional[str],
    folder_root: Path,
) -> Optional[Path]:
    """
    Resolve an included .tex file path.
    Priority:
    1) Relative to including file directory
    2) Relative to folder root
    3) As a literal path (relative/absolute), if it exists
    """
    base_dir = including_file.parent

    if include_cmd in DOUBLE_ARG_CMDS:
        # \import{dir}{file} uses 'dir' as a path prefix (often with trailing slash)
        dir_part = (dir_arg or "").strip()
        # Don't normalize slashes too aggressively; Path handles.
        candidate_rel = Path(dir_part) / normalize_tex_name(arg)
        candidates = [
            (base_dir / candidate_rel),
            (folder_root / candidate_rel),
            candidate_rel,  # relative to CWD
        ]
    else:
        candidate_rel = Path(normalize_tex_name(arg))
        candidates = [
            (base_dir / candidate_rel),
            (folder_root / candidate_rel),
            candidate_rel,
        ]

    for c in candidates:
        if c.is_absolute():
            if c.exists() and c.is_file():
                return c.resolve()
        else:
            p = c.resolve()
            if p.exists() and p.is_file():
                return p

    return None


def read_text(path: Path) -> str:
    # UTF-8 is most common; LaTeX projects sometimes use latin1.
    # Try utf-8 first, fallback to latin1 to avoid hard failures.
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="latin-1")


def safe_move(src: Path, dst_dir: Path) -> Path:
    """
    Move src to dst_dir, avoiding collisions by suffixing.
    Returns final destination path.
    """
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    if not dst.exists():
        shutil.move(str(src), str(dst))
        return dst

    stem = src.stem
    suffix = src.suffix
    k = 1
    while True:
        candidate = dst_dir / f"{stem}.{k}{suffix}"
        if not candidate.exists():
            shutil.move(str(src), str(candidate))
            return candidate
        k += 1


# ----------------------------
# Inlining engine
# ----------------------------
def inline_includes(
    file_path: Path,
    folder_root: Path,
    used: Set[Path],
    stack: List[Path],
    verbose: bool = False,
) -> InlineResult:
    """
    Recursively inline include commands inside file_path.
    - 'used' collects files that were inlined (excluding the top-level file itself).
    - 'stack' detects cycles.
    """
    fp = file_path.resolve()

    if fp in stack:
        cycle = " -> ".join(p.name for p in stack + [fp])
        raise RuntimeError(f"Include cycle detected: {cycle}")

    stack.append(fp)
    vget(verbose, f"[inline] Reading: {fp}")
    content = read_text(fp)

    out_lines: List[str] = []

    for line in content.splitlines(keepends=True):
        # Best-effort skip commented part when searching.
        cut = find_unescaped_percent(line)
        code_part = line if cut == -1 else line[:cut]
        comment_part = "" if cut == -1 else line[cut:]

        # We may have multiple include commands on the same line, so iterate until stable.
        new_code = code_part

        # Handle double-arg imports first
        while True:
            m = DOUBLE_ARG_RE.search(new_code)
            if not m:
                break
            cmd = m.group("cmd")
            dir_arg = m.group("dir")
            arg = m.group("arg")

            inc_path = resolve_include_path(fp, cmd, arg, dir_arg, folder_root)
            if inc_path is None:
                vget(verbose, f"[WARN] Could not resolve \\{cmd}{{{dir_arg}}}{{{arg}}} in {fp.name}")
                # Leave command as-is
                break

            used.add(inc_path)
            child = inline_includes(inc_path, folder_root, used, stack, verbose=verbose).text

            replacement = (
                f"% ==== BEGIN INLINED: {inc_path.name} (via \\{cmd}) ====\n"
                + child
                + ("" if child.endswith("\n") else "\n")
                + f"% ==== END INLINED: {inc_path.name} ====\n"
            )
            new_code = new_code[: m.start()] + replacement + new_code[m.end() :]

        # Handle single-arg commands
        while True:
            m = SINGLE_ARG_RE.search(new_code)
            if not m:
                break
            cmd = m.group("cmd")
            arg = m.group("arg")

            inc_path = resolve_include_path(fp, cmd, arg, None, folder_root)
            if inc_path is None:
                vget(verbose, f"[WARN] Could not resolve \\{cmd}{{{arg}}} in {fp.name}")
                break

            used.add(inc_path)
            child = inline_includes(inc_path, folder_root, used, stack, verbose=verbose).text

            replacement = (
                f"% ==== BEGIN INLINED: {inc_path.name} (via \\{cmd}) ====\n"
                + child
                + ("" if child.endswith("\n") else "\n")
                + f"% ==== END INLINED: {inc_path.name} ====\n"
            )
            new_code = new_code[: m.start()] + replacement + new_code[m.end() :]

        out_lines.append(new_code + comment_part)

    stack.pop()
    return InlineResult(text="".join(out_lines), used_files=used)


# ----------------------------
# Main file selection
# ----------------------------
TITLE_RE = re.compile(r"\\title\s*\{", re.IGNORECASE)
AUTHOR_RE = re.compile(r"\\author\s*\{", re.IGNORECASE)
BEGIN_DOC_RE = re.compile(r"\\begin\s*\{\s*document\s*\}", re.IGNORECASE)


def score_main_candidate(tex_path: Path) -> Tuple[int, int, int]:
    """
    Higher score is better.
    Returns (has_title_author, has_begin_doc, size_hint)
    """
    t = read_text(tex_path)
    has_title = 1 if TITLE_RE.search(t) else 0
    has_author = 1 if AUTHOR_RE.search(t) else 0
    has_title_author = 1 if (has_title and has_author) else 0
    has_begin_doc = 1 if BEGIN_DOC_RE.search(t) else 0
    size_hint = len(t)
    return (has_title_author, has_begin_doc, size_hint)


def choose_main_file(tex_files: List[Path], verbose: bool = False) -> Path:
    """
    Prefer:
    1) file containing both \title and \author
    2) then file containing \begin{document}
    3) then larger file as a heuristic
    """
    scored = [(score_main_candidate(p), p) for p in tex_files]
    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_path = scored[0]

    # Warn if multiple files tie on the top score in the main dimensions
    top = [p for (s, p) in scored if s[:2] == best_score[:2]]
    if len(top) > 1:
        log(f"[WARN] Multiple possible main files with similar signals: {[p.name for p in top]}. Chose {best_path.name}")

    vget(verbose, f"[MAIN] Selected main file: {best_path.name} score={best_score}")
    return best_path


# ----------------------------
# Folder preprocessing workflow
# ----------------------------
def preprocess_folder(folder: Path, dry_run: bool, verbose: bool, strip_fmt: bool) -> None:
    folder = folder.resolve()
    if not folder.exists() or not folder.is_dir():
        log(f"[ERROR] Not a folder: {folder}")
        return

    tex_files = sorted(folder.glob("*.tex"))
    if len(tex_files) == 0:
        vget(verbose, f"[SKIP] No .tex files in {folder}")
        return

    if len(tex_files) == 1:
        vget(verbose, f"[SKIP] Only one .tex file in {folder}: {tex_files[0].name}")
        return

    log(f"[INFO] Processing folder: {folder}  (tex files: {len(tex_files)})")

    main_tex = choose_main_file(tex_files, verbose=verbose)

    used: Set[Path] = set()
    try:
        inlined = inline_includes(
            main_tex,
            folder_root=folder,
            used=used,
            stack=[],
            verbose=verbose,
        )
    except RuntimeError as e:
        log(f"[ERROR] {folder}: {e}")
        return

    # Anything besides main that is not in used is "unmarked" per your requirement
    main_abs = main_tex.resolve()
    all_abs = {p.resolve() for p in tex_files}
    used_abs = {p.resolve() for p in inlined.used_files}

    leftover = sorted([p for p in (all_abs - {main_abs}) if p not in used_abs])
    if leftover:
        log(
            "[WARN] Unused .tex files (not inlined / not referenced from main): "
            + ", ".join(p.name for p in leftover)
        )

    # Write main file with inlined content
    expanded_text = expand_defined_macros(inlined.text)

    if dry_run:
        log(f"[dry-run] Would overwrite main file: {main_tex.name} (inlined {len(used_abs)} files)")
    else:
        final_text = inlined.text
        if strip_fmt:
            if not _STRIP_FMT_AVAILABLE:
                log("[WARN] --strip-formatting requested but strip.py not found; skipping.")
                
            else:
                final_text = _strip_formatting(final_text)
                vget(verbose, "[strip-fmt] Formatting commands stripped.")
        main_tex.write_text(final_text, encoding="utf-8")
        log(f"[INFO] Overwrote main file with inlined content: ...")

    # Move all non-main .tex into ./preprocessed/
    pre_dir = folder / "preprocessed"
    to_move = sorted([p for p in tex_files if p.resolve() != main_abs])

    if dry_run:
        log(f"[dry-run] Would create folder: {pre_dir}")
        log(f"[dry-run] Would move {len(to_move)} .tex files into preprocessed/: {[p.name for p in to_move]}")
    else:
        pre_dir.mkdir(parents=True, exist_ok=True)
        for p in to_move:
            moved_to = safe_move(p, pre_dir)
            vget(verbose, f"[MOVE] {p.name} -> {moved_to}")
        log(f"[INFO] Moved {len(to_move)} non-main .tex files into: {pre_dir.name}/")

    # Final sanity message
    remaining = sorted(folder.glob("*.tex"))
    if len(remaining) != 1 or remaining[0].resolve() != main_abs:
        if dry_run:
            log(
                f"[dry-run] After preprocessing, we keep: {[p.name for p in remaining]}"
            )
        else:
            log( 
                f"[WARN] After preprocessing, expected only main .tex to remain, but found: {[p.name for p in remaining]}"
            )
    else:
        log(f"[INFO] Folder now contains only main tex: {remaining[0].name}  (+ preprocessed/)")


def main() -> None:
    args = parse_args()
    folders = [Path(p) for p in args.folders]

    for f in folders:
        preprocess_folder(f, dry_run=args.dry_run, verbose=args.verbose, strip_fmt=args.strip_formatting)

if __name__ == "__main__":
    main()