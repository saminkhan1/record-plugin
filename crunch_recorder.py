"""Hermes Crunch dashboard bridge and full-computer recorder helpers."""

from __future__ import annotations

import hashlib
import json
import logging
import sys
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

PLUGIN_ROOT = Path(__file__).resolve().parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from recording_bundle import (  # noqa: E402
    builds_root as crunch_builds_root,
    read_json as read_crunch_json,
    recording_key as crunch_recording_key,
    write_json as write_crunch_json,
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
    write_crunch_json(path, validated)
    return path


_AUTHORIZATION_UNSET = object()
_APPROVAL_ROUTE_BLOCKER = {
    "code": "approval_route_unavailable",
    "message": (
        "Connect an approval-capable channel and set its default destination "
        "before turning on this workflow."
    ),
    "action": {
        "label": "Set up channels",
        "href": "/channels",
    },
}
_WEBHOOK_RUNTIME_BLOCKER = {
    "code": "webhook_receiver_unavailable",
    "message": (
        "Webhook reception is not enabled for this profile. Run "
        "`hermes gateway setup` to enable it before turning on this workflow."
    ),
}
_INSTALL_LOCK_TIMEOUT_SECONDS = 10.0
_INSTALL_LOCK_POLL_SECONDS = 0.05
_install_thread_locks_guard = threading.Lock()
_install_thread_locks: weakref.WeakValueDictionary[
    str, Any
] = weakref.WeakValueDictionary()


def _install_lock_timeout() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=(
            "Another process is installing this workflow build. "
            "Wait for it to finish, then try again."
        ),
    )


def _try_acquire_install_file_lock(handle) -> bool:
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


def _release_install_file_lock(handle) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


@contextmanager
def _crunch_install_lock(*, home: Path, build_dir: Path):
    """Serialize one profile/build install transaction across processes."""

    resolved_home = home.expanduser().resolve()
    resolved_build = build_dir.expanduser().resolve()
    lock_key = hashlib.sha256(
        str(resolved_build).encode("utf-8")
    ).hexdigest()
    lock_path = resolved_home / "crunch" / "install-locks" / f"{lock_key}.lock"
    deadline = time.monotonic() + _INSTALL_LOCK_TIMEOUT_SECONDS
    with _install_thread_locks_guard:
        thread_lock = _install_thread_locks.setdefault(
            str(lock_path), threading.Lock()
        )
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not thread_lock.acquire(timeout=remaining):
        raise _install_lock_timeout()

    handle = None
    acquired = False
    try:
        if fcntl is None and msvcrt is None:
            raise _install_lock_timeout()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            handle = lock_path.open("a+b")
        except OSError as exc:
            logging.getLogger(__name__).error(
                "Could not open Crunch install lock %s: %s",
                lock_path,
                exc,
            )
            raise _install_lock_timeout() from exc
        while not acquired:
            acquired = _try_acquire_install_file_lock(handle)
            if acquired:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _install_lock_timeout()
            time.sleep(min(_INSTALL_LOCK_POLL_SECONDS, remaining))
        yield
    finally:
        if handle is not None:
            if acquired:
                _release_install_file_lock(handle)
            handle.close()
        thread_lock.release()


@contextmanager
def _profile_runtime_scope(home: Path):
    """Bind one profile's Hermes home and secrets without gateway internals."""

    from agent.secret_scope import (
        build_profile_secret_scope,
        reset_secret_scope,
        set_secret_scope,
    )
    from hermes_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    resolved = home.expanduser().resolve()
    home_token = set_hermes_home_override(str(resolved))
    secret_token = set_secret_scope(build_profile_secret_scope(resolved))
    try:
        yield resolved
    finally:
        reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)


def _configured_approval_target(
    *,
    home: Path,
    expected_target: str | None = None,
    expected_route: Dict[str, Any] | None = None,
) -> tuple[str, Dict[str, Any]] | None:
    """Resolve one explicit profile home route using upstream Cron metadata.

    The dashboard process can prove configuration, not an in-memory adapter.
    The generic authorization provider repeats the stronger live adapter check
    before Cron claims each occurrence.
    """

    from agent.secret_scope import get_secret
    from cron.scheduler import cron_delivery_targets

    normalized_expected = str(expected_target or "").strip().lower()
    with _profile_runtime_scope(home):
        for item in cron_delivery_targets():
            if not isinstance(item, dict):
                continue
            if item.get("supports_exec_approval") is not True:
                continue
            target = str(item.get("id") or "").strip().lower()
            if not target or (
                normalized_expected and target != normalized_expected
            ):
                continue
            env_var = str(item.get("home_env_var") or "").strip()
            if not env_var:
                continue
            chat_id = str(get_secret(env_var, "") or "").strip()
            if not chat_id:
                continue
            thread_id = str(
                get_secret(f"{env_var}_THREAD_ID", "") or ""
            ).strip() or None
            route = {
                "platform": target,
                "chat_id": chat_id,
                "thread_id": thread_id,
            }
            if expected_route is not None and route != expected_route:
                continue
            return target, route
    return None


def resolve_default_approval_target(
    *,
    home: Path,
) -> tuple[str, Dict[str, Any]]:
    """Pin one configured home destination that can deliver approvals now."""

    target = _configured_approval_target(home=home)
    if target is None:
        raise HTTPException(
            status_code=409,
            detail=_APPROVAL_ROUTE_BLOCKER["message"],
        )
    return target


def _webhook_install_status(*, home: Path) -> Dict[str, Any]:
    """Project the upstream webhook platform's configured state."""

    try:
        from agent.secret_scope import is_multiplex_active
        from gateway.config import load_gateway_config
        from hermes_constants import get_hermes_home

        selected_home = home.expanduser().resolve()
        listener_home = get_hermes_home().expanduser().resolve()
        if selected_home != listener_home:
            return {
                "configured": False,
                "ready": False,
                "base_url": None,
                "reason": (
                    "Webhook workflows can currently be enabled only for the "
                    "profile owned by this running Hermes gateway."
                ),
            }
        listener_config = load_gateway_config()
        if is_multiplex_active() or listener_config.multiplex_profiles:
            return {
                "configured": False,
                "ready": False,
                "base_url": None,
                "reason": (
                    "Webhook workflows are not supported by a shared "
                    "multiplex profile listener."
                ),
            }

        from gateway.config import Platform

        with _profile_runtime_scope(selected_home):
            config = load_gateway_config()
            platform = config.platforms.get(Platform.WEBHOOK)
        configured = bool(platform and platform.enabled)
        extra = platform.extra if platform is not None else {}
        host = str(extra.get("host") or "127.0.0.1")
        port = int(extra.get("port") or 8644)
        display_host = "localhost" if host in {"", "0.0.0.0", "::"} else host
        return {
            "configured": configured,
            "ready": configured,
            "base_url": f"http://{display_host}:{port}",
            "reason": (
                ""
                if configured
                else "Enable the upstream Hermes webhook platform first."
            ),
        }
    except Exception as exc:
        return {
            "configured": False,
            "ready": False,
            "base_url": None,
            "reason": f"Webhook configuration is unavailable: {exc}",
        }


def _authorization_scheduler_status(*, home: Path) -> Dict[str, Any]:
    """Whether the resolved scheduler can carry live approval adapters."""

    try:
        from cron.scheduler_provider import resolve_cron_scheduler
        from gateway.status import resolve_gateway_liveness

        selected_home = home.expanduser().resolve()
        with _profile_runtime_scope(selected_home):
            provider = resolve_cron_scheduler()
        name = str(getattr(provider, "name", "") or "").strip().lower()
        in_process = name in {"builtin", "in-process", "inprocess"}
        gateway = resolve_gateway_liveness(
            profile_dir=selected_home,
            use_cache=False,
        )
        gateway_running = bool(getattr(gateway, "running", False))
        ready = in_process and gateway_running
        if not in_process:
            reason = (
                "Protected Crunch workflows require Hermes' in-process "
                "Cron scheduler because external scheduler callbacks do "
                "not carry live gateway approval adapters."
            )
        elif not gateway_running:
            reason = (
                "Protected Crunch workflows require a live Hermes messaging "
                "gateway. Desktop and dashboard-only schedulers do not carry "
                "fresh-human approval adapters."
            )
        else:
            reason = ""
        return {
            "ready": ready,
            "provider": name or None,
            "gateway_running": gateway_running,
            "gateway_source": getattr(gateway, "source", None),
            "reason": reason,
        }
    except Exception as exc:
        return {
            "ready": False,
            "provider": None,
            "gateway_running": False,
            "gateway_source": None,
            "reason": f"Cron scheduler capability is unavailable: {exc}",
        }


def crunch_install_readiness(
    *,
    home: Path,
    authorization: Any,
    run_mode: str = "schedule",
) -> Dict[str, Any]:
    """Project environmental blockers for the draft-to-installed transition."""

    from hermes_crunch.policy import execution_policy_requires_human

    normalized_run_mode = str(run_mode or "schedule").strip().lower()
    webhook_runtime = _webhook_install_status(home=home)
    webhook_ready = bool(webhook_runtime.get("configured"))
    blockers: list[Dict[str, Any]] = []
    if normalized_run_mode not in {"schedule", "webhook"}:
        blockers.append({
            "code": "run_mode_invalid",
            "message": "Choose a valid workflow start mode before turning it on.",
        })
    elif normalized_run_mode == "webhook" and not webhook_ready:
        webhook_blocker = dict(_WEBHOOK_RUNTIME_BLOCKER)
        reason = str(webhook_runtime.get("reason") or "").strip()
        if reason:
            webhook_blocker["message"] = reason
        blockers.append(webhook_blocker)

    try:
        requires_approval_route = execution_policy_requires_human(authorization)
    except (TypeError, ValueError):
        requires_approval_route = True
        blockers.append({
            "code": "authorization_invalid",
            "message": (
                "The workflow approval policy is invalid. Update the draft "
                "before turning it on."
            ),
        })
    authorization_scheduler = _authorization_scheduler_status(home=home)
    if requires_approval_route and not authorization_scheduler["ready"]:
        blockers.append({
            "code": "authorization_scheduler_unsupported",
            "message": authorization_scheduler["reason"],
        })
    configured_approval_target = _configured_approval_target(home=home)
    approval_target = configured_approval_target
    approval_route_ready = (
        configured_approval_target is not None
        if requires_approval_route
        else True
    )
    persistent_gateway_ready = (
        bool(authorization_scheduler.get("gateway_running"))
        if requires_approval_route
        else True
    )
    gateway_dispatch_ready = (
        approval_route_ready and authorization_scheduler["ready"]
    )
    if requires_approval_route and not approval_route_ready:
        blockers.append(dict(_APPROVAL_ROUTE_BLOCKER))
    return {
        "ready": not blockers,
        "requires_persistent_gateway": requires_approval_route,
        "persistent_gateway_ready": persistent_gateway_ready,
        "persistent_gateway": {
            "required": requires_approval_route,
            "ready": persistent_gateway_ready,
        },
        "requires_approval_route": requires_approval_route,
        "approval_route_ready": approval_route_ready,
        "gateway_dispatch_ready": gateway_dispatch_ready,
        "authorization_scheduler": authorization_scheduler,
        "approval_target": approval_target[0] if approval_target else None,
        "approval_route": approval_target[1] if approval_target else None,
        "jobs_api_ready": True,
        "jobs_api": {"required": False, "ready": True},
        "tracing_ready": True,
        "tracing": {"required": False, "ready": True},
        "webhook_runtime": webhook_runtime,
        "blockers": blockers,
    }


def prepare_install_authorization(
    *,
    home: Path,
    authorization: Dict[str, Any] | None,
) -> Dict[str, Any] | None:
    """Bind an active approval policy to an exact configured destination."""

    if authorization is None:
        return None
    from hermes_crunch.policy import execution_policy_requires_human

    resolved = dict(authorization)
    if execution_policy_requires_human(resolved):
        target, approval_route = resolve_default_approval_target(
            home=home,
        )
        resolved["target"] = target
        resolved["approval_route"] = approval_route
    else:
        resolved.pop("approval_route", None)
        resolved["target"] = "origin"
    return resolved


def require_approved_payload_install_ready(
    *,
    home: Path,
    payload: Dict[str, Any],
) -> None:
    """Fail closed if an approved protected payload lost its exact route."""

    from hermes_crunch.policy import (
        execution_policy_requires_human,
        normalize_execution_policy,
    )

    authorization = payload.get("authorization")
    try:
        if not execution_policy_requires_human(authorization):
            return
        policy = normalize_execution_policy(authorization)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail="The compiled workflow approval policy is invalid. Rebuild and review it.",
        ) from exc
    if not policy:
        return
    target = str(policy.get("target") or "").strip().lower()
    route = policy.get("approval_route")
    if (
        not target
        or target == "origin"
        or not isinstance(route, dict)
        or _configured_approval_target(
            home=home,
            expected_target=target,
            expected_route=route,
        )
        is None
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "The reviewed approval destination is no longer available. "
                "Return to the workflow review and set up channels before turning it on."
            ),
        )


def load_crunch_modules() -> Dict[str, Any]:
    """Import the Hermes Crunch engine bundled inside this plugin."""
    if str(PLUGIN_ROOT) not in sys.path:
        sys.path.insert(0, str(PLUGIN_ROOT))
    try:
        from hermes_crunch import approval, compiler, drafting, hermes, recording
        return {
            "approval": approval,
            "compiler": compiler,
            "drafting": drafting,
            "hermes": hermes,
            "recording": recording,
        }
    except ModuleNotFoundError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "Hermes Crunch plugin is missing its bundled hermes_crunch engine package. "
                "Reinstall or update the hermes-crunch plugin."
            ),
        ) from exc


def crunch_slug(value: str, default: str = "workflow") -> str:
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
    authorization_override: Any = _AUTHORIZATION_UNSET,
    delivery_override: Any = _AUTHORIZATION_UNSET,
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
    from hermes_crunch.evidence import verified_capture_acceptance

    review = dict(review)
    review["capture_acceptance"] = verified_capture_acceptance(recording_dir)
    workflow["review"] = review
    if authorization_override is not _AUTHORIZATION_UNSET:
        execution = workflow.get("execution")
        if not isinstance(execution, dict):
            raise HTTPException(status_code=400, detail="The reviewed workflow has no execution plan.")
        execution = dict(execution)
        execution["authorization"] = modules["compiler"].validate_execution_authorization(
            execution,
            authorization_override,
        )
        workflow["execution"] = execution
    run_mode = str(run_mode or "schedule").strip().lower()
    if run_mode not in {"schedule", "webhook"}:
        raise HTTPException(status_code=400, detail="run_mode must be schedule or webhook")
    trigger = workflow.get("trigger") if isinstance(workflow.get("trigger"), dict) else {}
    if run_mode == "webhook" and trigger.get("feasibility") != "supported":
        reason = str(trigger.get("reason") or "Hermes could not verify an authenticated configurable webhook for this event.")
        raise HTTPException(status_code=409, detail=f"This trigger cannot be created: {reason}")
    workflow["run_mode"] = run_mode
    workflow["schedule"] = "triggered" if run_mode == "webhook" else schedule
    workflow["deliver"] = (
        str(delivery_override).strip()
        if delivery_override is not _AUTHORIZATION_UNSET
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
    workflow_id = crunch_slug(str(workflow.get("id") or recording_dir.parent.name))
    build_digest = workflow_digest(workflow)
    out_dir = crunch_builds_root(home) / (
        f"{workflow_id}-{recording_dir.name}-{build_digest[:16]}"
    )
    result = modules["compiler"].compile_workflow(
        workflow,
        home,
        out_dir,
        provenance={
            "type": "hermes-crunch",
            "workflow_id": workflow_id,
            "recording_id": crunch_recording_key(home, recording_dir),
            "source_digest": str(review.get("source_digest") or ""),
            "draft_digest": digest,
            "draft_revision": int(review.get("draft_revision") or 1),
            "build_id": out_dir.name,
            "build_digest": build_digest,
        },
    )
    summary = read_crunch_json(out_dir / "compile-summary.json")
    artifact_manifest = read_crunch_json(out_dir / "artifacts.json")
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
    execution = workflow.get("execution") if isinstance(workflow.get("execution"), dict) else {}
    requirements = (
        execution.get("requirements")
        if isinstance(execution.get("requirements"), dict)
        else {}
    )
    install_guard = modules["compiler"].crunch_install_guard(workflow)
    write_crunch_json(out_dir / "install-guard.json", install_guard)
    envelope = {
        "version": 1,
        "provenance": summary.get("provenance"),
        "payload": result.payload,
        "payload_sha256": modules["approval"].payload_hash(result.payload),
        "prompt": result.payload.get("prompt"),
        "generated_script": execution.get("script"),
        "script_destination": script_destination,
        "script_source": script_source,
        "script_sha256": script_sha256,
        "execution_sha256": (
            (result.payload.get("authorization") or {}).get("execution_sha256")
            if isinstance(result.payload.get("authorization"), dict)
            else None
        ),
        "skills": result.payload.get("skills") or [],
        "enabled_toolsets": result.payload.get("enabled_toolsets") or [],
        "requirements": {
            key: requirements.get(key) or []
            for key in ("apps", "network_origins", "files", "commands")
        },
        "schedule": result.payload.get("schedule"),
        "delivery_route": result.payload.get("deliver"),
        "authorization": result.payload.get("authorization"),
        "artifacts": artifact_manifest.get("artifacts") or [],
        "artifacts_sha256": modules["approval"].artifact_bundle_hash(out_dir),
        "install_guard": install_guard,
    }
    envelope_path = out_dir / "approval-envelope.json"
    write_crunch_json(envelope_path, envelope)
    approval_envelope_sha256 = hashlib.sha256(envelope_path.read_bytes()).hexdigest()
    review_path = out_dir / "review.md"
    return {
        "profile": profile_name,
        "recording_dir": str(recording_dir),
        "workflow_path": str(workflow_path),
        "build_dir": str(out_dir),
        "workflow": workflow,
        "draft_digest": digest,
        "payload": result.payload,
        "approval_envelope": envelope,
        "approval_envelope_sha256": approval_envelope_sha256,
        "review_markdown": review_path.read_text(encoding="utf-8") if review_path.is_file() else "",
        "install_guard": install_guard,
        "status": "review_ready" if not install_guard["requires_review_resolution"] else "needs_attention",
    }


def _recording_digest(
    recording_dir: Path,
) -> str:
    """Hash the canonical capture inputs persisted for an approved draft."""

    from hermes_crunch.evidence import capture_source_digest

    return capture_source_digest(recording_dir)


def workflow_digest(workflow: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(workflow, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _approved_build_provenance(
    build_dir: Path, approval: Dict[str, Any]
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
    if approval.get("compile_summary_sha256") != summary_digest:
        raise ValueError("Compile summary is not covered by the stored approval")
    compiled = summary.get("provenance") if isinstance(summary, dict) else None
    if not isinstance(compiled, dict):
        raise ValueError("Compile summary is missing approved build provenance")
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
    if provenance.get("type") != "hermes-crunch":
        raise ValueError("Compile summary provenance type must be hermes-crunch")
    if provenance["build_id"] != build_dir.name:
        raise ValueError("Compile summary build_id does not match the approved build")
    if not build_dir.name.endswith(f"-{provenance['build_digest'][:16]}"):
        raise ValueError("Compile summary build_digest does not match the approved build")

    approval_fields = (
        "payload_sha256",
        "artifacts_sha256",
        "approval_envelope_sha256",
        "approved_by",
        "approved_at",
    )
    missing_approval = [
        field
        for field in approval_fields
        if not isinstance(approval.get(field), str) or not approval[field].strip()
    ]
    if missing_approval:
        raise ValueError(
            "Stored approval is missing required build linkage: "
            + ", ".join(missing_approval)
        )
    for field in (
        "payload_sha256",
        "artifacts_sha256",
        "approval_envelope_sha256",
    ):
        digest = approval[field]
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ValueError(f"Stored approval {field} is not a SHA-256 digest")

    payload_sha256 = approval["payload_sha256"]
    if summary.get("payload_sha256") != payload_sha256:
        raise ValueError("Compile summary payload does not match the stored approval")
    approval_receipt_sha256 = hashlib.sha256(
        json.dumps(approval, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    approval_envelope_sha256 = approval["approval_envelope_sha256"]
    provenance.update(
        {
            "type": "hermes-crunch",
            "build_id": build_dir.name,
            "payload_sha256": payload_sha256,
            "artifacts_sha256": approval["artifacts_sha256"],
            "approved_by": approval["approved_by"],
            "approved_at": approval["approved_at"],
            "approval_envelope_sha256": approval_envelope_sha256,
            "approval_receipt_sha256": approval_receipt_sha256,
        }
    )
    return provenance


def _install_verified_crunch_build(
    modules: Dict[str, Any],
    *,
    home: Path,
    profile_name: str,
    build_dir: Path,
    note: str,
    approval_envelope_sha256: str,
    webhook_trigger: Dict[str, Any] | None,
) -> Dict[str, Any]:
    """Commit one already-verified build while its install lock is held."""

    approval_path = build_dir / "approval.json"
    if approval_path.is_file():
        # Approval is an immutable receipt. An install retry reuses it instead
        # of silently minting a new timestamp and receipt for the same click.
        _, approval = modules["approval"].load_approved_payload(build_dir)
        if approval.get("approval_envelope_sha256") != approval_envelope_sha256:
            raise HTTPException(
                status_code=409,
                detail=(
                    "The stored approval does not cover this executable payload. "
                    "Recompile and review it again."
                ),
            )
    else:
        approval = modules["approval"].approve_build(
            build_dir,
            approved_by="dashboard",
            note=note or None,
            approval_envelope_sha256=approval_envelope_sha256,
        )
    payload, stored_approval = modules["approval"].load_approved_payload(build_dir)
    provenance = _approved_build_provenance(
        build_dir, stored_approval or approval
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
    webhook_install = None
    installed_job_id = ""
    try:
        install = modules["hermes"].install_cron_job(
            payload,
            hermes_home=home,
            provenance=provenance,
        )
        installed_job_id = str(install.get("job_id") or "").strip()
        if webhook_trigger is not None:
            if not installed_job_id:
                raise RuntimeError(
                    "Hermes did not return the installed workflow job id"
                )
            workflow_name = str(payload.get("name") or "workflow")
            route_name = (
                "crunch-"
                + crunch_slug(workflow_name, "workflow")[:32].lower()
                + "-"
                + installed_job_id[:12].lower()
            )
            webhook_install = modules["hermes"].install_webhook_trigger(
                hermes_home=home,
                name=route_name,
                job_id=installed_job_id,
                description=str(
                    webhook_trigger.get("description") or workflow_name
                ),
                events=[
                    str(item) for item in webhook_trigger.get("events") or []
                ],
                filters=webhook_trigger.get("filters"),
            )
        modules["hermes"].activate_cron_job(
            installed_job_id,
            provenance["install_idempotency_key"],
            hermes_home=home,
        )
    except Exception:
        if (
            isinstance(webhook_install, dict)
            and not webhook_install.get("idempotent_replay")
            and webhook_install.get("name")
            and installed_job_id
        ):
            try:
                modules["hermes"].remove_webhook_trigger(
                    hermes_home=home,
                    name=str(webhook_install["name"]),
                    job_id=installed_job_id,
                )
            except Exception:
                logging.getLogger(__name__).exception(
                    "Could not remove failed Crunch webhook route %s",
                    webhook_install.get("name"),
                )
        if installed_job_id:
            try:
                modules["hermes"].abort_cron_job(
                    installed_job_id,
                    provenance["install_idempotency_key"],
                    hermes_home=home,
                )
            except Exception:
                logging.getLogger(__name__).exception(
                    "Could not abort failed Crunch Cron reservation %s",
                    installed_job_id,
                )
        promotion.rollback()
        raise
    promotion.commit()
    return {
        "profile": profile_name,
        "build_dir": str(build_dir),
        "approval": stored_approval or approval,
        "payload": payload,
        "install": install,
        "webhook": webhook_install,
    }


def install_crunch_build(
    modules: Dict[str, Any],
    *,
    home: Path,
    profile_name: str,
    build_dir: Path,
    note: str,
    expected_approval_envelope_sha256: str,
    webhook_trigger: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    if not (build_dir / "cronjob-payload.json").is_file():
        raise HTTPException(status_code=404, detail="Compiled Crunch payload not found")
    guard_path = build_dir / "install-guard.json"
    if not guard_path.is_file():
        raise HTTPException(
            status_code=409,
            detail="The compiled install readiness guard is missing. Rebuild and review the workflow.",
        )
    try:
        guard = json.loads(guard_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=409,
            detail="The compiled install readiness guard is invalid. Rebuild and review the workflow.",
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
            detail="The compiled install readiness guard is invalid. Rebuild and review the workflow.",
        )
    if guard["requires_review_resolution"]:
        raise HTTPException(
            status_code=409,
            detail="Resolve every workflow review detail before installation",
        )
    envelope_path = build_dir / "approval-envelope.json"
    if not envelope_path.is_file():
        raise HTTPException(status_code=409, detail="Compile the approval envelope before approval")
    actual_approval_envelope_sha256 = hashlib.sha256(envelope_path.read_bytes()).hexdigest()
    if (
        not expected_approval_envelope_sha256
        or expected_approval_envelope_sha256 != actual_approval_envelope_sha256
    ):
        raise HTTPException(status_code=409, detail="The executable approval payload changed. Review the latest build.")
    try:
        envelope, verified_envelope_sha256 = (
            modules["approval"].verify_approval_envelope(
                build_dir,
                expected_sha256=expected_approval_envelope_sha256,
            )
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                "The executable approval payload no longer matches the reviewed "
                "build. Rebuild and review the workflow."
            ),
        ) from exc
    if verified_envelope_sha256 != actual_approval_envelope_sha256:
        raise HTTPException(
            status_code=409,
            detail="The executable approval payload changed. Review the latest build.",
        )
    if not isinstance(envelope, dict) or envelope.get("install_guard") != guard:
        raise HTTPException(
            status_code=409,
            detail="The install readiness guard is not covered by the approval payload. Rebuild and review the workflow.",
        )
    try:
        payload = read_crunch_json(build_dir / "cronjob-payload.json")
    except HTTPException as exc:
        raise HTTPException(
            status_code=409,
            detail="The compiled workflow payload is invalid. Rebuild and review the workflow.",
        ) from exc
    install_readiness = crunch_install_readiness(
        home=home,
        authorization=payload.get("authorization"),
        run_mode="webhook" if webhook_trigger is not None else "schedule",
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
    require_approved_payload_install_ready(home=home, payload=payload)
    with _crunch_install_lock(home=home, build_dir=build_dir):
        return _install_verified_crunch_build(
            modules,
            home=home,
            profile_name=profile_name,
            build_dir=build_dir,
            note=note,
            approval_envelope_sha256=actual_approval_envelope_sha256,
            webhook_trigger=webhook_trigger,
        )
