"""Persist submission intent before HTTP; uncertain submissions are never replayed."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from calliope import config
from calliope.comfyui.client import ComfyUIClient, WorkflowRejectedError
from calliope.db import get_db


def latest_receipt(job_id: int) -> dict[str, Any] | None:
    conn = get_db(config.settings.db_path)
    try:
        row = conn.execute(
            "SELECT * FROM render_attempts WHERE job_id = ? ORDER BY rowid DESC LIMIT 1",
            (job_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_receipt(
    attempt_id: str, state: str, *, prompt_id: str | None = None, history: dict | None = None
) -> None:
    conn = get_db(config.settings.db_path)
    try:
        conn.execute(
            "UPDATE render_attempts SET state=?, prompt_id=COALESCE(?,prompt_id), "
            "history_json=COALESCE(?,history_json) WHERE id=?",
            (state, prompt_id, json.dumps(history) if history is not None else None, attempt_id),
        )
        conn.commit()
    finally:
        conn.close()


def _owned(entry: Any, receipt: dict) -> bool:
    return (
        isinstance(entry, list)
        and len(entry) > 3
        and isinstance(entry[3], dict)
        and entry[3].get("calliope_attempt_id") == receipt["id"]
        and entry[3].get("client_id") == receipt["client_id"]
    )


async def ensure_submitted(
    job_id: int,
    client: ComfyUIClient,
    prepare: Callable[[], Awaitable[dict[str, Any]]],
) -> tuple[dict, dict | None]:
    receipt = latest_receipt(job_id)
    if receipt is None or receipt["state"] in ("rejected", "failed"):
        graph = await prepare()
        encoded = json.dumps(graph, sort_keys=True, separators=(",", ":"))
        attempt_id = str(uuid.uuid4())
        conn = get_db(config.settings.db_path)
        try:
            # Serialise concurrent observers before creating another submission intent.
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM render_attempts WHERE job_id=? ORDER BY rowid DESC LIMIT 1",
                (job_id,),
            ).fetchone()
            if existing is None or existing["state"] in ("rejected", "failed"):
                conn.execute(
                    "INSERT INTO render_attempts "
                    "(id,job_id,server_url,client_id,prompt_id,state,graph_json,graph_sha256) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        attempt_id,
                        job_id,
                        client.base_url,
                        client.client_id,
                        str(uuid.uuid4()),
                        "prepared",
                        encoded,
                        hashlib.sha256(encoded.encode()).hexdigest(),
                    ),
                )
            conn.commit()
        finally:
            conn.close()
        receipt = latest_receipt(job_id)
    assert receipt is not None
    if receipt["server_url"] != client.base_url:
        raise RuntimeError("Resume this render using its original ComfyUI server")
    client.client_id = receipt["client_id"]
    conn = get_db(config.settings.db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not job or job["status"] not in ("pending", "running"):
            raise RuntimeError("Job cancelled before submission")
        claimed = conn.execute(
            "UPDATE render_attempts SET state='submitting' WHERE id=? AND state='prepared'",
            (receipt["id"],),
        ).rowcount
        conn.commit()
    finally:
        conn.close()
    if claimed:
        try:
            prompt_id = await client.queue_prompt(
                json.loads(receipt["graph_json"]),
                prompt_id=receipt["prompt_id"],
                attempt_id=receipt["id"],
            )
        except WorkflowRejectedError:
            update_receipt(receipt["id"], "rejected")
            raise
        except Exception as exc:
            update_receipt(receipt["id"], "unknown")
            raise RuntimeError(
                "Submission response uncertain; not resubmitted. Retry to reconcile."
            ) from exc
        update_receipt(receipt["id"], "submitted", prompt_id=prompt_id)
        return latest_receipt(job_id), None

    if receipt.get("history_json"):
        return receipt, json.loads(receipt["history_json"])
    history = await client.get_history(receipt["prompt_id"])
    if history:
        update_receipt(receipt["id"], "completed", history=history)
        return latest_receipt(job_id), history
    queue = await client.get_queue()
    for entry in queue.get("queue_running", []) + queue.get("queue_pending", []):
        if _owned(entry, receipt):
            update_receipt(receipt["id"], "submitted", prompt_id=entry[1])
            return latest_receipt(job_id), None
    for prompt_id, history in (await client.recent_history()).items():
        if _owned(history.get("prompt"), receipt):
            update_receipt(receipt["id"], "completed", prompt_id=prompt_id, history=history)
            return latest_receipt(job_id), history
    raise RuntimeError(
        "Existing render is absent from server queue/history; not resubmitted. "
        "Restore server history or inspect the saved receipt before another render."
    )
