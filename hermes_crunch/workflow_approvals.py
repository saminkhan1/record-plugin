"""Durable, profile-scoped approval receipts for reviewed Crunch runs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home


_SCHEMA = """
CREATE TABLE IF NOT EXISTS crunch_approval_requests (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    occurrence_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    proposal_digest TEXT NOT NULL,
    revision_digest TEXT NOT NULL,
    action_json TEXT NOT NULL,
    route_json TEXT NOT NULL,
    artifact_json TEXT NOT NULL,
    session_key TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK(status IN ('pending','approved','denied','cancelled','timed_out')),
    execution_state TEXT NOT NULL
        CHECK(execution_state IN ('awaiting','claimed','completed','failed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    decided_at TEXT,
    claim_token TEXT,
    claimed_at TEXT,
    claim_expires_at TEXT,
    completed_at TEXT,
    result_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_crunch_approval_run
ON crunch_approval_requests(run_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_crunch_approval_status
ON crunch_approval_requests(status, execution_state, updated_at);
"""
_CLAIM_LEASE_SECONDS = 180


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _connect(home: Path | None = None) -> sqlite3.Connection:
    profile_home = (home or get_hermes_home()).expanduser().resolve()
    profile_home.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        str(profile_home / "state.db"),
        timeout=10,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=10000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(_SCHEMA)
    return connection


def _authorization_policy(job: dict[str, Any]) -> dict[str, Any]:
    extensions = (
        job.get("extensions")
        if isinstance(job.get("extensions"), dict)
        else {}
    )
    authorization = (
        extensions.get("authorization")
        if isinstance(extensions.get("authorization"), dict)
        else {}
    )
    policy = authorization.get("policy")
    return dict(policy) if isinstance(policy, dict) else {}


def _revision(job: dict[str, Any]) -> dict[str, Any]:
    origin = job.get("origin") if isinstance(job.get("origin"), dict) else {}
    provenance = (
        origin.get("provenance")
        if isinstance(origin.get("provenance"), dict)
        else {}
    )
    return {
        key: provenance.get(key)
        for key in (
            "draft_revision",
            "draft_digest",
            "build_digest",
            "payload_sha256",
            "artifacts_sha256",
            "approval_envelope_sha256",
        )
    }


def expire_stale_requests(*, home: Path | None = None) -> int:
    """Close timed-out prompts and ambiguous expired execution claims."""

    now = _now().isoformat()
    ambiguous = _canonical(
        {
            "success": False,
            "error": (
                "The approved action's prior execution cannot be proven safe "
                "to repeat after restart."
            ),
        }
    )
    with _connect(home) as connection:
        connection.execute("BEGIN IMMEDIATE")
        pending = connection.execute(
            """UPDATE crunch_approval_requests
               SET status='timed_out', execution_state='failed',
                   decided_at=?, completed_at=?, updated_at=?,
                   result_json=?
               WHERE status='pending' AND deadline_at <= ?""",
            (
                now,
                now,
                now,
                _canonical(
                    {
                        "success": False,
                        "error": "approval request timed out",
                    }
                ),
                now,
            ),
        ).rowcount
        claimed = connection.execute(
            """UPDATE crunch_approval_requests
               SET execution_state='failed', completed_at=?, updated_at=?,
                   result_json=?, claim_expires_at=NULL
               WHERE status='approved' AND execution_state='claimed'
                   AND claim_expires_at <= ?""",
            (now, now, ambiguous, now),
        ).rowcount
        connection.commit()
    return int(pending + claimed)


def create_request(
    *,
    job: dict[str, Any],
    policy: dict[str, Any],
    session_key: str,
    run_id: str,
    operation_id: str,
    approval_data: dict[str, Any],
    action_index: int,
    timeout_seconds: int,
    home: Path | None = None,
) -> dict[str, Any]:
    """Create the exact proposal receipt before notifying a human."""

    expire_stale_requests(home=home)
    occurrence_id = str(run_id or "").strip()
    if not occurrence_id:
        raise ValueError("Crunch approval requires a run occurrence id")
    revision = _revision(job)
    route = {
        "authorization_target": policy.get("target"),
        "approval_route": policy.get("approval_route"),
    }
    action = {
        "index": max(0, int(action_index)),
        "operation_id": str(operation_id or "").strip(),
        "command": approval_data.get("command"),
        "description": approval_data.get("description"),
        "pattern_key": approval_data.get("pattern_key"),
        "pattern_keys": approval_data.get("pattern_keys") or [],
    }
    artifact = {
        "job_id": job.get("id"),
        "job_name": job.get("name"),
        "script": job.get("script"),
        "artifacts_sha256": revision.get("artifacts_sha256"),
    }
    proposal = {
        "job_id": job.get("id"),
        "occurrence_id": occurrence_id,
        "revision": revision,
        "policy_id": policy.get("policy_id"),
        "operation_id": operation_id,
        "action": action,
        "route": route,
        "artifact": artifact,
    }
    proposal_digest = hashlib.sha256(
        _canonical(proposal).encode("utf-8")
    ).hexdigest()
    revision_digest = hashlib.sha256(
        _canonical(revision).encode("utf-8")
    ).hexdigest()
    request_id = hashlib.sha256(
        f"{occurrence_id}\0{proposal_digest}".encode("utf-8")
    ).hexdigest()
    now_value = _now()
    now = now_value.isoformat()
    deadline = (
        now_value + timedelta(seconds=max(1, int(timeout_seconds)))
    ).isoformat()
    with _connect(home) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """INSERT INTO crunch_approval_requests (
                   id, job_id, occurrence_id, run_id, policy_id, operation_id,
                   proposal_digest, revision_digest, action_json, route_json,
                   artifact_json, session_key, status, execution_state,
                   created_at, updated_at, deadline_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending',
                         'awaiting', ?, ?, ?)
               ON CONFLICT(id) DO NOTHING""",
            (
                request_id,
                str(job.get("id") or ""),
                occurrence_id,
                occurrence_id,
                str(policy.get("policy_id") or ""),
                str(operation_id or ""),
                proposal_digest,
                revision_digest,
                _canonical(action),
                _canonical(route),
                _canonical(artifact),
                session_key,
                now,
                now,
                deadline,
            ),
        )
        row = connection.execute(
            "SELECT * FROM crunch_approval_requests WHERE id=?",
            (request_id,),
        ).fetchone()
        connection.commit()
    if row is None:
        raise RuntimeError("Durable Crunch approval request was not stored")
    return _public(row)


def record_decision(
    request_id: str,
    *,
    run_id: str,
    decision: dict[str, Any],
    home: Path | None = None,
) -> dict[str, Any]:
    """Persist the human result and atomically claim an approval for this run."""

    resolved = bool(decision.get("resolved"))
    choice = str(decision.get("choice") or "")
    notify_failed = bool(decision.get("notify_failed"))
    reason = str(decision.get("reason") or "")
    approved = resolved and choice == "once"
    status = (
        "cancelled"
        if notify_failed
        else "timed_out"
        if not resolved
        else "approved"
        if approved
        else "denied"
    )
    now_value = _now()
    now = now_value.isoformat()
    claim_token = uuid.uuid4().hex if approved else None
    claim_expires = (
        (now_value + timedelta(seconds=_CLAIM_LEASE_SECONDS)).isoformat()
        if approved
        else None
    )
    execution_state = "claimed" if approved else "failed"
    result = _canonical(
        {
            "success": approved,
            "error": "" if approved else reason or status,
        }
    )
    with _connect(home) as connection:
        connection.execute("BEGIN IMMEDIATE")
        changed = connection.execute(
            """UPDATE crunch_approval_requests
               SET status=?, execution_state=?, decided_at=?, updated_at=?,
                   claim_token=?, claimed_at=?, claim_expires_at=?,
                   completed_at=?, result_json=?
               WHERE id=? AND run_id=? AND status='pending'
                   AND deadline_at > ?""",
            (
                status,
                execution_state,
                now,
                now,
                claim_token,
                now if approved else None,
                claim_expires,
                None if approved else now,
                result,
                request_id,
                str(run_id),
                now,
            ),
        ).rowcount
        if not changed:
            connection.execute(
                """UPDATE crunch_approval_requests
                   SET status='timed_out', execution_state='failed',
                       decided_at=?, completed_at=?, updated_at=?,
                       result_json=?
                   WHERE id=? AND status='pending' AND deadline_at <= ?""",
                (
                    now,
                    now,
                    now,
                    _canonical(
                        {
                            "success": False,
                            "error": "approval expired before consent was persisted",
                        }
                    ),
                    request_id,
                    now,
                ),
            )
        row = connection.execute(
            "SELECT * FROM crunch_approval_requests WHERE id=?",
            (request_id,),
        ).fetchone()
        connection.commit()
    if row is None:
        return {
            "approved": False,
            "message": "The durable approval request disappeared.",
        }
    request = _public(row)
    owned = bool(
        changed
        and request["status"] == "approved"
        and request["execution_state"] == "claimed"
        and request["run_id"] == str(run_id)
    )
    return {
        "approved": owned,
        "message": None if owned else reason or "The action was not approved.",
        "request": request,
    }


def complete_run(
    run_id: str,
    *,
    success: bool,
    error: str = "",
    home: Path | None = None,
) -> int:
    """Write terminal receipts for every action claimed by this exact run."""

    normalized = str(run_id or "").strip()
    if not normalized:
        return 0
    now = _now().isoformat()
    state = "completed" if success else "failed"
    result = _canonical({"success": bool(success), "error": str(error or "")})
    with _connect(home) as connection:
        return connection.execute(
            """UPDATE crunch_approval_requests
               SET execution_state=?, completed_at=?, updated_at=?,
                   claim_expires_at=NULL, result_json=?
               WHERE run_id=? AND status='approved'
                   AND execution_state='claimed'""",
            (state, now, now, result, normalized),
        ).rowcount


def latest_requests_by_run_id(
    run_ids: list[str] | tuple[str, ...] | set[str],
    *,
    home: Path | None = None,
) -> dict[str, dict[str, Any]]:
    normalized = list(
        dict.fromkeys(
            str(run_id).strip()
            for run_id in run_ids
            if str(run_id or "").strip()
        )
    )[:100]
    if not normalized:
        return {}
    expire_stale_requests(home=home)
    placeholders = ",".join("?" for _ in normalized)
    with _connect(home) as connection:
        rows = connection.execute(
            f"""SELECT current.*
                FROM crunch_approval_requests AS current
                WHERE current.run_id IN ({placeholders})
                  AND NOT EXISTS (
                    SELECT 1 FROM crunch_approval_requests AS newer
                    WHERE newer.run_id=current.run_id
                      AND (
                        newer.created_at > current.created_at OR
                        (newer.created_at=current.created_at AND newer.id > current.id)
                      )
                  )""",
            tuple(normalized),
        ).fetchall()
    return {str(row["run_id"]): _public(row) for row in rows}


def _public(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "job_id": row["job_id"],
        "occurrence_id": row["occurrence_id"],
        "run_id": row["run_id"],
        "policy_id": row["policy_id"],
        "operation_id": row["operation_id"],
        "proposal_digest": row["proposal_digest"],
        "revision_digest": row["revision_digest"],
        "action": json.loads(row["action_json"]),
        "route": json.loads(row["route_json"]),
        "artifact": json.loads(row["artifact_json"]),
        "session_key": row["session_key"],
        "status": row["status"],
        "execution_state": row["execution_state"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "deadline_at": row["deadline_at"],
        "decided_at": row["decided_at"],
        "claim_token": row["claim_token"],
        "claimed_at": row["claimed_at"],
        "claim_expires_at": row["claim_expires_at"],
        "completed_at": row["completed_at"],
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
    }
