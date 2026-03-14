#!/usr/bin/env python3
"""
render.py — Append translated JSON paragraphs into a Word (.docx) document.

Reads the replaced.json pipeline output and appends each chunk to an existing
or newly created .docx file using python-docx.

Key design decisions:
  - ALL LaTeX (body text, inline math, display equations) is converted to
    proper Word OMML math via pandoc (-f latex -t docx), then the resulting
    XML nodes are injected directly into our document — no lossy plain-text
    math rendering.
  - Tables are rendered by calling the Claude API: we send the translated
    LaTeX source and ask Claude to emit python-docx code, then exec() it.
  - Fonts are set uniformly with all four XML slots (ascii/eastAsia/hAnsi/cs):
      正文  body     -> 宋体
      标题  heading  -> 黑体
      注释  caption  -> 楷体
      代码  code     -> Courier New

Usage:
    python render.py [--json replaced.json] [--docx output.docx]
                     [--figures-dir .] [--no-tables] [--skip-title]

Dependencies (Python): python-docx, lxml, anthropic
Dependencies (system): pandoc
"""

import argparse
import copy
import json
import os
import re
import subprocess
import sys
import tempfile
import traceback
import zipfile
from pathlib import Path

import anthropic
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Inches, Pt, RGBColor
from lxml import etree

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

FONT_BODY    = "宋体"
FONT_HEADING = "黑体"
FONT_CAPTION = "楷体"
FONT_CODE    = "Courier New"

SIZE_BODY     = 12
SIZE_H1       = 16
SIZE_H2       = 14
SIZE_H3       = 12
SIZE_CAPTION  = 10
SIZE_CODE     = 9

BG_THEOREM = "EBF2FA"   # light blue for theorem/lemma/definition
BG_CODE    = "F5F5F5"   # light grey for verbatim code
BG_WARN    = "FFF3CD"   # light yellow for table fallback

CLAUDE_MODEL = "claude-sonnet-4-6"

MATH_ENV_LABELS = {
    "definition":  "定义",
    "lemma":       "引理",
    "theorem":     "定理",
    "corollary":   "推论",
    "proposition": "命题",
    "remark":      "注记",
    "claim":       "断言",
    "example":     "例",
    "fact":        "事实",
}

NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS_M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
NS   = {"w": NS_W, "m": NS_M}

# ─────────────────────────────────────────────────────────────────────────────
# Font helper — set ALL four XML font slots so CJK characters render correctly
# ─────────────────────────────────────────────────────────────────────────────

def set_run_font(run, font_name: str, size_pt: float = None,
                 bold: bool = False, italic: bool = False,
                 color: RGBColor = None):
    """
    Apply font_name to a python-docx Run across all four w:rFonts slots
    (ascii, eastAsia, hAnsi, cs) so that both Latin and CJK glyphs use
    the specified typeface.
    """
    rPr = run._r.get_or_add_rPr()

    old = rPr.find(qn("w:rFonts"))
    if old is not None:
        rPr.remove(old)

    rFonts = OxmlElement("w:rFonts")
    rFonts.set(qn("w:ascii"),    font_name)
    rFonts.set(qn("w:eastAsia"), font_name)
    rFonts.set(qn("w:hAnsi"),    font_name)
    rFonts.set(qn("w:cs"),       font_name)
    rPr.insert(0, rFonts)

    if size_pt:
        run.font.size = Pt(size_pt)
    if bold:
        run.bold = True
    if italic:
        run.italic = True
    if color:
        run.font.color.rgb = color


def _set_para_font(para, font_name: str, size_pt: float):
    """
    Set the paragraph-level default font via <w:pPr><w:rPr> so that runs
    which do not carry explicit rPr still inherit the correct CJK face.
    """
    pPr = para._p.get_or_add_pPr()
    rPr = pPr.find(qn("w:rPr"))
    if rPr is None:
        rPr = OxmlElement("w:rPr")
        pPr.append(rPr)

    old = rPr.find(qn("w:rFonts"))
    if old is not None:
        rPr.remove(old)

    rFonts = OxmlElement("w:rFonts")
    rFonts.set(qn("w:ascii"),    font_name)
    rFonts.set(qn("w:eastAsia"), font_name)
    rFonts.set(qn("w:hAnsi"),    font_name)
    rFonts.set(qn("w:cs"),       font_name)
    rPr.insert(0, rFonts)

    val = str(int(size_pt * 2))
    for tag in ("w:sz", "w:szCs"):
        e = rPr.find(qn(tag))
        if e is None:
            e = OxmlElement(tag)
            rPr.append(e)
        e.set(qn("w:val"), val)


def _fix_run_fonts(r_elem, font_name: str, size_pt: float):
    """
    Overwrite w:rFonts on a <w:r> lxml element (from pandoc output) to
    use font_name, ensuring eastAsia is set for CJK characters.
    """
    rPr = r_elem.find(qn("w:rPr"))
    if rPr is None:
        rPr = OxmlElement("w:rPr")
        r_elem.insert(0, rPr)

    old = rPr.find(qn("w:rFonts"))
    if old is not None:
        rPr.remove(old)

    rFonts = OxmlElement("w:rFonts")
    rFonts.set(qn("w:ascii"),    font_name)
    rFonts.set(qn("w:eastAsia"), font_name)
    rFonts.set(qn("w:hAnsi"),    font_name)
    rFonts.set(qn("w:cs"),       font_name)
    rPr.insert(0, rFonts)

    val = str(int(size_pt * 2))
    for tag in ("w:sz", "w:szCs"):
        e = rPr.find(qn(tag))
        if e is None:
            e = OxmlElement(tag)
            rPr.append(e)
        e.set(qn("w:val"), val)


# ─────────────────────────────────────────────────────────────────────────────
# Paragraph formatting helpers
# ─────────────────────────────────────────────────────────────────────────────

def _spacing(para, before_pt=0.0, after_pt=6.0):
    pPr = para._p.get_or_add_pPr()
    sp  = OxmlElement("w:spacing")
    sp.set(qn("w:before"), str(int(before_pt * 20)))
    sp.set(qn("w:after"),  str(int(after_pt  * 20)))
    pPr.append(sp)


def _indent(para, left_cm=0.0, first_line_cm=0.0):
    pPr = para._p.get_or_add_pPr()
    ind = OxmlElement("w:ind")
    if left_cm:
        ind.set(qn("w:left"),      str(int(left_cm       * 567)))
    if first_line_cm:
        ind.set(qn("w:firstLine"), str(int(first_line_cm * 567)))
    pPr.append(ind)


def _shade(para, fill_hex: str):
    pPr = para._p.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"),   "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"),  fill_hex)
    pPr.append(shd)


# ─────────────────────────────────────────────────────────────────────────────
# pandoc: LaTeX → OMML (native Word math)
# ─────────────────────────────────────────────────────────────────────────────

_TEX_PREAMBLE = (
    "\\documentclass{article}\n"
    "\\usepackage{amsmath,amssymb,amsfonts,mathtools,bm}\n"
    "\\begin{document}\n"
)
_TEX_POSTAMBLE = "\n\\end{document}\n"


def _pandoc_latex_to_xml(latex_body: str):
    """
    Write a minimal .tex, run pandoc, return the bytes of word/document.xml.
    Returns None on any failure.
    """
    src = _TEX_PREAMBLE + latex_body + _TEX_POSTAMBLE
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".tex", mode="w", encoding="utf-8", delete=False
        ) as f:
            f.write(src)
            tex_path = f.name

        docx_path = tex_path.replace(".tex", ".docx")
        result = subprocess.run(
            ["pandoc", tex_path, "-f", "latex", "-t", "docx",
             "-o", docx_path, "--quiet"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            print(f"  [pandoc stderr] {result.stderr[:300]}")
            return None

        with zipfile.ZipFile(docx_path) as z:
            return z.read("word/document.xml")

    except Exception as exc:
        print(f"  [pandoc exception] {exc}")
        return None
    finally:
        for p in (tex_path, docx_path):
            try:
                os.unlink(p)
            except Exception:
                pass


def _inject_pandoc_paras(latex_body: str, doc,
                          font_name: str, size_pt: float,
                          align: WD_ALIGN_PARAGRAPH,
                          indent_first_cm: float,
                          indent_left_cm:  float,
                          before_pt: float,
                          after_pt:  float) -> bool:
    """
    Convert latex_body via pandoc, then inject every non-empty <w:p> it
    produces into `doc`, applying the requested paragraph formatting and
    overwriting all run fonts to font_name/size_pt.

    Returns True if at least one paragraph was injected.
    """
    xml_bytes = _pandoc_latex_to_xml(latex_body)
    if xml_bytes is None:
        return False

    tree  = etree.fromstring(xml_bytes)
    paras = tree.findall(f".//{{{NS_W}}}p")

    injected = 0
    for src_p in paras:
        # Skip paragraphs that contain only a pPr (empty)
        content_children = [
            c for c in src_p
            if etree.QName(c.tag).localname != "pPr"
        ]
        if not content_children:
            continue

        dst_para = doc.add_paragraph()
        dst_para.alignment = align
        _indent(dst_para, left_cm=indent_left_cm, first_line_cm=indent_first_cm)
        _spacing(dst_para, before_pt=before_pt, after_pt=after_pt)
        _set_para_font(dst_para, font_name, size_pt)

        for child in src_p:
            if etree.QName(child.tag).localname == "pPr":
                continue            # we manage paragraph properties ourselves

            node = copy.deepcopy(child)
            local = etree.QName(node.tag).localname

            # Fix fonts on plain text runs
            if local == "r":
                _fix_run_fonts(node, font_name, size_pt)

            # Fix fonts on text runs buried inside math (m:oMath / m:oMathPara)
            _fix_math_text_runs(node, font_name)

            dst_para._p.append(node)

        injected += 1

    return injected > 0


def _fix_math_text_runs(elem, font_name: str):
    """
    Walk an lxml element tree and set the font on any <m:r> run whose
    <m:rPr> contains <m:nor> (normal/text mode inside math).
    This makes \\text{} content inside formulas use the document body font.
    """
    m_r_tag  = f"{{{NS_M}}}r"
    m_rPr    = f"{{{NS_M}}}rPr"
    m_nor    = f"{{{NS_M}}}nor"

    for m_run in elem.iter(m_r_tag):
        rPr = m_run.find(m_rPr)
        if rPr is not None and rPr.find(m_nor) is not None:
            # This is a \text{} run — apply the body font
            w_rPr = m_run.find(qn("w:rPr"))
            if w_rPr is None:
                w_rPr = OxmlElement("w:rPr")
                m_run.insert(0, w_rPr)
            old = w_rPr.find(qn("w:rFonts"))
            if old is not None:
                w_rPr.remove(old)
            rFonts = OxmlElement("w:rFonts")
            rFonts.set(qn("w:ascii"),    font_name)
            rFonts.set(qn("w:eastAsia"), font_name)
            rFonts.set(qn("w:hAnsi"),    font_name)
            rFonts.set(qn("w:cs"),       font_name)
            w_rPr.insert(0, rFonts)


# ─────────────────────────────────────────────────────────────────────────────
# LaTeX pre-processing helpers
# ─────────────────────────────────────────────────────────────────────────────

def _clean_cross_refs(text: str) -> str:
    """
    Clean up pipeline artefacts such as doubled section labels
    e.g. '第3.5节 节' → '第3.5节'.
    """
    text = re.sub(r'(第[0-9.]+[节章])\s*节', r'\1', text)
    return text


def _normalise_envs(text: str) -> str:
    """
    Replace non-standard environments with pandoc-compatible equivalents.
    e.g. wraptable → table, wrapfigure → figure.
    """
    text = re.sub(
        r'\\begin\{wraptable\}(\{[^}]*\})?\{[^}]*\}',
        r'\\begin{table}', text
    )
    text = re.sub(r'\\end\{wraptable\}',  r'\\end{table}',  text)
    text = re.sub(
        r'\\begin\{wrapfigure\}(\{[^}]*\})?\{[^}]*\}',
        r'\\begin{figure}', text
    )
    text = re.sub(r'\\end\{wrapfigure\}', r'\\end{figure}', text)
    return text


def _extract_caption(text: str) -> str:
    """Return the content of the first \\caption{...} in text."""
    m = re.search(r'\\caption\{((?:[^{}]|\{[^{}]*\})*)\}', text, re.DOTALL)
    return m.group(1).strip() if m else ""


def _extract_env_body(text: str, env_name: str):
    """
    Extract (optional_arg, body) from \\begin{env_name}[opt]...\\end{env_name}.
    Returns ("", text) if the environment is not found.
    """
    pat = re.compile(
        r'\\begin\{' + re.escape(env_name) + r'\*?\}'
        r'(?:\[([^\]]*)\])?'
        r'(.*?)'
        r'\\end\{' + re.escape(env_name) + r'\*?\}',
        re.DOTALL | re.IGNORECASE,
    )
    m = pat.search(text)
    if not m:
        return "", text
    return (m.group(1) or "").strip(), m.group(2).strip()


# ─────────────────────────────────────────────────────────────────────────────
# Caption paragraph
# ─────────────────────────────────────────────────────────────────────────────

def _add_caption(doc, label: str, caption_latex: str):
    """
    Render a figure/table caption in 楷体, centred.
    caption_latex may contain inline math.
    """
    if not label and not caption_latex:
        return

    if caption_latex:
        body = f"\\textbf{{{label}}}\\quad {caption_latex}"
        ok = _inject_pandoc_paras(
            body, doc,
            font_name=FONT_CAPTION, size_pt=SIZE_CAPTION,
            align=WD_ALIGN_PARAGRAPH.CENTER,
            indent_first_cm=0.0, indent_left_cm=0.0,
            before_pt=2.0, after_pt=8.0,
        )
        if ok:
            return

    # Fallback
    p   = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _spacing(p, before_pt=2, after_pt=8)
    run = p.add_run(f"{label}  {caption_latex}")
    set_run_font(run, FONT_CAPTION, SIZE_CAPTION, italic=True)


# ─────────────────────────────────────────────────────────────────────────────
# Chunk classifier
# ─────────────────────────────────────────────────────────────────────────────

_RE_SECTION       = re.compile(r'\\section\*?\{')
_RE_SUBSECTION    = re.compile(r'\\subsection\*?\{')
_RE_SUBSUBSECTION = re.compile(r'\\subsubsection\*?\{')
_RE_ABSTRACT      = re.compile(r'\\begin\{abstract\}')
_RE_FIGURE        = re.compile(r'\\begin\{(figure\*?|wrapfigure)\}')
_RE_TABLE         = re.compile(r'\\begin\{(table\*?|wraptable)\}')
_RE_VERBATIM      = re.compile(r'\\begin\{(verbatim|lstlisting|minted|Verbatim)\}')
_RE_MATHENV       = re.compile(
    r'\\begin\{(definition|lemma|theorem|corollary|proposition'
    r'|remark|claim|example|fact)\*?\}', re.IGNORECASE
)
_RE_PROOF    = re.compile(r'\\begin\{proof\}', re.IGNORECASE)
_RE_EQUATION = re.compile(
    r'\\begin\{(equation|align|multline|gather|eqnarray|flalign|alignat|split)\*?\}'
)


def classify(para: dict) -> str:
    """
    Classify a JSON paragraph dict.

    Returns one of: abstract | section | subsection | subsubsection |
                    equation | figure | table | code | mathenv | proof | paragraph
    """
    label = para.get("env_label", "")
    text  = para.get("text", "").strip()

    # Label-based fast paths
    if label == "摘要":              return "abstract"
    if re.match(r"^式",  label):     return "equation"
    if re.match(r"^图",  label):     return "figure"
    if re.match(r"^表",  label):     return "table"
    if re.match(r"^代码", label):    return "code"
    if re.match(r"^算法", label):    return "code"

    # Content-based
    if _RE_ABSTRACT.match(text):      return "abstract"
    if _RE_SECTION.match(text):       return "section"
    if _RE_SUBSECTION.match(text):    return "subsection"
    if _RE_SUBSUBSECTION.match(text): return "subsubsection"
    if _RE_EQUATION.match(text):      return "equation"
    if _RE_FIGURE.match(text):        return "figure"
    if _RE_TABLE.match(text):         return "table"
    if _RE_VERBATIM.match(text):      return "code"
    if _RE_MATHENV.match(text):       return "mathenv"
    if _RE_PROOF.match(text):         return "proof"

    return "paragraph"


# ─────────────────────────────────────────────────────────────────────────────
# Renderers
# ─────────────────────────────────────────────────────────────────────────────

# ── Chapter title ──────────────────────────────────────────────────────────

def render_chapter_title(doc, meta: dict):
    chapter  = meta.get("chapter", "")
    title_zh = meta.get("title_translation", meta.get("title", ""))
    text     = f"第{chapter}章  {title_zh}"

    h = doc.add_heading(text, level=0)
    h.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for run in h.runs:
        set_run_font(run, FONT_HEADING, SIZE_H1 + 2, bold=True)
    _spacing(h, before_pt=24, after_pt=12)


# ── Section headings ────────────────────────────────────────────────────────

def _extract_heading(translation: str, cmd: str):
    """
    Parse \\cmd{heading text} and return (heading_str, trailing_body_str).
    Handles nested braces.
    """
    pat = re.compile(r'\\' + re.escape(cmd) + r'\*?\{')
    m   = pat.match(translation.strip())
    if not m:
        return "", translation

    start = m.end() - 1      # index of opening '{'
    depth, i = 0, start
    while i < len(translation):
        ch = translation[i]
        if   ch == '{': depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                heading = translation[start + 1 : i].strip()
                body    = translation[i + 1 :].strip()
                return heading, body
        i += 1

    return translation[start + 1:].strip(), ""


def render_heading(doc, para: dict, level: int):
    translation = para.get("translation", para.get("text", "")).strip()
    env_label   = para.get("env_label", "")

    cmd_map = {1: "section", 2: "subsection", 3: "subsubsection"}
    heading_text, body_text = _extract_heading(translation, cmd_map[level])

    if not heading_text:
        m = re.search(r'\{([^}]+)\}', translation)
        heading_text = m.group(1) if m else translation
        body_text    = ""

    # Strip any remaining LaTeX markup from the heading string
    heading_clean = re.sub(r'\\[a-zA-Z]+\*?\{([^{}]*)\}', r'\1', heading_text)
    heading_clean = re.sub(r'\\[a-zA-Z]+\*?', '',           heading_clean).strip()

    # Numeric prefix from env_label (e.g. "3.5.1" from "3.5.1小节")
    numeric = re.sub(r'[节小].*$', '', env_label)
    prefix  = (numeric + "  ") if re.match(r'^\d', numeric) else ""

    size_map = {1: SIZE_H1, 2: SIZE_H2, 3: SIZE_H3}
    h = doc.add_heading(prefix + heading_clean, level=level)
    for run in h.runs:
        set_run_font(run, FONT_HEADING, size_map[level], bold=True)
    _spacing(h, before_pt=14, after_pt=5)

    # Render any body text that follows the heading command on the same chunk
    if body_text.strip():
        _render_para(doc, body_text)


# ── Abstract ────────────────────────────────────────────────────────────────

def render_abstract(doc, para: dict):
    translation = para.get("translation", para.get("text", ""))
    _opt, body  = _extract_env_body(translation, "abstract")
    if not body:
        body = translation

    h = doc.add_heading("摘要", level=1)
    for run in h.runs:
        set_run_font(run, FONT_HEADING, SIZE_H1, bold=True)
    _spacing(h, before_pt=14, after_pt=6)

    _render_para(doc, body, indent_first_cm=0.74, indent_left_cm=1.0)


# ── Body paragraph (the core text renderer) ─────────────────────────────────

def _render_para(doc, latex_text: str,
                 font_name: str = FONT_BODY, size_pt: float = SIZE_BODY,
                 align: WD_ALIGN_PARAGRAPH = WD_ALIGN_PARAGRAPH.JUSTIFY,
                 indent_first_cm: float = 0.74,
                 indent_left_cm:  float = 0.0,
                 before_pt: float = 0.0,
                 after_pt:  float = 6.0,
                 shade_hex: str   = None) -> bool:
    """
    The central render function for any piece of LaTeX/Chinese text.
    Routes through pandoc so that inline math becomes native OMML.
    Returns True if pandoc succeeded, False if plain-text fallback was used.
    """
    text = _clean_cross_refs(latex_text).strip()
    if not text:
        return True

    ok = _inject_pandoc_paras(
        text, doc,
        font_name=font_name, size_pt=size_pt,
        align=align,
        indent_first_cm=indent_first_cm,
        indent_left_cm=indent_left_cm,
        before_pt=before_pt,
        after_pt=after_pt,
    )

    if shade_hex and ok:
        # Apply shading retroactively to the last inserted paragraph
        # (walk backwards in doc.paragraphs to find it)
        for p in reversed(doc.paragraphs):
            if p._p.getparent() is not None:
                _shade(p, shade_hex)
                break

    if not ok:
        # Rough plain-text fallback
        plain = re.sub(r'\\[a-zA-Z]+\*?\{([^{}]*)\}', r'\1', text)
        plain = re.sub(r'\\[a-zA-Z]+\*?', '', plain)
        plain = plain.replace('$', '').replace('{', '').replace('}', '').strip()
        p = doc.add_paragraph()
        p.alignment = align
        _indent(p, left_cm=indent_left_cm, first_line_cm=indent_first_cm)
        _spacing(p, before_pt=before_pt, after_pt=after_pt)
        if shade_hex:
            _shade(p, shade_hex)
        run = p.add_run(plain)
        set_run_font(run, font_name, size_pt)

    return ok


def render_paragraph(doc, para: dict):
    translation = para.get("translation", para.get("text", ""))
    _render_para(doc, translation)


# ── Display equations ────────────────────────────────────────────────────────

def render_equation(doc, para: dict):
    """
    Convert a display-math environment to native Word OMML via pandoc.
    Appends a right-aligned equation label.
    """
    translation = para.get("translation", para.get("text", "")).strip()
    env_label   = para.get("env_label", "")

    tex = _clean_cross_refs(translation)

    ok = _inject_pandoc_paras(
        tex, doc,
        font_name=FONT_BODY, size_pt=SIZE_BODY,
        align=WD_ALIGN_PARAGRAPH.CENTER,
        indent_first_cm=0.0, indent_left_cm=0.0,
        before_pt=6.0, after_pt=2.0,
    )

    if not ok:
        # Fallback: raw LaTeX in monospace
        p   = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _spacing(p, before_pt=6, after_pt=2)
        run = p.add_run(translation)
        run.font.name = FONT_CODE
        run.font.size = Pt(SIZE_CODE)

    if env_label:
        lp  = doc.add_paragraph()
        lp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        _spacing(lp, before_pt=0, after_pt=8)
        lr  = lp.add_run(f"（{env_label}）")
        set_run_font(lr, FONT_BODY, 10)


# ── Figures ──────────────────────────────────────────────────────────────────

def render_figure(doc, para: dict, figures_dir: str = "."):
    translation   = para.get("translation", para.get("text", ""))
    env_label     = para.get("env_label", "")
    img_paths     = re.findall(
        r'\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}', translation
    )
    caption_latex = _extract_caption(translation)

    doc.add_paragraph()   # blank spacer

    embedded = False
    for rel in img_paths:
        full       = Path(figures_dir) / rel
        candidates = [full] + [
            full.with_suffix(ext) for ext in (".png", ".jpg", ".jpeg", ".pdf")
        ]
        found = next((c for c in candidates if c.exists()), None)
        if found:
            try:
                ip = doc.add_paragraph()
                ip.alignment = WD_ALIGN_PARAGRAPH.CENTER
                ip.add_run().add_picture(str(found), width=Inches(5.0))
                embedded = True
            except Exception as exc:
                print(f"  [warn] embed {found}: {exc}")

    if not embedded and img_paths:
        ph  = doc.add_paragraph()
        ph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _shade(ph, "F2F2F2")
        run = ph.add_run(f"[图片: {', '.join(img_paths)}]")
        set_run_font(run, FONT_BODY, 10, color=RGBColor(0x88, 0x88, 0x88))

    _add_caption(doc, env_label, caption_latex)
    doc.add_paragraph()   # blank spacer


# ── Code / verbatim blocks ───────────────────────────────────────────────────

def render_code(doc, para: dict):
    """
    Render verbatim/lstlisting as a grey monospace block.
    Uses the original (untranslated) text so code is never translated.
    """
    text      = para.get("text", "")
    env_label = para.get("env_label", "")

    body = text.strip()
    for env in ("verbatim", "lstlisting", "minted", "Verbatim"):
        _opt, inner = _extract_env_body(body, env)
        if inner:
            body = inner
            break
    else:
        body = re.sub(r'^\\begin\{[^}]+\}\*?', '', body)
        body = re.sub(r'\\end\{[^}]+\}\*?$',   '', body).strip()

    if env_label:
        lp = doc.add_paragraph()
        _spacing(lp, before_pt=8, after_pt=2)
        lr = lp.add_run(env_label)
        set_run_font(lr, FONT_BODY, SIZE_CODE, bold=True)

    for line in body.split("\n"):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after  = Pt(0)
        _indent(p, left_cm=0.5)
        _shade(p, BG_CODE)
        run = p.add_run(line)
        run.font.name = FONT_CODE
        run.font.size = Pt(SIZE_CODE)

    doc.add_paragraph()


# ── Math environments (theorem / lemma / definition / …) ────────────────────

def render_mathenv(doc, para: dict):
    translation = para.get("translation", para.get("text", "")).strip()
    m           = _RE_MATHENV.match(translation)
    if not m:
        render_paragraph(doc, para)
        return

    env_name = m.group(1).lower().rstrip("*")
    label_zh = MATH_ENV_LABELS.get(env_name, env_name)
    opt, body = _extract_env_body(translation, env_name)
    if not body:
        render_paragraph(doc, para)
        return

    display_label = f"【{label_zh}】" + (f"（{opt}）" if opt else "")

    # Label header in shaded paragraph
    hp = doc.add_paragraph()
    _shade(hp, BG_THEOREM)
    _indent(hp, left_cm=0.8)
    _spacing(hp, before_pt=8, after_pt=0)
    hr = hp.add_run(display_label)
    set_run_font(hr, FONT_BODY, SIZE_BODY, bold=True)

    # Body via pandoc (italic, same shade)
    ok = _inject_pandoc_paras(
        body, doc,
        font_name=FONT_BODY, size_pt=SIZE_BODY,
        align=WD_ALIGN_PARAGRAPH.JUSTIFY,
        indent_first_cm=0.0, indent_left_cm=0.8,
        before_pt=2.0, after_pt=8.0,
    )
    if not ok:
        fp = doc.add_paragraph()
        _shade(fp, BG_THEOREM)
        _indent(fp, left_cm=0.8)
        _spacing(fp, before_pt=2, after_pt=8)
        fr = fp.add_run(body)
        set_run_font(fr, FONT_BODY, SIZE_BODY, italic=True)


def render_proof(doc, para: dict):
    translation = para.get("translation", para.get("text", "")).strip()
    opt, body   = _extract_env_body(translation, "proof")
    label_zh    = "证明" + (f"（{opt}）" if opt else "")

    lp = doc.add_paragraph()
    _indent(lp, left_cm=0.8)
    _spacing(lp, before_pt=4, after_pt=0)
    lr = lp.add_run(label_zh + "  ")
    set_run_font(lr, FONT_BODY, SIZE_BODY, bold=True)

    ok = _inject_pandoc_paras(
        body or "", doc,
        font_name=FONT_BODY, size_pt=SIZE_BODY,
        align=WD_ALIGN_PARAGRAPH.JUSTIFY,
        indent_first_cm=0.0, indent_left_cm=0.8,
        before_pt=2.0, after_pt=4.0,
    )
    if not ok and body:
        fp = doc.add_paragraph()
        _indent(fp, left_cm=0.8)
        _spacing(fp, before_pt=2, after_pt=4)
        fr = fp.add_run(body)
        set_run_font(fr, FONT_BODY, SIZE_BODY, italic=True)

    # QED symbol
    qp = doc.add_paragraph()
    qp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    _spacing(qp, before_pt=0, after_pt=4)
    qr = qp.add_run("□")
    set_run_font(qr, FONT_BODY, SIZE_BODY)


# ── Tables via Claude API ────────────────────────────────────────────────────

_TABLE_SYSTEM_PROMPT = """\
You are a Python code generator for python-docx.

Generate code that adds a Word table to an existing `doc` object.

Available names already in scope (do NOT import or redefine them):
  doc, Pt, Inches, Cm, RGBColor, WD_ALIGN_PARAGRAPH, qn, OxmlElement

Rules:
1. Use ONLY the names listed above. Do not write any import statements.
2. Create the table with doc.add_table(rows=N, cols=M).
3. Populate every cell with the correct Chinese text from the LaTeX source.
4. Apply styling: bold the header row, set font size to 10pt, add simple borders.
5. To set borders on a cell, use this exact helper pattern:
       def set_cell_border(cell, **kwargs):
           tc = cell._tc
           tcPr = tc.get_or_add_tcPr()
           for edge, attrs in kwargs.items():
               tag = qn('w:' + edge)
               el = OxmlElement(tag)
               for k, v in attrs.items():
                   el.set(qn('w:' + k), v)
               tcPr.append(el)
6. Handle \\multicolumn by merging cells:
       table.cell(row, col_start).merge(table.cell(row, col_end))
7. After the table, add a centred caption paragraph in 楷体 10pt if a caption exists.
8. Set the font on every run you create:
       run = cell.paragraphs[0].add_run("text")
       run.font.name = "宋体"
       from docx.oxml import OxmlElement; from docx.oxml.ns import qn
       rPr = run._r.get_or_add_rPr()
       rFonts = OxmlElement("w:rFonts")
       for attr in ("w:ascii","w:eastAsia","w:hAnsi","w:cs"):
           rFonts.set(qn(attr), "宋体")
       rPr.insert(0, rFonts)
9. Output ONLY valid Python code — no markdown fences, no explanations."""


def _call_claude_for_table(translation: str, env_label: str,
                            client: anthropic.Anthropic) -> str:
    """
    Ask Claude to generate python-docx code for this LaTeX table.
    Returns the Python code string, or "" on failure.
    """
    prompt = (
        f"Convert this LaTeX table (label: {env_label}) to python-docx code "
        f"that writes it into `doc`.\n\n"
        f"```latex\n{translation}\n```\n\n"
        f"Output only the Python code."
    )
    try:
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2048,
            system=_TABLE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        # Strip markdown code fences if present
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```$",           "", raw)
        return raw.strip()
    except Exception as exc:
        print(f"  [warn] Claude API error (table {env_label}): {exc}")
        return ""


def render_table(doc, para: dict, client):
    """
    Render a table chunk:
      1. Call Claude API to get python-docx code.
      2. exec() that code with a rich namespace.
      3. Fall back to raw-LaTeX display on any failure.
    """
    translation   = para.get("translation", para.get("text", ""))
    env_label     = para.get("env_label", "")
    caption_latex = _extract_caption(translation)

    if client is None:
        _table_fallback(doc, env_label, caption_latex, translation)
        return

    print(f"  → Calling Claude API for table {env_label} …")
    code = _call_claude_for_table(translation, env_label, client)

    if not code:
        _table_fallback(doc, env_label, caption_latex, translation)
        return

    # Build exec namespace with everything the generated code might need
    exec_ns = {
        # Core objects
        "doc":                doc,
        "Pt":                 Pt,
        "Inches":             Inches,
        "Cm":                 Cm,
        "RGBColor":           RGBColor,
        "WD_ALIGN_PARAGRAPH": WD_ALIGN_PARAGRAPH,
        "qn":                 qn,
        "OxmlElement":        OxmlElement,
        # Convenience — LLM may reference these
        "FONT_BODY":    FONT_BODY,
        "FONT_CAPTION": FONT_CAPTION,
        "FONT_HEADING": FONT_HEADING,
        "SIZE_BODY":    SIZE_BODY,
        "SIZE_CAPTION": SIZE_CAPTION,
        "set_run_font": set_run_font,
    }

    try:
        exec(code, exec_ns)   # noqa: S102
        print(f"  ✓ Table {env_label} rendered via Claude API.")
    except Exception as exc:
        print(f"  [warn] exec failed for table {env_label}: {exc}")
        traceback.print_exc()
        _table_fallback(doc, env_label, caption_latex, translation)


def _table_fallback(doc, env_label: str, caption_latex: str, raw_latex: str):
    """Show the raw LaTeX source when table generation fails."""
    wp  = doc.add_paragraph()
    _shade(wp, BG_WARN)
    _spacing(wp, before_pt=6, after_pt=2)
    wr  = wp.add_run(f"[表格 {env_label} — 原始LaTeX]")
    set_run_font(wr, FONT_BODY, 10, color=RGBColor(0x99, 0x44, 0x00))

    snippet = raw_latex[:800] + ("…" if len(raw_latex) > 800 else "")
    for line in snippet.split("\n"):
        lp = doc.add_paragraph()
        _indent(lp, left_cm=0.5)
        _shade(lp, BG_WARN)
        _spacing(lp, before_pt=0, after_pt=0)
        lr = lp.add_run(line)
        lr.font.name = FONT_CODE
        lr.font.size = Pt(8)

    if caption_latex:
        _add_caption(doc, env_label, caption_latex)


# ─────────────────────────────────────────────────────────────────────────────
# Main dispatcher
# ─────────────────────────────────────────────────────────────────────────────

def render_chunk(doc, para: dict, figures_dir: str, client):
    """Classify and dispatch a single JSON paragraph to its renderer."""
    if para.get("syntax_error", False):
        print(f"  [skip] id={para.get('id')} (syntax_error)")
        return

    chunk_type = classify(para)
    pid        = para.get("id",        "?")
    label      = para.get("env_label", "")

    print(f"  [{pid:>4}] {label:<22} → {chunk_type}")

    dispatch = {
        "abstract":      lambda: render_abstract(doc, para),
        "section":       lambda: render_heading(doc, para, 1),
        "subsection":    lambda: render_heading(doc, para, 2),
        "subsubsection": lambda: render_heading(doc, para, 3),
        "equation":      lambda: render_equation(doc, para),
        "figure":        lambda: render_figure(doc, para, figures_dir),
        "table":         lambda: render_table(doc, para, client),
        "code":          lambda: render_code(doc, para),
        "mathenv":       lambda: render_mathenv(doc, para),
        "proof":         lambda: render_proof(doc, para),
        "paragraph":     lambda: render_paragraph(doc, para),
    }
    dispatch.get(chunk_type, dispatch["paragraph"])()


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Render replaced.json translation into a Word document."
    )
    ap.add_argument("--json",        default="replaced.json",
                    help="replaced.json path (default: replaced.json)")
    ap.add_argument("--docx",        default="output.docx",
                    help="Output .docx path (created or appended to)")
    ap.add_argument("--figures-dir", default=".",
                    help="Base dir for \\includegraphics resolution")
    ap.add_argument("--no-tables",   action="store_true",
                    help="Skip Claude API table generation (show raw LaTeX)")
    ap.add_argument("--skip-title",  action="store_true",
                    help="Do not prepend chapter title")
    args = ap.parse_args()

    # Load JSON
    json_path = Path(args.json)
    if not json_path.exists():
        sys.exit(f"Error: JSON not found: {json_path}")
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    documents = data.get("documents", [])
    if not documents:
        sys.exit("Error: no documents found in JSON.")

    # Claude client (for table generation)
    client = None
    if not args.no_tables:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if api_key:
            client = anthropic.Anthropic(api_key=api_key)
        else:
            print(
                "Warning: ANTHROPIC_API_KEY not set — table LLM generation disabled.\n"
                "         Pass --no-tables to suppress this warning."
            )

    # Open or create docx
    docx_path = Path(args.docx)
    if docx_path.exists():
        print(f"Opening: {docx_path}")
        doc = Document(str(docx_path))
    else:
        print(f"Creating: {docx_path}")
        doc = Document()
        for sec in doc.sections:
            sec.top_margin    = Cm(2.5)
            sec.bottom_margin = Cm(2.5)
            sec.left_margin   = Cm(3.0)
            sec.right_margin  = Cm(2.5)

    # Render
    for doc_meta in documents:
        paragraphs  = doc_meta.get("paragraphs", [])
        tex_file    = doc_meta.get("tex", "")
        figures_dir = str(Path(tex_file).parent) if tex_file else args.figures_dir

        print(f"\nDocument : {tex_file or '(unknown)'}")
        print(f"Title    : {doc_meta.get('title_translation', '')}")
        print(f"Chapter  : {doc_meta.get('chapter', '')}")
        print(f"Chunks   : {len(paragraphs)}")
        print(f"FigDir   : {figures_dir}\n")

        if not args.skip_title:
            render_chapter_title(doc, doc_meta)

        for para in paragraphs:
            render_chunk(doc, para, figures_dir, client)

    doc.save(str(docx_path))
    print(f"\n✓  Saved → {docx_path}")


if __name__ == "__main__":
    main()