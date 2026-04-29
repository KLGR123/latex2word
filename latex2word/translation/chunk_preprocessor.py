from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple


def find_matching_brace(text: str, open_pos: int) -> int:
    assert text[open_pos] == "{"
    depth = 1
    idx = open_pos + 1
    while idx < len(text) and depth > 0:
        char = text[idx]
        if char == "\\":
            idx += 2
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        idx += 1
    return idx if depth == 0 else -1


CAPTION_CMD_RE = re.compile(r"\\caption\*?")


def extract_captions(text: str) -> List[Tuple[int, int]]:
    results: List[Tuple[int, int]] = []
    for match in CAPTION_CMD_RE.finditer(text):
        idx = match.end()
        if idx < len(text) and text[idx] == "[":
            idx += 1
            while idx < len(text) and text[idx] != "]":
                idx += 1
            idx += 1
        while idx < len(text) and text[idx] in " \t\n":
            idx += 1
        if idx >= len(text) or text[idx] != "{":
            continue
        end = find_matching_brace(text, idx)
        if end == -1:
            continue
        results.append((idx + 1, end - 1))
    return results


DEFAULT_SKIP_ENVS = frozenset({
    "equation", "equation*",
    "align", "align*", "aligned", "alignat", "alignat*",
    "gather", "gather*", "gathered",
    "multline", "multline*",
    "flalign", "flalign*", "split",
    "cases", "cases*", "dcases", "dcases*", "rcases", "rcases*",
    "math", "displaymath", "subequations",
    "figure", "figure*", "subfigure", "subfloat", "wrapfigure", "SCfigure",
    "minipage", "center", "tikzpicture", "picture", "pspicture",
    "floatrow", "ffigbox",
    "table", "table*", "tabular", "tabular*", "tabularx", "tabulary",
    "tabularray", "longtable", "supertabular", "xtab", "array", "tabbing",
    "sidewaystable", "subtable",
    "verbatim", "verbatim*", "alltt", "lstlisting", "minted",
    "tcolorbox", "mdframed",
    "algorithm", "algorithm*", "algorithmic", "algorithmicx",
    "algorithm2e", "algpseudocode",
})
SKIP_ENVS = DEFAULT_SKIP_ENVS


def configure_skip_envs(envs) -> None:
    global SKIP_ENVS
    if not isinstance(envs, list):
        return
    configured = {str(env).strip() for env in envs if str(env).strip()}
    SKIP_ENVS = frozenset(set(DEFAULT_SKIP_ENVS) | configured)

BEGIN_ENV_RE = re.compile(r"^\\begin\{([^}]+)\}")


def preprocess_chunk(text: str) -> Dict[str, Any]:
    stripped = text.lstrip()
    if not stripped.startswith(r"\begin{"):
        return {
            "mode": "full",
            "translate_text": [text],
            "restore_fn": lambda translations: translations[0],
        }

    match = BEGIN_ENV_RE.match(stripped)
    env_name = match.group(1).strip() if match else ""
    if env_name not in SKIP_ENVS:
        return {
            "mode": "full",
            "translate_text": [text],
            "restore_fn": lambda translations: translations[0],
        }

    caption_spans = extract_captions(text)
    if not caption_spans:
        return {
            "mode": "skip",
            "translate_text": [],
            "restore_fn": lambda _: text,
        }

    caption_contents = [text[start:end] for start, end in caption_spans]

    def restore(translations: List[str]) -> str:
        result = text
        for (start, end), translated in zip(reversed(caption_spans), reversed(translations)):
            result = result[:start] + translated + result[end:]
        return result

    return {
        "mode": "caption_only",
        "translate_text": caption_contents,
        "restore_fn": restore,
    }


def strip_cjk_ascii_spaces(text: str) -> str:
    cjk = r'[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]'
    ascii_char = r'[A-Za-z0-9]'
    text = re.sub(rf'({cjk})\s+({ascii_char})', r'\1\2', text)
    text = re.sub(rf'({ascii_char})\s+({cjk})', r'\1\2', text)
    return text
