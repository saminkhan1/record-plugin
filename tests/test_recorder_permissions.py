from __future__ import annotations

import importlib.util
import json
import threading
from pathlib import Path

import pytest

_CAPTURE_AUTHORIZATION = {
    "capture_authorization_nonce": "a" * 64,
    "capture_dashboard_origin": "http://127.0.0.1:9119",
}
PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _load_recorder_module():
    module_path = PLUGIN_ROOT / "hermes_recorder.py"
    spec = importlib.util.spec_from_file_location("hermes_crunch_recorder_under_test", module_path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _dev_client(mod) -> Path:
    return mod.PLUGIN_ROOT / "native" / "hermes-recorder" / "hermes-recorder"


def _write_trace_bundle(mod, recording: Path, *, status: str = "recording", active: bool = True, metadata: dict | None = None) -> None:
    recording.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schemaVersion": mod.RECORDER_SCHEMA,
        "active": active,
        "state": status,
        "sessionID": "rec-1",
        "eventCount": 1,
        "paths": {"trace": "trace.jsonl", "artifacts": "artifacts/"},
    }
    if metadata:
        manifest.update(metadata)
    (recording / "metadata.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    (recording / "session.json").write_text(
        json.dumps({"schemaVersion": mod.RECORDER_SCHEMA, "status": status, "eventCount": 1}) + "\n",
        encoding="utf-8",
    )
    (recording / "trace.jsonl").write_text(
        json.dumps({
            "id": 1,
            "ts": "2026-07-06T01:48:37Z",
            "type": "context",
            "source": "system",
            "kind": "session.started",
            "payload": {"session": {"id": "rec-1", "maximum_duration_seconds": 60}},
            "quality": {"confidence": "high", "warnings": []},
        }) + "\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("nonce", "origin"),
    [
        ("short", "http://127.0.0.1:9119"),
        ("g" * 64, "http://127.0.0.1:9119"),
        ("a" * 64, "https://127.0.0.1:9119"),
        ("a" * 64, "http://127.0.0.1"),
        ("a" * 64, "http://127.0.0.1:9119/workflows"),
        ("a" * 64, "http://example.test:9119"),
    ],
)
def test_start_rejects_untrusted_capture_authorization_before_claim(
    tmp_path,
    monkeypatch,
    nonce,
    origin,
):
    mod = _load_recorder_module()

    def unexpected_claim(_home):
        raise AssertionError("capture claim must not run before authorization validation")

    monkeypatch.setattr(mod, "_claim_chrome_capture_start", unexpected_claim)

    with pytest.raises(mod.HTTPException) as exc:
        mod.start_native_recording(
            home=tmp_path,
            profile_name="default",
            workflow_id="delete-spam",
            name="Delete Spam",
            instructions="Delete spam.",
            schedule="every 1h",
            capture_authorization_nonce=nonce,
            capture_dashboard_origin=origin,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == mod._CAPTURE_AUTHORIZATION_BLOCK_DETAIL
    assert not (tmp_path / "crunch").exists()


def test_native_client_prefers_explicit_client(tmp_path, monkeypatch):
    mod = _load_recorder_module()
    client = tmp_path / "isolated-recorder" / "hermes-recorder"
    client.parent.mkdir()
    client.write_text("#!/bin/sh\n", encoding="utf-8")
    client.chmod(0o755)

    monkeypatch.setenv("HERMES_RECORDER_CLIENT", str(client))

    assert mod._native_client_path() == client.resolve()


@pytest.mark.parametrize(
    ("permission_state", "missing_key"),
    [
        (
            {
                "accessibilityTrusted": False,
                "screenCaptureGranted": False,
                "listenEventGranted": False,
            },
            "accessibilityTrusted",
        ),
        (
            {
                "accessibilityTrusted": True,
                "screenCaptureGranted": False,
                "listenEventGranted": False,
            },
            "screenCaptureGranted",
        ),
        (
            {
                "accessibilityTrusted": True,
                "screenCaptureGranted": True,
                "listenEventGranted": False,
            },
            "listenEventGranted",
        ),
    ],
)
def test_open_macos_privacy_settings_targets_first_missing_permission(permission_state, missing_key, monkeypatch):
    mod = _load_recorder_module()
    opened = []

    class FakePopen:
        def __init__(self, cmd, **_kwargs):
            opened.append(cmd)

    monkeypatch.setattr(mod.subprocess, "Popen", FakePopen)

    mod._open_macos_privacy_settings(permission_state)

    assert opened == [["open", mod._privacy_settings_url(mod._RECORDER_PERMISSION_SETTINGS_PANES[missing_key])]]


def test_active_native_recording_passively_reports_scoped_recording(tmp_path, monkeypatch):
    mod = _load_recorder_module()
    client = _dev_client(mod)
    recording = tmp_path / "crunch" / "recordings" / "active-workflow" / "rec-1"
    _write_trace_bundle(
        mod,
        recording,
        metadata={
            "profile": "default",
            "workflow_id": "active-workflow",
            "name": "Active Workflow",
            "instructions": "Demonstrate the active workflow.",
        },
    )
    repair_calls = []

    def passive_scan(**kwargs):
        repair_calls.append(kwargs)
        return {
            "reason": "active_recordings_left_running",
            "activeSessions": [{
                "pid": 42,
                "sessionState": {"active": True, "recordingDir": str(recording)},
            }],
            "terminated": [],
        }

    monkeypatch.setattr(mod, "_native_client_path", lambda: client)
    monkeypatch.setattr(mod, "_remote_backend_reason", lambda: None)
    monkeypatch.setattr(mod, "_reconcile_scoped_recorder_service", passive_scan)

    result = mod.active_native_recording(home=tmp_path, profile_name="default")

    assert repair_calls[0]["trigger"] is False
    assert result == {
        "recording_dir": str(recording),
        "recording_key": "active-workflow/rec-1",
        "name": "Active Workflow",
        "instructions": "Demonstrate the active workflow.",
        "status": "recording",
    }


def test_reconcile_scoped_service_terminates_only_idle_services_in_active_profile(tmp_path, monkeypatch):
    mod = _load_recorder_module()
    client = _dev_client(mod)
    recordings_root = tmp_path / "crunch" / "recordings"
    idle_root = recordings_root / "idle-workflow"
    active_root = recordings_root / "active-workflow"
    active_session = active_root / "active-session"
    active_session.mkdir(parents=True)
    (active_root / "service-state.json").write_text(
        json.dumps({"activeSessionPath": str(active_session), "sessionID": "active-session"}) + "\n",
        encoding="utf-8",
    )
    (active_session / "metadata.json").write_text(
        json.dumps({"active": True, "state": "recording", "sessionID": "active-session"}) + "\n",
        encoding="utf-8",
    )
    terminated = []

    monkeypatch.setattr(mod.os.sys, "platform", "darwin")
    monkeypatch.setattr(mod, "_native_client_path", lambda: client)
    monkeypatch.setattr(
        mod,
        "_scoped_recorder_processes",
        lambda _root: [
            {"pid": 101, "root": str(idle_root), "socket": "/tmp/idle.sock", "command": "hermes-recorder-service --root idle"},
            {"pid": 202, "root": str(active_root), "socket": "/tmp/active.sock", "command": "hermes-recorder-service --root active"},
        ],
    )
    monkeypatch.setattr(
        mod,
        "_terminate_scoped_recorder_processes",
        lambda processes: terminated.extend(processes) or [
            {"pid": item["pid"], "root": item["root"], "signal": "SIGTERM", "ok": True}
            for item in processes
        ],
    )

    passive = mod._reconcile_scoped_recorder_service(
        scope_root=recordings_root,
        client=client,
        remote_reason=None,
        trigger=False,
    )
    assert passive["reason"] == "active_recordings_left_running"
    assert [item["pid"] for item in passive["activeSessions"]] == [202]
    assert passive["terminated"] == []
    assert terminated == []

    repair = mod._reconcile_scoped_recorder_service(
        scope_root=recordings_root,
        client=client,
        remote_reason=None,
        trigger=True,
    )
    assert repair["reason"] == "terminated_idle_scoped_processes"
    assert [item["pid"] for item in terminated] == [101]
    assert [item["pid"] for item in repair["activeSessions"]] == [202]


def test_start_repairs_profile_scope_and_blocks_existing_active_recording(tmp_path, monkeypatch):
    mod = _load_recorder_module()
    repair_calls = []

    monkeypatch.setattr(mod, "_require_native_client", lambda: _dev_client(mod))
    monkeypatch.setattr(mod, "_remote_backend_reason", lambda: None)
    monkeypatch.setattr(
        mod,
        "_reconcile_scoped_recorder_service",
        lambda **kwargs: repair_calls.append(kwargs) or {
            "reason": "active_recordings_left_running",
            "activeSessions": [{"pid": 42, "root": str(tmp_path / "crunch" / "recordings" / "other")}],
            "terminated": [],
        },
    )

    with pytest.raises(mod.HTTPException) as exc:
        mod.start_native_recording(
            home=tmp_path,
            profile_name="default",
            workflow_id="delete-spam",
            name="delete spam",
            instructions="Delete spam.",
            schedule="every 1h",
            **_CAPTURE_AUTHORIZATION,
        )

    assert exc.value.status_code == 409
    assert repair_calls
    assert repair_calls[0]["scope_root"] == tmp_path / "crunch" / "recordings"
    assert repair_calls[0]["trigger"] is True


def test_start_serializes_root_capture_claim_across_profiles(tmp_path, monkeypatch):
    mod = _load_recorder_module()
    root = tmp_path / "home"
    first_started = threading.Event()
    release_first = threading.Event()
    second_attempted = threading.Event()
    state_lock = threading.Lock()
    active = False
    starts = []
    outcomes = []
    thread_errors = []

    def start_transaction(**kwargs):
        nonlocal active
        with state_lock:
            if active:
                raise mod.HTTPException(status_code=409, detail="active")
            starts.append(kwargs["profile_name"])
            first = len(starts) == 1
        if first:
            first_started.set()
            assert release_first.wait(timeout=5)
        with state_lock:
            active = True
        return {"status": "recording"}

    def invoke(home, profile_name, workflow_id, attempted=None):
        if attempted is not None:
            attempted.set()
        try:
            mod.start_native_recording(
                home=home,
                profile_name=profile_name,
                workflow_id=workflow_id,
                name=workflow_id,
                instructions="Record this workflow.",
                schedule="",
                **_CAPTURE_AUTHORIZATION,
            )
            outcomes.append(200)
        except mod.HTTPException as exc:
            outcomes.append(exc.status_code)
        except BaseException as exc:  # Keep thread failures visible to pytest.
            thread_errors.append(exc)

    monkeypatch.setattr(mod, "_start_native_recording", start_transaction)

    first = threading.Thread(target=invoke, args=(root, "default", "first"), daemon=True)
    second = threading.Thread(
        target=invoke,
        args=(root / "profiles" / "work", "work", "second", second_attempted),
        daemon=True,
    )
    first.start()
    assert first_started.wait(timeout=2)
    second.start()
    assert second_attempted.wait(timeout=2)
    second.join(timeout=0.1)
    second_was_blocked = second.is_alive()
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert thread_errors == []
    assert not first.is_alive()
    assert not second.is_alive()
    assert second_was_blocked
    assert starts == ["default"]
    assert sorted(outcomes) == [200, 409]


def test_start_native_recording_requests_permissions_then_blocks_without_bundle(tmp_path, monkeypatch):
    mod = _load_recorder_module()
    actions = []
    opened = []

    def fake_run_client(_client, *, root, action, max_duration_seconds=None, show_controls=None, settings_pane=None, permission_key=None):
        actions.append(action)
        if action == "permission-status":
            return {
                "permissionState": {
                    "platform": "macos",
                    "accessibilityTrusted": False,
                    "screenCaptureGranted": False,
                    "listenEventGranted": False,
                }
            }
        if action == "request-permissions":
            return {
                "permissionState": {
                    "platform": "macos",
                    "accessibilityTrusted": False,
                    "screenCaptureGranted": False,
                    "listenEventGranted": False,
                }
            }
        if action == "open-settings":
            return {"settingsPane": settings_pane}
        if action == "start":
            raise AssertionError("start must not run without permissions")
        raise AssertionError(f"unexpected recorder action {action}")

    monkeypatch.setattr(mod, "_require_native_client", lambda: _dev_client(mod))
    monkeypatch.setattr(mod, "_remote_backend_reason", lambda: None)
    monkeypatch.setattr(
        mod,
        "_reconcile_scoped_recorder_service",
        lambda **_kwargs: {"reason": "no_idle_processes", "activeSessions": [], "terminated": []},
    )
    monkeypatch.setattr(mod, "_run_client", fake_run_client)
    monkeypatch.setattr(mod, "chrome_provider_preflight", lambda: {"status": "ready", "needs_attention": []})
    monkeypatch.setattr(
        mod,
        "activate_chrome_capture",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("chrome capture must not start without permissions")),
    )
    monkeypatch.setattr(mod.subprocess, "Popen", lambda cmd, **_kwargs: opened.append(cmd))

    with pytest.raises(mod.HTTPException) as exc:
        mod.start_native_recording(
            home=tmp_path,
            profile_name="default",
            workflow_id="delete-spam",
            name="Delete Spam",
            instructions="Delete spam.",
            schedule="every 1h",
            **_CAPTURE_AUTHORIZATION,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == (
        "Missing required macOS recorder permissions: Accessibility, Screen Recording, Input Monitoring "
        "must be granted before recording."
    )
    assert "Input Monitoring" in exc.value.detail
    assert actions == ["permission-status", "request-permissions", "open-settings"]
    assert opened == []
    assert not (tmp_path / "crunch" / "recordings" / "delete-spam").exists()

def test_start_native_recording_hides_native_controls_for_dashboard(tmp_path, monkeypatch):
    mod = _load_recorder_module()
    calls = []

    def fake_run_client(_client, *, root, action, max_duration_seconds=None, show_controls=None, permission_key=None):
        if action == "permission-status":
            return {
                "permissionState": {
                    "platform": "macos",
                    "accessibilityTrusted": True,
                    "screenCaptureGranted": True,
                    "listenEventGranted": True,
                }
            }
        if action == "request-permissions":
            return {
                "permissionState": {
                    "platform": "macos",
                    "accessibilityTrusted": True,
                    "screenCaptureGranted": True,
                    "listenEventGranted": True,
                }
            }
        assert action == "start"
        calls.append({
            "root": root,
            "max_duration_seconds": max_duration_seconds,
            "show_controls": show_controls,
        })
        recording = root / "rec-1"
        _write_trace_bundle(mod, recording, status="recording", active=True)
        (recording / "capture-state.json").write_text(
                json.dumps({
                    "eventTapHealth": {"tapCreated": True, "tapEnabled": True},
                    "accessibilityHealth": {"ready": True},
                    "visualCaptureHealth": {"ready": True, "firstFrameAt": "2026-07-14T00:00:00Z"},
                }) + "\n",
            encoding="utf-8",
        )
        (recording / "visual").mkdir()
        (recording / "visual" / "frames.jsonl").write_text(
            json.dumps({"seq": 1, "payload": {"path": "visual/active.mp4"}}) + "\n",
            encoding="utf-8",
        )
        (recording / "visual" / "active.mp4").write_bytes(b"frame")
        return {"eventStreamRootPath": str(recording)}

    monkeypatch.setattr(mod, "_require_native_client", lambda: _dev_client(mod))
    monkeypatch.setattr(mod, "_remote_backend_reason", lambda: None)
    monkeypatch.setattr(
        mod,
        "_reconcile_scoped_recorder_service",
        lambda **_kwargs: {"reason": "no_idle_processes", "activeSessions": [], "terminated": []},
    )
    monkeypatch.setattr(mod, "_run_client", fake_run_client)
    monkeypatch.setattr(mod, "chrome_provider_preflight", lambda: {"status": "ready", "needs_attention": []})
    monkeypatch.setattr(mod, "activate_chrome_capture", lambda **_kwargs: None)
    monkeypatch.setattr(mod, "_wait_for_live_chrome_provider", lambda _recording: {"contentScriptReady": True})

    result = mod.start_native_recording(
        home=tmp_path,
        profile_name="default",
        workflow_id="delete-spam",
        name="Delete Spam",
        instructions="Delete spam.",
        schedule="every 1h",
        **_CAPTURE_AUTHORIZATION,
    )

    assert calls[0]["root"] == tmp_path / "crunch" / "recordings" / "delete-spam"
    assert calls[0]["show_controls"] is False
    assert result["status"] == "recording"
    metadata = json.loads((Path(result["recording_dir"]) / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["source"] == "dashboard"
    assert "capture_consent" not in metadata
    assert list(Path(result["recording_dir"]).glob(".start-readiness-*")) == []


def test_native_capture_readiness_requires_a_real_video_frame(tmp_path):
    mod = _load_recorder_module()
    recording = tmp_path / "recording"
    recording.mkdir()
    (recording / "capture-state.json").write_text(
        json.dumps(
            {
                "eventTapHealth": {"tapCreated": True, "tapEnabled": True},
                "accessibilityHealth": {"ready": True},
                "visualCaptureHealth": {
                    "ready": False,
                    "firstFrameAt": None,
                    "activeSurface": {"app": {"bundleIdentifier": "com.apple.loginwindow"}},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = mod._wait_for_native_capture_ready(recording, timeout_seconds=0)

    assert result == {}
    assert "first Screen Recording frame" in mod._native_live_block_detail(
        recording,
        json.loads((recording / "capture-state.json").read_text(encoding="utf-8"))
    )


def test_start_native_recording_requires_chrome_provider_ready(tmp_path, monkeypatch):
    mod = _load_recorder_module()
    actions = []

    def fake_run_client(_client, *, root, action, max_duration_seconds=None, show_controls=None):
        actions.append(action)
        if action == "start":
            raise AssertionError("native recording must not start without Chrome provider readiness")
        return {"permissionState": {"platform": "macos"}}

    monkeypatch.setattr(mod, "_require_native_client", lambda: _dev_client(mod))
    monkeypatch.setattr(mod, "_remote_backend_reason", lambda: None)
    monkeypatch.setattr(
        mod,
        "_reconcile_scoped_recorder_service",
        lambda **_kwargs: {"reason": "no_idle_processes", "activeSessions": [], "terminated": []},
    )
    monkeypatch.setattr(mod, "_run_client", fake_run_client)
    monkeypatch.setattr(
        mod,
        "chrome_provider_preflight",
        lambda: {
            "status": "needs_attention",
            "needs_attention": ["Hermes Chrome provider extension is not loaded in Chrome."],
        },
    )
    monkeypatch.setattr(
        mod,
        "activate_chrome_capture",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("chrome capture must not activate without provider readiness")),
    )

    with pytest.raises(mod.HTTPException) as exc:
        mod.start_native_recording(
            home=tmp_path,
            profile_name="default",
            workflow_id="delete-spam",
            name="Delete Spam",
            instructions="Delete spam.",
            schedule="every 1h",
            **_CAPTURE_AUTHORIZATION,
        )

    assert exc.value.status_code == 409
    assert mod._START_CHROME_PROVIDER_BLOCK_DETAIL in exc.value.detail
    assert "extension is not loaded" in exc.value.detail
    assert actions == []
    assert not (tmp_path / "crunch" / "recordings" / "delete-spam").exists()


def test_start_native_recording_requests_permissions_then_starts_when_granted(tmp_path, monkeypatch):
    mod = _load_recorder_module()
    actions = []
    start_calls = []

    def fake_run_client(_client, *, root, action, max_duration_seconds=None, show_controls=None, permission_key=None):
        actions.append(action)
        if action == "permission-status":
            return {
                "permissionState": {
                    "platform": "macos",
                    "accessibilityTrusted": False,
                    "screenCaptureGranted": True,
                    "listenEventGranted": True,
                }
            }
        if action == "request-permissions":
            return {
                "permissionState": {
                    "platform": "macos",
                    "accessibilityTrusted": True,
                    "screenCaptureGranted": True,
                    "listenEventGranted": True,
                }
            }
        assert action == "start"
        start_calls.append({
            "root": root,
            "max_duration_seconds": max_duration_seconds,
            "show_controls": show_controls,
        })
        recording = root / "rec-1"
        _write_trace_bundle(mod, recording, status="recording", active=True)
        (recording / "capture-state.json").write_text(
                json.dumps({
                    "eventTapHealth": {"tapCreated": True, "tapEnabled": True},
                    "accessibilityHealth": {"ready": True},
                    "visualCaptureHealth": {"ready": True, "firstFrameAt": "2026-07-14T00:00:00Z"},
                }) + "\n",
            encoding="utf-8",
        )
        (recording / "visual").mkdir()
        (recording / "visual" / "frames.jsonl").write_text(
            json.dumps({"seq": 1, "payload": {"path": "visual/active.mp4"}}) + "\n",
            encoding="utf-8",
        )
        (recording / "visual" / "active.mp4").write_bytes(b"frame")
        return {"eventStreamRootPath": str(recording)}

    monkeypatch.setattr(mod, "_require_native_client", lambda: _dev_client(mod))
    monkeypatch.setattr(mod, "_remote_backend_reason", lambda: None)
    monkeypatch.setattr(
        mod,
        "_reconcile_scoped_recorder_service",
        lambda **_kwargs: {"reason": "no_idle_processes", "activeSessions": [], "terminated": []},
    )
    monkeypatch.setattr(mod, "_run_client", fake_run_client)
    monkeypatch.setattr(mod, "chrome_provider_preflight", lambda: {"status": "ready", "needs_attention": []})
    monkeypatch.setattr(mod, "activate_chrome_capture", lambda **_kwargs: None)
    monkeypatch.setattr(mod, "_wait_for_live_chrome_provider", lambda _recording: {"contentScriptReady": True})

    result = mod.start_native_recording(
        home=tmp_path,
        profile_name="default",
        workflow_id="delete-spam",
        name="Delete Spam",
        instructions="Delete spam.",
        schedule="every 1h",
        **_CAPTURE_AUTHORIZATION,
    )

    assert actions[:3] == ["permission-status", "request-permissions", "start"]
    assert start_calls[0]["show_controls"] is False
    assert result["status"] == "recording"


def test_recording_status_finalizes_chrome_after_native_controls_stop(tmp_path, monkeypatch):
    mod = _load_recorder_module()
    recording = tmp_path / "crunch" / "recordings" / "delete-spam" / "rec-1"
    chrome_dir = recording / "chrome"
    chrome_dir.mkdir(parents=True)
    _write_trace_bundle(
        mod,
        recording,
        status="stopped",
        active=False,
        metadata={"chrome_provider": {"active": True}},
    )
    finalized = []

    monkeypatch.setattr(mod, "deactivate_chrome_capture", lambda **kwargs: finalized.append(kwargs) or {})
    monkeypatch.setattr(mod, "_reconcile_scoped_recorder_service", lambda **_kwargs: {"reason": "no_idle_processes"})
    monkeypatch.setattr(mod, "_native_client_path", lambda: _dev_client(mod))
    monkeypatch.setattr(mod, "_remote_backend_reason", lambda: None)

    status = mod.recording_status(home=tmp_path, recording_dir=recording)

    assert status["status"] == "stopped"
    assert finalized
    assert finalized[0]["recording_dir"] == recording


def test_stop_promotes_real_native_active_video_contract(tmp_path, monkeypatch):
    mod = _load_recorder_module()
    recording = tmp_path / "crunch" / "recordings" / "complete" / "rec-1"
    _write_trace_bundle(mod, recording)
    timestamp = "2026-07-06T01:48:37Z"
    (recording / "trace.jsonl").write_text(
        json.dumps({
            "id": 1,
            "ts": timestamp,
            "type": "action",
            "source": "native",
            "kind": "click",
                "payload": {
                    "meta": {"correlation_id": "stop-contract"},
                    "target": {"role": "AXButton", "label": "Done"},
                    "input": {"x": 10, "y": 20},
                },
            "quality": {"confidence": "high", "warnings": []},
        }) + "\n",
        encoding="utf-8",
    )
    (recording / "raw").mkdir()
    (recording / "visual").mkdir()
    (recording / "raw" / "input.jsonl").write_text(
        json.dumps({"seq": 1, "ts": timestamp, "kind": "leftMouseUp", "correlation_id": "stop-contract"}) + "\n",
        encoding="utf-8",
    )
    (recording / "visual" / "frames.jsonl").write_text(
        json.dumps({"seq": 2, "ts": timestamp, "kind": "visual.frame", "correlation_id": "stop-contract", "payload": {"path": "visual/active.mp4"}}) + "\n",
        encoding="utf-8",
    )
    (recording / "visual" / "active.mp4").write_bytes(b"native-video")

    def stop_client(*_args, **_kwargs):
        for name in ("metadata.json", "session.json"):
            path = recording / name
            data = json.loads(path.read_text(encoding="utf-8"))
            data.update({"active": False, "state": "stopped", "status": "stopped"})
            path.write_text(json.dumps(data) + "\n", encoding="utf-8")
        return {
            "active": False,
            "state": "inactive",
            "eventStreamRootPath": str(recording.parent),
        }

    monkeypatch.setattr(mod, "_require_native_client", lambda: Path("/fake/hermes-recorder"))
    monkeypatch.setattr(mod, "_native_client_path", lambda: Path("/fake/hermes-recorder"))
    monkeypatch.setattr(mod, "_run_client", stop_client)
    monkeypatch.setattr(mod, "deactivate_chrome_capture", lambda **_kwargs: None)
    monkeypatch.setattr(mod, "_reconcile_scoped_recorder_service", lambda **_kwargs: {"reason": "no_idle_processes"})
    monkeypatch.setattr(mod, "_remote_backend_reason", lambda: None)
    status = mod.stop_native_recording(home=tmp_path, recording_dir=recording)

    metadata = json.loads((recording / "metadata.json").read_text(encoding="utf-8"))
    assert status["status"] == "stopped"
    assert metadata["capture_verdict"]["ready"] is True
    assert metadata["capture_verdict"]["media_paths"] == ["visual/active.mp4"]
    manifest = json.loads((recording / "capture-manifest.json").read_text(encoding="utf-8"))
    assert any(item["path"] == "visual/active.mp4" for item in manifest["files"])



def test_stop_preserves_unready_v3_capture_and_returns_stopped_status(tmp_path, monkeypatch):
    mod = _load_recorder_module()
    recording = tmp_path / "crunch" / "recordings" / "target-gap" / "rec-1"
    _write_trace_bundle(
        mod,
        recording,
        metadata={"schemaVersion": mod.SCHEMA_VERSION},
    )
    (recording / "trace.jsonl").write_text(
        json.dumps(
            {
                "id": 1,
                "ts": "2026-07-06T01:48:37Z",
                "type": "action",
                "source": "native",
                "kind": "click",
                "payload": {"input": {}},
                "quality": {"confidence": "low", "warnings": ["missing_target", "missing_replay_data"]},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    def stop_client(*_args, **_kwargs):
        for name in ("metadata.json", "session.json"):
            path = recording / name
            data = json.loads(path.read_text(encoding="utf-8"))
            data.update({"active": False, "state": "stopped", "status": "stopped"})
            path.write_text(json.dumps(data) + "\n", encoding="utf-8")
        return {
            "active": False,
            "state": "inactive",
            "eventStreamRootPath": str(recording.parent),
        }

    monkeypatch.setattr(mod, "_require_native_client", lambda: Path("/fake/hermes-recorder"))
    monkeypatch.setattr(mod, "_native_client_path", lambda: Path("/fake/hermes-recorder"))
    monkeypatch.setattr(mod, "_run_client", stop_client)
    monkeypatch.setattr(mod, "deactivate_chrome_capture", lambda **_kwargs: None)
    monkeypatch.setattr(mod, "_reconcile_scoped_recorder_service", lambda **_kwargs: {"reason": "no_idle_processes"})
    monkeypatch.setattr(mod, "_remote_backend_reason", lambda: None)
    status = mod.stop_native_recording(
        home=tmp_path,
        recording_dir=recording,
        chrome_finalize_ok=False,
        chrome_finalize_error="continuity gap",
    )

    assert status["status"] == "stopped"
    metadata = json.loads((recording / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["schemaVersion"] == mod.SCHEMA_VERSION
    assert metadata["capture_verdict"]["ready"] is False
    assert {reason["code"] for reason in metadata["capture_verdict"]["reasons"]} >= {
        "missing_raw_input",
        "chrome_provider_finalization_failed",
    }
    assert (recording / "evidence-pack.json").is_file()
    assert (recording / "capture-manifest.json").is_file()

def test_live_chrome_join_requires_content_script_and_attached_target(tmp_path):
    mod = _load_recorder_module()
    recording = tmp_path / "recording"
    recording.mkdir()
    state = {
        "sessionActive": True,
        "desiredProvider": "chrome",
        "effectiveProvider": "chrome",
        "chromeSegmentActive": True,
        "contentScriptReady": False,
        "activeTargetAttached": True,
        "activeTabId": 7,
    }
    (recording / "capture-state.json").write_text(json.dumps(state) + "\n", encoding="utf-8")

    assert mod._wait_for_live_chrome_provider(recording, timeout_seconds=0) == {}

    state["contentScriptReady"] = True
    (recording / "capture-state.json").write_text(json.dumps(state) + "\n", encoding="utf-8")
    assert mod._wait_for_live_chrome_provider(recording, timeout_seconds=0)["activeTabId"] == 7

def test_scoped_recorder_process_filter_stays_under_profile_root(tmp_path):
    mod = _load_recorder_module()
    scope = tmp_path / "profile" / "crunch" / "recordings"
    inside = scope / "workflow-a"
    outside = tmp_path / "other-profile" / "crunch" / "recordings" / "workflow-b"
    rows = [
        {"pid": 101, "command": f"hermes-recorder-service --root {inside} --socket /tmp/inside.sock"},
        {"pid": 202, "command": f"hermes-recorder-service --root {outside} --socket /tmp/outside.sock"},
        {"pid": 303, "command": f"some-helper --root {inside}"},
    ]

    matches = mod._filter_scoped_recorder_processes(rows, scope)

    assert [item["pid"] for item in matches] == [101]
