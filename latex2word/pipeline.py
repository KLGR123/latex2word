from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Dict, List, Optional

from .config import PipelineConfig
from .preprocessing.engine import PreprocessOptions, run_preprocess
from .postprocessing.engine import PostprocessOptions, run_postprocess
from .progress import ProgressCallback, ProgressReporter
from .rules import apply_rules, load_rules
from .translation.engine import TranslationOptions, run_translation


def _format_elapsed(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60}m{total % 60:02d}s"


def _print_step(number: int, label: str) -> None:
    print("", flush=True)
    print("================================================================", flush=True)
    print(f"  STEP {number}: {label}", flush=True)
    print("================================================================", flush=True)


def _load_env_file(path: Path) -> Dict[str, str]:
    env: Dict[str, str] = {}
    if not path.exists():
        return env
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        env[key.strip()] = value
    return env


class StageRunner:
    def __init__(self, config: PipelineConfig, progress: ProgressReporter):
        self.config = config
        self.progress = progress

    def remove_file(self, path: Path) -> None:
        if path.exists():
            path.unlink()


class PreprocessStage(StageRunner):
    def _collect_folders(self) -> List[Path]:
        inputs_dir = Path(self.config.paths.inputs_dir)
        if not inputs_dir.exists():
            return []
        return sorted(path for path in inputs_dir.iterdir() if path.is_dir())

    def run(self) -> None:
        self.progress.emit("preprocess", "Collecting input folders", 2)
        folders = self._collect_folders()
        outputs_dir = Path(self.config.paths.outputs_dir)

        run_preprocess(
            PreprocessOptions(
                folders=folders,
                outputs_dir=outputs_dir,
                tex_verbose=self.config.preprocess.tex_verbose,
                strip_formatting=self.config.preprocess.strip_formatting,
                prefer_bbl_over_bib=self.config.preprocess.prefer_bbl_over_bib,
                bib_sort=self.config.preprocess.bib_sort,
                bib_dedup=self.config.preprocess.bib_dedup,
                bib_lang=self.config.preprocess.bib_lang,
                bib_start=self.config.preprocess.bib_start,
                chunk_encoding=self.config.preprocess.chunk_encoding,
                chunk_include_title=self.config.preprocess.chunk_include_title,
                chunk_keep_commands=self.config.preprocess.chunk_keep_commands,
                chunk_split_on_forced_linebreak=self.config.preprocess.chunk_split_on_forced_linebreak,
                chunk_strict=self.config.preprocess.chunk_strict,
            )
        )
        self.progress.emit("preprocess", "Preprocess complete", 20)


class TranslateStage(StageRunner):
    def _build_env(self) -> Dict[str, str]:
        return _load_env_file(Path(self.config.paths.secrets_file))

    def _resolve_terms_path(self) -> Optional[str]:
        configured = self.config.translate.terms
        if configured and Path(configured).exists():
            return configured
        if configured and not self.config.translate.auto_terms:
            return configured
        if self.config.translate.auto_terms:
            fallback = Path(self.config.paths.configs_dir) / "terms.json"
            if fallback.exists():
                return str(fallback)
        return None

    def run(self) -> None:
        self.progress.emit("translate", "Preparing translation", 22)
        outputs_dir = Path(self.config.paths.outputs_dir)
        input_path = outputs_dir / "chunks.json"
        output_path = outputs_dir / "translated.json"
        env_from_file = self._build_env()
        api_key = self.config.translate.api_key or env_from_file.get(self.config.translate.api_key_env)

        terms_path = self._resolve_terms_path()

        run_translation(
            TranslationOptions(
                input=str(input_path),
                output=str(output_path),
                provider=self.config.translate.provider,
                model=self.config.translate.model,
                concurrency=self.config.translate.concurrency,
                max_batch_tokens=self.config.translate.max_batch_tokens,
                api_key=api_key,
                base_url=self.config.translate.base_url,
                checkpoint=self.config.translate.checkpoint,
                terms=terms_path,
                strip_cjk_spaces=self.config.translate.strip_cjk_spaces,
                debug=self.config.translate.debug,
                progress=self._on_progress,
            )
        )

        if self.config.translate.cleanup_chunks:
            self.remove_file(input_path)

    def _on_progress(self, event: dict) -> None:
        if event.get("stage") != "translate":
            self.progress.emit(**event)
            return
        current = int(event.get("current") or 0)
        total = int(event.get("total") or 0)
        if total > 0:
            percent = 25 + (current / total) * 58
        else:
            percent = event.get("percent", 25)
        self.progress.emit(
            "translate",
            event.get("message", "Translating paragraphs"),
            percent,
            current=current,
            total=total,
        )


class PostprocessStage(StageRunner):
    def run(self) -> None:
        self.progress.emit("postprocess", "Building labels and references", 86)
        outputs_dir = Path(self.config.paths.outputs_dir)
        translated = outputs_dir / "translated.json"
        labeled = outputs_dir / "labeled.json"
        refmap = outputs_dir / "refmap.json"
        replaced = outputs_dir / "replaced.json"
        citations = outputs_dir / "citations.json"
        final_docx = outputs_dir / "final.docx"

        run_postprocess(
            PostprocessOptions(
                translated=translated,
                labeled=labeled,
                refmap=refmap,
                citations=citations,
                replaced=replaced,
                placeholder=self.config.postprocess.replace_placeholder,
                refmap_verbose=self.config.postprocess.refmap_verbose,
                replace_verbose=self.config.postprocess.replace_verbose,
            )
        )

        if self.config.postprocess.cleanup_translated:
            self.remove_file(translated)

        if self.config.postprocess.cleanup_labeled:
            self.remove_file(labeled)

        if self.config.postprocess.overwrite_docx and final_docx.exists():
            final_docx.unlink()

        import argparse
        from .rendering.engine import RenderEngine

        RenderEngine().run(
            argparse.Namespace(
                json=str(replaced),
                docx=str(final_docx),
                figures_dir=".",
                citations=str(citations),
                skip_title=self.config.postprocess.render_skip_title,
            )
        )
        self.progress.emit("done", "DOCX render complete", 100)


class Latex2WordPipeline:
    def __init__(self, config: PipelineConfig, progress: ProgressCallback | None = None):
        self.config = config
        self.progress = ProgressReporter(progress)
        apply_rules(load_rules(config.paths.rules_file))
        self.preprocess = PreprocessStage(config, self.progress)
        self.translate = TranslateStage(config, self.progress)
        self.postprocess = PostprocessStage(config, self.progress)

    def run(self, stage: str = "all") -> None:
        total_start = time.time()
        self.progress.emit("queued", "Pipeline started", 0)

        if stage in {"all", "preprocess"}:
            step_start = time.time()
            _print_step(1, "Preprocess  (chunk / bib)")
            self.preprocess.run()
            print(f"[INFO] Preprocess  (chunk / bib) done in {_format_elapsed(time.time() - step_start)}.", flush=True)

        if stage in {"all", "translate"}:
            step_start = time.time()
            _print_step(2, "Translate")
            self.translate.run()
            print(f"[INFO] Translate done in {_format_elapsed(time.time() - step_start)}.", flush=True)

        if stage in {"all", "postprocess"}:
            step_start = time.time()
            _print_step(3, "Postprocess (label / ref)")
            self.postprocess.run()
            print(f"[INFO] Postprocess (label / ref) done in {_format_elapsed(time.time() - step_start)}.", flush=True)

        print("", flush=True)
        print("================================================================", flush=True)
        print(f"  ALL STEPS COMPLETE  (total: {_format_elapsed(time.time() - total_start)})", flush=True)
        print("================================================================", flush=True)
