#!/usr/bin/env python3
# -*- coding: utf-8 -*-

r"""
label_envs.py

Post-process outputs/translated.json to assign display names (env_label) to
every paragraph:

  Environment blocks  ->  counter-based label scoped to the chapter:
    "图3-1"    first figure in chapter 3
    "表3-1"    first table  in chapter 3
    "算法3-1"  first algorithm in chapter 3
    "代码3-1"  first code block in chapter 3
    "式3-1"    first numbered math formula in chapter 3

  Abstract  ->  "摘要" (no chapter information needed)

  Section headings  ->  "chapter.N节" / "chapter.N.M小节":
    "3.1节"      first \section in chapter 3
    "3.1.2小节"  second \subsection under section 3.1
                 (\subsubsection and deeper are treated the same as \subsection)

  Plain text paragraphs  ->  inherit the label of the last seen heading:
    If the preceding heading was "3.2节",   the paragraph also gets "3.2节".
    If the preceding heading was "3.2.1小节", the paragraph also gets "3.2.1小节".
    Paragraphs that appear before any heading in a document get "chapter.0节".

Counters are scoped per document (chapter field) and reset appropriately:
  - env counters    : reset at the start of each document.
  - section_count   : reset at the start of each document.
  - sub_count       : reset each time a new section is encountered.

Usage:
    python3 label_envs.py                                    # default paths
    python3 label_envs.py --input  path/to/translated.json \\
                           --output path/to/labeled.json
    python3 label_envs.py --inplace                          # overwrite input
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from typing import Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# Environment classification
# ---------------------------------------------------------------------------

# Each category maps to a (Chinese prefix, set of LaTeX environment names).
_ENV_CATEGORIES: Dict[str, Tuple[str, frozenset]] = {
    "figure": (
        "图",
        frozenset({
            # Standard LaTeX
            "figure", "figure*",
            # subfigure / subcaption packages
            "subfigure", "subfigure*",
            "subfloat",
            # wrapfig package
            "wrapfigure", "wrapfigure*",
            # caption / float packages
            "SCfigure", "SCfigure*",
            "floatrow",
            "ffigbox",
            "capbeside",
            # sidecap package
            "SCtable", "SCtable*",       # side-caption table, but renders as figure-like float
            # rotating package
            "sidewaysfigure", "sidewaysfigure*",
            "turnfigure",
            # memoir / KOMA classes
            "captionedbox",
            # tikz / pgf
            "tikzpicture",               # standalone diagram blocks
            "tikzfigure",
            # pgfplots
            "tikzpicture",               # duplicate intentional (frozenset deduplicates)
            # floatflt package
            "floatingfigure",
            # picinpar package
            "figwindow",
            # cutwin package
            "cutout",
            # overpic / picture
            "overpic",
            # acmart / IEEE / NeurIPS custom
            "teaserfigure",
            "figurehere",
        }),
    ),
    "table": (
        "表",
        frozenset({
            # Standard LaTeX
            "table", "table*",
            # Inline tabular (no float wrapper, but often treated as a table block)
            "tabular", "tabular*",
            "tabularx", "tabularx*",
            "tabulary",
            "tabularray", "tblr",        # tabularray package
            "longtblr",                  # tabularray long table
            # longtable package
            "longtable", "longtable*",
            # supertabular / xtab packages
            "supertabular", "supertabular*",
            "mpsupertabular",
            "xtabular",
            # rotating package
            "sidewaystable", "sidewaystable*",
            "sidewaystabular",
            "turntable",
            # booktabs (no env, but some templates wrap in custom envs)
            # threeparttable package
            "threeparttable",
            "threeparttablex",
            # tabu package
            "tabu", "longtabu",
            # tabbing (primitive but used)
            "tabbing",
            # wraptable
            "wraptable", "wraptable*",
            # floatrow
            "floatrow",                  # shared with figure; frozenset handles overlap
            "ttabbox",
            # ctable package
            "ctable",
            # array package extras
            "array",
            # spreadtab
            "spreadtab",
            # NeurIPS / ICML custom
            "tablehere",
            "adjustbox",                 # sometimes wraps tables
        }),
    ),
    "algorithm": (
        "算法",
        frozenset({
            # algorithm / algorithmic packages
            "algorithm", "algorithm*",
            "algorithmic",
            # algorithmicx / algpseudocode
            "algorithmicx",
            "algpseudocode",
            # algorithm2e package
            "algorithm2e", "algorithm2e*",
            # clrscode / clrscode3e
            "codebox",
            # pseudocode package
            "pseudocode",
            # listings-based pseudo
            "lstpseudocode",
            # custom names seen in the wild
            "myalgorithm",
            "algo",
            "proc",                      # procedure blocks
            "procedure",
            "function",                  # used in algorithmicx
        }),
    ),
    "code": (
        "代码",
        frozenset({
            # listings package
            "lstlisting",
            # minted package (Pygments)
            "minted",
            "minted*",
            # verbatim
            "verbatim", "verbatim*",
            "Verbatim", "Verbatim*",     # fancyvrb package (capital V)
            "BVerbatim", "LVerbatim",    # fancyvrb variants
            "SaveVerbatim",
            # alltt package
            "alltt",
            # moreverb package
            "verbatimtab",
            "listing",                   # moreverb listing env
            # tcolorbox (code-box style)
            "tcolorbox",
            "tcblisting",                # tcolorbox + minted combo
            # mdframed used for code
            "mdframed",
            # spverbatim
            "spverbatim",
            # comment package (sometimes used to fence code)
            "comment",
            # Custom names common in papers
            "codeblock",
            "codebox",
            "sourcecode",
            "pycode",
            "bashcode",
            "jsoncode",
            "xmlcode",
            "sqlcode",
            # Overleaf / beamer examples
            "exampleblock",
            "example",
        }),
    ),
    "math": (
        "式",
        frozenset({
            # Standard LaTeX display math
            "equation", "equation*",
            "displaymath",
            # amsmath
            "align", "align*",
            "aligned",
            "alignat", "alignat*",
            "alignedat",
            "gather", "gather*",
            "gathered",
            "multline", "multline*",
            "flalign", "flalign*",
            "split",
            "cases", "cases*",
            "dcases", "dcases*",         # mathtools package
            "rcases", "rcases*",
            "numcases",                  # cases package
            # subequations groups
            "subequations",
            # eqnarray (legacy)
            "eqnarray", "eqnarray*",
            # empheq package (boxed equations)
            "empheq",
            # breqn package (auto line-breaking)
            "dmath", "dmath*",
            "dseries", "dseries*",
            "dgroup", "dgroup*",
            "darray", "darray*",
            # IEEEeqnarray (IEEEtran class)
            "IEEEeqnarray", "IEEEeqnarray*",
            # math inside minipage (sometimes chunked as block)
            "math",
        }),
    ),
}

# Regex: detect \begin{env_name} at the start of a paragraph (after stripping).
_BEGIN_ENV_RE = re.compile(r"\\begin\{([^}]+)\}")

# Display-math shorthand: \[...\]
# A paragraph consisting almost entirely of \[...\] counts as a formula.
_DISPLAY_MATH_RE = re.compile(r"^\s*\\\[", re.DOTALL)

# ---------------------------------------------------------------------------
# Section heading classification
# ---------------------------------------------------------------------------

# Matches the first LaTeX sectioning command in a paragraph (possibly preceded
# by a \label{} that was merged into it by chunk.py's merge_leading_labels).
# Groups: (1) command name without backslash, (2) title text inside braces.
_SECTION_CMD_RE = re.compile(
    r"\\(part|chapter|section|subsection|subsubsection|subsubsubsection"
    r"|paragraph|subparagraph)\*?\s*\{",
    re.IGNORECASE,
)

# Levels that map to "chapter.N节"  (top-level sectioning)
_SECTION_LEVEL  = frozenset({"part", "chapter", "section"})
# Levels that map to "chapter.N.M小节" (sub-level sectioning); everything
# deeper than subsection is treated identically per the requirement.
_SUBSECTION_LEVEL = frozenset({
    "subsection", "subsubsection", "subsubsubsection",
    "paragraph", "subparagraph",
})


def _classify_section(text: str) -> Optional[str]:
    """
    Return "section" or "subsection" if the paragraph is a sectioning command,
    else None.  The paragraph may contain a leading \\label{} (merged by the
    chunker) before the actual \\section / \\subsection command.
    """
    m = _SECTION_CMD_RE.search(text)
    if not m:
        return None
    cmd = m.group(1).lower()
    if cmd in _SECTION_LEVEL:
        return "section"
    if cmd in _SUBSECTION_LEVEL:
        return "subsection"
    return None


def _is_abstract(text: str) -> bool:
    """Return True if the paragraph is an abstract environment or heading."""
    stripped = text.lstrip()
    # \begin{abstract}
    if re.match(r"\\begin\{abstract\}", stripped, re.IGNORECASE):
        return True
    # \section*{Abstract} or \section{Abstract} (some templates use this)
    m = _SECTION_CMD_RE.search(stripped)
    if m:
        # extract the title text inside the first { }
        brace_start = stripped.index("{", m.start()) + 1
        brace_end   = stripped.index("}", brace_start)
        title = stripped[brace_start:brace_end].strip()
        if title.lower() == "abstract":
            return True
    return False


def _classify_paragraph(text: str) -> Optional[str]:
    """
    Return the category name ("figure", "table", "algorithm", "code", "math")
    if the paragraph is an environment block of that type, else None.

    Detection strategy:
    1. If the paragraph starts with \\begin{env}, look up env in category tables.
    2. If the paragraph starts with \\[ (display math shorthand), treat as "math".
    """
    stripped = text.lstrip()

    # Check \[ display math first (no \begin{} wrapper)
    if _DISPLAY_MATH_RE.match(stripped):
        return "math"

    m = _BEGIN_ENV_RE.match(stripped)
    if not m:
        return None

    env_name = m.group(1).strip()

    for category, (_prefix, env_set) in _ENV_CATEGORIES.items():
        if env_name in env_set:
            return category

    return None


# ---------------------------------------------------------------------------
# Main labeling logic
# ---------------------------------------------------------------------------

def label_paragraphs(documents: list) -> Tuple[list, Dict[str, int]]:
    r"""
    Walk every paragraph in every document and assign "env_label".

    Priority (checked in order for each paragraph):
      0. Abstract          -> "摘要", updates current_label.
      1. Section heading   -> "chapter.N节" or "chapter.N.M小节", updates current_label.
      2. Environment block -> "图/表/算法/代码/式 chapter-seq".
      3. Plain text        -> inherit current_label (defaults to "chapter.0节"
                              if no heading has been seen yet in this document).

    Counters are per-document (identified by the chapter field):
      - env_counters  : reset at the start of each document.
      - section_count : reset at the start of each document.
      - sub_count     : reset every time a new section is encountered.
    """
    # env_counters[category] = next seq number (1-based), reset per document
    summary: Dict[str, int] = defaultdict(int)

    for doc in documents:
        chapter: int = doc.get("chapter", 0)

        # Per-document state
        env_counters: Dict[str, int] = defaultdict(int)
        section_count  = 0   # increments on \section / \chapter / \part
        sub_count      = 0   # increments on \subsection / deeper; resets on new section
        current_label  = f"{chapter}.0节"  # inherited by plain-text paragraphs before any heading

        for para in doc.get("paragraphs", []):
            text: str = para.get("text", "")

            # --- 0. Abstract environment? ---
            if _is_abstract(text):
                para["env_label"] = "摘要"
                current_label = "摘要"
                summary["abstract"] += 1
                continue

            # --- 1. Section heading? ---
            sec_type = _classify_section(text)
            if sec_type == "section":
                section_count += 1
                sub_count = 0          # reset subsection counter for each new section
                lbl = f"{chapter}.{section_count}节"
                current_label = lbl
                para["env_label"] = lbl
                summary["section"] += 1
                continue

            if sec_type == "subsection":
                sub_count += 1
                lbl = f"{chapter}.{section_count}.{sub_count}小节"
                current_label = lbl
                para["env_label"] = lbl
                summary["subsection"] += 1
                continue

            # --- 2. Environment block? ---
            category = _classify_paragraph(text)
            if category is not None:
                env_counters[category] += 1
                seq = env_counters[category]
                prefix, _ = _ENV_CATEGORIES[category]
                para["env_label"] = f"{prefix}{chapter}-{seq}"
                summary[category] += 1
                continue

            # --- 3. Plain text paragraph: inherit current heading label ---
            para["env_label"] = current_label
            summary["text"] += 1

    return documents, dict(summary)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Add env_label fields to paragraphs in translated.json.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument(
        "--input",
        default=os.path.join("outputs", "translated.json"),
        help="Path to input translated JSON (default: outputs/translated.json)",
    )
    ap.add_argument(
        "--output",
        default=os.path.join("outputs", "labeled.json"),
        help="Path to output labeled JSON (default: outputs/labeled.json)",
    )
    ap.add_argument(
        "--inplace",
        action="store_true",
        help="Overwrite the input file instead of writing a separate output.",
    )
    return ap.parse_args()


def main() -> None:
    args = parse_args()

    # --- Load ---
    input_path = args.input
    if not os.path.exists(input_path):
        print(f"[ERROR] Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    documents = data.get("documents", [])
    if not documents:
        print("[WARNING] No documents found in input JSON.", file=sys.stderr)

    # --- Label ---
    documents, summary = label_paragraphs(documents)
    data["documents"] = documents

    # --- Write ---
    output_path = input_path if args.inplace else args.output
    out_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(out_dir, exist_ok=True)

    tmp_path = output_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, output_path)

    # --- Report ---
    category_labels = {
        "abstract":  "摘要     (abstract)",
        "section":   "节       (\\section)",
        "subsection":"小节     (\\subsection / deeper)",
        "figure":    "图       (figure)",
        "table":     "表       (table)",
        "algorithm": "算法     (algorithm)",
        "code":      "代码     (code)",
        "math":      "式       (math)",
        "text":      "文本     (plain text)",
    }
    total = sum(summary.values())
    print(f"[INFO] Labeled {total} paragraph(s):")
    for cat, label in category_labels.items():
        count = summary.get(cat, 0)
        if count:
            print(f"       {label}: {count}")
    print(f"[INFO] Output written to: {output_path}")


if __name__ == "__main__":
    main()