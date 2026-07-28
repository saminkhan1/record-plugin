"""Hermes Crunch dashboard plugin backend.

Mounted by Hermes under /api/plugins/hermes-crunch/.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import sys
import tempfile
import threading
import time
import uuid
import weakref
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from crunch_recorder import (  # noqa: E402
    _recording_digest,
    compile_reviewed_recording,
    crunch_install_readiness,
    crunch_slug,
    install_crunch_build,
    load_crunch_modules,
    load_review_conversation,
    prepare_install_authorization,
    resolve_default_approval_target,
    review_conversation_path,
    save_review_conversation,
    workflow_digest,
)
from hermes_recorder import (  # noqa: E402
    active_native_recording,
    delete_recording_bundle,
    discard_native_recording,
    recording_status as load_recording_status,
    start_native_recording,
    stop_native_recording,
)
from recording_bundle import (  # noqa: E402
    enumerate_recording_bundles,
    read_json,
    recording_bundle_diagnostic,
    recording_key as recording_bundle_key,
    recordings_root,
    resolve_recording_ref,
    safe_recording_dir,
    write_json,
)
from hermes_crunch.trace_schema import utc_now  # noqa: E402

router = APIRouter()
log = logging.getLogger(__name__)

_DRAFT_ACTIVE_STATUSES = frozenset({"drafting", "canceling", "finalizing"})
_DRAFT_TERMINAL_STATUSES = frozenset({"completed", "failed", "canceled"})
_DRAFT_INACTIVITY_SECONDS = 10 * 60
_DRAFT_MAX_RUNTIME_SECONDS = 30 * 60
_DRAFT_TERMINAL_RETENTION_SECONDS = 10 * 60
_DRAFT_MAX_OPERATIONS = 64


@dataclass
class _DraftOperation:
    operation_id: str
    recording_dir: Path
    status: str = "drafting"
    result: Optional[Dict[str, Any]] = None
    error: str = ""
    agent: Any = None
    cancel_requested: bool = False
    progress: List[Dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    created_monotonic: float = field(default_factory=time.monotonic)
    last_activity_monotonic: float = field(default_factory=time.monotonic)
    finished_monotonic: Optional[float] = None
    worker: Optional[threading.Thread] = None
    worker_started: bool = False
    terminal_event: threading.Event = field(default_factory=threading.Event)

    def _touch_locked(self) -> None:
        self.last_activity_monotonic = time.monotonic()

    def _finish_locked(self) -> None:
        if self.finished_monotonic is None:
            self.finished_monotonic = time.monotonic()
        self.terminal_event.set()

    def attach_agent(self, agent: Any) -> None:
        with self.lock:
            should_interrupt = (
                self.cancel_requested or self.status in _DRAFT_TERMINAL_STATUSES
            )
            if not should_interrupt:
                self.agent = agent
                self._touch_locked()
        if should_interrupt:
            agent.interrupt("Workflow drafting canceled by the operator")

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
            agent = self.agent
        if agent is not None:
            self._interrupt_agent(
                agent,
                "Workflow drafting canceled by the operator",
            )

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
            self.agent = None
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
            self.agent = None
            self._finish_locked()
            return True

    def ensure_terminal(self) -> None:
        """Close any worker path that unwound without declaring an outcome."""

        self.fail("Workflow drafting stopped before it produced a result. Please retry.")

    def detach_agent(self) -> None:
        with self.lock:
            self.agent = None

    @staticmethod
    def _interrupt_agent(agent: Any, reason: str) -> None:
        try:
            agent.interrupt(reason)
        except Exception:
            log.debug("Could not interrupt Crunch drafter", exc_info=True)

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
            self.progress.append(entry)
            self.progress = self.progress[-50:]
            self._touch_locked()

    def on_interim(self, text: str, **_details: Any) -> None:
        """Forward real mid-turn assistant commentary already emitted by Hermes."""
        visible = str(text or "").strip()
        if not visible:
            return
        with self.lock:
            self.progress.append({"type": "assistant.message", "text": visible})
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
            agent = self.agent
            canceled = self.cancel_requested

        if agent is not None:
            self._interrupt_agent(
                agent,
                "Workflow drafting timed out before it produced a result",
            )
        detail = (
            "Workflow drafting stopped before it produced a result. Please retry."
            if worker_stopped
            else "Workflow drafting made no progress for too long. Please retry."
        )
        accepted = self.fail(detail)
        if accepted:
            log.warning(
                "Crunch draft operation %s %s",
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
    """Monitor one worker independently of dashboard polling."""

    while not operation.terminal_event.wait(timeout=30):
        if operation.reconcile():
            return


class CrunchRecordingStartRequest(BaseModel):
    profile: str = "default"
    name: str = Field(min_length=1)
    instructions: str = Field(min_length=1)
    schedule: str = ""
    run_mode: str = "schedule"
    capture_authorization_nonce: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    capture_dashboard_origin: str = Field(min_length=1, max_length=256)


def _capture_request_dashboard_origin(request: Request) -> str:
    """Bind a capture start to this loopback server's actual browser origin."""

    parsed = None
    try:
        parsed = urlsplit(str(request.url))
        hostname = str(parsed.hostname or "").lower()
        port = parsed.port
        server = request.scope.get("server")
        bound_port = (
            int(server[1])
            if isinstance(server, (list, tuple)) and len(server) >= 2
            else None
        )
        client_host = str(request.client.host if request.client else "")
        client_is_loopback = ipaddress.ip_address(client_host).is_loopback
    except (IndexError, TypeError, ValueError):
        hostname = ""
        port = None
        bound_port = None
        client_is_loopback = False
    if (
        parsed is None
        or parsed.scheme != "http"
        or hostname not in {"127.0.0.1", "localhost", "::1"}
        or port is None
        or bound_port is None
        or port != bound_port
        or not client_is_loopback
    ):
        raise HTTPException(
            status_code=403,
            detail="Recording must be started from the local Hermes Workflows page.",
        )
    canonical_host = f"[{hostname}]" if ":" in hostname else hostname
    server_origin = f"http://{canonical_host}:{port}"
    request_origin = str(request.headers.get("origin") or "").strip()
    fetch_site = str(request.headers.get("sec-fetch-site") or "").strip().lower()
    if request_origin != server_origin or fetch_site not in {"", "same-origin"}:
        raise HTTPException(
            status_code=403,
            detail="Recording must be started from the active Hermes Workflows page.",
        )
    return server_origin


class CrunchRecordingDiscardRequest(BaseModel):
    profile: str = "default"


class CrunchRecordingStopRequest(CrunchRecordingDiscardRequest):
    chrome_finalize_ok: Optional[bool] = None
    chrome_finalize_error: str = Field(default="", max_length=500)


class CrunchRecordingDraftOnlyRequest(BaseModel):
    profile: str = "default"
    name: str = Field(min_length=1)
    instructions: str = Field(min_length=1)
    expected_draft_digest: Optional[str] = None
    review_thread_id: Optional[str] = None


class CrunchReviewConversationRequest(BaseModel):
    profile: str = "default"
    expected_draft_digest: str = Field(min_length=1)
    message: Optional[str] = Field(default=None, max_length=4000)


class CrunchScheduleRecordingRequest(BaseModel):
    profile: str = "default"
    recording_dir: str
    schedule: str = ""
    run_mode: str = "schedule"
    expected_draft_digest: str = Field(min_length=1)
    authorization: Optional[Dict[str, Any]] = None
    approve: bool = False
    expected_approval_envelope_sha256: Optional[str] = None


def _default_approval_target(*, home: Path) -> tuple[str, dict]:
    """Compatibility wrapper around the canonical Crunch install resolver."""

    return resolve_default_approval_target(home=home)


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
    home: Path | None = None,
    include_install_readiness: bool = True,
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
        "effective_approval_boundaries": compiler.effective_approval_boundaries(workflow),
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
    if home is not None and include_install_readiness:
        execution = (
            workflow.get("execution")
            if isinstance(workflow.get("execution"), dict)
            else {}
        )
        install_readiness = crunch_install_readiness(
            home=home,
            authorization=execution.get("authorization"),
            run_mode=str(workflow.get("run_mode") or "schedule"),
        )
        response["install_readiness"] = install_readiness
        response["approval_route_ready"] = install_readiness[
            "approval_route_ready"
        ]
        response["webhook_runtime"] = install_readiness["webhook_runtime"]
    return response


def _revision_context(
    *,
    workflow: dict[str, Any],
    draft_digest: str,
    draft_revision: int,
    thread: dict[str, Any],
) -> dict[str, Any]:
    review = workflow.get("review") if isinstance(workflow.get("review"), dict) else {}
    execution = workflow.get("execution") if isinstance(workflow.get("execution"), dict) else {}
    script = execution.get("script") if isinstance(execution.get("script"), dict) else {}
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
            "approval_steps": [
                dict(item)
                for item in review.get("approval_steps") or []
                if isinstance(item, dict)
            ],
            "trigger": workflow.get("trigger") if isinstance(workflow.get("trigger"), dict) else {},
            "execution": {
                "prompt": execution.get("prompt"),
                "requirements": execution.get("requirements"),
                "authorization": execution.get("authorization"),
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
    artifacts = [
        (candidate.output, (json.dumps(candidate.workflow, indent=2, sort_keys=True) + "\n").encode("utf-8")),
        (candidate.recording_dir / "draft-review.md", candidate.review_markdown.encode("utf-8")),
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
    execution = workflow.get("execution") if isinstance(workflow.get("execution"), dict) else {}
    requirements = execution.get("requirements") if isinstance(execution.get("requirements"), dict) else {}
    facts = {
        "draft_digest": draft_digest,
        "draft_revision": draft_revision,
        "workflow_name": str(workflow.get("name") or "")[:256],
        "workflow_instructions": str(workflow.get("instructions") or "")[:1200],
        "steps": review.get("execution_steps") if isinstance(review.get("execution_steps"), list) else [],
        "effective_approval_boundaries": compiler.effective_approval_boundaries(workflow),
        "blockers": compiler.review_blockers(workflow),
        "trigger": workflow.get("trigger") if isinstance(workflow.get("trigger"), dict) else {},
        "required_apps": requirements.get("apps") if isinstance(requirements.get("apps"), list) else [],
        "requirements": {
            key: requirements.get(key)
            for key in ("commands", "network_origins", "apps", "files")
            if isinstance(requirements.get(key), list)
        },
    }
    return json.dumps(facts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _cron_profile_home(profile: Optional[str]) -> Tuple[str, Path]:
    from hermes_cli import profiles as profiles_mod

    raw = (profile or "default").strip() or "default"
    try:
        canon = profiles_mod.normalize_profile_name(raw)
        profiles_mod.validate_profile_name(canon)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not profiles_mod.profile_exists(canon):
        raise HTTPException(status_code=404, detail=f"Profile '{canon}' does not exist.")
    return canon, profiles_mod.get_profile_dir(canon)


def _resume_recording_payload(home: Path, profile_name: str) -> Optional[Dict[str, Any]]:
    bundle_diagnostics = enumerate_recording_bundles(home)
    diagnostics_by_key = {
        str(item.get("recording_key") or ""): item
        for item in bundle_diagnostics
        if isinstance(item, dict)
    }

    def mark_recovery_error(recording_dir: Path, error: str) -> None:
        diagnostic = diagnostics_by_key.get(
            recording_bundle_key(home, recording_dir)
        )
        if diagnostic is None:
            return
        diagnostic["healthy"] = False
        errors = diagnostic.get("errors")
        if not isinstance(errors, list):
            errors = []
            diagnostic["errors"] = errors
        if error not in errors:
            errors.append(error)

    active = active_native_recording(home=home, profile_name=profile_name)
    if isinstance(active, dict) and active.get("recording_dir"):
        try:
            recording_dir = safe_recording_dir(home, str(active["recording_dir"]))
            metadata = read_json(recording_dir / "metadata.json")
        except HTTPException:
            # A stale/corrupt active pointer is one bundle diagnostic, not an
            # endpoint-wide failure. Continue to the healthy stopped captures.
            pass
        else:
            return {
                "recording": {
                    **active,
                    "instructions": metadata.get("instructions") or active.get("instructions") or "",
                    "schedule": metadata.get("schedule") or "",
                    "run_mode": "webhook" if metadata.get("trigger_requested") is True else "schedule",
                },
                "draft": None,
                "bundle_diagnostics": bundle_diagnostics,
            }

    candidates = []
    for recording_dir in recordings_root(home).glob("*/*"):
        metadata_path = recording_dir / "metadata.json"
        session_path = recording_dir / "session.json"
        if not metadata_path.is_file() or not session_path.is_file():
            continue
        try:
            metadata = read_json(metadata_path)
            session = read_json(session_path)
        except HTTPException:
            continue
        if metadata.get("source") != "dashboard":
            continue
        if metadata.get("profile") not in (None, "", profile_name):
            continue
        state = str(metadata.get("state") or session.get("status") or "stopped")
        if state in {"scheduled", "dismissed"}:
            continue
        diagnostic = diagnostics_by_key.get(
            recording_bundle_key(home, recording_dir)
        )
        if diagnostic is not None and not diagnostic.get("healthy"):
            continue
        try:
            modified_at = recording_dir.stat().st_mtime
        except OSError:
            modified_at = 0.0
        candidates.append((modified_at, recording_dir, metadata, state))
    if not candidates:
        return {
            "recording": None,
            "draft": None,
            "bundle_diagnostics": bundle_diagnostics,
        }

    modules = None
    for _, recording_dir, metadata, state in sorted(
        candidates,
        key=lambda item: item[0],
        reverse=True,
    ):
        recording = {
            "recording_dir": str(recording_dir),
            "recording_key": recording_bundle_key(home, recording_dir),
            "name": (
                metadata.get("name")
                or metadata.get("workflow_id")
                or recording_dir.parent.name
            ),
            "instructions": metadata.get("instructions") or "",
            "schedule": metadata.get("schedule") or "",
            "run_mode": (
                "webhook"
                if metadata.get("trigger_requested") is True
                else "schedule"
            ),
            "status": state,
        }
        workflow_path = recording_dir / "workflow.json"
        if not workflow_path.is_file():
            return {
                "recording": recording,
                "draft": None,
                "bundle_diagnostics": bundle_diagnostics,
            }

        try:
            workflow = read_json(workflow_path)
        except HTTPException as exc:
            mark_recovery_error(
                recording_dir,
                f"invalid:workflow.json:{type(exc).__name__}",
            )
            continue
        if modules is None:
            modules = load_crunch_modules()
        draft_digest = workflow_digest(workflow)
        try:
            conversation = load_review_conversation(recording_dir)
        except (OSError, UnicodeError, ValueError) as exc:
            mark_recovery_error(
                recording_dir,
                f"invalid:review-conversation.json:{type(exc).__name__}",
            )
            continue
        try:
            review_state = _review_conversation_response(
                workflow=workflow,
                draft_digest=draft_digest,
                conversation=conversation,
                compiler=modules["compiler"],
                home=home,
                include_install_readiness=False,
            )
        except (KeyError, TypeError, ValueError) as exc:
            mark_recovery_error(
                recording_dir,
                f"invalid:workflow-review:{type(exc).__name__}",
            )
            continue
        draft = {
            "recording_dir": str(recording_dir),
            "workflow": workflow,
            "draft_digest": draft_digest,
            "effective_approval_boundaries": review_state[
                "effective_approval_boundaries"
            ],
            "review_blockers": review_state["blockers"],
        }
        return {
            "recording": recording,
            "draft": draft,
            "bundle_diagnostics": bundle_diagnostics,
        }

    return {
        "recording": None,
        "draft": None,
        "bundle_diagnostics": bundle_diagnostics,
    }


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


@router.get("/recordings/resume")
def resume_recording(profile: str = "default"):
    profile_name, home = _cron_profile_home(profile)
    return _resume_recording_payload(home, profile_name)


@router.post("/recordings/start")
def start_recording(body: CrunchRecordingStartRequest, request: Request):
    request_dashboard_origin = _capture_request_dashboard_origin(request)
    if body.capture_dashboard_origin != request_dashboard_origin:
        raise HTTPException(
            status_code=403,
            detail="Chrome capture authorization does not match this Hermes dashboard.",
        )
    profile_name, home = _cron_profile_home(body.profile)
    workflow_id = crunch_slug(body.name, "workflow")
    run_mode = body.run_mode.strip().lower()
    if run_mode not in {"schedule", "webhook"}:
        raise HTTPException(status_code=400, detail="run_mode must be schedule or webhook")
    if run_mode == "schedule" and not body.schedule.strip():
        raise HTTPException(status_code=400, detail="Choose a schedule or an event trigger before recording.")
    return start_native_recording(
        home=home,
        profile_name=profile_name,
        workflow_id=workflow_id,
        name=body.name.strip(),
        instructions=body.instructions.strip(),
        schedule=body.schedule.strip() if run_mode == "schedule" else "triggered",
        capture_authorization_nonce=body.capture_authorization_nonce,
        capture_dashboard_origin=request_dashboard_origin,
        trigger_requested=run_mode == "webhook",
    )


@router.post("/recordings/{recording_key:path}/stop")
def stop_recording(recording_key: str, body: CrunchRecordingStopRequest):
    _profile_name, home = _cron_profile_home(body.profile)
    recording_dir = resolve_recording_ref(home, recording_key)
    return stop_native_recording(
        home=home,
        recording_dir=recording_dir,
        chrome_finalize_ok=body.chrome_finalize_ok,
        chrome_finalize_error=body.chrome_finalize_error,
    )


@router.post("/recordings/{recording_key:path}/discard")
def discard_recording(recording_key: str, body: CrunchRecordingDiscardRequest):
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


@router.post("/recordings/{recording_key:path}/permanent-delete")
def permanently_delete_recording(
    recording_key: str,
    body: CrunchRecordingDiscardRequest,
):
    """Delete one exact recovery bundle after capture shutdown is proven."""

    _profile_name, home = _cron_profile_home(body.profile)
    recording_dir = resolve_recording_ref(home, recording_key, require_bundle=False)
    diagnostic = recording_bundle_diagnostic(home, recording_dir)
    if diagnostic.get("state") == "scheduled":
        raise HTTPException(
            status_code=409,
            detail="An installed workflow's recording evidence cannot be permanently deleted.",
        )
    if diagnostic.get("healthy"):
        raise HTTPException(
            status_code=409,
            detail="Healthy recordings are retained through dismissal and cannot be permanently deleted here.",
        )
    if _cancel_active_draft_operations(recording_dir):
        raise HTTPException(
            status_code=409,
            detail="Drafting is being canceled. Wait, then delete the damaged recording.",
        )
    with _recording_operation_lock(recording_dir):
        if _draft_operation_active(recording_dir):
            raise HTTPException(
                status_code=409,
                detail="Drafting is still finishing cancellation.",
            )
        # Recheck under the per-recording lock so a bundle repaired while this
        # request waited cannot cross the destructive boundary as "damaged."
        current_diagnostic = recording_bundle_diagnostic(home, recording_dir)
        if current_diagnostic.get("state") == "scheduled":
            raise HTTPException(
                status_code=409,
                detail="An installed workflow's recording evidence cannot be permanently deleted.",
            )
        if current_diagnostic.get("healthy"):
            raise HTTPException(
                status_code=409,
                detail="The recording recovered and is no longer eligible for permanent deletion.",
            )
        return delete_recording_bundle(home=home, recording_dir=recording_dir)


@router.post("/recordings/{recording_key:path}/restore")
def restore_recording(recording_key: str, body: CrunchRecordingDiscardRequest):
    """Return one retained dismissed bundle to the normal review flow."""

    _profile_name, home = _cron_profile_home(body.profile)
    recording_dir = resolve_recording_ref(home, recording_key)
    with _recording_operation_lock(recording_dir):
        if _draft_operation_active(recording_dir):
            raise HTTPException(
                status_code=409,
                detail="Wait for drafting to finish before restoring this recording.",
            )
        status = load_recording_status(home=home, recording_dir=recording_dir)["status"]
        if status != "dismissed":
            raise HTTPException(
                status_code=409,
                detail="Only a retained dismissed recording can be restored.",
            )
        restored_state = (
            "drafted" if (recording_dir / "workflow.json").is_file() else "stopped"
        )
        for path in (recording_dir / "metadata.json", recording_dir / "session.json"):
            value = read_json(path)
            value["state"] = restored_state
            value["status"] = restored_state
            value.pop("dismissed_at", None)
            write_json(path, value)
        return {"status": restored_state}


@router.get("/recordings/{recording_key:path}/export")
def export_recording_bundle(recording_key: str, profile: str = "default"):
    """Export recovery evidence without buffering a potentially large video."""

    _profile_name, home = _cron_profile_home(profile)
    recording_dir = resolve_recording_ref(home, recording_key, require_bundle=False)
    fd, archive_name = tempfile.mkstemp(
        prefix=f"hermes-crunch-{recording_dir.name}-",
        suffix=".zip",
    )
    os.close(fd)
    archive_path = Path(archive_name)
    try:
        with zipfile.ZipFile(
            archive_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as output:
            for path in sorted(recording_dir.rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                resolved = path.resolve()
                try:
                    relative = resolved.relative_to(recording_dir)
                except ValueError:
                    continue
                output.write(resolved, relative.as_posix())
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise
    return FileResponse(
        archive_path,
        media_type="application/zip",
        filename=f"{recording_dir.name}.zip",
        background=BackgroundTask(archive_path.unlink, missing_ok=True),
    )


@router.get("/recordings/{recording_key:path}/review-conversation")
def get_review_conversation(
    recording_key: str,
    profile: str = "default",
    expected_draft_digest: str = "",
):
    modules = load_crunch_modules()
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
            home=home,
        )


@router.post("/recordings/{recording_key:path}/review-conversation")
def post_review_conversation(recording_key: str, body: CrunchReviewConversationRequest):
    modules = load_crunch_modules()
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
            home=home,
        )


@router.post("/recordings/{recording_key:path}/draft")
def draft_recording_only(recording_key: str, body: CrunchRecordingDraftOnlyRequest):
    modules = load_crunch_modules()
    _profile_name, home = _cron_profile_home(body.profile)
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
                return modules["drafting"].author_workflow(
                    body.instructions.strip(),
                    context,
                    recording_dir=recording_dir,
                    home=home,
                    on_agent=operation.attach_agent,
                    on_progress=operation.on_progress,
                    on_interim=operation.on_interim,
                    revision_context=revision_context,
                )

            candidate = modules["recording"].build_draft_candidate(
                recording_dir,
                output=workflow_path,
                name=body.name.strip(),
                instructions=body.instructions.strip(),
                workflow_author=author,
            )
            candidate_review = candidate.workflow.get("review") if isinstance(candidate.workflow.get("review"), dict) else {}
            candidate_review["draft_revision"] = previous_revision + 1
            if source_digest:
                candidate_review["source_draft_digest"] = source_digest
                candidate_review["source_draft_revision"] = previous_revision
            candidate.workflow["review"] = candidate_review
            candidate.metadata["draft_revision"] = candidate_review["draft_revision"]
            candidate_review["source_digest"] = _recording_digest(recording_dir)
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
                "effective_approval_boundaries": modules["compiler"].effective_approval_boundaries(candidate.workflow),
                "review_blockers": modules["compiler"].review_blockers(candidate.workflow),
            }
            operation.complete(draft)
        except BaseException as exc:
            accepted = operation.fail(exc)
            if accepted and not operation.cancel_requested:
                log.exception("Crunch recording draft failed")
        finally:
            for _target, temporary in staged:
                temporary.unlink(missing_ok=True)
            operation.detach_agent()
            operation.ensure_terminal()

    worker = threading.Thread(
        target=run_draft,
        daemon=True,
        name=f"hermes-crunch-draft-{operation.operation_id[:8]}",
    )
    operation.attach_worker(worker)
    try:
        worker.start()
    except BaseException as exc:
        operation.fail(exc)
        log.exception("Could not start Crunch drafting worker")
    else:
        operation.mark_worker_started()
        try:
            threading.Thread(
                target=_watch_draft_operation,
                args=(operation,),
                daemon=True,
                name=f"hermes-crunch-watch-{operation.operation_id[:8]}",
            ).start()
        except BaseException:
            # Status polling also reconciles this operation; failure to create
            # the convenience watcher must not invalidate a live worker.
            log.exception("Could not start Crunch drafting watchdog")
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
def schedule_recording(body: CrunchScheduleRecordingRequest):
    modules = load_crunch_modules()
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


def _schedule_recording_locked(
    body: CrunchScheduleRecordingRequest,
    *,
    modules: Dict[str, Any],
    profile_name: str,
    home: Path,
    recording_dir: Path,
):
    if _draft_operation_active(recording_dir):
        raise HTTPException(status_code=409, detail="A draft operation is already in progress.")

    workflow_path = recording_dir / "workflow.json"
    if not workflow_path.is_file():
        raise HTTPException(status_code=404, detail="Draft workflow not found")
    workflow = read_json(workflow_path)
    if workflow_digest(workflow) != body.expected_draft_digest:
        raise HTTPException(status_code=409, detail="The reviewed draft changed. Reload the workflow review.")
    blockers = modules["compiler"].review_blockers(workflow)
    if blockers:
        raise HTTPException(status_code=409, detail="Resolve every review detail before scheduling.")
    conversation = load_review_conversation(recording_dir)
    state, _current_blockers, _thread = _derived_review_state(
        workflow=workflow,
        draft_digest=body.expected_draft_digest,
        conversation=conversation,
        compiler=modules["compiler"],
    )
    if state != "ready_to_schedule":
        raise HTTPException(status_code=409, detail="Finish the workflow review before scheduling.")

    execution = workflow.get("execution") if isinstance(workflow.get("execution"), dict) else {}
    requested_authorization = (
        body.authorization
        if isinstance(body.authorization, dict)
        else execution.get("authorization")
    )
    try:
        authorization = modules["compiler"].validate_authorization_selection(
            execution,
            requested_authorization,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    install_readiness = crunch_install_readiness(
        home=home,
        authorization=authorization,
        run_mode=body.run_mode,
    )
    if not install_readiness["ready"]:
        blocker = next(
            (
                item
                for item in install_readiness["blockers"]
                if isinstance(item, dict) and item.get("message")
            ),
            {},
        )
        raise HTTPException(
            status_code=409,
            detail=str(blocker.get("message") or "The workflow is not ready to install."),
        )

    authorization_args: Dict[str, Any] = {
        "authorization_override": prepare_install_authorization(
            home=home,
            authorization=authorization,
        )
    }

    try:
        draft = compile_reviewed_recording(
            modules,
            home=home,
            profile_name=profile_name,
            recording_dir=recording_dir,
            schedule=body.schedule,
            run_mode=body.run_mode,
            expected_draft_digest=body.expected_draft_digest,
            **authorization_args,
        )
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("Crunch recording schedule failed during draft")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response = dict(draft)
    response["install"] = None
    if not body.approve:
        response["run_mode"] = body.run_mode.strip().lower()
        response["status"] = "awaiting_approval"
        return response
    if (
        body.expected_approval_envelope_sha256
        != draft.get("approval_envelope_sha256")
    ):
        raise HTTPException(
            status_code=409,
            detail="The executable payload changed. Review the new approval envelope.",
        )
    try:
        install = install_crunch_build(
            modules,
            home=home,
            profile_name=profile_name,
            build_dir=Path(str(draft["build_dir"])),
            note="Approved from Workflows",
            expected_approval_envelope_sha256=str(
                body.expected_approval_envelope_sha256 or ""
            ),
            webhook_trigger=(
                workflow.get("trigger")
                if body.run_mode.strip().lower() == "webhook"
                and isinstance(workflow.get("trigger"), dict)
                else None
            ),
        )
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("Crunch recording schedule failed during install")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response["install"] = install
    native = install.get("install") if isinstance(install.get("install"), dict) else {}
    response["job_id"] = native.get("job_id")
    response["run_mode"] = body.run_mode.strip().lower()
    response["webhook"] = install.get("webhook")
    response["cron_url"] = f"/cron?profile={profile_name}"
    response["status"] = "installed"
    try:
        _mark_recording_scheduled(recording_dir, response["job_id"])
    except Exception:
        log.exception("Crunch workflow installed but its recording lifecycle marker could not be updated")
    return response
