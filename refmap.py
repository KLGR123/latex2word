#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
refmap.py

Build an internal cross-reference lookup dictionary from labeled.json
(produced by label.py).

For every paragraph, all LaTeX labeling commands are extracted:
  \\label{key}          -- standard cross-reference anchor
  \\bibitem{key}        -- bibliography entry
  \\hypertarget{name}   -- hyperref named anchor
  \\tag{text}           -- custom equation tag (\\tag* included)
  \\newlabel{key}       -- manually declared label (rare, but valid)
  \\refstepcounter + implicit label (not parsed; too implicit)

Output structure:
{
  "1": {
    "sec:intro":    "1.1节",
    "fig:overview": "图1-2",
    ...
  },
  "2": {
    ...
  }
}

Keys are chapter numbers (as strings, for JSON compatibility).
Values are the env_label of the paragraph containing the label.

One paragraph may carry multiple labels; each maps to the same env_label.

Usage:
  python3 refmap.py --input outputs/labeled.json --output outputs/refmap.json
  python3 refmap.py --input outputs/labeled.json --output outputs/refmap.json --verbose
"""

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
    # Note: tag content is a display string, not a ref key, but it IS used
    # with \ref in some custom setups; include for completeness.
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
# Core builder
# ---------------------------------------------------------------------------

def build_refmap(
    documents: List[dict],
    verbose: bool = False,
) -> Tuple[Dict[str, Dict[str, str]], Dict[str, int]]:
    """
    Walk all documents and build the chapter-keyed reference map.

    Returns
    -------
    refmap : {chapter_str: {label_key: env_label}}
    stats  : summary counters for logging
    """
    # Use defaultdict so we can freely add keys
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
                        # Duplicate key with a different env_label in the same chapter
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

    # Convert defaultdict to plain dict for clean JSON output
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
        # Clean up temp file on failure
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
            "from labeled.json."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
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

    # Build
    refmap, stats = build_refmap(documents, verbose=args.verbose)

    # Write
    write_json_atomic(args.output, refmap)

    # Report
    total_keys  = stats.get("total", 0)
    collisions  = stats.get("collision", 0)
    chapters    = len(refmap)

    print(f"[INFO] Chapters indexed  : {chapters}")
    print(f"[INFO] Total label keys  : {total_keys}")

    # Per-category breakdown
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

    # Per-chapter key counts
    print("[INFO] Keys per chapter:")
    for ch, mapping in sorted(refmap.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 0):
        print(f"         chapter {ch:>3} : {len(mapping)} key(s)")

    print(f"[INFO] Output written to: {args.output}")


if __name__ == "__main__":
    main()