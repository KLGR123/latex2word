"""Unified pipeline package for latex2word."""

from .config import PipelineConfig, load_pipeline_config

__all__ = ["PipelineConfig", "Latex2WordPipeline", "load_pipeline_config"]


def __getattr__(name: str):
    if name == "Latex2WordPipeline":
        from .pipeline import Latex2WordPipeline

        return Latex2WordPipeline
    raise AttributeError(name)
