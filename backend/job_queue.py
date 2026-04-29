from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict

from .job_utils import append_event, read_json, update_status


class QueueFullError(RuntimeError):
    pass


class JobAlreadyQueuedError(RuntimeError):
    pass


@dataclass(frozen=True)
class QueuedJob:
    job_id: str
    job_dir: Path


class LocalJobQueue:
    def __init__(self, *, max_concurrent_jobs: int, max_pending_jobs: int, project_root: Path):
        self.max_concurrent_jobs = max(1, max_concurrent_jobs)
        self.max_pending_jobs = max(1, max_pending_jobs)
        self.project_root = project_root
        self._queue: asyncio.Queue[QueuedJob] = asyncio.Queue(maxsize=self.max_pending_jobs)
        self._workers: list[asyncio.Task[None]] = []
        self._active: Dict[str, asyncio.subprocess.Process] = {}
        self._cancelled: set[str] = set()
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._workers = [
            asyncio.create_task(self._worker_loop(index), name=f"latex2word-queue-{index}")
            for index in range(self.max_concurrent_jobs)
        ]

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False

        for task in self._workers:
            task.cancel()
        for process in list(self._active.values()):
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
        for task in self._workers:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._workers.clear()
        self._active.clear()

    async def enqueue(self, job_id: str, job_dir: Path) -> None:
        if self._queue.full():
            raise QueueFullError("当前排队任务过多，请稍后重试。")
        queued = QueuedJob(job_id=job_id, job_dir=job_dir)
        await self._queue.put(queued)
        queue_depth = self._queue.qsize()
        update_status(
            job_dir,
            state="queued",
            stage="queued",
            message="Queued for processing",
            percent=0,
            queue_depth=queue_depth,
        )
        append_event(
            job_dir,
            {
                "type": "status",
                "state": "queued",
                "stage": "queued",
                "message": "Queued for processing",
                "percent": 0,
                "queue_depth": queue_depth,
            },
        )

    async def cancel(self, job_id: str, job_dir: Path) -> bool:
        process = self._active.get(job_id)
        if process is not None:
            with contextlib.suppress(ProcessLookupError):
                if sys.platform == "win32":
                    process.terminate()
                else:
                    process.send_signal(signal.SIGTERM)
            self._cancelled.add(job_id)
            update_status(job_dir, state="cancelled", stage="cancelled", message="任务已取消", percent=0)
            append_event(job_dir, {"type": "status", "state": "cancelled", "stage": "cancelled", "message": "任务已取消", "percent": 0})
            return True

        status = read_json(job_dir / "status.json", default={}) or {}
        if status.get("state") == "queued":
            self._cancelled.add(job_id)
            update_status(job_dir, state="cancelled", stage="cancelled", message="任务已取消", percent=0)
            append_event(job_dir, {"type": "status", "state": "cancelled", "stage": "cancelled", "message": "任务已取消", "percent": 0})
            return True
        return False

    async def _worker_loop(self, worker_index: int) -> None:
        while True:
            queued = await self._queue.get()
            try:
                await self._run_job(queued, worker_index)
            finally:
                self._queue.task_done()

    async def _run_job(self, queued: QueuedJob, worker_index: int) -> None:
        if queued.job_id in self._cancelled:
            self._cancelled.discard(queued.job_id)
            return

        cmd = [sys.executable, "-m", "backend.worker", str(queued.job_dir)]
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(self.project_root),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._active[queued.job_id] = process
        update_status(
            queued.job_dir,
            state="running",
            stage="queued",
            message="Worker started",
            percent=0,
            pid=process.pid,
            worker_slot=worker_index,
        )
        append_event(
            queued.job_dir,
            {
                "type": "status",
                "state": "running",
                "stage": "queued",
                "message": "Worker started",
                "percent": 0,
                "pid": process.pid,
                "worker_slot": worker_index,
            },
        )
        try:
            await process.wait()
        finally:
            self._active.pop(queued.job_id, None)
            self._cancelled.discard(queued.job_id)


class RedisJobQueue:
    def __init__(
        self,
        *,
        redis_url: str,
        max_concurrent_jobs: int,
        max_pending_jobs: int,
        project_root: Path,
        key_prefix: str = "latex2word",
    ):
        self.redis_url = redis_url
        self.max_concurrent_jobs = max(1, max_concurrent_jobs)
        self.max_pending_jobs = max(1, max_pending_jobs)
        self.project_root = project_root
        self.key_prefix = key_prefix.rstrip(":") or "latex2word"
        self.queue_key = f"{self.key_prefix}:jobs:pending"
        self.processing_key = f"{self.key_prefix}:jobs:processing"
        self.cancelled_key = f"{self.key_prefix}:jobs:cancelled"
        self.payload_key = f"{self.key_prefix}:jobs:payloads"
        self._workers: list[asyncio.Task[None]] = []
        self._active: Dict[str, asyncio.subprocess.Process] = {}
        self._redis = None
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        try:
            from redis.asyncio import from_url
        except ImportError as exc:
            raise RuntimeError("未安装 redis 包，无法启用 Redis 队列。") from exc
        self._redis = from_url(self.redis_url, decode_responses=True)
        await self._redis.ping()
        await self._requeue_processing_jobs()
        self._started = True
        self._workers = [
            asyncio.create_task(self._worker_loop(index), name=f"latex2word-redis-queue-{index}")
            for index in range(self.max_concurrent_jobs)
        ]

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        for task in self._workers:
            task.cancel()
        for process in list(self._active.values()):
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
        for task in self._workers:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._workers.clear()
        self._active.clear()
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    async def enqueue(self, job_id: str, job_dir: Path) -> None:
        if self._redis is None:
            raise RuntimeError("Redis queue 尚未启动。")
        queue_depth = int(await self._redis.llen(self.queue_key))
        if queue_depth >= self.max_pending_jobs:
            raise QueueFullError("当前排队任务过多，请稍后重试。")
        payload = json.dumps({"job_id": job_id, "job_dir": str(job_dir)}, ensure_ascii=False)
        existing = await self._redis.hget(self.payload_key, job_id)
        if existing:
            raise JobAlreadyQueuedError("任务已经在队列中，请勿重复提交。")
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.hset(self.payload_key, job_id, payload)
            pipe.lpush(self.queue_key, payload)
            await pipe.execute()
        queue_depth = int(await self._redis.llen(self.queue_key))
        update_status(
            job_dir,
            state="queued",
            stage="queued",
            message="Queued for processing",
            percent=0,
            queue_depth=queue_depth,
        )
        append_event(
            job_dir,
            {
                "type": "status",
                "state": "queued",
                "stage": "queued",
                "message": "Queued for processing",
                "percent": 0,
                "queue_depth": queue_depth,
            },
        )

    async def cancel(self, job_id: str, job_dir: Path) -> bool:
        process = self._active.get(job_id)
        if process is not None:
            with contextlib.suppress(ProcessLookupError):
                if sys.platform == "win32":
                    process.terminate()
                else:
                    process.send_signal(signal.SIGTERM)
            update_status(job_dir, state="cancelled", stage="cancelled", message="任务已取消", percent=0)
            append_event(job_dir, {"type": "status", "state": "cancelled", "stage": "cancelled", "message": "任务已取消", "percent": 0})
            return True
        if self._redis is None:
            return False
        payload = await self._redis.hget(self.payload_key, job_id)
        if payload:
            removed = int(await self._redis.lrem(self.queue_key, 1, payload))
            if removed:
                await self._redis.hdel(self.payload_key, job_id)
                update_status(job_dir, state="cancelled", stage="cancelled", message="任务已取消", percent=0)
                append_event(job_dir, {"type": "status", "state": "cancelled", "stage": "cancelled", "message": "任务已取消", "percent": 0})
                return True
        await self._redis.sadd(self.cancelled_key, job_id)
        status = read_json(job_dir / "status.json", default={}) or {}
        if status.get("state") == "queued":
            update_status(job_dir, state="cancelled", stage="cancelled", message="任务已取消", percent=0)
            append_event(job_dir, {"type": "status", "state": "cancelled", "stage": "cancelled", "message": "任务已取消", "percent": 0})
            return True
        return False

    async def _requeue_processing_jobs(self) -> None:
        if self._redis is None:
            return
        while True:
            payload = await self._redis.rpoplpush(self.processing_key, self.queue_key)
            if payload is None:
                break

    async def _worker_loop(self, worker_index: int) -> None:
        if self._redis is None:
            raise RuntimeError("Redis queue 尚未启动。")
        while True:
            payload = await self._redis.brpoplpush(self.queue_key, self.processing_key, timeout=5)
            if not payload:
                continue
            raw = json.loads(payload)
            queued = QueuedJob(job_id=str(raw["job_id"]), job_dir=Path(str(raw["job_dir"])))
            try:
                await self._run_job(queued, payload, worker_index)
            except Exception:
                update_status(queued.job_dir, state="failed", stage="failed", message="队列执行失败", percent=0)
                append_event(queued.job_dir, {"type": "status", "state": "failed", "stage": "failed", "message": "队列执行失败", "percent": 0})

    async def _run_job(self, queued: QueuedJob, payload: str, worker_index: int) -> None:
        if self._redis is None:
            raise RuntimeError("Redis queue 尚未启动。")
        was_cancelled = int(await self._redis.srem(self.cancelled_key, queued.job_id))
        if was_cancelled:
            await self._redis.lrem(self.processing_key, 1, payload)
            await self._redis.hdel(self.payload_key, queued.job_id)
            return

        process: asyncio.subprocess.Process | None = None
        try:
            cmd = [sys.executable, "-m", "backend.worker", str(queued.job_dir)]
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self.project_root),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            self._active[queued.job_id] = process
            update_status(
                queued.job_dir,
                state="running",
                stage="queued",
                message="Worker started",
                percent=0,
                pid=process.pid,
                worker_slot=worker_index,
            )
            append_event(
                queued.job_dir,
                {
                    "type": "status",
                    "state": "running",
                    "stage": "queued",
                    "message": "Worker started",
                    "percent": 0,
                    "pid": process.pid,
                    "worker_slot": worker_index,
                },
            )
            await process.wait()
        finally:
            if process is not None:
                self._active.pop(queued.job_id, None)
            await self._redis.lrem(self.processing_key, 1, payload)
            await self._redis.hdel(self.payload_key, queued.job_id)
