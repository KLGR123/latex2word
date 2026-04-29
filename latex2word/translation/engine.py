from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass
from typing import Dict, Optional

from backend.model_catalog import list_provider_names
from .checkpoint import CheckpointManager
from .orchestrator import translate_all
from .providers import build_provider
from .terms import load_terms
from ..progress import ProgressCallback

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TranslationOptions:
    input: str
    output: str
    provider: str
    model: str
    concurrency: int = 10
    max_batch_tokens: int = 1500
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    checkpoint: Optional[str] = None
    terms: Optional[str] = None
    strip_cjk_spaces: bool = False
    debug: bool = False
    progress: Optional[ProgressCallback] = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Async concurrent LLM translation of LaTeX chunks JSON.",
    )
    parser.add_argument("--input", required=True, help="Input chunks JSON from preprocessing")
    parser.add_argument("--output", required=True, help="Output translated JSON")
    parser.add_argument(
        "--provider",
        required=True,
        choices=list_provider_names(),
        help="LLM provider",
    )
    parser.add_argument("--model", required=True, help="Model ID (e.g. claude-sonnet-4-6, gpt-4o)")
    parser.add_argument("--concurrency", type=int, default=10, help="Max concurrent API requests")
    parser.add_argument("--max-batch-tokens", type=int, default=1500, help="Max estimated tokens per batch")
    parser.add_argument("--api-key", default=None, help="API key (overrides env var)")
    parser.add_argument("--base-url", default=None, help="Custom base URL")
    parser.add_argument("--checkpoint", default=None, help="Checkpoint file path")
    parser.add_argument(
        "--terms",
        default=None,
        metavar="TERMS_JSON",
        help="Path to chapter-scoped glossary JSON.",
    )
    parser.add_argument(
        "--strip-cjk-spaces",
        action="store_true",
        default=False,
        help="Remove spaces between CJK characters and ASCII/digits in translated text.",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    return parser


def load_input_documents(path: str):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        sys.exit(f"[ERROR] Input file not found: {path}")
    except json.JSONDecodeError as exc:
        sys.exit(f"[ERROR] Invalid JSON in {path}: {exc}")

    documents = data.get("documents", [])
    if not documents:
        sys.exit("[ERROR] No documents found in input JSON.")
    total = sum(len(doc.get("paragraphs", [])) for doc in documents)
    log.info("Loaded %d document(s), %d paragraph(s) total.", len(documents), total)
    return documents


def write_output(path: str, docs_out) -> None:
    out_dir = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(out_dir, exist_ok=True)
    tmp_out = path + ".tmp"
    with open(tmp_out, "w", encoding="utf-8") as handle:
        json.dump({"documents": docs_out}, handle, ensure_ascii=False, indent=2)
    os.replace(tmp_out, path)
    log.info("Wrote translated output to %s", path)


def run_translation(options: TranslationOptions) -> None:
    if options.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    documents = load_input_documents(options.input)

    terms_by_chapter: Optional[Dict[int, Dict[str, str]]] = None
    if options.terms:
        terms_by_chapter = load_terms(options.terms)
        log.info(
            "Loaded glossary for %d chapter(s) from %s",
            len(terms_by_chapter),
            options.terms,
        )

    provider = build_provider(
        options.provider,
        options.model,
        api_key=options.api_key,
        base_url=options.base_url,
    )
    sem = asyncio.Semaphore(options.concurrency)
    checkpoint_path = options.checkpoint or (options.output + ".checkpoint.json")
    checkpoint = CheckpointManager(checkpoint_path)

    try:
        docs_out = asyncio.run(
            translate_all(
                documents,
                provider,
                sem,
                checkpoint,
                max_batch_tokens=options.max_batch_tokens,
                terms_by_chapter=terms_by_chapter,
                strip_cjk_spaces=options.strip_cjk_spaces,
                progress=options.progress,
            )
        )
    except KeyboardInterrupt:
        log.info("Interrupted. Progress saved to checkpoint: %s", checkpoint_path)
        sys.exit(130)

    write_output(options.output, docs_out)
    checkpoint.remove()
    log.info("Done. Checkpoint removed.")


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    run_translation(
        TranslationOptions(
            input=args.input,
            output=args.output,
            provider=args.provider,
            model=args.model,
            concurrency=args.concurrency,
            max_batch_tokens=args.max_batch_tokens,
            api_key=args.api_key,
            base_url=args.base_url,
            checkpoint=args.checkpoint,
            terms=args.terms,
            strip_cjk_spaces=args.strip_cjk_spaces,
            debug=args.debug,
        )
    )
