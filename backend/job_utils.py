from __future__ import annotations

import json
import os
import shutil
import tarfile
import zipfile
from datetime import datetime, timezone
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from .model_catalog import validate_provider_and_model


MAX_PAPERS = 10
MAX_ZIP_BYTES = 256 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 12000


class JobError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def append_event(job_dir: Path, event: Dict[str, Any]) -> None:
    event = dict(event)
    event.setdefault("time", utc_now())
    events_path = job_dir / "events.ndjson"
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def update_status(job_dir: Path, **updates: Any) -> Dict[str, Any]:
    status_path = job_dir / "status.json"
    status = read_json(status_path, default={}) or {}
    status.update(updates)
    status["updated_at"] = utc_now()
    write_json(status_path, status)
    return status


def load_events(job_dir: Path, after: int = 0) -> List[Dict[str, Any]]:
    events_path = job_dir / "events.ndjson"
    if not events_path.exists():
        return []
    events: List[Dict[str, Any]] = []
    with events_path.open("r", encoding="utf-8") as handle:
        for index, line in enumerate(handle):
            if index < after:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            event["offset"] = index + 1
            events.append(event)
    return events


def normalize_chapters(papers: List[Dict[str, Any]]) -> List[int]:
    if len(papers) > MAX_PAPERS:
        raise JobError(f"最多支持 {MAX_PAPERS} 篇 paper。")

    used = set()
    chapters: List[int] = []
    for index, paper in enumerate(papers):
        raw = paper.get("chapter")
        chapter = None
        if raw not in (None, ""):
            try:
                chapter = int(raw)
            except (TypeError, ValueError) as exc:
                raise JobError("章号必须是 1 到 10 的整数。") from exc
            if chapter < 1 or chapter > MAX_PAPERS:
                raise JobError("章号必须在 1 到 10 之间。")
            if chapter in used:
                raise JobError(f"章号 {chapter} 重复。")

        if chapter is None:
            for candidate in range(1, MAX_PAPERS + 1):
                if candidate not in used:
                    chapter = candidate
                    break

        if chapter is None:
            raise JobError("无法自动分配章号。")
        used.add(chapter)
        chapters.append(chapter)
    return chapters


def validate_rendering(rendering: Any) -> Dict[str, Any]:
    if not isinstance(rendering, dict):
        return {}
    allowed = {
        "fonts": {"body", "heading", "caption", "code", "ascii"},
        "sizes": {"body", "h1", "h2", "h3", "caption", "code"},
        "colors": {"theorem_bg", "code_bg", "table_bg"},
    }
    cleaned: Dict[str, Any] = {}
    for group, keys in allowed.items():
        values = rendering.get(group)
        if not isinstance(values, dict):
            continue
        cleaned[group] = {}
        for key in keys:
            if key not in values:
                continue
            value = values[key]
            if group == "sizes":
                try:
                    number = int(value)
                except (TypeError, ValueError):
                    continue
                cleaned[group][key] = max(6, min(72, number))
            elif group == "colors":
                text = str(value).strip().lstrip("#").upper()
                if len(text) == 6 and all(ch in "0123456789ABCDEF" for ch in text):
                    cleaned[group][key] = text
            else:
                text = str(value).strip()
                if text:
                    cleaned[group][key] = text[:80]
    return cleaned


def validate_translate(translate: Any) -> Dict[str, str]:
    if not isinstance(translate, dict):
        translate = {}
    provider = str(translate.get("provider") or "deepseek").strip()
    model = str(translate.get("model") or "deepseek-chat").strip()
    try:
        return validate_provider_and_model(provider, model)
    except ValueError as exc:
        raise JobError(str(exc)) from exc


def validate_provider_runtime(translate: Dict[str, str]) -> None:
    provider = translate["provider"]
    package_by_provider = {
        "openai": "openai",
        "deepseek": "openai",
        "kimi": "openai",
        "anthropic": "anthropic",
    }
    package = package_by_provider.get(provider)
    if package and find_spec(package) is None:
        raise JobError(
            f"当前 Python 环境缺少 {package} 包，无法使用 provider={provider}。"
            "请先运行 ./install.sh 或 python -m pip install -e ."
        )


def validate_terms(terms: Any) -> Dict[str, Any]:
    if terms is None:
        return {}
    if isinstance(terms, list):
        converted: Dict[str, Any] = {}
        for entry in terms:
            if not isinstance(entry, dict):
                raise JobError("terms 数组中的每一项都必须是对象。")
            chapter = entry.get("chapter")
            values = entry.get("terms")
            if chapter is None or not isinstance(values, dict):
                raise JobError("terms 数组项必须包含 chapter 和 terms。")
            converted[str(chapter)] = {str(key): str(value) for key, value in values.items()}
        return converted
    if not isinstance(terms, dict):
        raise JobError("terms 必须是 JSON 对象。")
    cleaned: Dict[str, Any] = {}
    for chapter, values in terms.items():
        if not isinstance(values, dict):
            raise JobError("terms 的每个章节值都必须是对象。")
        cleaned[str(chapter)] = {str(key): str(value) for key, value in values.items()}
    return cleaned


def _common_root(names: Iterable[str]) -> str | None:
    roots = set()
    for name in names:
        parts = [part for part in name.split("/") if part]
        if not parts:
            continue
        roots.add(parts[0])
    if len(roots) == 1:
        return next(iter(roots))
    return None


def _safe_target(base: Path, relative: str) -> Path:
    target = (base / relative).resolve()
    base_resolved = base.resolve()
    if target != base_resolved and base_resolved not in target.parents:
        raise JobError("zip 包含不安全路径。")
    return target


def is_supported_archive_name(filename: str) -> bool:
    lower = filename.lower()
    return lower.endswith((".zip", ".tar", ".tar.gz", ".tgz"))


def inspect_zip(path: Path) -> Tuple[List[zipfile.ZipInfo], str | None]:
    if path.stat().st_size == 0:
        raise JobError("zip 文件为空。")
    if path.stat().st_size > MAX_ZIP_BYTES:
        raise JobError("zip 文件过大。")
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if not infos:
                raise JobError("zip 文件为空。")
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                raise JobError("zip 文件条目过多。")
            total_size = sum(info.file_size for info in infos)
            if total_size > MAX_UNCOMPRESSED_BYTES:
                raise JobError("zip 解压后体积过大。")
            names = [
                info.filename.replace("\\", "/")
                for info in infos
                if not info.is_dir() and not info.filename.startswith("__MACOSX/")
            ]
            if not names:
                raise JobError("zip 文件为空。")
            if not any(name.lower().endswith(".tex") for name in names):
                raise JobError("zip 中缺少 .tex 文件。")
            return infos, _common_root(names)
    except zipfile.BadZipFile as exc:
        raise JobError("zip 文件损坏或格式不正确。") from exc


def inspect_tar(path: Path) -> Tuple[List[tarfile.TarInfo], str | None]:
    if path.stat().st_size == 0:
        raise JobError("tar 文件为空。")
    if path.stat().st_size > MAX_ZIP_BYTES:
        raise JobError("tar 文件过大。")
    try:
        with tarfile.open(path) as archive:
            infos = archive.getmembers()
            if not infos:
                raise JobError("tar 文件为空。")
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                raise JobError("tar 文件条目过多。")
            total_size = sum(info.size for info in infos if info.isfile())
            if total_size > MAX_UNCOMPRESSED_BYTES:
                raise JobError("tar 解压后体积过大。")
            names = [
                info.name.replace("\\", "/")
                for info in infos
                if info.isfile() and not info.name.startswith("__MACOSX/")
            ]
            if not names:
                raise JobError("tar 文件为空。")
            if not any(name.lower().endswith(".tex") for name in names):
                raise JobError("tar 中缺少 .tex 文件。")
            return infos, _common_root(names)
    except tarfile.TarError as exc:
        raise JobError("tar 文件损坏或格式不正确。") from exc


def extract_zip_to_inputs(zip_path: Path, chapter_dir: Path) -> Dict[str, Any]:
    infos, common_root = inspect_zip(zip_path)
    if chapter_dir.exists():
        shutil.rmtree(chapter_dir)
    chapter_dir.mkdir(parents=True, exist_ok=True)

    extracted = 0
    with zipfile.ZipFile(zip_path) as archive:
        for info in infos:
            name = info.filename.replace("\\", "/")
            if not name or name.startswith("__MACOSX/"):
                continue
            parts = [part for part in name.split("/") if part and part not in {".", ".."}]
            if not parts:
                continue
            if common_root and parts[0] == common_root:
                parts = parts[1:]
            if not parts:
                continue
            relative = "/".join(parts)
            target = _safe_target(chapter_dir, relative)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue

            mode = info.external_attr >> 16
            if (mode & 0o170000) == 0o120000:
                raise JobError("zip 中包含符号链接，已拒绝解压。")

            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination)
            extracted += 1

    tex_files = sorted(str(path.relative_to(chapter_dir)) for path in chapter_dir.glob("*.tex"))
    recursive_tex_files = sorted(str(path.relative_to(chapter_dir)) for path in chapter_dir.rglob("*.tex"))
    references = sorted(
        str(path.relative_to(chapter_dir))
        for pattern in ("*.bib", "*.bbl")
        for path in chapter_dir.glob(pattern)
    )
    if not tex_files:
        raise JobError("解压后章节根目录缺少 .tex 文件。请确认 zip 根目录直接包含 main.tex 或论文 tex 文件。")

    return {
        "file_count": extracted,
        "tex_files": tex_files,
        "recursive_tex_files": recursive_tex_files,
        "references": references,
        "stripped_root": common_root,
    }


def extract_tar_to_inputs(tar_path: Path, chapter_dir: Path) -> Dict[str, Any]:
    infos, common_root = inspect_tar(tar_path)
    if chapter_dir.exists():
        shutil.rmtree(chapter_dir)
    chapter_dir.mkdir(parents=True, exist_ok=True)

    extracted = 0
    with tarfile.open(tar_path) as archive:
        for info in infos:
            name = info.name.replace("\\", "/")
            if not name or name.startswith("__MACOSX/"):
                continue
            parts = [part for part in name.split("/") if part and part not in {".", ".."}]
            if not parts:
                continue
            if common_root and parts[0] == common_root:
                parts = parts[1:]
            if not parts:
                continue
            relative = "/".join(parts)
            target = _safe_target(chapter_dir, relative)
            if info.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if info.issym() or info.islnk() or not info.isfile():
                raise JobError("tar 中包含不支持的链接或特殊文件，已拒绝解压。")

            source = archive.extractfile(info)
            if source is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination)
            extracted += 1

    return _summarize_extracted(chapter_dir, extracted, common_root)


def extract_archive_to_inputs(archive_path: Path, chapter_dir: Path) -> Dict[str, Any]:
    lower = archive_path.name.lower()
    if lower.endswith(".zip"):
        return extract_zip_to_inputs(archive_path, chapter_dir)
    if lower.endswith((".tar", ".tar.gz", ".tgz")):
        return extract_tar_to_inputs(archive_path, chapter_dir)
    raise JobError("只支持 .zip、.tar、.tar.gz 或 .tgz 文件。")


def _summarize_extracted(chapter_dir: Path, extracted: int, common_root: str | None) -> Dict[str, Any]:
    tex_files = sorted(str(path.relative_to(chapter_dir)) for path in chapter_dir.glob("*.tex"))
    recursive_tex_files = sorted(str(path.relative_to(chapter_dir)) for path in chapter_dir.rglob("*.tex"))
    references = sorted(
        str(path.relative_to(chapter_dir))
        for pattern in ("*.bib", "*.bbl")
        for path in chapter_dir.glob(pattern)
    )
    if not tex_files:
        raise JobError("解压后章节根目录缺少 .tex 文件。请确认压缩包根目录直接包含 main.tex 或论文 tex 文件。")

    return {
        "file_count": extracted,
        "tex_files": tex_files,
        "recursive_tex_files": recursive_tex_files,
        "references": references,
        "stripped_root": common_root,
    }
