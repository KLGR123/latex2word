#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
render.py

Convert labeled.json (+ refmap.json + citations.json) into a Word .docx file.

Pipeline summary
----------------
  labeled.json  → paragraph blocks (text + translation + env_label)
  refmap.json   → {chapter: {label_key: env_label_string}}
  citations.json→ {cite_key: {id: N, citation: "..."}}
  inputs/{ch}/  → image files referenced in LaTeX figures

Output structure per chapter
-----------------------------
  第X章  Title           (Heading 1, 宋体 18pt bold, centered)
  摘要 / 节 / 小节        (Heading 2/3, 宋体 bold)
  plain text paragraphs  (宋体 12pt, justified)
  figures                (image + 楷体 caption below)
  tables                 (python-docx Table + 楷体 caption above)
  display math           (OMML, centered)
  algorithms / code      (Courier New, monospace)
  ── after all chapters ──
  参考文献               (Heading 1, centered)
  [1] citation text      (宋体 12pt)

Usage
-----
  python3 render.py \\
      --labeled  outputs/labeled.json \\
      --refmap   outputs/refmap.json \\
      --citations outputs/citations.json \\
      --inputs-dir inputs/ \\
      --output   outputs/result.docx \\
      [--toc] [--font-size 12] [--page-size a4]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cn2an
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Inches, Pt, RGBColor, Emu

from table import parse_latex_table

# ---------------------------------------------------------------------------
# Optional math support
# ---------------------------------------------------------------------------
try:
    from latex2mathml.converter import convert as _latex_to_mathml
    from mathml2omml import convert as _mathml_to_omml
    MATH_OK = True
except ImportError:
    MATH_OK = False
    print("[WARN] latex2mathml / mathml2omml not installed; math rendered as plain text.",
          file=sys.stderr)

# ---------------------------------------------------------------------------
# Optional PDF→PNG
# ---------------------------------------------------------------------------
try:
    from pdf2image import convert_from_path as _pdf_to_images
    PDF_OK = True
except ImportError:
    PDF_OK = False
    print("[WARN] pdf2image not installed; PDF figures will be skipped.", file=sys.stderr)


# ---------------------------------------------------------------------------
# Font / style constants
# ---------------------------------------------------------------------------

FONT_BODY    = "宋体"
FONT_CAPTION = "楷体"
FONT_CODE    = "Courier New"
FONT_HEADING = "宋体"

SIZE_BODY     = Pt(12)
SIZE_CAPTION  = Pt(10.5)
SIZE_CODE     = Pt(10)
SIZE_H1       = Pt(18)
SIZE_H2       = Pt(16)
SIZE_H3       = Pt(14)

# A4 page content width in cm (A4 210mm − 2×30mm margin = 150mm)
PAGE_CONTENT_WIDTH_CM = 15.0

# ---------------------------------------------------------------------------
# Helpers — number to Chinese
# ---------------------------------------------------------------------------

_NUM_TO_ZH = {
    3: "三", 4: "四", 5: "五", 6: "六",
    7: "七", 8: "八", 9: "九", 10: "十",
}


def chapter_num_to_zh(n: int) -> str:
    """Convert chapter number to Chinese: 3 → '三', 11 → '十一'."""
    try:
        return cn2an.an2cn(str(n), "low")
    except Exception:
        return _NUM_TO_ZH.get(n, str(n))


# ---------------------------------------------------------------------------
# Document style setup
# ---------------------------------------------------------------------------

def _set_run_cjk_font(run, font_name: str) -> None:
    """Set both ASCII and East-Asian font on a run."""
    run.font.name = font_name
    rPr = run._r.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rPr.insert(0, rFonts)
    rFonts.set(qn("w:eastAsia"), font_name)
    rFonts.set(qn("w:ascii"),    font_name)
    rFonts.set(qn("w:hAnsi"),    font_name)


def _set_para_spacing(para, before_pt: float = 0, after_pt: float = 6,
                      line_rule=WD_LINE_SPACING.MULTIPLE, line_val: float = 1.25) -> None:
    pPr = para._p.get_or_add_pPr()
    spacing = pPr.find(qn("w:spacing"))
    if spacing is None:
        spacing = OxmlElement("w:spacing")
        pPr.append(spacing)
    spacing.set(qn("w:before"), str(int(before_pt * 20)))
    spacing.set(qn("w:after"),  str(int(after_pt  * 20)))
    spacing.set(qn("w:line"),   str(int(line_val  * 240)))
    spacing.set(qn("w:lineRule"), "auto")


def setup_styles(doc: Document, font_size_pt: float = 12.0) -> None:
    """Configure document default styles for Chinese academic documents."""
    # Default document font
    styles = doc.styles

    # Normal style
    normal = styles["Normal"]
    normal.font.name = FONT_BODY
    normal.font.size = Pt(font_size_pt)
    nfmt = normal._element.find(qn("w:rPr"))
    # Set CJK font via XML
    rPr = normal.element.find(qn("w:rPr"))
    if rPr is None:
        rPr = OxmlElement("w:rPr")
        normal.element.append(rPr)
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rPr.insert(0, rFonts)
    rFonts.set(qn("w:eastAsia"), FONT_BODY)
    rFonts.set(qn("w:ascii"),    FONT_BODY)

    # Heading 1 — 第X章
    _setup_heading(styles, "Heading 1", SIZE_H1, bold=True,
                   align=WD_ALIGN_PARAGRAPH.CENTER,
                   space_before=24, space_after=12, outline=0)
    # Heading 2 — X.Y 节
    _setup_heading(styles, "Heading 2", SIZE_H2, bold=True,
                   align=WD_ALIGN_PARAGRAPH.LEFT,
                   space_before=18, space_after=6, outline=1)
    # Heading 3 — X.Y.Z 小节
    _setup_heading(styles, "Heading 3", SIZE_H3, bold=True,
                   align=WD_ALIGN_PARAGRAPH.LEFT,
                   space_before=12, space_after=6, outline=2)

    # Caption style
    if "Caption" not in [s.name for s in styles]:
        cap_style = styles.add_style("Caption", WD_STYLE_TYPE.PARAGRAPH)
    else:
        cap_style = styles["Caption"]
    cap_style.font.name = FONT_CAPTION
    cap_style.font.size = SIZE_CAPTION
    cap_style.font.italic = False
    _set_pstyle_cjk(cap_style, FONT_CAPTION)

    # Code style
    if "Code" not in [s.name for s in styles]:
        code_style = styles.add_style("Code", WD_STYLE_TYPE.PARAGRAPH)
    else:
        code_style = styles["Code"]
    code_style.font.name = FONT_CODE
    code_style.font.size = SIZE_CODE
    _set_pstyle_cjk(code_style, FONT_CODE)


def _setup_heading(styles, name: str, size: Pt, bold: bool,
                   align: WD_ALIGN_PARAGRAPH, space_before: float,
                   space_after: float, outline: int) -> None:
    try:
        h = styles[name]
    except KeyError:
        h = styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
    h.font.name = FONT_HEADING
    h.font.size = size
    h.font.bold = bold
    h.font.color.rgb = RGBColor(0, 0, 0)
    _set_pstyle_cjk(h, FONT_HEADING)
    pPr = h.paragraph_format
    pPr.alignment = align
    pPr.space_before = Pt(space_before)
    pPr.space_after  = Pt(space_after)
    # Set outline level for TOC
    el_pPr = h.element.find(qn("w:pPr"))
    if el_pPr is None:
        el_pPr = OxmlElement("w:pPr")
        h.element.append(el_pPr)
    outline_el = el_pPr.find(qn("w:outlineLvl"))
    if outline_el is None:
        outline_el = OxmlElement("w:outlineLvl")
        el_pPr.append(outline_el)
    outline_el.set(qn("w:val"), str(outline))


def _set_pstyle_cjk(style, font_name: str) -> None:
    """Set East-Asian font on a paragraph style's rPr."""
    el = style.element
    rPr = el.find(qn("w:rPr"))
    if rPr is None:
        rPr = OxmlElement("w:rPr")
        el.append(rPr)
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rPr.insert(0, rFonts)
    rFonts.set(qn("w:eastAsia"), font_name)
    rFonts.set(qn("w:ascii"),    font_name)
    rFonts.set(qn("w:hAnsi"),    font_name)


# ---------------------------------------------------------------------------
# Page setup
# ---------------------------------------------------------------------------

def set_page_size(doc: Document, size: str = "a4") -> None:
    """Set page size and margins."""
    section = doc.sections[0]
    if size.lower() == "a4":
        section.page_width  = Cm(21.0)
        section.page_height = Cm(29.7)
    else:  # letter
        section.page_width  = Cm(21.59)
        section.page_height = Cm(27.94)
    section.left_margin   = Cm(3.0)
    section.right_margin  = Cm(3.0)
    section.top_margin    = Cm(2.5)
    section.bottom_margin = Cm(2.5)


# ---------------------------------------------------------------------------
# Table of Contents
# ---------------------------------------------------------------------------

def insert_toc(doc: Document) -> None:
    """Insert a TOC field at the current end of the document."""
    para = doc.add_paragraph()
    para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = para.add_run("目录")
    run.bold = True
    run.font.size = SIZE_H2
    _set_run_cjk_font(run, FONT_HEADING)

    # TOC field instruction
    para_toc = doc.add_paragraph()
    fldChar_begin = OxmlElement("w:fldChar")
    fldChar_begin.set(qn("w:fldCharType"), "begin")
    instrText = OxmlElement("w:instrText")
    instrText.set(qn("xml:space"), "preserve")
    instrText.text = 'TOC \\o "1-3" \\h \\z \\u'
    fldChar_end = OxmlElement("w:fldChar")
    fldChar_end.set(qn("w:fldCharType"), "end")
    run_toc = para_toc.add_run()
    run_toc._r.append(fldChar_begin)
    run_toc._r.append(instrText)
    run_toc._r.append(fldChar_end)

    doc.add_paragraph()   # spacer after TOC


# ---------------------------------------------------------------------------
# Math rendering
# ---------------------------------------------------------------------------

def _latex_math_to_omml(latex_math: str) -> Optional[str]:
    """Convert a LaTeX math expression to OMML XML string."""
    if not MATH_OK:
        return None
    try:
        mathml = _latex_to_mathml(latex_math)
        omml  = _mathml_to_omml(mathml)
        return omml
    except Exception as exc:
        print(f"[WARN] Math conversion failed for {latex_math[:60]!r}: {exc}", file=sys.stderr)
        return None


_OMML_NS  = "http://schemas.openxmlformats.org/officeDocument/2006/math"
_W_NS     = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R_NS     = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

# Full namespace map required for OMML parsing
_OMML_NSMAP = {
    "m":   _OMML_NS,
    "w":   _W_NS,
    "r":   _R_NS,
    "wp":  "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "a":   "http://schemas.openxmlformats.org/drawingml/2006/main",
    "mc":  "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "wpc": "http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas",
}


def _insert_omml_into_para(para, omml_str: str) -> bool:
    """
    Parse OMML string (which lacks namespace declarations) and append to paragraph.
    Wraps the fragment in a temporary root that declares all required namespaces.
    """
    try:
        from lxml import etree
        ns_attrs = " ".join(
            f'xmlns:{prefix}="{uri}"' for prefix, uri in _OMML_NSMAP.items()
        )
        wrapped  = f"<root {ns_attrs}>{omml_str}</root>"
        root_el  = etree.fromstring(wrapped.encode("utf-8"))
        for child in list(root_el):
            para._p.append(child)
        return True
    except Exception as exc:
        print(f"[WARN] OMML insert failed: {exc}", file=sys.stderr)
        return False


def _extract_display_math(text: str) -> Optional[str]:
    """
    Extract math content from display-math environments.
    Returns the raw LaTeX math (without outer environment wrapper).
    """
    # \begin{equation}...\end{equation} and variants
    m = re.search(
        r"\\begin\{(equation\*?|align\*?|gather\*?|multline\*?|"
        r"flalign\*?|eqnarray\*?|displaymath)\}"
        r"(.*?)"
        r"\\end\{\1\}",
        text, re.DOTALL
    )
    if m:
        return m.group(2).strip()
    # \[...\]
    m = re.search(r"\\\[(.*?)\\\]", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # $$...$$
    m = re.search(r"\$\$(.*?)\$\$", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return None


def add_display_math_paragraph(doc: Document, latex_text: str,
                                label_text: str = "") -> None:
    """Add a centered display-math paragraph (OMML or plain text fallback)."""
    math_content = _extract_display_math(latex_text)
    if math_content is None:
        math_content = latex_text  # best effort

    para = doc.add_paragraph()
    para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_para_spacing(para, before_pt=6, after_pt=6)

    if MATH_OK:
        omml = _latex_math_to_omml(math_content)
        if omml:
            _insert_omml_into_para(para, omml)
            return

    # Fallback: plain text
    run = para.add_run(math_content)
    run.font.name = FONT_CODE
    run.font.size = SIZE_CODE


# ---------------------------------------------------------------------------
# Inline LaTeX parser → list of run specs
# ---------------------------------------------------------------------------

# Run spec: dict with keys:
#   type   : "text" | "math" | "footnote" | "superscript"
#   text   : str
#   bold   : bool
#   italic : bool
#   code   : bool
#   omml   : str  (for type=="math")
#   fn_runs: list (for type=="footnote")

RunSpec = Dict[str, Any]

# Cite command variants (all produce superscript [N])
_CITE_CMD_RE = re.compile(
    r"\\(?:cite[a-zA-Z]*)\*?\s*(?:\[[^\]]*\])?\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}"
)

# Ref command variants → resolve via refmap
_REF_CMD_RE = re.compile(
    r"\\(?:ref|eqref|autoref|cref|Cref|hyperref|pageref|vref)\*?\s*"
    r"(?:\[[^\]]*\])?\s*\{([^}]+)\}"
)


def _build_cite_lookup(citations: Dict[str, Any]) -> Dict[str, int]:
    """Build {cite_key: id_number} from citations dict."""
    return {k: v["id"] for k, v in citations.items()}


class InlineParser:
    """
    Recursive-descent parser for inline LaTeX in translation strings.
    Produces a flat list of RunSpec dicts.
    """

    def __init__(
        self,
        cite_lookup: Dict[str, int],
        refmap: Dict[str, str],    # {label_key: env_label}
        chapter: int,
        full_refmap: Dict[str, Dict[str, str]],  # {chapter_str: {key: label}}
    ):
        self.cite_lookup = cite_lookup
        self.refmap      = refmap
        self.chapter     = chapter
        self.full_refmap = full_refmap

    # ------------------------------------------------------------------ public

    def parse(self, text: str) -> List[RunSpec]:
        """Parse inline LaTeX text into RunSpec list."""
        specs: List[RunSpec] = []
        self._parse_fragment(text, specs, bold=False, italic=False, code=False)
        return specs

    # ----------------------------------------------------------------- private

    def _parse_fragment(self, s: str, out: List[RunSpec],
                        bold: bool, italic: bool, code: bool) -> None:
        i = 0
        n = len(s)
        buf = []  # accumulate plain text characters

        def flush():
            if buf:
                out.append({"type": "text", "text": "".join(buf),
                            "bold": bold, "italic": italic, "code": code})
                buf.clear()

        while i < n:
            c = s[i]

            # Comment
            if c == "%" and (i == 0 or s[i-1] != "\\"):
                # Skip to end of line
                while i < n and s[i] != "\n":
                    i += 1
                continue

            # Backslash
            if c == "\\":
                # Double backslash → line break → space in inline context
                if i + 1 < n and s[i+1] == "\\":
                    buf.append(" ")
                    i += 2
                    continue

                cmd, i = self._read_cmd(s, i)
                if cmd is None:
                    buf.append("\\")
                    continue

                handled = self._handle_cmd(cmd, s, i, out, buf, flush,
                                           bold, italic, code)
                if handled is not None:
                    i = handled
                else:
                    # Unknown command: skip
                    pass
                continue

            # Inline math $...$
            if c == "$":
                # Check for $$
                if i + 1 < n and s[i+1] == "$":
                    end = s.find("$$", i + 2)
                    if end != -1:
                        math_src = s[i+2:end]
                        flush()
                        out.append(self._make_math(math_src))
                        i = end + 2
                        continue
                # Single $
                j = i + 1
                while j < n:
                    if s[j] == "$" and (j == 0 or s[j-1] != "\\"):
                        break
                    if s[j] == "\\" and j + 1 < n:
                        j += 2
                        continue
                    j += 1
                if j < n:
                    math_src = s[i+1:j]
                    flush()
                    out.append(self._make_math(math_src))
                    i = j + 1
                else:
                    buf.append(c)
                    i += 1
                continue

            # Tilde → non-breaking space (in Chinese context just a space)
            if c == "~":
                buf.append("\u00a0")
                i += 1
                continue

            # En/em dash
            if c == "-" and i + 1 < n and s[i+1] == "-":
                if i + 2 < n and s[i+2] == "-":
                    buf.append("—")
                    i += 3
                else:
                    buf.append("–")
                    i += 2
                continue

            # Opening brace group (orphan)
            if c == "{":
                end = _find_balanced_brace(s, i)
                inner = s[i+1:end-1]
                flush()
                self._parse_fragment(inner, out, bold, italic, code)
                i = end
                continue

            buf.append(c)
            i += 1

        flush()

    def _read_cmd(self, s: str, i: int) -> Tuple[Optional[str], int]:
        """Read control sequence starting at backslash. Return (name, new_i)."""
        assert s[i] == "\\"
        i += 1
        if i >= len(s):
            return None, i
        if s[i].isalpha() or s[i] == "@":
            j = i
            while j < len(s) and (s[j].isalpha() or s[j] == "@" or s[j] == "*"):
                j += 1
            return s[i:j], j
        # Single char
        return s[i], i + 1

    def _handle_cmd(self, cmd: str, s: str, i: int,
                    out: List[RunSpec], buf: list, flush,
                    bold: bool, italic: bool, code: bool) -> Optional[int]:
        """
        Handle a command. Return new index if handled, else None.
        """
        # --- Escaped specials ---
        if cmd in ("%", "&", "#", "_", "{", "}", "$"):
            buf.append(cmd)
            return i

        if cmd == "textbackslash":
            buf.append("\\")
            return i

        if cmd == "LaTeX":
            buf.append("LaTeX")
            return i

        if cmd == "TeX":
            buf.append("TeX")
            return i

        # --- Formatting wrappers ---
        if cmd in ("textbf",):
            content, i2 = _read_brace_arg(s, i)
            flush()
            self._parse_fragment(content, out, bold=True, italic=italic, code=code)
            return i2

        if cmd in ("textit", "emph", "textsl"):
            content, i2 = _read_brace_arg(s, i)
            flush()
            self._parse_fragment(content, out, bold=bold, italic=True, code=code)
            return i2

        if cmd in ("texttt",):
            content, i2 = _read_brace_arg(s, i)
            flush()
            self._parse_fragment(content, out, bold=bold, italic=italic, code=True)
            return i2

        if cmd in ("textrm", "textsf", "textsc", "textup", "textmd",
                   "textnormal", "normalfont", "text"):
            content, i2 = _read_brace_arg(s, i)
            flush()
            self._parse_fragment(content, out, bold=bold, italic=italic, code=False)
            return i2

        if cmd in ("underline", "uline"):
            content, i2 = _read_brace_arg(s, i)
            flush()
            self._parse_fragment(content, out, bold=bold, italic=italic, code=code)
            return i2

        if cmd in ("uppercase", "MakeUppercase"):
            content, i2 = _read_brace_arg(s, i)
            flush()
            self._parse_fragment(content.upper(), out, bold=bold, italic=italic, code=code)
            return i2

        # --- Color (discard color, keep content) ---
        if cmd in ("textcolor", "color"):
            _, i2 = _read_brace_arg(s, i)    # consume color arg
            content, i3 = _read_brace_arg(s, i2)
            flush()
            self._parse_fragment(content, out, bold=bold, italic=italic, code=code)
            return i3

        if cmd == "colorbox":
            _, i2 = _read_brace_arg(s, i)
            content, i3 = _read_brace_arg(s, i2)
            flush()
            self._parse_fragment(content, out, bold=bold, italic=italic, code=code)
            return i3

        # --- Cite variants ---
        if cmd.startswith("cite") or cmd in ("nocite",):
            # Skip optional args
            i2 = i
            while i2 < len(s) and s[i2] in " \t\n":
                i2 += 1
            # Consume up to two optional args
            for _ in range(2):
                if i2 < len(s) and s[i2] == "[":
                    end_b = s.find("]", i2)
                    i2 = (end_b + 1) if end_b != -1 else i2 + 1
                    while i2 < len(s) and s[i2] in " \t\n":
                        i2 += 1
            keys_str, i3 = _read_brace_arg(s, i2)
            flush()
            if cmd != "nocite":
                out.append(self._make_cite(keys_str))
            return i3

        # --- Ref variants ---
        if cmd in ("ref", "eqref", "autoref", "cref", "Cref",
                   "pageref", "vref", "Autoref", "labelcref"):
            # Optional arg for cref
            i2 = i
            while i2 < len(s) and s[i2] in " \t\n":
                i2 += 1
            if i2 < len(s) and s[i2] == "[":
                end_b = s.find("]", i2)
                i2 = (end_b + 1) if end_b != -1 else i2 + 1
            key, i3 = _read_brace_arg(s, i2)
            flush()
            resolved = self._resolve_ref(key.strip())
            buf.append(resolved)
            return i3

        # --- Footnote ---
        if cmd == "footnote":
            content, i2 = _read_brace_arg(s, i)
            flush()
            fn_runs: List[RunSpec] = []
            self._parse_fragment(content, fn_runs, bold=False, italic=False, code=False)
            out.append({"type": "footnote", "fn_runs": fn_runs, "text": content})
            return i2

        if cmd == "footnotetext":
            content, i2 = _read_brace_arg(s, i)
            flush()
            fn_runs: List[RunSpec] = []
            self._parse_fragment(content, fn_runs, bold=False, italic=False, code=False)
            out.append({"type": "footnote", "fn_runs": fn_runs, "text": content})
            return i2

        # --- URL / href ---
        if cmd == "url":
            url, i2 = _read_brace_arg(s, i)
            buf.append(url)
            return i2

        if cmd == "href":
            _, i2 = _read_brace_arg(s, i)      # URL (discard)
            text, i3 = _read_brace_arg(s, i2)
            flush()
            self._parse_fragment(text, out, bold=bold, italic=italic, code=code)
            return i3

        # --- Spacing / misc (discard) ---
        if cmd in ("hspace", "hspace*", "vspace", "vspace*",
                   "phantom", "hphantom", "vphantom"):
            _, i2 = _read_brace_arg(s, i)
            return i2

        if cmd in ("quad", "qquad", "enspace", "thinspace",
                   "medspace", "thickspace", "hfill", "noindent",
                   "centering", "par", "newline", "linebreak",
                   "smallskip", "medskip", "bigskip", "strut",
                   "protect", "relax", "nobreakspace", "leavevmode"):
            return i

        if cmd in ("mbox", "hbox", "fbox", "framebox", "makebox"):
            # Skip optional args
            i2 = i
            while i2 < len(s) and s[i2] in " \t\n":
                i2 += 1
            for _ in range(2):
                if i2 < len(s) and s[i2] == "[":
                    end_b = s.find("]", i2)
                    i2 = (end_b + 1) if end_b != -1 else i2 + 1
                    while i2 < len(s) and s[i2] in " \t\n":
                        i2 += 1
            content, i3 = _read_brace_arg(s, i2)
            flush()
            self._parse_fragment(content, out, bold=bold, italic=italic, code=code)
            return i3

        if cmd in ("label", "index"):
            _, i2 = _read_brace_arg(s, i)
            return i2

        # --- Section / environment wrapper remnants (strip) ---
        if cmd in ("section", "subsection", "subsubsection", "chapter",
                   "paragraph", "subparagraph"):
            # Skip optional short title
            i2 = i
            while i2 < len(s) and s[i2] in " \t\n":
                i2 += 1
            if i2 < len(s) and s[i2] == "[":
                end_b = s.find("]", i2)
                i2 = (end_b + 1) if end_b != -1 else i2 + 1
            content, i3 = _read_brace_arg(s, i2)
            flush()
            self._parse_fragment(content, out, bold=True, italic=italic, code=code)
            return i3

        if cmd in ("begin", "end"):
            _, i2 = _read_brace_arg(s, i)
            return i2

        if cmd in ("item",):
            buf.append("• ")
            return i

        # Font switches (no arg)
        if cmd in ("bfseries",):
            return i
        if cmd in ("itshape", "slshape"):
            return i
        if cmd in ("ttfamily",):
            return i
        if cmd in ("normalsize", "large", "Large", "LARGE", "huge", "Huge",
                   "small", "footnotesize", "scriptsize", "tiny"):
            return i

        # Math operators & symbols (keep as-is for plain-text fallback)
        if cmd in ("operatorname", "mathrm", "mathbf", "mathit",
                   "mathcal", "mathbb", "boldsymbol", "mathsf"):
            content, i2 = _read_brace_arg(s, i)
            buf.append(content)
            return i2

        # Superscript / subscript text
        if cmd in ("textsuperscript",):
            content, i2 = _read_brace_arg(s, i)
            flush()
            out.append({"type": "superscript", "text": content,
                        "bold": bold, "italic": italic})
            return i2

        if cmd in ("textsubscript",):
            content, i2 = _read_brace_arg(s, i)
            flush()
            out.append({"type": "subscript", "text": content,
                        "bold": bold, "italic": italic})
            return i2

        # Unknown command — try to skip its arguments gracefully
        return i   # leave i unchanged; outer loop will advance

    # ----------------------------------------------------------------- helpers

    def _make_cite(self, keys_str: str) -> RunSpec:
        """Build a superscript citation run from comma-separated keys."""
        keys = [k.strip() for k in keys_str.split(",")]
        ids = []
        for k in keys:
            if k in self.cite_lookup:
                ids.append(str(self.cite_lookup[k]))
            else:
                ids.append("文献不存在")
        label = "[" + ",".join(ids) + "]"
        return {"type": "superscript", "text": label, "bold": False, "italic": False}

    def _resolve_ref(self, key: str) -> str:
        """Resolve a \\ref{key} to its display label via refmap."""
        # Try current chapter first
        val = self.refmap.get(key)
        if val:
            return val
        # Try all chapters
        for ch_map in self.full_refmap.values():
            val = ch_map.get(key)
            if val:
                return val
        return f"[引用不存在:{key}]"

    def _make_math(self, latex_math: str) -> RunSpec:
        """Build an inline math RunSpec."""
        if MATH_OK:
            omml = _latex_math_to_omml(latex_math)
            if omml:
                return {"type": "math", "text": latex_math, "omml": omml}
        return {"type": "text", "text": f"${latex_math}$",
                "bold": False, "italic": True, "code": False}


# ---------------------------------------------------------------------------
# Footnote manager (native Word footnotes via OPC XML injection)
# ---------------------------------------------------------------------------

class FootnoteManager:
    """
    Collects footnote content during rendering and injects footnotes.xml
    into the docx package after the document is fully built.

    Usage:
        fm = FootnoteManager(doc)
        # during rendering, pass fm to apply_run_specs
        fm.inject()   # call once before doc.save()
    """

    FOOTNOTES_CT  = (
        "application/vnd.openxmlformats-officedocument"
        ".wordprocessingml.footnotes+xml"
    )
    FOOTNOTES_RT  = (
        "http://schemas.openxmlformats.org/officeDocument"
        "/2006/relationships/footnotes"
    )
    FOOTNOTES_URI = "/word/footnotes.xml"

    def __init__(self, doc: Document):
        self._doc      = doc
        self._counter  = 1          # footnote ids start at 1
        self._entries: List[Tuple[int, List[RunSpec]]] = []

    # ---------------------------------------------------------------- public

    def add_footnote_to_para(self, para, fn_runs: List[RunSpec]) -> int:
        """
        Insert a footnote reference mark into *para* and record content.
        Returns the assigned footnote id.
        """
        fn_id = self._counter
        self._counter += 1
        self._entries.append((fn_id, fn_runs))
        self._add_ref_mark(para, fn_id)
        return fn_id

    def inject(self) -> None:
        """
        Build footnotes.xml and wire it into the document OPC package.
        Must be called once, right before doc.save().
        """
        if not self._entries:
            return
        xml_bytes = self._build_xml()
        doc_part  = self._doc.part

        from docx.opc.part import Part
        from docx.opc.packuri import PackURI
        fn_part = Part(
            PackURI(self.FOOTNOTES_URI),
            self.FOOTNOTES_CT,
            xml_bytes,
            doc_part.package,
        )
        doc_part.relate_to(fn_part, self.FOOTNOTES_RT)

    # --------------------------------------------------------------- private

    def _add_ref_mark(self, para, fn_id: int) -> None:
        """Add <w:footnoteReference w:id="N"/> as a superscript run."""
        run = para.add_run()
        r   = run._r
        rPr = OxmlElement("w:rPr")
        rStyle = OxmlElement("w:rStyle")
        rStyle.set(qn("w:val"), "FootnoteReference")
        rPr.append(rStyle)
        r.insert(0, rPr)
        fnRef = OxmlElement("w:footnoteReference")
        fnRef.set(qn("w:id"), str(fn_id))
        r.append(fnRef)

    def _build_xml(self) -> bytes:
        """Build a complete footnotes.xml byte string."""
        from lxml import etree

        W = _W_NS
        root = etree.Element(f"{{{W}}}footnotes", nsmap={
            "w": W,
            "r": _R_NS,
            "m": _OMML_NS,
        })

        # Separator footnotes (required by Word spec)
        for fn_type, fn_id in (("separator", -1), ("continuationSeparator", 0)):
            fn_el = etree.SubElement(root, f"{{{W}}}footnote")
            fn_el.set(f"{{{W}}}type", fn_type)
            fn_el.set(f"{{{W}}}id",   str(fn_id))
            p_el = etree.SubElement(fn_el, f"{{{W}}}p")
            r_el = etree.SubElement(p_el,  f"{{{W}}}r")
            etree.SubElement(r_el, f"{{{W}}}{fn_type}")

        # Actual footnotes
        for fn_id, fn_runs in self._entries:
            fn_el = etree.SubElement(root, f"{{{W}}}footnote")
            fn_el.set(f"{{{W}}}id", str(fn_id))
            p_el  = etree.SubElement(fn_el, f"{{{W}}}p")
            pPr   = etree.SubElement(p_el,  f"{{{W}}}pPr")
            pSty  = etree.SubElement(pPr,   f"{{{W}}}pStyle")
            pSty.set(f"{{{W}}}val", "FootnoteText")

            # Reference mark run
            r_ref = etree.SubElement(p_el, f"{{{W}}}r")
            rPr   = etree.SubElement(r_ref, f"{{{W}}}rPr")
            rSty  = etree.SubElement(rPr,   f"{{{W}}}rStyle")
            rSty.set(f"{{{W}}}val", "FootnoteReference")
            etree.SubElement(r_ref, f"{{{W}}}footnoteRef")

            # Space after mark
            r_sp  = etree.SubElement(p_el, f"{{{W}}}r")
            t_sp  = etree.SubElement(r_sp, f"{{{W}}}t")
            t_sp.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            t_sp.text = " "

            # Content runs (rendered as plain text from fn_runs)
            text = self._runs_to_plain_text(fn_runs)
            r_ct  = etree.SubElement(p_el, f"{{{W}}}r")
            t_ct  = etree.SubElement(r_ct, f"{{{W}}}t")
            t_ct.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            t_ct.text = text

        return etree.tostring(
            root,
            xml_declaration=True,
            encoding="UTF-8",
            standalone=True,
        )

    @staticmethod
    def _runs_to_plain_text(runs: List[RunSpec]) -> str:
        parts = []
        for r in runs:
            if r.get("type") == "text":
                parts.append(r.get("text", ""))
            elif r.get("type") == "math":
                parts.append(f"${r.get('text','')}$")
            elif r.get("type") == "superscript":
                parts.append(r.get("text", ""))
        return "".join(parts)




def apply_run_specs(para, specs: List[RunSpec], doc: Document,
                    base_font: str = FONT_BODY, base_size: Pt = SIZE_BODY,
                    footnote_mgr: Optional["FootnoteManager"] = None) -> None:
    """
    Append run specs to an existing paragraph (no paragraph creation here).
    Footnotes generate Word native footnotes when footnote_mgr is provided,
    otherwise fall back to inline parenthetical notes.
    """
    for spec in specs:
        stype = spec.get("type", "text")

        if stype == "math":
            omml = spec.get("omml")
            if omml:
                _insert_omml_into_para(para, omml)
            else:
                run = para.add_run(spec.get("text", ""))
                run.font.name = FONT_CODE
                run.font.size = SIZE_CODE
            continue

        if stype == "footnote":
            fn_runs = spec.get("fn_runs", [])
            if footnote_mgr is not None:
                footnote_mgr.add_footnote_to_para(para, fn_runs)
            else:
                # Fallback: inline note in parentheses
                text = FootnoteManager._runs_to_plain_text(fn_runs)
                run  = para.add_run(f"（注：{text}）")
                run.font.name = base_font
                run.font.size = Pt(9)
                _set_run_cjk_font(run, base_font)
            continue

        if stype == "superscript":
            run = para.add_run(spec["text"])
            run.font.size = Pt(7)
            run.font.superscript = True
            run.font.name = base_font
            _set_run_cjk_font(run, base_font)
            continue

        if stype == "subscript":
            run = para.add_run(spec["text"])
            run.font.size = Pt(8)
            run.font.subscript = True
            run.font.name = base_font
            _set_run_cjk_font(run, base_font)
            continue

        # Plain text run
        text = spec.get("text", "")
        if not text:
            continue
        run = para.add_run(text)
        run.bold   = spec.get("bold",   False)
        run.italic = spec.get("italic", False)
        font_name  = FONT_CODE if spec.get("code") else base_font
        run.font.name = font_name
        run.font.size = SIZE_CODE if spec.get("code") else base_size
        _set_run_cjk_font(run, font_name)


# ---------------------------------------------------------------------------
# Paragraph-level renderers
# ---------------------------------------------------------------------------

def add_page_break(doc: Document) -> None:
    para = doc.add_paragraph()
    run = para.add_run()
    run.add_break(docx_page_break())


def docx_page_break():
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    br = OxmlElement("w:br")
    br.set(qn("w:type"), "page")
    return br


def _do_page_break(doc: Document) -> None:
    """Insert a page break via paragraph."""
    para = doc.add_paragraph()
    para._p.append(_make_page_break_element())


def _make_page_break_element():
    br = OxmlElement("w:br")
    br.set(qn("w:type"), "page")
    run = OxmlElement("w:r")
    run.append(br)
    return run


# ------------------------------------------------------------------

def add_chapter_heading(doc: Document, chapter: int, title: str) -> None:
    """Add 第X章 Title as Heading 1."""
    zh_num = chapter_num_to_zh(chapter)
    heading_text = f"第{zh_num}章  {title}"
    para = doc.add_paragraph(style="Heading 1")
    run = para.add_run(heading_text)
    run.bold = True
    run.font.size = SIZE_H1
    _set_run_cjk_font(run, FONT_HEADING)


def _is_section_command(text: str) -> bool:
    """Return True if text IS (or primarily contains) a sectioning command."""
    s = text.strip()
    return bool(re.match(
        r"^(?:\\label\{[^}]*\}\s*)?\\(?:part|chapter|section|subsection"
        r"|subsubsection|paragraph|subparagraph)\*?",
        s
    ))


def _parse_section_label(env_label: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Parse env_label like '3.1节' or '3.1.2小节'.
    Returns (numbering_prefix, heading_type) e.g. ('3.1', '节') or ('3.1.2', '小节').
    """
    m = re.match(r"^(\d+\.\d+(?:\.\d+)*)([节小节]+)$", env_label)
    if m:
        return m.group(1), m.group(2)
    return None, None


def add_section_heading(doc: Document, env_label: str,
                        translation: str, inline_parser: InlineParser) -> None:
    """Add a section or subsection heading."""
    num_prefix, htype = _parse_section_label(env_label)
    if num_prefix is None:
        return

    # Determine heading level
    depth = num_prefix.count(".")
    if depth == 1:
        style_name = "Heading 2"
        size = SIZE_H2
    else:
        style_name = "Heading 3"
        size = SIZE_H3

    # Extract title text from translation: \section{...} or \subsection{...}
    title_text = _extract_heading_text(translation)
    full_text  = f"{num_prefix}  {title_text}"

    para = doc.add_paragraph(style=style_name)
    run  = para.add_run(full_text)
    run.bold      = True
    run.font.size = size
    _set_run_cjk_font(run, FONT_HEADING)


def _extract_heading_text(translation: str) -> str:
    """
    Extract the title content from a heading command like \\section{...}.
    """
    # Match section-like commands
    m = re.search(
        r"\\(?:section|subsection|subsubsection|chapter|paragraph)\*?"
        r"\s*(?:\[[^\]]*\])?\s*\{",
        translation
    )
    if not m:
        # Possibly just plain text
        return translation.strip()
    start = m.end() - 1  # position of '{'
    end   = _find_balanced_brace(translation, start)
    return translation[start+1:end-1].strip()


def add_abstract_paragraph(doc: Document, translation: str,
                            inline_parser: InlineParser,
                            footnote_mgr: Optional[FootnoteManager] = None) -> None:
    """Add abstract as a normal paragraph block."""
    body = _strip_env(translation, "abstract")

    para_lbl = doc.add_paragraph(style="Heading 2")
    run = para_lbl.add_run("摘要")
    run.bold = True
    run.font.size = SIZE_H2
    _set_run_cjk_font(run, FONT_HEADING)

    add_body_paragraph(doc, body, inline_parser, footnote_mgr=footnote_mgr)


def add_body_paragraph(doc: Document, translation: str,
                       inline_parser: InlineParser,
                       font: str = FONT_BODY, size: Pt = SIZE_BODY,
                       footnote_mgr: Optional[FootnoteManager] = None) -> None:
    """Add a justified body paragraph."""
    text = _preprocess_body_text(translation)
    if not text.strip():
        return

    para = doc.add_paragraph()
    para.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    _set_para_spacing(para)

    specs = inline_parser.parse(text)
    apply_run_specs(para, specs, doc, base_font=font, base_size=size,
                    footnote_mgr=footnote_mgr)


def _preprocess_body_text(translation: str) -> str:
    """
    Strip environment wrappers from body text.
    Remove \\begin{...} and \\end{...} lines but keep inner content.
    """
    # Remove abstract/theorem/proof etc. wrappers
    result = re.sub(
        r"\\begin\{[^}]+\}|\\end\{[^}]+\}",
        "",
        translation
    )
    result = re.sub(r"\\label\{[^}]+\}", "", result)
    result = result.strip()
    return result


def add_figure_paragraph(doc: Document, para_data: dict,
                         chapter: int, inputs_dir: str,
                         inline_parser: InlineParser,
                         env_label: str,
                         footnote_mgr: Optional[FootnoteManager] = None) -> None:
    """Insert figure image(s) + translated caption."""
    text        = para_data.get("text", "")
    translation = para_data.get("translation", "")

    img_paths = _extract_image_paths(text, chapter, inputs_dir)
    caption   = _extract_caption_text(translation)

    for img_path in img_paths:
        if not img_path or not os.path.exists(img_path):
            ph = doc.add_paragraph(f"[图片缺失: {os.path.basename(img_path)}]")
            ph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            continue

        png_path = _ensure_png(img_path)
        if png_path is None:
            ph = doc.add_paragraph(f"[PDF图片转换失败: {os.path.basename(img_path)}]")
            ph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            continue

        try:
            para = doc.add_paragraph()
            para.alignment = WD_ALIGN_PARAGRAPH.CENTER
            _set_para_spacing(para, before_pt=6, after_pt=3)
            run = para.add_run()
            run.add_picture(png_path, width=Cm(PAGE_CONTENT_WIDTH_CM * 0.9))
        except Exception as exc:
            print(f"[WARN] Could not insert image {png_path}: {exc}", file=sys.stderr)
            ph = doc.add_paragraph(f"[图片插入失败: {os.path.basename(img_path)}]")
            ph.alignment = WD_ALIGN_PARAGRAPH.CENTER

    if caption:
        cap_para = doc.add_paragraph(style="Caption")
        cap_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _set_para_spacing(cap_para, before_pt=3, after_pt=6)
        specs = inline_parser.parse(f"{env_label} {caption}")
        apply_run_specs(cap_para, specs, doc,
                        base_font=FONT_CAPTION, base_size=SIZE_CAPTION,
                        footnote_mgr=footnote_mgr)


def add_table_paragraph(doc: Document, para_data: dict,
                        inline_parser: InlineParser,
                        env_label: str,
                        footnote_mgr: Optional[FootnoteManager] = None) -> None:
    """Render a LaTeX table using table.py, with caption above."""
    text        = para_data.get("text", "")
    translation = para_data.get("translation", "")

    caption = _extract_caption_text(translation)
    if caption:
        cap_para = doc.add_paragraph(style="Caption")
        cap_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _set_para_spacing(cap_para, before_pt=6, after_pt=3)
        specs = inline_parser.parse(f"{env_label} {caption}")
        apply_run_specs(cap_para, specs, doc,
                        base_font=FONT_CAPTION, base_size=SIZE_CAPTION,
                        footnote_mgr=footnote_mgr)

    result = parse_latex_table(text, doc, translation_text=translation)
    if result is None:
        fb = doc.add_paragraph(f"[表格解析失败，请参考原文: {env_label}]")
        fb.alignment = WD_ALIGN_PARAGRAPH.CENTER

    spacer = doc.add_paragraph()
    _set_para_spacing(spacer, before_pt=0, after_pt=6)


def add_algorithm_paragraph(doc: Document, translation: str,
                             env_label: str) -> None:
    """Render algorithm/pseudocode block as monospace paragraphs."""
    # Extract caption if any
    caption = _extract_caption_text(translation)

    if caption:
        cap_para = doc.add_paragraph(style="Caption")
        cap_para.alignment = WD_ALIGN_PARAGRAPH.LEFT
        _set_para_spacing(cap_para, before_pt=6, after_pt=3)
        run = cap_para.add_run(f"{env_label}  {caption}")
        run.bold = True
        run.font.name = FONT_CAPTION
        run.font.size = SIZE_CAPTION
        _set_run_cjk_font(run, FONT_CAPTION)

    # Extract body (between begin{algorithm}/begin{algorithmic} ... end)
    body = _extract_algorithm_body(translation)
    lines = body.splitlines()

    for line in lines:
        clean = _clean_algorithm_line(line)
        if not clean.strip():
            continue
        p = doc.add_paragraph(style="Code")
        p.paragraph_format.left_indent = Cm(_count_leading_spaces(line) * 0.2)
        run = p.add_run(clean.rstrip())
        run.font.name = FONT_CODE
        run.font.size = SIZE_CODE


def add_code_paragraph(doc: Document, text: str, env_label: str) -> None:
    """Render verbatim / lstlisting code block."""
    # Extract code content
    body = _extract_verbatim_body(text)
    lines = body.splitlines()

    for line in lines:
        p = doc.add_paragraph(style="Code")
        run = p.add_run(line.rstrip())
        run.font.name = FONT_CODE
        run.font.size = SIZE_CODE


# ---------------------------------------------------------------------------
# Helper: environment body extraction
# ---------------------------------------------------------------------------

def _strip_env(text: str, env_name: str) -> str:
    """Strip \\begin{env}...\\end{env} wrapper from text."""
    text = re.sub(rf"\\begin\{{{re.escape(env_name)}\}}", "", text)
    text = re.sub(rf"\\end\{{{re.escape(env_name)}\}}",   "", text)
    return text.strip()


def _extract_caption_text(text: str) -> str:
    """Extract text from first \\caption{...}."""
    m = re.search(r"\\caption\*?\s*(?:\[[^\]]*\])?\s*\{", text, re.DOTALL)
    if not m:
        return ""
    start = m.end() - 1
    end   = _find_balanced_brace(text, start)
    raw   = text[start+1:end-1]
    # Remove label inside caption
    raw = re.sub(r"\\label\{[^}]+\}", "", raw)
    return raw.strip()


def _extract_image_paths(text: str, chapter: int, inputs_dir: str) -> List[str]:
    """
    Extract image file paths from \\includegraphics commands.
    Resolves relative paths against inputs/{chapter}/.
    """
    base_dir = os.path.join(inputs_dir, str(chapter))
    paths: List[str] = []

    for m in re.finditer(
        r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{([^}]+)\}", text
    ):
        rel = m.group(1).strip()
        # Try as-is
        candidates = [
            os.path.join(base_dir, rel),
            os.path.join(base_dir, rel + ".png"),
            os.path.join(base_dir, rel + ".pdf"),
            os.path.join(base_dir, rel + ".jpg"),
            os.path.join(base_dir, rel + ".jpeg"),
            os.path.join(base_dir, rel + ".eps"),
        ]
        for c in candidates:
            if os.path.exists(c):
                paths.append(c)
                break
        else:
            paths.append(os.path.join(base_dir, rel))  # keep for error reporting

    return paths


def _ensure_png(img_path: str) -> Optional[str]:
    """
    Convert image to PNG if needed.
    Returns path to a PNG file, or None on failure.
    """
    ext = Path(img_path).suffix.lower()
    if ext in (".png", ".jpg", ".jpeg"):
        return img_path
    if ext == ".pdf":
        if not PDF_OK:
            return None
        try:
            pages = _pdf_to_images(img_path, dpi=150, first_page=1, last_page=1)
            if not pages:
                return None
            tmp = tempfile.NamedTemporaryFile(
                suffix=".png", delete=False, dir=tempfile.gettempdir()
            )
            pages[0].save(tmp.name, "PNG")
            tmp.close()
            return tmp.name
        except Exception as exc:
            print(f"[WARN] PDF→PNG failed for {img_path}: {exc}", file=sys.stderr)
            return None
    # EPS or other: try Pillow
    try:
        from PIL import Image
        img = Image.open(img_path)
        tmp = tempfile.NamedTemporaryFile(
            suffix=".png", delete=False, dir=tempfile.gettempdir()
        )
        img.save(tmp.name, "PNG")
        tmp.close()
        return tmp.name
    except Exception as exc:
        print(f"[WARN] Image convert failed for {img_path}: {exc}", file=sys.stderr)
        return None


def _extract_algorithm_body(text: str) -> str:
    """
    Extract the body of an algorithm environment,
    stripping algorithmic/algorithm wrappers and \\caption.
    """
    # Remove outer \begin{algorithm}...\end{algorithm}
    body = re.sub(r"\\begin\{algorithm[^\}]*\}", "", text)
    body = re.sub(r"\\end\{algorithm[^\}]*\}",   "", body)
    # Remove \begin{algorithmic}...\end{algorithmic} wrappers but keep content
    body = re.sub(r"\\begin\{algorithmic[^\}]*\}", "", body)
    body = re.sub(r"\\end\{algorithmic[^\}]*\}",   "", body)
    # Remove caption and label
    body = re.sub(r"\\caption\*?\s*(?:\[[^\]]*\])?\s*\{[^}]*\}", "", body)
    body = re.sub(r"\\label\{[^}]+\}", "", body)
    return body.strip()


def _extract_verbatim_body(text: str) -> str:
    """Extract content of verbatim / lstlisting environment."""
    for env in ("lstlisting", "minted", "verbatim", "Verbatim", "alltt"):
        m = re.search(
            rf"\\begin\{{{re.escape(env)}\}}(?:\[[^\]]*\])?(.*?)\\end\{{{re.escape(env)}\}}",
            text, re.DOTALL
        )
        if m:
            return m.group(1)
    # Fallback: strip wrappers generically
    body = re.sub(r"\\begin\{[^}]+\}", "", text)
    body = re.sub(r"\\end\{[^}]+\}",   "", body)
    return body.strip()


def _clean_algorithm_line(line: str) -> str:
    """
    Convert common algorithm macro names to plain text.
    """
    s = line
    replacements = [
        (r"\\REQUIRE\b",  "输入："),
        (r"\\ENSURE\b",   "输出："),
        (r"\\STATE\b",    ""),
        (r"\\IF\b",       "如果 "),
        (r"\\ELSIF\b",    "否则如果 "),
        (r"\\ELSE\b",     "否则"),
        (r"\\ENDIF\b",    "结束如果"),
        (r"\\FOR\b",      "对于 "),
        (r"\\FORALL\b",   "对所有 "),
        (r"\\ENDFOR\b",   "结束循环"),
        (r"\\WHILE\b",    "当 "),
        (r"\\ENDWHILE\b", "结束循环"),
        (r"\\RETURN\b",   "返回 "),
        (r"\\PRINT\b",    "输出 "),
        (r"\\COMMENT\{([^}]*)\}", r"// \1"),
        (r"\\algorithmiccomment\{([^}]*)\}", r"// \1"),
        (r"\\FUNCTION\{([^}]*)\}\{([^}]*)\}", r"函数 \1(\2)"),
        (r"\\ENDFUNCTION\b", "结束函数"),
        (r"\\PROCEDURE\{([^}]*)\}\{([^}]*)\}", r"过程 \1(\2)"),
        (r"\\ENDPROCEDURE\b", "结束过程"),
    ]
    for pattern, repl in replacements:
        s = re.sub(pattern, repl, s)
    # Remove remaining \cmd (standalone with no args)
    s = re.sub(r"\\[a-zA-Z]+\b", "", s)
    # Clean up braces
    s = s.replace("{", "").replace("}", "")
    return s


def _count_leading_spaces(line: str) -> int:
    return len(line) - len(line.lstrip())


# ---------------------------------------------------------------------------
# Brace matching utility (module-level)
# ---------------------------------------------------------------------------

def _find_balanced_brace(s: str, start: int) -> int:
    """Return index after closing '}' for '{' at s[start]."""
    assert s[start] == "{"
    depth = 1
    i = start + 1
    n = len(s)
    while i < n and depth:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    return i


def _read_brace_arg(s: str, i: int) -> Tuple[str, int]:
    while i < len(s) and s[i] in " \t\n":
        i += 1
    if i >= len(s) or s[i] != "{":
        return "", i
    end = _find_balanced_brace(s, i)
    return s[i+1:end-1], end


# ---------------------------------------------------------------------------
# References page
# ---------------------------------------------------------------------------

def add_references_page(doc: Document, citations: Dict[str, Any]) -> None:
    """Append a page break and full reference list."""
    # Page break
    _do_page_break(doc)

    # "参考文献" heading
    h = doc.add_paragraph(style="Heading 1")
    run = h.add_run("参考文献")
    run.bold = True
    run.font.size = SIZE_H1
    _set_run_cjk_font(run, FONT_HEADING)

    # Sort by id
    entries = sorted(citations.values(), key=lambda v: v["id"])
    seen_ids: set = set()
    for entry in entries:
        eid = entry["id"]
        if eid in seen_ids:
            continue
        seen_ids.add(eid)
        line = f"[{eid}] {entry['citation']}"
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        _set_para_spacing(p, before_pt=2, after_pt=2, line_val=1.15)
        run = p.add_run(line)
        run.font.name = FONT_BODY
        run.font.size = Pt(10.5)
        _set_run_cjk_font(run, FONT_BODY)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def dispatch_paragraph(
    doc: Document,
    para_data: dict,
    chapter: int,
    inline_parser: InlineParser,
    inputs_dir: str,
    footnote_mgr: Optional[FootnoteManager] = None,
) -> None:
    """Route a paragraph to the appropriate renderer based on env_label."""
    env_label   = para_data.get("env_label", "")
    text        = para_data.get("text", "")
    translation = para_data.get("translation", text)

    # --- Abstract ---
    if env_label == "摘要":
        add_abstract_paragraph(doc, translation, inline_parser,
                                footnote_mgr=footnote_mgr)
        return

    # --- Section heading: only if paragraph IS the section command ---
    if re.match(r"^\d+\.\d+节$", env_label):
        if _is_section_command(translation):
            add_section_heading(doc, env_label, translation, inline_parser)
            return
        # else: body paragraph belonging to this section

    # --- Subsection heading ---
    if re.match(r"^\d+\.\d+\.\d+小节$", env_label):
        if _is_section_command(translation):
            add_section_heading(doc, env_label, translation, inline_parser)
            return

    # --- Figure ---
    if re.match(r"^图\d+-\d+$", env_label):
        add_figure_paragraph(doc, para_data, chapter, inputs_dir,
                             inline_parser, env_label,
                             footnote_mgr=footnote_mgr)
        return

    # --- Table ---
    if re.match(r"^表\d+-\d+$", env_label):
        add_table_paragraph(doc, para_data, inline_parser, env_label,
                            footnote_mgr=footnote_mgr)
        return

    # --- Display math (equation block) ---
    if re.match(r"^式\d+-\d+$", env_label):
        add_display_math_paragraph(doc, text)
        return

    # --- Algorithm ---
    if re.match(r"^算法\d+-\d+$", env_label):
        add_algorithm_paragraph(doc, translation, env_label)
        return

    # --- Code block ---
    if re.match(r"^代码\d+-\d+$", env_label):
        add_code_paragraph(doc, text, env_label)
        return

    # --- Plain text paragraph (default) ---
    if translation.strip():
        add_body_paragraph(doc, translation, inline_parser,
                           footnote_mgr=footnote_mgr)


# ---------------------------------------------------------------------------
# Main rendering loop
# ---------------------------------------------------------------------------

def render(
    labeled_path: str,
    refmap_path: str,
    citations_path: str,
    inputs_dir: str,
    output_path: str,
    toc: bool = False,
    font_size_pt: float = 12.0,
    page_size: str = "a4",
) -> None:
    # Load data
    with open(labeled_path,   "r", encoding="utf-8") as f:
        labeled = json.load(f)
    with open(refmap_path,    "r", encoding="utf-8") as f:
        full_refmap: Dict[str, Dict[str, str]] = json.load(f)
    with open(citations_path, "r", encoding="utf-8") as f:
        citations: Dict[str, Any] = json.load(f)

    cite_lookup = _build_cite_lookup(citations)

    # Build document
    doc = Document()
    setup_styles(doc, font_size_pt=font_size_pt)
    set_page_size(doc, page_size)

    # Footnote manager (shared across all chapters)
    footnote_mgr = FootnoteManager(doc)

    # Table of contents (at beginning)
    if toc:
        insert_toc(doc)
        _do_page_break(doc)

    # Process each chapter document
    documents = labeled.get("documents", [])
    for doc_idx, chapter_doc in enumerate(documents):
        chapter = chapter_doc.get("chapter", 0)
        title   = chapter_doc.get("title",   "")
        paras   = chapter_doc.get("paragraphs", [])

        # Chapter page break (except first if TOC already did it)
        if doc_idx > 0:
            _do_page_break(doc)

        # Build per-chapter refmap
        chapter_refmap = full_refmap.get(str(chapter), {})

        # Inline parser for this chapter
        inline_parser = InlineParser(
            cite_lookup=cite_lookup,
            refmap=chapter_refmap,
            chapter=chapter,
            full_refmap=full_refmap,
        )

        # Chapter heading
        clean_title = _clean_title(title)
        add_chapter_heading(doc, chapter, clean_title)

        # Paragraphs
        for para_data in paras:
            try:
                dispatch_paragraph(doc, para_data, chapter,
                                   inline_parser, inputs_dir,
                                   footnote_mgr=footnote_mgr)
            except Exception as exc:
                print(
                    f"[WARN] Para id={para_data.get('id')} "
                    f"chapter={chapter} env_label={para_data.get('env_label')}: {exc}",
                    file=sys.stderr,
                )

    # References
    add_references_page(doc, citations)

    # Inject footnotes into OPC package (must be before save)
    footnote_mgr.inject()

    # Save
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    doc.save(output_path)
    print(f"[INFO] Saved → {output_path}")


def _clean_title(title: str) -> str:
    """Strip LaTeX commands from a title string."""
    t = re.sub(r"\\[a-zA-Z]+\s*(?:\{[^}]*\})*", " ", title)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _build_cite_lookup(citations: Dict[str, Any]) -> Dict[str, int]:
    return {k: v["id"] for k, v in citations.items()}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Render labeled LaTeX chunks into a Word .docx document.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--labeled",    required=True,
                    help="Path to labeled.json (output of label.py)")
    ap.add_argument("--refmap",     required=True,
                    help="Path to refmap.json (output of refmap.py)")
    ap.add_argument("--citations",  required=True,
                    help="Path to citations.json (output of bib.py)")
    ap.add_argument("--inputs-dir", required=True, dest="inputs_dir",
                    help="Root directory for input LaTeX projects (contains {chapter}/ subdirs)")
    ap.add_argument("--output",     required=True,
                    help="Output .docx file path")
    ap.add_argument("--toc",        action="store_true", default=False,
                    help="Include auto-generated table of contents")
    ap.add_argument("--font-size",  type=float, default=12.0, dest="font_size",
                    help="Base body font size in pt (default: 12)")
    ap.add_argument("--page-size",  default="a4", choices=["a4", "letter"],
                    dest="page_size",
                    help="Page size: a4 (default) or letter")
    args = ap.parse_args()

    render(
        labeled_path   = args.labeled,
        refmap_path    = args.refmap,
        citations_path = args.citations,
        inputs_dir     = args.inputs_dir,
        output_path    = args.output,
        toc            = args.toc,
        font_size_pt   = args.font_size,
        page_size      = args.page_size,
    )


if __name__ == "__main__":
    main()