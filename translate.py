#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
translate.py

Async concurrent LLM translation of chunked LaTeX paragraphs.

Reads the JSON produced by chunk.py, translates each paragraph to Chinese
using an LLM API (Anthropic / OpenAI / DeepSeek), and writes an enriched
JSON with an added "translation" field on each paragraph.

Features
--------
- Fixed concurrency via asyncio.Semaphore (never more than N in-flight)
- Short chunk auto-batching: adjacent small paragraphs are merged into one
  API call to reduce overhead and cost
- Exponential backoff + jitter retry on rate-limit / transient errors
- Checkpoint / resume: results saved incrementally; interrupted runs pick
  up from where they left off
- Environment block handling: only \\caption{} inside \\begin{} blocks is
  translated; the rest of the environment is preserved verbatim
- Section title local cache: common section names (Introduction, etc.) are
  translated without an API call
- Chapter-scoped glossary: optional --terms JSON injects per-chapter
  terminology into the system prompt

Supported providers
-------------------
  anthropic   ->  uses anthropic.AsyncAnthropic (ANTHROPIC_API_KEY)
  openai      ->  uses openai.AsyncOpenAI       (OPENAI_API_KEY)
  deepseek    ->  uses openai.AsyncOpenAI with DeepSeek base URL
                  (DEEPSEEK_API_KEY, or --base-url + --api-key)

Input JSON schema (from chunk.py)
----------------------------------
{
  "documents": [
    {
      "tex": "path/to/file.tex",
      "chapter": 7,
      "paragraphs": [{"id": 1, "text": "..."}, ...]
    }
  ]
}

Output JSON schema
------------------
Same structure, with "translation" added to every paragraph dict and
"title_translation" added to every document dict.

Usage
-----
  python3 translate.py --input chunks.json --output translated.json \\
      --provider anthropic --model claude-sonnet-4-6 --concurrency 10

  python3 translate.py --input chunks.json --output translated.json \\
      --provider openai --model gpt-4o --concurrency 10

  python3 translate.py --input chunks.json --output translated.json \\
      --provider deepseek --model deepseek-chat --concurrency 15

  # with chapter-scoped glossary
  python3 translate.py --input chunks.json --output translated.json \\
      --provider anthropic --model claude-sonnet-4-6 --terms terms.json

  # resume an interrupted run (checkpoint is loaded automatically)
  python3 translate.py --input chunks.json --output translated.json \\
      --provider anthropic --model claude-sonnet-4-6 --concurrency 10
"""

import argparse
import asyncio
import json
import logging
import os
import random
import re
import sys
from abc import ABC, abstractmethod
from copy import deepcopy
from typing import Any, Callable, Dict, List, Optional, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# System prompt construction
# ---------------------------------------------------------------------------

_SYSTEM_BASE = (
    "你是一位专业的学术翻译，负责将英文 LaTeX 论文段落翻译为中文。"
    "翻译时须严格遵守以下规则：\n"
    "1. 保留所有 LaTeX 命令原样不变，包括 \\textbf{}、\\cite{}、\\label{}、"
    "数学公式（$...$、\\[...\\] 等），仅翻译命令中或命令之间的自然语言文本；\n"
    "2. 仅将文本中的英文标点替换为对应中文全角标点：逗号用（，）、句号用（。）、"
    "分号用（；）、括号用（（））、引号用（""）；\n"
    "3. LaTeX 命令内部（如公式内容、命令参数中的变量名）的符号不得修改，LaTex 命令内部的标点符号也不得修改，例如 \\citep{} 或 $...$ 中的英文逗号；\n"
    "4. 直接输出译文，不要添加任何解释、前缀或总结。"
)

_TERMS_PREFIX = (
    "\n\n以下是本章节的专业术语对照表，翻译时请严格参照使用：\n"
)
_TERMS_SUFFIX = (
    "\n表中未出现的专业术语、算法名称、系统名称或英文缩写，请保留原文不变。"
)

_BATCH_SUFFIX = (
    "\n\n用户会一次发送多段文本，每段以 %%SEP_N%% 开头（N 为编号）。"
    "请按相同格式逐段返回译文：%%SEP_N%%\\n译文，每段之间用空行分隔，不要输出原文。"
)


def _make_system(
    terms: Optional[Dict[str, str]] = None,
    batch: bool = False,
) -> str:
    """Build a system prompt, optionally injecting a glossary and batch instructions."""
    s = _SYSTEM_BASE
    if terms:
        items = "、".join(f"{k} → {v}" for k, v in terms.items())
        s += _TERMS_PREFIX + items + _TERMS_SUFFIX
    if batch:
        s += _BATCH_SUFFIX
    return s


# ---------------------------------------------------------------------------
# Batch prompt building and parsing
# ---------------------------------------------------------------------------

# Use %%SEP_N%% as separator — this pattern never appears in LaTeX source.
_SEP_PATTERN = re.compile(r"%%SEP_(\d+)%%")


def _build_batch_prompt(texts: List[str]) -> str:
    parts = [f"%%SEP_{i}%%\n{t}" for i, t in enumerate(texts, 1)]
    return "\n\n".join(parts)


def _parse_batch_response(response: str, expected: int) -> List[str]:
    """
    Parse a %%SEP_N%%-delimited batch response back into individual translations.
    Falls back to blank-line splitting when markers are absent or mismatched.
    """
    pieces = _SEP_PATTERN.split(response.strip())
    # pieces: ['prefix', '1', 'text1', '2', 'text2', ...]
    results: Dict[int, str] = {}
    i = 1
    while i + 1 < len(pieces):
        try:
            idx = int(pieces[i])
            text = pieces[i + 1].strip()
            results[idx] = text
        except (ValueError, IndexError):
            pass
        i += 2

    if len(results) == expected:
        return [results[k] for k in sorted(results)]

    # Fallback: split on blank lines
    fallback = [p.strip() for p in re.split(r"\n\s*\n", response.strip()) if p.strip()]
    if len(fallback) == expected:
        log.debug("Batch parse used fallback blank-line split.")
        return fallback

    log.warning(
        "Batch parse mismatch: expected %d segments, got %d. Padding.", expected, len(fallback)
    )
    while len(fallback) < expected:
        fallback.append("")
    return fallback[:expected]


# ---------------------------------------------------------------------------
# Section title local cache (avoids API calls for trivial headings)
# ---------------------------------------------------------------------------

# Maps lowercase English section title -> Chinese translation
SECTION_TITLE_CACHE: Dict[str, str] = {
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

# Matches \section*{Title}\label{...}  (label part optional, case-insensitive cmd)
_SECTION_RE = re.compile(
    r"^(\\(?:part|chapter|section|subsection|subsubsection)\*?)\{([^}]+)\}"
    r"(\s*\\label\{[^}]*\})?$",
    re.IGNORECASE,
)

def try_section_cache(text: str) -> Optional[str]:
    """
    If text is a bare section heading whose title exists in the local cache,
    return the ready-made Chinese translation (preserving cmd and label).
    Returns None when not applicable.
    """
    m = _SECTION_RE.match(text.strip())
    if not m:
        return None
    cmd, title, label = m.group(1), m.group(2).strip(), m.group(3) or ""
    translated = SECTION_TITLE_CACHE.get(title.lower())
    if translated:
        return f"{cmd}{{{translated}}}{label}"
    return None


async def _assign_title_translations(
    docs_out: List[Dict],
    provider: "BaseProvider",
    sem: asyncio.Semaphore,
) -> None:
    """
    Translate the 'title' field of each document and store the result as
    'title_translation' on the same document object.

    Titles found in SECTION_TITLE_CACHE are resolved without an API call;
    the remainder are translated in a single batched request.
    Documents without a 'title' field get an empty string.
    """
    # Pair each doc index with its English title (skip missing/empty).
    to_translate: List[Tuple[int, str]] = []
    for i, doc in enumerate(docs_out):
        title_en = (doc.get("title") or "").strip()
        if not title_en:
            doc["title_translation"] = ""
            continue
        cached = SECTION_TITLE_CACHE.get(title_en.lower())
        if cached:
            doc["title_translation"] = cached
        else:
            to_translate.append((i, title_en))

    if not to_translate:
        return

    titles_en = [t for _, t in to_translate]
    system = _make_system(batch=len(titles_en) > 1)
    async with sem:
        translations = await provider.translate_batch(titles_en, system=system)

    for (doc_idx, _), title_zh in zip(to_translate, translations):
        docs_out[doc_idx]["title_translation"] = title_zh


# ---------------------------------------------------------------------------
# Brace-depth tracking
# ---------------------------------------------------------------------------

def _find_matching_brace(text: str, open_pos: int) -> int:
    """
    Given the position of '{' in text, return the position AFTER the matching '}'.
    Returns -1 if the brace is never closed.
    Handles arbitrary nesting depth and skips escaped characters.
    """
    assert text[open_pos] == "{"
    depth = 1
    i = open_pos + 1
    n = len(text)
    while i < n and depth > 0:
        c = text[i]
        if c == "\\":
            i += 2  # skip backslash + next char (e.g. \{ \} \\)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    return i if depth == 0 else -1


_CAPTION_CMD_RE = re.compile(r"\\caption\*?")


def extract_captions(text: str) -> List[Tuple[int, int]]:
    """
    Find all \\caption{...} and \\caption[short]{long} occurrences in text.

    Returns a list of (content_start, content_end) pairs representing the
    span of text INSIDE the outer braces (exclusive), suitable for slicing:
        text[content_start:content_end]
    Handles arbitrary brace nesting depth and optional [short] arguments.
    """
    results: List[Tuple[int, int]] = []
    n = len(text)
    for m in _CAPTION_CMD_RE.finditer(text):
        j = m.end()
        # Skip optional [short caption]
        if j < n and text[j] == "[":
            j += 1
            while j < n and text[j] != "]":
                j += 1
            j += 1  # skip ']'
        # Skip whitespace between \caption and {
        while j < n and text[j] in " \t\n":
            j += 1
        if j >= n or text[j] != "{":
            continue
        end = _find_matching_brace(text, j)
        if end == -1:
            continue
        # content is between the outer braces
        results.append((j + 1, end - 1))
    return results


# ---------------------------------------------------------------------------
# Chunk preprocessing: environment blocks vs. normal paragraphs
# ---------------------------------------------------------------------------

# Environments whose content should NOT be translated.
# Anything \begin{xxx} where xxx is NOT in this set will be fully translated.
_SKIP_ENVS: frozenset = frozenset({
    # Math
    "equation", "equation*",
    "align", "align*", "aligned", "alignat", "alignat*",
    "gather", "gather*", "gathered",
    "multline", "multline*",
    "flalign", "flalign*",
    "split",
    "cases", "cases*", "dcases", "dcases*", "rcases", "rcases*",
    "math", "displaymath",
    "subequations",
    # Figures
    "figure", "figure*",
    "subfigure", "subfloat",
    "wrapfigure", "SCfigure",
    "minipage",
    "center",
    "tikzpicture", "picture", "pspicture",
    "floatrow", "ffigbox",
    # Tables
    "table", "table*",
    "tabular", "tabular*",
    "tabularx", "tabulary", "tabularray",
    "longtable", "supertabular", "xtab",
    "array", "tabbing",
    "sidewaystable", "subtable",
    # Code / verbatim
    "verbatim", "verbatim*",
    "alltt",
    "lstlisting",
    "minted",
    "tcolorbox",
    "mdframed",
    # Algorithms
    "algorithm", "algorithm*",
    "algorithmic", "algorithmicx",
    "algorithm2e",
    "algpseudocode",
})

# Matches the environment name from \begin{name} or \begin{name*}
_BEGIN_ENV_RE = re.compile(r"^\\begin\{([^}]+)\}")

def preprocess_chunk(text: str) -> Dict[str, Any]:
    stripped = text.lstrip()
    if not stripped.startswith(r"\begin{"):
        # Normal paragraph — translate everything.
        return {
            "mode": "full",
            "translate_text": [text],
            "restore_fn": lambda translations: translations[0],
        }

    # Identify the environment name.
    m = _BEGIN_ENV_RE.match(stripped)
    env_name = m.group(1).strip() if m else ""

    if env_name not in _SKIP_ENVS:
        # Text-content environment (abstract, theorem, proof, lemma, etc.)
        # — translate the whole chunk just like a normal paragraph.
        return {
            "mode": "full",
            "translate_text": [text],
            "restore_fn": lambda translations: translations[0],
        }

    # Environment is in the skip set (math / figure / table / code / algorithm).
    # Only \caption{} contents need translation; everything else is kept verbatim.
    caption_spans = extract_captions(text)
    if not caption_spans:
        return {
            "mode": "skip",
            "translate_text": [],
            "restore_fn": lambda _: text,
        }

    caption_contents = [text[s:e] for s, e in caption_spans]

    def restore(translations: List[str]) -> str:
        result = text
        for (start, end), translated in zip(
            reversed(caption_spans), reversed(translations)
        ):
            result = result[:start] + translated + result[end:]
        return result

    return {
        "mode": "caption_only",
        "translate_text": caption_contents,
        "restore_fn": restore,
    }


def strip_cjk_ascii_spaces(text: str) -> str:
    CJK = r'[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]'
    ASCII_CHAR = r'[A-Za-z0-9]'
    text = re.sub(rf'({CJK})\s+({ASCII_CHAR})', r'\1\2', text)
    text = re.sub(rf'({ASCII_CHAR})\s+({CJK})',  r'\1\2', text)
    return text


# ---------------------------------------------------------------------------
# Post-translation syntax integrity check
# ---------------------------------------------------------------------------

# Patterns for LaTeX constructs that must survive translation unchanged.
_SYNTAX_PATTERNS: Dict[str, re.Pattern] = {
    # \cite{...} \citep{...} \citet{...} etc.
    "cite":    re.compile(r"\\cite[a-zA-Z]*\*?\{[^}]+\}"),
    # Inline math $...$ and $$...$$
    "inline_math": re.compile(r"\$\$?.+?\$\$?", re.DOTALL),
    # Display math \[...\]
    "display_math": re.compile(r"\\\[.+?\\\]", re.DOTALL),
    # Math environments \begin{equation} etc.
    "math_env": re.compile(
        r"\\begin\{(?:equation|align|gather|multline|flalign|alignat)"
        r"\*?\}.*?\\end\{(?:equation|align|gather|multline|flalign|alignat)\*?\}",
        re.DOTALL,
    ),
    # \ref \eqref \autoref \cref \Cref \hyperref etc.
    "ref":     re.compile(r"\\(?:ref|eqref|autoref|cref|Cref|hyperref|pageref)\*?\{[^}]+\}"),
    # \label{...}
    "label":   re.compile(r"\\label\{[^}]+\}"),
    # \footnote{...}
    "footnote": re.compile(r"\\footnote\{"),
}


def _extract_syntax_tokens(text: str) -> Dict[str, List[str]]:
    """
    Extract all occurrences of protected LaTeX syntax from text.
    Returns {category: [matched_string, ...]} for categories with at least
    one match.
    """
    result: Dict[str, List[str]] = {}
    for name, pat in _SYNTAX_PATTERNS.items():
        matches = pat.findall(text)
        if matches:
            result[name] = matches
    return result


def _normalize_token(token: str) -> str:
    """Collapse all internal whitespace for comparison purposes."""
    return re.sub(r'\s+', ' ', token).strip()


def check_syntax_integrity(
    original: str,
    translated: str,
) -> Tuple[bool, List[str]]:
    """
    Compare protected syntax tokens between original and translated text.

    Returns
    -------
    (ok, missing)
        ok      -- True if every token present in original also appears in translated.
        missing -- List of tokens that disappeared (empty when ok is True).
    """
    before = _extract_syntax_tokens(original)
    after  = _extract_syntax_tokens(translated)
    missing: List[str] = []

    for category, tokens in before.items():
        after_tokens_set = {_normalize_token(t) for t in after.get(category, [])}
        for token in set(tokens):
            if _normalize_token(token) not in after_tokens_set:
                missing.append(token)

    return (len(missing) == 0), missing


# ---------------------------------------------------------------------------
# Provider abstraction
# ---------------------------------------------------------------------------

class BaseProvider(ABC):
    """Translate a flat list of texts in a single API call."""

    MAX_RETRIES = 5

    async def translate_batch(
        self,
        texts: List[str],
        system: Optional[str] = None,
    ) -> List[str]:
        """
        Translate *texts* in one API call (or a single call for one text).
        *system* overrides the default system prompt when provided.
        """
        if len(texts) == 1:
            sys_prompt = system if system is not None else _make_system(batch=False)
            result = await self._call_with_retry(sys_prompt, texts[0])
            return [result]

        sys_prompt = system if system is not None else _make_system(batch=True)
        prompt = _build_batch_prompt(texts)
        response = await self._call_with_retry(sys_prompt, prompt)
        return _parse_batch_response(response, len(texts))

    async def _call_with_retry(self, system: str, user: str) -> str:
        for attempt in range(self.MAX_RETRIES):
            try:
                return await self._call_api(system, user)
            except Exception as exc:
                if not self._is_retryable(exc):
                    raise
                wait = (2 ** attempt) + random.random()
                log.warning(
                    "Retryable error (attempt %d/%d): %s — waiting %.1fs",
                    attempt + 1,
                    self.MAX_RETRIES,
                    exc,
                    wait,
                )
                await asyncio.sleep(wait)
        # Final attempt — let the exception propagate.
        return await self._call_api(system, user)

    @abstractmethod
    async def _call_api(self, system: str, user: str) -> str: ...

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        name = type(exc).__name__
        msg = str(exc)
        retryable_names = {
            "RateLimitError", "APIStatusError", "APIConnectionError",
            "APITimeoutError", "InternalServerError", "ServiceUnavailableError",
            "Timeout", "ConnectionError",
        }
        if name in retryable_names:
            return True
        for code in ("429", "500", "502", "503", "504", "529"):
            if code in msg:
                return True
        return False


class AnthropicProvider(BaseProvider):
    def __init__(self, model: str, api_key: Optional[str] = None):
        try:
            from anthropic import AsyncAnthropic
        except ImportError:
            sys.exit("[ERROR] anthropic package not installed. Run: pip install anthropic")
        self.client = AsyncAnthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))
        self.model = model

    async def _call_api(self, system: str, user: str) -> str:
        msg = await self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return msg.content[0].text


class OpenAICompatibleProvider(BaseProvider):
    def __init__(
        self,
        model: str,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        provider_name: str = "openai",
    ):
        try:
            from openai import AsyncOpenAI
        except ImportError:
            sys.exit("[ERROR] openai package not installed. Run: pip install openai")

        if api_key is None:
            env_key = {
                "openai": "OPENAI_API_KEY",
                "deepseek": "DEEPSEEK_API_KEY",
            }.get(provider_name, "OPENAI_API_KEY")
            api_key = os.environ.get(env_key)

        kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = AsyncOpenAI(**kwargs)
        self.model = model

    async def _call_api(self, system: str, user: str) -> str:
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=4096,
        )
        return resp.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# Batching: merge short chunks into single API calls
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English text."""
    return max(1, len(text) // 4)


def make_batches(
    paragraphs: List[Dict],
    max_batch_tokens: int = 1500,
) -> List[List[Dict]]:
    """
    Group adjacent paragraphs into batches so the total estimated token
    count stays under max_batch_tokens.  A single paragraph that exceeds
    the limit is its own batch.
    """
    batches: List[List[Dict]] = []
    current: List[Dict] = []
    current_tokens = 0

    for para in paragraphs:
        tok = estimate_tokens(para["text"])
        if current and current_tokens + tok > max_batch_tokens:
            batches.append(current)
            current = []
            current_tokens = 0
        current.append(para)
        current_tokens += tok

    if current:
        batches.append(current)

    return batches


# ---------------------------------------------------------------------------
# Checkpoint manager
# ---------------------------------------------------------------------------

class CheckpointManager:
    """
    Persists translation results incrementally to a JSON file so that an
    interrupted run can resume from where it left off.

    Key format: "{doc_index}:{para_id}"
    """

    def __init__(self, path: str):
        self.path = path
        self._lock = asyncio.Lock()
        self._data: Dict[str, str] = {}

    def load(self) -> Dict[str, str]:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
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
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=2)
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


# ---------------------------------------------------------------------------
# Core translation orchestrator
# ---------------------------------------------------------------------------

async def translate_all(
    documents: List[Dict],
    provider: BaseProvider,
    sem: asyncio.Semaphore,
    checkpoint: CheckpointManager,
    max_batch_tokens: int = 1500,
    terms_by_chapter: Optional[Dict[int, Dict[str, str]]] = None,
    strip_cjk_spaces: bool = False,
) -> List[Dict]:
    """
    Translate all paragraphs across all documents concurrently.
    Returns a deep copy of `documents` with "translation" added to each
    paragraph and "title_translation" added to each document.
    """
    docs_out = deepcopy(documents)
    cached = checkpoint.load()
    syntax_error_count = [0]   # mutable counter accessible inside process_batch closure

    total = sum(len(d["paragraphs"]) for d in documents)
    done_count = 0
    done_lock = asyncio.Lock()

    async def process_batch(doc_idx: int, batch: List[Dict]) -> None:
        nonlocal done_count

        keys = [CheckpointManager.make_key(doc_idx, p["id"]) for p in batch]

        # --- Phase 1: skip paragraphs already present in checkpoint ---
        missing_indices = [i for i, k in enumerate(keys) if k not in cached]
        if not missing_indices:
            async with done_lock:
                done_count += len(batch)
            return

        # --- Phase 2: section cache & chunk preprocessing ---
        #
        # For each missing paragraph we decide on one of three outcomes:
        #   "cached"  -- answered by section title local cache; no API needed
        #   "skip"    -- \begin{} block with no \caption{}; keep original text
        #   "api"     -- needs API translation; carries texts + restore_fn
        per_para_info: List[Dict[str, Any]] = []

        for idx in missing_indices:
            para = batch[idx]
            text = para["text"]

            # 2a. Section title local cache
            section_hit = try_section_cache(text)
            if section_hit is not None:
                per_para_info.append({
                    "status": "cached",
                    "para_idx": idx,
                    "para": para,
                    "result": section_hit,
                })
                continue

            # 2b. Preprocess: environment block vs. normal paragraph
            prep = preprocess_chunk(text)

            if prep["mode"] == "skip":
                # Environment with no caption — nothing to translate
                per_para_info.append({
                    "status": "skip",
                    "para_idx": idx,
                    "para": para,
                    "result": text,
                })
                continue

            # mode == "full" or "caption_only" — needs API
            per_para_info.append({
                "status": "api",
                "para_idx": idx,
                "para": para,
                "texts": prep["translate_text"],     # List[str]
                "restore_fn": prep["restore_fn"],    # Callable[[List[str]], str]
                "mode": prep["mode"],
            })

        # --- Phase 3: persist section-cache / skip results immediately ---
        for info in per_para_info:
            if info["status"] in ("cached", "skip"):
                key = CheckpointManager.make_key(doc_idx, info["para"]["id"])
                await checkpoint.save(key, info["result"])
                cached[key] = info["result"]
                # These paths bypass the API; syntax is preserved by construction.
                target_para = next(
                    p for p in docs_out[doc_idx]["paragraphs"]
                    if p["id"] == info["para"]["id"]
                )
                target_para["syntax_error"] = False

        # --- Phase 4: flatten all API-needed texts into one list ---
        api_infos = [info for info in per_para_info if info["status"] == "api"]

        if api_infos:
            flat_texts: List[str] = []
            # flat_map[i] = (api_info_index, text_index_within_that_para)
            flat_map: List[Tuple[int, int]] = []
            for i, info in enumerate(api_infos):
                for j, t in enumerate(info["texts"]):
                    flat_texts.append(t)
                    flat_map.append((i, j))

            # Build chapter-scoped system prompt
            chapter = docs_out[doc_idx].get("chapter")
            terms = (terms_by_chapter or {}).get(chapter)
            system = _make_system(terms=terms, batch=len(flat_texts) > 1)

            log.debug(
                "doc %d — API call: %d text(s) [para ids: %s]",
                doc_idx,
                len(flat_texts),
                [info["para"]["id"] for info in api_infos],
            )

            async with sem:
                flat_translations = await provider.translate_batch(flat_texts, system=system)

            # Scatter flat translations back into per-para buckets
            results_by_info: List[List[str]] = [[] for _ in api_infos]
            for (info_idx, _text_idx), translation in zip(flat_map, flat_translations):
                results_by_info[info_idx].append(translation)

            # Restore each paragraph and persist to checkpoint
            for info, per_para_translations in zip(api_infos, results_by_info):
                final_text = info["restore_fn"](per_para_translations)
                if strip_cjk_spaces and info["mode"] in ("full", "caption_only"):
                    final_text = strip_cjk_ascii_spaces(final_text)

                # Syntax integrity check against the original source text.
                original_text = info["para"]["text"]
                ok, missing = check_syntax_integrity(original_text, final_text)
                if not ok:
                    syntax_error_count[0] += 1
                    log.warning(
                        "Syntax broken — doc %d para %d, missing: %s",
                        doc_idx,
                        info["para"]["id"],
                        missing,
                    )

                # Write syntax_error flag directly onto the output paragraph object.
                target_para = next(
                    p for p in docs_out[doc_idx]["paragraphs"]
                    if p["id"] == info["para"]["id"]
                )
                target_para["syntax_error"] = not ok
                if not ok:
                    target_para["syntax_error_tokens"] = missing

                key = CheckpointManager.make_key(doc_idx, info["para"]["id"])
                await checkpoint.save(key, final_text)
                cached[key] = final_text

        async with done_lock:
            done_count += len(batch)
            log.info("Progress: %d / %d paragraphs translated", done_count, total)

    # Build tasks for every batch across all documents
    tasks = []
    for doc_idx, doc in enumerate(docs_out):
        batches = make_batches(doc["paragraphs"], max_batch_tokens)
        log.info(
            "doc %d (%s): %d paragraphs -> %d batches",
            doc_idx,
            doc.get("tex", ""),
            len(doc["paragraphs"]),
            len(batches),
        )
        for batch in batches:
            tasks.append(process_batch(doc_idx, batch))

    await asyncio.gather(*tasks, return_exceptions=False)

    total_paras = sum(len(d["paragraphs"]) for d in docs_out)
    log.info(
        "Syntax check: %d / %d paragraphs have broken syntax.",
        syntax_error_count[0],
        total_paras,
    )

    # Merge cached translations into output paragraphs
    for doc_idx, doc in enumerate(docs_out):
        for para in doc["paragraphs"]:
            key = CheckpointManager.make_key(doc_idx, para["id"])
            para["translation"] = cached.get(key, "")

    # Translate the 'title' field of each document; result stored as 'title_translation'.
    log.info("Computing title_translation for all documents...")
    await _assign_title_translations(docs_out, provider, sem)

    return docs_out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_provider(args: argparse.Namespace) -> BaseProvider:
    provider_name = args.provider.lower()
    model = args.model
    api_key = args.api_key or None

    if provider_name == "anthropic":
        return AnthropicProvider(model=model, api_key=api_key)

    if provider_name in ("openai", "deepseek"):
        base_url = args.base_url
        if provider_name == "deepseek" and not base_url:
            base_url = "https://api.deepseek.com"
        return OpenAICompatibleProvider(
            model=model,
            api_key=api_key,
            base_url=base_url,
            provider_name=provider_name,
        )

    sys.exit(f"[ERROR] Unknown provider: {provider_name!r}. Choose: anthropic, openai, deepseek")


def load_terms(path: str) -> Dict[int, Dict[str, str]]:
    """Load a chapter-keyed glossary JSON and return {chapter_int: {en: zh, ...}}."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        sys.exit(f"[ERROR] Terms file not found: {path}")
    except json.JSONDecodeError as exc:
        sys.exit(f"[ERROR] Invalid JSON in terms file {path}: {exc}")

    result: Dict[int, Dict[str, str]] = {}
    for entry in raw:
        chapter = entry.get("chapter")
        terms = entry.get("terms", {})
        if chapter is not None and isinstance(terms, dict):
            result[int(chapter)] = terms
        else:
            log.warning("Skipping malformed terms entry: %s", entry)
    return result


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Async concurrent LLM translation of LaTeX chunks JSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--input", required=True, help="Input chunks JSON (from chunk.py)")
    ap.add_argument("--output", required=True, help="Output translated JSON")
    ap.add_argument(
        "--provider",
        required=True,
        choices=["anthropic", "openai", "deepseek"],
        help="LLM provider",
    )
    ap.add_argument("--model", required=True, help="Model ID (e.g. claude-sonnet-4-6, gpt-4o)")
    ap.add_argument(
        "--concurrency",
        type=int,
        default=10,
        help="Max concurrent API requests (default: 10)",
    )
    ap.add_argument(
        "--max-batch-tokens",
        type=int,
        default=1500,
        help="Max estimated tokens per batch (default: 1500)",
    )
    ap.add_argument("--api-key", default=None, help="API key (overrides env var)")
    ap.add_argument(
        "--base-url",
        default=None,
        help="Custom base URL (for OpenAI-compatible endpoints)",
    )
    ap.add_argument(
        "--checkpoint",
        default=None,
        help="Checkpoint file path (default: <output>.checkpoint.json)",
    )
    ap.add_argument(
        "--terms",
        default=None,
        metavar="TERMS_JSON",
        help=(
            "Path to a chapter-scoped glossary JSON. "
            "Format: [{\"chapter\": N, \"terms\": {\"En\": \"中文\", ...}}, ...]"
        ),
    )
    ap.add_argument(
        "--strip-cjk-spaces",
        action="store_true",
        default=False,
        help="Remove spaces between CJK characters and ASCII/digits in translated text.",
    )
    ap.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = ap.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # Load input chunks
    try:
        with open(args.input, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        sys.exit(f"[ERROR] Input file not found: {args.input}")
    except json.JSONDecodeError as exc:
        sys.exit(f"[ERROR] Invalid JSON in {args.input}: {exc}")

    documents = data.get("documents", [])
    if not documents:
        sys.exit("[ERROR] No documents found in input JSON.")

    total = sum(len(d.get("paragraphs", [])) for d in documents)
    log.info("Loaded %d document(s), %d paragraph(s) total.", len(documents), total)

    # Load optional glossary
    terms_by_chapter: Optional[Dict[int, Dict[str, str]]] = None
    if args.terms:
        terms_by_chapter = load_terms(args.terms)
        log.info(
            "Loaded glossary for %d chapter(s) from %s",
            len(terms_by_chapter),
            args.terms,
        )

    provider = build_provider(args)
    sem = asyncio.Semaphore(args.concurrency)

    checkpoint_path = args.checkpoint or (args.output + ".checkpoint.json")
    checkpoint = CheckpointManager(checkpoint_path)

    # Run translation
    try:
        docs_out = asyncio.run(
            translate_all(
                documents,
                provider,
                sem,
                checkpoint,
                max_batch_tokens=args.max_batch_tokens,
                terms_by_chapter=terms_by_chapter,
                strip_cjk_spaces=args.strip_cjk_spaces,
            )
        )
    except KeyboardInterrupt:
        log.info("Interrupted. Progress saved to checkpoint: %s", checkpoint_path)
        sys.exit(130)

    # Write output atomically
    out_dir = os.path.dirname(os.path.abspath(args.output)) or "."
    os.makedirs(out_dir, exist_ok=True)
    tmp_out = args.output + ".tmp"
    with open(tmp_out, "w", encoding="utf-8") as f:
        json.dump({"documents": docs_out}, f, ensure_ascii=False, indent=2)
    os.replace(tmp_out, args.output)
    log.info("Wrote translated output to %s", args.output)

    checkpoint.remove()
    log.info("Done. Checkpoint removed.")


if __name__ == "__main__":
    main()