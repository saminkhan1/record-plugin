"""One typed boundary between the drafting model and Hermes Cron."""

from __future__ import annotations

import json
import threading
from typing import Any

from .compiler import CompileError, draft_submission_schema, validate_draft_plan


class PlanningError(ValueError):
    pass


class PlanningSession:
    def __init__(
        self,
        task_id: str,
        *,
        job_defaults: dict[str, str],
        script_namespace: str,
    ) -> None:
        self.task_id = task_id
        self.job_defaults = dict(job_defaults)
        self.script_namespace = script_namespace
        self.lock = threading.RLock()
        self.submitted: dict[str, Any] | None = None


_SESSIONS: dict[str, PlanningSession] = {}
_SESSIONS_LOCK = threading.RLock()


def begin(
    task_id: str,
    *,
    job_defaults: dict[str, str],
    script_namespace: str,
) -> PlanningSession:
    task_id = str(task_id or "")
    if not task_id.startswith("record:planner:"):
        raise PlanningError("invalid Record planner task")
    session = PlanningSession(
        task_id,
        job_defaults=job_defaults,
        script_namespace=script_namespace,
    )
    with _SESSIONS_LOCK:
        _SESSIONS[task_id] = session
    return session


def get(task_id: str) -> PlanningSession | None:
    with _SESSIONS_LOCK:
        return _SESSIONS.get(task_id)


def finish(task_id: str) -> dict[str, Any] | None:
    with _SESSIONS_LOCK:
        session = _SESSIONS.pop(task_id, None)
    return session.submitted if session else None


def submit_draft(args: dict[str, Any], *, task_id: str = "", **_kwargs: Any) -> str:
    session = get(task_id)
    if session is None:
        return _json_error("planner session is not active")
    with session.lock:
        if session.submitted is not None:
            return _json_error("record_submit_draft may only be called once")
        try:
            session.submitted = validate_draft_plan(
                args.get("plan"),
                job_defaults=session.job_defaults,
                script_namespace=session.script_namespace,
            )
        except CompileError as exc:
            return _json_error(str(exc))
    return json.dumps(
        {
            "success": True,
            "message": "Executable Hermes job draft accepted.",
        }
    )


def register(ctx: Any) -> None:
    ctx.register_tool(
        name="record_submit_draft",
        toolset="record_planning",
        schema={
            "name": "record_submit_draft",
            "description": (
                "Validate and store the exact Hermes Cron job proposal shown "
                "to the user."
            ),
            "parameters": draft_submission_schema(),
        },
        handler=submit_draft,
        description="Store one executable Hermes Cron job proposal.",
    )


def _json_error(message: str) -> str:
    return json.dumps({"success": False, "error": message}, ensure_ascii=False)
