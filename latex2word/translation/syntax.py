from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

SYNTAX_PATTERNS: Dict[str, re.Pattern] = {
    "cite": re.compile(r"\\cite[a-zA-Z]*\*?\{[^}]+\}"),
    "inline_math": re.compile(r"\$\$?.+?\$\$?", re.DOTALL),
    "display_math": re.compile(r"\\\[.+?\\\]", re.DOTALL),
    "math_env": re.compile(
        r"\\begin\{(?:equation|align|gather|multline|flalign|alignat)"
        r"\*?\}.*?\\end\{(?:equation|align|gather|multline|flalign|alignat)\*?\}",
        re.DOTALL,
    ),
    "ref": re.compile(r"\\(?:ref|eqref|autoref|cref|Cref|hyperref|pageref)\*?\{[^}]+\}"),
    "label": re.compile(r"\\label\{[^}]+\}"),
    "footnote": re.compile(r"\\footnote\{"),
}


def configure_syntax_patterns(config: Dict[str, Any] | None) -> None:
    """Add custom syntax-preservation regexes from the rules file."""
    if not isinstance(config, dict):
        return

    extra_patterns = config.get("extra_patterns")
    if not isinstance(extra_patterns, dict):
        return

    for name, pattern_text in extra_patterns.items():
        name = str(name).strip()
        if not name or not isinstance(pattern_text, str) or not pattern_text.strip():
            continue
        try:
            SYNTAX_PATTERNS[name] = re.compile(pattern_text, re.DOTALL)
        except re.error:
            continue


def extract_syntax_tokens(text: str) -> Dict[str, List[str]]:
    result: Dict[str, List[str]] = {}
    for name, pattern in SYNTAX_PATTERNS.items():
        matches = pattern.findall(text)
        if matches:
            result[name] = matches
    return result


def normalize_token(token: str) -> str:
    return re.sub(r"\s+", " ", token).strip()


def check_syntax_integrity(original: str, translated: str) -> Tuple[bool, List[str]]:
    before = extract_syntax_tokens(original)
    after = extract_syntax_tokens(translated)
    missing: List[str] = []
    for category, tokens in before.items():
        after_tokens_set = {normalize_token(token) for token in after.get(category, [])}
        for token in set(tokens):
            if normalize_token(token) not in after_tokens_set:
                missing.append(token)
    return len(missing) == 0, missing
