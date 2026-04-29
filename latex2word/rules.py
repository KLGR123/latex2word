from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


def load_rules(path: str) -> Dict[str, Any]:
    rules_path = Path(path)
    if not rules_path.exists():
        return {}
    with rules_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Rules file must contain a JSON object: {rules_path}")
    return data


def apply_rules(rules: Dict[str, Any]) -> None:
    if not rules:
        return

    preprocess = rules.get("preprocess", {})
    if isinstance(preprocess, dict):
        from .preprocessing.chunker import configure_chunk_block_envs

        configure_chunk_block_envs(preprocess.get("chunk_block_envs"))

    translation = rules.get("translation", {})
    if isinstance(translation, dict):
        from .translation.chunk_preprocessor import configure_skip_envs
        from .translation.prompts import configure_prompts
        from .translation.section_cache import configure_section_title_cache
        from .translation.syntax import configure_syntax_patterns

        configure_prompts(translation.get("prompts"))
        configure_section_title_cache(translation.get("section_title_cache"))
        configure_skip_envs(translation.get("skip_envs"))
        configure_syntax_patterns(translation.get("syntax"))

    postprocess = rules.get("postprocess", {})
    if isinstance(postprocess, dict):
        from .postprocessing.labeling import configure_env_categories

        configure_env_categories(postprocess.get("label_env_categories"))

    rendering = rules.get("rendering", {})
    if isinstance(rendering, dict):
        from .rendering.settings import configure_render_settings

        configure_render_settings(rendering)
