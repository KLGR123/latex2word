from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List

from . import chunker
from .tex_inliner import preprocess_folder


@dataclass(frozen=True)
class PreprocessOptions:
    folders: List[Path]
    outputs_dir: Path
    tex_verbose: bool = False
    strip_formatting: bool = False
    prefer_bbl_over_bib: bool = True
    bib_sort: str = "file"
    bib_dedup: bool = False
    bib_lang: str = "en"
    bib_start: int = 1
    chunk_encoding: str = "utf-8"
    chunk_include_title: bool = True
    chunk_keep_commands: bool = True
    chunk_split_on_forced_linebreak: bool = False
    chunk_strict: bool = False


def collect_reference_files(folders: Iterable[Path], prefer_bbl: bool) -> List[Path]:
    ref_files: List[Path] = []
    for folder in folders:
        bbls = sorted(folder.glob("*.bbl"))
        bibs = sorted(folder.glob("*.bib"))
        if prefer_bbl and bbls:
            ref_files.extend(bbls)
        elif bibs:
            ref_files.extend(bibs)
        elif bbls:
            ref_files.extend(bbls)
    return ref_files


def collect_tex_files(folders: Iterable[Path]) -> List[Path]:
    tex_files: List[Path] = []
    for folder in folders:
        tex_files.extend(sorted(folder.glob("*.tex")))
    return tex_files


def run_bibliography(
    paths: List[Path],
    output_path: Path,
    sort: str,
    dedup: bool,
    start: int,
    lang: str,
    strict: bool = False,
    log_level: str = "INFO",
) -> None:
    from . import bibliography

    if start < 1:
        print("[ERROR] --start must be >= 1", file=sys.stderr)
        sys.exit(2)

    logger = bibliography.setup_logger(log_level)
    issues = bibliography.IssueCollector()

    btp_logger = logging.getLogger("bibtexparser")
    btp_logger.setLevel(logging.WARNING)
    btp_logger.propagate = False
    btp_logger.handlers = []
    btp_logger.addHandler(bibliography.ForwardToIssuesHandler(issues, stage="bibtexparser"))

    lang_map = {"en": "et al", "zh": "等", "ja": "他", "fr": "et al", "de": "u. a."}
    bibliography.LANG_TAIL = lang_map.get(lang, "et al")

    try:
        entries = bibliography.load_bib_entries(
            [str(path) for path in paths],
            issues=issues,
            strict=strict,
            logger=logger,
        )
    except Exception:
        issues.print_summary(logger)
        sys.exit(1)

    if not entries:
        issues.error("pipeline", "No entries loaded from input files", files=[str(path) for path in paths])
        issues.print_summary(logger)
        sys.exit(1)

    try:
        if dedup:
            entries = bibliography.deduplicate_entries(entries, issues=issues)
        else:
            bibliography.populate_aliases(entries, issues=issues)
    except Exception as exc:
        issues.error("pipeline", "Failed during dedup/alias stage", error=str(exc))
        if strict:
            issues.print_summary(logger)
            sys.exit(1)

    try:
        entries = bibliography.sort_entries(entries, sort)
    except Exception as exc:
        issues.error("pipeline", "Failed during sort stage", mode=sort, error=str(exc))
        if strict:
            issues.print_summary(logger)
            sys.exit(1)

    try:
        lookup = bibliography.build_lookup(entries, start=start, issues=issues, strict=strict)
    except Exception:
        issues.print_summary(logger)
        sys.exit(1)

    try:
        bibliography.atomic_write_json(str(output_path), lookup, issues=issues, strict=strict)
    except Exception:
        issues.print_summary(logger)
        sys.exit(1)

    logger.info(
        "Wrote %d citation keys (%d unique entries) -> %s",
        len(lookup),
        len(entries),
        output_path,
    )
    issues.print_summary(logger)
    if issues.has_errors():
        sys.exit(1)


def build_chunk_payload(
    tex_files: List[Path],
    encoding: str,
    include_title: bool,
    keep_commands: bool,
    split_on_forced_linebreak: bool,
    strict: bool,
) -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []
    failed = 0

    for tex_path in tex_files:
        tex_path_str = str(tex_path)
        tex = chunker.read_text(tex_path_str, encoding, strict=strict)
        if not tex:
            failed += 1
            continue

        title = ""
        if include_title:
            try:
                title = chunker.extract_title(tex) or ""
            except Exception as exc:
                chunker.eprint(f"[WARNING] Failed to extract title from {tex_path}: {exc}")
                if strict:
                    raise

        try:
            tex = chunker.expand_defined_macros(tex)
        except Exception as exc:
            chunker.eprint(f"[WARNING] Macro expansion failed for {tex_path}: {exc}")
            if strict:
                raise

        try:
            doc = chunker.extract_document(tex)
        except Exception as exc:
            chunker.eprint(f"[ERROR] Failed to extract document body from {tex_path}: {exc}")
            failed += 1
            if strict:
                raise
            continue

        try:
            chunks = chunker.chunk_document(
                doc,
                keep_commands=keep_commands,
                split_on_forced_linebreak=split_on_forced_linebreak,
            )
        except Exception as exc:
            chunker.eprint(f"[ERROR] Chunking failed for {tex_path}: {exc}")
            failed += 1
            if strict:
                raise
            continue

        try:
            chapter_val = chunker.resolve_chapter_from_parent(tex_path_str)
        except Exception as exc:
            chunker.eprint(f"[ERROR] Invalid chapter folder for {tex_path}: {exc}")
            failed += 1
            if strict:
                sys.exit(1)
            continue

        record: Dict[str, Any] = {
            "tex": tex_path_str,
            "paragraphs": [{"id": idx + 1, "text": text} for idx, text in enumerate(chunks)],
            "chapter": chapter_val,
        }
        if include_title:
            record["title"] = title
        results.append(record)

    if failed:
        print(f"[WARNING] Failed documents: {failed}", file=sys.stderr)
        if strict:
            sys.exit(1)
    return {"documents": results}


def run_chunker(
    tex_files: List[Path],
    output_path: Path,
    encoding: str,
    include_title: bool,
    keep_commands: bool,
    split_on_forced_linebreak: bool,
    strict: bool,
) -> None:
    payload = build_chunk_payload(
        tex_files=tex_files,
        encoding=encoding,
        include_title=include_title,
        keep_commands=keep_commands,
        split_on_forced_linebreak=split_on_forced_linebreak,
        strict=strict,
    )
    ok = chunker.atomic_write_json(str(output_path), payload, encoding=encoding, strict=strict)
    if not ok:
        sys.exit(1)
    total_chunks = sum(len(doc.get("paragraphs", [])) for doc in payload["documents"])
    print(f"[INFO] Wrote {len(payload['documents'])} document(s), {total_chunks} chunk(s) -> {output_path}")


def run_preprocess(options: PreprocessOptions) -> None:
    options.outputs_dir.mkdir(parents=True, exist_ok=True)

    if options.folders:
        for folder in options.folders:
            preprocess_folder(
                folder,
                dry_run=False,
                verbose=options.tex_verbose,
                strip_fmt=options.strip_formatting,
            )
    else:
        print("[WARNING] No input folders found.", file=sys.stderr)

    ref_files = collect_reference_files(options.folders, options.prefer_bbl_over_bib)
    tex_files = collect_tex_files(options.folders)
    citations_path = options.outputs_dir / "citations.json"

    if ref_files:
        run_bibliography(
            paths=ref_files,
            output_path=citations_path,
            sort=options.bib_sort,
            dedup=options.bib_dedup,
            start=options.bib_start,
            lang=options.bib_lang,
        )
    elif tex_files:
        print("[INFO] No .bib/.bbl found; trying embedded thebibliography blocks in .tex files.")
        run_bibliography(
            paths=tex_files,
            output_path=citations_path,
            sort=options.bib_sort,
            dedup=options.bib_dedup,
            start=options.bib_start,
            lang=options.bib_lang,
        )
    else:
        print(
            f"No .bib or .bbl files found in folders: {' '.join(str(folder) for folder in options.folders) or '(none)'}",
            file=sys.stderr,
        )
        citations_path.write_text("{}\n", encoding="utf-8")

    if tex_files:
        run_chunker(
            tex_files=tex_files,
            output_path=options.outputs_dir / "chunks.json",
            encoding=options.chunk_encoding,
            include_title=options.chunk_include_title,
            keep_commands=options.chunk_keep_commands,
            split_on_forced_linebreak=options.chunk_split_on_forced_linebreak,
            strict=options.chunk_strict,
        )
    else:
        print(
            f"No .tex files found in folders: {' '.join(str(folder) for folder in options.folders) or '(none)'}",
            file=sys.stderr,
        )
