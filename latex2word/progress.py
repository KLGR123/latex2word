from __future__ import annotations

from typing import Any, Callable, Dict, Optional

ProgressEvent = Dict[str, Any]
ProgressCallback = Callable[[ProgressEvent], None]


class ProgressReporter:
    def __init__(self, callback: Optional[ProgressCallback] = None):
        self.callback = callback

    def emit(self, stage: str, message: str, percent: float | None = None, **data: Any) -> None:
        if self.callback is None:
            return
        event: ProgressEvent = {
            "stage": stage,
            "message": message,
        }
        if percent is not None:
            event["percent"] = max(0.0, min(100.0, float(percent)))
        event.update(data)
        self.callback(event)

