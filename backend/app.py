from __future__ import annotations

import asyncio
import contextlib
import json
import mimetypes
import os
import tempfile
from pathlib import Path
from typing import List

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse

from .auth import (
    AuthError,
    SESSION_COOKIE,
    create_session,
    delete_session,
    get_session_user,
    register_user,
)
from .db import initialize_database
from .job_utils import JobError, load_events, read_json
from .job_queue import JobAlreadyQueuedError, LocalJobQueue, QueueFullError, RedisJobQueue
from .model_catalog import get_catalog_payload
from .orders import (
    OrderError,
    attach_job_to_order,
    create_quote_order,
    ensure_order_paid,
    get_job_owner_id,
    get_order,
    mark_order_paid,
    sync_job_state,
)
from .server import prepare_job_from_paths
from .settings import AppSettings, get_settings


def create_app(settings: AppSettings | None = None) -> FastAPI:
    config = settings or get_settings()
    config.jobs_dir.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="latex2word API", version="0.2.0")
    app.state.settings = config
    app.state.job_queue = (
        RedisJobQueue(
            redis_url=config.redis_url,
            max_concurrent_jobs=config.max_concurrent_jobs,
            max_pending_jobs=config.max_pending_jobs,
            project_root=config.project_root,
            key_prefix=config.redis_prefix,
        )
        if config.redis_url
        else LocalJobQueue(
            max_concurrent_jobs=config.max_concurrent_jobs,
            max_pending_jobs=config.max_pending_jobs,
            project_root=config.project_root,
        )
    )

    if config.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
        )

    def _job_dir(job_id: str) -> Path:
        job_dir = config.jobs_dir / job_id
        if not job_dir.exists():
            raise HTTPException(status_code=404, detail="Job not found")
        return job_dir

    def _optional_user(request: Request) -> dict | None:
        session_id = request.cookies.get(SESSION_COOKIE)
        return get_session_user(config.db_path, session_id)

    def _require_user(request: Request) -> dict:
        user = _optional_user(request)
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        return user

    def _safe_frontend_target(raw_path: str) -> Path:
        if raw_path in {"", "/"}:
            return config.frontend_dir / "index.html"
        if raw_path.startswith("/assets/"):
            target = (config.assets_dir / raw_path.removeprefix("/assets/")).resolve()
            base = config.assets_dir.resolve()
        else:
            target = (config.frontend_dir / raw_path.lstrip("/")).resolve()
            base = config.frontend_dir.resolve()
        if target != base and base not in target.parents:
            raise HTTPException(status_code=403, detail="Forbidden")
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail="Not found")
        return target

    async def _persist_upload(upload: UploadFile) -> Path:
        suffix = Path(upload.filename or "paper.zip").suffix or ".upload"
        fd, raw_path = tempfile.mkstemp(prefix="upload-", suffix=suffix, dir=config.jobs_dir)
        temp_path = Path(raw_path)
        total = 0
        try:
            with os.fdopen(fd, "wb") as handle:
                while True:
                    chunk = await upload.read(config.upload_chunk_bytes)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > config.max_upload_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=f"单个上传文件过大，当前上限为 {config.max_upload_bytes // (1024 * 1024)} MB。",
                        )
                    handle.write(chunk)
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                temp_path.unlink()
            raise
        finally:
            await upload.close()
        return temp_path

    @app.on_event("startup")
    async def startup() -> None:
        initialize_database(config.db_path)
        await app.state.job_queue.start()

    @app.on_event("shutdown")
    async def shutdown() -> None:
        await app.state.job_queue.stop()

    @app.get("/api/catalog")
    async def get_catalog() -> JSONResponse:
        return JSONResponse(get_catalog_payload())

    @app.post("/api/auth/register")
    async def auth_register(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
            user = register_user(
                config.db_path,
                str(payload.get("email") or ""),
                str(payload.get("password") or ""),
            )
            session_id, user = create_session(
                config.db_path,
                str(payload.get("email") or ""),
                str(payload.get("password") or ""),
            )
        except AuthError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        result = JSONResponse({"user": user})
        result.set_cookie(SESSION_COOKIE, session_id, httponly=True, samesite="lax")
        return result

    @app.post("/api/auth/login")
    async def auth_login(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
            session_id, user = create_session(
                config.db_path,
                str(payload.get("email") or ""),
                str(payload.get("password") or ""),
            )
        except AuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        result = JSONResponse({"user": user})
        result.set_cookie(SESSION_COOKIE, session_id, httponly=True, samesite="lax")
        return result

    @app.post("/api/auth/logout")
    async def auth_logout(request: Request) -> JSONResponse:
        delete_session(config.db_path, request.cookies.get(SESSION_COOKIE))
        result = JSONResponse({"ok": True})
        result.delete_cookie(SESSION_COOKIE)
        return result

    @app.get("/api/auth/me")
    async def auth_me(request: Request) -> JSONResponse:
        user = _optional_user(request)
        return JSONResponse({"user": user})

    @app.post("/api/orders/quote")
    async def create_order_quote(request: Request, user: dict = Depends(_require_user)) -> JSONResponse:
        try:
            payload = await request.json()
            order = create_quote_order(config.db_path, payload, user_id=str(user["id"]))
            return JSONResponse({"order": order})
        except OrderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/orders/{order_id}")
    async def fetch_order(order_id: str, user: dict = Depends(_require_user)) -> JSONResponse:
        try:
            return JSONResponse({"order": get_order(config.db_path, order_id, user_id=str(user["id"]))})
        except OrderError as exc:
            raise HTTPException(status_code=403 if "无权" in str(exc) else 404, detail=str(exc)) from exc

    @app.post("/api/orders/{order_id}/mock-pay")
    async def mock_pay_order(order_id: str, request: Request, user: dict = Depends(_require_user)) -> JSONResponse:
        try:
            payload = await request.json()
            payment_channel = str(payload.get("payment_channel") or "mock").strip() or "mock"
            return JSONResponse({"order": mark_order_paid(config.db_path, order_id, payment_channel, user_id=str(user["id"]))})
        except OrderError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/jobs")
    async def list_jobs(user: dict = Depends(_require_user)) -> JSONResponse:
        jobs = []
        for status_path in sorted(
            config.jobs_dir.glob("*/status.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        ):
            status = read_json(status_path, default=None)
            if isinstance(status, dict) and get_job_owner_id(config.db_path, str(status.get("id") or "")) == user["id"]:
                jobs.append(status)
        return JSONResponse({"jobs": jobs[:50]})

    @app.post("/api/jobs")
    async def create_job(
        payload: str = Form(...),
        papers: List[UploadFile] = File(...),
        user: dict = Depends(_require_user),
    ) -> JSONResponse:
        files = []
        try:
            parsed_payload = json.loads(payload or "{}")
            order_id = str(parsed_payload.get("order_id") or "").strip()
            if not order_id:
                raise HTTPException(status_code=400, detail="缺少 order_id，支付后才能开始转换。")
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="payload JSON 格式不正确。") from exc

        try:
            for paper in papers:
                temp_path = await _persist_upload(paper)
                files.append((paper.filename or "paper.zip", temp_path))
            estimate = parsed_payload.get("estimate") or {}
            ensure_order_paid(
                config.db_path,
                order_id,
                user_id=str(user["id"]),
                provider=str(parsed_payload.get("translate", {}).get("provider") or "").strip().lower(),
                model=str(parsed_payload.get("translate", {}).get("model") or "").strip(),
                estimated_chars=int(estimate.get("estimated_chars") or 0),
            )
            prepared = prepare_job_from_paths(parsed_payload, files)
            attach_job_to_order(config.db_path, order_id, prepared["job_id"], user_id=str(user["id"]))
            await app.state.job_queue.enqueue(prepared["job_id"], prepared["job_dir"])
            status = read_json(prepared["job_dir"] / "status.json", default={})
            sync_job_state(config.db_path, prepared["job_id"], str(status.get("state") or "queued"))
            return JSONResponse(
                status_code=202,
                content={"job": status, "papers": prepared["request"]["papers"]},
            )
        except JobError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OrderError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except QueueFullError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except JobAlreadyQueuedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        finally:
            for _, temp_path in files:
                with contextlib.suppress(FileNotFoundError):
                    temp_path.unlink()

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str, user: dict = Depends(_require_user)) -> JSONResponse:
        job_dir = _job_dir(job_id)
        if get_job_owner_id(config.db_path, job_id) != user["id"]:
            raise HTTPException(status_code=403, detail="无权访问该任务。")
        status = read_json(job_dir / "status.json", default={}) or {}
        sync_job_state(config.db_path, job_id, str(status.get("state") or "queued"))
        return JSONResponse({"job": status})

    @app.get("/api/jobs/{job_id}/events")
    async def get_job_events(job_id: str, request: Request, after: int = 0, user: dict = Depends(_require_user)):
        job_dir = _job_dir(job_id)
        if get_job_owner_id(config.db_path, job_id) != user["id"]:
            raise HTTPException(status_code=403, detail="无权访问该任务。")
        accept = request.headers.get("accept", "")
        if "text/event-stream" not in accept:
            events = load_events(job_dir, after=after)
            status = read_json(job_dir / "status.json", default={})
            return JSONResponse({"events": events, "job": status})

        async def stream():
            current_after = after
            deadline = asyncio.get_running_loop().time() + 60
            while asyncio.get_running_loop().time() < deadline:
                if await request.is_disconnected():
                    break
                events = load_events(job_dir, after=current_after)
                for event in events:
                    current_after = int(event["offset"])
                    payload = json.dumps(event, ensure_ascii=False)
                    yield f"id: {current_after}\ndata: {payload}\n\n"
                status = read_json(job_dir / "status.json", default={}) or {}
                sync_job_state(config.db_path, job_id, str(status.get("state") or "queued"))
                if status.get("state") in {"completed", "failed", "cancelled"}:
                    break
                await asyncio.sleep(1)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/api/jobs/{job_id}/download")
    async def download_job(job_id: str, user: dict = Depends(_require_user)):
        job_dir = _job_dir(job_id)
        if get_job_owner_id(config.db_path, job_id) != user["id"]:
            raise HTTPException(status_code=403, detail="无权下载该任务结果。")
        status = read_json(job_dir / "status.json", default={}) or {}
        docx = job_dir / "outputs" / "final.docx"
        if status.get("state") != "completed" or not docx.exists():
            raise HTTPException(status_code=409, detail="Word 文档尚未生成。")
        return FileResponse(
            docx,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename="latex2word-final.docx",
        )

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str, user: dict = Depends(_require_user)) -> JSONResponse:
        job_dir = _job_dir(job_id)
        if get_job_owner_id(config.db_path, job_id) != user["id"]:
            raise HTTPException(status_code=403, detail="无权取消该任务。")
        cancelled = await app.state.job_queue.cancel(job_id, job_dir)
        if not cancelled:
            status = read_json(job_dir / "status.json", default={}) or {}
            raise HTTPException(
                status_code=409,
                detail=f"当前状态无法取消：{status.get('state') or 'unknown'}",
            )
        status = read_json(job_dir / "status.json", default={}) or {}
        sync_job_state(config.db_path, job_id, str(status.get("state") or "cancelled"))
        return JSONResponse({"job": status})

    @app.get("/api/jobs/{job_id}/logs")
    async def get_job_logs(job_id: str, user: dict = Depends(_require_user)) -> PlainTextResponse:
        job_dir = _job_dir(job_id)
        if get_job_owner_id(config.db_path, job_id) != user["id"]:
            raise HTTPException(status_code=403, detail="无权访问该任务日志。")
        log_path = job_dir / "worker.log"
        if not log_path.exists():
            return PlainTextResponse("")
        return PlainTextResponse(log_path.read_text(encoding="utf-8")[-20000:])

    @app.get("/assets/{asset_path:path}")
    async def get_asset(asset_path: str):
        target = _safe_frontend_target(f"/assets/{asset_path}")
        media_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        return FileResponse(target, media_type=media_type)

    @app.get("/{path:path}")
    async def serve_frontend(path: str = ""):
        normalized = f"/{path}" if path else "/"
        target = _safe_frontend_target(normalized)
        media_type = mimetypes.guess_type(str(target))[0] or "text/html"
        if target.suffix == ".js":
            media_type = "text/javascript"
        return FileResponse(target, media_type=media_type)

    return app


app = create_app()


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "backend.app:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
