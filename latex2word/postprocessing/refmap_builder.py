import argparse
import json
import os
import re
import sys
from collections import defaultdict
from typing import Dict, List, Tuple


# ---------------------------------------------------------------------------
# Label extraction patterns
# ---------------------------------------------------------------------------

# Each entry: (category_name, compiled_regex)
# The regex must capture the label key in group 1.
_LABEL_PATTERNS: List[Tuple[str, re.Pattern]] = [
    # \label{key}  -- most common; also \label[optional]{key} (rarely used)
    (
        "label",
        re.compile(r"\\label(?:\[[^\]]*\])?\{([^}]+)\}"),
    ),
    # \bibitem[optional]{key}
    (
        "bibitem",
        re.compile(r"\\bibitem(?:\[[^\]]*\])?\{([^}]+)\}"),
    ),
    # \hypertarget{name}{text}  -- only the name (group 1) is the anchor key
    (
        "hypertarget",
        re.compile(r"\\hypertarget\{([^}]+)\}"),
    ),
    # \tag{text} and \tag*{text}  -- equation tags used as display labels
    (
        "tag",
        re.compile(r"\\tag\*?\{([^}]+)\}"),
    ),
    # \newlabel{key}{...}  -- written by LaTeX into .aux; sometimes appears
    # directly in source (e.g. manually crafted aux-style macros)
    (
        "newlabel",
        re.compile(r"\\newlabel\{([^}]+)\}"),
    ),
]


def extract_label_keys(text: str) -> Dict[str, List[str]]:
    """
    Extract all label keys from a paragraph's LaTeX source text.

    Returns {category: [key, ...]} for every category with at least one match.
    Keys are stripped of surrounding whitespace.
    """
    result: Dict[str, List[str]] = {}
    for category, pattern in _LABEL_PATTERNS:
        matches = [m.group(1).strip() for m in pattern.finditer(text)]
        if matches:
            result[category] = matches
    return result


# ---------------------------------------------------------------------------
# Label-command stripping
# ---------------------------------------------------------------------------

# Patterns ordered so that multi-arg forms are tried before single-arg ones.
# Each tuple: (re.Pattern, replacement_string_or_None)
# None means "use a callable" (for hypertarget, which keeps its text arg).
#
# Surrounding whitespace strategy: strip any mix of spaces, tabs, and newlines
# on both sides of the command.  Using \s* would risk collapsing paragraph
# breaks, so we are deliberate:
#   - Leading  : strip spaces/tabs on the same line (\t\  ) and at most one
#                preceding newline.
#   - Trailing : strip spaces/tabs on the remainder of the same line and the
#                newline that terminates it (making an otherwise-blank line
#                disappear entirely).
# This preserves blank-line paragraph separators between chunks.
_LABEL_STRIP_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # \newlabel{key}{...second-arg...}  -- keep nothing
    (
        re.compile(r"\n?[ \t]*\\newlabel\{[^}]+\}\{[^}]*\}[ \t]*\n?"),
        "",
    ),
    # \hypertarget{name}{display_text}  -- keep display_text, drop wrapper
    # Handled separately via _HYPERTARGET_STRIP_RE below.

    # \label[opt]{key}  -- keep nothing
    (
        re.compile(r"\n?[ \t]*\\label(?:\[[^\]]*\])?\{[^}]+\}[ \t]*\n?"),
        "",
    ),
    # \bibitem[opt]{key}  -- keep nothing (bibliography anchor only)
    (
        re.compile(r"\n?[ \t]*\\bibitem(?:\[[^\]]*\])?\{[^}]+\}[ \t]*\n?"),
        "",
    ),
    # \tag*?{text}  -- keep nothing (equation tag is display formatting)
    (
        re.compile(r"\n?[ \t]*\\tag\*?\{[^}]+\}[ \t]*\n?"),
        "",
    ),
]

# \hypertarget{name}{display_text}  -> display_text
# Simple [^}]* for display_text; if nesting is needed a brace-depth approach
# would be required, but anchor labels virtually never contain nested braces.
_HYPERTARGET_STRIP_RE = re.compile(
    r"\n?[ \t]*\\hypertarget\{[^}]+\}\{([^}]*)\}[ \t]*\n?"
)

# After stripping it is possible that three or more consecutive newlines appear
# (e.g. when a label was on its own line between two real lines).
# Collapse to at most two newlines to avoid excessive vertical whitespace
# inside a chunk without disturbing paragraph-level blank-line separators.
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


def strip_label_commands(text: str) -> str:
    """
    Remove LaTeX label/anchor commands from *text*, preserving the surrounding
    content.  \\hypertarget{name}{display_text} is reduced to display_text;
    all other label commands are deleted entirely.

    Horizontal whitespace and newlines directly adjacent to each removed
    command are also trimmed so that no orphan blank lines or extra spaces
    are left behind.
    """
    # 1. \hypertarget: keep display text, drop the rest
    text = _HYPERTARGET_STRIP_RE.sub(lambda m: m.group(1), text)

    # 2. All other label commands: drop entirely
    for pattern, replacement in _LABEL_STRIP_PATTERNS:
        text = pattern.sub(replacement, text)

    # 3. Collapse accidental triple+ newlines created by stripping
    text = _MULTI_NEWLINE_RE.sub("\n\n", text)

    return text


# ---------------------------------------------------------------------------
# Core builder
# ---------------------------------------------------------------------------

def build_refmap(
    documents: List[dict],
    verbose: bool = False,
) -> Tuple[Dict[str, Dict[str, str]], Dict[str, int]]:
    """
    Walk all documents and build the chapter-keyed reference map.

    As a side-effect, each paragraph's "text" field is updated in-place:
    every label command that was extracted is stripped from the text
    (including surrounding whitespace / newlines) because its information
    is now recorded in the refmap.

    Returns
    -------
    refmap : {chapter_str: {label_key: env_label}}
    stats  : summary counters for logging
    """
    refmap: Dict[str, Dict[str, str]] = defaultdict(dict)

    stats = defaultdict(int)
    collision_examples: List[str] = []

    for doc in documents:
        chapter = str(doc.get("chapter", "unknown"))

        for para in doc.get("paragraphs", []):
            text: str = para.get("text", "")
            env_label: str = para["env_label"]  # guaranteed present per spec

            found = extract_label_keys(text)

            for category, keys in found.items():
                for key in keys:
                    stats["total"] += 1
                    stats[f"category:{category}"] += 1

                    existing = refmap[chapter].get(key)
                    if existing is not None and existing != env_label:
                        stats["collision"] += 1
                        if len(collision_examples) < 5:
                            collision_examples.append(
                                f"  chapter={chapter} key={key!r}: "
                                f"{existing!r} -> {env_label!r} (overwritten)"
                            )
                        if verbose:
                            print(
                                f"[WARN] Duplicate label key in chapter {chapter}: "
                                f"{key!r} was {existing!r}, now {env_label!r}",
                                file=sys.stderr,
                            )

                    refmap[chapter][key] = env_label

            # Strip all found label commands from the paragraph text.
            # Only touch the field when there is something to remove.
            if found:
                para["text"] = strip_label_commands(text)
                if isinstance(para.get("translation"), str):
                    para["translation"] = strip_label_commands(para["translation"])

    return {ch: dict(mapping) for ch, mapping in sorted(refmap.items())}, dict(stats)


# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------

def load_json(path: str) -> dict:
    if not os.path.exists(path):
        sys.exit(f"[ERROR] Input file not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        sys.exit(f"[ERROR] Invalid JSON in {path}: {exc}")


def write_json_atomic(path: str, data: dict) -> None:
    out_dir = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(out_dir, exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as exc:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        sys.exit(f"[ERROR] Failed to write {path}: {exc}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=(
            "Build a chapter-keyed internal reference lookup dictionary "
            "from labeled.json, stripping label commands from paragraph text."
        ),
    )
    ap.add_argument(
        "--input",
        default=os.path.join("outputs", "labeled.json"),
        help="Path to labeled.json (default: outputs/labeled.json)",
    )
    ap.add_argument(
        "--output",
        default=os.path.join("outputs", "refmap.json"),
        help="Path to output refmap.json (default: outputs/refmap.json)",
    )
    ap.add_argument(
        "--labeled-out",
        default=None,
        metavar="PATH",
        help=(
            "If given, write the modified labeled.json (with label commands "
            "stripped from paragraph text) to this path instead of overwriting "
            "the input.  Defaults to overwriting --input in-place."
        ),
    )
    ap.add_argument(
        "--verbose",
        action="store_true",
        help="Print duplicate-key warnings to stderr.",
    )
    return ap.parse_args()


def main() -> None:
    args = parse_args()

    # Load
    data = load_json(args.input)
    documents = data.get("documents", [])
    if not documents:
        print("[WARNING] No documents found in input JSON.", file=sys.stderr)

    total_paras = sum(len(d.get("paragraphs", [])) for d in documents)
    print(f"[INFO] Loaded {len(documents)} document(s), {total_paras} paragraph(s).")

    # Build refmap (modifies paragraph text in-place)
    refmap, stats = build_refmap(documents, verbose=args.verbose)

    # Write refmap.json
    write_json_atomic(args.output, refmap)

    # Write back the modified labeled.json (with labels stripped from text)
    labeled_out = args.labeled_out or args.input
    data["documents"] = documents
    write_json_atomic(labeled_out, data)
    if labeled_out == args.input:
        print(f"[INFO] Updated labeled.json in-place: {args.input}")
    else:
        print(f"[INFO] Wrote stripped labeled.json to: {labeled_out}")

    # Report
    total_keys  = stats.get("total", 0)
    collisions  = stats.get("collision", 0)
    chapters    = len(refmap)

    print(f"[INFO] Chapters indexed  : {chapters}")
    print(f"[INFO] Total label keys  : {total_keys}")

    category_display = {
        "label":       r"\label{}",
        "bibitem":     r"\bibitem{}",
        "hypertarget": r"\hypertarget{}",
        "tag":         r"\tag{}",
        "newlabel":    r"\newlabel{}",
    }
    for cat, display in category_display.items():
        count = stats.get(f"category:{cat}", 0)
        if count:
            print(f"         {display:<22} {count}")

    if collisions:
        print(
            f"[WARN]  {collisions} duplicate key collision(s) detected "
            f"(last value kept). Re-run with --verbose for details."
        )
    else:
        print("[INFO] No duplicate key collisions.")

    print(f"[INFO] Refmap written to: {args.output}")


if __name__ == "__main__":
    main()
