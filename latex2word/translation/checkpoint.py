from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Dict

log = logging.getLogger(__name__)


class CheckpointManager:
    def __init__(self, path: str):
        self.path = path
        self._lock = asyncio.Lock()
        self._data: Dict[str, str] = {}

    def load(self) -> Dict[str, str]:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    self._data = json.load(handle)
                log.info("Loaded checkpoint with %d entries from %s", len(self._data), self.path)
            except Exception as exc:
                log.warning("Could not load checkpoint (%s), starting fresh.", exc)
                self._data = {}
        return self._data

    async def save(self, key: str, value: str) -> None:
        async with self._lock:
            self._data[key] = value
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as handle:
                    json.dump(self._data, handle, ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)
            except Exception as exc:
                log.error("Failed to write checkpoint: %s", exc)

    def remove(self) -> None:
        try:
            if os.path.exists(self.path):
                os.remove(self.path)
        except Exception:
            pass

    @staticmethod
    def make_key(doc_idx: int, para_id: int) -> str:
        return f"{doc_idx}:{para_id}"
