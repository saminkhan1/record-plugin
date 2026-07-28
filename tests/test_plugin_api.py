from __future__ import annotations

import hashlib
import json
import multiprocessing
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import crunch_recorder
import recording_bundle
from hermes_crunch import (
    approval,
    authorization,
    compiler,
    evidence,
    hermes,
    recording,
)

DASHBOARD_ROOT = PLUGIN_ROOT / "dashboard"
if str(DASHBOARD_ROOT) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_ROOT))
import plugin_api as dashboard_api

_CAPTURE_AUTHORIZATION = "a" * 64
_CAPTURE_DASHBOARD_ORIGIN = "http://127.0.0.1:9119"


def _hold_crunch_install_lock(
    home: str,
    build_dir: str,
    ready,
    release,
) -> None:
    with crunch_recorder._crunch_install_lock(
        home=Path(home),
        build_dir=Path(build_dir),
    ):
        ready.set()
        if not release.wait(timeout=5):
            raise RuntimeError("Timed out waiting to release the install lock")


def _capture_request(
    *,
    origin: str | None = _CAPTURE_DASHBOARD_ORIGIN,
    client_host: str = "127.0.0.1",
) -> Request:
    headers = [(b"host", b"127.0.0.1:9119")]
    if origin is not None:
        headers.extend(
            [
                (b"origin", origin.encode("ascii")),
                (b"sec-fetch-site", b"same-origin"),
            ]
        )
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/plugins/hermes-crunch/recordings/start",
            "raw_path": b"/api/plugins/hermes-crunch/recordings/start",
            "query_string": b"",
            "headers": headers,
            "server": ("127.0.0.1", 9119),
            "client": (client_host, 49152),
        }
    )


def _workflow():
    return {
        "id": "example",
        "name": "Example",
        "instructions": "Create the verified result.",
        "execution": {
            "no_agent": False,
            "script": None,
            "prompt": "Create the result and verify it.",
            "skills": [],
            "enabled_toolsets": ["file"],
            "requirements": {"commands": [], "network_origins": [], "apps": [], "files": []},
            "authorization": {
                "mode": "human_in_loop",
                "effects": [],
                "operations": [],
                "checkpoints": [],
            },
        },
        "review": {"warnings": []},
    }


def _create_authorization() -> dict:
    return {
        "mode": "human_in_loop",
        "effects": [],
        "operations": [
            {
                "id": "create-result",
                "effect": "create",
                "description": "Create the reviewed result.",
                "tool": "write_file",
                "arguments": {"field": "path", "equals": "result.txt"},
            }
        ],
        "checkpoints": [],
        "target": "origin",
    }


def _compiled_review_build(tmp_path: Path) -> tuple[Path, Path, dict]:
    home = tmp_path / "home"
    recording_dir = tmp_path / "recording"
    recording_dir.mkdir()
    evidence_pack = {
        "session_id": "recording",
        "capture_verdict": {"ready": True, "reasons": []},
        "quality": {},
        "summary": {
            "action_count": 1,
            "target_evidence_count": 1,
            "mutation_evidence_count": 1,
            "outcome_evidence_count": 1,
        },
    }
    (recording_dir / "evidence-pack.json").write_text(
        json.dumps(evidence_pack), encoding="utf-8"
    )
    evidence.seal_capture(recording_dir, evidence_pack=evidence_pack)
    workflow = _workflow()
    workflow["review"].update(
        {"source_digest": "a" * 64, "draft_revision": 4}
    )
    workflow_path = recording_dir / "workflow.json"
    workflow_path.write_text(json.dumps(workflow), encoding="utf-8")
    result = crunch_recorder.compile_reviewed_recording(
        {"approval": approval, "compiler": compiler},
        home=home,
        profile_name="default",
        recording_dir=recording_dir,
        schedule="every 30m",
        expected_draft_digest=crunch_recorder.workflow_digest(workflow),
    )
    return home, Path(result["build_dir"]), result


def _set_install_runtime_ready(monkeypatch) -> None:
    route = {
        "platform": "telegram",
        "chat_id": "approval-room",
        "thread_id": None,
    }
    monkeypatch.setattr(
        crunch_recorder,
        "_configured_approval_target",
        lambda **_kwargs: ("telegram", dict(route)),
    )
    monkeypatch.setattr(
        crunch_recorder,
        "_webhook_install_status",
        lambda **_kwargs: {
            "configured": True,
            "ready": True,
            "base_url": "https://hooks.example.test",
            "reason": "",
        },
    )
    monkeypatch.setattr(
        crunch_recorder,
        "_authorization_scheduler_status",
        lambda **_kwargs: {
            "ready": True,
            "provider": "builtin",
            "gateway_running": True,
            "gateway_source": "test",
            "reason": "",
        },
    )
    monkeypatch.setattr(
        authorization,
        "_approval_platform_is_capable",
        lambda _platform: True,
    )


def test_recording_start_passes_capture_authorization_to_native_boundary(
    tmp_path,
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        dashboard_api,
        "_cron_profile_home",
        lambda _profile: ("default", tmp_path),
    )
    monkeypatch.setattr(
        dashboard_api,
        "start_native_recording",
        lambda **kwargs: calls.append(kwargs) or {"status": "recording"},
    )
    body = dashboard_api.CrunchRecordingStartRequest(
        name="Example",
        instructions="Record this workflow.",
        schedule="every 1h",
        capture_authorization_nonce=_CAPTURE_AUTHORIZATION,
        capture_dashboard_origin=_CAPTURE_DASHBOARD_ORIGIN,
    )

    assert dashboard_api.start_recording(body, _capture_request()) == {
        "status": "recording"
    }
    assert calls[0]["capture_authorization_nonce"] == _CAPTURE_AUTHORIZATION
    assert calls[0]["capture_dashboard_origin"] == _CAPTURE_DASHBOARD_ORIGIN


@pytest.mark.parametrize(
    ("request_origin", "claimed_origin", "client_host"),
    [
        (
            "http://127.0.0.1:54821",
            "http://127.0.0.1:54821",
            "127.0.0.1",
        ),
        (
            _CAPTURE_DASHBOARD_ORIGIN,
            "http://127.0.0.1:54821",
            "127.0.0.1",
        ),
        (None, _CAPTURE_DASHBOARD_ORIGIN, "127.0.0.1"),
        (_CAPTURE_DASHBOARD_ORIGIN, _CAPTURE_DASHBOARD_ORIGIN, "192.0.2.10"),
    ],
)
def test_recording_start_rejects_forged_dashboard_origin_before_profile_access(
    monkeypatch,
    request_origin,
    claimed_origin,
    client_host,
):
    monkeypatch.setattr(
        dashboard_api,
        "_cron_profile_home",
        lambda _profile: (_ for _ in ()).throw(
            AssertionError("profile must not be accessed before origin validation")
        ),
    )
    body = dashboard_api.CrunchRecordingStartRequest(
        name="Example",
        instructions="Record this workflow.",
        schedule="every 1h",
        capture_authorization_nonce=_CAPTURE_AUTHORIZATION,
        capture_dashboard_origin=claimed_origin,
    )

    with pytest.raises(HTTPException) as exc:
        dashboard_api.start_recording(
            body,
            _capture_request(origin=request_origin, client_host=client_host),
        )

    assert exc.value.status_code == 403


def test_recording_start_rejects_host_port_not_bound_socket_before_profile_access(
    monkeypatch,
):
    monkeypatch.setattr(
        dashboard_api,
        "_cron_profile_home",
        lambda _profile: (_ for _ in ()).throw(
            AssertionError("profile must not be accessed before origin validation")
        ),
    )
    forged_origin = "http://127.0.0.1:54821"
    body = dashboard_api.CrunchRecordingStartRequest(
        name="Example",
        instructions="Record this workflow.",
        schedule="every 1h",
        capture_authorization_nonce=_CAPTURE_AUTHORIZATION,
        capture_dashboard_origin=forged_origin,
    )
    request = _capture_request(origin=forged_origin)
    request.scope["headers"] = [
        (b"host", b"127.0.0.1:54821"),
        (b"origin", forged_origin.encode("ascii")),
        (b"sec-fetch-site", b"same-origin"),
    ]

    with pytest.raises(HTTPException) as exc:
        dashboard_api.start_recording(body, request)

    assert exc.value.status_code == 403


def test_default_approval_target_fails_closed_without_a_configured_route(tmp_path):
    with pytest.raises(HTTPException) as exc:
        dashboard_api._default_approval_target(home=tmp_path)

    assert exc.value.status_code == 409
    assert "approval-capable channel" in str(exc.value.detail)




def test_schedule_keeps_approval_route_separate_from_result_delivery(
    tmp_path, monkeypatch
):
    _set_install_runtime_ready(monkeypatch)
    recording_dir = tmp_path / "recording"
    recording_dir.mkdir()
    workflow = _workflow()
    authorization = _create_authorization()
    workflow["execution"]["authorization"] = authorization
    (recording_dir / "workflow.json").write_text(
        json.dumps(workflow),
        encoding="utf-8",
    )
    digest = crunch_recorder.workflow_digest(workflow)
    captured = {}

    class FakeCompiler:
        @staticmethod
        def review_blockers(_workflow):
            return []

        @staticmethod
        def validate_authorization_selection(_execution, requested):
            return compiler.validate_authorization_selection(_execution, requested)

    def fake_compile(_modules, **kwargs):
        captured.update(kwargs)
        return {"approval_envelope_sha256": "a" * 64}

    monkeypatch.setattr(
        dashboard_api,
        "compile_reviewed_recording",
        fake_compile,
    )
    monkeypatch.setattr(
        crunch_recorder,
        "resolve_default_approval_target",
        lambda **_kwargs: (
            "telegram",
            {
                "platform": "telegram",
                "chat_id": "approval-room",
                "thread_id": None,
            },
        ),
    )

    result = dashboard_api._schedule_recording_locked(
        dashboard_api.CrunchScheduleRecordingRequest(
            profile="default",
            recording_dir=str(recording_dir),
            schedule="0 9 * * *",
            expected_draft_digest=digest,
            authorization={
                **_create_authorization(),
            },
        ),
        modules={"compiler": FakeCompiler()},
        profile_name="default",
        home=tmp_path / "home",
        recording_dir=recording_dir,
    )

    assert result["status"] == "awaiting_approval"
    assert captured["authorization_override"]["target"] == "telegram"
    assert captured["authorization_override"]["approval_route"] == {
        "platform": "telegram",
        "chat_id": "approval-room",
        "thread_id": None,
    }
    assert "delivery_override" not in captured

    captured.clear()
    disabled_authorization = _create_authorization()
    disabled_authorization["operations"][0]["enabled"] = False
    dashboard_api._schedule_recording_locked(
        dashboard_api.CrunchScheduleRecordingRequest(
            profile="default",
            recording_dir=str(recording_dir),
            schedule="0 9 * * *",
            expected_draft_digest=digest,
            authorization=disabled_authorization,
        ),
        modules={"compiler": FakeCompiler()},
        profile_name="default",
        home=tmp_path / "home",
        recording_dir=recording_dir,
    )
    assert "authorization_override" in captured
    assert (
        captured["authorization_override"]["operations"][0]["enabled"]
        is False
    )


def test_revision_context_preserves_human_check_identity():
    workflow = _workflow()
    workflow["review"] = {
        "execution_steps": ["Create the reminder.", "Create the note."],
        "approval_steps": [
            {"step": 1, "operation_id": "create-reminder", "effect": "create"},
            {"step": 2, "operation_id": "create-note", "effect": "create"},
        ],
    }

    context = dashboard_api._revision_context(
        workflow=workflow,
        draft_digest="digest",
        draft_revision=2,
        thread={"messages": [], "feedback_summary": "Keep both checks."},
    )

    assert context["prior_plan"]["approval_steps"] == workflow["review"]["approval_steps"]


def test_review_state_drops_only_obsolete_rerecord_threads():
    class ReviewCompiler:
        def __init__(self, blockers):
            self.blockers = blockers

        def review_blockers(self, _workflow):
            return self.blockers

        @staticmethod
        def effective_approval_boundaries(_workflow):
            return {"boundaries": []}

    workflow = _workflow()
    thread = {
        "thread_id": "old-rerecord-thread",
        "draft_digest": "draft-digest",
        "draft_revision": 1,
        "state": "needs_rerecord",
        "messages": [{
            "role": "assistant",
            "content": "Record the workflow again.",
        }],
    }
    conversation = {"threads": [thread]}

    recovered = dashboard_api._review_conversation_response(
        workflow=workflow,
        draft_digest="draft-digest",
        conversation=conversation,
        compiler=ReviewCompiler([{
            "kind": "reminders_destination_unverified",
            "message": "Choose the reminder list.",
            "resolution": "redraft",
        }]),
    )

    assert recovered["state"] == "needs_input"
    assert recovered["needs_initial_turn"] is True
    assert recovered["thread_id"] is None
    assert recovered["threads"] == []

    still_corrupt = dashboard_api._review_conversation_response(
        workflow=workflow,
        draft_digest="draft-digest",
        conversation=conversation,
        compiler=ReviewCompiler([{
            "kind": "capture_acceptance_invalid",
            "message": "The saved recording could not be verified.",
            "resolution": "rerecord",
        }]),
    )

    assert still_corrupt["state"] == "needs_rerecord"
    assert still_corrupt["needs_initial_turn"] is False
    assert still_corrupt["thread_id"] == "old-rerecord-thread"
    assert [item["thread_id"] for item in still_corrupt["threads"]] == [
        "old-rerecord-thread"
    ]


def test_compile_reviewed_recording_rejects_stale_review(tmp_path):
    recording_dir = tmp_path / "recording"
    recording_dir.mkdir()
    (recording_dir / "workflow.json").write_text(json.dumps(_workflow()), encoding="utf-8")

    with pytest.raises(HTTPException, match="reviewed draft changed"):
        crunch_recorder.compile_reviewed_recording(
            {"compiler": compiler},
            home=tmp_path / "home",
            profile_name="default",
            recording_dir=recording_dir,
            schedule="every 30m",
            expected_draft_digest="stale",
        )


def test_compile_choices_create_immutable_content_addressed_builds(tmp_path):
    recording_dir = tmp_path / "recording"
    recording_dir.mkdir()
    source_workflow = _workflow()
    source_workflow["review"].update(
        {"source_digest": "recording-digest", "draft_revision": 4}
    )
    workflow_path = recording_dir / "workflow.json"
    workflow_path.write_text(json.dumps(source_workflow), encoding="utf-8")
    reviewed_digest = crunch_recorder.workflow_digest(source_workflow)
    modules = {"approval": approval, "compiler": compiler}

    local = crunch_recorder.compile_reviewed_recording(
        modules,
        home=tmp_path / "home",
        profile_name="default",
        recording_dir=recording_dir,
        schedule="every 30m",
        expected_draft_digest=reviewed_digest,
        delivery_override="local",
    )
    protected = crunch_recorder.compile_reviewed_recording(
        modules,
        home=tmp_path / "home",
        profile_name="default",
        recording_dir=recording_dir,
        schedule="every 30m",
        expected_draft_digest=reviewed_digest,
        authorization_override={
            "mode": "human_in_loop",
            "effects": [],
            "target": "telegram",
            "operations": [
                {
                    "id": "write-result",
                    "effect": "create",
                    "description": "Write the reviewed result.",
                    "tool": "write_file",
                    "arguments": {
                        "field": "path",
                        "equals": "result.txt",
                    },
                }
            ],
            "checkpoints": [],
        },
        delivery_override="telegram",
    )

    assert local["build_dir"] != protected["build_dir"]
    assert local["payload"]["deliver"] == "local"
    assert local["payload"]["authorization"]["mode"] == "human_in_loop"
    assert local["payload"]["authorization"]["operations"] == []
    assert local["payload"]["authorization"]["script"] is None
    assert local["payload"]["authorization"]["execution_sha256"]
    assert protected["payload"]["deliver"] == "telegram"
    assert protected["payload"]["authorization"]["effects"] == []
    assert (
        protected["payload"]["authorization"]["operations"][0]["id"]
        == "write-result"
    )
    assert local["draft_digest"] == reviewed_digest
    assert protected["draft_digest"] == reviewed_digest
    for build in (local, protected):
        summary = json.loads(
            (Path(build["build_dir"]) / "compile-summary.json").read_text(
                encoding="utf-8"
            )
        )
        provenance = summary["provenance"]
        assert provenance["recording_id"] == recording_dir.name
        assert provenance["source_digest"] == "recording-digest"
        assert provenance["draft_digest"] == reviewed_digest
        assert provenance["draft_revision"] == 4
        assert provenance["build_id"] == Path(build["build_dir"]).name
        assert provenance["build_digest"] == crunch_recorder.workflow_digest(
            build["workflow"]
        )
        assert provenance["build_digest"] != provenance["draft_digest"]
    assert json.loads(workflow_path.read_text(encoding="utf-8")) == source_workflow


def test_compile_webhook_trigger_requires_supported_reviewed_trigger(tmp_path):
    recording_dir = tmp_path / "recording"
    recording_dir.mkdir()
    source_workflow = _workflow()
    source_workflow["trigger"] = {
        "requested": True,
        "feasibility": "supported",
        "description": "When a GitHub issue opens",
        "source": "GitHub",
        "events": ["issues"],
        "filters": [{"field": "action", "equals": "opened"}],
        "reason": "GitHub provides a compatible signed webhook.",
    }
    path = recording_dir / "workflow.json"
    path.write_text(json.dumps(source_workflow), encoding="utf-8")

    result = crunch_recorder.compile_reviewed_recording(
        {"approval": approval, "compiler": compiler},
        home=tmp_path / "home",
        profile_name="default",
        recording_dir=recording_dir,
        schedule="",
        run_mode="webhook",
        expected_draft_digest=crunch_recorder.workflow_digest(source_workflow),
    )

    assert result["payload"]["schedule"] == "triggered"
    assert result["workflow"]["run_mode"] == "webhook"

    source_workflow["trigger"]["feasibility"] = "unsupported"
    source_workflow["trigger"]["reason"] = "The local app cannot send a signed webhook."
    path.write_text(json.dumps(source_workflow), encoding="utf-8")
    with pytest.raises(HTTPException, match="This trigger cannot be created"):
        crunch_recorder.compile_reviewed_recording(
            {"compiler": compiler},
            home=tmp_path / "home",
            profile_name="default",
            recording_dir=recording_dir,
            schedule="",
            run_mode="webhook",
            expected_draft_digest=crunch_recorder.workflow_digest(source_workflow),
        )


@pytest.mark.asyncio
async def test_reviewed_crunch_draft_installs_and_runs_through_hermes_webhook(
    tmp_path,
    monkeypatch,
):
    import hmac

    from gateway.config import PlatformConfig
    import gateway.platforms.webhook as webhook_module
    from gateway.platforms.webhook import WebhookAdapter
    from cron.authorization import (
        register_authorization_provider,
        unregister_authorization_provider,
    )
    from hermes_cli.config import load_config, save_config
    from hermes_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )
    import tools.approval as approval_tools

    home = tmp_path / "home"
    recording_dir = _write_dashboard_recording(home)
    evidence_pack = {
        "session_id": recording_dir.name,
        "capture_verdict": {"ready": True, "reasons": []},
        "quality": {},
        "summary": {
            "action_count": 1,
            "target_evidence_count": 1,
            "mutation_evidence_count": 1,
            "outcome_evidence_count": 1,
        },
    }
    (recording_dir / "evidence-pack.json").write_text(
        json.dumps(evidence_pack),
        encoding="utf-8",
    )
    evidence.seal_capture(recording_dir, evidence_pack=evidence_pack)
    script_body = (
        "import json, sys\n"
        "event = json.load(sys.stdin)\n"
        "print(f\"handled issue {event['payload']['issue']['number']}\")\n"
    )
    plan = compiler.validate_draft_plan(
        {
            "selection_reason": "Use the reviewed native execution.",
            "state_strategy": "The event payload is the complete run state.",
            "idempotency_strategy": "Deduplicate by webhook delivery id.",
            "steps": ["Handle the matching issue and verify the result."],
            "approval_steps": [{
                "step": 1,
                "script_id": "execute-reviewed-script",
                "effect": "execute",
            }],
            "trigger": {
                "requested": True,
                "feasibility": "supported",
                "description": "When a GitHub issue is opened",
                "source": "GitHub",
                "events": ["issues"],
                "filters": [{"field": "action", "equals": "opened"}],
                "reason": "GitHub signs this event with X-Hub-Signature-256.",
            },
            "execution": {
                "no_agent": True,
                "script": {
                    "path": "issue-trigger.py",
                    "body": script_body,
                },
                "prompt": None,
                "skills": [],
                "enabled_toolsets": [],
                "requirements": {
                    "commands": [],
                    "network_origins": [],
                    "apps": [],
                    "files": [],
                },
                "authorization": {
                    "mode": "human_in_loop",
                    "effects": [],
                    "operations": [],
                    "checkpoints": [],
                    "script": {
                        "id": "execute-reviewed-script",
                        "effect": "execute",
                        "description": "Execute the exact reviewed script.",
                    },
                },
            },
            "warnings": [],
        },
        available_toolsets=set(),
    )
    workflow = {
        "id": "issue-workflow",
        "name": "Issue workflow",
        "instructions": "Handle each newly opened GitHub issue.",
        "execution": plan["execution"],
        "trigger": plan["trigger"],
        "review": {
            "warnings": plan["warnings"],
            "execution_steps": plan["steps"],
            "approval_steps": plan["approval_steps"],
            "source_digest": "a" * 64,
            "draft_revision": 1,
        },
    }
    workflow_path = recording_dir / "workflow.json"
    workflow_path.write_text(json.dumps(workflow), encoding="utf-8")
    draft_digest = crunch_recorder.workflow_digest(workflow)

    _set_install_runtime_ready(monkeypatch)
    monkeypatch.setattr(
        approval_tools,
        "request_fresh_gateway_approval",
        lambda *_args, **_kwargs: {
            "approved": True,
            "choice": "once",
            "outcome": "approved",
        },
    )
    monkeypatch.setattr(
        dashboard_api,
        "_cron_profile_home",
        lambda _profile: ("default", home),
    )
    provider_created = register_authorization_provider(
        authorization.PROVIDER_ID,
        is_available=authorization._authorization_available,
        scope=authorization._authorization_scope,
    )
    token = set_hermes_home_override(home)
    try:
        config = load_config()
        config.setdefault("platforms", {})["webhook"] = {
            "enabled": True,
            "extra": {
                "host": "127.0.0.1",
                "port": 8644,
                "public_url": "https://hooks.example.test",
            },
        }
        save_config(config)

        # The Hermes gateway already exists before Crunch installs this route.
        adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={
            "host": "127.0.0.1",
            "port": 0,
        }))
        adapter._reload_dynamic_routes()

        review = dashboard_api.schedule_recording(
            dashboard_api.CrunchScheduleRecordingRequest(
                profile="default",
                recording_dir=str(recording_dir),
                schedule="",
                run_mode="webhook",
                expected_draft_digest=draft_digest,
                authorization=workflow["execution"]["authorization"],
            )
        )
        assert review["status"] == "awaiting_approval"
        assert review["payload"]["schedule"] == "triggered"
        envelope = review["approval_envelope"]
        assert envelope["script_source"] == script_body
        assert envelope["script_sha256"] == hashlib.sha256(
            script_body.encode("utf-8")
        ).hexdigest()
        assert envelope["execution_sha256"] == (
            review["payload"]["authorization"]["execution_sha256"]
        )
        assert "## Exact Script Source" in review["review_markdown"]
        approval.verify_approval_envelope(Path(review["build_dir"]))

        approval_hash = review["approval_envelope_sha256"]
        install_request = dashboard_api.CrunchScheduleRecordingRequest(
            profile="default",
            recording_dir=str(recording_dir),
            schedule="",
            run_mode="webhook",
            expected_draft_digest=draft_digest,
            authorization=workflow["execution"]["authorization"],
            approve=True,
            expected_approval_envelope_sha256=approval_hash,
        )
        installed = dashboard_api.schedule_recording(install_request)
        retried = dashboard_api.schedule_recording(install_request)

        assert installed["status"] == "installed"
        assert installed["run_mode"] == "webhook"
        assert installed["webhook"]["url"] == (
            "http://127.0.0.1:8644/webhooks/"
            f"crunch-issue-workflow-{installed['job_id']}"
        )
        assert retried["job_id"] == installed["job_id"]
        assert retried["webhook"]["name"] == installed["webhook"]["name"]
        assert retried["webhook"]["url"] == installed["webhook"]["url"]
        assert retried["webhook"]["secret"] == installed["webhook"]["secret"]
        assert retried["webhook"]["idempotent_replay"] is True
        assert retried["install"]["install"]["idempotent_replay"] is True

        subscriptions_path = home / "webhook_subscriptions.json"
        subscriptions = json.loads(
            subscriptions_path.read_text(encoding="utf-8")
        )
        assert list(subscriptions) == [installed["webhook"]["name"]]
        route = subscriptions[installed["webhook"]["name"]]
        assert route["trigger_job_id"] == installed["job_id"]
        assert "job_home" not in route
        assert route["events"] == ["issues"]
        assert route["filters"] == {
            "all": [{"field": "action", "equals": "opened"}]
        }
        assert subscriptions_path.stat().st_mode & 0o777 == 0o600

        ignored_body = json.dumps(
            {"action": "closed", "issue": {"number": 18}}
        ).encode()
        ignored_signature = "sha256=" + hmac.new(
            installed["webhook"]["secret"].encode(),
            ignored_body,
            hashlib.sha256,
        ).hexdigest()
        body = json.dumps(
            {"action": "opened", "issue": {"number": 19}}
        ).encode()
        signature = "sha256=" + hmac.new(
            installed["webhook"]["secret"].encode(),
            body,
            hashlib.sha256,
        ).hexdigest()

        class FakeResponse:
            def __init__(self, payload, *, status=200):
                self.status = status
                self._payload = payload

            async def json(self):
                return self._payload

        class FakeRequest:
            def __init__(self, raw_body, headers):
                self.match_info = {
                    "route_name": installed["webhook"]["name"],
                }
                self.headers = headers
                self.content_length = len(raw_body)
                self._raw_body = raw_body

            async def read(self):
                return self._raw_body

        class FakeRequestTooLarge(Exception):
            pass

        monkeypatch.setattr(
            webhook_module,
            "web",
            types.SimpleNamespace(
                HTTPRequestEntityTooLarge=FakeRequestTooLarge,
                json_response=lambda payload, status=200: FakeResponse(
                    payload,
                    status=status,
                ),
            ),
        )

        async def deliver(raw_body, headers):
            return await adapter._handle_webhook(
                FakeRequest(raw_body, headers)
            )

        ignored = await deliver(
            ignored_body,
            {
                "Content-Type": "application/json",
                "X-GitHub-Event": "issues",
                "X-GitHub-Delivery": "delivery-18",
                "X-Hub-Signature-256": ignored_signature,
            },
        )
        assert ignored.status == 200
        assert await ignored.json() == {
            "status": "ignored",
            "reason": "filter",
            "route": installed["webhook"]["name"],
        }
        with hermes._profile_cron_scope(home):
            from cron.jobs import get_due_jobs

            assert get_due_jobs() == []

        headers = {
            "Content-Type": "application/json",
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": "delivery-19",
            "X-Hub-Signature-256": signature,
        }
        response = await deliver(body, headers)
        assert response.status == 202
        assert (await response.json())["status"] == "queued"
        duplicate = await deliver(body, headers)
        assert duplicate.status == 200
        assert (await duplicate.json())["status"] == "duplicate"

        with hermes._profile_cron_scope(home):
            from cron.jobs import get_job
            from cron.scheduler import run_one_job

            class ApprovalLoop:
                @staticmethod
                def is_running():
                    return True

            class ApprovalAdapter:
                @staticmethod
                def send_exec_approval(**_kwargs):
                    return None

            due = get_due_jobs()
            assert len(due) == 1
            assert run_one_job(
                due[0],
                adapters={"telegram": ApprovalAdapter()},
                loop=ApprovalLoop(),
            ) is True
            completed = get_job(installed["job_id"])

        assert completed["state"] == "awaiting_trigger"
        outputs = list(
            (home / "cron" / "output" / installed["job_id"]).glob("*.md")
        )
        assert len(outputs) == 1
        assert "handled issue 19" in outputs[0].read_text(encoding="utf-8")
    finally:
        reset_hermes_home_override(token)
        if provider_created:
            unregister_authorization_provider(authorization.PROVIDER_ID)


def test_install_rolls_back_promoted_script_when_native_create_fails(
    tmp_path,
    monkeypatch,
):
    _set_install_runtime_ready(monkeypatch)
    home, build, compiled = _compiled_review_build(tmp_path)
    rolled_back = []
    promotion = types.SimpleNamespace(rollback=lambda: rolled_back.append(True), commit=lambda: None)
    modules = {
        "approval": approval,
        "compiler": types.SimpleNamespace(promote_compiled_artifacts=lambda *_args: promotion),
        "hermes": types.SimpleNamespace(install_cron_job=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("failed"))),
    }

    with pytest.raises(RuntimeError, match="failed"):
        crunch_recorder.install_crunch_build(
            modules,
            home=home,
            profile_name="default",
            build_dir=build,
            note="Approved",
            expected_approval_envelope_sha256=compiled["approval_envelope_sha256"],
        )
    assert rolled_back == [True]


def test_same_build_install_waits_for_open_transaction(
    tmp_path,
    monkeypatch,
):
    _set_install_runtime_ready(monkeypatch)
    home, build, compiled = _compiled_review_build(tmp_path)
    first_promoting = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    second_promoting = threading.Event()
    calls_lock = threading.Lock()
    promotion_calls = 0
    results = []
    errors = []

    def promote(*_args):
        nonlocal promotion_calls
        with calls_lock:
            promotion_calls += 1
            call_number = promotion_calls
        if call_number == 1:
            first_promoting.set()
            assert release_first.wait(timeout=2)
        else:
            second_promoting.set()
        return types.SimpleNamespace(
            rollback=lambda: None,
            commit=lambda: None,
        )

    class FakeHermes:
        @staticmethod
        def install_cron_job(*_args, **_kwargs):
            return {
                "job_id": f"job-{threading.current_thread().name}",
                "idempotent_replay": False,
            }

        @staticmethod
        def activate_cron_job(*_args, **_kwargs):
            return {}

    modules = {
        "approval": approval,
        "compiler": types.SimpleNamespace(
            promote_compiled_artifacts=promote
        ),
        "hermes": FakeHermes(),
    }

    def install(*, mark_started=False):
        if mark_started:
            second_started.set()
        try:
            results.append(
                crunch_recorder.install_crunch_build(
                    modules,
                    home=home,
                    profile_name="default",
                    build_dir=build,
                    note="Approved",
                    expected_approval_envelope_sha256=compiled[
                        "approval_envelope_sha256"
                    ],
                )
            )
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=install, name="first")
    second = threading.Thread(
        target=install,
        kwargs={"mark_started": True},
        name="second",
    )
    first.start()
    assert first_promoting.wait(timeout=2)
    second.start()
    assert second_started.wait(timeout=2)
    assert second_promoting.wait(timeout=0.2) is False
    assert second.is_alive()

    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert len(results) == 2
    assert second_promoting.is_set()


def test_install_lock_fails_closed_during_cross_process_contention(
    tmp_path,
    monkeypatch,
):
    if (
        crunch_recorder.fcntl is None
        and getattr(crunch_recorder, "msvcrt", None) is None
    ):
        pytest.skip("No cross-process file-lock backend is available")

    home = tmp_path / "home"
    build = tmp_path / "build"
    build.mkdir()
    process_context = multiprocessing.get_context(
        "spawn" if sys.platform == "win32" else "fork"
    )
    ready = process_context.Event()
    release = process_context.Event()
    holder = process_context.Process(
        target=_hold_crunch_install_lock,
        args=(str(home), str(build), ready, release),
    )
    holder.start()
    try:
        assert ready.wait(timeout=3)
        monkeypatch.setattr(
            crunch_recorder,
            "_INSTALL_LOCK_TIMEOUT_SECONDS",
            0.2,
        )
        started_at = time.monotonic()
        with pytest.raises(HTTPException) as exc_info:
            with crunch_recorder._crunch_install_lock(
                home=home,
                build_dir=build,
            ):
                pass
        elapsed = time.monotonic() - started_at
        assert exc_info.value.status_code == 409
        assert "Another process is installing" in str(exc_info.value.detail)
        assert 0.15 <= elapsed < 2
    finally:
        release.set()
        holder.join(timeout=3)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=3)

    assert holder.exitcode == 0
    with crunch_recorder._crunch_install_lock(
        home=home,
        build_dir=build,
    ):
        pass


@pytest.mark.parametrize(
    ("route_replay", "expected_remove"),
    [(False, True), (True, False)],
)
def test_activation_failure_cleans_only_new_route_and_rolls_back(
    tmp_path,
    monkeypatch,
    route_replay,
    expected_remove,
):
    _set_install_runtime_ready(monkeypatch)
    home, build, compiled = _compiled_review_build(tmp_path)
    actions = []
    promotion = types.SimpleNamespace(
        rollback=lambda: actions.append("rollback"),
        commit=lambda: actions.append("commit"),
    )

    class FakeHermes:
        @staticmethod
        def install_cron_job(*_args, **_kwargs):
            actions.append("reserve")
            return {"job_id": "job-1", "idempotent_replay": False}

        @staticmethod
        def install_webhook_trigger(**_kwargs):
            actions.append("route")
            return {
                "name": "crunch-example-job-1",
                "idempotent_replay": route_replay,
            }

        @staticmethod
        def activate_cron_job(*_args, **_kwargs):
            actions.append("activate")
            raise RuntimeError("activation failed")

        @staticmethod
        def remove_webhook_trigger(**_kwargs):
            actions.append("remove")
            return True

        @staticmethod
        def abort_cron_job(*_args, **_kwargs):
            actions.append("abort")
            return True

    modules = {
        "approval": approval,
        "compiler": types.SimpleNamespace(
            promote_compiled_artifacts=lambda *_args: promotion
        ),
        "hermes": FakeHermes(),
    }

    with pytest.raises(RuntimeError, match="activation failed"):
        crunch_recorder.install_crunch_build(
            modules,
            home=home,
            profile_name="default",
            build_dir=build,
            note="Approved",
            expected_approval_envelope_sha256=compiled[
                "approval_envelope_sha256"
            ],
            webhook_trigger={
                "description": "When an issue opens",
                "events": ["issues"],
                "filters": None,
            },
        )

    assert ("remove" in actions) is expected_remove
    assert actions[-2:] == ["abort", "rollback"]
    assert "commit" not in actions


def test_install_blocks_unavailable_webhook_before_reserving_a_job(tmp_path):
    home, build, compiled = _compiled_review_build(tmp_path)
    actions = []
    promotion = types.SimpleNamespace(
        rollback=lambda: actions.append("rollback"),
        commit=lambda: actions.append("commit"),
    )
    modules = {
        "approval": approval,
        "compiler": types.SimpleNamespace(
            promote_compiled_artifacts=lambda *_args: (
                actions.append("promote"),
                promotion,
            )[1]
        ),
        "hermes": types.SimpleNamespace(
            install_cron_job=lambda *_args, **_kwargs: actions.append("reserve"),
            install_webhook_trigger=lambda **_kwargs: actions.append("webhook"),
        ),
    }

    with pytest.raises(HTTPException) as exc_info:
        crunch_recorder.install_crunch_build(
            modules,
            home=home,
            profile_name="default",
            build_dir=build,
            note="Approved",
            expected_approval_envelope_sha256=compiled["approval_envelope_sha256"],
            webhook_trigger={
                "description": "When an issue opens",
                "events": ["issues"],
                "filters": [{"field": "action", "equals": "opened"}],
            },
        )
    assert exc_info.value.status_code == 409
    assert "profile owned by this running Hermes gateway" in str(
        exc_info.value.detail
    )
    assert actions == []
    assert not (build / "approval.json").exists()


def test_cancel_draft_interrupts_agent_and_discards_late_result(tmp_path):
    interrupted = []
    operation = dashboard_api._DraftOperation(operation_id="draft-1", recording_dir=tmp_path)
    operation.attach_agent(types.SimpleNamespace(interrupt=interrupted.append))

    operation.cancel()

    assert operation.status == "canceling"
    assert interrupted == ["Workflow drafting canceled by the operator"]
    assert operation.begin_commit() is False
    assert operation.status == "canceled"


def test_cancel_before_agent_attach_interrupts_when_agent_is_created(tmp_path):
    interrupted = []
    operation = dashboard_api._DraftOperation(operation_id="draft-1", recording_dir=tmp_path)
    operation.cancel()
    operation.attach_agent(types.SimpleNamespace(interrupt=interrupted.append))

    assert operation.status == "canceling"
    assert interrupted == ["Workflow drafting canceled by the operator"]


def test_draft_progress_uses_agent_callback_output_and_omits_results(tmp_path):
    operation = dashboard_api._DraftOperation(operation_id="draft-1", recording_dir=tmp_path)

    operation.on_progress("tool.started", "skill_view", "Reading calendar skill", {"name": "secret"})
    operation.on_progress(
        "tool.completed", "skill_view", None, None,
        duration=1.26, is_error=False, result="private tool result",
    )

    payload = dashboard_api._draft_operation_payload(operation)
    assert payload["progress"] == [
        {"type": "tool.started", "tool": "skill_view", "text": "Reading calendar skill"},
        {"type": "tool.completed", "tool": "skill_view", "duration": 1.3, "is_error": False},
    ]
    assert "private tool result" not in json.dumps(payload)

    operation.on_interim("I found the existing calendar skill.", already_streamed=True)
    assert dashboard_api._draft_operation_payload(operation)["progress"][-1] == {
        "type": "assistant.message",
        "text": "I found the existing calendar skill.",
    }


def _write_dashboard_recording(tmp_path: Path, *, state: str = "drafted") -> Path:
    recording_dir = tmp_path / "crunch" / "recordings" / "example" / "rec-1"
    recording_dir.mkdir(parents=True)
    metadata = {
        "source": "dashboard",
        "profile": "default",
        "workflow_id": "example",
        "name": "Example workflow",
        "instructions": "Create the verified result.",
        "schedule": "every 30m",
        "state": state,
        "status": state,
    }
    (recording_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    (recording_dir / "session.json").write_text(json.dumps({"status": state}), encoding="utf-8")
    (recording_dir / "trace.jsonl").write_text("", encoding="utf-8")
    return recording_dir


def test_redraft_rejects_a_stale_digest_without_requiring_feedback(tmp_path, monkeypatch):
    recording_dir = _write_dashboard_recording(tmp_path, state="stopped")
    workflow = _workflow()
    (recording_dir / "workflow.json").write_text(
        json.dumps(workflow),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        dashboard_api,
        "_cron_profile_home",
        lambda _profile: ("default", tmp_path),
    )
    monkeypatch.setattr(
        dashboard_api,
        "load_recording_status",
        lambda **_kwargs: {"status": "stopped"},
    )
    operation_ids_before = set(dashboard_api._DRAFT_OPERATIONS)

    with pytest.raises(HTTPException, match="reviewed draft changed") as exc_info:
        dashboard_api.draft_recording_only(
            "example/rec-1",
            dashboard_api.CrunchRecordingDraftOnlyRequest(
                profile="default",
                name="Example workflow",
                instructions="Create the verified result.",
                expected_draft_digest="stale",
            ),
        )

    assert exc_info.value.status_code == 409
    assert set(dashboard_api._DRAFT_OPERATIONS) == operation_ids_before


def test_resume_recording_uses_resolved_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(
        dashboard_api, "_cron_profile_home", lambda _profile: ("resolved", tmp_path)
    )
    monkeypatch.setattr(
        dashboard_api,
        "_resume_recording_payload",
        lambda home, profile_name: (home, profile_name),
    )

    assert dashboard_api.resume_recording("alias") == (tmp_path, "resolved")


def test_discard_serializes_with_drafting_and_cancels_the_draft(tmp_path, monkeypatch):
    recording_dir = _write_dashboard_recording(tmp_path, state="stopped")
    operation = dashboard_api._DraftOperation(operation_id="draft-dismiss", recording_dir=recording_dir)
    with dashboard_api._DRAFT_OPERATIONS_LOCK:
        dashboard_api._DRAFT_OPERATIONS[operation.operation_id] = operation
    monkeypatch.setattr(dashboard_api, "_cron_profile_home", lambda _profile: ("default", tmp_path))
    try:
        with pytest.raises(HTTPException) as exc_info:
            dashboard_api.discard_recording("example/rec-1", dashboard_api.CrunchRecordingDiscardRequest())
        assert exc_info.value.status_code == 409
        assert operation.status == "canceling"
    finally:
        with dashboard_api._DRAFT_OPERATIONS_LOCK:
            dashboard_api._DRAFT_OPERATIONS.pop(operation.operation_id, None)


def test_discard_marks_the_bundle_dismissed_and_does_not_remove_it(tmp_path, monkeypatch):
    recording_dir = _write_dashboard_recording(tmp_path, state="stopped")
    monkeypatch.setattr(dashboard_api, "_cron_profile_home", lambda _profile: ("default", tmp_path))
    monkeypatch.setattr(dashboard_api, "load_recording_status", lambda **_kwargs: {"status": "stopped"})
    calls = []
    monkeypatch.setattr(
        dashboard_api,
        "discard_native_recording",
        lambda **kwargs: calls.append(kwargs) or {"status": "dismissed"},
    )

    result = dashboard_api.discard_recording("example/rec-1", dashboard_api.CrunchRecordingDiscardRequest())

    assert result == {"status": "dismissed"}
    assert calls == [{"home": tmp_path, "recording_dir": recording_dir}]
    assert recording_dir.is_dir()


def test_dashboard_uses_an_accessible_in_app_dismiss_confirmation():
    source = (DASHBOARD_ROOT / "dist" / "index.js").read_text(encoding="utf-8")

    assert "window.confirm" not in source
    assert "dismissConfirmOpen" in source
    assert 'role: "dialog"' in source
    assert '"aria-modal": "true"' in source
    assert 'event.key === "Escape"' in source


def test_dashboard_provider_timeout_covers_native_prepare_and_awaits_cleanup():
    source = (DASHBOARD_ROOT / "dist" / "index.js").read_text(encoding="utf-8")
    provider_source = (
        DASHBOARD_ROOT.parent / "chrome_capture" / "extension" / "src" / "background.js"
    ).read_text(encoding="utf-8")

    dashboard_timeout = int(
        re.search(
            r"CHROME_PROVIDER_REQUEST_TIMEOUT_MS\s*=\s*(\d+)",
            source,
        ).group(1)
    )
    native_timeout = int(
        re.search(
            r'nativeRequest\("status", \{\}, (\d+)\)',
            provider_source,
        ).group(1)
    )

    assert dashboard_timeout > native_timeout
    assert 'await requestChromePageRecorder("cancel", "", captureAuthorization)' in source


def test_resume_recording_restores_latest_pending_draft(tmp_path, monkeypatch):
    recording_dir = _write_dashboard_recording(tmp_path)
    workflow = _workflow()
    workflow["review"] = {"warnings": [], "draft_revision": 2}
    (recording_dir / "workflow.json").write_text(json.dumps(workflow), encoding="utf-8")
    (recording_dir / "draft-review.md").write_text("# Review\n", encoding="utf-8")
    monkeypatch.setattr(
        dashboard_api,
        "active_native_recording",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        dashboard_api,
        "crunch_install_readiness",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("draft restoration must not depend on live readiness")
        ),
    )

    result = dashboard_api._resume_recording_payload(tmp_path, "default")

    assert result["recording"]["name"] == "Example workflow"
    assert result["recording"]["schedule"] == "every 30m"
    assert result["draft"]["workflow"] == workflow
    assert result["draft"]["draft_digest"] == crunch_recorder.workflow_digest(workflow)
    assert "install_readiness" not in result["draft"]


def test_retry_draft_revision_uses_existing_persisted_workflow(tmp_path, monkeypatch):
    recording_dir = _write_dashboard_recording(tmp_path)
    session_before = (recording_dir / "session.json").read_bytes()
    evidence_pack = {
        "session_id": recording_dir.name,
        "capture_verdict": {"ready": True, "reasons": []},
        "quality": {},
        "summary": {
            "action_count": 1,
            "target_evidence_count": 1,
            "mutation_evidence_count": 1,
            "outcome_evidence_count": 1,
        },
    }
    (recording_dir / "evidence-pack.json").write_text(
        json.dumps(evidence_pack), encoding="utf-8"
    )
    evidence.seal_capture(recording_dir, evidence_pack=evidence_pack)
    # Exercise the upgrade path used by recordings sealed before lifecycle
    # projections were removed from the immutable capture boundary.
    legacy_manifest_path = recording_dir / "capture-manifest.json"
    legacy_manifest = json.loads(legacy_manifest_path.read_text(encoding="utf-8"))
    legacy_manifest["schemaVersion"] = "hermes-crunch-capture-manifest-v1"
    legacy_manifest["files"] = [
        *[
            {
                "path": name,
                "bytes": (recording_dir / name).stat().st_size,
                "sha256": hashlib.sha256((recording_dir / name).read_bytes()).hexdigest(),
            }
            for name in ("metadata.json", "session.json")
        ],
        *legacy_manifest["files"],
    ]
    legacy_manifest_path.write_text(
        json.dumps(legacy_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    prior_source_digest = crunch_recorder._recording_digest(recording_dir)
    workflow = _workflow()
    workflow["review"] = {"warnings": [], "draft_revision": 2}
    workflow_path = recording_dir / "workflow.json"
    workflow_path.write_text(json.dumps(workflow), encoding="utf-8")

    candidate_workflow = _workflow()
    candidate = types.SimpleNamespace(
        output=workflow_path,
        recording_dir=recording_dir,
        workflow=candidate_workflow,
        metadata={},
        review_markdown="# Review\n",
    )
    modules = {
        "compiler": types.SimpleNamespace(
            effective_approval_boundaries=lambda _workflow: {"boundaries": []},
            review_blockers=lambda _workflow: [],
        ),
        "drafting": types.SimpleNamespace(author_workflow=lambda *_args, **_kwargs: candidate_workflow),
        "recording": types.SimpleNamespace(
            build_draft_candidate=lambda *_args, **_kwargs: candidate,
            seal_draft_revision=recording.seal_draft_revision,
        ),
    }
    monkeypatch.setattr(dashboard_api, "_cron_profile_home", lambda _profile: ("default", tmp_path))
    monkeypatch.setattr(dashboard_api, "resolve_recording_ref", lambda _home, _key: recording_dir)
    monkeypatch.setattr(dashboard_api, "load_recording_status", lambda **_kwargs: {"status": "stopped"})
    monkeypatch.setattr(dashboard_api, "load_crunch_modules", lambda: modules)
    monkeypatch.setattr(
        dashboard_api,
        "crunch_install_readiness",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("draft publication must not depend on live readiness")
        ),
    )
    dashboard_api._DRAFT_OPERATIONS.clear()

    operation = dashboard_api.draft_recording_only(
        "example/rec-1",
        dashboard_api.CrunchRecordingDraftOnlyRequest(
            profile="default",
            name="Example workflow",
            instructions="Create the verified result.",
        ),
    )
    operation_id = operation["operation_id"]
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        current = dashboard_api._DRAFT_OPERATIONS[operation_id]
        if current.status in {"completed", "failed", "canceled"}:
            break
        time.sleep(0.01)

    current = dashboard_api._DRAFT_OPERATIONS[operation_id]
    assert current.status == "completed"
    assert current.result["workflow"]["review"]["draft_revision"] == 3
    assert json.loads((recording_dir / "metadata.json").read_text(encoding="utf-8"))["draft_revision"] == 3
    assert (recording_dir / "session.json").read_bytes() == session_before
    source_digest = current.result["workflow"]["review"]["source_digest"]
    assert source_digest == crunch_recorder._recording_digest(recording_dir)
    assert source_digest == prior_source_digest
    assert json.loads((recording_dir / "capture-manifest.json").read_text(encoding="utf-8"))["schemaVersion"] == "hermes-crunch-capture-manifest-v2"
    assert evidence.verified_capture_acceptance(recording_dir)["blockers"] == []


def test_mark_recording_scheduled_closes_pending_lifecycle(tmp_path):
    recording_dir = _write_dashboard_recording(tmp_path)

    dashboard_api._mark_recording_scheduled(recording_dir, "job-123")

    for filename in ("metadata.json", "session.json"):
        value = json.loads((recording_dir / filename).read_text(encoding="utf-8"))
        assert value["state"] == "scheduled"
        assert value["status"] == "scheduled"
        assert value["scheduled_job_id"] == "job-123"
        assert value["scheduled_at"]


def test_dashboard_review_projection_counts_inline_controls_and_hides_generic_capture():
    bundle = DASHBOARD_ROOT / "dist" / "index.js"
    harness = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const marker = "  function WorkflowPage() {";
if (!source.includes(marker)) throw new Error("WorkflowPage marker missing");
const instrumented = source.replace(
  marker,
  "  globalThis.__reviewHelpers = { normalizeBoundaries, summarizeHumanChecks, approvalTargetLabel, profileScopedActionHref, selectInstallReadinessBlockers, projectStepCaptureWarnings, recordingNeedsRerecord, summarizeRerecordBlockers };\n" + marker,
);
const sandbox = {
  window: {
    __HERMES_PLUGIN_SDK__: {
      React: { createElement() { return null; } },
      hooks: {},
      components: {},
      schedule: {},
    },
    __HERMES_PLUGINS__: { register() {} },
  },
};
Object.assign(sandbox, sandbox.window);
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(instrumented, sandbox);
const helpers = sandbox.__reviewHelpers;
const startSource = source.slice(
  source.indexOf("    async function start() {"),
  source.indexOf("    async function stopAndDraft() {"),
);
const normalized = helpers.normalizeBoundaries({ boundaries: [
  {
    key: "operation:create-note",
    kind: "operation",
    operation_id: "create-note",
    effects: ["create"],
    description: "Create the Example Domain note",
    enabled: true,
    steps: [1],
  },
  {
    key: "operation:write",
    kind: "operation",
    operation_id: "write",
    effects: ["update"],
    description: "Create or refresh the record",
    enabled: true,
    steps: [2],
  },
  {
    key: "script:execute-reviewed-script",
    kind: "script",
    effect: "execute",
    script_id: "execute-reviewed-script",
    description: "Execute the exact reviewed script",
    enabled: false,
    steps: [3],
  },
] });
if (normalized.error) throw new Error(normalized.error);
const warnings = helpers.projectStepCaptureWarnings({
  warnings: [
    {
      kind: "capture_quality_gap",
      step: 2,
      message: "The recording does not prove which record version was saved.",
      verify: "Confirm the saved record contains the demonstrated fields.",
    },
    {
      kind: "capture_quality_gap",
      step: 1,
      message: "Capture diagnostic: evidence action blocked.",
      verify: "Inspect internal capture state.",
    },
  ],
  recording_warnings: [
    {
      kind: "capture_quality_gap",
      step_id: "step-10",
      message: "The draft is best-effort; review any affected step.",
    },
  ],
}, 3);
process.stdout.write(JSON.stringify({
  kinds: normalized.boundaries.map((boundary) => boundary.kind),
  initial: helpers.summarizeHumanChecks(normalized.boundaries, {}),
  allOff: helpers.summarizeHumanChecks(normalized.boundaries, {
    "operation:create-note": false,
    "operation:write": false,
    "script:execute-reviewed-script": false,
  }),
  allOn: helpers.summarizeHumanChecks(normalized.boundaries, {
    "operation:create-note": true,
    "operation:write": true,
    "script:execute-reviewed-script": true,
  }),
  approvalTarget: helpers.approvalTargetLabel({
    approval_target: "telegram",
    approval_route: { platform: "discord", chat_id: "do-not-render" },
  }),
  profiledChannelsHref: helpers.profileScopedActionHref("/channels", "writer"),
  profiledPluginHref: helpers.profileScopedActionHref("/plugins?highlight=langfuse", "writer"),
  scheduleBlockers: helpers.selectInstallReadinessBlockers([
    { code: "approval_route_unavailable" },
    { code: "approval_gateway_unavailable" },
    { code: "webhook_receiver_unavailable" },
  ], "schedule").map((item) => item.code),
  warnings,
  cleanWarnings: helpers.projectStepCaptureWarnings({ warnings: [], recording_warnings: [] }, 3),
  evidenceRerecord: helpers.summarizeRerecordBlockers([
    {
      kind: "capture_evidence_incomplete",
      message: "internal provider diagnostics must not be rendered",
      resolution: "rerecord",
    },
  ]),
  integrityRerecord: helpers.summarizeRerecordBlockers([
    {
      kind: "capture_acceptance_invalid",
      message: "internal manifest diagnostics must not be rendered",
      resolution: "rerecord",
    },
  ]),
  evidenceRequiresRerecord: helpers.recordingNeedsRerecord([
    {
      kind: "capture_evidence_incomplete",
      resolution: "rerecord",
    },
  ]),
  integrityRequiresRerecord: helpers.recordingNeedsRerecord([
    {
      kind: "capture_acceptance_invalid",
      resolution: "rerecord",
    },
  ]),
  hidesTechnicalRerecordTranscript: source.includes(
    "if (requiresNewDemonstration && rerecordBlockerText)"
  ),
  hasGlobalCaptureCopy: source.includes("Best-effort capture"),
  hasWorkflowWideCopy: source.toLowerCase().includes("workflow-wide"),
  hasOutcomeFirstGuidance: source.includes(
    "Work normally—you do not need to explain every click."
  ),
  hasSuccessCheckCopy: source.includes("Hermes will perform this check: "),
  hasApprovalPauseCopy: source.includes("Approval pauses let you authorize a change"),
  namesHomeGateway: source.includes("Each enabled protected action will pause before it runs")
    && source.includes("home gateway for this profile"),
  rendersAllReadinessBlockers: source.includes("selectedReadinessBlockers.map"),
  hasRecordingDisclosure: source.includes("Before recording")
    || source.includes("I understand and agree to this recording and data use.")
    || source.includes("Raw evidence stays in this local Hermes profile."),
  hasRecordingConsentState: source.includes("recordingConsent")
    || source.includes("capture_consent_acknowledged")
    || source.includes("capture_consent_version"),
  startRequestCarriesCaptureAuthorization:
    startSource.includes("capture_authorization_nonce: captureAuthorization")
    && startSource.includes("capture_dashboard_origin: window.location.origin"),
  startButtonIsDirectGesture: source.includes("onClick: start")
    && source.includes("disabled: !!busy")
    && source.includes('"aria-describedby": "hc-recording-requirements"'),
  hasWebApprovalDecision: source.includes("/api/cron/approvals")
    || source.includes("Approve request")
    || source.includes("Deny request"),
  hasLegacyHumanCheckCopy: /["']Human checks?["']/.test(source),
  hasPlainActivationCopy: source.includes("Review final setup")
    && source.includes("Turn on workflow")
    && !source.includes("Compile for approval")
    && !source.includes("Approve exact payload and install"),
  technicalDetailsAreCollapsed: source.includes(
    'h("details", { className: "hc-approval-envelope" }'
  ),
  exactScriptSourceIsVisible: source.includes(
    'h("section", {'
  ) && source.includes('className: "hc-script-review"')
    && source.includes('"Exact script source"'),
  hasRecordAgainCta: source.includes('"Record again"'),
}));
"""
    result = subprocess.run(
        ["node", "-e", harness, str(bundle)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    projection = json.loads(result.stdout)

    assert projection == {
        "kinds": ["operation", "operation", "script"],
        "initial": "3 approval pauses · 2 on · 2 suggested · 3 steps",
        "allOff": "3 approval pauses · 0 on · 2 suggested · 3 steps",
        "allOn": "3 approval pauses · 3 on · 2 suggested · 3 steps",
        "approvalTarget": "telegram",
        "profiledChannelsHref": "/channels?profile=writer",
        "profiledPluginHref": "/plugins?highlight=langfuse&profile=writer",
        "scheduleBlockers": [
            "approval_route_unavailable",
            "approval_gateway_unavailable",
        ],
        "warnings": {
            "2": {
                "verify": "Confirm the saved record contains the demonstrated fields.",
            }
        },
        "cleanWarnings": {},
        "evidenceRerecord": "",
        "integrityRerecord": (
            "Part of the saved demonstration is missing or changed, so Hermes can’t safely build from it. "
            "Nothing has been turned on. Please show the workflow again."
        ),
        "evidenceRequiresRerecord": False,
        "integrityRequiresRerecord": True,
        "hidesTechnicalRerecordTranscript": True,
        "hasGlobalCaptureCopy": False,
        "hasWorkflowWideCopy": False,
        "hasOutcomeFirstGuidance": True,
        "hasSuccessCheckCopy": True,
        "hasApprovalPauseCopy": True,
        "namesHomeGateway": True,
        "rendersAllReadinessBlockers": True,
        "hasRecordingDisclosure": False,
        "hasRecordingConsentState": False,
        "startRequestCarriesCaptureAuthorization": True,
        "startButtonIsDirectGesture": True,
        "hasWebApprovalDecision": False,
        "hasLegacyHumanCheckCopy": False,
        "hasPlainActivationCopy": True,
        "technicalDetailsAreCollapsed": True,
        "exactScriptSourceIsVisible": True,
        "hasRecordAgainCta": False,
    }


def test_dashboard_draft_operation_always_releases_busy_state():
    bundle = DASHBOARD_ROOT / "dist" / "index.js"
    harness = r"""
const fs = require("node:fs");
const source = fs.readFileSync(process.argv[1], "utf8");
const startMarker = "    async function startDraftOperation(reviewThreadId) {";
const endMarker = "\n    async function cancelDraft()";
const start = source.indexOf(startMarker);
const end = source.indexOf(endMarker, start);
if (start < 0 || end < 0) throw new Error("startDraftOperation source missing");
const functionSource = source.slice(start, end);

function build(postImpl) {
  const state = { busy: "seed", operationId: "seed", canceling: true, applied: false, payload: null };
  const deps = {
    profile: "default",
    name: "Example",
    instructions: "Create the result.",
    draft: { draft_digest: "digest-1" },
    recordingKey: "example/recording-1",
    updatePageState(patch) {
      if ("busy" in patch) state.busy = patch.busy;
      if ("draftOperationId" in patch) state.operationId = patch.draftOperationId;
      if ("cancelingDraft" in patch) state.canceling = patch.cancelingDraft;
    },
    post: async (_path, payload) => {
      state.payload = payload;
      return postImpl();
    },
    async waitForDraft() { return { draft_digest: "digest-2" }; },
    applyDraft() { state.applied = true; },
  };
  const names = Object.keys(deps);
  const factory = new Function(...names, functionSource + "; return startDraftOperation;");
  return { state, run: factory(...names.map((name) => deps[name])) };
}

(async () => {
  const success = build(async () => ({ operation_id: "operation-1", progress: [] }));
  await success.run("thread-1");
  const ordinary = build(async () => ({ operation_id: "operation-2", progress: [] }));
  await ordinary.run();
  const failure = build(async () => { throw new Error("draft failed"); });
  let failureMessage = "";
  try {
    await failure.run("thread-1");
  } catch (error) {
    failureMessage = error.message;
  }
  process.stdout.write(JSON.stringify({
    success: success.state,
    ordinary: ordinary.state,
    failure: failure.state,
    failureMessage,
  }));
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""
    result = subprocess.run(
        ["node", "-e", harness, str(bundle)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert json.loads(result.stdout) == {
        "success": {
            "busy": "",
            "operationId": "",
            "canceling": False,
            "applied": True,
            "payload": {
                "profile": "default",
                "name": "Example",
                "instructions": "Create the result.",
                "expected_draft_digest": "digest-1",
                "review_thread_id": "thread-1",
            },
        },
        "ordinary": {
            "busy": "",
            "operationId": "",
            "canceling": False,
            "applied": True,
            "payload": {
                "profile": "default",
                "name": "Example",
                "instructions": "Create the result.",
                "expected_draft_digest": "digest-1",
            },
        },
        "failure": {
            "busy": "",
            "operationId": "",
            "canceling": False,
            "applied": False,
            "payload": {
                "profile": "default",
                "name": "Example",
                "instructions": "Create the result.",
                "expected_draft_digest": "digest-1",
                "review_thread_id": "thread-1",
            },
        },
        "failureMessage": "draft failed",
    }


def test_install_binds_approved_build_provenance(tmp_path, monkeypatch):
    _set_install_runtime_ready(monkeypatch)
    home, build, compiled = _compiled_review_build(tmp_path)
    summary = json.loads((build / "compile-summary.json").read_text(encoding="utf-8"))
    modules = {
        "approval": approval,
        "compiler": compiler,
        "hermes": hermes,
    }

    with monkeypatch.context() as atomic_failure:
        atomic_failure.setattr(
            recording_bundle.os,
            "replace",
            lambda *_args: (_ for _ in ()).throw(OSError("injected pre-replace crash")),
        )
        with pytest.raises(OSError, match="injected pre-replace crash"):
            approval.approve_build(
                build,
                approved_by="dashboard",
                approval_envelope_sha256=compiled["approval_envelope_sha256"],
            )
    assert not (build / "approval.json").exists()

    result = crunch_recorder.install_crunch_build(
        modules,
        home=home,
        profile_name="default",
        build_dir=build,
        note="Approved",
        expected_approval_envelope_sha256=compiled["approval_envelope_sha256"],
    )

    retry = crunch_recorder.install_crunch_build(
        modules,
        home=home,
        profile_name="default",
        build_dir=build,
        note="Approved",
        expected_approval_envelope_sha256=compiled["approval_envelope_sha256"],
    )

    approved = result["approval"]
    assert retry["approval"] == approved
    assert retry["install"]["job_id"] == result["install"]["job_id"]
    assert retry["install"]["idempotent_replay"] is True
    with hermes._profile_cron_scope(home):
        from cron.jobs import list_jobs

        jobs = list_jobs(include_disabled=True)
    assert len(jobs) == 1
    provenance = jobs[0]["origin"]["provenance"]
    compiled_provenance = summary["provenance"]
    for field in (
        "workflow_id",
        "recording_id",
        "source_digest",
        "draft_digest",
        "draft_revision",
        "build_id",
        "build_digest",
    ):
        assert provenance[field] == compiled_provenance[field]
    assert provenance["payload_sha256"] == approved["payload_sha256"]
    assert provenance["artifacts_sha256"] == approved["artifacts_sha256"]
    assert provenance["approved_by"] == "dashboard"
    assert approved["compile_summary_sha256"] == approval.payload_hash(summary)
    receipt_digest = hashlib.sha256(
        json.dumps(approved, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert (
        provenance["approval_envelope_sha256"]
        == compiled["approval_envelope_sha256"]
    )
    assert provenance["approval_receipt_sha256"] == receipt_digest


@pytest.mark.parametrize(
    ("filename", "contents", "message"),
    [
        ("compile-summary.json", None, "compile summary"),
        ("compile-summary.json", "{", "compile summary"),
        ("artifacts.json", None, "artifact manifest"),
        ("artifacts.json", "{", "artifact manifest"),
    ],
)
def test_install_rejects_missing_or_corrupt_build_record(
    tmp_path, filename, contents, message
):
    home, build, compiled = _compiled_review_build(tmp_path)
    path = build / filename
    if contents is None:
        path.unlink()
    else:
        path.write_text(contents, encoding="utf-8")

    with pytest.raises(HTTPException) as exc_info:
        crunch_recorder.install_crunch_build(
            {"approval": approval},
            home=home,
            profile_name="default",
            build_dir=build,
            note="Approved",
            expected_approval_envelope_sha256=compiled["approval_envelope_sha256"],
        )
    assert exc_info.value.status_code == 409
    assert "no longer matches the reviewed build" in str(exc_info.value.detail)


def test_install_rejects_compile_summary_mutated_after_approval(
    tmp_path,
    monkeypatch,
):
    _set_install_runtime_ready(monkeypatch)
    home, build, compiled = _compiled_review_build(tmp_path)

    def approve_then_mutate(*args, **kwargs):
        approved = approval.approve_build(*args, **kwargs)
        summary_path = build / "compile-summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary["provenance"]["draft_digest"] = "f" * 64
        summary_path.write_text(json.dumps(summary), encoding="utf-8")
        return approved

    guarded_approval = types.SimpleNamespace(
        approve_build=approve_then_mutate,
        load_approved_payload=approval.load_approved_payload,
        verify_approval_envelope=approval.verify_approval_envelope,
    )
    with pytest.raises(approval.ApprovalError, match="changed after approval"):
        crunch_recorder.install_crunch_build(
            {"approval": guarded_approval},
            home=home,
            profile_name="default",
            build_dir=build,
            note="Approved",
            expected_approval_envelope_sha256=compiled["approval_envelope_sha256"],
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("omit_recording", "missing required provenance: recording_id"),
        ("change_build", "build_digest does not match the approved build"),
    ],
)
def test_install_rejects_incomplete_or_mismatched_provenance(
    tmp_path, mutation, message
):
    home, build, compiled = _compiled_review_build(tmp_path)
    summary_path = build / "compile-summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if mutation == "omit_recording":
        del summary["provenance"]["recording_id"]
    else:
        summary["provenance"]["build_digest"] = "f" * 64
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    with pytest.raises(HTTPException) as exc_info:
        crunch_recorder.install_crunch_build(
            {"approval": approval},
            home=home,
            profile_name="default",
            build_dir=build,
            note="Approved",
            expected_approval_envelope_sha256=compiled["approval_envelope_sha256"],
        )
    assert exc_info.value.status_code == 409
    assert "no longer matches the reviewed build" in str(exc_info.value.detail)
