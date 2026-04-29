from __future__ import annotations

import json
import logging
import sys
from typing import Any, Dict

log = logging.getLogger(__name__)


def load_terms(path: str) -> Dict[int, Dict[str, str]]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        sys.exit(f"[ERROR] Terms file not found: {path}")
    except json.JSONDecodeError as exc:
        sys.exit(f"[ERROR] Invalid JSON in terms file {path}: {exc}")

    return normalize_terms(raw)


def normalize_terms(raw: Any) -> Dict[int, Dict[str, str]]:
    result: Dict[int, Dict[str, str]] = {}

    if isinstance(raw, dict):
        iterable = [{"chapter": chapter, "terms": terms} for chapter, terms in raw.items()]
    elif isinstance(raw, list):
        iterable = raw
    else:
        log.warning("Skipping malformed terms payload: %s", raw)
        return result

    for entry in iterable:
        if not isinstance(entry, dict):
            log.warning("Skipping malformed terms entry: %s", entry)
            continue
        chapter = entry.get("chapter")
        terms = entry.get("terms", {})
        if chapter is None or not isinstance(terms, dict):
            log.warning("Skipping malformed terms entry: %s", entry)
            continue
        try:
            chapter_number = int(chapter)
        except (TypeError, ValueError):
            log.warning("Skipping terms entry with invalid chapter: %s", entry)
            continue
        result[chapter_number] = {str(key): str(value) for key, value in terms.items()}
    return result
