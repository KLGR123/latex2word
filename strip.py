#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
strip.py

Strip LaTeX formatting/rendering commands from LaTeX source while preserving
the textual content they wrap.  Math, citations, cross-references, and
structural commands (sections, environments, etc.) are left untouched.

Public API
----------
    from strip import strip_formatting
    clean = strip_formatting(tex_source)

Categories handled
------------------
1. Single-arg wrappers  -- \\textbf{foo}  ->  foo
2. Two-arg wrappers     -- \\textcolor{red}{foo}  ->  foo  (2nd arg kept)
3. Three-arg wrappers   -- \\resizebox{w}{h}{foo}  ->  foo (3rd arg kept)
4. Standalone switches  -- \\bfseries, \\centering, ...  ->  (removed)
5. Spacing macros w/ arg-- \\hspace{1cm}, \\phantom{x}  ->  (removed entirely)
6. Standalone spacing   -- \\quad, \\hfill, ...  ->  (removed)
7. Font-size switches   -- \\large, \\tiny, ...  ->  (removed)
"""

from __future__ import annotations

import re
from typing import List, Tuple


# ---------------------------------------------------------------------------
# Command tables
# ---------------------------------------------------------------------------

# \\cmd{content}  ->  content
_SINGLE_ARG_WRAPPERS: List[str] = [
    # font shape / series
    "textbf", "textit", "texttt", "textrm", "textsf", "textsc",
    "textsl", "textup", "textmd", "textnormal",
    # emphasis
    "emph",
    # underline / decoration
    "underline", "uline", "uuline", "uwave",
    "sout", "xout", "dashuline", "dotuline",
    # case transformations
    "uppercase", "lowercase", "MakeUppercase", "MakeLowercase",
    "MakeTextUppercase", "MakeTextLowercase",
    # boxes that are purely cosmetic (keep content)
    "mbox", "hbox", "fbox", "framebox",
    # super/subscript (keep content, lose the script positioning)
    "textsuperscript", "textsubscript",
    # misc decorative
    "overline", "underbar",
]

# \\cmd{ignored_arg}{content}  ->  content
_TWO_ARG_WRAPPERS: List[str] = [
    # color -- includes both \textcolor and \color (the latter uses a brace arg)
    "textcolor", "color",
    # highlight / box with color
    "colorbox",
    # rotation / scaling (cosmetic)
    "rotatebox", "scalebox",
    # makebox with optional width arg handled separately; this catches plain form
    "makebox",
    # href: \\href{url}{text} -> text
    "href",
]

# \\cmd{arg1}{arg2}{content}  ->  content
_THREE_ARG_WRAPPERS: List[str] = [
    "resizebox", "resizebox*",
    "fcolorbox",   # \\fcolorbox{border-color}{bg-color}{text}
]

# Standalone switch commands (no argument, just removed)
_STANDALONE_SWITCHES: List[str] = [
    # font series
    "bfseries", "mdseries",
    # font shape
    "itshape", "slshape", "scshape", "upshape",
    # font family
    "rmfamily", "sffamily", "ttfamily",
    # normalization
    "normalfont",
    # alignment (inside environments; structural \\begin{center} is left alone)
    "centering", "raggedright", "raggedleft",
    # paragraph control
    "noindent", "indent",
    # line/page quality hints
    "sloppy", "fussy",
    # line-spacing switches (setspace package)
    "singlespacing", "doublespacing", "onehalfspacing",
    # misc
    "protect",
    "leavevmode",
    "strut", "mathstrut",
]

# Font-size switch commands (no argument, just removed)
_SIZE_SWITCHES: List[str] = [
    "tiny", "scriptsize", "footnotesize", "small", "normalsize",
    "large", "Large", "LARGE", "huge", "Huge",
]

# Spacing commands that consume one brace argument and are removed entirely.
# Note: fontsize is NOT listed here; it takes two args and belongs only in
# _SPACING_TWO_ARG.
_SPACING_ONE_ARG: List[str] = [
    "hspace", "hspace*", "vspace", "vspace*",
    "phantom", "hphantom", "vphantom",
    "kern",
    # box sizing helpers
    "settowidth", "settoheight", "settodepth",
]

# Spacing commands that consume TWO brace arguments and are removed entirely
_SPACING_TWO_ARG: List[str] = [
    "fontsize",       # \\fontsize{size}{baselineskip}
    "setlength",
    "addtolength",
]

# Standalone spacing glue (no argument, just removed)
_SPACING_STANDALONE: List[str] = [
    # horizontal
    "quad", "qquad", "enspace", "thinspace", "negthinspace",
    "medspace", "thickspace", "negmedspace", "negthickspace",
    "hfill", "hss",
    "nobreakspace",
    # vertical
    "smallskip", "medskip", "bigskip",
    "vfill", "vss",
    # line / page breaks (formatting artefacts, not content)
    "newline", "linebreak", "nolinebreak",
    "pagebreak", "nopagebreak", "clearpage", "cleardoublepage", "newpage",
    # paragraph separator helpers
    "par",
]


# ---------------------------------------------------------------------------
# Low-level brace/bracket reading
# ---------------------------------------------------------------------------

def _find_brace_end(s: str, start: int) -> int:
    """
    Given that s[start] == '{', return the index *after* the matching '}'.
    Respects nested braces and skips over escaped characters.
    Returns len(s) if the brace is never closed (best-effort).
    """
    depth = 1
    i = start + 1
    n = len(s)
    while i < n and depth > 0:
        c = s[i]
        if c == '\\':
            i += 2  # skip control char / escaped brace
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
        i += 1
    return i


def _skip_opt_arg(s: str, i: int) -> int:
    """
    If s[i] (after whitespace) is '[', skip past the matching ']' and return
    the new index; otherwise return i unchanged.
    Handles nested brackets naively (no escaping inside []).
    """
    n = len(s)
    j = i
    while j < n and s[j] in ' \t\n':
        j += 1
    if j < n and s[j] == '[':
        j += 1
        while j < n and s[j] != ']':
            j += 1
        j += 1  # skip ']'
        return j
    return i


def _skip_ws(s: str, i: int) -> int:
    """Skip whitespace (including newlines) and return new index."""
    while i < len(s) and s[i] in ' \t\n\r':
        i += 1
    return i


# ---------------------------------------------------------------------------
# Verbatim / math zone detection  (best-effort, for protection)
# ---------------------------------------------------------------------------

_VERBATIM_BEGIN  = re.compile(r"\\begin\{(verbatim\*?|lstlisting|minted|Verbatim)\}")
_VERBATIM_END    = re.compile(r"\\end\{(verbatim\*?|lstlisting|minted|Verbatim)\}")
# Pre-compiled so we can call .match(string, pos) with a position offset.
# re.match(pattern, string, pos) is WRONG: the 3rd positional arg is `flags`,
# not `pos`.  Only compiled pattern objects support .match(string, pos).
_MATH_ENV_BEGIN  = re.compile(r"\\begin\{([a-zA-Z*]+)\}")


def _build_protected_zones(tex: str) -> List[Tuple[int, int]]:
    """
    Return list of (start, end) index pairs for regions that must not be
    modified: verbatim environments and display/inline math.
    We protect these so that commands like \\tiny inside math are not removed.
    """
    zones: List[Tuple[int, int]] = []
    n = len(tex)
    i = 0
    while i < n:
        # verbatim environment
        m = _VERBATIM_BEGIN.match(tex, i)
        if m:
            end_tag = f"\\end{{{m.group(1)}}}"
            end_idx = tex.find(end_tag, m.end())
            if end_idx == -1:
                zones.append((i, n))
                break
            zones.append((i, end_idx + len(end_tag)))
            i = end_idx + len(end_tag)
            continue

        # \verb|...|  or \verb+...+
        if tex[i:i+5] == "\\verb":
            j = i + 5
            # optional *
            if j < n and tex[j] == '*':
                j += 1
            if j < n:
                delim = tex[j]
                j += 1
                while j < n and tex[j] != delim:
                    j += 1
                j += 1
            zones.append((i, j))
            i = j
            continue

        # display math \[...\]
        if tex[i:i+2] == "\\[":
            end = tex.find("\\]", i + 2)
            end = end + 2 if end != -1 else n
            zones.append((i, end))
            i = end
            continue

        # inline math \(...\)
        if tex[i:i+2] == "\\(":
            end = tex.find("\\)", i + 2)
            end = end + 2 if end != -1 else n
            zones.append((i, end))
            i = end
            continue

        # display math $$...$$
        if tex[i:i+2] == "$$":
            end = tex.find("$$", i + 2)
            end = end + 2 if end != -1 else n
            zones.append((i, end))
            i = end
            continue

        # inline math $...$  (single $)
        if tex[i] == "$" and (i == 0 or tex[i-1] != "\\"):
            j = i + 1
            while j < n:
                if tex[j] == "$" and tex[j-1] != "\\":
                    break
                j += 1
            zones.append((i, j + 1))
            i = j + 1
            continue

        # \begin{equation} / {align} / {math} / etc.
        if tex[i:i+7] == "\\begin{":
            m2 = _MATH_ENV_BEGIN.match(tex, i)
            if m2:
                env = m2.group(1)
                if env in ("equation", "equation*", "align", "align*",
                           "eqnarray", "eqnarray*", "multline", "multline*",
                           "gather", "gather*", "flalign", "flalign*",
                           "math", "displaymath", "array"):
                    end_tag = f"\\end{{{env}}}"
                    end_idx = tex.find(end_tag, m2.end())
                    end_pos = end_idx + len(end_tag) if end_idx != -1 else n
                    zones.append((i, end_pos))
                    i = end_pos
                    continue

        i += 1
    return zones


def _in_protected(pos: int, zones: List[Tuple[int, int]]) -> bool:
    """Binary-search based check for whether pos falls inside any protected zone."""
    lo, hi = 0, len(zones) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        a, b = zones[mid]
        if b <= pos:
            lo = mid + 1
        elif a > pos:
            hi = mid - 1
        else:
            return True
    return False


# ---------------------------------------------------------------------------
# Core replacement pass
# ---------------------------------------------------------------------------

def _build_pattern(cmds: List[str]) -> re.Pattern:
    """Build a regex that matches any of the listed command names after a backslash."""
    escaped = sorted([re.escape(c) for c in cmds], key=len, reverse=True)
    return re.compile(r"\\(" + "|".join(escaped) + r")(?![a-zA-Z*])")


_PAT_SINGLE     = _build_pattern(_SINGLE_ARG_WRAPPERS)
_PAT_TWO_W      = _build_pattern(_TWO_ARG_WRAPPERS)
_PAT_THREE_W    = _build_pattern(_THREE_ARG_WRAPPERS)
_PAT_STANDALONE = _build_pattern(
    _STANDALONE_SWITCHES + _SIZE_SWITCHES + _SPACING_STANDALONE
)
_PAT_SPACE_ONE  = _build_pattern([c.rstrip("*") for c in _SPACING_ONE_ARG])
_PAT_SPACE_TWO  = _build_pattern([c.rstrip("*") for c in _SPACING_TWO_ARG])


def _apply_replacements(tex: str, zones: List[Tuple[int, int]]) -> str:
    """
    Single-pass character-level walk that applies all stripping rules.
    Protected zones are copied verbatim.

    Extracted wrapper content is recursively processed so that nested
    formatting commands (e.g. \\textbf{\\textit{x}}) are fully stripped.
    """
    out: List[str] = []
    n = len(tex)
    i = 0

    while i < n:
        # Fast-path: if inside a protected zone, copy verbatim up to zone end.
        if _in_protected(i, zones):
            # Advance to the end of the innermost zone that covers i.
            for (a, b) in zones:
                if a <= i < b:
                    out.append(tex[i:b])
                    i = b
                    break
            continue

        if tex[i] != '\\':
            out.append(tex[i])
            i += 1
            continue

        # --- Try each command category in priority order ---

        # 1. Three-arg wrappers: \\cmd{a1}{a2}{content} -> content
        m = _PAT_THREE_W.match(tex, i)
        if m:
            j = _skip_ws(tex, m.end())
            j = _skip_opt_arg(tex, j)
            if j < n and tex[j] == '{':
                j = _find_brace_end(tex, j)    # skip arg1
                j = _skip_ws(tex, j)
            if j < n and tex[j] == '{':
                j = _find_brace_end(tex, j)    # skip arg2
                j = _skip_ws(tex, j)
            if j < n and tex[j] == '{':
                content_start = j + 1
                content_end   = _find_brace_end(tex, j) - 1
                # Recursively strip the extracted content.
                inner = tex[content_start:content_end]
                out.append(_apply_replacements(inner, _shift_zones(zones, -content_start)))
                i = content_end + 1
                continue
            # Fallback: could not parse args; emit backslash and move on.
            out.append(tex[i])
            i += 1
            continue

        # 2. Two-arg wrappers: \\cmd{a1}{content} -> content
        m = _PAT_TWO_W.match(tex, i)
        if m:
            j = _skip_ws(tex, m.end())
            j = _skip_opt_arg(tex, j)
            if j < n and tex[j] == '{':
                j = _find_brace_end(tex, j)    # skip arg1
                j = _skip_ws(tex, j)
            if j < n and tex[j] == '{':
                content_start = j + 1
                content_end   = _find_brace_end(tex, j) - 1
                inner = tex[content_start:content_end]
                out.append(_apply_replacements(inner, _shift_zones(zones, -content_start)))
                i = content_end + 1
                continue
            out.append(tex[i])
            i += 1
            continue

        # 3. Single-arg wrappers: \\cmd{content} -> content
        m = _PAT_SINGLE.match(tex, i)
        if m:
            j = _skip_ws(tex, m.end())
            j = _skip_opt_arg(tex, j)
            if j < n and tex[j] == '{':
                content_start = j + 1
                content_end   = _find_brace_end(tex, j) - 1
                inner = tex[content_start:content_end]
                out.append(_apply_replacements(inner, _shift_zones(zones, -content_start)))
                i = content_end + 1
                continue
            out.append(tex[i])
            i += 1
            continue

        # 4. Spacing commands that discard two brace args
        m = _PAT_SPACE_TWO.match(tex, i)
        if m:
            j = _skip_ws(tex, m.end())
            j = _skip_opt_arg(tex, j)
            if j < n and tex[j] == '{':
                j = _find_brace_end(tex, j)
                j = _skip_ws(tex, j)
            if j < n and tex[j] == '{':
                j = _find_brace_end(tex, j)
            i = j
            continue

        # 5. Spacing commands that discard one brace arg
        m = _PAT_SPACE_ONE.match(tex, i)
        if m:
            j = _skip_ws(tex, m.end())
            j = _skip_opt_arg(tex, j)          # e.g. \\hspace*{1cm}
            if j < n and tex[j] == '{':
                j = _find_brace_end(tex, j)
            i = j
            continue

        # 6. Standalone switches / size / spacing (no arg consumed)
        m = _PAT_STANDALONE.match(tex, i)
        if m:
            i = m.end()
            continue

        # 7. \\ (forced line break in tabular/poetry) -> single newline
        if tex[i:i+2] == "\\\\" and (i + 2 >= n or tex[i+2] not in ('\\',)):
            out.append("\n")
            i += 2
            continue

        # Default: copy character
        out.append(tex[i])
        i += 1

    return "".join(out)


def _shift_zones(zones: List[Tuple[int, int]], delta: int) -> List[Tuple[int, int]]:
    """
    Translate protected zone coordinates by delta so they remain valid
    after slicing a substring out of the original source string.
    Zones that fall entirely outside [0, ...) after shifting are dropped.
    """
    shifted = []
    for (a, b) in zones:
        na, nb = a + delta, b + delta
        if nb <= 0:
            continue
        shifted.append((max(na, 0), nb))
    return shifted


# ---------------------------------------------------------------------------
# Post-processing cleanup
# ---------------------------------------------------------------------------

def _cleanup(tex: str) -> str:
    """
    Light cleanup after stripping:
    - Collapse runs of blank lines to at most one blank line separator.
    - Remove lines that became blank (contained only a formatting command).
    - Trim trailing whitespace on each line.
    """
    lines = tex.splitlines()
    cleaned: List[str] = []
    blank_run = 0
    for ln in lines:
        ln = ln.rstrip()
        if ln == "":
            blank_run += 1
            if blank_run <= 1:   # preserve at most one blank line separator
                cleaned.append("")
        else:
            blank_run = 0
            cleaned.append(ln)
    return "\n".join(cleaned)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def strip_formatting(tex: str) -> str:
    """
    Remove LaTeX formatting/rendering commands from *tex* while keeping the
    textual content they wrap.  Math, verbatim, structural commands, and
    cross-references are left untouched.

    Parameters
    ----------
    tex : str
        Raw LaTeX source (may be a full document or a fragment).

    Returns
    -------
    str
        The cleaned LaTeX source.
    """
    zones = _build_protected_zones(tex)
    stripped = _apply_replacements(tex, zones)
    return _cleanup(stripped)


# ---------------------------------------------------------------------------
# CLI usage (for quick testing)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python strip.py <file.tex>", file=sys.stderr)
        sys.exit(1)

    path = sys.argv[1]
    try:
        with open(path, encoding="utf-8") as f:
            source = f.read()
    except UnicodeDecodeError:
        with open(path, encoding="latin-1") as f:
            source = f.read()

    print(strip_formatting(source))