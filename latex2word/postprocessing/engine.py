from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

from .labeling import label_paragraphs
from .refmap_builder import build_refmap, write_json_atomic as write_refmap_json
from .replacer import MissingKeyTracker, replace_documents, write_json_atomic as write_replaced_json


@dataclass(frozen=True)
class PostprocessOptions:
    translated: Path
    labeled: Path
    refmap: Path
    citations: Path
    replaced: Path
    placeholder: str = "[未找到]"
    refmap_verbose: bool = False
    replace_verbose: bool = False


def load_json(path: Path, label: str) -> Any:
    if not path.exists():
        print(f"[ERROR] {label} file not found: {path}", file=sys.stderr)
        sys.exit(1)
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:
        print(f"[ERROR] Invalid JSON in {label} ({path}): {exc}", file=sys.stderr)
        sys.exit(1)


def write_json_atomic(path: Path, data: Any) -> None:
    out_dir = path.parent
    os.makedirs(out_dir, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as exc:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        print(f"[ERROR] Failed to write {path}: {exc}", file=sys.stderr)
        sys.exit(1)


def print_label_summary(summary: Dict[str, int], output_path: Path) -> None:
    category_labels = {
        "abstract": "摘要     (abstract)",
        "section": "节       (\\section)",
        "subsection": "小节     (\\subsection / deeper)",
        "figure": "图       (figure)",
        "table": "表       (table)",
        "algorithm": "算法     (algorithm)",
        "code": "代码     (code)",
        "math": "式       (math)",
        "text": "文本     (plain text)",
    }
    total = sum(summary.values())
    print(f"[INFO] Labeled {total} paragraph(s):")
    for category, display in category_labels.items():
        count = summary.get(category, 0)
        if count:
            print(f"       {display}: {count}")
    print(f"[INFO] Output written to: {output_path}")


def print_refmap_summary(stats: Dict[str, int], refmap: Dict[str, Dict[str, str]], output_path: Path) -> None:
    total_keys = stats.get("total", 0)
    collisions = stats.get("collision", 0)
    print(f"[INFO] Chapters indexed  : {len(refmap)}")
    print(f"[INFO] Total label keys  : {total_keys}")

    category_display = {
        "label": r"\label{}",
        "bibitem": r"\bibitem{}",
        "hypertarget": r"\hypertarget{}",
        "tag": r"\tag{}",
        "newlabel": r"\newlabel{}",
    }
    for category, display in category_display.items():
        count = stats.get(f"category:{category}", 0)
        if count:
            print(f"         {display:<22} {count}")

    if collisions:
        print(
            f"[WARN]  {collisions} duplicate key collision(s) detected "
            f"(last value kept). Re-run with --verbose for details."
        )
    else:
        print("[INFO] No duplicate key collisions.")
    print(f"[INFO] Refmap written to: {output_path}")


def print_replace_summary(
    documents: list,
    citations: Dict[str, Any],
    refmap: Dict[str, Dict[str, str]],
    output_path: Path,
    tracker: MissingKeyTracker,
) -> None:
    total_paras = sum(len(doc.get("paragraphs", [])) for doc in documents)
    print(f"[INFO] Loaded {len(documents)} document(s), {total_paras} paragraph(s).")
    print(f"[INFO] Citations index: {len(citations)} key(s).")
    total_ref_keys = sum(len(mapping) for mapping in refmap.values())
    print(f"[INFO] Refmap: {len(refmap)} chapter(s), {total_ref_keys} label key(s).")
    print(f"[INFO] Output written to: {output_path}")
    tracker.print_summary()


def run_postprocess(options: PostprocessOptions) -> None:
    data = load_json(options.translated, "translated")
    documents = data.get("documents", [])
    if not documents:
        print("[WARNING] No documents found in translated.json.", file=sys.stderr)

    documents, label_summary = label_paragraphs(documents)
    data["documents"] = documents
    write_json_atomic(options.labeled, data)
    print_label_summary(label_summary, options.labeled)

    total_paras = sum(len(doc.get("paragraphs", [])) for doc in documents)
    print(f"[INFO] Loaded {len(documents)} document(s), {total_paras} paragraph(s).")
    refmap, refmap_stats = build_refmap(documents, verbose=options.refmap_verbose)
    write_refmap_json(str(options.refmap), refmap)
    data["documents"] = documents
    write_json_atomic(options.labeled, data)
    print(f"[INFO] Updated labeled.json in-place: {options.labeled}")
    print_refmap_summary(refmap_stats, refmap, options.refmap)

    citations: Dict[str, Any] = load_json(options.citations, "citations")
    docs_out, tracker = replace_documents(
        documents,
        citations=citations,
        refmap=refmap,
        placeholder=options.placeholder,
        verbose=options.replace_verbose,
    )
    write_replaced_json(str(options.replaced), {"documents": docs_out})
    print_replace_summary(documents, citations, refmap, options.replaced, tracker)
