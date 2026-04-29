from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass
class PathConfig:
    project_root: str
    inputs_dir: str
    outputs_dir: str
    configs_dir: str
    rules_file: str
    secrets_file: str


@dataclass
class PreprocessConfig:
    tex_verbose: bool = True
    strip_formatting: bool = True
    prefer_bbl_over_bib: bool = True
    bib_sort: str = "year"
    bib_dedup: bool = True
    bib_lang: str = "en"
    bib_start: int = 1
    chunk_encoding: str = "utf-8"
    chunk_include_title: bool = True
    chunk_keep_commands: bool = True
    chunk_split_on_forced_linebreak: bool = True
    chunk_strict: bool = True


@dataclass
class TranslateConfig:
    provider: str = "deepseek"
    model: str = "deepseek-chat"
    concurrency: int = 8
    max_batch_tokens: int = 1500
    api_key: Optional[str] = None
    api_key_env: str = "DEEPSEEK_API_KEY"
    base_url: Optional[str] = "https://api.deepseek.com"
    checkpoint: Optional[str] = None
    terms: Optional[str] = None
    auto_terms: bool = True
    strip_cjk_spaces: bool = True
    debug: bool = False
    cleanup_chunks: bool = False


@dataclass
class PostprocessConfig:
    refmap_verbose: bool = True
    replace_verbose: bool = True
    replace_placeholder: str = "[未找到]"
    render_skip_title: bool = False
    overwrite_docx: bool = True
    cleanup_translated: bool = False
    cleanup_labeled: bool = False


@dataclass
class PipelineConfig:
    paths: PathConfig
    preprocess: PreprocessConfig
    translate: TranslateConfig
    postprocess: PostprocessConfig

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _default_config(project_root: Path) -> PipelineConfig:
    configs_dir = project_root / "configs"
    return PipelineConfig(
        paths=PathConfig(
            project_root=str(project_root),
            inputs_dir=str(project_root / "inputs"),
            outputs_dir=str(project_root / "outputs"),
            configs_dir=str(configs_dir),
            rules_file=str(configs_dir / "rules.json"),
            secrets_file=str(project_root / "secrets.env"),
        ),
        preprocess=PreprocessConfig(),
        translate=TranslateConfig(
            terms=str(configs_dir / "terms.json"),
        ),
        postprocess=PostprocessConfig(),
    )


def _merge_into_dataclass(obj: Any, data: Dict[str, Any]) -> None:
    for key, value in data.items():
        if not hasattr(obj, key):
            raise ValueError(f"Unknown config key: {key}")
        current = getattr(obj, key)
        if hasattr(current, "__dataclass_fields__") and isinstance(value, dict):
            _merge_into_dataclass(current, value)
        else:
            setattr(obj, key, value)


def _load_json_file(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("Config file must contain a JSON object.")
    return data


def load_pipeline_config(project_root: Path, config_path: Optional[Path] = None) -> PipelineConfig:
    config = _default_config(project_root)
    if config_path is None:
        return config
    raw = _load_json_file(config_path)
    _merge_into_dataclass(config, raw)
    return config


def set_nested_attr(config: PipelineConfig, path: str, value: Any) -> None:
    target: Any = config
    parts = path.split(".")
    for part in parts[:-1]:
        target = getattr(target, part)
    setattr(target, parts[-1], value)
