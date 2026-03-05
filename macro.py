import re
from dataclasses import dataclass
from typing import Optional, Dict, Tuple, List

@dataclass
class Macro:
    name: str
    n_args: int
    body: str
    has_optional: bool = False
    opt_default: Optional[str] = None

# -----------------------------
# Basic LaTeX lexing helpers
# -----------------------------

def _is_letter(ch: str) -> bool:
    return ch.isalpha() or ch == '@'  # allow @ in internal macro names

def _skip_spaces(s: str, i: int) -> int:
    n = len(s)
    while i < n and s[i].isspace():
        i += 1
    return i

def _skip_comment(s: str, i: int) -> int:
    """If at %, skip until end of line. Return new index."""
    n = len(s)
    if i < n and s[i] == '%':
        while i < n and s[i] != '\n':
            i += 1
    return i

def _read_control_sequence(s: str, i: int) -> Tuple[Optional[str], int]:
    """
    Read a control sequence starting at backslash.
    Returns (name_without_backslash, new_index).
    """
    n = len(s)
    if i >= n or s[i] != '\\':
        return None, i
    i += 1
    if i >= n:
        return "", i
    if _is_letter(s[i]):
        j = i
        while j < n and _is_letter(s[j]):
            j += 1
        return s[i:j], j
    # single-char command like \{ \_ \% \\ etc.
    return s[i], i + 1

def _read_balanced_braces(s: str, i: int) -> Tuple[str, int]:
    """
    Read {...} starting at '{' with balanced braces (naive, but handles nesting).
    Returns (content_without_outer_braces, new_index_after_closing_brace).
    """
    assert s[i] == '{'
    depth = 0
    n = len(s)
    start = i + 1
    i += 1
    depth = 1
    while i < n and depth > 0:
        if s[i] == '\\':  # skip escaped sequences (don't treat braces inside \{ as structure)
            _, i2 = _read_control_sequence(s, i)
            i = i2
            continue
        if s[i] == '{':
            depth += 1
        elif s[i] == '}':
            depth -= 1
        i += 1
    end = i - 1  # index of the closing brace
    return s[start:end], i

def _read_bracket_optional(s: str, i: int) -> Tuple[str, int]:
    """
    Read [...] starting at '[' (no nesting for simplicity).
    Returns (content_without_brackets, new_index_after_closing_bracket).
    """
    assert s[i] == '['
    n = len(s)
    i += 1
    start = i
    while i < n and s[i] != ']':
        i += 1
    return s[start:i], min(i + 1, n)

def _read_one_arg(s: str, i: int) -> Tuple[str, int]:
    """
    Read one LaTeX argument:
    - If next non-space is '{', read balanced braces.
    - Else read a single token: control seq or single char.
    Returns (arg_text, new_index).
    """
    i = _skip_spaces(s, i)
    if i >= len(s):
        return "", i
    if s[i] == '{':
        content, j = _read_balanced_braces(s, i)
        return "{" + content + "}", j  # keep braces in the argument text
    if s[i] == '\\':
        name, j = _read_control_sequence(s, i)
        return "\\" + (name or ""), j
    # single char token
    return s[i], i + 1

def _substitute_body(body: str, args: List[str]) -> str:
    """
    Replace #1..#9 in body with provided args (args are raw LaTeX text).
    """
    def repl(m):
        idx = int(m.group(1)) - 1
        return args[idx] if 0 <= idx < len(args) else m.group(0)
    return re.sub(r"#([1-9])", repl, body)

# -----------------------------
# Macro definition parsers
# -----------------------------

_DEF_LIKE = {"def", "gdef", "edef", "xdef"}
_NEWCOMMAND_LIKE = {"newcommand", "renewcommand", "providecommand"}

def _parse_newcommand(s: str, i: int) -> Tuple[Optional[Macro], int]:
    """
    Parse \newcommand / \renewcommand / \providecommand at index i (which points to backslash).
    Supports:
      \newcommand{\foo}[2][default]{body}
      \newcommand\foo[2]{body}
    Returns (Macro or None, new_index).
    """
    cmd, j = _read_control_sequence(s, i)
    if cmd not in _NEWCOMMAND_LIKE:
        return None, i

    k = _skip_spaces(s, j)
    k = _skip_comment(s, k)

    # macro name: either {\foo} or \foo
    if k < len(s) and s[k] == '{':
        inside, k2 = _read_balanced_braces(s, k)
        # inside should look like \foo
        m = re.match(r"\s*\\([A-Za-z@]+)\s*$", inside)
        if not m:
            return None, i
        name = m.group(1)
        k = k2
    else:
        if k < len(s) and s[k] == '\\':
            name2, k2 = _read_control_sequence(s, k)
            if not name2:
                return None, i
            name = name2
            k = k2
        else:
            return None, i

    k = _skip_spaces(s, k)
    k = _skip_comment(s, k)

    # optional: [n_args]
    n_args = 0
    has_optional = False
    opt_default = None

    if k < len(s) and s[k] == '[':
        raw_n, k = _read_bracket_optional(s, k)
        raw_n = raw_n.strip()
        if raw_n.isdigit():
            n_args = int(raw_n)
        else:
            # sometimes first [] is default, but in \newcommand syntax first [] is arg count
            # we bail to avoid wrong parse
            return None, i

        k = _skip_spaces(s, k)
        k = _skip_comment(s, k)

        # second optional: [default] only if n_args>=1
        if k < len(s) and s[k] == '[':
            has_optional = True
            opt_default, k = _read_bracket_optional(s, k)

    # body: { ... }
    k = _skip_spaces(s, k)
    k = _skip_comment(s, k)

    if k >= len(s) or s[k] != '{':
        return None, i
    body_content, k = _read_balanced_braces(s, k)

    # store body without outer braces (we keep it exactly as in source)
    macro = Macro(
        name=name,
        n_args=n_args,
        body=body_content,
        has_optional=has_optional,
        opt_default=opt_default
    )
    return macro, k

def _parse_def(s: str, i: int) -> Tuple[Optional[Macro], int]:
    """
    Parse \def-like commands:
      \def\foo#1#2{body}
    Returns (Macro or None, new_index).
    """
    cmd, j = _read_control_sequence(s, i)
    if cmd not in _DEF_LIKE:
        return None, i

    k = _skip_spaces(s, j)
    k = _skip_comment(s, k)

    # macro name must be a control sequence \foo
    if k >= len(s) or s[k] != '\\':
        return None, i
    name, k = _read_control_sequence(s, k)
    if not name or not re.match(r"^[A-Za-z@]+$", name):
        return None, i

    # read parameter text until the first '{' that starts body
    n = len(s)
    param_text_start = k
    while k < n and s[k] != '{':
        # stop at comment line
        if s[k] == '%':
            k = _skip_comment(s, k)
            continue
        k += 1
    if k >= n or s[k] != '{':
        return None, i

    param_text = s[param_text_start:k]
    # count occurrences of #<digit>
    arg_nums = re.findall(r"#([1-9])", param_text)
    n_args = max(map(int, arg_nums)) if arg_nums else 0

    body_content, k = _read_balanced_braces(s, k)
    macro = Macro(name=name, n_args=n_args, body=body_content)
    return macro, k

# -----------------------------
# Expansion engine (streaming)
# -----------------------------

def expand_defined_macros(latex: str, *, max_recursion: int = 8) -> str:
    """
    Expand user-defined macros introduced by \\newcommand/\\renewcommand/\\providecommand
    and \\def/\\gdef/\\edef/\\xdef.

    Strategy:
      - Single pass through the document.
      - When encountering a macro definition, record it (and output the definition unchanged).
      - When encountering a usage of a recorded macro, expand it in-place.

    Notes / limitations:
      - Ignores TeX grouping scoping ({...}) and \\begin{...} verbatim-like environments.
      - Arguments: supports {..} and also single-token args (common enough).
      - Optional arg: supports the \\newcommand optional default form.
      - Does not implement advanced expansion primitives (\\expandafter, \\csname, \\let, catcodes).
    """
    macros: Dict[str, Macro] = {}

    def expand_at(s: str, i: int, depth: int) -> Tuple[Optional[str], int]:
        """Try to expand a macro invocation at position i (backslash)."""
        name, j = _read_control_sequence(s, i)
        if not name:
            return None, i
        if name not in macros:
            return None, i

        m = macros[name]
        k = j

        # parse optional arg if applicable
        args: List[str] = []
        if m.has_optional:
            k2 = _skip_spaces(s, k)
            if k2 < len(s) and s[k2] == '[':
                opt_val, k = _read_bracket_optional(s, k2)
                args.append("{" + opt_val + "}")  # normalize to braced form for substitution
            else:
                args.append("{" + (m.opt_default or "") + "}")

        # parse required args
        for _ in range(m.n_args - (1 if m.has_optional else 0)):
            arg, k = _read_one_arg(s, k)
            args.append(arg)

        expanded = _substitute_body(m.body, args)

        # recursively expand inside the expansion (usually desired)
        if depth < max_recursion:
            expanded = _expand_string(expanded, depth + 1)

        return expanded, k

    def _expand_string(s: str, depth: int) -> str:
        out = []
        i = 0
        n = len(s)
        while i < n:
            # keep comments verbatim
            if s[i] == '%':
                j = _skip_comment(s, i)
                out.append(s[i:j])
                i = j
                continue

            if s[i] == '\\':
                # if this is a macro-definition command, parse and record
                macro, j = _parse_newcommand(s, i)
                if macro is None:
                    macro, j = _parse_def(s, i)
                if macro is not None:
                    macros[macro.name] = macro
                    # output the definition exactly as-is
                    out.append(s[i:j])
                    i = j
                    continue

                # else: try to expand a known macro invocation
                expanded, j = expand_at(s, i, depth)
                if expanded is not None:
                    out.append(expanded)
                    i = j
                    continue

                # default: keep the control sequence as-is
                name, j2 = _read_control_sequence(s, i)
                out.append("\\" + (name or ""))
                i = j2
                continue

            # default: copy char
            out.append(s[i])
            i += 1

        return "".join(out)

    return _expand_string(latex, depth=0)

# -----------------------------
# Quick demo
# -----------------------------
if __name__ == "__main__":
    sample = r"""
\newcommand{\figleft}{{\em (Left)}}
\newcommand{\newterm}[1]{{\bf #1}}
\def\figref#1{figure~\ref{#1}}

As shown in \figref{abc}, \figleft\ we define \newterm{JSCC}.
"""
    print(expand_defined_macros(sample))