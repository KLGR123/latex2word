from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from docx import Document
from docx.shared import Cm

from .chunk_renderer import ChunkRenderer
from .context import RenderContext
from .footnotes import init_footnotes


class RenderEngine:
    def __init__(self):
        self.ctx = RenderContext()
        self.renderer = ChunkRenderer(self.ctx)

    @staticmethod
    def build_parser() -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            description="Render replaced.json translation into a Word document."
        )
        parser.add_argument("--json", default="replaced.json", help="Input replaced.json (default: replaced.json)")
        parser.add_argument(
            "--docx",
            default="output.docx",
            help="Output .docx -- created if absent, appended to if present",
        )
        parser.add_argument(
            "--figures-dir",
            default=".",
            help="Base directory for resolving \\includegraphics paths",
        )
        parser.add_argument(
            "--citations",
            default=None,
            help="Path to citations.json -- appends a reference list page",
        )
        parser.add_argument(
            "--skip-title",
            action="store_true",
            help="Do not prepend the chapter title heading",
        )
        return parser

    @staticmethod
    def load_documents(json_path: Path):
        if not json_path.exists():
            sys.exit(f"Error: not found: {json_path}")
        with open(json_path, encoding="utf-8") as handle:
            data = json.load(handle)
        documents = data.get("documents", [])
        if not documents:
            sys.exit("Error: no documents found in JSON.")
        return documents

    @staticmethod
    def open_document(docx_path: Path):
        if docx_path.exists():
            print(f"Opening : {docx_path}")
            return Document(str(docx_path))
        print(f"Creating: {docx_path}")
        doc = Document()
        for sec in doc.sections:
            sec.top_margin = Cm(2.5)
            sec.bottom_margin = Cm(2.5)
            sec.left_margin = Cm(3.0)
            sec.right_margin = Cm(2.5)
        return doc

    def render_documents(self, doc, documents, skip_title: bool, default_figures_dir: str) -> None:
        init_footnotes(doc, self.ctx)
        for doc_meta in documents:
            paragraphs = doc_meta.get("paragraphs", [])
            tex_file = doc_meta.get("tex", "")
            figures_dir = str(Path(tex_file).parent) if tex_file else default_figures_dir
            print(f"\nDocument : {tex_file or '(unknown)'}")
            print(f"Title    : {doc_meta.get('title_translation', '')}")
            print(f"Chapter  : {doc_meta.get('chapter', '')}")
            print(f"Chunks   : {len(paragraphs)}")
            print(f"FigDir   : {figures_dir}\n")
            if not skip_title:
                self.renderer.render_chapter_title(doc, doc_meta)
            for para in paragraphs:
                self.renderer.render_chunk(doc, para, figures_dir)

    def cleanup(self) -> None:
        for tmp in self.ctx.temp_files:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def run(self, args: argparse.Namespace) -> None:
        json_path = Path(args.json)
        docx_path = Path(args.docx)
        documents = self.load_documents(json_path)
        doc = self.open_document(docx_path)
        self.render_documents(doc, documents, skip_title=args.skip_title, default_figures_dir=args.figures_dir)
        if args.citations:
            print("\nAppending reference list ...")
            self.renderer.render_references(doc, args.citations)
        doc.save(str(docx_path))
        print(f"\n[done] Saved -> {docx_path}")
        self.cleanup()


def main() -> None:
    engine = RenderEngine()
    parser = engine.build_parser()
    args = parser.parse_args()
    engine.run(args)
