from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass
class RenderContext:
    temp_files: List[str] = field(default_factory=list)
    footnote_next_id: int = 1
    footnotes_root: Optional[Any] = None
