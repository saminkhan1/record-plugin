from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from hermes_plugins.record.record import (
    compiler,
    evidence,
    hermes,
    recording,
)
from hermes_plugins.record import chrome_capture, hermes_recorder, record_recorder
from hermes_plugins.record.recording_bundle import write_json
from hermes_plugins.record.hermes_recorder import discard_native_recording
from hermes_plugins.record.record.trace_schema import (
    LEGACY_SCHEMA_VERSION,
    SCHEMA_VERSION,
    http_url_origin,
    normalize_trace_row,
    url_origin,
)


def _row(row_id: int, *, source: str, kind: str, payload: dict) -> dict:
    return {
        "id": row_id,
        "ts": f"2026-07-12T12:00:0{row_id}Z",
        "type": "action",
        "source": source,
        "kind": kind,
        "payload": payload,
        "quality": {"confidence": "high", "warnings": []},
    }


def test_chrome_segment_start_waits_for_backend_recording(monkeypatch):
    monkeypatch.setattr(chrome_capture, "_resolve_active_state", lambda: None)

    result = chrome_capture.ChromeNativeHost().handle_message({
        "id": 7,
        "type": "segment.start",
    })

    assert result == {
        "id": 7,
        "ok": True,
        "active": False,
        "segmentActive": False,
        "desiredProvider": "native",
    }


def test_desktop_chrome_authority_and_finalize_ack_round_trip(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "profiles" / "customer"
    recording_dir = (
        home
        / "record"
        / "recordings"
        / "customer-workflow"
        / "recording-1"
    )
    recording_dir.mkdir(parents=True)
    for name, value in (
        (
            "metadata.json",
            {
                "schemaVersion": SCHEMA_VERSION,
                "active": True,
                "state": "recording",
                "sessionID": "recording-1",
            },
        ),
        (
            "session.json",
            {
                "schemaVersion": SCHEMA_VERSION,
                "active": True,
                "status": "recording",
                "sessionID": "recording-1",
            },
        ),
    ):
        (recording_dir / name).write_text(
            json.dumps(value),
            encoding="utf-8",
        )
    (recording_dir / "trace.jsonl").write_text("", encoding="utf-8")

    authorization = "ab" * 32
    chrome_capture.activate_chrome_capture(
        home=home,
        profile_name="customer",
        recording_dir=recording_dir,
        workflow_id="customer-workflow",
        title="Customer workflow",
        capture_authorization_sha256=hashlib.sha256(
            authorization.encode("ascii"),
        ).hexdigest(),
        capture_authorization=authorization,
    )
    monkeypatch.setenv("HERMES_HOME", str(home))

    host = chrome_capture.ChromeNativeHost()
    status = host.handle_message({"id": 1, "type": "status"})
    desktop = status["desktopCaptureAuthority"]
    assert status["ok"] is True
    assert status["protocolVersion"] == chrome_capture.CHROME_PROTOCOL_VERSION
    assert desktop["active"] is True
    assert desktop["recordingId"] == "recording-1"
    assert desktop["captureAuthorization"] == authorization

    request = chrome_capture.request_desktop_chrome_finalize(
        home=home,
        recording_dir=recording_dir,
    )
    ack = host.handle_message(
        {
            "id": 2,
            "type": "desktop.finalize.ack",
            "protocolVersion": chrome_capture.CHROME_PROTOCOL_VERSION,
            "recordingId": "recording-1",
            "requestId": request["id"],
            "result": {"ok": True, "continuity": {"ok": True, "reasons": []}},
        }
    )
    assert ack["ok"] is True
    persisted = chrome_capture.desktop_chrome_finalize_ack(
        home=home,
        recording_dir=recording_dir,
        request_id=request["id"],
    )
    assert persisted["result"]["continuity"]["ok"] is True


def test_dismissal_is_a_durable_terminal_state_not_a_recursive_delete(tmp_path, monkeypatch):
    recording_dir = tmp_path / "record" / "recordings" / "example" / "rec-1"
    recording_dir.mkdir(parents=True)
    for name, value in (
        ("metadata.json", {"state": "stopped", "status": "stopped"}),
        ("session.json", {"state": "stopped", "status": "stopped"}),
    ):
        (recording_dir / name).write_text(json.dumps(value), encoding="utf-8")
    (recording_dir / "trace.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(hermes_recorder, "deactivate_chrome_capture", lambda **_kwargs: None)

    result = discard_native_recording(home=tmp_path, recording_dir=recording_dir)

    assert result == {"status": "dismissed"}
    assert recording_dir.is_dir()
    for name in ("metadata.json", "session.json"):
        state = json.loads((recording_dir / name).read_text(encoding="utf-8"))
        assert state["state"] == "dismissed"
        assert state["status"] == "dismissed"
        assert state["dismissed_at"]


def test_scheduled_recording_cannot_be_discarded(tmp_path):
    recording_dir = tmp_path / "record" / "recordings" / "example" / "rec-1"
    recording_dir.mkdir(parents=True)
    for name in ("metadata.json", "session.json"):
        (recording_dir / name).write_text(
            json.dumps({"state": "scheduled", "status": "scheduled"}),
            encoding="utf-8",
        )

    with pytest.raises(HTTPException, match="scheduled workflow cannot be discarded"):
        discard_native_recording(home=tmp_path, recording_dir=recording_dir)
    assert recording_dir.is_dir()


def test_instruction_requirements_support_local_sites_and_arbitrary_observed_apps():
    trace = [{
        "type": "context",
        "payload": {
            "app": {
                "name": "Acme Ledger Pro",
                "bundle_id": "com.acme.ledger",
            }
        },
    }]

    assert evidence.instruction_browser_origins({
        "instructions": (
            "Open http://localhost:3000/quarter-close and "
            "HTTPS://EXAMPLE.TEST/export, then export the report."
        )
    }) == {"http://localhost:3000", "https://example.test"}
    assert http_url_origin("chrome-extension://example/page") == ""
    assert url_origin("chrome-extension://example/page") == "chrome-extension://example"
    assert evidence.instruction_required_apps({
        "instructions": "Export the quarter in Acme Ledger Pro."
    }, trace=trace) == {
        "Acme Ledger Pro": {"bundle:com.acme.ledger", "name:acme ledger pro"}
    }


def test_normalize_accepts_targetless_chrome_document_scroll_with_zero_offsets():
    row = normalize_trace_row(
        _row(
            78,
            source="chrome",
            kind="scroll",
            payload={
                "browser": {"url": "https://example.test/page", "origin": "https://example.test"},
                "input": {"scroll_x": 0, "scroll_y": 0},
            },
        )
    )

    assert "missing_target" not in row["quality"]["warnings"]
    assert row["quality"]["confidence"] == "high"


def test_normalize_rejects_targetless_chrome_scroll_without_complete_page_data():
    row = normalize_trace_row(
        _row(
            112,
            source="chrome",
            kind="scroll",
            payload={
                "browser": {"url": "https://example.test/page", "origin": "https://example.test"},
                "input": {"scroll_x": 12},
            },
        )
    )

    assert "missing_target" in row["quality"]["warnings"]
    assert row["quality"]["confidence"] == "low"


def test_normalize_keeps_targetless_non_chrome_scroll_invalid():
    row = normalize_trace_row(
        _row(
            160,
            source="native",
            kind="scroll",
            payload={"input": {"scroll_x": 0, "scroll_y": 0}},
        )
    )

    assert "missing_target" in row["quality"]["warnings"]
    assert row["quality"]["confidence"] == "low"



def test_normalize_reconciles_historical_missing_target_warnings():
    persisted_scroll = _row(
        161,
        source="chrome",
        kind="scroll",
        payload={
            "browser": {"url": "https://example.test/page", "origin": "https://example.test"},
            "input": {"scroll_x": 120, "scroll_y": 240},
        },
    )
    persisted_scroll["quality"]["warnings"] = ["missing_target", "other_warning"]
    normalized_scroll = normalize_trace_row(persisted_scroll)
    assert normalized_scroll["quality"]["warnings"] == ["other_warning"]

    historical_selection = _row(
        162,
        source="native",
        kind="select",
        payload={"input": {"text": "selected text"}},
    )
    historical_selection["quality"]["warnings"] = ["missing_target", "selection_warning"]
    normalized_selection = normalize_trace_row(historical_selection)
    assert normalized_selection["quality"]["warnings"] == ["selection_warning"]


def test_normalize_accepts_historical_chrome_fallback_selection():
    row = _row(
        164,
        source="native",
        kind="native.chrome_fallback",
        payload={
            "meta": {"original_kind": "selection.changed"},
            "input": {"selected_text": "https://youtu.be/example"},
        },
    )
    row["type"] = "evidence"
    row["quality"]["warnings"] = ["missing_target"]

    normalized = normalize_trace_row(row)

    assert "missing_target" not in normalized["quality"]["warnings"]


def test_normalize_accepts_targetless_chrome_fallback_shortcut():
    row = _row(
        165,
        source="native",
        kind="native.chrome_fallback",
        payload={
            "meta": {"original_kind": "keyboard.shortcut"},
            "input": {"code": 37, "key": "l", "modifiers": ["command"]},
        },
    )
    row["type"] = "evidence"

    normalized = normalize_trace_row(row)

    assert "missing_target" not in normalized["quality"]["warnings"]

def test_normalize_preserves_missing_target_for_target_required_action():
    row = _row(
        163,
        source="chrome",
        kind="select",
        payload={"input": {"text": "selected text"}},
    )
    row["quality"]["warnings"] = ["missing_target", "other_warning"]

    normalized = normalize_trace_row(row)

    assert normalized["quality"]["warnings"] == ["missing_target", "other_warning"]

def test_normalize_and_evidence_accept_targetless_navigate(tmp_path: Path):
    row = _row(
        190,
        source="chrome",
        kind="navigate",
        payload={
            "browser": {
                "url": "https://example.test/start",
                "origin": "https://example.test",
                "title": "Start",
                "frame_id": "frame-1",
                "top_url": "https://example.test/start",
            },
            "effect": {"after_url": "https://example.test/next"},
        },
    )
    normalized = normalize_trace_row(row)
    assert "missing_target" not in normalized["quality"]["warnings"]
    recording_dir = _write_recording(tmp_path, instructions="Navigate", rows=[row])
    evidence.build_evidence_pack(recording_dir)


def test_normalized_missing_replay_data_is_a_nonblocking_capture_gap(tmp_path: Path):
    row = _row(
        191,
        source="chrome",
        kind="type",
        payload={"target": {"selector": "input[name=email]"}, "input": {}},
    )
    normalized = normalize_trace_row(row)
    assert normalized["type"] == "evidence"
    assert "missing_replay_data" in normalized["quality"]["warnings"]
    recording_dir = _write_recording(tmp_path, instructions="Type", rows=[row])
    pack = evidence.build_evidence_pack(recording_dir)
    assert pack["quality"]["blocking"] == [{"action_id": 191, "warnings": ["missing_replay_data"]}]

def test_evidence_capsule_accepts_targetless_chrome_document_scroll():
    action = normalize_trace_row(
        _row(
            234,
            source="chrome",
            kind="scroll",
            payload={
                "browser": {"url": "https://example.test/page", "origin": "https://example.test"},
                "input": {"scroll_x": 0, "scroll_y": 240},
            },
        )
    )

    capsule = evidence._action_capsule(
        action,
        trace=[],
        raw_input=[],
        raw_context=[],
        surfaces=[],
        frames=[],
    )

    assert "missing_target" not in capsule["warnings"]


def test_evidence_capsule_does_not_promote_unrelated_nearby_rows_to_proof():
    action = normalize_trace_row(
        _row(
            235,
            source="native",
            kind="click",
            payload={
                "app": {"bundle_id": "com.acme.primary"},
                "target": {"role": "AXButton", "label": "Run"},
                "input": {"x": 10, "y": 20},
            },
        )
    )
    action["ts"] = "2026-07-12T12:00:01Z"
    unrelated_raw = {
        "seq": 9,
        "ts": "2026-07-12T12:00:01.100Z",
        "payload": {"app": {"bundle_id": "com.acme.other"}, "target": {"label": "Unrelated"}},
    }
    unrelated_frame = {
        "seq": 10,
        "ts": "2026-07-12T12:00:01.200Z",
        "payload": {"app": {"bundle_id": "com.acme.other"}, "path": "visual/active.mp4"},
    }

    capsule = evidence._action_capsule(
        action,
        trace=[],
        raw_input=[unrelated_raw],
        raw_context=[],
        surfaces=[],
        frames=[unrelated_frame],
    )

    assert capsule["raw_source"] == {}
    assert capsule["raw_source_attribution"] == "unattributed"
    assert capsule["nearby_raw_diagnostic"] == {"count": 1, "first_seq": 9, "last_seq": 9}
    assert capsule["visual"] == []
    assert capsule["visual_attribution"] == "unattributed"
    assert capsule["nearby_visual_diagnostic"] == [unrelated_frame]
    assert {"missing_raw_input", "missing_active_surface_visual"} <= set(capsule["warnings"])


def test_evidence_capsule_keeps_uncorrelated_nearby_effect_as_diagnostic():
    action = normalize_trace_row(
        _row(
            236,
            source="native",
            kind="click",
            payload={
                "app": {"bundle_id": "com.acme.primary"},
                "meta": {"correlation_id": "effect-236"},
                "target": {"role": "AXButton", "label": "Run"},
                "input": {"x": 10, "y": 20},
            },
        )
    )
    action["ts"] = "2026-07-12T12:00:01Z"
    nearby_effect = {
        "id": 237,
        "ts": "2026-07-12T12:00:01.200Z",
        "type": "evidence",
        "source": "native",
        "kind": "window.changed",
        "payload": {
            "app": {"bundle_id": "com.acme.primary"},
            "effect": {"status": "complete"},
        },
    }
    causal_effect = {
        "id": 238,
        "ts": "2026-07-12T12:00:01.300Z",
        "type": "evidence",
        "source": "native",
        "kind": "screenshot",
        "payload": {
            "meta": {"after_action_id": "effect-236"},
            "effect": {"artifact_path": "artifacts/after-run.png"},
        },
    }

    capsule = evidence._action_capsule(
        action,
        trace=[nearby_effect, causal_effect],
        raw_input=[],
        raw_context=[],
        surfaces=[],
        frames=[],
    )

    assert capsule["effects"] == [{
        "trace_id": 238,
        "kind": "screenshot",
        "timestamp": "2026-07-12T12:00:01.300Z",
        "app": {},
        "window": {},
        "effect": {"artifact_path": "artifacts/after-run.png"},
        "attribution": "after_action_id",
        "causal": True,
    }]
    assert capsule["nearby_effect_diagnostic"] == [{
        "trace_id": 237,
        "kind": "window.changed",
        "timestamp": "2026-07-12T12:00:01.200Z",
        "app": {"bundle_id": "com.acme.primary"},
        "window": {},
        "effect": {"status": "complete"},
        "attribution": "nearby_identity",
        "causal": False,
    }]


def test_outcome_evidence_is_drafting_context_not_capture_integrity():
    mutation = {
        "id": "notes-mutation",
        "timestamp": "2026-07-12T12:00:01Z",
        "action": "paste",
        "target": {"role": "AXTextArea", "id": "Note Body Text View"},
        # A nearby frame from before the mutation and the mutation itself are
        # diagnostics, not proof that the requested note title exists.
        "visual": [{"ts": "2026-07-12T12:00:00.900Z"}],
        "visual_attribution": "surface_or_segment_identity",
        "effects": [],
        "verified_post_state": {},
        "warnings": [],
    }
    weak = evidence._summary([mutation])
    assert weak["outcome_evidence_count"] == 0
    weak_acceptance = evidence.capture_acceptance(
        {"capture_verdict": {"ready": True, "reasons": []}, "quality": {}, "summary": weak},
        manifest_sha256="a" * 64,
    )
    assert weak_acceptance["blockers"] == []
    assert weak_acceptance["summary"]["outcome_evidence_count"] == 0
    empty_acceptance = evidence.capture_acceptance(
        {"capture_verdict": {"ready": True, "reasons": []}, "quality": {}, "summary": {}},
        manifest_sha256="a" * 64,
    )
    assert empty_acceptance["blockers"] == []
    incomplete_acceptance = evidence.capture_acceptance(
        {
            "capture_verdict": {
                "ready": False,
                "reasons": [{"code": "missing_chrome_actions"}],
            },
            "quality": {},
            "summary": {
                "target_evidence_count": 1,
                "mutation_evidence_count": 1,
                "outcome_evidence_count": 1,
            },
        },
        manifest_sha256="a" * 64,
    )
    assert incomplete_acceptance["blockers"] == []
    assert incomplete_acceptance["summary"]["outcome_evidence_count"] == 1

    # Preserve compatibility with already-persisted acceptance JSON without
    # letting historical semantic blockers force the user to demonstrate the
    # same workflow again.
    legacy_semantic_acceptance = {
        **weak_acceptance,
        "blockers": [{
            "code": "missing_success_evidence",
            "message": "No post-save receipt was correlated.",
            "resolution": "rerecord",
        }],
    }
    assert evidence.capture_acceptance_blockers(legacy_semantic_acceptance) == []
    integrity_failure = evidence._capture_acceptance_failure(
        "capture_acceptance_invalid",
        "Hermes could not verify the saved recording.",
    )
    assert evidence.capture_acceptance_blockers(integrity_failure) == [{
        "code": "capture_acceptance_invalid",
        "message": "Hermes could not verify the saved recording.",
        "resolution": "rerecord",
    }]

    observed = {
        **mutation,
        "verified_post_state": {
            "source": "accessibility_observation",
            "causal": True,
            "app": {"bundle_id": "com.apple.Notes"},
            "values": ["Customer Follow-up\nThe requested summary from example.com"],
        },
    }
    verified = evidence._summary([observed])
    assert verified["outcome_evidence_count"] == 1

    native_mutation = {
        "id": 563,
        "ts": "2026-07-12T12:00:01.000Z",
        "type": "action",
        "source": "native",
        "kind": "type",
        "payload": {
            "app": {"bundle_id": "com.apple.Notes"},
            "target": {"id": "Note Body Text View", "ax": {"identifier": "Note Body Text View"}},
            "input": {"text": "Customer Follow-up"},
        },
    }
    ax_receipt = {
        "id": 564,
        "ts": "2026-07-12T12:00:01.211Z",
        "type": "action",
        "source": "native",
        "kind": "click",
        "payload": {
            "app": {"bundle_id": "com.apple.Notes"},
            "target": {
                "id": "Note Body Text View",
                "ax": {
                    "identifier": "Note Body Text View",
                    "value_preview": "Customer Follow-up\nSummary\nhttps://example.com",
                },
            },
        },
    }
    receipt = evidence._verified_post_state(native_mutation, trace=[native_mutation, ax_receipt])
    assert receipt["source"] == "accessibility_post_state_snapshot"
    assert receipt["receipt_trace_id"] == 564
    assert receipt["latency_ms"] == 211
    assert evidence._summary([{**mutation, "verified_post_state": receipt}])["outcome_evidence_count"] == 1

    for invalid in (
        {**ax_receipt, "ts": "2026-07-12T12:00:00.999Z"},
        {**ax_receipt, "ts": "2026-07-12T12:00:02.001Z"},
        {**ax_receipt, "payload": {**ax_receipt["payload"], "app": {"bundle_id": "com.apple.Spotlight"}}},
        {**ax_receipt, "payload": {**ax_receipt["payload"], "target": {"id": "Other Text View", "ax": {"identifier": "Other Text View", "value_preview": "Customer Follow-up\nhttps://example.com"}}}},
    ):
        assert evidence._verified_post_state(native_mutation, trace=[native_mutation, invalid]) == {}

    intervening = {**native_mutation, "id": 564, "ts": "2026-07-12T12:00:01.100Z"}
    later_receipt = {**ax_receipt, "id": 565, "ts": "2026-07-12T12:00:01.211Z"}
    assert evidence._verified_post_state(native_mutation, trace=[native_mutation, intervening, later_receipt]) == {}



def test_target_and_replay_gaps_preserve_draftability_and_mark_capture_incomplete():
    quality = evidence._quality(
        [
            {"id": "target-gap", "warnings": ["missing_target"]},
            {"id": "replay-gap", "warnings": ["missing_replay_data"]},
        ],
        {},
    )

    assert quality["reasons"] == []
    assert {item["action_id"] for item in quality["blocking"]} == {"target-gap", "replay-gap"}

def _write_recording(
    tmp_path: Path,
    *,
    instructions: str,
    rows: list[dict],
    schema_version: str = LEGACY_SCHEMA_VERSION,
) -> Path:
    root = tmp_path / "home" / "record" / "recordings" / "customer-workflow" / "rec-1"
    root.mkdir(parents=True)
    metadata = {
        "schemaVersion": schema_version,
        "workflow_id": "customer-workflow",
        "state": "stopped",
        "status": "stopped",
        "active": False,
        "eventCount": len(rows),
        "instructions": instructions,
        "paths": {"trace": "trace.jsonl", "artifacts": "artifacts/"},
    }
    (root / "metadata.json").write_text(json.dumps(metadata) + "\n", encoding="utf-8")
    (root / "session.json").write_text(
        json.dumps({"schemaVersion": schema_version, "status": "stopped"}) + "\n",
        encoding="utf-8",
    )
    (root / "trace.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    return root


def _captured_customer_actions() -> list[dict]:
    return [
        _row(
            1,
            source="chrome",
            kind="click",
            payload={
                "app": {"name": "Google Chrome", "bundle_id": "com.google.Chrome"},
                "browser": {
                    "url": "https://mail.example.test/inbox",
                    "origin": "https://mail.example.test",
                    "title": "Inbox",
                },
                "target": {"selector": "button.archive", "label": "Archive"},
                "input": {"button": "left"},
            },
        ),
        _row(
            2,
            source="native",
            kind="paste",
            payload={
                "app": {"name": "Notes", "bundle_id": "com.apple.Notes"},
                "window": {"title": "Customer Notes"},
                "target": {"role": "AXTextArea", "label": "Private note", "secure_input": True},
                "input": {"secure_input": True, "redacted": True},
            },
        ),
    ]


def _draft_plan() -> dict:
    return {
        "steps": [
            "Archive the demonstrated customer message.",
            "Verify it is no longer in the inbox.",
        ],
        "job_spec": {
            "name": "Profile-owned tools",
            "schedule": "0 9 * * *",
            "deliver": "local",
            "no_agent": False,
            "prompt": "Archive the demonstrated message and verify the result.",
            "skills": [],
        },
        "script_source": None,
        "trigger": {
            "requested": False,
            "feasibility": "not_requested",
            "description": "",
            "source": None,
            "events": [],
            "filters": None,
            "reason": "No event trigger was requested.",
        },
        "warnings": [],
    }


def test_selected_profile_keeps_its_full_cron_tool_configuration(tmp_path):
    plan = _draft_plan()
    plan["job_spec"]["enabled_toolsets"] = []

    normalized = compiler.validate_draft_plan(plan)
    workflow = {
        **normalized,
        "id": "profile-owned-tools",
        "name": "Profile-owned tools",
        "schedule": "0 9 * * *",
    }
    compiled = compiler.compile_workflow(
        workflow,
        tmp_path / "profile",
        tmp_path / "build",
    )

    assert "enabled_toolsets" not in compiled.payload
    assert "enabled_toolsets" not in hermes._cron_tool_arguments(
        "create",
        compiled.payload,
    )

    plan["job_spec"]["enabled_toolsets"] = ["browser"]
    with pytest.raises(
        compiler.CompileError,
        match="enabled_toolsets.*at most 0 items",
    ):
        compiler.validate_draft_plan(plan)


def test_approval_loads_the_sealed_job_without_recompiling(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    recording_dir = home / "record" / "recordings" / "workflow" / "session"
    recording_dir.mkdir(parents=True)
    workflow = {
        "id": "workflow",
        "name": "Profile-owned tools",
        "instructions": "Archive the message and verify it.",
        **compiler.validate_draft_plan(_draft_plan()),
        "review": {"draft_revision": 1},
    }
    modules = record_recorder.load_record_modules()
    prepared = record_recorder.prepare_recording_build(
        modules,
        home=home,
        profile_name="default",
        recording_dir=recording_dir,
        workflow=workflow,
    )
    workflow["build"] = {
        "build_dir": prepared["build_dir"],
        "review_envelope_sha256": prepared["review_envelope_sha256"],
    }
    write_json(recording_dir / "workflow.json", workflow)
    digest = record_recorder.workflow_digest(workflow)

    monkeypatch.setattr(
        modules["compiler"],
        "compile_workflow",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("approval must not compile")
        ),
    )
    loaded = record_recorder.load_recording_build(
        modules,
        home=home,
        profile_name="default",
        recording_dir=recording_dir,
        expected_draft_digest=digest,
        schedule="0 9 * * *",
        run_mode="schedule",
    )

    assert loaded["payload"] == {"action": "create", **workflow["job_spec"]}


def test_drafting_surfaces_warning_when_required_chrome_actions_were_not_captured(tmp_path):
    instructions = "In Chrome at https://mail.example.test, archive the demonstrated message."
    recording_dir = _write_recording(
        tmp_path,
        instructions=instructions,
        rows=[_captured_customer_actions()[1]],
    )

    workflow = recording.build_draft_candidate(
        recording_dir,
        output=recording_dir / "workflow.json",
        name="Archive Customer Message",
        instructions=instructions,
        workflow_author=lambda _context: _draft_plan(),
    ).workflow

    capture_warnings = [
        warning for warning in workflow["review"]["recording_warnings"]
        if warning.get("kind") == "capture_quality_gap"
    ]
    assert any(warning.get("code") == "browser_action_evidence_gap" for warning in capture_warnings)


def test_drafting_surfaces_warning_when_required_desktop_output_was_not_captured(tmp_path):
    output_path = tmp_path / "customer-report.txt"
    instructions = f"Create and save the report at {output_path}."
    recording_dir = _write_recording(
        tmp_path,
        instructions=instructions,
        rows=[_captured_customer_actions()[0]],
    )

    workflow = recording.build_draft_candidate(
        recording_dir,
        output=recording_dir / "workflow.json",
        name="Create Customer Report",
        instructions=instructions,
        workflow_author=lambda _context: _draft_plan(),
    ).workflow

    capture_warnings = [
        warning for warning in workflow["review"]["recording_warnings"]
        if warning.get("kind") == "capture_quality_gap"
    ]
    assert any(warning.get("code") == "desktop_output_evidence_gap" for warning in capture_warnings)


def test_v2_bundle_remains_draftable_without_v3_raw_evidence(tmp_path):
    recording_dir = _write_recording(
        tmp_path,
        instructions="Archive the demonstrated message in Chrome.",
        rows=[_captured_customer_actions()[0]],
    )

    workflow = recording.build_draft_candidate(
        recording_dir,
        output=recording_dir / "workflow.json",
        name="Archive message",
        instructions="Archive the demonstrated message in Chrome.",
        workflow_author=lambda _context: _draft_plan(),
    ).workflow

    assert workflow["name"] == "Archive message"


def test_empty_but_readable_stopped_bundle_creates_best_effort_draft(tmp_path):
    recording_dir = _write_recording(
        tmp_path,
        instructions="Prepare the requested weekly summary.",
        rows=[],
        schema_version=SCHEMA_VERSION,
    )

    workflow = recording.build_draft_candidate(
        recording_dir,
        output=recording_dir / "workflow.json",
        name="Weekly summary",
        instructions="Prepare the requested weekly summary.",
        workflow_author=lambda _context: _draft_plan(),
    ).workflow

    assert workflow["name"] == "Weekly summary"
    assert workflow["review"]["capture_acceptance"]["blockers"]
    assert "capture_quality" not in workflow["review"]
    assert any(
        warning.get("code") == "no_meaningful_actions"
        for warning in workflow["review"]["recording_warnings"]
    )


def test_unreadable_derived_evidence_index_falls_back_to_trace_and_instructions(tmp_path, monkeypatch):
    recording_dir = _write_recording(
        tmp_path,
        instructions="Prepare the requested weekly summary.",
        rows=[_captured_customer_actions()[0]],
        schema_version=SCHEMA_VERSION,
    )
    monkeypatch.setattr(
        recording,
        "load_evidence_pack",
        lambda _root: (_ for _ in ()).throw(ValueError("derived index unavailable")),
    )

    workflow = recording.build_draft_candidate(
        recording_dir,
        output=recording_dir / "workflow.json",
        name="Weekly summary",
        instructions="Prepare the requested weekly summary.",
        workflow_author=lambda _context: _draft_plan(),
    ).workflow

    assert workflow["name"] == "Weekly summary"
    assert any(
        warning.get("code") == "evidence_index_unavailable"
        for warning in workflow["review"]["recording_warnings"]
    )


def test_authoring_failure_propagates_without_fabricating_a_workflow(tmp_path):
    recording_dir = _write_recording(
        tmp_path,
        instructions="Copy example.com into a new Notes note.",
        rows=_captured_customer_actions(),
    )

    def fail_authoring(_context):
        raise RuntimeError("internal compiler diagnostic")

    with pytest.raises(RuntimeError, match="internal compiler diagnostic"):
        recording.build_draft_candidate(
            recording_dir,
            output=recording_dir / "workflow.json",
            name="Example to Notes",
            instructions="Copy example.com into a new Notes note.",
            workflow_author=fail_authoring,
        )

    assert not (recording_dir / "workflow.json").exists()


def test_agent_authored_steps_reach_the_review_unchanged(tmp_path):
    recording_dir = _write_recording(
        tmp_path,
        instructions=(
            "Get the latest Weng Techno video and add its URL to Reminders."
        ),
        rows=_captured_customer_actions(),
    )
    authored = _draft_plan()
    authored["steps"] = [
        "Get the latest video from the Weng Techno YouTube channel.",
        "Add the video URL to Reminders to listen to later.",
    ]
    workflow = recording.build_draft_candidate(
        recording_dir,
        output=recording_dir / "workflow.json",
        name="Weng Techno listen later",
        instructions="Get the latest video and add it to Reminders.",
        workflow_author=lambda _context: authored,
    ).workflow

    assert workflow["review"]["execution_steps"] == authored["steps"]



def test_modern_capture_marked_evidence_unready_still_drafts_with_quality_metadata(tmp_path):
    recording_dir = _write_recording(
        tmp_path,
        instructions="Type the customer note.",
        schema_version=SCHEMA_VERSION,
        rows=[
            _row(
                1,
                source="native",
                kind="type",
                payload={"app": {"name": "Notes"}, "input": {}},
            )
        ],
    )
    workflow = recording.build_draft_candidate(
        recording_dir,
        output=recording_dir / "workflow.json",
        name="Type customer note",
        instructions="Type the customer note.",
        workflow_author=lambda _context: _draft_plan(),
    ).workflow

    assert workflow["review"]["capture_acceptance"]["blockers"]

def test_v3_bundle_drafts_when_required_native_evidence_is_absent(tmp_path):
    recording_dir = _write_recording(
        tmp_path,
        instructions="Click the active Notes button.",
        schema_version=SCHEMA_VERSION,
        rows=[
            _row(
                1,
                source="native",
                kind="click",
                payload={
                    "app": {"name": "Notes", "bundle_id": "com.apple.Notes"},
                    "window": {"title": "Customer notes"},
                    "target": {"role": "AXButton", "label": "Done"},
                    "input": {"x": 100, "y": 200},
                },
            )
        ],
    )

    workflow = recording.build_draft_candidate(
        recording_dir,
        output=recording_dir / "workflow.json",
        name="Notes action",
        instructions="Click the active Notes button.",
        workflow_author=lambda _context: _draft_plan(),
    ).workflow

    assert workflow["review"]["capture_acceptance"]["blockers"]
    assert workflow["review"]["recording_warnings"]


def test_v3_evidence_pack_correlates_native_source_rows_and_seals(tmp_path):
    timestamp = "2026-07-12T12:00:01Z"
    recording_dir = _write_recording(
        tmp_path,
        instructions="Click Done in Notes.",
        schema_version=SCHEMA_VERSION,
        rows=[
            _row(
                1,
                source="native",
                kind="click",
                payload={
                    "app": {"name": "Notes", "bundle_id": "com.apple.Notes"},
                    "window": {"title": "Customer notes"},
                    "target": {"role": "AXButton", "label": "Done"},
                    "input": {"x": 100, "y": 200},
                },
            )
        ],
    )
    (recording_dir / "raw").mkdir()
    (recording_dir / "visual").mkdir()
    (recording_dir / "raw" / "input.jsonl").write_text(
        json.dumps({"seq": 7, "ts": timestamp, "kind": "leftMouseUp", "payload": {"x": 100, "y": 200}}) + "\n",
        encoding="utf-8",
    )
    (recording_dir / "raw" / "context.jsonl").write_text("", encoding="utf-8")
    (recording_dir / "raw" / "active-surface.jsonl").write_text(
        json.dumps({"seq": 8, "ts": timestamp, "kind": "active_surface.changed", "payload": {"app": {"bundle_id": "com.apple.Notes"}, "window": {"title": "Customer notes"}}}) + "\n",
        encoding="utf-8",
    )
    (recording_dir / "visual" / "frames.jsonl").write_text(
        json.dumps({"seq": 9, "ts": timestamp, "kind": "visual.frame", "payload": {"path": "visual/active.mp4"}}) + "\n",
        encoding="utf-8",
    )
    (recording_dir / "visual" / "active.mp4").write_bytes(b"test-video")
    continuity_path = recording_dir / "chrome-continuity.json"
    continuity_path.write_text('{"status":"complete"}\n', encoding="utf-8")

    pack = evidence.build_evidence_pack(recording_dir)
    completeness = evidence.capture_completeness(recording_dir, pack)
    manifest = evidence.seal_capture(recording_dir, evidence_pack=pack)

    assert completeness == {
        "ready": True,
        "reasons": [],
        "frame_count": 1,
        "media_paths": ["visual/active.mp4"],
    }
    assert pack["actions"][0]["raw_source"] == {"count": 1, "first_seq": 7, "last_seq": 7}
    assert pack["actions"][0]["surface"]["app"]["bundle_id"] == "com.apple.Notes"
    assert manifest["schemaVersion"] == evidence.CAPTURE_MANIFEST_SCHEMA
    assert all(item["path"] not in {"metadata.json", "session.json"} for item in manifest["files"])
    assert all(item["path"] != "chrome-continuity.json" for item in manifest["files"])
    assert any(item["path"] == "visual/active.mp4" for item in manifest["files"])
    assert (recording_dir / "capture-manifest.json").is_file()

    # Historical v2 seals bound the now-retired continuity sidecar. They remain
    # valid, while current sealing deliberately leaves the stale file unsealed.
    manifest["files"].append({
        "path": "chrome-continuity.json",
        "bytes": continuity_path.stat().st_size,
        "sha256": hashlib.sha256(continuity_path.read_bytes()).hexdigest(),
    })
    (recording_dir / "capture-manifest.json").write_text(
        json.dumps(manifest, sort_keys=True),
        encoding="utf-8",
    )

    workflow = recording.build_draft_candidate(
        recording_dir,
        output=recording_dir / "workflow.json",
        name="Notes action",
        instructions="Click Done in Notes.",
        workflow_author=lambda _context: _draft_plan(),
    ).workflow
    assert workflow["name"] == "Notes action"
    acceptance = evidence.verified_capture_acceptance(recording_dir)
    assert acceptance["blockers"] == []
    assert acceptance["summary"]["mutation_evidence_count"] == 0
    assert acceptance["summary"]["outcome_evidence_count"] == 0
    assert compiler.review_blockers(workflow) == []

    # Immutable evidence still fails closed even though lifecycle projections
    # are intentionally outside the source boundary.
    (recording_dir / "trace.jsonl").write_text("{}\n", encoding="utf-8")
    acceptance = evidence.verified_capture_acceptance(recording_dir)
    assert acceptance["blockers"][0]["code"] == "capture_acceptance_invalid"
    assert "capture_manifest_file_mismatch:trace.jsonl" in acceptance["blockers"][0]["details"]


def test_capture_completeness_requires_media_referenced_by_frame_index(tmp_path):
    recording_dir = _write_recording(tmp_path, instructions="Inspect evidence.", rows=[])
    (recording_dir / "raw").mkdir()
    (recording_dir / "visual").mkdir()
    (recording_dir / "raw" / "input.jsonl").write_text("{}\n", encoding="utf-8")
    (recording_dir / "visual" / "frames.jsonl").write_text(
        json.dumps({"seq": 1, "payload": {"path": "visual/renamed-by-producer.mp4"}}) + "\n",
        encoding="utf-8",
    )
    # An unrelated video must not satisfy a broken reference.
    (recording_dir / "visual" / "active-1.mp4").write_bytes(b"orphan-video")
    pack = evidence.build_evidence_pack(recording_dir)

    completeness = evidence.capture_completeness(recording_dir, pack)

    assert completeness["ready"] is False
    assert completeness["media_paths"] == ["visual/renamed-by-producer.mp4"]
    assert completeness["reasons"] == [
        {"code": "no_meaningful_actions"},
        {"code": "missing_referenced_media", "path": "visual/renamed-by-producer.mp4"}
    ]


def test_v3_evidence_pack_does_not_block_for_a_late_visual_filter_error(tmp_path):
    recording_dir = _write_recording(
        tmp_path,
        instructions="Click Done in Notes.",
        schema_version=SCHEMA_VERSION,
        rows=[
            _row(
                1,
                source="native",
                kind="click",
                payload={"app": {"bundle_id": "com.apple.Notes"}, "target": {"label": "Done"}, "input": {"x": 1, "y": 1}},
            )
        ],
    )
    metadata = json.loads((recording_dir / "metadata.json").read_text(encoding="utf-8"))
    metadata["visualCaptureHealth"] = {"error": "active_window_not_shareable"}
    (recording_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    (recording_dir / "raw").mkdir()
    (recording_dir / "visual").mkdir()
    (recording_dir / "raw" / "input.jsonl").write_text(json.dumps({"seq": 1, "ts": "2026-07-12T12:00:01Z"}) + "\n", encoding="utf-8")
    (recording_dir / "visual" / "frames.jsonl").write_text(json.dumps({"seq": 2, "ts": "2026-07-12T12:00:01Z"}) + "\n", encoding="utf-8")

    pack = evidence.build_evidence_pack(recording_dir)

    assert pack["quality"]["warnings"] == ["active_surface_visual_error"]
