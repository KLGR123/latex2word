from __future__ import annotations

import re
from typing import Tuple

from .settings import MATH_FONT_UPGRADES, RE_INLINE_MATH


DISPLAY_MATH_ENV_RE = re.compile(
    r'\\begin\{'
    r'(?:equation\*?|displaymath|align\*?|aligned|alignat\*?|alignedat|gather\*?|'
    r'gathered|multline\*?|flalign\*?|split|cases\*?|dcases\*?|rcases\*?|'
    r'numcases|subequations|eqnarray\*?|empheq|dmath\*?|dseries\*?|dgroup\*?|'
    r'darray\*?|IEEEeqnarray\*?|math'
    r')\}.*?\\end\{'
    r'(?:equation\*?|displaymath|align\*?|aligned|alignat\*?|alignedat|gather\*?|'
    r'gathered|multline\*?|flalign\*?|split|cases\*?|dcases\*?|rcases\*?|'
    r'numcases|subequations|eqnarray\*?|empheq|dmath\*?|dseries\*?|dgroup\*?|'
    r'darray\*?|IEEEeqnarray\*?|math'
    r')\}',
    re.DOTALL | re.IGNORECASE,
)
FLOAT_WRAPPER_RE = re.compile(
    r'^\s*\\begin\{(?:figure\*?|table\*?)\}.*?\\end\{(?:figure\*?|table\*?)\}\s*$',
    re.DOTALL | re.IGNORECASE,
)
NON_MATH_FLOAT_CONTENT_RE = re.compile(
    r'\\includegraphics|\\begin\{(?:tabular\*?|tabularx|tabulary|tabularray|tblr|'
    r'longtable\*?|tikzpicture|picture|subfigure\*?|subfloat|minipage)\}',
    re.IGNORECASE,
)


def clean_spaces(text: str) -> str:
    cjk = r'[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]'
    ascii_chars = r'[!-~]'
    text = re.sub(rf'({cjk})\s+', r'\1', text)
    text = re.sub(rf'\s+({cjk})', r'\1', text)
    text = re.sub(
        r'([。，、；：！？…—\u201c\u201d\u2018\u2019「」『』【】《》（）·.,;:!?])\s+',
        r'\1',
        text,
    )
    text = re.sub(rf'({ascii_chars}) {{2,}}({ascii_chars})', r'\1 \2', text)
    return text


def clean_cross_refs(text: str) -> str:
    return re.sub(r'(第[0-9.]+[节章])\s*节', r'\1', text)


def drop_braced_args(text: str, count: int) -> str:
    pos = 0
    for _ in range(count):
        while pos < len(text) and text[pos] in " \t\n":
            pos += 1
        if pos < len(text) and text[pos] == "[":
            depth = 0
            while pos < len(text):
                if text[pos] == "[":
                    depth += 1
                elif text[pos] == "]":
                    depth -= 1
                    if depth == 0:
                        pos += 1
                        break
                pos += 1
            while pos < len(text) and text[pos] in " \t\n":
                pos += 1
        if pos < len(text) and text[pos] == "{":
            depth = 0
            while pos < len(text):
                if text[pos] == "{":
                    depth += 1
                elif text[pos] == "}":
                    depth -= 1
                    if depth == 0:
                        pos += 1
                        break
                pos += 1
    return text[pos:]


def clean_title_latex(text: str) -> str:
    text = re.sub(r'(?<!\\)%[^\n]*', '', text)

    def drop_raisebox(value: str) -> str:
        out = []
        idx = 0
        while idx < len(value):
            match = re.search(r'\\raisebox\s*', value[idx:])
            if not match:
                out.append(value[idx:])
                break
            out.append(value[idx: idx + match.start()])
            rest = drop_braced_args(value[idx + match.end():], 2)
            idx = len(value) - len(rest)
        return ''.join(out)

    text = drop_raisebox(text)
    text = re.sub(r'\\includegraphics\s*(?:\[[^\]]*\])?\s*\{[^}]*\}', '', text)
    text = text.replace('\\\\', ' ')
    for _ in range(3):
        text = re.sub(r'\\[a-zA-Z]+\*?(?:\[[^\]]*\])?\{([^{}]*)\}', r'\1', text)
    text = re.sub(r'\\[a-zA-Z]+\*?', '', text)
    return re.sub(r'\s+', ' ', text).strip()


def normalize_math_fonts(text: str) -> str:
    def fix_span(match: re.Match) -> str:
        inner = match.group(1)
        for old, new in MATH_FONT_UPGRADES:
            inner = re.sub(re.escape(old) + r'(?=[\s{])', lambda _, _new=new: _new, inner)
        return "$" + inner + "$"

    return RE_INLINE_MATH.sub(fix_span, text)


def has_wrapped_display_math(text: str) -> bool:
    if not FLOAT_WRAPPER_RE.match(text):
        return False
    if not DISPLAY_MATH_ENV_RE.search(text):
        return False
    return not NON_MATH_FLOAT_CONTENT_RE.search(text)


def extract_display_math_block(text: str) -> str:
    match = DISPLAY_MATH_ENV_RE.search(text)
    return match.group(0).strip() if match else text


def extract_caption(text: str) -> str:
    match = re.search(r'\\caption\{((?:[^{}]|\{[^{}]*\})*)\}', text, re.DOTALL)
    return match.group(1).strip() if match else ""


def extract_env_body(text: str, env_name: str) -> Tuple[str, str]:
    match = re.search(
        r'\\begin\{' + re.escape(env_name) + r'\*?\}'
        r'(?:\[([^\]]*)\])?'
        r'(.*?)'
        r'\\end\{' + re.escape(env_name) + r'\*?\}',
        text,
        re.DOTALL | re.IGNORECASE,
    )
    return ((match.group(1) or "").strip(), match.group(2).strip()) if match else ("", text)


def split_environment_fragment(text: str, env_name: str) -> Tuple[str, str, str]:
    """
    Split text into (before, body, after) around the first env occurrence.
    If the closing marker is missing, body consumes the remaining text.
    If the env is absent, returns (text, "", "").
    """
    begin = re.search(
        r'\\begin\{' + re.escape(env_name) + r'\}(?:\[([^\]]*)\])?',
        text,
        re.IGNORECASE,
    )
    if not begin:
        return text, "", ""
    before = text[:begin.start()].strip()
    rest = text[begin.end():]
    end = re.search(r'\\end\{' + re.escape(env_name) + r'\}', rest, re.IGNORECASE)
    if not end:
        return before, rest.strip(), ""
    return before, rest[:end.start()].strip(), rest[end.end():].strip()


def sanitize_fragment_for_pandoc(text: str, strip_proof_markers: bool = True) -> str:
    """
    Remove layout-only LaTeX fragments that commonly make pandoc fail when a
    paragraph is rendered in isolation instead of inside its original env.
    """
    text = text or ""

    # Convert simple emphasis envs to plain text content.
    text = re.sub(r'\\begin\{em\}', '', text)
    text = re.sub(r'\\end\{em\}', '', text)
    text = re.sub(r'\\bf\s*', '', text)

    # Drop isolated table/wraptable shells and booktabs rules.
    text = re.sub(r'\\begin\{wraptable\}(?:\[[^\]]*\])?(?:\{[^}]*\}){0,2}', '', text)
    text = re.sub(r'\\end\{wraptable\}', '', text)
    text = re.sub(r'\\begin\{tabular\*?\}(?:\{[^}]*\}){0,2}', '', text)
    text = re.sub(r'\\end\{tabular\*?\}', '', text)
    text = re.sub(r'\\(?:toprule|midrule|bottomrule)\b', '', text)

    if strip_proof_markers:
        text = re.sub(r'\\begin\{proof\}(?:\[[^\]]*\])?', '', text)
        text = re.sub(r'\\end\{proof\}', '', text)

    # Captions appearing outside figure/table envs should degrade to plain text.
    text = re.sub(r'\\caption\{((?:[^{}]|\{[^{}]*\})*)\}', r'\1', text, flags=re.DOTALL)

    # Clean up leftover whitespace after stripping commands.
    text = re.sub(r'[ \t]+\n', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def strip_equation_trailing_punct(tex: str) -> str:
    return re.sub(
        r'([,.])\s*(\\end\{(?:equation|align|gather|multline|flalign|alignat)\*?\})',
        r'\2',
        tex,
        flags=re.DOTALL,
    )
