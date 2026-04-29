from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _split_csv(raw: str) -> List[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class AppSettings:
    host: str = os.environ.get("LATEX2WORD_HOST", "127.0.0.1")
    port: int = int(os.environ.get("LATEX2WORD_PORT", "8787"))
    cors_origins: List[str] = None  # type: ignore[assignment]
    max_concurrent_jobs: int = int(os.environ.get("LATEX2WORD_MAX_CONCURRENT_JOBS", "2"))
    max_pending_jobs: int = int(os.environ.get("LATEX2WORD_MAX_PENDING_JOBS", "32"))
    max_upload_bytes: int = int(os.environ.get("LATEX2WORD_MAX_UPLOAD_BYTES", str(256 * 1024 * 1024)))
    upload_chunk_bytes: int = int(os.environ.get("LATEX2WORD_UPLOAD_CHUNK_BYTES", str(1024 * 1024)))
    redis_url: str | None = os.environ.get("LATEX2WORD_REDIS_URL") or None
    redis_prefix: str = os.environ.get("LATEX2WORD_REDIS_PREFIX", "latex2word")
    jobs_dir: Path = PROJECT_ROOT / ".latex2word_jobs"
    db_path: Path = PROJECT_ROOT / ".latex2word_data" / "app.db"
    frontend_dir: Path = PROJECT_ROOT / "frontend"
    assets_dir: Path = PROJECT_ROOT / "assets"
    project_root: Path = PROJECT_ROOT

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "cors_origins",
            _split_csv(os.environ.get("LATEX2WORD_CORS_ORIGINS", "http://127.0.0.1:8787,http://localhost:8787")),
        )


def get_settings() -> AppSettings:
    return AppSettings()
