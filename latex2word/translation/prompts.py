from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

SYSTEM_BASE = (
    "你是一位专业的学术翻译，负责将英文 LaTeX 论文段落翻译为中文。"
    "翻译时须严格遵守以下规则：\n"
    "1. 保留所有 LaTeX 命令原样不变，包括 \\textbf{}、\\cite{}、\\label{}、"
    "数学公式（$...$、\\[...\\] 等），仅翻译命令中或命令之间的自然语言文本；\n"
    "2. 仅将文本中的英文标点替换为对应中文全角标点：逗号用（，）、句号用（。）、"
    "分号用（；）、括号用（（））、引号用（""）；\n"
    "3. LaTeX 命令内部（如公式内容、命令参数中的变量名）的符号不得修改，LaTex 命令内部的标点符号也不得修改，例如 \\citep{} 或 $...$ 中的英文逗号；\n"
    "4. 直接输出译文，不要添加任何解释、前缀或总结。"
)

TERMS_PREFIX = "\n\n以下是本章节的专业术语对照表，翻译时请严格参照使用：\n"
TERMS_SUFFIX = "\n表中未出现的专业术语、算法名称、系统名称或英文缩写，请保留原文不变。"
BATCH_SUFFIX = (
    "\n\n用户会一次发送多段文本，每段以 %%SEP_N%% 开头（N 为编号）。"
    "请按相同格式逐段返回译文：%%SEP_N%%\\n译文，每段之间用空行分隔，不要输出原文。"
)


def configure_prompts(config: Dict[str, Any] | None) -> None:
    """Apply prompt text overrides while keeping the default prompt contract."""
    if not isinstance(config, dict):
        return

    global SYSTEM_BASE, TERMS_PREFIX, TERMS_SUFFIX, BATCH_SUFFIX
    if isinstance(config.get("system_base"), str) and config["system_base"].strip():
        SYSTEM_BASE = config["system_base"]
    if isinstance(config.get("terms_prefix"), str):
        TERMS_PREFIX = config["terms_prefix"]
    if isinstance(config.get("terms_suffix"), str):
        TERMS_SUFFIX = config["terms_suffix"]
    if isinstance(config.get("batch_suffix"), str):
        BATCH_SUFFIX = config["batch_suffix"]

SEP_PATTERN = re.compile(r"%%SEP_(\d+)%%")


def make_system(terms: Optional[Dict[str, str]] = None, batch: bool = False) -> str:
    system = SYSTEM_BASE
    if terms:
        items = "、".join(f"{key} → {value}" for key, value in terms.items())
        system += TERMS_PREFIX + items + TERMS_SUFFIX
    if batch:
        system += BATCH_SUFFIX
    return system


def build_batch_prompt(texts: List[str]) -> str:
    return "\n\n".join(f"%%SEP_{idx}%%\n{text}" for idx, text in enumerate(texts, 1))


def parse_batch_response(response: str, expected: int) -> List[str]:
    pieces = SEP_PATTERN.split(response.strip())
    results: Dict[int, str] = {}
    idx = 1
    while idx + 1 < len(pieces):
        try:
            item_idx = int(pieces[idx])
            results[item_idx] = pieces[idx + 1].strip()
        except (ValueError, IndexError):
            pass
        idx += 2

    if len(results) == expected:
        return [results[k] for k in sorted(results)]

    fallback = [p.strip() for p in re.split(r"\n\s*\n", response.strip()) if p.strip()]
    if len(fallback) == expected:
        log.debug("Batch parse used fallback blank-line split.")
        return fallback

    log.warning(
        "Batch parse mismatch: expected %d segments, got %d. Padding.",
        expected,
        len(fallback),
    )
    while len(fallback) < expected:
        fallback.append("")
    return fallback[:expected]
