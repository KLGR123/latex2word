from __future__ import annotations

from . import settings
from .latex_utils import has_wrapped_display_math


def classify_paragraph(para: dict) -> str:
    label = para.get("env_label", "")
    text = para.get("text", "").strip()

    if label == "摘要":
        return "abstract"
    if label.startswith("式"):
        return "equation"
    if has_wrapped_display_math(text):
        return "equation"
    if label.startswith("图"):
        return "figure"
    if label.startswith("表"):
        return "table"
    if label.startswith("代码"):
        return "code"
    if label.startswith("算法"):
        return "code"

    if settings.RE_ABSTRACT.match(text):
        return "abstract"
    if settings.RE_SECTION.match(text):
        return "section"
    if settings.RE_SUBSECTION.match(text):
        return "subsection"
    if settings.RE_SUBSUBSECTION.match(text):
        return "subsubsection"
    if settings.RE_EQUATION.match(text):
        return "equation"
    if has_wrapped_display_math(text):
        return "equation"
    if settings.RE_FIGURE.match(text):
        return "figure"
    if settings.RE_TABLE.match(text):
        return "table"
    if settings.RE_ALGORITHM.match(text):
        return "code"
    if settings.RE_VERBATIM.match(text):
        return "code"
    if settings.RE_MATHENV.match(text):
        return "mathenv"
    if "\\begin{proof}" in text.lower():
        return "proof"
    if settings.RE_PROOF.match(text):
        return "proof"
    if settings.RE_PARAGRAPH_CMD.match(text):
        return "inline_heading"
    return "paragraph"
