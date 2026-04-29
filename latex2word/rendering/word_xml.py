from __future__ import annotations

from typing import Any

from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt

from .settings import FONTS, NS_M, RE_DEDUP_SECTION_POST, RE_PUNCT_TRAILING_SPACE


MONOSPACE_FONTS = {"Courier New", "Courier", "Consolas", "Monaco"}


def clean_xml_text_nodes(paragraphs) -> None:
    for para in paragraphs:
        for run in para.runs:
            if run.text:
                run.text = RE_PUNCT_TRAILING_SPACE.sub(r"\1", run.text)
                run.text = RE_DEDUP_SECTION_POST.sub(r"\1", run.text)


def make_rfonts(cjk_font: str) -> Any:
    el = OxmlElement("w:rFonts")
    if cjk_font in MONOSPACE_FONTS:
        for attr in ("w:ascii", "w:eastAsia", "w:hAnsi", "w:cs"):
            el.set(qn(attr), cjk_font)
    else:
        el.set(qn("w:ascii"), FONTS.ascii)
        el.set(qn("w:hAnsi"), FONTS.ascii)
        el.set(qn("w:eastAsia"), cjk_font)
        el.set(qn("w:cs"), FONTS.ascii)
    return el


def set_run_font(run, font_name: str, size_pt: float | None = None,
                 bold: bool = False, italic: bool = False, color=None) -> None:
    rpr = run._r.get_or_add_rPr()
    old = rpr.find(qn("w:rFonts"))
    if old is not None:
        rpr.remove(old)
    rpr.insert(0, make_rfonts(font_name))
    if size_pt:
        run.font.size = Pt(size_pt)
    if bold:
        run.bold = True
    if italic:
        run.italic = True
    if color:
        run.font.color.rgb = color


def set_para_font(para, font_name: str, size_pt: float) -> None:
    ppr = para._p.get_or_add_pPr()
    rpr = ppr.find(qn("w:rPr"))
    if rpr is None:
        rpr = OxmlElement("w:rPr")
        ppr.append(rpr)
    old = rpr.find(qn("w:rFonts"))
    if old is not None:
        rpr.remove(old)
    rpr.insert(0, make_rfonts(font_name))
    half_pts = str(int(size_pt * 2))
    for tag in ("w:sz", "w:szCs"):
        elem = rpr.find(qn(tag))
        if elem is None:
            elem = OxmlElement(tag)
            rpr.append(elem)
        elem.set(qn("w:val"), half_pts)


def fix_lxml_run_fonts(r_elem, font_name: str, size_pt: float) -> None:
    rpr = r_elem.find(qn("w:rPr"))
    if rpr is None:
        rpr = OxmlElement("w:rPr")
        r_elem.insert(0, rpr)
    old = rpr.find(qn("w:rFonts"))
    if old is not None:
        rpr.remove(old)
    rpr.insert(0, make_rfonts(font_name))
    half_pts = str(int(size_pt * 2))
    for tag in ("w:sz", "w:szCs"):
        elem = rpr.find(qn(tag))
        if elem is None:
            elem = OxmlElement(tag)
            rpr.append(elem)
        elem.set(qn("w:val"), half_pts)


def fix_math_text_runs(elem, font_name: str) -> None:
    m_r = f"{{{NS_M}}}r"
    m_rpr = f"{{{NS_M}}}rPr"
    m_nor = f"{{{NS_M}}}nor"
    for run in elem.iter(m_r):
        rpr = run.find(m_rpr)
        if rpr is None or rpr.find(m_nor) is None:
            continue
        w_rpr = run.find(qn("w:rPr"))
        if w_rpr is None:
            w_rpr = OxmlElement("w:rPr")
            run.insert(0, w_rpr)
        old = w_rpr.find(qn("w:rFonts"))
        if old is not None:
            w_rpr.remove(old)
        w_rpr.insert(0, make_rfonts(font_name))


def spacing(para, before_pt: float = 0.0, after_pt: float = 6.0) -> None:
    sp = OxmlElement("w:spacing")
    sp.set(qn("w:before"), str(int(before_pt * 20)))
    sp.set(qn("w:after"), str(int(after_pt * 20)))
    para._p.get_or_add_pPr().append(sp)


def indent(para, left_cm: float = 0.0, first_line_cm: float = 0.0, hanging_cm: float = 0.0) -> None:
    ind = OxmlElement("w:ind")
    if left_cm:
        ind.set(qn("w:left"), str(int(left_cm * 567)))
    if first_line_cm:
        ind.set(qn("w:firstLine"), str(int(first_line_cm * 567)))
    if hanging_cm:
        ind.set(qn("w:hanging"), str(int(hanging_cm * 567)))
    para._p.get_or_add_pPr().append(ind)


def shade(para, fill_hex: str) -> None:
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill_hex)
    para._p.get_or_add_pPr().append(shd)
