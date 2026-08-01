"""Chrome adaptive capture provider logs for Record recordings."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import json
import os
import struct
import sys
import time
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from hermes_constants import get_default_hermes_root
except Exception:  # pragma: no cover - native host can run from a thin env
    get_default_hermes_root = None  # type: ignore[assignment]

from .record.trace_schema import (
    normalize_trace_row,
    utc_now,
    url_origin,
)

PLUGIN_ROOT = Path(__file__).resolve().parent


HOST_NAME = "com.hermes.chrome_capture"
ACTIVE_STATE_FILENAME = "chrome-capture-active.json"
CHROME_CAPTURE_SCHEMA = "hermes-chrome-provider-v1"
ADAPTIVE_CAPTURE_SCHEMA = "hermes-adaptive-capture-v1"
CHROME_PROTOCOL_VERSION = 4
CAPTURE_STATE_FILENAME = "capture-state.json"
MAX_SCREENSHOT_COUNT = 40
MAX_SCREENSHOT_BYTES = 5 * 1024 * 1024
DESKTOP_CAPTURE_AUTHORITY_SOURCE = "desktop"


def active_chrome_recording(home: Path) -> Dict[str, Any]:
    """Return the one live Chrome-provider recording across all Hermes profiles."""

    path = _active_state_path(home)
    state = _read_optional_json(path) if path else {}
    if state.get("active") is not True:
        return {}
    recording_dir = Path(str(state.get("recording_dir") or "")).expanduser()
    metadata = _read_optional_json(recording_dir / "metadata.json")
    if metadata.get("active") is not True or str(metadata.get("state") or "") != "recording":
        return {}
    return state


def activate_chrome_capture(
    *,
    home: Path,
    profile_name: str,
    recording_dir: Path,
    workflow_id: str,
    title: str,
    capture_authorization_sha256: str,
    capture_authorization: str,
) -> Dict[str, Any]:
    """Publish a Desktop-authorized recording for the Chrome Native Messaging host."""

    recording_dir = recording_dir.expanduser().resolve()
    raw_authorization = str(capture_authorization or "").strip().lower()
    if (
        len(raw_authorization) != 64
        or any(character not in "0123456789abcdef" for character in raw_authorization)
        or not hmac.compare_digest(
            hashlib.sha256(raw_authorization.encode("ascii")).hexdigest(),
            capture_authorization_sha256,
        )
    ):
        raise ValueError("Desktop Chrome capture authorization is invalid")
    state = {
        "schemaVersion": CHROME_CAPTURE_SCHEMA,
        "active": True,
        "profile": profile_name,
        "profile_home": str(home.expanduser().resolve()),
        "recording_dir": str(recording_dir),
        "workflow_id": workflow_id,
        "title": title,
        "capture_authorization_sha256": capture_authorization_sha256,
        "capture_authority_source": DESKTOP_CAPTURE_AUTHORITY_SOURCE,
        "desktop_capture_authorization": raw_authorization,
        "started_at": utc_now(),
        "updated_at": utc_now(),
    }
    init_chrome_bundle(recording_dir, state)
    capture_state = _write_capture_state(
        recording_dir,
        {
            "sessionActive": True,
            "effectiveProvider": "native",
            "chromeSegmentActive": False,
        },
        reason="chrome_provider_requested",
    )
    active_path = _active_state_path(home)
    if active_path:
        _write_json(active_path, state)
        try:
            os.chmod(active_path, 0o600)
        except OSError:
            pass
    _patch_recording_metadata(
        recording_dir,
        {
            "captureState": capture_state,
            "chrome_provider": _public_capture_state(state, recording_dir),
        },
    )
    return state


def deactivate_chrome_capture(*, home: Path, recording_dir: Path) -> Dict[str, Any]:
    """Finalize Chrome provider logs and clear its active pointer."""

    recording_dir = recording_dir.expanduser().resolve()
    capture_state: Dict[str, Any] = {}
    if _is_trace_recording_dir(recording_dir):
        capture_state = _write_capture_state(
            recording_dir,
            {
                "sessionActive": False,
                "desiredProvider": "native",
                "effectiveProvider": "native",
                "chromeSegmentActive": False,
                "chromeSegmentID": None,
                "lastHeartbeatAt": None,
                "activeTargetId": None,
                "activeTabId": None,
                "activeWindowId": None,
                "activeUrl": None,
                "activeTitle": None,
                "activeTargetAttached": False,
                "activeTargetUpdatedAt": None,
                "contentScriptReady": False,
                "contentScriptUpdatedAt": None,
            },
            reason="session_stopped",
        )
    manifest = finalize_chrome_bundle(recording_dir)
    path = _active_state_path(home)
    if path and path.exists():
        try:
            current = _read_json(path)
        except Exception:
            current = {}
        if str(current.get("recording_dir") or "") == str(recording_dir):
            path.unlink(missing_ok=True)
    _patch_recording_metadata(
        recording_dir,
        {
            "captureState": capture_state,
            "chrome_provider": chrome_capture_status(recording_dir),
        },
    )
    return manifest


def request_desktop_chrome_finalize(
    *,
    home: Path,
    recording_dir: Path,
) -> Dict[str, Any]:
    """Publish one recording-scoped finalize request for the Chrome extension."""

    recording_dir = recording_dir.expanduser().resolve()
    path = _active_state_path(home)
    if path is None or not path.is_file():
        raise RuntimeError("No active Chrome capture is available")
    state = _read_optional_json(path)
    if (
        state.get("active") is not True
        or state.get("capture_authority_source")
        != DESKTOP_CAPTURE_AUTHORITY_SOURCE
        or str(state.get("recording_dir") or "") != str(recording_dir)
    ):
        raise RuntimeError("Desktop Chrome capture is not active for this recording")
    request = {
        "id": uuid.uuid4().hex,
        "recordingId": str(
            _read_capture_state(recording_dir).get("sessionID")
            or recording_dir.name
        ),
        "requested_at": utc_now(),
    }
    state["desktop_finalize_request"] = request
    state.pop("desktop_finalize_ack", None)
    state["updated_at"] = utc_now()
    _write_json(path, state)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return request


def desktop_chrome_finalize_ack(
    *,
    home: Path,
    recording_dir: Path,
    request_id: str,
) -> Dict[str, Any]:
    path = _active_state_path(home)
    state = _read_optional_json(path) if path else {}
    if str(state.get("recording_dir") or "") != str(
        recording_dir.expanduser().resolve()
    ):
        return {}
    ack = state.get("desktop_finalize_ack")
    if not isinstance(ack, dict) or str(ack.get("id") or "") != request_id:
        return {}
    return ack


def init_chrome_bundle(recording_dir: Path, state: Dict[str, Any]) -> None:
    chrome_dir = _chrome_dir(recording_dir)
    chrome_dir.mkdir(parents=True, exist_ok=True)
    session_path = chrome_dir / "session.json"
    if not session_path.exists():
        _write_json(
            session_path,
            {
                "schemaVersion": CHROME_CAPTURE_SCHEMA,
                "captureSessionId": uuid.uuid4().hex,
                "recording_dir": str(recording_dir),
                "started_at": state.get("started_at") or utc_now(),
                "profile": state.get("profile"),
                "workflow_id": state.get("workflow_id"),
                "title": state.get("title"),
                "state": "recording",
                "chromeSegmentActive": False,
            },
        )
    metadata_path = chrome_dir / "metadata.json"
    if not metadata_path.exists():
        _write_json(
            metadata_path,
            {
                "schemaVersion": CHROME_CAPTURE_SCHEMA,
                "state": "recording",
                "started_at": state.get("started_at") or utc_now(),
                "event_count": 0,
                "artifact_count": 0,
            },
        )


def chrome_capture_status(recording_dir: Path) -> Dict[str, Any]:
    chrome_dir = _chrome_dir(recording_dir)
    metadata = _read_optional_json(recording_dir / "metadata.json")
    capture_state = _read_capture_state(recording_dir)
    artifacts_dir = recording_dir / "artifacts"
    artifact_count = sum(1 for path in artifacts_dir.rglob("*") if path.is_file()) if artifacts_dir.exists() else 0
    return {
        "available": chrome_dir.exists(),
        "schemaVersion": CHROME_CAPTURE_SCHEMA,
        "directory": str(chrome_dir),
        "session": _read_optional_json(chrome_dir / "session.json"),
        "metadata": _read_optional_json(chrome_dir / "metadata.json"),
        "event_count": _chrome_event_count(recording_dir, metadata),
        "artifact_count": artifact_count,
        "capture_state": capture_state,
        "latest_segment": {
            "activeTargetId": capture_state.get("activeTargetId"),
            "activeTabId": capture_state.get("activeTabId"),
            "activeWindowId": capture_state.get("activeWindowId"),
            "activeUrl": capture_state.get("activeUrl"),
            "activeTitle": capture_state.get("activeTitle"),
            "chromeSegmentActive": capture_state.get("chromeSegmentActive"),
            "contentScriptReady": capture_state.get("contentScriptReady"),
        },
    }


def chrome_provider_preflight() -> Dict[str, Any]:
    manifest_paths = _native_host_manifest_paths()
    installed_manifests = []
    allowed_extension_ids: List[str] = []
    host_path = ""
    host_path_exists = False
    for path in manifest_paths:
        manifest = _read_optional_json(path)
        if not manifest:
            continue
        allowed = _extension_ids_from_origins(manifest.get("allowed_origins"))
        for extension_id in allowed:
            if extension_id not in allowed_extension_ids:
                allowed_extension_ids.append(extension_id)
        candidate_host = str(manifest.get("path") or "")
        candidate_exists = Path(candidate_host).expanduser().is_file() if candidate_host else False
        if candidate_host and not host_path:
            host_path = candidate_host
            host_path_exists = candidate_exists
        installed_manifests.append({
            "path": str(path),
            "hostPath": candidate_host,
            "hostPathExists": candidate_exists,
            "allowedExtensionIds": allowed,
        })

    expected_extension = _provider_extension_identity()
    expected_extension_id = str(expected_extension.get("extensionId") or "")
    expected_extension_version = str(expected_extension.get("extensionVersion") or "")
    candidate_extension_ids = list(allowed_extension_ids)
    if expected_extension_id and expected_extension_id not in candidate_extension_ids:
        candidate_extension_ids.append(expected_extension_id)
    detected_extensions = _installed_chrome_extensions(candidate_extension_ids)
    detected_extension_ids = list(dict.fromkeys(
        str(item.get("extensionId") or "")
        for item in detected_extensions
        if item.get("extensionId")
    ))
    expected_extension_loaded = any(
        item.get("extensionId") == expected_extension_id
        and item.get("extensionVersion") == expected_extension_version
        for item in detected_extensions
    )
    needs_attention = []
    if not installed_manifests:
        needs_attention.append("Hermes Chrome provider native host is not installed.")
    elif not host_path_exists:
        needs_attention.append("Hermes Chrome provider native host path is missing.")
    if not allowed_extension_ids:
        needs_attention.append("Hermes Chrome provider native host has no registered extension id.")
    elif expected_extension_id and expected_extension_id not in allowed_extension_ids:
        needs_attention.append(
            "Hermes Chrome provider native host is registered for an older extension identity. "
            "Reinstall the current Hermes Chrome Provider."
        )
    if not expected_extension_id or not expected_extension_version:
        needs_attention.append(
            "Hermes Chrome provider packaged identity is missing or invalid. "
            "Reinstall the current Hermes release."
        )
    if not detected_extension_ids:
        needs_attention.append("Hermes Chrome provider extension is not loaded in Chrome.")
    elif expected_extension_id and expected_extension_id not in detected_extension_ids:
        needs_attention.append(
            "The current Hermes Chrome Provider extension is not loaded in Chrome. "
            "Reload or reinstall the current provider."
        )
    elif not expected_extension_loaded:
        needs_attention.append(
            "Chrome has an older Hermes Chrome Provider build loaded. "
            "Reload or reinstall the packaged provider."
        )

    return {
        "schemaVersion": CHROME_CAPTURE_SCHEMA,
        "expectedExtensionId": expected_extension_id,
        "expectedExtensionVersion": expected_extension_version,
        "nativeHostInstalled": bool(installed_manifests),
        "nativeHostManifests": installed_manifests,
        "hostPath": host_path,
        "hostPathExists": host_path_exists,
        "allowedExtensionIds": allowed_extension_ids,
        "extensionDetected": bool(detected_extension_ids),
        "detectedExtensionIds": detected_extension_ids,
        "detectedExtensions": detected_extensions,
        "status": "ready" if not needs_attention else "needs_attention",
        "needs_attention": needs_attention,
    }


def finalize_chrome_bundle(recording_dir: Path) -> Dict[str, Any]:
    chrome_dir = _chrome_dir(recording_dir)
    if not chrome_dir.exists():
        return {"schemaVersion": CHROME_CAPTURE_SCHEMA, "available": False}
    metadata = _read_optional_json(chrome_dir / "metadata.json")
    metadata.update(
        {
            "schemaVersion": CHROME_CAPTURE_SCHEMA,
            "state": "stopped",
            "ended_at": utc_now(),
            "event_count": _chrome_event_count(recording_dir),
            "artifact_count": chrome_capture_status(recording_dir).get("artifact_count", 0),
        }
    )
    _write_json(chrome_dir / "metadata.json", metadata)
    session = _read_optional_json(chrome_dir / "session.json")
    session.update(
        {
            "state": "stopped",
            "ended_at": metadata["ended_at"],
            "chromeSegmentActive": False,
            "chromeSegmentID": None,
            "activeTargetId": None,
            "activeTabId": None,
            "activeWindowId": None,
            "activeUrl": None,
            "activeTitle": None,
            "activeTargetAttached": False,
            "contentScriptReady": False,
            "contentScriptUpdatedAt": None,
        }
    )
    _write_json(chrome_dir / "session.json", session)
    return {
        "schemaVersion": CHROME_CAPTURE_SCHEMA,
        "available": True,
        "counts": {
            "trace_events": metadata["event_count"],
            "artifacts": metadata["artifact_count"],
        },
        "files": {
            "trace": "trace.jsonl",
            "artifacts": "artifacts/",
        },
    }


def native_host_main() -> int:
    host = ChromeNativeHost()
    return host.run()


class ChromeNativeHost:
    def run(self) -> int:
        while True:
            message = _read_native_message(sys.stdin.buffer)
            if message is None:
                return 0
            response = self.handle_message(message)
            _write_native_message(sys.stdout.buffer, response)

    def handle_message(self, message: Dict[str, Any]) -> Dict[str, Any]:
        message_id = message.get("id")
        try:
            kind = str(message.get("type") or message.get("action") or "")
            if kind in {"hello", "status"}:
                payload = self._status()
            elif kind == "segment.start":
                payload = self._segment_start(message)
            elif kind == "segment.heartbeat":
                payload = self._segment_heartbeat(message)
            elif kind == "segment.stop":
                payload = self._segment_stop(message)
            elif kind == "trace.context":
                payload = self._append_trace_message(message, row_type="context")
            elif kind == "trace.actions":
                payload = self._append_trace_message(message, row_type="action")
            elif kind == "trace.evidence":
                payload = self._append_trace_message(message, row_type="evidence")
            elif kind == "recording.finalize":
                payload = self._finalize_recording(message)
            elif kind == "desktop.finalize.ack":
                payload = self._desktop_finalize_ack(message)
            elif kind == "screenshot":
                payload = self._save_screenshot(message)
            else:
                raise ValueError(f"Unknown message type: {kind or '<missing>'}")
            return {"id": message_id, "ok": True, **payload}
        except Exception as exc:
            return {"id": message_id, "ok": False, "error": str(exc)}

    def _active_recording(self) -> Dict[str, Any]:
        state = _resolve_active_state()
        if not state:
            raise RuntimeError("No active Record recording is available for the Chrome provider")
        recording_dir = Path(str(state.get("recording_dir") or "")).expanduser().resolve()
        if not _is_trace_recording_dir(recording_dir):
            raise RuntimeError("Active Record recording is missing or incomplete")
        init_chrome_bundle(recording_dir, state)
        return {**state, "recording_dir": str(recording_dir)}

    def _status(self) -> Dict[str, Any]:
        provider_identity = _provider_extension_identity()
        state = _resolve_active_state()
        if not state:
            return {
                "active": False,
                "schemaVersion": CHROME_CAPTURE_SCHEMA,
                "protocolVersion": CHROME_PROTOCOL_VERSION,
                **provider_identity,
            }
        recording_dir = Path(str(state.get("recording_dir") or "")).expanduser().resolve()
        capture_state = _read_capture_state(recording_dir)
        recording_id = str(capture_state.get("sessionID") or recording_dir.name)
        payload = {
            "active": True,
            "schemaVersion": CHROME_CAPTURE_SCHEMA,
            "protocolVersion": CHROME_PROTOCOL_VERSION,
            **provider_identity,
            "recording_dir": str(recording_dir),
            "recordingId": recording_id,
            "captureState": capture_state,
            "desiredProvider": capture_state.get("desiredProvider") or "native",
            "chromeSegmentActive": bool(capture_state.get("chromeSegmentActive")),
            "capture": chrome_capture_status(recording_dir),
        }
        if (
            state.get("capture_authority_source")
            == DESKTOP_CAPTURE_AUTHORITY_SOURCE
        ):
            payload["desktopCaptureAuthority"] = {
                "active": True,
                "source": DESKTOP_CAPTURE_AUTHORITY_SOURCE,
                "recordingId": recording_id,
                "captureAuthorization": state.get(
                    "desktop_capture_authorization"
                ),
                "finalizeRequest": state.get("desktop_finalize_request"),
                "finalizeAck": state.get("desktop_finalize_ack"),
            }
        return payload

    def _segment_start(self, message: Dict[str, Any]) -> Dict[str, Any]:
        if not _resolve_active_state():
            return {
                "active": False,
                "segmentActive": False,
                "desiredProvider": "native",
            }
        state = self._active_recording()
        _validate_capture_authorization(state, message)
        recording_dir = Path(str(state["recording_dir"]))
        capture_state = _read_capture_state(recording_dir)
        desired = str(capture_state.get("desiredProvider") or "native")
        if desired != "chrome":
            capture_state = _write_capture_state(
                recording_dir,
                {
                    "sessionActive": True,
                    "effectiveProvider": "native",
                    "chromeSegmentActive": False,
                },
                reason="chrome_segment_not_requested",
            )
            return {
                "recording_dir": str(recording_dir),
                "segmentActive": False,
                "desiredProvider": desired,
                "captureState": capture_state,
            }
        if message.get("protocolVersion") != CHROME_PROTOCOL_VERSION:
            capture_state = _write_capture_state(
                recording_dir,
                {
                    "sessionActive": True,
                    "desiredProvider": "chrome",
                    "effectiveProvider": "native",
                    "chromeSegmentActive": False,
                    "chromeSegmentID": None,
                    "contentScriptReady": False,
                    "protocolVersion": message.get("protocolVersion"),
                },
                reason="chrome_protocol_mismatch",
            )
            return {
                "recording_dir": str(recording_dir),
                "segmentActive": False,
                "protocolVersion": CHROME_PROTOCOL_VERSION,
                "captureState": capture_state,
            }
        if message.get("contentScriptReady") is not True:
            capture_state = _write_capture_state(
                recording_dir,
                {
                    "sessionActive": True,
                    "desiredProvider": "chrome",
                    "effectiveProvider": "native",
                    "chromeSegmentActive": False,
                    "chromeSegmentID": None,
                    **_active_target_state_from_message(message, segment_id=None),
                },
                reason="chrome_content_script_unavailable",
            )
            return {
                "recording_dir": str(recording_dir),
                "segmentActive": False,
                "desiredProvider": desired,
                "captureState": capture_state,
            }

        was_active = (
            bool(capture_state.get("chromeSegmentActive"))
            and str(capture_state.get("effectiveProvider") or "") == "chrome"
            and bool(capture_state.get("chromeSegmentID"))
        )
        segment_id = str(capture_state.get("chromeSegmentID") or "") if was_active else ""
        if not segment_id:
            segment_id = uuid.uuid4().hex
        now = utc_now()
        capture_state = _write_capture_state(
            recording_dir,
            {
                "sessionActive": True,
                "desiredProvider": "chrome",
                "effectiveProvider": "chrome",
                "chromeSegmentActive": True,
                "chromeSegmentID": segment_id,
                "lastHeartbeatAt": now,
                "extensionID": message.get("extensionId"),
                "protocolVersion": CHROME_PROTOCOL_VERSION,
                **_active_target_state_from_message(message, segment_id=segment_id),
            },
            reason="chrome_segment_refreshed" if was_active else "chrome_segment_started",
        )
        if not was_active:
            _append_trace_rows(recording_dir, [_trace_context_row("chrome.segment.started", message, capture_state)])
        chrome_dir = _chrome_dir(recording_dir)
        session = _read_optional_json(chrome_dir / "session.json")
        session.update(
            {
                "state": "recording",
                "chromeSegmentActive": True,
                "chromeSegmentID": segment_id,
                "activeTargetId": capture_state.get("activeTargetId"),
                "activeTabId": capture_state.get("activeTabId"),
                "activeWindowId": capture_state.get("activeWindowId"),
                "activeUrl": capture_state.get("activeUrl"),
                "activeTitle": capture_state.get("activeTitle"),
                "activeTargetAttached": capture_state.get("activeTargetAttached"),
                "contentScriptReady": capture_state.get("contentScriptReady"),
                "contentScriptUpdatedAt": capture_state.get("contentScriptUpdatedAt"),
                "extension_id": message.get("extensionId"),
                "chrome_user_agent": message.get("userAgent"),
                "updated_at": now,
            }
        )
        _write_json(chrome_dir / "session.json", session)
        _refresh_counts(recording_dir)
        _patch_recording_metadata(recording_dir, {"captureState": capture_state, "chrome_provider": chrome_capture_status(recording_dir)})
        return {
            "recording_dir": str(recording_dir),
            "recordingId": capture_state.get("sessionID") or recording_dir.name,
            "protocolVersion": CHROME_PROTOCOL_VERSION,
            "captureSessionId": session.get("captureSessionId"),
            "segmentId": segment_id,
            "segmentActive": True,
            "captureState": capture_state,
        }

    def _segment_heartbeat(self, message: Dict[str, Any]) -> Dict[str, Any]:
        state = self._active_recording()
        recording_dir = Path(str(state["recording_dir"]))
        capture_state = _read_capture_state(recording_dir)
        self._validate_segment_identity(message, recording_dir, capture_state)
        if str(capture_state.get("desiredProvider") or "native") != "chrome":
            return self._segment_stop({**message, "reason": "native_requested_provider_change"})
        if message.get("contentScriptReady") is not True:
            return self._segment_stop({**message, "reason": "chrome_content_script_unavailable"})
        existing_segment_id = str(capture_state.get("chromeSegmentID") or "")
        segment_id = existing_segment_id
        capture_state = _write_capture_state(
            recording_dir,
            {
                "sessionActive": True,
                "desiredProvider": "chrome",
                "effectiveProvider": "chrome",
                "chromeSegmentActive": True,
                "chromeSegmentID": segment_id,
                "lastHeartbeatAt": utc_now(),
                "extensionID": message.get("extensionId"),
                **_active_target_state_from_message(message, segment_id=segment_id),
            },
            reason="chrome_segment_heartbeat",
        )
        _append_trace_rows(recording_dir, [_trace_context_row("chrome.segment.heartbeat", message, capture_state)])
        return {
            "recording_dir": str(recording_dir),
            "recordingId": capture_state.get("sessionID") or recording_dir.name,
            "protocolVersion": CHROME_PROTOCOL_VERSION,
            "segmentId": segment_id,
            "segmentActive": True,
            "captureState": capture_state,
        }

    def _segment_stop(self, message: Dict[str, Any]) -> Dict[str, Any]:
        state = self._active_recording()
        recording_dir = Path(str(state["recording_dir"]))
        capture_state = _read_capture_state(recording_dir)
        was_active = bool(capture_state.get("chromeSegmentActive")) or str(capture_state.get("effectiveProvider") or "") == "chrome"
        segment_id = str(capture_state.get("chromeSegmentID") or "")
        prior_capture_state = dict(capture_state)
        capture_state = _write_capture_state(
            recording_dir,
            {
                "sessionActive": True,
                "effectiveProvider": "native",
                "chromeSegmentActive": False,
                "chromeSegmentID": None,
                "lastHeartbeatAt": None,
                "extensionID": message.get("extensionId"),
                "activeTargetId": None,
                "activeTabId": None,
                "activeWindowId": None,
                "activeUrl": None,
                "activeTitle": None,
                "activeTargetAttached": False,
                "activeTargetUpdatedAt": None,
                "contentScriptReady": False,
                "contentScriptUpdatedAt": None,
            },
            reason=str(message.get("reason") or "chrome_segment_stopped"),
        )
        if was_active:
            _append_trace_rows(recording_dir, [_trace_context_row("chrome.segment.stopped", message, prior_capture_state)])
        chrome_dir = _chrome_dir(recording_dir)
        session = _read_optional_json(chrome_dir / "session.json")
        session.update(
            {
                "state": "recording",
                "chromeSegmentActive": False,
                "chromeSegmentID": None,
                "activeTargetId": None,
                "activeTabId": None,
                "activeWindowId": None,
                "activeUrl": None,
                "activeTitle": None,
                "activeTargetAttached": False,
                "contentScriptReady": False,
                "contentScriptUpdatedAt": None,
                "updated_at": utc_now(),
            }
        )
        _write_json(chrome_dir / "session.json", session)
        _refresh_counts(recording_dir)
        _patch_recording_metadata(recording_dir, {"captureState": capture_state, "chrome_provider": chrome_capture_status(recording_dir)})
        return {
            "recording_dir": str(recording_dir),
            "segmentId": segment_id,
            "segmentActive": False,
            "captureState": capture_state,
        }

    def _append_trace_message(self, message: Dict[str, Any], *, row_type: str) -> Dict[str, Any]:
        recording_dir = Path(str(self._active_recording()["recording_dir"]))
        capture_state = _read_capture_state(recording_dir)
        self._validate_segment_identity(message, recording_dir, capture_state)
        if not (
            capture_state.get("sessionActive") is True
            and capture_state.get("desiredProvider") == "chrome"
            and capture_state.get("effectiveProvider") == "chrome"
            and capture_state.get("chromeSegmentActive") is True
            and capture_state.get("contentScriptReady") is True
        ):
            raise RuntimeError("Chrome recording segment is not live")
        rows = _message_items(message, "rows") or _message_items(message, "events")
        if not rows and isinstance(message.get("row"), dict):
            rows = [message["row"]]
        normalized = [_coerce_trace_row(item, row_type=row_type, message=message) for item in rows if isinstance(item, dict)]
        accepted, accepted_event_ids = _append_trace_rows_with_ack(recording_dir, normalized)
        if accepted or accepted_event_ids:
            _refresh_counts(recording_dir)
        return {
            "accepted": len(accepted_event_ids) if accepted_event_ids else accepted,
            "acceptedEventIds": accepted_event_ids,
        }

    def _finalize_recording(self, message: Dict[str, Any]) -> Dict[str, Any]:
        recording_dir = Path(str(self._active_recording()["recording_dir"]))
        capture_state = _read_capture_state(recording_dir)
        # Provider segments are short-lived app-surface leases. The recording
        # continuity ledger outlives them so Chrome -> desktop -> review can be
        # finalized after the Chrome lease has legitimately closed.
        self._validate_recording_identity(message, recording_dir, capture_state)
        rows = _read_jsonl(recording_dir / "trace.jsonl")
        chrome_actions = [
            row for row in rows
            if row.get("source") == "chrome" and row.get("type") == "action"
        ]
        sequences = sorted({
            int(sequence)
            for row in chrome_actions
            for sequence in [((row.get("payload") or {}).get("meta") or {}).get("provider_sequence")]
            if isinstance(sequence, int) and sequence > 0
        })
        origins = sorted(
            {
                origin
                for row in chrome_actions
                for candidate in [
                    ((row.get("payload") or {}).get("browser") or {}).get("url"),
                    ((row.get("payload") or {}).get("browser") or {}).get("top_url"),
                    ((row.get("payload") or {}).get("browser") or {}).get("origin"),
                ]
                for origin in [url_origin(candidate) or str(candidate or "")]
                if origin.startswith(("http://", "https://"))
            }
        )
        artifacts = [
            row for row in rows
            if row.get("source") == "chrome"
            and row.get("type") == "evidence"
            and ((row.get("payload") or {}).get("effect") or {}).get("artifact_path")
        ]
        expected_sequences = list(range(1, max(sequences, default=0) + 1))
        reasons: List[str] = []
        if not chrome_actions:
            reasons.append("missing_chrome_actions")
        if not origins:
            reasons.append("missing_chrome_origin")
        if not artifacts:
            reasons.append("missing_chrome_artifact")
        if not sequences or sequences != expected_sequences:
            reasons.append("provider_sequence_gap")
        continuity = {
            "version": 1,
            "ok": not reasons,
            "reasons": reasons,
            "recording_id": message.get("recordingId"),
            "segment_id": message.get("segmentId"),
            "action_count": len(chrome_actions),
            "origins": origins,
            "artifact_count": len(artifacts),
            "provider_sequences": sequences,
            "finalized_at": utc_now(),
        }
        row = {
            "type": "context" if continuity["ok"] else "evidence",
            "source": "chrome",
            "kind": "chrome.provider.finalized",
            "payload": {"provider": {"continuity": continuity}},
            "quality": {
                "confidence": "high" if continuity["ok"] else "low",
                "warnings": [] if continuity["ok"] else ["chrome_provider_continuity_failed", *reasons],
            },
        }
        _append_trace_rows(recording_dir, [row])
        _refresh_counts(recording_dir)
        return {"continuity": continuity}

    def _desktop_finalize_ack(self, message: Dict[str, Any]) -> Dict[str, Any]:
        state = self._active_recording()
        if (
            state.get("capture_authority_source")
            != DESKTOP_CAPTURE_AUTHORITY_SOURCE
        ):
            raise RuntimeError("Desktop finalization is not active")
        recording_dir = Path(str(state["recording_dir"]))
        capture_state = _read_capture_state(recording_dir)
        self._validate_recording_identity(message, recording_dir, capture_state)
        request = state.get("desktop_finalize_request")
        request_id = str(message.get("requestId") or "")
        if (
            not isinstance(request, dict)
            or not request_id
            or request_id != str(request.get("id") or "")
        ):
            raise RuntimeError("Desktop finalization request does not match")
        result = message.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Desktop finalization result is invalid")
        ack = {
            "id": request_id,
            "recordingId": message.get("recordingId"),
            "received_at": utc_now(),
            "result": result,
        }
        active_path = _active_state_path(None)
        if active_path is None:
            raise RuntimeError("Desktop finalization state is unavailable")
        current = _read_optional_json(active_path)
        if (
            str(current.get("recording_dir") or "")
            != str(recording_dir)
        ):
            raise RuntimeError("Desktop finalization recording changed")
        current["desktop_finalize_ack"] = ack
        current["updated_at"] = utc_now()
        _write_json(active_path, current)
        try:
            os.chmod(active_path, 0o600)
        except OSError:
            pass
        return {"finalizeAck": ack}

    def _save_screenshot(self, message: Dict[str, Any]) -> Dict[str, Any]:
        recording_dir = Path(str(self._active_recording()["recording_dir"]))
        capture_state = _read_capture_state(recording_dir)
        self._validate_segment_identity(message, recording_dir, capture_state)
        if capture_state.get("sessionActive") is not True or capture_state.get("chromeSegmentActive") is not True:
            raise RuntimeError("Chrome recording segment is not live")
        data_url = str(message.get("dataUrl") or "")
        if not data_url.startswith("data:image/png;base64,"):
            raise ValueError("screenshot must be a PNG data URL")
        screenshots_dir = recording_dir / "artifacts" / "screenshots"
        existing_count = sum(1 for path in screenshots_dir.glob("*.png") if path.is_file()) if screenshots_dir.exists() else 0
        if existing_count >= MAX_SCREENSHOT_COUNT:
            return {"skipped": True, "reason": "screenshot_limit_reached"}
        try:
            image_bytes = base64.b64decode(data_url.split(",", 1)[1], validate=True)
        except Exception as exc:
            raise ValueError("screenshot payload is not valid base64") from exc
        if len(image_bytes) > MAX_SCREENSHOT_BYTES:
            return {"skipped": True, "reason": "screenshot_too_large", "bytes": len(image_bytes)}
        tab_id = _safe_id(message.get("tabId") or "unknown")
        label = _safe_id(message.get("label") or "screenshot")
        stamp = str(int(time.time() * 1000))
        rel = Path("artifacts") / "screenshots" / f"{stamp}-tab-{tab_id}-{label}.png"
        path = recording_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(image_bytes)
        screenshot_meta: Dict[str, Any] = {"reason": message.get("label") or "screenshot"}
        after_action_id = str(message.get("afterActionId") or "").strip()
        if after_action_id:
            screenshot_meta["after_action_id"] = after_action_id
        row = {
            "type": "evidence",
            "source": "chrome",
            "kind": "screenshot",
            "payload": {
                "browser": _chrome_browser_payload(message, capture_state),
                "meta": screenshot_meta,
                "effect": {"artifact_path": str(rel)},
            },
            "quality": {"confidence": "medium", "warnings": []},
        }
        try:
            accepted = _append_trace_rows(recording_dir, [row])
            if not accepted:
                raise RuntimeError("Record recording already stopped")
        except Exception:
            path.unlink(missing_ok=True)
            raise
        _refresh_counts(recording_dir)
        return {"path": str(path), "artifact_path": str(rel)}

    def _validate_segment_identity(
        self,
        message: Dict[str, Any],
        recording_dir: Path,
        capture_state: Dict[str, Any],
    ) -> None:
        self._validate_recording_identity(message, recording_dir, capture_state)
        expected_segment = str(capture_state.get("chromeSegmentID") or "")
        if not expected_segment or str(message.get("segmentId") or "") != expected_segment:
            raise RuntimeError("Chrome message belongs to a different segment")

    def _validate_recording_identity(
        self,
        message: Dict[str, Any],
        recording_dir: Path,
        capture_state: Dict[str, Any],
    ) -> None:
        expected_recording = str(capture_state.get("sessionID") or recording_dir.name)
        if str(message.get("recordingId") or "") != expected_recording:
            raise RuntimeError("Chrome message belongs to a different recording")
        if message.get("protocolVersion") != CHROME_PROTOCOL_VERSION:
            raise RuntimeError("Chrome provider protocol version does not match")


def _active_target_state_from_message(message: Dict[str, Any], *, segment_id: Optional[str] = None) -> Dict[str, Any]:
    active_tab_id = message.get("activeTabId") if message.get("activeTabId") is not None else message.get("tabId")
    active_window_id = message.get("activeWindowId") if message.get("activeWindowId") is not None else message.get("windowId")
    active_url = message.get("activeUrl") or message.get("url")
    active_title = message.get("activeTitle") or message.get("title")
    active_target_id = message.get("activeTargetId")
    content_script_ready = bool(message.get("contentScriptReady"))
    active_target_attached = bool(message.get("activeTargetAttached")) or content_script_ready
    if not active_target_id and segment_id and active_tab_id is not None and active_window_id is not None:
        active_target_id = f"{segment_id}:{active_window_id}:{active_tab_id}"
    updates = {
        "activeTargetId": active_target_id,
        "activeTabId": active_tab_id,
        "activeWindowId": active_window_id,
        "activeUrl": active_url,
        "activeTitle": active_title,
        "activeTargetAttached": active_target_attached,
        "contentScriptReady": content_script_ready,
    }
    if any(value is not None for value in updates.values()):
        updated_at = message.get("activeTargetUpdatedAt") or utc_now()
        updates["activeTargetUpdatedAt"] = updated_at
        updates["contentScriptUpdatedAt"] = message.get("contentScriptUpdatedAt") or updated_at
        return updates
    return {}




def _capture_state_path(recording_dir: Path) -> Path:
    return recording_dir.expanduser().resolve() / CAPTURE_STATE_FILENAME


def _read_capture_state(recording_dir: Path) -> Dict[str, Any]:
    path = _capture_state_path(recording_dir)
    state = _read_optional_json(path)
    if not state:
        metadata = _read_optional_json(recording_dir / "metadata.json")
        session_active = bool(metadata.get("active")) or str(metadata.get("state") or "") == "recording"
        state = {
            "schemaVersion": ADAPTIVE_CAPTURE_SCHEMA,
            "sessionID": metadata.get("sessionID") or recording_dir.name,
            "sessionActive": session_active,
            "desiredProvider": "native",
            "effectiveProvider": "native",
            "chromeSegmentActive": False,
            "chromeSegmentID": None,
            "lastHeartbeatAt": None,
            "activeTargetId": None,
            "activeTabId": None,
            "activeWindowId": None,
            "activeUrl": None,
            "activeTitle": None,
            "activeTargetAttached": False,
            "activeTargetUpdatedAt": None,
            "contentScriptReady": False,
            "contentScriptUpdatedAt": None,
            "updatedAt": utc_now(),
            "reason": "capture_state_defaulted",
        }
    state.setdefault("schemaVersion", ADAPTIVE_CAPTURE_SCHEMA)
    state.setdefault("desiredProvider", "native")
    state.setdefault("effectiveProvider", "native")
    state.setdefault("chromeSegmentActive", False)
    state.setdefault("sessionActive", True)
    state.setdefault("activeTargetId", None)
    state.setdefault("activeTabId", None)
    state.setdefault("activeWindowId", None)
    state.setdefault("activeUrl", None)
    state.setdefault("activeTitle", None)
    state.setdefault("activeTargetAttached", False)
    state.setdefault("activeTargetUpdatedAt", None)
    state.setdefault("contentScriptReady", False)
    state.setdefault("contentScriptUpdatedAt", None)
    return state


def _write_capture_state(recording_dir: Path, updates: Dict[str, Any], *, reason: str) -> Dict[str, Any]:
    with _recording_lock(recording_dir):
        state = _read_capture_state(recording_dir)
        state.update(updates)
        state["schemaVersion"] = ADAPTIVE_CAPTURE_SCHEMA
        state["updatedAt"] = utc_now()
        state["reason"] = reason
        _write_json(_capture_state_path(recording_dir), state)
        return state


def _refresh_counts(recording_dir: Path) -> None:
    chrome_dir = _chrome_dir(recording_dir)
    chrome_dir.mkdir(parents=True, exist_ok=True)
    recording_metadata = _read_optional_json(recording_dir / "metadata.json")
    artifacts_dir = recording_dir / "artifacts"
    artifact_count = sum(1 for path in artifacts_dir.rglob("*") if path.is_file()) if artifacts_dir.exists() else 0
    metadata = _read_optional_json(chrome_dir / "metadata.json")
    metadata.update(
        {
            "schemaVersion": CHROME_CAPTURE_SCHEMA,
            "state": metadata.get("state") or "recording",
            "updated_at": utc_now(),
            "event_count": _chrome_event_count(recording_dir, recording_metadata),
            "artifact_count": artifact_count,
        }
    )
    _write_json(chrome_dir / "metadata.json", metadata)


def _resolve_active_state() -> Dict[str, Any]:
    path = _active_state_path(None)
    if not path or not path.is_file():
        return {}
    state = _read_optional_json(path)
    if state.get("active") is not True:
        return {}
    recording_dir = Path(str(state.get("recording_dir") or "")).expanduser().resolve()
    metadata = _read_optional_json(recording_dir / "metadata.json")
    if (
        not _is_trace_recording_dir(recording_dir)
        or metadata.get("active") is not True
        or str(metadata.get("state") or "") != "recording"
    ):
        return {}
    return state


def _validate_capture_authorization(
    state: Dict[str, Any],
    message: Dict[str, Any],
) -> None:
    expected_digest = str(state.get("capture_authorization_sha256") or "")
    authorization = str(message.get("captureAuthorization") or "")
    supplied_source = str(
        message.get("captureAuthoritySource") or ""
    ).strip().lower()
    supplied_digest = hashlib.sha256(authorization.encode("utf-8")).hexdigest()
    invalid = (
        len(expected_digest) != 64
        or not authorization
        or supplied_source != DESKTOP_CAPTURE_AUTHORITY_SOURCE
        or not hmac.compare_digest(supplied_digest, expected_digest)
    )
    if invalid:
        raise RuntimeError(
            "Chrome capture authorization does not match the active Hermes recording"
        )


def _trace_context_row(kind: str, message: Dict[str, Any], capture_state: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "context",
        "source": "chrome",
        "kind": kind,
        "payload": {
            "browser": _chrome_browser_payload(message, capture_state),
            "provider": {
                "desired": capture_state.get("desiredProvider") or "native",
                "effective": capture_state.get("effectiveProvider") or "native",
                "attached": bool(capture_state.get("activeTargetAttached")),
                "segment_id": capture_state.get("chromeSegmentID") or "",
                "heartbeat_at": capture_state.get("lastHeartbeatAt") or capture_state.get("updatedAt") or utc_now(),
                "content_script_ready": bool(capture_state.get("contentScriptReady")),
            },
            "environment": {
                "user_agent": message.get("userAgent") or "",
                "platform": message.get("platform") or "",
                "language": message.get("language") or "",
                "languages": message.get("languages") or [],
                "timezone": message.get("timezone") or "",
                "online": message.get("online") if message.get("online") is not None else True,
            },
            "meta": {"reason": message.get("reason") or ""},
        },
        "quality": {"confidence": "high", "warnings": []},
    }


def _coerce_trace_row(item: Dict[str, Any], *, row_type: str, message: Dict[str, Any]) -> Dict[str, Any]:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    if not payload:
        payload = {key: item[key] for key in ("session", "app", "window", "browser", "provider", "environment", "meta", "target", "input", "effect") if isinstance(item.get(key), dict)}
    if "browser" not in payload:
        payload = {**payload, "browser": _chrome_browser_payload({**message, **item}, {})}
    return {
        "id": item.get("id") or 0,
        "ts": item.get("ts") or item.get("timestamp") or utc_now(),
        "type": item.get("type") or row_type,
        "source": item.get("source") or "chrome",
        "kind": item.get("kind") or item.get("action") or "capture.error",
        "payload": payload,
        "quality": item.get("quality") if isinstance(item.get("quality"), dict) else {"confidence": "medium", "warnings": []},
    }


def _chrome_browser_payload(message: Dict[str, Any], capture_state: Dict[str, Any]) -> Dict[str, Any]:
    url = str(message.get("url") or message.get("topUrl") or capture_state.get("activeUrl") or "")
    return {
        "url": url,
        "origin": url_origin(url),
        "title": message.get("title") or capture_state.get("activeTitle") or "",
        "tab_id": message.get("tabId") or message.get("activeTabId") or capture_state.get("activeTabId") or 0,
        "window_id": message.get("windowId") or message.get("activeWindowId") or capture_state.get("activeWindowId") or 0,
        "frame_id": message.get("frameId") if message.get("frameId") is not None else 0,
        "parent_frame_id": message.get("parentFrameId") if message.get("parentFrameId") is not None else 0,
        "document_id": message.get("documentId") or "",
        "frame_url": message.get("frameUrl") or url,
        "top_url": message.get("topUrl") or url,
        "active_target_id": message.get("activeTargetId") or capture_state.get("activeTargetId") or "",
        "profile_hint": message.get("profileHint") or "current-profile",
        "viewport": message.get("viewport") if isinstance(message.get("viewport"), dict) else {},
    }


def _append_trace_rows(recording_dir: Path, rows: List[Dict[str, Any]]) -> int:
    accepted, _accepted_event_ids = _append_trace_rows_with_ack(recording_dir, rows)
    return accepted


def _append_trace_rows_with_ack(
    recording_dir: Path,
    rows: List[Dict[str, Any]],
) -> Tuple[int, List[str]]:
    """Append trace rows and return only provider IDs proven present on disk.

    A recording can stop after the native host validates its segment but before
    it acquires the trace lock. In that race, new rows are rejected and must not
    be acknowledged to the extension's durable outbox. Provider IDs already in
    the trace remain safe to acknowledge because retrying them is idempotent.
    """

    rows = [row for row in rows if isinstance(row, dict)]
    if not rows:
        return 0, []
    recording_dir = recording_dir.expanduser().resolve()
    metadata_path = recording_dir / "metadata.json"
    trace_path = recording_dir / "trace.jsonl"
    lock_path = recording_dir / "trace.jsonl.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_fh:
        fcntl.lockf(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            metadata = _read_optional_json(metadata_path)
            existing_rows = _read_jsonl(trace_path)
            requested_provider_ids = [
                str(((row.get("payload") or {}).get("meta") or {}).get("provider_event_id") or "")
                for row in rows
            ]
            seen_provider_ids = {
                str(((row.get("payload") or {}).get("meta") or {}).get("provider_event_id") or "")
                for row in existing_rows
            }
            seen_provider_ids.discard("")
            if metadata.get("active") is not True or str(metadata.get("state") or "") != "recording":
                acknowledged = list(dict.fromkeys(
                    provider_id
                    for provider_id in requested_provider_ids
                    if provider_id and provider_id in seen_provider_ids
                ))
                return 0, acknowledged
            next_id = max(
                [int(row.get("id") or 0) for row in existing_rows if isinstance(row.get("id"), int)]
                or [0]
            )
            normalized_rows: List[Dict[str, Any]] = []
            for row in rows:
                provider_id = str(((row.get("payload") or {}).get("meta") or {}).get("provider_event_id") or "")
                if provider_id and provider_id in seen_provider_ids:
                    continue
                next_id += 1
                row = {**row, "id": next_id, "ts": row.get("ts") or utc_now()}
                normalized_rows.append(normalize_trace_row(row))
                if provider_id:
                    seen_provider_ids.add(provider_id)
            _append_jsonl(trace_path, normalized_rows)
            metadata["eventCount"] = next_id
            all_rows = [*existing_rows, *normalized_rows]
            metadata["chromeEventCount"] = sum(1 for row in all_rows if row.get("source") == "chrome")
            metadata["paths"] = {**(metadata.get("paths") if isinstance(metadata.get("paths"), dict) else {}), "trace": "trace.jsonl"}
            provider_status = chrome_capture_status(recording_dir)
            provider_status["event_count"] = metadata["chromeEventCount"]
            metadata["chrome_provider"] = provider_status
            _write_json(metadata_path, metadata)
            acknowledged = list(dict.fromkeys(
                provider_id
                for provider_id in requested_provider_ids
                if provider_id and provider_id in seen_provider_ids
            ))
            return len(normalized_rows), acknowledged
        finally:
            fcntl.lockf(lock_fh.fileno(), fcntl.LOCK_UN)


def _message_items(message: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
    value = message.get(key)
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _public_capture_state(state: Dict[str, Any], recording_dir: Path) -> Dict[str, Any]:
    return {
        "available": True,
        "schemaVersion": CHROME_CAPTURE_SCHEMA,
        "active": bool(state.get("active")),
        "profile": state.get("profile"),
        "recording_dir": str(recording_dir),
        "directory": str(_chrome_dir(recording_dir)),
        "started_at": state.get("started_at"),
        "updated_at": state.get("updated_at"),
    }


def _patch_recording_metadata(recording_dir: Path, values: Dict[str, Any]) -> None:
    with _recording_lock(recording_dir):
        for name in ("metadata.json", "session.json"):
            path = recording_dir / name
            if not path.is_file():
                continue
            data = _read_optional_json(path)
            data.update(values)
            _write_json(path, data)


@contextmanager
def _recording_lock(recording_dir: Path):
    lock_path = recording_dir.expanduser().resolve() / "trace.jsonl.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_fh:
        fcntl.lockf(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.lockf(lock_fh.fileno(), fcntl.LOCK_UN)


def _active_state_path(home: Optional[Path]) -> Optional[Path]:
    root = _default_root_for_home(home)
    if root is None:
        return None
    return root.expanduser().resolve() / "record" / ACTIVE_STATE_FILENAME


def _default_root_for_home(home: Optional[Path]) -> Optional[Path]:
    if home is None:
        return _safe_get_default_root()
    try:
        resolved = home.expanduser().resolve()
        if resolved.parent.name == "profiles":
            return resolved.parent.parent
        return resolved
    except Exception:
        return _safe_get_default_root()


def _safe_get_default_root() -> Optional[Path]:
    if get_default_hermes_root is not None:
        try:
            return Path(get_default_hermes_root()).expanduser().resolve()
        except Exception:
            pass
    env_home = os.environ.get("HERMES_HOME", "").strip()
    if env_home:
        home = Path(env_home).expanduser().resolve()
        return home.parent.parent if home.parent.name == "profiles" else home
    return Path.home() / ".hermes"


def _chrome_dir(recording_dir: Path) -> Path:
    return recording_dir.expanduser().resolve() / "chrome"


def _is_trace_recording_dir(path: Path) -> bool:
    return (
        path.expanduser().is_dir()
        and (path / "metadata.json").is_file()
        and (path / "session.json").is_file()
        and (path / "trace.jsonl").is_file()
    )


def _native_host_manifest_paths() -> List[Path]:
    if sys.platform == "darwin":
        roots = [
            Path.home() / "Library" / "Application Support" / "Google" / "Chrome" / "NativeMessagingHosts",
        ]
        if os.environ.get("HERMES_RECORD_TART_MANUAL") == "1":
            roots.insert(0, Path.home() / ".record-clean-chrome" / "NativeMessagingHosts")
    elif sys.platform == "win32":
        roots = []
    else:
        roots = [
            Path.home() / ".config" / "google-chrome" / "NativeMessagingHosts",
            Path.home() / ".config" / "chromium" / "NativeMessagingHosts",
        ]
    return [root / f"{HOST_NAME}.json" for root in roots]


def _extension_ids_from_origins(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    result: List[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text.startswith("chrome-extension://"):
            continue
        extension_id = text.removeprefix("chrome-extension://").strip("/")
        if extension_id and extension_id not in result:
            result.append(extension_id)
    return result


def _provider_extension_identity() -> Dict[str, str]:
    return _extension_identity_from_manifest(
        PLUGIN_ROOT / "chrome_capture" / "extension" / "manifest.json"
    )


def _extension_identity_from_manifest(manifest_path: Path) -> Dict[str, str]:
    manifest = _read_optional_json(manifest_path)
    key = str(manifest.get("key") or "").strip()
    version = str(manifest.get("version") or "").strip()
    if not key:
        return {"extensionId": "", "extensionVersion": version}
    try:
        digest = hashlib.sha256(base64.b64decode(key, validate=True)).digest()[:16]
        extension_id = "".join(
            chr(ord("a") + int(nibble, 16))
            for nibble in digest.hex()
        )
    except Exception:
        extension_id = ""
    return {
        "extensionId": extension_id,
        "extensionVersion": version,
    }


def _installed_chrome_extensions(extension_ids: List[str]) -> List[Dict[str, str]]:
    wanted = {item for item in extension_ids if item}
    if not wanted:
        return []
    detected: Dict[Tuple[str, str], Dict[str, str]] = {}
    for preferences in _chrome_preferences_paths():
        data = _read_optional_json(preferences)
        settings = data.get("extensions", {}).get("settings", {}) if isinstance(data.get("extensions"), dict) else {}
        if not isinstance(settings, dict):
            continue
        for extension_id, info in settings.items():
            if extension_id not in wanted:
                continue
            if isinstance(info, dict) and info.get("state") == 0:
                continue
            if not isinstance(info, dict):
                continue
            raw_path = str(info.get("path") or "").strip()
            extension_root = Path(raw_path).expanduser() if raw_path else None
            if extension_root is not None and not extension_root.is_absolute():
                extension_root = preferences.parent / "Extensions" / extension_root
            manifest = (
                _read_optional_json(extension_root / "manifest.json")
                if extension_root is not None
                else {}
            )
            if not manifest and isinstance(info.get("manifest"), dict):
                manifest = info["manifest"]
            profile = preferences.parent.name
            key = (profile, extension_id)
            record = {
                "extensionId": extension_id,
                "extensionVersion": str(manifest.get("version") or ""),
                "profile": profile,
                "source": "preferences",
            }
            existing = detected.get(key)
            if existing is None or (
                not existing.get("extensionVersion")
                and record["extensionVersion"]
            ):
                detected[key] = record
    for extension_id in _live_devtools_extension_ids(wanted):
        if not any(item["extensionId"] == extension_id for item in detected.values()):
            detected[("devtools", extension_id)] = {
                "extensionId": extension_id,
                "extensionVersion": "",
                "profile": "devtools",
                "source": "devtools",
            }
    return list(detected.values())


def _live_devtools_extension_ids(wanted: set[str]) -> List[str]:
    if os.environ.get("HERMES_RECORD_TART_MANUAL") != "1":
        return []
    try:
        with urllib.request.urlopen("http://127.0.0.1:9222/json/list", timeout=0.5) as response:
            targets = json.load(response)
    except Exception:
        return []
    detected: List[str] = []
    for target in targets if isinstance(targets, list) else []:
        if not isinstance(target, dict) or target.get("type") != "service_worker":
            continue
        url = str(target.get("url") or "")
        if not url.startswith("chrome-extension://"):
            continue
        extension_id = url.removeprefix("chrome-extension://").split("/", 1)[0]
        if extension_id in wanted and extension_id not in detected:
            detected.append(extension_id)
    return detected


def _chrome_preferences_paths() -> List[Path]:
    if sys.platform != "darwin":
        return []
    roots = [
        Path.home() / "Library" / "Application Support" / "Google" / "Chrome",
    ]
    paths: List[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        local_state = _read_optional_json(root / "Local State")
        profile_state = (
            local_state.get("profile")
            if isinstance(local_state.get("profile"), dict)
            else {}
        )
        active_names = [
            str(item)
            for item in profile_state.get("last_active_profiles", [])
            if isinstance(item, str)
        ]
        last_used = str(profile_state.get("last_used") or "")
        if last_used and last_used not in active_names:
            active_names.append(last_used)
        profile_roots = []
        for name in active_names:
            candidate = root / name
            if (
                name
                and name not in {".", ".."}
                and Path(name).name == name
                and candidate.is_dir()
            ):
                profile_roots.append(candidate)
        if not profile_roots:
            profile_roots = sorted(
                path.parent
                for path in root.glob("*/Preferences")
            )
        for profile_root in profile_roots:
            paths.extend([
                profile_root / "Preferences",
                profile_root / "Secure Preferences",
            ])
    return [path for path in paths if path.is_file()]


def _safe_id(value: Any) -> str:
    text = str(value or "").strip().lower()
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in text)
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    return cleaned[:80] or "item"


def _write_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_json(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _read_optional_json(path: Path) -> Dict[str, Any]:
    try:
        return _read_json(path)
    except Exception:
        return {}


def _append_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    rows = list(rows)
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.is_file():
        return rows
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
    return rows


def _chrome_event_count(recording_dir: Path, metadata: Optional[Dict[str, Any]] = None) -> int:
    metadata = metadata if isinstance(metadata, dict) else _read_optional_json(recording_dir / "metadata.json")
    stored = metadata.get("chromeEventCount")
    if isinstance(stored, int) and stored >= 0:
        return stored
    return sum(1 for row in _read_jsonl(recording_dir / "trace.jsonl") if row.get("source") == "chrome")


def _read_native_message(stream) -> Optional[Dict[str, Any]]:
    raw_length = stream.read(4)
    if not raw_length:
        return None
    if len(raw_length) != 4:
        raise EOFError("Incomplete native message length")
    length = struct.unpack("<I", raw_length)[0]
    raw = stream.read(length)
    if len(raw) != length:
        raise EOFError("Incomplete native message body")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Native message must be a JSON object")
    return value


def _write_native_message(stream, message: Dict[str, Any]) -> None:
    raw = json.dumps(message, sort_keys=True, default=str).encode("utf-8")
    stream.write(struct.pack("<I", len(raw)))
    stream.write(raw)
    stream.flush()


if __name__ == "__main__":
    raise SystemExit(native_host_main())
