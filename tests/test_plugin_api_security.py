from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from types import SimpleNamespace
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _load_plugin_api():
    package_name = f"record_plugin_api_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        package_name,
        PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    if spec is None or spec.loader is None:
        raise ImportError("Could not load Record plugin API")
    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    spec.loader.exec_module(module)
    return importlib.import_module(f"{package_name}.dashboard.plugin_api")


def _authorization_request(api):
    return api.RecordDesktopCaptureAuthorizationRequest(
        profile="default",
        name="Customer workflow",
        instructions="Open the customer portal and export the report.",
        schedule="0 9 * * 1-5",
        run_mode="schedule",
    )


def _start_request(api, authorization):
    request = _authorization_request(api).model_dump()
    return api.RecordDesktopRecordingStartRequest(
        **request,
        capture_authorization_nonce=authorization["capture_authorization_nonce"],
        capture_authorization=authorization,
    )


def test_desktop_capture_authorization_is_bound_and_single_use(
    tmp_path,
    monkeypatch,
):
    api = _load_plugin_api()
    api._DESKTOP_CAPTURE_AUTHORIZATIONS.clear()
    monkeypatch.setattr(api, "_cron_profile_home", lambda _profile: ("default", tmp_path))
    starts = []
    monkeypatch.setattr(
        api,
        "start_native_recording",
        lambda **kwargs: starts.append(kwargs) or {"status": "recording"},
    )

    authorization = api.provision_desktop_capture_authorization(
        _authorization_request(api),
    )
    assert authorization["source"] == "desktop"
    assert authorization["profile"] == "default"

    result = api.start_desktop_recording(_start_request(api, authorization))
    assert result == {"status": "recording"}
    assert starts[0]["capture_authorization_nonce"] == authorization["capture_authorization_nonce"]

    with pytest.raises(HTTPException, match="already used"):
        api.start_desktop_recording(_start_request(api, authorization))


def test_desktop_capture_authorization_rejects_changed_recording(
    tmp_path,
    monkeypatch,
):
    api = _load_plugin_api()
    api._DESKTOP_CAPTURE_AUTHORIZATIONS.clear()
    monkeypatch.setattr(api, "_cron_profile_home", lambda _profile: ("default", tmp_path))
    monkeypatch.setattr(
        api,
        "start_native_recording",
        lambda **_kwargs: pytest.fail("mismatched authorization started recording"),
    )

    authorization = api.provision_desktop_capture_authorization(
        _authorization_request(api),
    )
    changed = _start_request(api, authorization).model_copy(
        update={"name": "A different workflow"},
    )
    with pytest.raises(HTTPException, match="does not match"):
        api.start_desktop_recording(changed)


def test_scheduled_recording_projection_tracks_authoritative_cron_presence(
    tmp_path,
):
    api = _load_plugin_api()
    recording_dir = tmp_path / "recording"
    recording_dir.mkdir()
    for name in ("metadata.json", "session.json"):
        (recording_dir / name).write_text(
            json.dumps(
                {
                    "state": "scheduled",
                    "status": "scheduled",
                    "scheduled_job_id": "job-123",
                }
            ),
            encoding="utf-8",
        )

    assert api._reconcile_recording_schedule(
        recording_dir,
        SimpleNamespace(cron_job_presence=lambda _job_id: "present"),
        SimpleNamespace(seal_draft_revision=lambda _recording_dir: None),
    ) == "present"
    for name in ("metadata.json", "session.json"):
        value = json.loads((recording_dir / name).read_text(encoding="utf-8"))
        assert value["state"] == "scheduled"
        assert "schedule_missing_at" not in value

    seal_states = []
    assert api._reconcile_recording_schedule(
        recording_dir,
        SimpleNamespace(cron_job_presence=lambda _job_id: "missing"),
        SimpleNamespace(
            seal_draft_revision=lambda target: seal_states.append(
                json.loads((target / "metadata.json").read_text(encoding="utf-8"))["state"]
            )
        ),
    ) == "missing"
    assert seal_states == ["scheduled"]
    projected = [
        json.loads((recording_dir / name).read_text(encoding="utf-8"))
        for name in ("metadata.json", "session.json")
    ]
    assert {value["state"] for value in projected} == {"needs_redraft"}
    assert {value["status"] for value in projected} == {"needs_redraft"}
    assert {value["scheduled_job_id"] for value in projected} == {"job-123"}
    assert len({value["schedule_missing_at"] for value in projected}) == 1


def test_unknown_cron_read_does_not_claim_a_scheduled_job_is_missing(tmp_path):
    api = _load_plugin_api()
    recording_dir = tmp_path / "recording"
    recording_dir.mkdir()
    for name in ("metadata.json", "session.json"):
        (recording_dir / name).write_text(
            json.dumps(
                {
                    "state": "scheduled",
                    "status": "scheduled",
                    "scheduled_job_id": "job-123",
                }
            ),
            encoding="utf-8",
        )

    assert api._reconcile_recording_schedule(
        recording_dir,
        SimpleNamespace(cron_job_presence=lambda _job_id: "unknown"),
        SimpleNamespace(seal_draft_revision=lambda _recording_dir: None),
    ) == "unknown"
    for name in ("metadata.json", "session.json"):
        value = json.loads((recording_dir / name).read_text(encoding="utf-8"))
        assert value["state"] == "scheduled"
        assert "schedule_missing_at" not in value


def test_divergent_lifecycle_projection_fails_closed_without_cron_read(tmp_path):
    api = _load_plugin_api()
    recording_dir = tmp_path / "recording"
    recording_dir.mkdir()
    (recording_dir / "metadata.json").write_text(
        json.dumps(
            {
                "state": "scheduled",
                "status": "scheduled",
                "scheduled_job_id": "job-123",
            }
        ),
        encoding="utf-8",
    )
    (recording_dir / "session.json").write_text(
        json.dumps({"state": "drafted", "status": "drafted"}),
        encoding="utf-8",
    )

    def unexpected_read(_job_id):
        raise AssertionError("divergent lifecycle state queried Cron")

    assert api._reconcile_recording_schedule(
        recording_dir,
        SimpleNamespace(cron_job_presence=unexpected_read),
        SimpleNamespace(seal_draft_revision=lambda _recording_dir: None),
    ) == "unknown"
    assert json.loads(
        (recording_dir / "metadata.json").read_text(encoding="utf-8")
    )["state"] == "scheduled"
    assert json.loads(
        (recording_dir / "session.json").read_text(encoding="utf-8")
    )["state"] == "drafted"


def test_draft_artifacts_project_both_lifecycle_files_to_drafted(tmp_path):
    api = _load_plugin_api()
    recording_dir = tmp_path / "recording"
    recording_dir.mkdir()
    (recording_dir / "session.json").write_text(
        json.dumps(
            {
                "state": "needs_redraft",
                "status": "needs_redraft",
                "scheduled_job_id": "old-job",
            }
        ),
        encoding="utf-8",
    )
    candidate = SimpleNamespace(
        recording_dir=recording_dir,
        output=recording_dir / "workflow.json",
        workflow={"review": {"draft_revision": 2}},
        review_markdown="# Review\n",
        metadata={
            "state": "drafted",
            "status": "drafted",
            "drafted_at": "2026-08-09T01:00:00Z",
            "draft_revision": 2,
        },
    )

    artifacts = {
        path.name: json.loads(content)
        for path, content in api._candidate_artifacts(candidate)
        if path.suffix == ".json"
    }

    assert artifacts["metadata.json"]["state"] == "drafted"
    assert artifacts["metadata.json"]["status"] == "drafted"
    assert artifacts["session.json"] == {
        "state": "drafted",
        "status": "drafted",
        "scheduled_job_id": "old-job",
        "drafted_at": "2026-08-09T01:00:00Z",
        "draft_revision": 2,
    }
