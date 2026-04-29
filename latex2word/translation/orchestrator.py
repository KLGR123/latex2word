from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple

from ..progress import ProgressCallback
from .batching import make_batches
from .checkpoint import CheckpointManager
from .chunk_preprocessor import preprocess_chunk, strip_cjk_ascii_spaces
from .prompts import make_system
from .providers import BaseProvider
from .section_cache import SECTION_TITLE_CACHE, try_section_cache
from .syntax import check_syntax_integrity

log = logging.getLogger(__name__)


async def assign_title_translations(
    docs_out: List[Dict],
    provider: BaseProvider,
    sem: asyncio.Semaphore,
) -> None:
    to_translate: List[Tuple[int, str]] = []
    for idx, doc in enumerate(docs_out):
        title_en = (doc.get("title") or "").strip()
        if not title_en:
            doc["title_translation"] = ""
            continue
        cached = SECTION_TITLE_CACHE.get(title_en.lower())
        if cached:
            doc["title_translation"] = cached
        else:
            to_translate.append((idx, title_en))

    if not to_translate:
        return

    titles_en = [title for _, title in to_translate]
    system = make_system(batch=len(titles_en) > 1)
    async with sem:
        translations = await provider.translate_batch(titles_en, system=system)

    for (doc_idx, _), title_zh in zip(to_translate, translations):
        docs_out[doc_idx]["title_translation"] = title_zh


async def translate_all(
    documents: List[Dict],
    provider: BaseProvider,
    sem: asyncio.Semaphore,
    checkpoint: CheckpointManager,
    max_batch_tokens: int = 1500,
    terms_by_chapter: Optional[Dict[int, Dict[str, str]]] = None,
    strip_cjk_spaces: bool = False,
    progress: Optional[ProgressCallback] = None,
) -> List[Dict]:
    docs_out = deepcopy(documents)
    cached = checkpoint.load()
    syntax_error_count = [0]
    total = sum(len(doc["paragraphs"]) for doc in documents)
    done_count = 0
    done_lock = asyncio.Lock()

    if progress is not None:
        progress({
            "stage": "translate",
            "message": "Translation batches prepared",
            "current": 0,
            "total": total,
        })

    async def process_batch(doc_idx: int, batch: List[Dict]) -> None:
        nonlocal done_count
        keys = [CheckpointManager.make_key(doc_idx, para["id"]) for para in batch]
        missing_indices = [idx for idx, key in enumerate(keys) if key not in cached]
        if not missing_indices:
            async with done_lock:
                done_count += len(batch)
            return

        per_para_info: List[Dict[str, Any]] = []
        for idx in missing_indices:
            para = batch[idx]
            text = para["text"]
            section_hit = try_section_cache(text)
            if section_hit is not None:
                per_para_info.append({
                    "status": "cached",
                    "para_idx": idx,
                    "para": para,
                    "result": section_hit,
                })
                continue

            prep = preprocess_chunk(text)
            if prep["mode"] == "skip":
                per_para_info.append({
                    "status": "skip",
                    "para_idx": idx,
                    "para": para,
                    "result": text,
                })
                continue

            per_para_info.append({
                "status": "api",
                "para_idx": idx,
                "para": para,
                "texts": prep["translate_text"],
                "restore_fn": prep["restore_fn"],
                "mode": prep["mode"],
            })

        for info in per_para_info:
            if info["status"] in ("cached", "skip"):
                key = CheckpointManager.make_key(doc_idx, info["para"]["id"])
                await checkpoint.save(key, info["result"])
                cached[key] = info["result"]
                target_para = next(
                    para for para in docs_out[doc_idx]["paragraphs"]
                    if para["id"] == info["para"]["id"]
                )
                target_para["syntax_error"] = False

        api_infos = [info for info in per_para_info if info["status"] == "api"]
        if api_infos:
            flat_texts: List[str] = []
            flat_map: List[Tuple[int, int]] = []
            for info_idx, info in enumerate(api_infos):
                for text_idx, text in enumerate(info["texts"]):
                    flat_texts.append(text)
                    flat_map.append((info_idx, text_idx))

            chapter = docs_out[doc_idx].get("chapter")
            terms = (terms_by_chapter or {}).get(chapter)
            system = make_system(terms=terms, batch=len(flat_texts) > 1)

            log.debug(
                "doc %d — API call: %d text(s) [para ids: %s]",
                doc_idx,
                len(flat_texts),
                [info["para"]["id"] for info in api_infos],
            )

            async with sem:
                flat_translations = await provider.translate_batch(flat_texts, system=system)

            results_by_info: List[List[str]] = [[] for _ in api_infos]
            for (info_idx, _text_idx), translation in zip(flat_map, flat_translations):
                results_by_info[info_idx].append(translation)

            for info, per_para_translations in zip(api_infos, results_by_info):
                final_text = info["restore_fn"](per_para_translations)
                if strip_cjk_spaces and info["mode"] in ("full", "caption_only"):
                    final_text = strip_cjk_ascii_spaces(final_text)

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

                target_para = next(
                    para for para in docs_out[doc_idx]["paragraphs"]
                    if para["id"] == info["para"]["id"]
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
            if progress is not None:
                progress({
                    "stage": "translate",
                    "message": "Translating paragraphs",
                    "current": done_count,
                    "total": total,
                })

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

    total_paras = sum(len(doc["paragraphs"]) for doc in docs_out)
    log.info(
        "Syntax check: %d / %d paragraphs have broken syntax.",
        syntax_error_count[0],
        total_paras,
    )

    for doc_idx, doc in enumerate(docs_out):
        for para in doc["paragraphs"]:
            key = CheckpointManager.make_key(doc_idx, para["id"])
            para["translation"] = cached.get(key, "")

    log.info("Computing title_translation for all documents...")
    if progress is not None:
        progress({
            "stage": "translate",
            "message": "Computing title translations",
            "current": total,
            "total": total,
        })
    await assign_title_translations(docs_out, provider, sem)
    return docs_out
