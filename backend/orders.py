from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

from .db import connect, execute, fetch_one, initialize_database, json_dumps
from .job_utils import utc_now
from .model_catalog import find_model, get_provider_catalog


class OrderError(ValueError):
    pass


@dataclass(frozen=True)
class QuoteRequest:
    provider: str
    model: str
    estimated_chars: int


def _normalize_estimated_chars(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise OrderError("estimated_chars 必须是整数。") from exc
    if value <= 0:
        raise OrderError("estimated_chars 必须大于 0。")
    return value


def build_quote(request: QuoteRequest) -> Dict[str, Any]:
    model = find_model(request.provider, request.model)
    if model is None:
        raise OrderError("模型不存在，无法生成报价。")
    providers = get_provider_catalog()
    provider_info = providers[request.provider]

    chars_per_token = float(model.get("chars_per_token") or 1.7)
    output_ratio = float(model.get("default_output_ratio") or 1.1)
    input_tokens = max(1, int((request.estimated_chars / chars_per_token) + 0.9999))
    output_tokens = max(1, int(((request.estimated_chars * output_ratio) / chars_per_token) + 0.9999))
    input_cost = (input_tokens / 1_000_000) * float(model.get("input_price_per_mtok") or 0)
    output_cost = (output_tokens / 1_000_000) * float(model.get("output_price_per_mtok") or 0)
    raw_cost = input_cost + output_cost
    markup_multiplier = float(model.get("markup_multiplier") or 2.2)
    minimum_price = float(model.get("minimum_price") or 2.0)
    amount = max(minimum_price, raw_cost * markup_multiplier)

    return {
        "provider": request.provider,
        "provider_label": provider_info["label"],
        "model": request.model,
        "model_label": model["label"],
        "estimated_chars": request.estimated_chars,
        "estimated_input_tokens": input_tokens,
        "estimated_output_tokens": output_tokens,
        "output_ratio": output_ratio,
        "input_cost": round(input_cost, 6),
        "output_cost": round(output_cost, 6),
        "raw_cost": round(raw_cost, 6),
        "markup_multiplier": markup_multiplier,
        "minimum_price": minimum_price,
        "currency": provider_info["currency"],
        "amount": round(amount, 2),
        "pricing_as_of": provider_info["pricing_as_of"],
        "pricing_note": provider_info["pricing_note"],
    }


def create_quote_order(db_path: Path, payload: Dict[str, Any], *, user_id: str) -> Dict[str, Any]:
    initialize_database(db_path)
    provider = str(payload.get("provider") or "").strip().lower()
    model = str(payload.get("model") or "").strip()
    estimated_chars = _normalize_estimated_chars(payload.get("estimated_chars"))
    quote = build_quote(QuoteRequest(provider=provider, model=model, estimated_chars=estimated_chars))

    order_id = uuid.uuid4().hex
    now = utc_now()
    with connect(db_path) as conn:
        execute(
            conn,
            """
            INSERT INTO orders (
                id, user_id, status, payment_channel, provider, model,
                estimated_chars, estimated_input_tokens, estimated_output_tokens,
                output_ratio, currency, amount, quote_details_json,
                job_id, created_at, updated_at, paid_at
            ) VALUES (?, ?, 'awaiting_payment', NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL)
            """,
            (
                order_id,
                user_id,
                quote["provider"],
                quote["model"],
                quote["estimated_chars"],
                quote["estimated_input_tokens"],
                quote["estimated_output_tokens"],
                quote["output_ratio"],
                quote["currency"],
                quote["amount"],
                json_dumps(quote),
                now,
                now,
            ),
        )
        conn.commit()

    return get_order(db_path, order_id)


def get_order(db_path: Path, order_id: str, *, user_id: str | None = None) -> Dict[str, Any]:
    initialize_database(db_path)
    with connect(db_path) as conn:
        row = fetch_one(conn, "SELECT * FROM orders WHERE id = ?", (order_id,))
        if row is None:
            raise OrderError("订单不存在。")
        if user_id is not None and row["user_id"] != user_id:
            raise OrderError("无权访问该订单。")
        quote = row["quote_details_json"]
        details = {} if not quote else json.loads(quote)
        return {
            "id": row["id"],
            "status": row["status"],
            "payment_channel": row["payment_channel"],
            "provider": row["provider"],
            "model": row["model"],
            "estimated_chars": row["estimated_chars"],
            "amount": row["amount"],
            "currency": row["currency"],
            "job_id": row["job_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "paid_at": row["paid_at"],
            "quote": details,
        }


def mark_order_paid(db_path: Path, order_id: str, payment_channel: str, *, user_id: str) -> Dict[str, Any]:
    initialize_database(db_path)
    now = utc_now()
    with connect(db_path) as conn:
        row = fetch_one(conn, "SELECT status, user_id FROM orders WHERE id = ?", (order_id,))
        if row is None:
            raise OrderError("订单不存在。")
        if row["user_id"] != user_id:
            raise OrderError("无权支付该订单。")
        status = str(row["status"])
        if status == "paid":
            return get_order(db_path, order_id, user_id=user_id)
        if status != "awaiting_payment":
            raise OrderError(f"当前订单状态不允许支付：{status}")
        execute(
            conn,
            """
            UPDATE orders
            SET status = 'paid', payment_channel = ?, paid_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (payment_channel, now, now, order_id),
        )
        conn.commit()
    return get_order(db_path, order_id, user_id=user_id)


def ensure_order_paid(db_path: Path, order_id: str, *, user_id: str, provider: str, model: str, estimated_chars: int) -> Dict[str, Any]:
    order = get_order(db_path, order_id, user_id=user_id)
    if order["status"] != "paid":
        raise OrderError("订单尚未支付。")
    quote = order["quote"]
    if quote.get("provider") != provider or quote.get("model") != model:
        raise OrderError("当前模型配置与已支付订单不一致，请重新报价。")
    paid_chars = int(order["estimated_chars"])
    if abs(paid_chars - estimated_chars) > max(800, int(paid_chars * 0.08)):
        raise OrderError("文档预估字数变化较大，请重新报价后再支付。")
    return order


def attach_job_to_order(db_path: Path, order_id: str, job_id: str, *, user_id: str) -> Dict[str, Any]:
    initialize_database(db_path)
    now = utc_now()
    with connect(db_path) as conn:
        row = fetch_one(conn, "SELECT status, user_id FROM orders WHERE id = ?", (order_id,))
        if row is None:
            raise OrderError("订单不存在。")
        if row["user_id"] != user_id:
            raise OrderError("无权启动该订单。")
        status = str(row["status"])
        if status not in {"paid", "processing"}:
            raise OrderError(f"当前订单状态无法启动任务：{status}")
        execute(
            conn,
            "UPDATE orders SET status = 'processing', job_id = ?, updated_at = ? WHERE id = ?",
            (job_id, now, order_id),
        )
        execute(
            conn,
            """
            INSERT INTO jobs (id, order_id, state, created_at, updated_at)
            VALUES (?, ?, 'queued', ?, ?)
            ON CONFLICT(id) DO UPDATE SET order_id = excluded.order_id, state = excluded.state, updated_at = excluded.updated_at
            """,
            (job_id, order_id, now, now),
        )
        conn.commit()
    return get_order(db_path, order_id, user_id=user_id)


def sync_job_state(db_path: Path, job_id: str, state: str) -> None:
    initialize_database(db_path)
    now = utc_now()
    with connect(db_path) as conn:
        row = fetch_one(conn, "SELECT order_id FROM jobs WHERE id = ?", (job_id,))
        if row is None:
            return
        order_id = row["order_id"]
        execute(conn, "UPDATE jobs SET state = ?, updated_at = ? WHERE id = ?", (state, now, job_id))
        if order_id:
            order_state = {
                "queued": "processing",
                "running": "processing",
                "completed": "completed",
                "failed": "failed",
                "cancelled": "cancelled",
            }.get(state, "processing")
            execute(conn, "UPDATE orders SET status = ?, updated_at = ? WHERE id = ?", (order_state, now, order_id))
        conn.commit()


def get_job_owner_id(db_path: Path, job_id: str) -> str | None:
    initialize_database(db_path)
    with connect(db_path) as conn:
        row = fetch_one(
            conn,
            """
            SELECT orders.user_id
            FROM jobs
            JOIN orders ON orders.id = jobs.order_id
            WHERE jobs.id = ?
            """,
            (job_id,),
        )
        return None if row is None else row["user_id"]
