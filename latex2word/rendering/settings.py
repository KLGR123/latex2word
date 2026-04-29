from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple


@dataclass
class FontSettings:
    body: str = "宋体"
    heading: str = "黑体"
    caption: str = "楷体"
    code: str = "Courier New"
    ascii: str = "Times New Roman"


@dataclass
class SizeSettings:
    body: int = 12
    h1: int = 16
    h2: int = 14
    h3: int = 12
    caption: int = 10
    code: int = 9


@dataclass
class ColorSettings:
    theorem_bg: str = "EBF2FA"
    code_bg: str = "F5F5F5"
    table_bg: str = "FFF3CD"


FONTS = FontSettings()
SIZES = SizeSettings()
COLORS = ColorSettings()

NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS_M = "http://schemas.openxmlformats.org/officeDocument/2006/math"

RE_PUNCT_TRAILING_SPACE = re.compile(
    r'([。，、；：！？…—\u201c\u201d\u2018\u2019「」『』【】《》（）·.,;:!?])\s+'
)
RE_PARAGRAPH_CMD = re.compile(r'\\(?:paragraph|subparagraph)\*?\{')
RE_DEDUP_SECTION_POST = re.compile(r'(\d[\d.\s]*(?:小节|节))\s*节')
RE_SECTION = re.compile(r'\\section\*?\{')
RE_SUBSECTION = re.compile(r'\\subsection\*?\{')
RE_SUBSUBSECTION = re.compile(r'\\subsubsection\*?\{')
RE_ABSTRACT = re.compile(r'\\begin\{abstract\}', re.IGNORECASE)

DEFAULT_RENDER_ENVS: Dict[str, Tuple[str, ...]] = {
    "figure": (
        "figure", "figure*", "subfigure", "subfigure*", "subfloat", "wrapfigure",
        "wrapfigure*", "SCfigure", "SCfigure*", "floatrow", "ffigbox", "capbeside",
        "sidewaysfigure", "sidewaysfigure*", "turnfigure", "captionedbox",
        "tikzpicture", "tikzfigure", "floatingfigure", "figwindow", "cutout",
        "overpic", "teaserfigure", "figurehere",
    ),
    "table": (
        "table", "table*", "tabular", "tabular*", "tabularx", "tabularx*",
        "tabulary", "tabularray", "tblr", "longtblr", "longtable", "longtable*",
        "supertabular", "supertabular*", "mpsupertabular", "xtabular",
        "sidewaystable", "sidewaystable*", "sidewaystabular", "turntable",
        "threeparttable", "threeparttablex", "tabu", "longtabu", "tabbing",
        "wraptable", "wraptable*", "ttabbox", "ctable", "array", "spreadtab",
        "tablehere", "adjustbox",
    ),
    "algorithm": (
        "algorithm", "algorithm*", "algorithmic", "algorithmicx", "algpseudocode",
        "algorithm2e", "algorithm2e*", "pseudocode", "lstpseudocode",
        "myalgorithm", "algo", "proc", "procedure", "function",
    ),
    "code": (
        "verbatim", "verbatim*", "lstlisting", "minted", "minted*", "Verbatim",
        "Verbatim*", "BVerbatim", "LVerbatim", "SaveVerbatim", "alltt",
        "verbatimtab", "listing", "tcolorbox", "tcblisting", "mdframed",
        "spverbatim", "codeblock", "codebox", "sourcecode", "pycode",
        "bashcode", "jsoncode", "xmlcode", "sqlcode", "exampleblock", "example",
    ),
    "equation": (
        "equation", "equation*", "displaymath", "align", "align*", "aligned",
        "alignat", "alignat*", "alignedat", "gather", "gather*", "gathered",
        "multline", "multline*", "flalign", "flalign*", "split", "cases",
        "cases*", "dcases", "dcases*", "rcases", "rcases*", "numcases",
        "subequations", "eqnarray", "eqnarray*", "empheq", "dmath", "dmath*",
        "dseries", "dseries*", "dgroup", "dgroup*", "darray", "darray*",
        "IEEEeqnarray", "IEEEeqnarray*", "math",
    ),
}
RENDER_ENVS: Dict[str, set[str]] = {
    category: set(envs) for category, envs in DEFAULT_RENDER_ENVS.items()
}


def _compile_begin_env_regex(envs: set[str]) -> re.Pattern:
    alternatives = "|".join(re.escape(env) for env in sorted(envs, key=len, reverse=True))
    return re.compile(rf'\\begin\{{(?:{alternatives})\}}')


RE_FIGURE = _compile_begin_env_regex(RENDER_ENVS["figure"])
RE_TABLE = _compile_begin_env_regex(RENDER_ENVS["table"])
RE_ALGORITHM = _compile_begin_env_regex(RENDER_ENVS["algorithm"])
RE_VERBATIM = _compile_begin_env_regex(RENDER_ENVS["code"])
RE_EQUATION = _compile_begin_env_regex(RENDER_ENVS["equation"])

RE_MATHENV = re.compile(
    r'\\begin\{'
    r'(definition|lemma|theorem|corollary|proposition|remark|claim|example|fact'
    r')\*?\}',
    re.IGNORECASE,
)

RE_PROOF = re.compile(r'\\begin\{proof\}', re.IGNORECASE)

MATH_ENV_LABELS = {
    "definition": "定义",
    "lemma": "引理",
    "theorem": "定理",
    "corollary": "推论",
    "proposition": "命题",
    "remark": "注记",
    "claim": "断言",
    "example": "例",
    "fact": "事实",
}


def _update_dataclass(instance: Any, values: Any) -> None:
    if not isinstance(values, dict):
        return
    for key, value in values.items():
        if not hasattr(instance, key):
            continue
        current = getattr(instance, key)
        if isinstance(current, int):
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
        elif isinstance(current, str):
            value = str(value)
        setattr(instance, key, value)


def configure_render_settings(config: Dict[str, Any] | None) -> None:
    """Apply rendering settings from the rules file without clearing defaults."""
    if not isinstance(config, dict):
        return

    global RE_FIGURE, RE_TABLE, RE_ALGORITHM, RE_VERBATIM, RE_EQUATION
    _update_dataclass(FONTS, config.get("fonts"))
    _update_dataclass(SIZES, config.get("sizes"))
    _update_dataclass(COLORS, config.get("colors"))

    labels = config.get("math_env_labels")
    if isinstance(labels, dict):
        for env_name, label in labels.items():
            env_name = str(env_name).strip().lower()
            label = str(label).strip()
            if env_name and label:
                MATH_ENV_LABELS[env_name] = label

    envs = config.get("envs")
    if isinstance(envs, dict):
        for category, raw_envs in envs.items():
            category = str(category).strip()
            if category not in RENDER_ENVS or not isinstance(raw_envs, (list, tuple, set)):
                continue
            configured = {str(env).strip() for env in raw_envs if str(env).strip()}
            RENDER_ENVS[category].update(configured)
        RE_FIGURE = _compile_begin_env_regex(RENDER_ENVS["figure"])
        RE_TABLE = _compile_begin_env_regex(RENDER_ENVS["table"])
        RE_ALGORITHM = _compile_begin_env_regex(RENDER_ENVS["algorithm"])
        RE_VERBATIM = _compile_begin_env_regex(RENDER_ENVS["code"])
        RE_EQUATION = _compile_begin_env_regex(RENDER_ENVS["equation"])

TEX_PREAMBLE = (
    "\\documentclass{article}\n"
    "\\usepackage{amsmath,amssymb,amsfonts,mathtools,bm}\n"
    "\\begin{document}\n"
)
TEX_POSTAMBLE = "\n\\end{document}\n"

MATH_FONT_UPGRADES: List[Tuple[str, str]] = [
    (r"\rm", r"\mathrm"),
    (r"\bf", r"\mathbf"),
    (r"\it", r"\mathit"),
    (r"\sf", r"\mathsf"),
    (r"\tt", r"\mathtt"),
    (r"\cal", r"\mathcal"),
]

RE_INLINE_MATH = re.compile(r'(?<!\\)\$((?:[^$\\]|\\.)+?)(?<!\\)\$')
