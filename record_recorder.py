"""Record dashboard bridge and full-computer recorder helpers."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import weakref
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Mapping

from fastapi import HTTPException

try:
    import fcntl
except ImportError:
    fcntl = None
    try:
        import msvcrt
    except ImportError:
        msvcrt = None

from .recording_bundle import (
    builds_root as record_builds_root,
    read_json as read_record_json,
    recording_key as record_recording_key,
    write_json as write_record_json,
)

REVIEW_CONVERSATION_FILENAME = "review-conversation.json"
REVIEW_CONVERSATION_SCHEMA_VERSION = 1
REVIEW_CONVERSATION_STATES = {
    "needs_input",
    "needs_rerecord",
    "ready_to_redraft",
    "ready_to_schedule",
    "applied",
}


def review_conversation_path(recording_dir: Path) -> Path:
    """Return the recording-local clarification sidecar path."""
    return recording_dir.expanduser().resolve() / REVIEW_CONVERSATION_FILENAME


def _validate_review_conversation(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Review conversation must be an object")
    if value.get("schema_version", REVIEW_CONVERSATION_SCHEMA_VERSION) != REVIEW_CONVERSATION_SCHEMA_VERSION:
        raise ValueError("Unsupported review conversation schema")
    threads = value.get("threads", [])
    if not isinstance(threads, list):
        raise ValueError("Review conversation threads must be a list")
    for thread in threads:
        if not isinstance(thread, dict):
            raise ValueError("Review conversation thread must be an object")
        if not isinstance(thread.get("thread_id"), str) or not thread["thread_id"].strip():
            raise ValueError("Review conversation thread_id is required")
        if not isinstance(thread.get("draft_digest"), str) or not thread["draft_digest"].strip():
            raise ValueError("Review conversation draft_digest is required")
        if isinstance(thread.get("draft_revision"), bool) or not isinstance(thread.get("draft_revision"), int) or thread["draft_revision"] < 1:
            raise ValueError("Review conversation draft_revision is invalid")
        if thread.get("state") not in REVIEW_CONVERSATION_STATES:
            raise ValueError("Invalid review conversation state")
        if not isinstance(thread.get("open_questions"), list) or any(
            not isinstance(question, str) or not question.strip()
            for question in thread["open_questions"]
        ):
            raise ValueError("Review conversation open_questions are invalid")
        if not isinstance(thread.get("feedback_summary"), str):
            raise ValueError("Review conversation feedback_summary is invalid")
        if thread.get("applied_to_digest") not in (None, "") and not isinstance(thread.get("applied_to_digest"), str):
            raise ValueError("Review conversation applied_to_digest is invalid")
        if not isinstance(thread.get("created_at"), str) or not isinstance(thread.get("updated_at"), str):
            raise ValueError("Review conversation timestamps are required")
        messages = thread.get("messages", [])
        if not isinstance(messages, list):
            raise ValueError("Review conversation messages must be a list")
        expected_role = "user"
        for message in messages:
            if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
                raise ValueError("Review conversation messages must be user or assistant")
            if message["role"] != expected_role:
                raise ValueError("Review conversation messages must alternate roles")
            if not isinstance(message.get("content"), str) or not message["content"].strip():
                raise ValueError("Review conversation message content is required")
            if "display_content" in message and not isinstance(message["display_content"], str):
                raise ValueError("Review conversation display_content is invalid")
            if "created_at" in message and not isinstance(message["created_at"], str):
                raise ValueError("Review conversation message timestamp is invalid")
            expected_role = "assistant" if expected_role == "user" else "user"
        if not messages or expected_role != "user":
            raise ValueError("Review conversation cannot end with a dangling user turn")
    return value


def load_review_conversation(recording_dir: Path) -> dict[str, Any]:
    """Load the validated sidecar, returning an empty schema when absent."""
    path = review_conversation_path(recording_dir)
    if not path.is_file():
        return {"schema_version": REVIEW_CONVERSATION_SCHEMA_VERSION, "threads": []}
    return _validate_review_conversation(json.loads(path.read_text(encoding="utf-8")))


def save_review_conversation(recording_dir: Path, conversation: Mapping[str, Any]) -> Path:
    """Atomically replace the recording-local clarification sidecar."""
    validated = _validate_review_conversation(dict(conversation))
    path = review_conversation_path(recording_dir)
    write_record_json(path, validated)
    return path


_UNSET = object()
_PUBLICATION_LOCK_TIMEOUT_SECONDS = 10.0
_PUBLICATION_LOCK_POLL_SECONDS = 0.05
_publication_thread_locks_guard = threading.Lock()
_publication_thread_locks: weakref.WeakValueDictionary[
    str, Any
] = weakref.WeakValueDictionary()


def _publication_lock_timeout() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=(
            "Another process is publishing this workflow build. "
            "Wait for it to finish, then try again."
        ),
    )


def _try_acquire_publication_file_lock(handle) -> bool:
    if fcntl is not None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (BlockingIOError, OSError):
            return False
    if msvcrt is not None:
        try:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except (BlockingIOError, OSError):
            return False
    return False


def _release_publication_file_lock(handle) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


@contextmanager
def _record_publication_lock(*, home: Path, build_dir: Path):
    """Serialize one profile/build publication transaction across processes."""

    resolved_home = home.expanduser().resolve()
    resolved_build = build_dir.expanduser().resolve()
    lock_key = hashlib.sha256(
        str(resolved_build).encode("utf-8")
    ).hexdigest()
    lock_path = resolved_home / "record" / "publication-locks" / f"{lock_key}.lock"
    deadline = time.monotonic() + _PUBLICATION_LOCK_TIMEOUT_SECONDS
    with _publication_thread_locks_guard:
        thread_lock = _publication_thread_locks.setdefault(
            str(lock_path), threading.Lock()
        )
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not thread_lock.acquire(timeout=remaining):
        raise _publication_lock_timeout()

    handle = None
    acquired = False
    try:
        if fcntl is None and msvcrt is None:
            raise _publication_lock_timeout()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            handle = lock_path.open("a+b")
        except OSError as exc:
            logging.getLogger(__name__).error(
                "Could not open Record publication lock %s: %s",
                lock_path,
                exc,
            )
            raise _publication_lock_timeout() from exc
        while not acquired:
            acquired = _try_acquire_publication_file_lock(handle)
            if acquired:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _publication_lock_timeout()
            time.sleep(min(_PUBLICATION_LOCK_POLL_SECONDS, remaining))
        yield
    finally:
        if handle is not None:
            if acquired:
                _release_publication_file_lock(handle)
            handle.close()
        thread_lock.release()


def load_record_modules() -> Dict[str, Any]:
    """Import the Record engine bundled inside this plugin."""
    try:
        from .record import (
            compiler,
            drafting,
            hermes,
            recording,
            review_receipt,
        )
        return {
            "review_receipt": review_receipt,
            "compiler": compiler,
            "drafting": drafting,
            "hermes": hermes,
            "recording": recording,
        }
    except ModuleNotFoundError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "Record plugin is missing its bundled record engine package. "
                "Reinstall or update the record plugin."
            ),
        ) from exc


def record_slug(value: str, default: str = "workflow") -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in value.strip())
    cleaned = cleaned.strip("-._")
    return cleaned or default


def compile_reviewed_recording(
    modules: Dict[str, Any],
    *,
    home: Path,
    profile_name: str,
    recording_dir: Path,
    schedule: str,
    run_mode: str = "schedule",
    expected_draft_digest: str = "",
    delivery_override: Any = _UNSET,
) -> Dict[str, Any]:
    workflow_path = recording_dir / "workflow.json"
    workflow = modules["compiler"].load_workflow(workflow_path)
    digest = workflow_digest(workflow)
    if expected_draft_digest and expected_draft_digest != digest:
        raise HTTPException(status_code=409, detail="The reviewed draft changed. Review the latest cron draft before scheduling.")
    review = workflow.get("review") if isinstance(workflow.get("review"), dict) else {}
    # Install choices are a new immutable build, not a mutation of the draft
    # the operator reviewed. This closes the check-then-rewrite race and keeps
    # the displayed draft digest meaningful for concurrent requests.
    workflow = deepcopy(workflow)
    from .record.evidence import verified_capture_acceptance

    review = dict(review)
    review["capture_acceptance"] = verified_capture_acceptance(recording_dir)
    workflow["review"] = review
    run_mode = str(run_mode or "schedule").strip().lower()
    if run_mode not in {"schedule", "webhook"}:
        raise HTTPException(
            status_code=400,
            detail="run_mode must be schedule or webhook",
        )
    trigger = (
        workflow.get("trigger")
        if isinstance(workflow.get("trigger"), dict)
        else {}
    )
    if run_mode == "webhook" and trigger.get("feasibility") != "supported":
        reason = str(
            trigger.get("reason")
            or "Hermes could not verify an authenticated configurable webhook for this event."
        )
        raise HTTPException(
            status_code=409,
            detail=f"This trigger cannot be created: {reason}",
        )
    if run_mode == "schedule" and not str(schedule or "").strip():
        raise HTTPException(status_code=400, detail="Choose a Hermes schedule.")
    workflow["run_mode"] = run_mode
    workflow["schedule"] = (
        "triggered" if run_mode == "webhook" else str(schedule).strip()
    )
    workflow["deliver"] = (
        str(delivery_override).strip()
        if delivery_override is not _UNSET
        else "local"
    )
    execution = (
        dict(workflow.get("execution"))
        if isinstance(workflow.get("execution"), dict)
        else None
    )
    if execution is None:
        raise HTTPException(
            status_code=400,
            detail="The reviewed workflow has no execution plan.",
        )
    workflow["execution"] = execution
    workflow_id = record_slug(str(workflow.get("id") or recording_dir.parent.name))
    build_digest = workflow_digest(workflow)
    out_dir = record_builds_root(home) / (
        f"{workflow_id}-{recording_dir.name}-{build_digest[:16]}"
    )
    result = modules["compiler"].compile_workflow(
        workflow,
        home,
        out_dir,
        provenance={
            "type": "record",
            "workflow_id": workflow_id,
            "recording_id": record_recording_key(home, recording_dir),
            "source_digest": str(review.get("source_digest") or ""),
            "draft_digest": digest,
            "draft_revision": int(review.get("draft_revision") or 1),
            "build_id": out_dir.name,
            "build_digest": build_digest,
        },
    )
    summary = read_record_json(out_dir / "compile-summary.json")
    artifact_manifest = read_record_json(out_dir / "artifacts.json")
    script_destination = result.payload.get("script")
    script_source = None
    script_sha256 = None
    if isinstance(script_destination, str) and script_destination:
        staged_scripts = (out_dir / "staged-runtime" / "scripts").resolve()
        staged_script = (staged_scripts / script_destination).resolve()
        try:
            staged_script.relative_to(staged_scripts)
        except ValueError as exc:
            raise ValueError("Compiled script escapes the staged scripts directory") from exc
        script_bytes = staged_script.read_bytes()
        script_source = script_bytes.decode("utf-8")
        script_sha256 = hashlib.sha256(script_bytes).hexdigest()
    publication_guard = modules["compiler"].record_install_guard(workflow)
    write_record_json(out_dir / "publication-guard.json", publication_guard)
    envelope = {
        "version": 1,
        "provenance": summary.get("provenance"),
        "payload": result.payload,
        "payload_sha256": modules["review_receipt"].payload_hash(result.payload),
        "script_source": script_source,
        "script_sha256": script_sha256,
        "artifacts": artifact_manifest.get("artifacts") or [],
        "artifacts_sha256": modules["review_receipt"].artifact_bundle_hash(out_dir),
        "publication_guard": publication_guard,
    }
    envelope_path = out_dir / modules["review_receipt"].REVIEW_ENVELOPE_FILENAME
    write_record_json(envelope_path, envelope)
    review_envelope_sha256 = hashlib.sha256(envelope_path.read_bytes()).hexdigest()
    review_path = out_dir / "review.md"
    return {
        "profile": profile_name,
        "recording_dir": str(recording_dir),
        "workflow_path": str(workflow_path),
        "build_dir": str(out_dir),
        "workflow": workflow,
        "draft_digest": digest,
        "payload": result.payload,
        "review_envelope": envelope,
        "review_envelope_sha256": review_envelope_sha256,
        "review_markdown": review_path.read_text(encoding="utf-8") if review_path.is_file() else "",
        "publication_guard": publication_guard,
        "status": (
            "review_ready"
            if not publication_guard["requires_review_resolution"]
            else "needs_attention"
        ),
    }


def _recording_digest(
    recording_dir: Path,
) -> str:
    """Hash the canonical capture inputs persisted for a reviewed draft."""

    from .record.evidence import capture_source_digest

    return capture_source_digest(recording_dir)


def workflow_digest(workflow: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(workflow, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _reviewed_build_provenance(
    build_dir: Path, receipt: Dict[str, Any]
) -> Dict[str, Any]:
    """Return immutable build facts copied into the installed Cron job."""
    summary_path = build_dir / "compile-summary.json"
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Missing compile summary: {summary_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid compile summary JSON: {summary_path}: {exc}") from exc
    if not isinstance(summary, dict):
        raise ValueError("Compile summary must be a JSON object")
    summary_digest = hashlib.sha256(
        json.dumps(summary, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if receipt.get("compile_summary_sha256") != summary_digest:
        raise ValueError("Compile summary is not covered by the review receipt")
    compiled = summary.get("provenance") if isinstance(summary, dict) else None
    if not isinstance(compiled, dict):
        raise ValueError("Compile summary is missing reviewed build provenance")
    provenance = dict(compiled)

    required_text = (
        "workflow_id",
        "recording_id",
        "source_digest",
        "draft_digest",
        "build_id",
        "build_digest",
    )
    missing = [
        field
        for field in required_text
        if not isinstance(provenance.get(field), str)
        or not str(provenance[field]).strip()
    ]
    if missing:
        raise ValueError(
            "Compile summary is missing required provenance: " + ", ".join(missing)
        )
    digest_fields = ("source_digest", "draft_digest", "build_digest")
    invalid_digests = [
        field
        for field in digest_fields
        if len(provenance[field]) != 64
        or any(character not in "0123456789abcdef" for character in provenance[field])
    ]
    if invalid_digests:
        raise ValueError(
            "Compile summary contains invalid provenance digests: "
            + ", ".join(invalid_digests)
        )
    revision = provenance.get("draft_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError("Compile summary draft_revision must be a positive integer")
    if provenance.get("type") != "record":
        raise ValueError("Compile summary provenance type must be record")
    if provenance["build_id"] != build_dir.name:
        raise ValueError("Compile summary build_id does not match the reviewed build")
    if not build_dir.name.endswith(f"-{provenance['build_digest'][:16]}"):
        raise ValueError("Compile summary build_digest does not match the reviewed build")

    receipt_fields = (
        "payload_sha256",
        "artifacts_sha256",
        "review_envelope_sha256",
        "reviewed_by",
        "reviewed_at",
    )
    missing_receipt = [
        field
        for field in receipt_fields
        if not isinstance(receipt.get(field), str) or not receipt[field].strip()
    ]
    if missing_receipt:
        raise ValueError(
            "Review receipt is missing required build linkage: "
            + ", ".join(missing_receipt)
        )
    for field in (
        "payload_sha256",
        "artifacts_sha256",
        "review_envelope_sha256",
    ):
        digest = receipt[field]
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ValueError(f"Review receipt {field} is not a SHA-256 digest")

    payload_sha256 = receipt["payload_sha256"]
    if summary.get("payload_sha256") != payload_sha256:
        raise ValueError("Compile summary payload does not match the review receipt")
    review_receipt_sha256 = hashlib.sha256(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    review_envelope_sha256 = receipt["review_envelope_sha256"]
    provenance.update(
        {
            "type": "record",
            "build_id": build_dir.name,
            "payload_sha256": payload_sha256,
            "artifacts_sha256": receipt["artifacts_sha256"],
            "reviewed_by": receipt["reviewed_by"],
            "reviewed_at": receipt["reviewed_at"],
            "review_envelope_sha256": review_envelope_sha256,
            "review_receipt_sha256": review_receipt_sha256,
        }
    )
    return provenance


def _publish_reviewed_record_build(
    modules: Dict[str, Any],
    *,
    home: Path,
    profile_name: str,
    build_dir: Path,
    note: str,
    review_envelope_sha256: str,
    webhook_trigger: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Publish one reviewed build while its profile/build lock is held."""
    receipt_path = (
        build_dir / modules["review_receipt"].REVIEW_RECEIPT_FILENAME
    )
    if receipt_path.is_file():
        # A publication retry reuses the immutable receipt for the same click.
        _, receipt = modules["review_receipt"].load_reviewed_payload(build_dir)
        if receipt.get("review_envelope_sha256") != review_envelope_sha256:
            raise HTTPException(
                status_code=409,
                detail=(
                    "The stored review does not cover this executable payload. "
                    "Recompile and review it again."
                ),
            )
    else:
        receipt = modules["review_receipt"].record_review_receipt(
            build_dir,
            reviewed_by=f"hermes-profile:{profile_name}",
            note=note or None,
            review_envelope_sha256=review_envelope_sha256,
        )
    payload, stored_receipt = modules["review_receipt"].load_reviewed_payload(
        build_dir
    )
    provenance = _reviewed_build_provenance(
        build_dir, stored_receipt or receipt
    )
    install_identity = {
        key: provenance.get(key)
        for key in (
            "recording_id",
            "source_digest",
            "draft_revision",
            "draft_digest",
            "build_id",
            "build_digest",
            "payload_sha256",
            "artifacts_sha256",
        )
    }
    install_identity["profile_home"] = str(home.expanduser().resolve())
    provenance["install_idempotency_key"] = hashlib.sha256(
        json.dumps(
            install_identity,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    promotion = modules["compiler"].promote_compiled_artifacts(build_dir, home)
    try:
        install = modules["hermes"].install_cron_job(
            payload,
            hermes_home=home,
            provenance=provenance,
            webhook_trigger=webhook_trigger,
        )
    except Exception:
        promotion.rollback()
        raise
    promotion.commit()
    return {
        "profile": profile_name,
        "build_dir": str(build_dir),
        "review_receipt": stored_receipt or receipt,
        "payload": payload,
        "install": install,
        "webhook": install.get("webhook"),
    }


def publish_record_build(
    modules: Dict[str, Any],
    *,
    home: Path,
    profile_name: str,
    build_dir: Path,
    note: str,
    expected_review_envelope_sha256: str,
    webhook_trigger: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    if not (build_dir / "cronjob-payload.json").is_file():
        raise HTTPException(status_code=404, detail="Compiled Record payload not found")
    guard_path = build_dir / "publication-guard.json"
    if not guard_path.is_file():
        raise HTTPException(
            status_code=409,
            detail="The compiled publication guard is missing. Rebuild and review the workflow.",
        )
    try:
        guard = json.loads(guard_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=409,
            detail="The compiled publication guard is invalid. Rebuild and review the workflow.",
        ) from exc
    if (
        not isinstance(guard, dict)
        or not isinstance(guard.get("blockers"), list)
        or not isinstance(guard.get("requires_review_resolution"), bool)
        or guard["requires_review_resolution"] != bool(guard["blockers"])
        or any(not isinstance(item, dict) for item in guard["blockers"])
    ):
        raise HTTPException(
            status_code=409,
            detail="The compiled publication guard is invalid. Rebuild and review the workflow.",
        )
    if guard["requires_review_resolution"]:
        raise HTTPException(
            status_code=409,
            detail="Resolve every workflow review detail before publication",
        )
    envelope_path = (
        build_dir / modules["review_receipt"].REVIEW_ENVELOPE_FILENAME
    )
    if not envelope_path.is_file():
        raise HTTPException(
            status_code=409,
            detail="Compile the review envelope before publication",
        )
    actual_review_envelope_sha256 = hashlib.sha256(
        envelope_path.read_bytes()
    ).hexdigest()
    if (
        not expected_review_envelope_sha256
        or expected_review_envelope_sha256 != actual_review_envelope_sha256
    ):
        raise HTTPException(
            status_code=409,
            detail="The executable payload changed. Review the latest build.",
        )
    try:
        envelope, verified_envelope_sha256 = (
            modules["review_receipt"].verify_review_envelope(
                build_dir,
                expected_sha256=expected_review_envelope_sha256,
            )
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                "The executable payload no longer matches the reviewed "
                "build. Rebuild and review the workflow."
            ),
        ) from exc
    if verified_envelope_sha256 != actual_review_envelope_sha256:
        raise HTTPException(
            status_code=409,
            detail="The executable payload changed. Review the latest build.",
        )
    if not isinstance(envelope, dict) or envelope.get("publication_guard") != guard:
        raise HTTPException(
            status_code=409,
            detail="The publication guard is not covered by the reviewed payload. Rebuild and review the workflow.",
        )
    try:
        modules["review_receipt"].load_payload(build_dir)
    except (OSError, UnicodeError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail="The compiled workflow payload is invalid. Rebuild and review the workflow.",
        ) from exc
    with _record_publication_lock(home=home, build_dir=build_dir):
        return _publish_reviewed_record_build(
            modules,
            home=home,
            profile_name=profile_name,
            build_dir=build_dir,
            note=note,
            review_envelope_sha256=actual_review_envelope_sha256,
            webhook_trigger=webhook_trigger,
        )
