"""V2 workflow trace native recorder bridge for Hermes Crunch."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import psutil
from fastapi import HTTPException
PLUGIN_ROOT = Path(__file__).resolve().parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from chrome_capture import (  # noqa: E402
    _active_state_path as chrome_active_state_path,
    active_chrome_recording,
    activate_chrome_capture,
    chrome_provider_preflight,
    deactivate_chrome_capture,
)
from recording_bundle import (  # noqa: E402
    is_trace_recording_dir,
    read_json,
    read_jsonl,
    recording_key,
    recordings_root,
    write_json,
)
from hermes_crunch.trace_schema import SCHEMA_VERSION, utc_now  # noqa: E402
from hermes_crunch.evidence import (  # noqa: E402
    build_evidence_pack,
    capture_completeness,
    finalize_evidence_pack,
    seal_capture,
)


log = logging.getLogger(__name__)
RECORDER_SCHEMA = SCHEMA_VERSION
_CAPTURE_AUTHORIZATION_BLOCK_DETAIL = (
    "The recording page could not establish a trusted Chrome capture authorization."
)

_PERMISSION_UNAVAILABLE_MESSAGE = "Native recorder permission status is unavailable. Hermes will request permissions before recording."
_START_PERMISSION_BLOCK_DETAIL = (
    "Missing required macOS recorder permissions: Accessibility, Screen Recording, and Input Monitoring "
    "must be granted before recording."
)
_START_CHROME_PROVIDER_BLOCK_DETAIL = "Hermes Chrome provider must be ready before recording."
_START_CHROME_LIVE_BLOCK_DETAIL = (
    "Hermes Chrome provider is installed but its page recorder did not join the recording. "
    "Reload the Hermes Chrome Provider extension and the Workflows page, then try again."
)
_LIVE_CHROME_JOIN_TIMEOUT_SECONDS = 4.0
_LIVE_CHROME_JOIN_POLL_SECONDS = 0.05
_NATIVE_CAPTURE_READY_TIMEOUT_SECONDS = 8.0


def _validated_capture_authorization(
    nonce: str,
    dashboard_origin: str,
) -> tuple[str, str]:
    normalized_nonce = str(nonce or "").strip().lower()
    if (
        len(normalized_nonce) != 64
        or any(character not in "0123456789abcdef" for character in normalized_nonce)
    ):
        raise HTTPException(
            status_code=409,
            detail=_CAPTURE_AUTHORIZATION_BLOCK_DETAIL,
        )
    normalized_origin = str(dashboard_origin or "").strip()
    try:
        parsed = urlsplit(normalized_origin)
        hostname = str(parsed.hostname or "").lower()
        port = parsed.port
    except (TypeError, ValueError):
        parsed = None
        hostname = ""
        port = None
    if (
        parsed is None
        or parsed.scheme != "http"
        or hostname not in {"127.0.0.1", "localhost", "::1"}
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(
            status_code=409,
            detail=_CAPTURE_AUTHORIZATION_BLOCK_DETAIL,
        )
    canonical_host = f"[{hostname}]" if ":" in hostname else hostname
    return normalized_nonce, f"http://{canonical_host}:{port}"
_MODERN_PRIVACY_SETTINGS_URL = "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?"
_RECORDER_PERMISSION_REQUIREMENTS = (
    ("accessibilityTrusted", "Accessibility permission is not granted"),
    ("screenCaptureGranted", "Screen Recording permission is not granted"),
    ("listenEventGranted", "Input Monitoring permission is not granted"),
)
_RECORDER_PERMISSION_SETTINGS_PANES = {
    "accessibilityTrusted": "Privacy_Accessibility",
    "screenCaptureGranted": "Privacy_ScreenCapture",
    "listenEventGranted": "Privacy_ListenEvent",
}
_LIVE_CHROME_REQUIREMENTS = {
    "sessionActive": True,
    "desiredProvider": "chrome",
    "effectiveProvider": "chrome",
    "chromeSegmentActive": True,
    "contentScriptReady": True,
    "activeTargetAttached": True,
}


@contextmanager
def _claim_chrome_capture_start(home: Path):
    """Serialize one root's capture start transaction across Hermes profiles."""

    active_state_path = chrome_active_state_path(home)
    if active_state_path is None:  # A concrete home always resolves a root.
        raise RuntimeError("Hermes Chrome capture root is unavailable")
    claim_root = active_state_path.parent
    claim_root.mkdir(parents=True, exist_ok=True)
    claim_fd = os.open(claim_root, os.O_RDONLY)
    try:
        fcntl.flock(claim_fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(claim_fd, fcntl.LOCK_UN)
        os.close(claim_fd)


def _empty_permission_state() -> Dict[str, Any]:
    return {
        "platform": "macos" if os.sys.platform == "darwin" else os.sys.platform,
        "accessibilityTrusted": None,
        "screenCaptureGranted": None,
        "listenEventGranted": None,
        "source": "unavailable",
    }


def _is_macos_permission_state(permission_state: Dict[str, Any]) -> bool:
    platform = str(permission_state.get("platform") or ("macos" if os.sys.platform == "darwin" else os.sys.platform))
    return platform == "macos"


def _normalize_permission_state(raw: Any) -> Dict[str, Any]:
    permission_state = dict(raw) if isinstance(raw, dict) else _empty_permission_state()
    permission_state["source"] = "native_service"
    return permission_state


def _recorder_permission_needs_attention(permission_state: Dict[str, Any], *, require_verified: bool) -> List[str]:
    if not _is_macos_permission_state(permission_state):
        return []
    if permission_state.get("source") != "native_service":
        return [_PERMISSION_UNAVAILABLE_MESSAGE] if require_verified else []
    return [message for key, message in _RECORDER_PERMISSION_REQUIREMENTS if permission_state.get(key) is not True]


def _first_missing_recorder_permission_key(permission_state: Dict[str, Any]) -> Optional[str]:
    for key, _message in _RECORDER_PERMISSION_REQUIREMENTS:
        if permission_state.get(key) is not True:
            return key
    return None


def _chrome_provider_needs_attention(chrome_provider: Dict[str, Any]) -> List[str]:
    return [str(item) for item in chrome_provider.get("needs_attention", []) if item]


def _start_permission_block_detail(permission_attention: List[str]) -> str:
    missing = []
    for message in permission_attention:
        if message.endswith(" permission is not granted"):
            missing.append(message[: -len(" permission is not granted")])
    if not missing:
        return _START_PERMISSION_BLOCK_DETAIL
    return f"Missing required macOS recorder permissions: {', '.join(missing)} must be granted before recording."


def _privacy_settings_url(pane: str = "Privacy") -> str:
    return f"{_MODERN_PRIVACY_SETTINGS_URL}{pane}"


def _open_macos_privacy_settings(
    permission_state: Optional[Dict[str, Any]] = None,
    *,
    client: Optional[Path] = None,
    root: Optional[Path] = None,
    permission_key: Optional[str] = None,
) -> None:
    pane = _RECORDER_PERMISSION_SETTINGS_PANES.get(permission_key or "", "Privacy")
    if pane == "Privacy" and isinstance(permission_state, dict):
        for key, _message in _RECORDER_PERMISSION_REQUIREMENTS:
            if permission_state.get(key) is not True:
                pane = _RECORDER_PERMISSION_SETTINGS_PANES[key]
                break
    if client is not None and root is not None and pane != "Privacy":
        try:
            result = _run_client(client, root=root, action="open-settings", settings_pane=pane)
            if result.get("settingsPane") == pane:
                return
        except Exception:
            pass
    subprocess.Popen(
        ["open", _privacy_settings_url(pane)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def active_native_recording(*, home: Path, profile_name: str) -> Optional[Dict[str, Any]]:
    root = recordings_root(home)
    service_process_state = _reconcile_scoped_recorder_service(
        scope_root=root,
        client=_native_client_path(),
        remote_reason=_remote_backend_reason(),
        trigger=False,
    )
    return _active_recording_summary(home, profile_name, service_process_state)


def _active_recording_summary(
    home: Path,
    profile_name: str,
    service_process_state: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    root = recordings_root(home)
    for process in service_process_state.get("activeSessions") or []:
        session_state = process.get("sessionState") if isinstance(process, dict) else None
        recording_dir = Path(str((session_state or {}).get("recordingDir") or "")).expanduser()
        if not recording_dir.is_dir() or not _path_is_at_or_under(recording_dir, root):
            continue
        metadata = _read_optional_json(recording_dir / "metadata.json")
        if metadata.get("profile") not in (None, "", profile_name):
            continue
        return {
            "recording_dir": str(recording_dir),
            "recording_key": recording_key(home, recording_dir),
            "name": metadata.get("name") or metadata.get("workflow_id") or recording_dir.parent.name,
            "instructions": metadata.get("instructions") or "",
            "status": "recording",
        }
    return None


def _request_native_recorder_permissions(
    client: Path,
    *,
    root: Path,
    permission_key: Optional[str] = None,
) -> Dict[str, Any]:
    if permission_key is not None and permission_key not in _RECORDER_PERMISSION_SETTINGS_PANES:
        raise ValueError(f"unsupported recorder permission: {permission_key}")
    kwargs: Dict[str, Any] = {"root": root, "action": "request-permissions"}
    if permission_key is not None:
        kwargs["permission_key"] = permission_key
    result = _run_client(client, **kwargs)
    return _normalize_permission_state(result.get("permissionState"))


def _request_next_native_recorder_permission(client: Path, *, root: Path) -> Dict[str, Any]:
    result = _run_client(client, root=root, action="permission-status")
    current = _normalize_permission_state(result.get("permissionState"))
    permission_key = _first_missing_recorder_permission_key(current)
    if permission_key is None:
        return current
    return _request_native_recorder_permissions(client, root=root, permission_key=permission_key)


def start_native_recording(
    *,
    home: Path,
    profile_name: str,
    workflow_id: str,
    name: str,
    instructions: str,
    schedule: str,
    capture_authorization_nonce: str,
    capture_dashboard_origin: str,
    trigger_requested: bool = False,
    max_duration_seconds: int = 1800,
) -> Dict[str, Any]:
    nonce, dashboard_origin = _validated_capture_authorization(
        capture_authorization_nonce,
        capture_dashboard_origin,
    )
    with _claim_chrome_capture_start(home):
        return _start_native_recording(
            home=home,
            profile_name=profile_name,
            workflow_id=workflow_id,
            name=name,
            instructions=instructions,
            schedule=schedule,
            capture_authorization_nonce=nonce,
            capture_dashboard_origin=dashboard_origin,
            trigger_requested=trigger_requested,
            max_duration_seconds=max_duration_seconds,
        )


def _start_native_recording(
    *,
    home: Path,
    profile_name: str,
    workflow_id: str,
    name: str,
    instructions: str,
    schedule: str,
    capture_authorization_nonce: str,
    capture_dashboard_origin: str,
    trigger_requested: bool,
    max_duration_seconds: int,
) -> Dict[str, Any]:
    _ensure_local_backend()
    client = _require_native_client()
    recordings = recordings_root(home)
    workflow_root = recordings / workflow_id
    remote_reason = _remote_backend_reason()
    service_process_repair = _reconcile_scoped_recorder_service(
        scope_root=recordings,
        client=client,
        remote_reason=remote_reason,
        trigger=True,
    )
    if service_process_repair.get("activeSessions"):
        raise HTTPException(status_code=409, detail="Another Crunch recording is already active. Stop it before starting a new recording.")
    global_chrome_recording = active_chrome_recording(home)
    if global_chrome_recording:
        raise HTTPException(
            status_code=409,
            detail="Another Hermes profile is already using the Chrome recorder. Stop it before starting a new recording.",
        )
    chrome_provider = chrome_provider_preflight()
    chrome_attention = _chrome_provider_needs_attention(chrome_provider)
    log.info(
        "Crunch recording start requested workflow_id=%s profile=%s chrome_preflight=%s",
        workflow_id,
        profile_name,
        chrome_provider.get("status"),
    )
    if chrome_attention:
        raise HTTPException(status_code=409, detail=f"{_START_CHROME_PROVIDER_BLOCK_DETAIL} {'; '.join(chrome_attention)}")
    try:
        permission_state = _request_next_native_recorder_permission(client, root=recordings)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Native recorder permission request failed: {exc}") from exc
    permission_attention = _recorder_permission_needs_attention(permission_state, require_verified=True)
    if permission_attention:
        _open_macos_privacy_settings(
            permission_state,
            client=client,
            root=recordings,
            permission_key=_first_missing_recorder_permission_key(permission_state),
        )
        _reconcile_scoped_recorder_service(
            scope_root=recordings,
            client=client,
            remote_reason=remote_reason,
            trigger=True,
        )
        raise HTTPException(status_code=409, detail=_start_permission_block_detail(permission_attention))
    recording_dir: Optional[Path] = None
    try:
        try:
            result = _run_client(
                client,
                root=workflow_root,
                action="start",
                max_duration_seconds=max_duration_seconds,
                show_controls=False,
            )
        except RuntimeError as exc:
            if "Missing required macOS recorder permissions" in str(exc):
                _open_macos_privacy_settings()
                _reconcile_scoped_recorder_service(
                    scope_root=recordings,
                    client=client,
                    remote_reason=remote_reason,
                    trigger=True,
                )
                raise HTTPException(status_code=409, detail=_START_PERMISSION_BLOCK_DETAIL) from exc
            raise
        recording_dir = _result_recording_dir(result)
        log.info("Crunch native recording started workflow_id=%s recording_dir=%s", workflow_id, recording_dir)
        native_capture_state = _wait_for_native_capture_ready(recording_dir)
        if not native_capture_state:
            failed_native_state = _read_optional_json(recording_dir / "capture-state.json")
            log.error("Crunch native input capture did not become ready recording_dir=%s", recording_dir)
            raise HTTPException(
                status_code=503,
                detail=_native_live_block_detail(recording_dir, failed_native_state),
            )
        try:
            _verify_atomic_bundle_write(recording_dir)
        except OSError as exc:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Hermes could not create an atomic recording bundle. Check disk space and write access for "
                    f"{recording_dir.parent}, then try again. ({type(exc).__name__})"
                ),
            ) from exc
        log.info(
            "Crunch native capture stack ready recording_dir=%s health=%s",
            recording_dir,
            json.dumps(native_capture_state.get("eventTapHealth", {}), sort_keys=True, default=str),
        )
        activate_chrome_capture(
            home=home,
            profile_name=profile_name,
            recording_dir=recording_dir,
            workflow_id=workflow_id,
            title=name or workflow_id,
            capture_authorization_sha256=hashlib.sha256(
                capture_authorization_nonce.encode("ascii")
            ).hexdigest(),
            capture_dashboard_origin=capture_dashboard_origin,
        )
        _patch_session_metadata(
            recording_dir,
            {
                "schemaVersion": RECORDER_SCHEMA,
                "workflow_id": workflow_id,
                "name": name.strip(),
                "instructions": instructions.strip(),
                "schedule": schedule.strip(),
                "trigger_requested": bool(trigger_requested),
                "profile": profile_name,
                "source": "dashboard",
                "recorder_version": 3,
                "review": {"inputs": [], "validations": []},
                "variable_inputs": [],
                "validations": [],
                "input_schema": {"type": "object", "properties": {}},
                "transport": "native-helper",
                "native_helper_path": str(client),
                "service_process_repair": service_process_repair,
                "capture_providers": ["native", "chrome"],
                "capture_provider_mode": "adaptive",
            },
        )
        live_chrome_state = _wait_for_live_chrome_provider(recording_dir)
        if not live_chrome_state:
            failed_chrome_state = _read_optional_json(recording_dir / "capture-state.json")
            log.warning(
                "Crunch Chrome provider join failed recording_dir=%s state=%s",
                recording_dir,
                json.dumps(failed_chrome_state, sort_keys=True, default=str),
            )
            raise HTTPException(status_code=409, detail=_chrome_live_block_detail(failed_chrome_state))
        log.info(
            "Crunch Chrome provider joined recording_dir=%s reason=%s tab_id=%s url=%s",
            recording_dir,
            live_chrome_state.get("reason"),
            live_chrome_state.get("activeTabId"),
            live_chrome_state.get("activeUrl"),
        )
        return recording_status(home=home, recording_dir=recording_dir)
    except Exception as exc:
        recording_dir = recording_dir or _failed_start_recording_dir(workflow_root)
        if recording_dir is None:
            raise
        rollback = _abort_unready_recording(
            client=client,
            home=home,
            recording_dir=recording_dir,
        )
        if rollback["shutdownProven"]:
            raise
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        raise HTTPException(
            status_code=503,
            detail=(
                f"{detail} Hermes preserved the incomplete recording at {recording_dir} because native and "
                "Chrome shutdown could not be proven. Verify both recorders have stopped before starting "
                "another capture."
            ),
        ) from exc


def _wait_for_live_chrome_provider(
    recording_dir: Path,
    *,
    timeout_seconds: float = _LIVE_CHROME_JOIN_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        state = _read_optional_json(recording_dir / "capture-state.json")
        if not _missing_live_chrome_signals(state):
            return state
        if time.monotonic() >= deadline:
            return {}
        time.sleep(_LIVE_CHROME_JOIN_POLL_SECONDS)


def _missing_live_chrome_signals(state: Dict[str, Any]) -> List[str]:
    missing = [
        key
        for key, expected in _LIVE_CHROME_REQUIREMENTS.items()
        if state.get(key) != expected
    ]
    if state.get("activeTabId") is None:
        missing.append("activeTabId")
    return missing


def _wait_for_native_capture_ready(
    recording_dir: Path,
    *,
    timeout_seconds: float = _NATIVE_CAPTURE_READY_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        state = _read_optional_json(recording_dir / "capture-state.json")
        if not _missing_native_capture_signals(recording_dir, state):
            return state
        if time.monotonic() >= deadline:
            return {}
        time.sleep(_LIVE_CHROME_JOIN_POLL_SECONDS)


def _missing_native_capture_signals(
    recording_dir: Path,
    state: Dict[str, Any],
) -> List[str]:
    event_tap = state.get("eventTapHealth") if isinstance(state.get("eventTapHealth"), dict) else {}
    accessibility = state.get("accessibilityHealth") if isinstance(state.get("accessibilityHealth"), dict) else {}
    visual = state.get("visualCaptureHealth") if isinstance(state.get("visualCaptureHealth"), dict) else {}
    missing: List[str] = []
    if event_tap.get("tapCreated") is not True or event_tap.get("tapEnabled") is not True:
        missing.append("Input Monitoring capture")
    if accessibility.get("ready") is not True:
        missing.append("Accessibility capture")
    if visual.get("ready") is not True or not visual.get("firstFrameAt"):
        missing.append("first Screen Recording frame")
    elif not read_jsonl(recording_dir / "visual" / "frames.jsonl"):
        missing.append("persisted Screen Recording frame")
    return missing


def _native_live_block_detail(recording_dir: Path, state: Dict[str, Any]) -> str:
    visual = state.get("visualCaptureHealth") if isinstance(state.get("visualCaptureHealth"), dict) else {}
    missing = _missing_native_capture_signals(recording_dir, state)
    detail = "Hermes did not start a complete recorder transaction."
    if missing:
        detail += " Missing: " + ", ".join(missing) + "."
    if visual.get("error"):
        detail += f" Screen capture reported: {visual['error']}."
    detail += " Keep Chrome visible, verify the named Privacy & Security permission, and try Start recording again."
    return detail


def _verify_atomic_bundle_write(recording_dir: Path) -> None:
    """Prove the live bundle supports durable same-directory replacement."""

    for required in ("metadata.json", "session.json", "trace.jsonl"):
        if not (recording_dir / required).is_file():
            raise OSError(f"recording bundle is missing {required}")
    token = uuid.uuid4().hex.encode("ascii")
    pending = recording_dir / f".start-readiness-{uuid.uuid4().hex}.tmp"
    committed = recording_dir / f".start-readiness-{uuid.uuid4().hex}"
    try:
        with pending.open("xb") as handle:
            handle.write(token)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(pending, committed)
        if committed.read_bytes() != token:
            raise OSError("atomic recording probe content mismatch")
    finally:
        pending.unlink(missing_ok=True)
        committed.unlink(missing_ok=True)


def _chrome_live_block_detail(state: Dict[str, Any]) -> str:
    missing = _missing_live_chrome_signals(state)
    reason = str(state.get("reason") or "no_capture_state")
    detail = f"{_START_CHROME_LIVE_BLOCK_DETAIL} Last provider state: {reason}."
    if missing:
        detail += f" Missing live signals: {', '.join(missing)}."
    return detail


def _failed_start_recording_dir(workflow_root: Path) -> Optional[Path]:
    state = _active_recording_state(workflow_root)
    raw = str(state.get("recordingDir") or "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    if candidate.is_dir() and _path_is_at_or_under(candidate, workflow_root):
        return candidate
    return None


def _native_recording_shutdown_proven(recording_dir: Path) -> bool:
    recording_dir = _soft_resolve(recording_dir)
    service_state = _read_optional_json(recording_dir.parent / "service-state.json")
    active_path = str(service_state.get("activeSessionPath") or "").strip()
    if active_path and _soft_resolve(Path(active_path)) == recording_dir:
        return False
    metadata = _read_optional_json(recording_dir / "metadata.json")
    return metadata.get("active") is not True and str(metadata.get("state") or "") != "recording"


def _stop_owned_native_recording_for_cleanup(
    *,
    client: Optional[Path],
    recording_dir: Path,
    stop_if_active: bool = True,
) -> Dict[str, Any]:
    """Stop or verify only the exact owner; fail closed on uncertain state."""

    target = _soft_resolve(recording_dir)
    workflow_root = target.parent
    try:
        processes = _scoped_recorder_processes(workflow_root, strict=True)
    except RuntimeError as exc:
        return {
            "shutdownProven": False,
            "errors": [f"native_process_query:{type(exc).__name__}:{exc}"],
            "processes": [],
        }
    exact_processes = [
        process
        for process in processes
        if str(process.get("root") or "").strip()
        and _soft_resolve(Path(str(process["root"]))) == workflow_root
    ]
    unexpected_processes = [process for process in processes if process not in exact_processes]
    errors: List[str] = []
    status: Optional[Dict[str, Any]] = None
    if client is not None:
        try:
            status = _run_client(client, root=workflow_root, action="status")
        except Exception as exc:
            errors.append(f"native_status:{type(exc).__name__}:{exc}")

    if status is None:
        return {
            "shutdownProven": not processes and _native_recording_shutdown_proven(target),
            "errors": errors,
            "processes": processes,
        }

    raw_owner = str(status.get("eventStreamRootPath") or "").strip()
    owner = _soft_resolve(Path(raw_owner)) if raw_owner else None
    if owner == target and stop_if_active:
        try:
            _run_client(client, root=workflow_root, action="stop")
        except Exception as exc:
            errors.append(f"native_stop:{type(exc).__name__}:{exc}")
        try:
            status = _run_client(client, root=workflow_root, action="status")
        except Exception as exc:
            errors.append(f"native_status_after_stop:{type(exc).__name__}:{exc}")
            try:
                remaining = _scoped_recorder_processes(workflow_root, strict=True)
            except RuntimeError as process_exc:
                errors.append(f"native_process_query:{type(process_exc).__name__}:{process_exc}")
                return {
                    "shutdownProven": False,
                    "errors": errors,
                    "processes": [],
                }
            return {
                "shutdownProven": not remaining and _native_recording_shutdown_proven(target),
                "errors": errors,
                "processes": remaining,
            }
        raw_owner = str(status.get("eventStreamRootPath") or "").strip()
        owner = _soft_resolve(Path(raw_owner)) if raw_owner else None

    shutdown_proven = (
        not unexpected_processes
        and len(exact_processes) <= 1
        and status.get("active") is False
        and owner == workflow_root
        and _native_recording_shutdown_proven(target)
    )
    return {
        "shutdownProven": shutdown_proven,
        "errors": errors,
        "processes": processes,
        "status": status,
    }


def _chrome_recording_shutdown_proven(*, home: Path, recording_dir: Path) -> bool:
    recording_dir = _soft_resolve(recording_dir)
    active_path = chrome_active_state_path(home)
    active_state = _read_optional_json(active_path) if active_path else {}
    raw_active_recording = str(active_state.get("recording_dir") or "").strip()
    if raw_active_recording and _soft_resolve(Path(raw_active_recording)) == recording_dir:
        return False
    capture_state = _read_optional_json(recording_dir / "capture-state.json")
    return (
        capture_state.get("sessionActive") is not True
        and capture_state.get("chromeSegmentActive") is not True
    )


def _abort_unready_recording(*, client: Path, home: Path, recording_dir: Path) -> Dict[str, Any]:
    errors: List[str] = []
    native_cleanup = _stop_owned_native_recording_for_cleanup(
        client=client,
        recording_dir=recording_dir,
    )
    errors.extend(native_cleanup["errors"])
    try:
        deactivate_chrome_capture(home=home, recording_dir=recording_dir)
    except Exception as exc:
        errors.append(f"chrome_stop:{type(exc).__name__}:{exc}")
    native_stopped = bool(native_cleanup["shutdownProven"])
    chrome_stopped = _chrome_recording_shutdown_proven(home=home, recording_dir=recording_dir)
    shutdown_proven = native_stopped and chrome_stopped
    bundle_deleted = False
    if shutdown_proven:
        try:
            shutil.rmtree(recording_dir)
            bundle_deleted = not recording_dir.exists()
        except OSError as exc:
            errors.append(f"bundle_delete:{type(exc).__name__}:{exc}")
    else:
        try:
            _patch_session_metadata(
                recording_dir,
                {
                    "start_rollback": {
                        "shutdown_proven": False,
                        "native_stopped": native_stopped,
                        "chrome_stopped": chrome_stopped,
                        "errors": errors,
                        "recorded_at": utc_now(),
                    },
                },
            )
        except Exception:
            log.exception("Crunch could not persist failed-start recovery metadata recording_dir=%s", recording_dir)
    return {
        "shutdownProven": shutdown_proven,
        "nativeStopped": native_stopped,
        "chromeStopped": chrome_stopped,
        "bundleDeleted": bundle_deleted,
        "errors": errors,
    }


def stop_native_recording(
    *,
    home: Path,
    recording_dir: Path,
    chrome_finalize_ok: Optional[bool] = None,
    chrome_finalize_error: str = "",
) -> Dict[str, Any]:
    client = _require_native_client()
    workflow_root = recording_dir.parent
    native_stop_result: Dict[str, Any] = {}
    native_stop_error: Optional[Exception] = None
    for _attempt in range(2):
        try:
            native_stop_result = _run_client(client, root=workflow_root, action="stop")
            native_stop_error = None
            break
        except Exception as exc:
            native_stop_error = exc
    native_shutdown = _stop_owned_native_recording_for_cleanup(
        client=client,
        recording_dir=recording_dir,
        stop_if_active=False,
    )
    if not native_shutdown["shutdownProven"]:
        errors = [
            str(item).strip()
            for item in native_shutdown.get("errors") or []
            if str(item).strip()
        ]
        detail = "; ".join(errors) or str(
            native_stop_error
            or "recorder service shutdown could not be proven"
        )
        raise HTTPException(status_code=503, detail=f"Native recorder stop failed: {detail}") from native_stop_error
    native_finalization = (
        dict(native_stop_result.get("captureFinalization"))
        if isinstance(native_stop_result.get("captureFinalization"), dict)
        else {"ok": native_stop_error is None}
    )
    if native_stop_error is not None:
        native_finalization.update({
            "ok": False,
            "warning": "native_stop_response_unverified",
            "error": f"{type(native_stop_error).__name__}: {native_stop_error}",
        })
    chrome_finalization: Dict[str, Any] = {
        "ok": chrome_finalize_ok is not False,
        **(
            {
                "warning": "chrome_provider_continuity_unverified",
                "error": chrome_finalize_error.strip()[:500] or "Chrome capture continuity was not verified",
            }
            if chrome_finalize_ok is False
            else {}
        ),
    }
    try:
        deactivate_chrome_capture(home=home, recording_dir=recording_dir)
    except Exception as exc:
        # The native capture is already safely stopped. Chrome finalization is
        # a source of continuity diagnostics, not authority to keep a recording
        # open or prevent a best-effort draft.
        log.warning(
            "Crunch Chrome finalization failed after native stop recording_dir=%s: %s",
            recording_dir,
            exc,
        )
        chrome_finalization = {
            "ok": False,
            "warning": "chrome_provider_finalization_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
    _patch_session_metadata(
        recording_dir,
        {
            "schemaVersion": RECORDER_SCHEMA,
            "evidence_pack_path": "evidence-pack.json",
            "capture_manifest_path": "capture-manifest.json",
            "native_finalization": native_finalization,
            "chrome_finalization": chrome_finalization,
        },
    )
    evidence_pack: Dict[str, Any]
    capture_verdict: Dict[str, Any]
    try:
        # Assemble capsules once after all provider diagnostics are persisted.
        # Long employee workflows may contain thousands of raw rows and frames;
        # rebuilding the pack would repeat correlation work unnecessarily.
        evidence_pack = build_evidence_pack(recording_dir)
        capture_verdict = capture_completeness(recording_dir, evidence_pack)
        evidence_pack = finalize_evidence_pack(
            recording_dir,
            evidence_pack,
            verdict=capture_verdict,
        )
    except Exception as exc:
        # The native recorder has already stopped. Derived indexing is useful
        # but reconstructable, so preserve the readable trace and return a
        # stopped recording that the drafter can consume best-effort.
        log.exception("Crunch evidence assembly degraded recording_dir=%s", recording_dir)
        capture_verdict = {
            "ready": False,
            "reasons": [{"code": "evidence_assembly_failed", "error_type": type(exc).__name__}],
            "frame_count": 0,
            "media_paths": [],
        }
        evidence_pack = {
            "schemaVersion": "hermes-crunch-evidence-v1",
            "recording_schema": RECORDER_SCHEMA,
            "session_id": recording_dir.name,
            "workflow_id": recording_dir.parent.name,
            "summary": {"action_count": 0, "outcome_evidence_count": 0},
            "actions": [],
            "artifacts": [],
            "capture_verdict": capture_verdict,
        }
        try:
            write_json(recording_dir / "evidence-pack.json", evidence_pack)
        except Exception:
            log.exception("Crunch fallback evidence warning could not be written recording_dir=%s", recording_dir)

    _patch_session_metadata(
        recording_dir,
        {"capture_verdict": capture_verdict},
    )
    try:
        seal_capture(recording_dir, evidence_pack=evidence_pack)
    except Exception as exc:
        log.exception("Crunch capture sealing degraded recording_dir=%s", recording_dir)
        seal_reason = {"code": "capture_seal_failed", "error_type": type(exc).__name__}
        reasons = capture_verdict.get("reasons") if isinstance(capture_verdict.get("reasons"), list) else []
        capture_verdict = {**capture_verdict, "ready": False, "reasons": [*reasons, seal_reason]}
        try:
            evidence_pack = finalize_evidence_pack(recording_dir, evidence_pack, verdict=capture_verdict)
            _patch_session_metadata(
                recording_dir,
                {"capture_verdict": capture_verdict},
            )
        except Exception:
            log.exception("Crunch capture seal warning could not be persisted recording_dir=%s", recording_dir)
    _reconcile_scoped_recorder_service(
        scope_root=recordings_root(home),
        client=client,
        remote_reason=_remote_backend_reason(),
        trigger=True,
    )
    return {"status": "stopped"}


def discard_native_recording(*, home: Path, recording_dir: Path) -> Dict[str, Any]:
    """End a dashboard recording without destroying its evidence bundle.

    Dismissal is a terminal lifecycle transition, not a filesystem operation.
    Keeping the sealed bundle makes the outcome durable across reloads and gives
    support a recoverable artifact if the native stop path fails midway.
    """
    state = _recording_state(
        read_json(recording_dir / "metadata.json"),
        read_json(recording_dir / "session.json"),
    )
    if state == "scheduled":
        raise HTTPException(status_code=409, detail="A scheduled workflow cannot be discarded.")
    if state == "recording":
        stop_native_recording(home=home, recording_dir=recording_dir)
    else:
        try:
            deactivate_chrome_capture(home=home, recording_dir=recording_dir)
        except Exception:
            log.debug("Recording cleanup skipped Chrome finalization", exc_info=True)
    _patch_session_metadata(
        recording_dir,
        {
            "state": "dismissed",
            "status": "dismissed",
            "dismissed_at": utc_now(),
        },
    )
    return {"status": "dismissed"}


def delete_recording_bundle(*, home: Path, recording_dir: Path) -> Dict[str, Any]:
    """Permanently delete one explicitly selected, inactive recording bundle."""

    root = _soft_resolve(recordings_root(home))
    target = _soft_resolve(recording_dir)
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Recording deletion is limited to this profile's Crunch recordings directory.",
        ) from exc
    if len(relative.parts) != 2:
        raise HTTPException(status_code=400, detail="Recording deletion requires one exact recording bundle.")
    if not target.is_dir():
        raise HTTPException(status_code=404, detail="Hermes workflow trace recording directory not found.")

    errors: List[str] = []
    with _claim_chrome_capture_start(home):
        native_cleanup = _stop_owned_native_recording_for_cleanup(
            client=_native_client_path(),
            recording_dir=target,
        )
        errors.extend(native_cleanup["errors"])
        if not _chrome_recording_shutdown_proven(home=home, recording_dir=target):
            try:
                deactivate_chrome_capture(home=home, recording_dir=target)
            except Exception as exc:
                errors.append(f"chrome_stop:{type(exc).__name__}:{exc}")

        native_stopped = bool(native_cleanup["shutdownProven"])
        chrome_stopped = _chrome_recording_shutdown_proven(home=home, recording_dir=target)
        if not native_stopped or not chrome_stopped:
            detail = (
                "Hermes refused permanent deletion because recorder shutdown could not be proven "
                f"(native_stopped={native_stopped}, chrome_stopped={chrome_stopped})."
            )
            if errors:
                detail = f"{detail} {'; '.join(errors)}"
            raise HTTPException(status_code=409, detail=detail)
        try:
            shutil.rmtree(target)
        except OSError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"Hermes could not permanently delete the recording bundle: {type(exc).__name__}: {exc}",
            ) from exc

    return {"status": "deleted", "recording_key": relative.as_posix()}


def recording_status(*, home: Path, recording_dir: Path) -> Dict[str, Any]:
    metadata = read_json(recording_dir / "metadata.json")
    session = read_json(recording_dir / "session.json")
    status = _recording_state(metadata, session)
    if status != "recording":
        _cleanup_stopped_recording(home=home, recording_dir=recording_dir)
        metadata = read_json(recording_dir / "metadata.json")
        session = read_json(recording_dir / "session.json")
        status = _recording_state(metadata, session)
    return {
        "recording_dir": str(recording_dir),
        "recording_key": recording_key(home, recording_dir),
        "metadata": metadata,
        "session": session,
        "status": status,
    }


def _recording_state(metadata: Dict[str, Any], session: Dict[str, Any]) -> str:
    return str(
        metadata.get("state")
        or session.get("status")
        or ("recording" if metadata.get("active") else "stopped")
    )


def _cleanup_stopped_recording(*, home: Path, recording_dir: Path) -> None:
    metadata = _read_optional_json(recording_dir / "metadata.json")
    session = _read_optional_json(recording_dir / "session.json")
    if (
        _recording_state(metadata, session) == "stopped"
        and (
            not (recording_dir / "evidence-pack.json").is_file()
            or not (recording_dir / "capture-manifest.json").is_file()
        )
    ):
        # The native service can stop itself at its maximum duration without a
        # live HTTP request. Complete the same host-owned Chrome/evidence/seal
        # transaction the next time the recording is observed.
        stop_native_recording(home=home, recording_dir=recording_dir)
        return
    _deactivate_chrome_capture_if_current(home=home, recording_dir=recording_dir)
    _reconcile_scoped_recorder_service(
        scope_root=recordings_root(home),
        client=_native_client_path(),
        remote_reason=_remote_backend_reason(),
        trigger=True,
    )


def _deactivate_chrome_capture_if_current(*, home: Path, recording_dir: Path) -> None:
    metadata = _read_optional_json(recording_dir / "metadata.json")
    chrome_provider = metadata.get("chrome_provider") if isinstance(metadata.get("chrome_provider"), dict) else {}
    active_pointer = active_chrome_recording(home)
    chrome_metadata = _read_optional_json(recording_dir / "chrome" / "metadata.json")
    chrome_state = str(chrome_metadata.get("state") or "")
    should_finalize = (
        chrome_provider.get("active") is True
        or str(active_pointer.get("recording_dir") or "") == str(recording_dir.expanduser().resolve())
        or ((recording_dir / "chrome").is_dir() and chrome_state not in {"", "stopped"})
    )
    if should_finalize:
        try:
            deactivate_chrome_capture(home=home, recording_dir=recording_dir)
        except Exception:
            pass


def _require_native_client() -> Path:
    client = _native_client_path()
    if client is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Native Hermes recorder helper is unavailable. Set "
                "HERMES_RECORDER_CLIENT to the signed distribution helper or "
                "build the standalone native/recorder-service overlay."
            ),
        )
    return client


def _ensure_local_backend() -> None:
    reason = _remote_backend_reason()
    if reason:
        raise HTTPException(status_code=409, detail=reason)


def _remote_backend_reason() -> Optional[str]:
    remote_url = os.environ.get("HERMES_DESKTOP_REMOTE_URL", "").strip()
    if remote_url:
        return "Native recording is local-only and is disabled while Hermes Desktop is connected to a remote backend."
    return None


def _reconcile_scoped_recorder_service(
    *,
    scope_root: Path,
    client: Optional[Path],
    remote_reason: Optional[str],
    trigger: bool,
) -> Dict[str, Any]:
    """Restart only stale idle recorder services in the current recording scope."""
    base: Dict[str, Any] = {
        "platform": "macos" if os.sys.platform == "darwin" else os.sys.platform,
        "checked": False,
        "triggered": bool(trigger),
        "scopeRoot": str(scope_root),
        "processes": [],
        "terminated": [],
        "activeSessions": [],
        "skipped": [],
        "reason": "",
    }
    if os.sys.platform != "darwin":
        base["reason"] = "not_macos"
        return base
    if remote_reason:
        base["reason"] = "remote_backend"
        return base
    if client is None:
        base["reason"] = "helper_missing"
        return base
    processes = _scoped_recorder_processes(scope_root)
    base["checked"] = True
    base["processes"] = processes
    if not processes:
        base["reason"] = "no_scoped_processes"
        return base

    candidates: List[Dict[str, Any]] = []
    for process in processes:
        root_value = str(process.get("root") or "").strip()
        if not root_value:
            skipped = dict(process)
            skipped["reason"] = "missing_root"
            base["skipped"].append(skipped)
            continue
        session_state = _active_recording_state(Path(root_value))
        process["sessionState"] = session_state
        if session_state.get("active") is True:
            base["activeSessions"].append(process)
            continue
        if session_state.get("active") is None:
            skipped = dict(process)
            skipped["reason"] = session_state.get("reason") or "unknown_session_state"
            base["skipped"].append(skipped)
            continue
        candidates.append(process)

    if candidates and trigger:
        base["terminated"] = _terminate_scoped_recorder_processes(candidates)
    if base["terminated"]:
        base["reason"] = "terminated_idle_scoped_processes"
    elif base["activeSessions"]:
        base["reason"] = "active_recordings_left_running"
    elif candidates:
        base["reason"] = "idle_scoped_processes_detected"
    elif base["skipped"]:
        base["reason"] = "no_safe_processes_to_terminate"
    else:
        base["reason"] = "no_idle_processes"
    return base


def _scoped_recorder_processes(scope_root: Path, *, strict: bool = False) -> List[Dict[str, Any]]:
    return _filter_scoped_recorder_processes(_recorder_process_rows(strict=strict), scope_root)


def _recorder_process_rows(*, strict: bool = False) -> List[Dict[str, Any]]:
    ps = shutil.which("ps") or "/bin/ps"
    try:
        proc = subprocess.run(
            [ps, "-axo", "pid=,command="],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
    except Exception as exc:
        if strict:
            raise RuntimeError(f"unable to inspect recorder processes: {type(exc).__name__}: {exc}") from exc
        return []
    if proc.returncode != 0:
        if strict:
            detail = (proc.stderr or proc.stdout or f"ps exited {proc.returncode}").strip()
            raise RuntimeError(f"unable to inspect recorder processes: {detail}")
        return []
    rows: List[Dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, command = stripped.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        rows.append({"pid": pid, "command": command.strip()})
    return rows


def _filter_scoped_recorder_processes(rows: List[Dict[str, Any]], scope_root: Path) -> List[Dict[str, Any]]:
    matches: List[Dict[str, Any]] = []
    scope = _soft_resolve(scope_root)
    for row in rows:
        pid = int(row.get("pid") or 0)
        command = str(row.get("command") or "")
        if pid <= 0 or pid == os.getpid():
            continue
        if "hermes-recorder-service" not in command and "Hermes Recorder Service" not in command:
            continue
        parsed = _parse_recorder_service_command(command)
        root_value = parsed.get("root", "")
        if root_value and not _path_is_at_or_under(Path(root_value), scope):
            continue
        if not root_value and str(scope_root) not in command:
            continue
        matches.append({
            "pid": pid,
            "root": root_value,
            "socket": parsed.get("socket", ""),
            "command": command[:500],
        })
    return matches


def _parse_recorder_service_command(command: str) -> Dict[str, str]:
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    parsed: Dict[str, str] = {"root": "", "socket": ""}
    for index, part in enumerate(parts):
        if part in {"--root", "--socket"} and index + 1 < len(parts):
            parsed[part[2:]] = parts[index + 1]
        elif part.startswith("--root="):
            parsed["root"] = part.split("=", 1)[1]
        elif part.startswith("--socket="):
            parsed["socket"] = part.split("=", 1)[1]
    return parsed


def _active_recording_state(root: Path) -> Dict[str, Any]:
    state = _read_optional_json(root / "service-state.json")
    raw_session = str(state.get("activeSessionPath") or "").strip()
    if not raw_session:
        return {"active": False, "reason": "no_active_session"}
    session_dir = Path(raw_session)
    metadata_path = session_dir / "metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "active": None,
            "reason": f"metadata_unreadable:{type(exc).__name__}",
            "recordingDir": str(session_dir),
        }
    if not isinstance(metadata, dict):
        return {"active": None, "reason": "metadata_not_object", "recordingDir": str(session_dir)}
    return {
        "active": metadata.get("active") is True,
        "state": str(metadata.get("state") or ""),
        "recordingDir": str(session_dir),
        "sessionID": str(metadata.get("sessionID") or session_dir.name),
    }


def _terminate_scoped_recorder_processes(processes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for process in processes:
        pid = int(process.get("pid") or 0)
        if pid <= 0:
            continue
        result: Dict[str, Any] = {
            "pid": pid,
            "root": process.get("root", ""),
            "signal": "SIGTERM",
            "ok": False,
        }
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.05)
            result["aliveAfterSignal"] = psutil.pid_exists(pid)
            result["ok"] = not result["aliveAfterSignal"]
        except ProcessLookupError:
            result["ok"] = True
            result["alreadyExited"] = True
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        results.append(result)
    return results


def _path_is_at_or_under(path: Path, scope: Path) -> bool:
    resolved = _soft_resolve(path)
    try:
        resolved.relative_to(scope)
        return True
    except ValueError:
        return False


def _soft_resolve(path: Path) -> Path:
    try:
        return path.expanduser().resolve(strict=False)
    except Exception:
        return Path(os.path.abspath(os.path.expanduser(str(path))))


def _read_optional_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _native_client_path() -> Optional[Path]:
    env_path = os.environ.get("HERMES_RECORDER_CLIENT", "").strip()
    candidates: list[Path] = []
    if env_path:
        candidates.append(Path(env_path).expanduser())
    overlay_path = os.environ.get("HERMES_CRUNCH_NATIVE_ROOT", "").strip()
    overlay_roots = (
        [Path(overlay_path).expanduser()]
        if overlay_path
        else [PLUGIN_ROOT / "native"]
    )
    for overlay_root in overlay_roots:
        candidates.extend(
            [
                overlay_root / "hermes-recorder" / "hermes-recorder",
                overlay_root
                / "recorder-service"
                / ".build"
                / "debug"
                / "hermes-recorder",
                overlay_root
                / "recorder-service"
                / ".build"
                / "release"
                / "hermes-recorder",
            ]
        )
        build_root = overlay_root / "recorder-service" / ".build"
        candidates.extend(sorted(build_root.glob("*/debug/hermes-recorder")))
        candidates.extend(sorted(build_root.glob("*/release/hermes-recorder")))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    return None


def _run_client(
    client: Path,
    *,
    root: Path,
    action: str,
    max_duration_seconds: Optional[int] = None,
    show_controls: Optional[bool] = None,
    settings_pane: Optional[str] = None,
    permission_key: Optional[str] = None,
) -> Dict[str, Any]:
    cmd = [str(client), "event-stream", action, "--root", str(root), "--json"]
    if max_duration_seconds:
        cmd.extend(["--max-duration-seconds", str(max_duration_seconds)])
    if show_controls is True:
        cmd.append("--show-controls")
    elif show_controls is False:
        cmd.append("--hide-controls")
    if settings_pane:
        cmd.extend(["--settings-pane", settings_pane])
    if permission_key:
        cmd.extend(["--permission", permission_key])
    proc = subprocess.run(
        cmd,
        text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=8,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"recorder client exited {proc.returncode}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"recorder client returned invalid JSON: {proc.stdout[:400]}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("recorder client returned a non-object response")
    return payload


def _result_recording_dir(result: Dict[str, Any]) -> Path:
    raw = str(result.get("eventStreamRootPath") or "").strip()
    if not raw:
        raise RuntimeError("recorder start did not return eventStreamRootPath")
    path = Path(raw).expanduser().resolve()
    if not is_trace_recording_dir(path):
        raise RuntimeError(f"recorder did not create a trace session at {path}")
    return path


def _patch_session_metadata(recording_dir: Path, values: Dict[str, Any]) -> None:
    for name in ("metadata.json", "session.json"):
        path = recording_dir / name
        data = read_json(path)
        data.update(values)
        if name == "session.json":
            data["status"] = data.get("state") or data.get("status") or "recording"
        write_json(path, data)
