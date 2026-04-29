from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .db import connect, execute, fetch_one, initialize_database
from .job_utils import utc_now


SESSION_COOKIE = "latex2word_session"
SESSION_DAYS = 14


class AuthError(ValueError):
    pass


def _utc_after_days(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat().replace("+00:00", "Z")


def _normalize_email(raw: str) -> str:
    email = (raw or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise AuthError("请输入有效邮箱地址。")
    return email


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise AuthError("密码至少需要 8 位。")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return f"{salt.hex()}:{digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split(":", 1)
    except ValueError:
        return False
    salt = bytes.fromhex(salt_hex)
    expected = bytes.fromhex(digest_hex)
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return hmac.compare_digest(actual, expected)


def register_user(db_path: Path, email: str, password: str) -> dict:
    initialize_database(db_path)
    normalized = _normalize_email(email)
    user_id = uuid.uuid4().hex
    now = utc_now()
    with connect(db_path) as conn:
        existing = fetch_one(conn, "SELECT id FROM users WHERE email = ?", (normalized,))
        if existing is not None:
            raise AuthError("该邮箱已注册。")
        execute(
            conn,
            "INSERT INTO users (id, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (user_id, normalized, hash_password(password), now),
        )
        conn.commit()
    return {"id": user_id, "email": normalized, "created_at": now}


def create_session(db_path: Path, email: str, password: str) -> tuple[str, dict]:
    initialize_database(db_path)
    normalized = _normalize_email(email)
    with connect(db_path) as conn:
        row = fetch_one(conn, "SELECT * FROM users WHERE email = ?", (normalized,))
        if row is None or not verify_password(password, str(row["password_hash"])):
            raise AuthError("邮箱或密码错误。")
        session_id = uuid.uuid4().hex
        now = utc_now()
        expires_at = _utc_after_days(SESSION_DAYS)
        execute(
            conn,
            "INSERT INTO sessions (id, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (session_id, row["id"], now, expires_at),
        )
        conn.commit()
        return session_id, {"id": row["id"], "email": row["email"], "created_at": row["created_at"]}


def get_session_user(db_path: Path, session_id: str | None) -> dict | None:
    if not session_id:
        return None
    initialize_database(db_path)
    with connect(db_path) as conn:
        row = fetch_one(
            conn,
            """
            SELECT users.id, users.email, users.created_at, sessions.expires_at
            FROM sessions
            JOIN users ON users.id = sessions.user_id
            WHERE sessions.id = ?
            """,
            (session_id,),
        )
        if row is None:
            return None
        expires_at = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
        if expires_at <= datetime.now(timezone.utc):
            execute(conn, "DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.commit()
            return None
        return {"id": row["id"], "email": row["email"], "created_at": row["created_at"]}


def delete_session(db_path: Path, session_id: str | None) -> None:
    if not session_id:
        return
    initialize_database(db_path)
    with connect(db_path) as conn:
        execute(conn, "DELETE FROM sessions WHERE id = ?", (session_id,))
        conn.commit()
