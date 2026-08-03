"""Record Desktop backend routes.

Mounted by Hermes under /api/plugins/record/.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
import time
import uuid
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from hermes_cli.plugins import discover_plugins

discover_plugins()

from hermes_plugins.record.record_recorder import (  # noqa: E402
    _recording_digest,
    load_recording_build,
    prepare_recording_build,
    record_slug,
    load_record_modules,
    load_review_conversation,
    publish_record_build,
    review_conversation_path,
    save_review_conversation,
    workflow_digest,
)
from hermes_plugins.record.hermes_recorder import (  # noqa: E402
    active_native_recording,
    discard_native_recording,
    recording_status as load_recording_status,
    start_native_recording,
    stop_desktop_recording,
)
from hermes_plugins.record.recording_bundle import (  # noqa: E402
    read_json,
    resolve_recording_ref,
    safe_recording_dir,
    write_json,
)
from hermes_plugins.record.record.trace_schema import (  # noqa: E402
    utc_now,
)

router = APIRouter()
log = logging.getLogger(__name__)

_DRAFT_ACTIVE_STATUSES = frozenset({"drafting", "canceling", "finalizing"})
_DRAFT_TERMINAL_STATUSES = frozenset({"completed", "failed", "canceled"})
_DRAFT_INACTIVITY_SECONDS = 10 * 60
_DRAFT_MAX_RUNTIME_SECONDS = 30 * 60
_DRAFT_TERMINAL_RETENTION_SECONDS = 10 * 60
_DRAFT_MAX_OPERATIONS = 64
_DESKTOP_CAPTURE_AUTHORIZATION_TTL_SECONDS = 2 * 60
_DESKTOP_CAPTURE_AUTHORIZATION_LIMIT = 64


@dataclass
class _DraftOperation:
    operation_id: str
    recording_dir: Path
    status: str = "drafting"
    result: Optional[Dict[str, Any]] = None
    error: str = ""
    cancel_requested: bool = False
    progress: List[Dict[str, Any]] = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock)
    created_monotonic: float = field(default_factory=time.monotonic)
    last_activity_monotonic: float = field(default_factory=time.monotonic)
    finished_monotonic: Optional[float] = None
    worker: Optional[threading.Thread] = None
    worker_started: bool = False
    terminal_event: threading.Event = field(default_factory=threading.Event)
    phase: str = "discovery"

    def _touch_locked(self) -> None:
        self.last_activity_monotonic = time.monotonic()

    def _finish_locked(self) -> None:
        if self.finished_monotonic is None:
            self.finished_monotonic = time.monotonic()
        self.terminal_event.set()

    def cancel(self) -> None:
        with self.lock:
            if self.status in _DRAFT_TERMINAL_STATUSES:
                return
            if self.status == "finalizing":
                raise HTTPException(status_code=409, detail="Draft is finalizing")
            self.cancel_requested = True
            self._touch_locked()
            if self.status == "drafting":
                self.status = "canceling"

    def begin_commit(self) -> bool:
        """Claim the irreversible publication boundary for this draft."""
        with self.lock:
            if self.cancel_requested or self.status != "drafting":
                if self.status not in _DRAFT_TERMINAL_STATUSES:
                    if self.cancel_requested:
                        self.status = "canceled"
                        self._finish_locked()
                return False
            self.status = "finalizing"
            self.phase = "compiling"
            self._touch_locked()
            return True

    def complete(self, result: Dict[str, Any]) -> bool:
        """Publish one successful terminal outcome exactly once."""

        with self.lock:
            if self.cancel_requested:
                if self.status not in _DRAFT_TERMINAL_STATUSES:
                    self.status = "canceled"
                    self._finish_locked()
                return False
            if self.status != "finalizing":
                return False
            self.result = result
            self.error = ""
            self.status = "completed"
            self.phase = "complete"
            self._finish_locked()
            return True

    def fail(self, exc: BaseException | str) -> bool:
        """Resolve an active worker failure without leaving a zombie operation."""

        if isinstance(exc, BaseException):
            detail = str(exc).strip() or type(exc).__name__
        else:
            detail = str(exc).strip()
        with self.lock:
            if self.status in _DRAFT_TERMINAL_STATUSES:
                return False
            if self.cancel_requested:
                self.status = "canceled"
            else:
                self.error = detail or "Workflow drafting stopped unexpectedly."
                self.status = "failed"
            self._finish_locked()
            return True

    def ensure_terminal(self) -> None:
        """Close any worker path that unwound without declaring an outcome."""
        self.fail("Workflow drafting stopped before it produced a result. Please retry.")

    def on_progress(
        self,
        event_type: str,
        tool_name: str = "",
        preview: Any = None,
        _display_args: Any = None,
        **details: Any,
    ) -> None:
        """Keep the agent's existing display-ready progress, without tool data/results."""
        entry: Dict[str, Any] = {
            "type": str(event_type or "progress"),
            "tool": str(tool_name or ""),
        }
        if preview not in (None, ""):
            entry["text"] = str(preview)
        if details.get("duration") is not None:
            entry["duration"] = round(float(details["duration"]), 1)
        if details.get("is_error") is not None:
            entry["is_error"] = bool(details["is_error"])
        with self.lock:
            phase_map = {
                "draft.discovery": "discovery",
                "draft.completed": "complete",
            }
            self.phase = phase_map.get(str(event_type), self.phase)
            self.progress.append(entry)
            self.progress = self.progress[-50:]
            self._touch_locked()

    def attach_worker(self, worker: threading.Thread) -> None:
        with self.lock:
            self.worker = worker

    def mark_worker_started(self) -> None:
        with self.lock:
            self.worker_started = True
            self._touch_locked()

    def reconcile(self, now: Optional[float] = None) -> bool:
        """Fail closed when an active draft stops making forward progress."""

        current = time.monotonic() if now is None else now
        with self.lock:
            if self.status in _DRAFT_TERMINAL_STATUSES:
                return False
            worker = self.worker
            worker_stopped = (
                self.worker_started
                and worker is not None
                and not worker.is_alive()
            )
            idle_for = current - self.last_activity_monotonic
            runtime = current - self.created_monotonic
            timed_out = (
                self.status != "finalizing"
                and (
                    idle_for >= _DRAFT_INACTIVITY_SECONDS
                    or runtime >= _DRAFT_MAX_RUNTIME_SECONDS
                )
            )
            if not worker_stopped and not timed_out:
                return False
            canceled = self.cancel_requested

        detail = (
            "Workflow drafting stopped before it produced a result. Please retry."
            if worker_stopped
            else "Workflow drafting made no progress for too long. Please retry."
        )
        accepted = self.fail(detail)
        if accepted:
            log.warning(
                "Record draft operation %s %s",
                self.operation_id,
                "canceled after timeout" if canceled else "failed watchdog reconciliation",
            )
        return accepted


_DRAFT_OPERATIONS: Dict[str, _DraftOperation] = {}
_DRAFT_OPERATIONS_LOCK = threading.Lock()
_RECORDING_OPERATION_LOCKS: weakref.WeakValueDictionary[str, threading.RLock] = (
    weakref.WeakValueDictionary()
)
_RECORDING_OPERATION_LOCKS_GUARD = threading.Lock()
_DESKTOP_CAPTURE_AUTHORIZATIONS: Dict[str, Dict[str, Any]] = {}
_DESKTOP_CAPTURE_AUTHORIZATIONS_LOCK = threading.Lock()


def _recording_operation_lock(recording_dir: Path) -> threading.RLock:
    key = str(recording_dir.expanduser().resolve())
    with _RECORDING_OPERATION_LOCKS_GUARD:
        return _RECORDING_OPERATION_LOCKS.setdefault(key, threading.RLock())


def _draft_operation_payload(operation: _DraftOperation) -> Dict[str, Any]:
    operation.reconcile()
    with operation.lock:
        payload: Dict[str, Any] = {
            "operation_id": operation.operation_id,
            "recording_dir": str(operation.recording_dir),
            "status": operation.status,
            "progress": list(operation.progress),
            "phase": operation.phase,
        }
        if operation.status == "completed":
            payload["result"] = operation.result
        elif operation.status == "failed":
            payload["error"] = operation.error
        return payload


def _get_draft_operation(operation_id: str) -> _DraftOperation:
    with _DRAFT_OPERATIONS_LOCK:
        _cleanup_draft_operations_locked()
        operation = _DRAFT_OPERATIONS.get(operation_id)
    if operation is None:
        raise HTTPException(status_code=404, detail="Draft operation not found")
    return operation


def _draft_operation_active(recording_dir: Path) -> bool:
    """Return whether this recording has an active draft operation."""

    with _DRAFT_OPERATIONS_LOCK:
        _cleanup_draft_operations_locked()
        operations = [
            operation
            for operation in _DRAFT_OPERATIONS.values()
            if operation.recording_dir == recording_dir
        ]
    for operation in operations:
        operation.reconcile()
    return any(operation.status in _DRAFT_ACTIVE_STATUSES for operation in operations)


def _cancel_active_draft_operations(recording_dir: Path) -> bool:
    """Request cancellation without racing a draft's publication boundary."""

    with _DRAFT_OPERATIONS_LOCK:
        operations = [
            operation
            for operation in _DRAFT_OPERATIONS.values()
            if operation.recording_dir == recording_dir
        ]
    active = False
    for operation in operations:
        if operation.status in _DRAFT_ACTIVE_STATUSES:
            active = True
            operation.cancel()
    return active


def _cleanup_draft_operations_locked() -> None:
    """Bound retained polling results without dropping active work."""

    now = time.monotonic()
    expired = [
        operation_id
        for operation_id, operation in _DRAFT_OPERATIONS.items()
        if operation.status in _DRAFT_TERMINAL_STATUSES
        and operation.finished_monotonic is not None
        and now - operation.finished_monotonic >= _DRAFT_TERMINAL_RETENTION_SECONDS
    ]
    for operation_id in expired:
        _DRAFT_OPERATIONS.pop(operation_id, None)

    overflow = len(_DRAFT_OPERATIONS) - (_DRAFT_MAX_OPERATIONS - 1)
    if overflow <= 0:
        return
    terminal = sorted(
        (
            operation
            for operation in _DRAFT_OPERATIONS.values()
            if operation.status in _DRAFT_TERMINAL_STATUSES
        ),
        key=lambda operation: operation.finished_monotonic or operation.created_monotonic,
    )
    for operation in terminal[:overflow]:
        _DRAFT_OPERATIONS.pop(operation.operation_id, None)


def _register_draft_operation(operation: _DraftOperation) -> None:
    with _DRAFT_OPERATIONS_LOCK:
        _cleanup_draft_operations_locked()
        for existing in _DRAFT_OPERATIONS.values():
            if existing.recording_dir != operation.recording_dir:
                continue
            existing.reconcile()
            if existing.status in _DRAFT_ACTIVE_STATUSES:
                raise HTTPException(
                    status_code=409,
                    detail="Drafting is already in progress for this recording.",
                )
        if len(_DRAFT_OPERATIONS) >= _DRAFT_MAX_OPERATIONS:
            raise HTTPException(
                status_code=503,
                detail="Too many workflow drafts are active. Wait for one to finish, then retry.",
            )
        _DRAFT_OPERATIONS[operation.operation_id] = operation


def _watch_draft_operation(operation: _DraftOperation) -> None:
    """Monitor one worker independently of Desktop polling."""

    while not operation.terminal_event.wait(timeout=30):
        if operation.reconcile():
            return


class RecordDesktopCaptureAuthorizationRequest(BaseModel):
    profile: str = ""
    name: str = Field(min_length=1)
    instructions: str = Field(min_length=1)
    schedule: str = ""
    run_mode: str = "schedule"


class RecordDesktopRecordingStartRequest(
    RecordDesktopCaptureAuthorizationRequest
):
    capture_authorization_nonce: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    capture_authorization: Optional[Dict[str, Any]] = None


class RecordRecordingDiscardRequest(BaseModel):
    profile: str = ""


class RecordRecordingDraftOnlyRequest(BaseModel):
    profile: str = ""
    name: str = Field(min_length=1)
    instructions: str = Field(min_length=1)
    schedule: str = ""
    expected_draft_digest: Optional[str] = None
    review_thread_id: Optional[str] = None


class RecordReviewConversationRequest(BaseModel):
    profile: str = ""
    expected_draft_digest: str = Field(min_length=1)
    message: Optional[str] = Field(default=None, max_length=4000)


class RecordScheduleRecordingRequest(BaseModel):
    profile: str = ""
    recording_dir: str
    schedule: str = ""
    run_mode: str = "schedule"
    expected_draft_digest: str = Field(min_length=1)
    publish: bool = False


class RecordSetJobRequest(BaseModel):
    profile: str = ""
    recording_dir: str
    schedule: str = ""
    run_mode: str = "schedule"
    expected_draft_digest: str = Field(min_length=1)


def _review_thread_for_digest(conversation: dict[str, Any], draft_digest: str) -> dict[str, Any] | None:
    threads = conversation.get("threads") if isinstance(conversation.get("threads"), list) else []
    return next(
        (
            thread
            for thread in reversed(threads)
            if isinstance(thread, dict) and thread.get("draft_digest") == draft_digest
        ),
        None,
    )


def _public_review_thread(thread: dict[str, Any]) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    for message in thread.get("messages") if isinstance(thread.get("messages"), list) else []:
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
            continue
        content = message.get("display_content")
        if not isinstance(content, str) or not content.strip():
            if message.get("internal"):
                continue
            content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        payload = {"role": message["role"], "content": content.strip()}
        if isinstance(message.get("created_at"), str):
            payload["created_at"] = message["created_at"]
        messages.append(payload)
    return {
        "thread_id": thread.get("thread_id"),
        "draft_revision": thread.get("draft_revision"),
        "state": thread.get("state"),
        "messages": messages,
        "feedback_summary": str(thread.get("feedback_summary") or ""),
    }


def _script_review(workflow: dict[str, Any]) -> dict[str, str] | None:
    job_spec = workflow.get("job_spec") if isinstance(workflow.get("job_spec"), dict) else {}
    script = workflow.get("script_source") if isinstance(workflow.get("script_source"), dict) else None
    if script is None:
        return None
    path = job_spec.get("script")
    source = script.get("body")
    if (
        not isinstance(path, str)
        or not path.strip()
        or not isinstance(source, str)
        or not source.strip()
    ):
        return None
    source = source if source.endswith("\n") else source + "\n"
    return {
        "path": path.strip(),
        "source": source,
        "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
    }


def _review_requires_rerecord(blockers: list[dict[str, Any]]) -> bool:
    return any(
        isinstance(item, dict) and item.get("resolution") == "rerecord"
        for item in blockers
    )


def _thread_matches_current_blockers(
    thread: dict[str, Any],
    blockers: list[dict[str, Any]],
) -> bool:
    """Do not let an obsolete re-record response pin a recovered draft.

    Review threads are persisted with the draft digest. Product policy can
    legitimately reclassify a capture diagnostic after that thread was saved,
    so the live compiler blockers remain authoritative for re-recording.
    """

    return not (
        thread.get("state") == "needs_rerecord"
        and not _review_requires_rerecord(blockers)
    )


def _derived_review_state(
    *,
    workflow: dict[str, Any],
    draft_digest: str,
    conversation: dict[str, Any],
    compiler: Any,
) -> tuple[str, list[dict[str, Any]], dict[str, Any] | None]:
    blockers = compiler.review_blockers(workflow)
    thread = _review_thread_for_digest(conversation, draft_digest)
    if thread is not None and not _thread_matches_current_blockers(thread, blockers):
        thread = None
    if _review_requires_rerecord(blockers):
        return (
            "needs_rerecord",
            blockers,
            thread if thread is not None and thread.get("state") == "needs_rerecord" else None,
        )
    if thread is not None and thread.get("state") != "applied":
        return str(thread.get("state")), blockers, thread
    if blockers:
        return "needs_input", blockers, None
    return "ready_to_schedule", blockers, None


def _review_conversation_response(
    *,
    workflow: dict[str, Any],
    draft_digest: str,
    conversation: dict[str, Any],
    compiler: Any,
) -> dict[str, Any]:
    state, blockers, thread = _derived_review_state(
        workflow=workflow,
        draft_digest=draft_digest,
        conversation=conversation,
        compiler=compiler,
    )
    response = {
        "state": state,
        "needs_initial_turn": (
            thread is None and state == "needs_input"
        ),
        "blockers": blockers,
        "script_review": _script_review(workflow),
        "thread_id": thread.get("thread_id") if thread else None,
        "threads": [
            _public_review_thread(item)
            for item in conversation.get("threads", [])
            if isinstance(item, dict)
            and (
                item.get("draft_digest") == draft_digest
                or item.get("state") == "applied"
            )
            and _thread_matches_current_blockers(item, blockers)
        ],
    }
    return response


def _revision_context(
    *,
    workflow: dict[str, Any],
    draft_digest: str,
    draft_revision: int,
    thread: dict[str, Any],
) -> dict[str, Any]:
    review = workflow.get("review") if isinstance(workflow.get("review"), dict) else {}
    job_spec = workflow.get("job_spec") if isinstance(workflow.get("job_spec"), dict) else {}
    script = workflow.get("script_source") if isinstance(workflow.get("script_source"), dict) else {}
    operator_messages = [
        str(message.get("display_content") or message.get("content") or "").strip()
        for message in thread.get("messages", [])
        if isinstance(message, dict)
        and message.get("role") == "user"
        and str(message.get("display_content") or "").strip()
    ]
    return {
        "feedback_summary": str(thread.get("feedback_summary") or "").strip(),
        "operator_messages": operator_messages,
        "prior_digest": draft_digest,
        "prior_revision": draft_revision,
        "prior_plan": {
            "review_outline": list(review.get("execution_steps") or []),
            "trigger": workflow.get("trigger") if isinstance(workflow.get("trigger"), dict) else {},
            "job_spec": {
                "prompt": job_spec.get("prompt"),
                "skills": job_spec.get("skills"),
                "no_agent": job_spec.get("no_agent"),
                "script_path": script.get("path"),
            },
        },
    }


def _stage_artifacts(artifacts: list[tuple[Path, bytes]]) -> list[tuple[Path, Path]]:
    staged: list[tuple[Path, Path]] = []
    try:
        for target, content in artifacts:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            with temporary.open("wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            staged.append((target, temporary))
        return staged
    except Exception:
        for _target, temporary in staged:
            temporary.unlink(missing_ok=True)
        raise


def _restore_artifact(target: Path, previous: bytes | None) -> None:
    if previous is None:
        target.unlink(missing_ok=True)
        return
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.restore")
    temporary.write_bytes(previous)
    os.replace(temporary, target)


def _publish_staged_artifacts(staged: list[tuple[Path, Path]]) -> None:
    snapshots: list[tuple[Path, bytes | None]] = []
    published: list[Path] = []
    try:
        for target, temporary in staged:
            snapshots.append((target, target.read_bytes() if target.exists() else None))
            os.replace(temporary, target)
            published.append(target)
    except Exception:
        for target, previous in reversed(snapshots):
            if target in published:
                _restore_artifact(target, previous)
        raise
    finally:
        for _target, temporary in staged:
            temporary.unlink(missing_ok=True)


def _candidate_artifacts(candidate: Any, conversation: dict[str, Any] | None = None) -> list[tuple[Path, bytes]]:
    review_markdown = candidate.review_markdown
    build = candidate.workflow.get("build") if isinstance(candidate.workflow.get("build"), dict) else {}
    build_review = Path(str(build.get("build_dir") or "")) / "review.md"
    if build_review.is_file():
        review_markdown = build_review.read_text(encoding="utf-8")
    artifacts = [
        (candidate.output, (json.dumps(candidate.workflow, indent=2, sort_keys=True) + "\n").encode("utf-8")),
        (candidate.recording_dir / "draft-review.md", review_markdown.encode("utf-8")),
        (candidate.recording_dir / "metadata.json", (json.dumps(candidate.metadata, indent=2, sort_keys=True) + "\n").encode("utf-8")),
    ]
    if conversation is not None:
        artifacts.append((
            review_conversation_path(candidate.recording_dir),
            (json.dumps(conversation, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        ))
    return artifacts


def _review_seed(
    *,
    workflow: dict[str, Any],
    draft_digest: str,
    draft_revision: int,
    compiler: Any,
) -> str:
    review = workflow.get("review") if isinstance(workflow.get("review"), dict) else {}
    job_spec = workflow.get("job_spec") if isinstance(workflow.get("job_spec"), dict) else {}
    facts = {
        "draft_digest": draft_digest,
        "draft_revision": draft_revision,
        "workflow_name": str(workflow.get("name") or "")[:256],
        "workflow_instructions": str(workflow.get("instructions") or "")[:1200],
        "steps": review.get("execution_steps") if isinstance(review.get("execution_steps"), list) else [],
        "blockers": compiler.review_blockers(workflow),
        "trigger": workflow.get("trigger") if isinstance(workflow.get("trigger"), dict) else {},
        "job_spec": job_spec,
    }
    return json.dumps(facts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _cron_profile_home(profile: Optional[str]) -> Tuple[str, Path]:
    from hermes_plugins.record.record.runtime import plugin_profile

    active_name, active_home = plugin_profile()
    raw = str(profile or "").strip()
    if raw and raw.lower() != active_name:
        raise HTTPException(
            status_code=409,
            detail=(
                "Hermes changed profiles. Reopen Workflows from the "
                f"active Hermes backend ({active_name})."
            ),
        )
    return active_name, active_home


def _active_recording_payload(home: Path, profile_name: str) -> Dict[str, Any]:
    active = active_native_recording(home=home, profile_name=profile_name)
    if isinstance(active, dict) and active.get("recording_dir"):
        try:
            recording_dir = safe_recording_dir(home, str(active["recording_dir"]))
            metadata = read_json(recording_dir / "metadata.json")
        except HTTPException:
            # A corrupt live session cannot be resumed safely.
            pass
        else:
            return {
                "recording": {
                    **active,
                    "instructions": metadata.get("instructions") or active.get("instructions") or "",
                    "schedule": metadata.get("schedule") or "",
                    "run_mode": (
                        "webhook"
                        if metadata.get("trigger_requested") is True
                        else "schedule"
                    ),
                },
            }
    return {"recording": None}


def _mark_recording_scheduled(
    recording_dir: Path,
    job_id: Optional[str],
) -> None:
    scheduled_at = utc_now()
    for path in (recording_dir / "metadata.json", recording_dir / "session.json"):
        value = read_json(path)
        value["state"] = "scheduled"
        value["status"] = "scheduled"
        value["scheduled_at"] = scheduled_at
        if job_id:
            value["scheduled_job_id"] = job_id
        write_json(path, value)


@router.get("/recordings/active")
def active_recording(profile: str = ""):
    profile_name, home = _cron_profile_home(profile)
    return _active_recording_payload(home, profile_name)


@router.get("/webhook-status")
def get_webhook_status(profile: str = ""):
    _profile_name, home = _cron_profile_home(profile)
    modules = load_record_modules()
    try:
        return modules["hermes"].webhook_runtime_status(hermes_home=home)
    except Exception as exc:
        log.warning("Could not read Hermes webhook receiver status: %s", exc)
        return {
            "enabled": False,
            "configured": False,
            "ready": False,
            "reason": str(exc),
        }


def _start_mode(run_mode: str, schedule: str) -> tuple[str, str]:
    mode = str(run_mode or "schedule").strip().lower()
    if mode not in {"schedule", "webhook"}:
        raise HTTPException(
            status_code=400,
            detail="run_mode must be schedule or webhook",
        )
    normalized_schedule = str(schedule or "").strip()
    if mode == "schedule" and not normalized_schedule:
        raise HTTPException(
            status_code=400,
            detail="Choose a schedule or an event trigger before recording.",
        )
    return mode, "" if mode == "webhook" else normalized_schedule


def _desktop_capture_authorization_claim(
    *,
    profile: str,
    name: str,
    instructions: str,
    schedule: str,
    run_mode: str,
) -> Dict[str, Any]:
    return {
        "profile": profile,
        "name": name.strip(),
        "instructions": instructions.strip(),
        "schedule": schedule,
        "run_mode": run_mode,
    }


def _issue_desktop_capture_authorization(claim: Dict[str, Any]) -> str:
    nonce = secrets.token_hex(32)
    now = time.monotonic()
    with _DESKTOP_CAPTURE_AUTHORIZATIONS_LOCK:
        expired = [
            token
            for token, authorization in _DESKTOP_CAPTURE_AUTHORIZATIONS.items()
            if now - float(authorization.get("issued_monotonic") or 0)
            > _DESKTOP_CAPTURE_AUTHORIZATION_TTL_SECONDS
        ]
        for token in expired:
            _DESKTOP_CAPTURE_AUTHORIZATIONS.pop(token, None)
        if len(_DESKTOP_CAPTURE_AUTHORIZATIONS) >= _DESKTOP_CAPTURE_AUTHORIZATION_LIMIT:
            oldest = min(
                _DESKTOP_CAPTURE_AUTHORIZATIONS,
                key=lambda token: float(
                    _DESKTOP_CAPTURE_AUTHORIZATIONS[token].get("issued_monotonic")
                    or 0
                ),
            )
            _DESKTOP_CAPTURE_AUTHORIZATIONS.pop(oldest, None)
        _DESKTOP_CAPTURE_AUTHORIZATIONS[nonce] = {
            "issued_monotonic": now,
            "claim": dict(claim),
        }
    return nonce


def _consume_desktop_capture_authorization(
    nonce: str,
    claim: Dict[str, Any],
) -> None:
    now = time.monotonic()
    with _DESKTOP_CAPTURE_AUTHORIZATIONS_LOCK:
        authorization = _DESKTOP_CAPTURE_AUTHORIZATIONS.pop(nonce, None)
    if (
        not authorization
        or now - float(authorization.get("issued_monotonic") or 0)
        > _DESKTOP_CAPTURE_AUTHORIZATION_TTL_SECONDS
        or authorization.get("claim") != claim
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "Desktop Chrome capture authorization expired, was already "
                "used, or does not match this recording."
            ),
        )


@router.post("/recordings/capture-authorization")
def provision_desktop_capture_authorization(
    body: RecordDesktopCaptureAuthorizationRequest,
):
    run_mode, schedule = _start_mode(body.run_mode, body.schedule)
    profile_name, _home = _cron_profile_home(body.profile)
    claim = _desktop_capture_authorization_claim(
        profile=profile_name,
        name=body.name,
        instructions=body.instructions,
        schedule=schedule,
        run_mode=run_mode,
    )
    nonce = _issue_desktop_capture_authorization(claim)
    return {
        "source": "desktop",
        "profile": profile_name,
        "capture_authorization_nonce": nonce,
    }


@router.post("/recordings/start-desktop")
def start_desktop_recording(body: RecordDesktopRecordingStartRequest):
    run_mode, schedule = _start_mode(body.run_mode, body.schedule)
    profile_name, home = _cron_profile_home(body.profile)
    authorization = (
        body.capture_authorization
        if isinstance(body.capture_authorization, dict)
        else {}
    )
    if (
        authorization.get("source") != "desktop"
        or authorization.get("profile") != profile_name
        or authorization.get("capture_authorization_nonce")
        != body.capture_authorization_nonce
    ):
        raise HTTPException(
            status_code=409,
            detail="Desktop Chrome capture authorization does not match.",
        )
    _consume_desktop_capture_authorization(
        body.capture_authorization_nonce,
        _desktop_capture_authorization_claim(
            profile=profile_name,
            name=body.name,
            instructions=body.instructions,
            schedule=schedule,
            run_mode=run_mode,
        ),
    )
    return start_native_recording(
        home=home,
        profile_name=profile_name,
        workflow_id=record_slug(body.name, "workflow"),
        name=body.name.strip(),
        instructions=body.instructions.strip(),
        schedule=schedule,
        capture_authorization_nonce=body.capture_authorization_nonce,
        trigger_requested=run_mode == "webhook",
    )


@router.post("/recordings/{recording_key:path}/stop-desktop")
def stop_recording_from_desktop(
    recording_key: str,
    body: RecordRecordingDiscardRequest,
):
    _profile_name, home = _cron_profile_home(body.profile)
    recording_dir = resolve_recording_ref(home, recording_key)
    return stop_desktop_recording(
        home=home,
        recording_dir=recording_dir,
    )


@router.post("/recordings/{recording_key:path}/discard")
def discard_recording(recording_key: str, body: RecordRecordingDiscardRequest):
    _profile_name, home = _cron_profile_home(body.profile)
    recording_dir = resolve_recording_ref(home, recording_key, require_bundle=False)
    # Do not delete or mutate a bundle while its drafter is reading/staging it.
    # Cancellation is deliberately separate from the final dismissal request so
    # the worker can release its publication lock without a request deadlock.
    if _cancel_active_draft_operations(recording_dir):
        raise HTTPException(
            status_code=409,
            detail="Drafting is being canceled. Wait for cancellation to finish, then discard the recording.",
        )
    with _recording_operation_lock(recording_dir):
        if _draft_operation_active(recording_dir):
            raise HTTPException(status_code=409, detail="Drafting is still finishing cancellation.")
        try:
            state = load_recording_status(home=home, recording_dir=recording_dir)["status"]
        except HTTPException:
            raise HTTPException(
                status_code=409,
                detail="This recording is damaged and cannot be discarded from the workflow page.",
            )
        if state == "scheduled":
            raise HTTPException(status_code=409, detail="A scheduled workflow cannot be discarded.")
        if state == "dismissed":
            return {"status": "dismissed"}
        return discard_native_recording(home=home, recording_dir=recording_dir)


@router.get("/recordings/{recording_key:path}/review-conversation")
def get_review_conversation(
    recording_key: str,
    profile: str = "",
    expected_draft_digest: str = "",
):
    modules = load_record_modules()
    profile_name, home = _cron_profile_home(profile)
    recording_dir = resolve_recording_ref(home, recording_key)
    with _recording_operation_lock(recording_dir):
        workflow_path = recording_dir / "workflow.json"
        if not workflow_path.is_file():
            raise HTTPException(status_code=404, detail="Draft workflow not found")
        workflow = read_json(workflow_path)
        draft_digest = workflow_digest(workflow)
        if not expected_draft_digest or expected_draft_digest != draft_digest:
            raise HTTPException(status_code=409, detail="The reviewed draft changed. Reload the workflow review.")
        return _review_conversation_response(
            workflow=workflow,
            draft_digest=draft_digest,
            conversation=load_review_conversation(recording_dir),
            compiler=modules["compiler"],
        )


@router.post("/recordings/{recording_key:path}/review-conversation")
def post_review_conversation(recording_key: str, body: RecordReviewConversationRequest):
    modules = load_record_modules()
    profile_name, home = _cron_profile_home(body.profile)
    recording_dir = resolve_recording_ref(home, recording_key)
    with _recording_operation_lock(recording_dir):
        if _draft_operation_active(recording_dir):
            raise HTTPException(status_code=409, detail="A draft operation is already in progress.")
        workflow_path = recording_dir / "workflow.json"
        if not workflow_path.is_file():
            raise HTTPException(status_code=404, detail="Draft workflow not found")
        workflow = read_json(workflow_path)
        draft_digest = workflow_digest(workflow)
        if body.expected_draft_digest != draft_digest:
            raise HTTPException(status_code=409, detail="The reviewed draft changed. Reload the workflow review.")
        review = workflow.get("review") if isinstance(workflow.get("review"), dict) else {}
        draft_revision = int(review.get("draft_revision") or 1)
        conversation = load_review_conversation(recording_dir)
        state, _blockers, thread = _derived_review_state(
            workflow=workflow,
            draft_digest=draft_digest,
            conversation=conversation,
            compiler=modules["compiler"],
        )
        needs_initial_turn = (
            thread is None and state == "needs_input"
        )
        message = body.message.strip() if isinstance(body.message, str) else None
        if body.message is not None and not message:
            raise HTTPException(status_code=400, detail="Review message must contain text.")
        if message is None:
            if thread is not None or not needs_initial_turn:
                raise HTTPException(status_code=409, detail="An initial review turn is not pending.")
        elif state == "ready_to_redraft":
            raise HTTPException(status_code=409, detail="Update the draft or discard the pending feedback first.")
        elif state == "needs_rerecord" and not needs_initial_turn:
            raise HTTPException(status_code=409, detail="This recording must be recorded again before it can change.")

        is_new_thread = thread is None
        now = utc_now()
        if thread is None:
            seed = _review_seed(
                workflow=workflow,
                draft_digest=draft_digest,
                draft_revision=draft_revision,
                compiler=modules["compiler"],
            )
            content = seed if message is None else seed + "\n\nOperator message:\n" + message
            thread = {
                "thread_id": uuid.uuid4().hex,
                "draft_digest": draft_digest,
                "draft_revision": draft_revision,
                "state": "needs_input",
                "messages": [{
                    "role": "user",
                    "content": content,
                    "display_content": message or "",
                    "internal": message is None,
                    "created_at": now,
                }],
                "open_questions": [],
                "feedback_summary": "",
                "applied_to_digest": None,
                "created_at": now,
                "updated_at": now,
            }
            history = []
            operator_message = content
        else:
            history = list(thread.get("messages") or [])
            operator_message = message or ""

        try:
            result = modules["drafting"].clarify_workflow(
                workflow=workflow,
                draft_digest=draft_digest,
                conversation_history=history,
                user_message=operator_message,
                home=home,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if message is not None and not is_new_thread:
            thread["messages"].append({
                "role": "user",
                "content": message,
                "display_content": message,
                "created_at": now,
            })
        thread["messages"].append({
            "role": "assistant",
            "content": result["reply"],
            "display_content": result["reply"],
            "created_at": utc_now(),
        })
        thread["state"] = result["state"]
        thread["open_questions"] = result["open_questions"]
        thread["feedback_summary"] = result["feedback_summary"]
        thread["updated_at"] = utc_now()
        if not any(item is thread for item in conversation.get("threads", [])):
            conversation.setdefault("threads", []).append(thread)
        save_review_conversation(recording_dir, conversation)
        return _review_conversation_response(
            workflow=workflow,
            draft_digest=draft_digest,
            conversation=conversation,
            compiler=modules["compiler"],
        )


@router.post("/recordings/{recording_key:path}/draft")
def draft_recording_only(recording_key: str, body: RecordRecordingDraftOnlyRequest):
    modules = load_record_modules()
    profile_name, home = _cron_profile_home(body.profile)
    recording_dir = resolve_recording_ref(home, recording_key)
    current = load_recording_status(home=home, recording_dir=recording_dir)
    if current.get("status") == "recording":
        raise HTTPException(status_code=409, detail="Stop the recording before drafting.")
    if body.review_thread_id and not body.expected_draft_digest:
        raise HTTPException(
            status_code=400,
            detail="expected_draft_digest is required with review_thread_id.",
        )

    def validated_revision() -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
        if not body.expected_draft_digest:
            return None, None, None
        workflow_path = recording_dir / "workflow.json"
        if not workflow_path.is_file():
            raise HTTPException(status_code=409, detail="The reviewed draft no longer exists.")
        workflow = read_json(workflow_path)
        digest = workflow_digest(workflow)
        if digest != body.expected_draft_digest:
            raise HTTPException(status_code=409, detail="The reviewed draft changed. Reload before updating it.")
        conversation = load_review_conversation(recording_dir)
        if not body.review_thread_id:
            return workflow, None, None
        thread = _review_thread_for_digest(conversation, digest)
        if thread is None or thread.get("thread_id") != body.review_thread_id or thread.get("state") != "ready_to_redraft":
            raise HTTPException(status_code=409, detail="The requested feedback is no longer ready to update.")
        if any(
            item.get("resolution") == "rerecord"
            for item in modules["compiler"].review_blockers(workflow)
            if isinstance(item, dict)
        ):
            raise HTTPException(status_code=409, detail="This recording is missing replay evidence and must be recorded again.")
        return workflow, conversation, thread

    with _recording_operation_lock(recording_dir):
        source_workflow, source_conversation, source_thread = validated_revision()
        workflow_path = recording_dir / "workflow.json"
        baseline_workflow = source_workflow
        if baseline_workflow is None and workflow_path.is_file():
            baseline_workflow = read_json(workflow_path)
        baseline_digest = (
            workflow_digest(baseline_workflow)
            if baseline_workflow is not None
            else ""
        )
        baseline_review = (
            baseline_workflow.get("review")
            if baseline_workflow is not None
            and isinstance(baseline_workflow.get("review"), dict)
            else {}
        )
        previous_revision = int(baseline_review.get("draft_revision") or 0)
        if _draft_operation_active(recording_dir):
            raise HTTPException(status_code=409, detail="Drafting is already in progress for this recording.")
        operation = _DraftOperation(
            operation_id=uuid.uuid4().hex,
            recording_dir=recording_dir,
        )
        _register_draft_operation(operation)

    def run_draft() -> None:
        staged: list[tuple[Path, Path]] = []
        try:
            revision_context: dict[str, Any] | None = None
            source_digest = ""
            if source_workflow is not None and source_thread is not None:
                review = source_workflow.get("review") if isinstance(source_workflow.get("review"), dict) else {}
                source_digest = workflow_digest(source_workflow)
                revision_context = _revision_context(
                    workflow=source_workflow,
                    draft_digest=source_digest,
                    draft_revision=int(review.get("draft_revision") or 1),
                    thread=source_thread,
                )

            def author(context: dict[str, Any]) -> dict[str, Any]:
                author_context = dict(context)
                if body.schedule.strip():
                    author_context["requested_schedule"] = body.schedule.strip()
                return modules["drafting"].author_workflow(
                    body.instructions.strip(),
                    author_context,
                    recording_dir=recording_dir,
                    home=home,
                    on_progress=operation.on_progress,
                    revision_context=revision_context,
                )

            candidate = modules["recording"].build_draft_candidate(
                recording_dir,
                output=workflow_path,
                name=body.name.strip(),
                instructions=body.instructions.strip(),
                workflow_author=author,
            )
            candidate.metadata["profile"] = profile_name
            if body.schedule.strip():
                candidate.metadata["schedule"] = body.schedule.strip()
            candidate_review = candidate.workflow.get("review") if isinstance(candidate.workflow.get("review"), dict) else {}
            candidate_review["draft_revision"] = previous_revision + 1
            if source_digest:
                candidate_review["source_draft_digest"] = source_digest
                candidate_review["source_draft_revision"] = previous_revision
            candidate.workflow["review"] = candidate_review
            candidate.metadata["draft_revision"] = candidate_review["draft_revision"]
            candidate_review["source_digest"] = _recording_digest(recording_dir)
            prepared = prepare_recording_build(
                modules,
                home=home,
                profile_name=profile_name,
                recording_dir=recording_dir,
                workflow=candidate.workflow,
            )
            candidate.workflow["build"] = {
                "build_dir": prepared["build_dir"],
                "review_envelope_sha256": prepared["review_envelope_sha256"],
            }
            next_digest = workflow_digest(candidate.workflow)

            conversation_to_publish: dict[str, Any] | None = None
            if source_conversation is not None and source_thread is not None:
                conversation_to_publish = json.loads(json.dumps(source_conversation))
                thread = _review_thread_for_digest(conversation_to_publish, source_digest)
                if thread is None or thread.get("thread_id") != body.review_thread_id:
                    raise HTTPException(status_code=409, detail="The requested feedback changed before publication.")
                thread["state"] = "applied"
                thread["applied_to_digest"] = next_digest
                thread["updated_at"] = utc_now()

            staged = _stage_artifacts(_candidate_artifacts(candidate, conversation_to_publish))
            with _recording_operation_lock(recording_dir):
                current_workflow, current_conversation, current_thread = validated_revision()
                if source_digest and (
                    current_workflow is None
                    or workflow_digest(current_workflow) != source_digest
                    or current_thread is None
                    or current_thread.get("thread_id") != body.review_thread_id
                ):
                    raise HTTPException(status_code=409, detail="The reviewed draft changed before publication.")
                if not source_digest and baseline_digest:
                    if not workflow_path.is_file() or workflow_digest(read_json(workflow_path)) != baseline_digest:
                        raise HTTPException(status_code=409, detail="The existing draft changed before publication.")
                if not operation.begin_commit():
                    return
                # Migrate a valid legacy capture seal before publishing the
                # draft's mutable metadata/session lifecycle projections.
                modules["recording"].seal_draft_revision(recording_dir)
                _publish_staged_artifacts(staged)

            draft = {
                "recording_dir": str(recording_dir),
                "workflow": candidate.workflow,
                "draft_digest": next_digest,
                "review_blockers": modules["compiler"].review_blockers(candidate.workflow),
            }
            operation.complete(draft)
        except BaseException as exc:
            accepted = operation.fail(exc)
            if accepted and not operation.cancel_requested:
                log.exception("Record recording draft failed")
        finally:
            for _target, temporary in staged:
                temporary.unlink(missing_ok=True)
            operation.ensure_terminal()

    def launch_worker() -> None:
        worker = threading.Thread(
            target=run_draft,
            daemon=True,
            name=f"record-draft-{operation.operation_id[:8]}",
        )
        operation.attach_worker(worker)
        try:
            worker.start()
        except BaseException as exc:
            operation.fail(exc)
            log.exception("Could not start Record drafting worker")
        else:
            operation.mark_worker_started()

    launch_worker()
    try:
        threading.Thread(
            target=_watch_draft_operation,
            args=(operation,),
            daemon=True,
            name=f"record-watch-{operation.operation_id[:8]}",
        ).start()
    except BaseException:
        # Status polling also reconciles this operation; failure to create the
        # convenience watcher must not invalidate a live worker.
        log.exception("Could not start Record drafting watchdog")
    return _draft_operation_payload(operation)


@router.get("/drafts/{operation_id}")
def draft_operation_status(operation_id: str):
    return _draft_operation_payload(_get_draft_operation(operation_id))


@router.post("/drafts/{operation_id}/cancel")
def cancel_draft_operation(operation_id: str):
    operation = _get_draft_operation(operation_id)
    operation.cancel()
    return _draft_operation_payload(operation)


@router.post("/recordings/schedule")
def schedule_recording(body: RecordScheduleRecordingRequest):
    modules = load_record_modules()
    profile_name, home = _cron_profile_home(body.profile)
    recording_dir = safe_recording_dir(home, body.recording_dir)
    with _recording_operation_lock(recording_dir):
        return _schedule_recording_locked(
            body,
            modules=modules,
            profile_name=profile_name,
            home=home,
            recording_dir=recording_dir,
        )


@router.post("/recordings/set-job")
def set_recording_as_hermes_job(body: RecordSetJobRequest):
    run_mode, schedule = _start_mode(body.run_mode, body.schedule)
    modules = load_record_modules()
    profile_name, home = _cron_profile_home(body.profile)
    recording_dir = safe_recording_dir(home, body.recording_dir)
    request = RecordScheduleRecordingRequest(
        profile=profile_name,
        recording_dir=str(recording_dir),
        schedule=schedule if run_mode == "schedule" else "",
        run_mode=run_mode,
        expected_draft_digest=body.expected_draft_digest,
        publish=True,
    )
    with _recording_operation_lock(recording_dir):
        return _schedule_recording_locked(
            request,
            modules=modules,
            profile_name=profile_name,
            home=home,
            recording_dir=recording_dir,
        )


def _schedule_recording_locked(
    body: RecordScheduleRecordingRequest,
    *,
    modules: Dict[str, Any],
    profile_name: str,
    home: Path,
    recording_dir: Path,
):
    if _draft_operation_active(recording_dir):
        raise HTTPException(status_code=409, detail="A draft operation is already in progress.")
    run_mode, schedule = _start_mode(body.run_mode, body.schedule)

    workflow_path = recording_dir / "workflow.json"
    if not workflow_path.is_file():
        raise HTTPException(status_code=404, detail="Draft workflow not found")
    workflow = read_json(workflow_path)
    if workflow_digest(workflow) != body.expected_draft_digest:
        raise HTTPException(status_code=409, detail="The reviewed draft changed. Reload the workflow review.")
    blockers = modules["compiler"].review_blockers(workflow)
    if blockers:
        raise HTTPException(status_code=409, detail="Resolve every review detail before setting this job.")
    conversation = load_review_conversation(recording_dir)
    state, _current_blockers, _thread = _derived_review_state(
        workflow=workflow,
        draft_digest=body.expected_draft_digest,
        conversation=conversation,
        compiler=modules["compiler"],
    )
    if state != "ready_to_schedule":
        raise HTTPException(status_code=409, detail="Finish the workflow review before setting this job.")

    if run_mode == "webhook":
        trigger = workflow.get("trigger") if isinstance(workflow.get("trigger"), dict) else {}
        if trigger.get("feasibility") != "supported":
            raise HTTPException(
                status_code=409,
                detail=str(
                    trigger.get("reason")
                    or "This recording does not contain a supported Hermes webhook trigger."
                ),
            )

    if run_mode == "webhook" and body.publish:
        try:
            webhook_status = modules["hermes"].webhook_runtime_status(
                hermes_home=home
            )
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not webhook_status.get("ready"):
            raise HTTPException(
                status_code=409,
                detail=str(
                    webhook_status.get("reason")
                    or "The webhook receiver is not ready for external events."
                ),
            )

    try:
        draft = load_recording_build(
            modules,
            home=home,
            profile_name=profile_name,
            recording_dir=recording_dir,
            schedule=schedule if run_mode == "schedule" else "",
            run_mode=run_mode,
            expected_draft_digest=body.expected_draft_digest,
        )
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("Record could not load the reviewed Hermes job build")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response = dict(draft)
    response["install"] = None
    if not body.publish:
        response["run_mode"] = run_mode
        response["status"] = "review_ready"
        return response
    expected_review_envelope_sha256 = str(
        draft.get("review_envelope_sha256") or ""
    )
    try:
        install = publish_record_build(
            modules,
            home=home,
            profile_name=profile_name,
            build_dir=Path(str(draft["build_dir"])),
            note="Reviewed and set as a Hermes job from Workflows",
            expected_review_envelope_sha256=expected_review_envelope_sha256,
            webhook_trigger=(
                draft["workflow"].get("trigger")
                if run_mode == "webhook"
                and isinstance(draft["workflow"].get("trigger"), dict)
                else None
            ),
        )
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("Record recording schedule failed during install")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response["install"] = install
    native = install.get("install") if isinstance(install.get("install"), dict) else {}
    response["job_id"] = native.get("job_id")
    response["run_mode"] = run_mode
    response["webhook"] = install.get("webhook")
    response["cron_url"] = f"/cron?profile={profile_name}"
    response["status"] = "installed"
    try:
        _mark_recording_scheduled(recording_dir, response["job_id"])
    except Exception:
        log.exception("Record workflow installed but its recording lifecycle marker could not be updated")
    return response
