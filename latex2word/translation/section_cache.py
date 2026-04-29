from __future__ import annotations

import re
from typing import Dict, Optional

DEFAULT_SECTION_TITLE_CACHE: Dict[str, str] = {
    "abstract": "摘要",
    "introduction": "引言",
    "background": "背景",
    "related work": "相关工作",
    "related works": "相关工作",
    "preliminaries": "预备知识",
    "problem formulation": "问题建模",
    "problem statement": "问题描述",
    "methodology": "方法论",
    "method": "方法",
    "methods": "方法",
    "approach": "方法",
    "proposed method": "所提方法",
    "model": "模型",
    "framework": "框架",
    "architecture": "架构",
    "experiment": "实验",
    "experiments": "实验",
    "experimental setup": "实验设置",
    "experimental results": "实验结果",
    "evaluation": "评估",
    "results": "结果",
    "results and discussion": "结果与讨论",
    "analysis": "分析",
    "ablation study": "消融实验",
    "discussion": "讨论",
    "conclusion": "结论",
    "conclusions": "结论",
    "conclusion and future work": "结论与展望",
    "future work": "未来工作",
    "limitations": "局限性",
    "acknowledgement": "致谢",
    "acknowledgements": "致谢",
    "acknowledgment": "致谢",
    "acknowledgments": "致谢",
    "references": "参考文献",
    "appendix": "附录",
    "supplementary material": "补充材料",
    "supplementary": "补充材料",
    "notation": "符号说明",
    "overview": "概述",
}

SECTION_TITLE_CACHE: Dict[str, str] = dict(DEFAULT_SECTION_TITLE_CACHE)


def configure_section_title_cache(cache: Optional[Dict[str, str]]) -> None:
    if not isinstance(cache, dict):
        return
    SECTION_TITLE_CACHE.update({
        str(key).lower(): str(value)
        for key, value in cache.items()
        if key and value
    })

SECTION_RE = re.compile(
    r"^(\\(?:part|chapter|section|subsection|subsubsection)\*?)\{([^}]+)\}"
    r"(\s*\\label\{[^}]*\})?$",
    re.IGNORECASE,
)


def try_section_cache(text: str) -> Optional[str]:
    match = SECTION_RE.match(text.strip())
    if not match:
        return None
    cmd, title, label = match.group(1), match.group(2).strip(), match.group(3) or ""
    translated = SECTION_TITLE_CACHE.get(title.lower())
    if translated:
        return f"{cmd}{{{translated}}}{label}"
    return None
