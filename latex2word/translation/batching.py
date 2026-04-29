from __future__ import annotations

from typing import Dict, List


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def make_batches(paragraphs: List[Dict], max_batch_tokens: int = 1500) -> List[List[Dict]]:
    batches: List[List[Dict]] = []
    current: List[Dict] = []
    current_tokens = 0
    for para in paragraphs:
        tokens = estimate_tokens(para["text"])
        if current and current_tokens + tokens > max_batch_tokens:
            batches.append(current)
            current = []
            current_tokens = 0
        current.append(para)
        current_tokens += tokens
    if current:
        batches.append(current)
    return batches
