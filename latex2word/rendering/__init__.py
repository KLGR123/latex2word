"""Rendering package for DOCX output."""

__all__ = ["RenderEngine", "main"]


def __getattr__(name: str):
    if name in {"RenderEngine", "main"}:
        from .engine import RenderEngine, main

        return {"RenderEngine": RenderEngine, "main": main}[name]
    raise AttributeError(name)
