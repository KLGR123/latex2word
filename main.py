#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict

from backend.model_catalog import list_provider_names
from latex2word import Latex2WordPipeline, load_pipeline_config
from latex2word.config import PipelineConfig, set_nested_attr


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Unified latex2word pipeline entrypoint.",
    )
    parser.add_argument(
        "--stage",
        choices=["all", "preprocess", "translate", "postprocess"],
        default="all",
        help="Run the whole pipeline or a single stage.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Optional JSON config file that overrides built-in defaults.",
    )
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="Print the resolved config as JSON and exit.",
    )

    parser.add_argument("--inputs-dir", default=None)
    parser.add_argument("--outputs-dir", default=None)
    parser.add_argument("--configs-dir", default=None)
    parser.add_argument("--rules-file", default=None)
    parser.add_argument("--secrets-file", default=None)

    parser.add_argument("--tex-verbose", default=None, action=argparse.BooleanOptionalAction)
    parser.add_argument("--strip-formatting", default=None, action=argparse.BooleanOptionalAction)
    parser.add_argument("--prefer-bbl-over-bib", default=None, action=argparse.BooleanOptionalAction)
    parser.add_argument("--bib-sort", default=None, choices=["file", "year", "key"])
    parser.add_argument("--bib-dedup", default=None, action=argparse.BooleanOptionalAction)
    parser.add_argument("--bib-lang", default=None, choices=["en", "zh", "ja", "fr", "de"])
    parser.add_argument("--bib-start", default=None, type=int)
    parser.add_argument("--chunk-encoding", default=None)
    parser.add_argument("--chunk-include-title", default=None, action=argparse.BooleanOptionalAction)
    parser.add_argument("--chunk-keep-commands", default=None, action=argparse.BooleanOptionalAction)
    parser.add_argument("--chunk-split-on-forced-linebreak", default=None, action=argparse.BooleanOptionalAction)
    parser.add_argument("--chunk-strict", default=None, action=argparse.BooleanOptionalAction)

    parser.add_argument("--provider", default=None, choices=list_provider_names())
    parser.add_argument("--model", default=None)
    parser.add_argument("--concurrency", default=None, type=int)
    parser.add_argument("--max-batch-tokens", default=None, type=int)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--api-key-env", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--checkpoint", default=None)
    parser.add_argument("--terms", default=None)
    parser.add_argument("--auto-terms", default=None, action=argparse.BooleanOptionalAction)
    parser.add_argument("--strip-cjk-spaces", default=None, action=argparse.BooleanOptionalAction)
    parser.add_argument("--translate-debug", default=None, action=argparse.BooleanOptionalAction)
    parser.add_argument("--cleanup-chunks", default=None, action=argparse.BooleanOptionalAction)

    parser.add_argument("--refmap-verbose", default=None, action=argparse.BooleanOptionalAction)
    parser.add_argument("--replace-verbose", default=None, action=argparse.BooleanOptionalAction)
    parser.add_argument("--replace-placeholder", default=None)
    parser.add_argument("--render-skip-title", default=None, action=argparse.BooleanOptionalAction)
    parser.add_argument("--overwrite-docx", default=None, action=argparse.BooleanOptionalAction)
    parser.add_argument("--cleanup-translated", default=None, action=argparse.BooleanOptionalAction)
    parser.add_argument("--cleanup-labeled", default=None, action=argparse.BooleanOptionalAction)
    return parser


def _cli_overrides(args: argparse.Namespace) -> Dict[str, Any]:
    mapping = {
        "inputs_dir": "paths.inputs_dir",
        "outputs_dir": "paths.outputs_dir",
        "configs_dir": "paths.configs_dir",
        "rules_file": "paths.rules_file",
        "secrets_file": "paths.secrets_file",
        "tex_verbose": "preprocess.tex_verbose",
        "strip_formatting": "preprocess.strip_formatting",
        "prefer_bbl_over_bib": "preprocess.prefer_bbl_over_bib",
        "bib_sort": "preprocess.bib_sort",
        "bib_dedup": "preprocess.bib_dedup",
        "bib_lang": "preprocess.bib_lang",
        "bib_start": "preprocess.bib_start",
        "chunk_encoding": "preprocess.chunk_encoding",
        "chunk_include_title": "preprocess.chunk_include_title",
        "chunk_keep_commands": "preprocess.chunk_keep_commands",
        "chunk_split_on_forced_linebreak": "preprocess.chunk_split_on_forced_linebreak",
        "chunk_strict": "preprocess.chunk_strict",
        "provider": "translate.provider",
        "model": "translate.model",
        "concurrency": "translate.concurrency",
        "max_batch_tokens": "translate.max_batch_tokens",
        "api_key": "translate.api_key",
        "api_key_env": "translate.api_key_env",
        "base_url": "translate.base_url",
        "checkpoint": "translate.checkpoint",
        "terms": "translate.terms",
        "auto_terms": "translate.auto_terms",
        "strip_cjk_spaces": "translate.strip_cjk_spaces",
        "translate_debug": "translate.debug",
        "cleanup_chunks": "translate.cleanup_chunks",
        "refmap_verbose": "postprocess.refmap_verbose",
        "replace_verbose": "postprocess.replace_verbose",
        "replace_placeholder": "postprocess.replace_placeholder",
        "render_skip_title": "postprocess.render_skip_title",
        "overwrite_docx": "postprocess.overwrite_docx",
        "cleanup_translated": "postprocess.cleanup_translated",
        "cleanup_labeled": "postprocess.cleanup_labeled",
    }
    overrides: Dict[str, Any] = {}
    for arg_name, config_path in mapping.items():
        value = getattr(args, arg_name)
        if value is not None:
            overrides[config_path] = value
    return overrides


def _apply_overrides(config: PipelineConfig, overrides: Dict[str, Any]) -> PipelineConfig:
    for path, value in overrides.items():
        set_nested_attr(config, path, value)
    return config


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent
    config_path = Path(args.config).resolve() if args.config else None
    config = load_pipeline_config(project_root, config_path)
    config = _apply_overrides(config, _cli_overrides(args))

    if args.print_config:
        print(json.dumps(config.to_dict(), ensure_ascii=False, indent=2))
        return

    pipeline = Latex2WordPipeline(config)
    pipeline.run(stage=args.stage)


if __name__ == "__main__":
    main()
