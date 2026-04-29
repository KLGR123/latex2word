from __future__ import annotations

import json
import mimetypes
import os
import shutil
import subprocess
import sys
import time
import uuid
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.parse import parse_qs, urlparse

from .job_utils import (
    JobError,
    append_event,
    extract_archive_to_inputs,
    is_supported_archive_name,
    load_events,
    normalize_chapters,
    read_json,
    update_status,
    utc_now,
    validate_rendering,
    validate_provider_runtime,
    validate_terms,
    validate_translate,
    write_json,
)
from .model_catalog import get_catalog_payload


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = PROJECT_ROOT / "frontend"
ASSETS_DIR = PROJECT_ROOT / "assets"
JOBS_DIR = PROJECT_ROOT / ".latex2word_jobs"


def json_response(handler: BaseHTTPRequestHandler, status: int, data: Any) -> None:
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(raw)


def text_response(handler: BaseHTTPRequestHandler, status: int, text: str) -> None:
    raw = text.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(raw)


def safe_name(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)
    return cleaned[:120] or "paper.zip"


def _prepare_request(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[int]]:
    papers = payload.get("papers")
    if not isinstance(papers, list) or not papers:
        raise JobError("至少需要上传一篇 paper。")

    chapters = normalize_chapters(papers)
    translate = validate_translate(payload.get("translate"))
    validate_provider_runtime(translate)

    request = {
        "created_at": utc_now(),
        "papers": [],
        "translate": translate,
        "rendering": validate_rendering(payload.get("rendering")),
        "terms": validate_terms(payload.get("terms")),
    }
    return request, papers, chapters


def _create_job_workspace(created_at: str) -> Tuple[str, Path, Path, Path]:
    job_id = uuid.uuid4().hex
    job_dir = JOBS_DIR / job_id
    uploads_dir = job_dir / "uploads"
    inputs_dir = job_dir / "inputs"
    uploads_dir.mkdir(parents=True, exist_ok=False)
    inputs_dir.mkdir(parents=True, exist_ok=True)

    update_status(
        job_dir,
        id=job_id,
        state="preparing",
        stage="upload",
        message="Preparing uploaded papers",
        percent=0,
        created_at=created_at,
    )
    return job_id, job_dir, uploads_dir, inputs_dir


def _finalize_job_request(
    request: Dict[str, Any],
    papers: List[Dict[str, Any]],
    chapters: List[int],
    uploads_dir: Path,
    inputs_dir: Path,
    file_records: List[Tuple[str, Path]],
    *,
    copy_files: bool,
) -> Dict[str, Any]:
    if len(file_records) != len(papers):
        raise JobError("上传文件数量和 papers 元数据数量不一致。")

    for index, ((filename, source_path), paper, chapter) in enumerate(
        zip(file_records, papers, chapters),
        start=1,
    ):
        if not is_supported_archive_name(filename):
            raise JobError("只支持 .zip、.tar、.tar.gz 或 .tgz 文件。")
        upload_path = uploads_dir / f"{index:02d}-{safe_name(filename)}"
        if source_path.resolve() == upload_path.resolve():
            pass
        elif copy_files:
            shutil.copy2(source_path, upload_path)
        else:
            os.replace(source_path, upload_path)
        result = extract_archive_to_inputs(upload_path, inputs_dir / str(chapter))
        request["papers"].append({
            "original_filename": filename,
            "upload_path": str(upload_path),
            "chapter": chapter,
            "validation": result,
            "client_metadata": paper,
        })

    return request


class MultipartRequest:
    def __init__(self, handler: BaseHTTPRequestHandler):
        content_type = handler.headers.get("Content-Type", "")
        length = int(handler.headers.get("Content-Length", "0") or "0")
        body = handler.rfile.read(length)
        raw = (
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
            + body
        )
        message = BytesParser(policy=policy.default).parsebytes(raw)
        self.fields: Dict[str, str] = {}
        self.files: List[Tuple[str, str, bytes]] = []

        if not message.is_multipart():
            raise JobError("请求必须使用 multipart/form-data。")

        for part in message.iter_parts():
            disposition = part.get("Content-Disposition", "")
            if "form-data" not in disposition:
                continue
            name = part.get_param("name", header="content-disposition")
            filename = part.get_param("filename", header="content-disposition")
            payload = part.get_payload(decode=True) or b""
            if filename:
                self.files.append((name or "file", filename, payload))
            elif name:
                charset = part.get_content_charset() or "utf-8"
                self.fields[name] = payload.decode(charset, errors="replace")


def prepare_job(payload: Dict[str, Any], files: List[Tuple[str, str, bytes]]) -> Dict[str, Any]:
    request, papers, chapters = _prepare_request(payload)
    job_id, job_dir, uploads_dir, inputs_dir = _create_job_workspace(request["created_at"])

    try:
        file_records: List[Tuple[str, Path]] = []
        for index, (_, filename, content) in enumerate(files, start=1):
            upload_path = uploads_dir / f"{index:02d}-{safe_name(filename)}"
            upload_path.write_bytes(content)
            file_records.append((filename, upload_path))
        _finalize_job_request(
            request,
            papers,
            chapters,
            uploads_dir,
            inputs_dir,
            file_records,
            copy_files=False,
        )
        for index, chapter in enumerate(chapters, start=1):
            append_event(job_dir, {
                "type": "progress",
                "stage": "upload",
                "message": f"Paper {index} prepared as chapter {chapter}",
                "percent": min(12, index / len(papers) * 12),
            })
    except Exception:
        update_status(job_dir, state="failed", stage="upload", message="上传文件校验失败", percent=0)
        raise

    write_json(job_dir / "request.json", request)
    return {"job_id": job_id, "job_dir": job_dir, "request": request}


def prepare_job_from_paths(payload: Dict[str, Any], files: List[Tuple[str, Path]]) -> Dict[str, Any]:
    request, papers, chapters = _prepare_request(payload)
    job_id, job_dir, uploads_dir, inputs_dir = _create_job_workspace(request["created_at"])
    try:
        _finalize_job_request(
            request,
            papers,
            chapters,
            uploads_dir,
            inputs_dir,
            files,
            copy_files=False,
        )
        for index, chapter in enumerate(chapters, start=1):
            append_event(job_dir, {
                "type": "progress",
                "stage": "upload",
                "message": f"Paper {index} prepared as chapter {chapter}",
                "percent": min(12, index / len(papers) * 12),
            })
    except Exception:
        update_status(job_dir, state="failed", stage="upload", message="上传文件校验失败", percent=0)
        raise

    write_json(job_dir / "request.json", request)
    return {"job_id": job_id, "job_dir": job_dir, "request": request}


def start_worker(job_id: str, job_dir: Path) -> None:
    cmd = [sys.executable, "-m", "backend.worker", str(job_dir)]
    process = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    update_status(job_dir, state="queued", stage="queued", message="Worker started", percent=0, pid=process.pid)
    append_event(job_dir, {"type": "status", "state": "queued", "stage": "queued", "message": "Worker started", "percent": 0, "pid": process.pid})


class Latex2WordHandler(BaseHTTPRequestHandler):
    server_version = "latex2word-web/0.1"

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/jobs":
            self.handle_list_jobs()
        elif path == "/api/catalog":
            self.handle_catalog_get()
        elif path.startswith("/api/jobs/"):
            self.handle_job_get(path, parse_qs(parsed.query))
        else:
            self.serve_static(path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/jobs":
            self.handle_create_job()
        else:
            json_response(self, HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def serve_static(self, path: str) -> None:
        if path in {"", "/"}:
            target = FRONTEND_DIR / "index.html"
        elif path.startswith("/assets/"):
            target = (ASSETS_DIR / path.removeprefix("/assets/")).resolve()
            if ASSETS_DIR.resolve() not in target.parents and target != ASSETS_DIR.resolve():
                text_response(self, HTTPStatus.FORBIDDEN, "Forbidden")
                return
        else:
            target = (FRONTEND_DIR / path.lstrip("/")).resolve()
            if FRONTEND_DIR.resolve() not in target.parents and target != FRONTEND_DIR.resolve():
                text_response(self, HTTPStatus.FORBIDDEN, "Forbidden")
                return
        if not target.exists() or not target.is_file():
            text_response(self, HTTPStatus.NOT_FOUND, "Not found")
            return

        raw = target.read_bytes()
        mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if target.suffix == ".js":
            mime = "text/javascript"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def handle_create_job(self) -> None:
        try:
            request = MultipartRequest(self)
            payload = json.loads(request.fields.get("payload") or "{}")
            prepared = prepare_job(payload, request.files)
            start_worker(prepared["job_id"], prepared["job_dir"])
            status = read_json(prepared["job_dir"] / "status.json", default={})
            json_response(self, HTTPStatus.ACCEPTED, {"job": status, "papers": prepared["request"]["papers"]})
        except json.JSONDecodeError:
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": "payload JSON 格式不正确。"})
        except JobError as exc:
            json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def handle_list_jobs(self) -> None:
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
        jobs = []
        for status_path in sorted(JOBS_DIR.glob("*/status.json"), key=lambda path: path.stat().st_mtime, reverse=True):
            status = read_json(status_path, default=None)
            if isinstance(status, dict):
                jobs.append(status)
        json_response(self, HTTPStatus.OK, {"jobs": jobs[:50]})

    def handle_catalog_get(self) -> None:
        json_response(self, HTTPStatus.OK, get_catalog_payload())

    def handle_job_get(self, path: str, query: Dict[str, List[str]]) -> None:
        parts = [part for part in path.split("/") if part]
        if len(parts) < 3:
            json_response(self, HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        job_id = parts[2]
        job_dir = JOBS_DIR / job_id
        if not job_dir.exists():
            json_response(self, HTTPStatus.NOT_FOUND, {"error": "Job not found"})
            return

        action = parts[3] if len(parts) > 3 else ""
        if action == "":
            status = read_json(job_dir / "status.json", default={})
            json_response(self, HTTPStatus.OK, {"job": status})
        elif action == "events":
            self.handle_events(job_dir, query)
        elif action == "download":
            self.handle_download(job_dir)
        elif action == "logs":
            log_path = job_dir / "worker.log"
            text_response(self, HTTPStatus.OK, log_path.read_text(encoding="utf-8")[-20000:] if log_path.exists() else "")
        else:
            json_response(self, HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def handle_events(self, job_dir: Path, query: Dict[str, List[str]]) -> None:
        accept = self.headers.get("Accept", "")
        after = int((query.get("after") or ["0"])[0] or "0")
        if "text/event-stream" not in accept:
            events = load_events(job_dir, after=after)
            status = read_json(job_dir / "status.json", default={})
            json_response(self, HTTPStatus.OK, {"events": events, "job": status})
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        deadline = time.time() + 60
        while time.time() < deadline:
            events = load_events(job_dir, after=after)
            for event in events:
                after = int(event["offset"])
                raw = f"id: {after}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")
                self.wfile.write(raw)
                self.wfile.flush()
            status = read_json(job_dir / "status.json", default={}) or {}
            if status.get("state") in {"completed", "failed", "cancelled"}:
                break
            time.sleep(1)

    def handle_download(self, job_dir: Path) -> None:
        status = read_json(job_dir / "status.json", default={}) or {}
        docx = job_dir / "outputs" / "final.docx"
        if status.get("state") != "completed" or not docx.exists():
            json_response(self, HTTPStatus.CONFLICT, {"error": "Word 文档尚未生成。"})
            return
        raw = docx.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        self.send_header("Content-Disposition", 'attachment; filename="latex2word-final.docx"')
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run latex2word web API server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()

    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((args.host, args.port), Latex2WordHandler)
    print(f"latex2word web server: http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
