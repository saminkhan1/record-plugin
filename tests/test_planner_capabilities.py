from __future__ import annotations

import json

import pytest

from hermes_plugins.record.record import drafting, planner, runtime
from hermes_plugins.record.record.compiler import CompileError, validate_draft_plan


def _agent_plan() -> dict:
    return {
        "steps": ["Archive the message.", "Verify it left the inbox."],
        "trigger": None,
        "job_spec": {
            "prompt": "Archive the demonstrated message and verify the result.",
            "skills": [],
            "no_agent": False,
        },
        "script_source": None,
        "warnings": [],
    }


def _script_plan() -> dict:
    return {
        "steps": ["Run the deterministic operation.", "Verify its result."],
        "trigger": None,
        "job_spec": {
            "prompt": "",
            "skills": [],
            "no_agent": True,
        },
        "script_source": {"path": "run.py", "body": "print('ok')\n"},
        "warnings": [],
    }


def _begin(task_id: str):
    return planner.begin(
        task_id,
        job_defaults={
            "name": "Inbox cleanup",
            "schedule": "0 9 * * *",
            "deliver": "local",
        },
        script_namespace="record/workflow123",
    )


def test_planner_publishes_one_typed_submission_tool():
    registered = {}

    class FakeContext:
        def register_tool(self, **kwargs):
            registered[kwargs["name"]] = kwargs["schema"]

    planner.register(FakeContext())

    assert set(registered) == {"record_submit_draft"}
    submit = registered["record_submit_draft"]
    assert submit["parameters"]["required"] == ["plan"]
    plan_schema = submit["parameters"]["properties"]["plan"]
    assert set(plan_schema["required"]) == {"steps", "job_spec", "warnings"}
    assert plan_schema["additionalProperties"] is False
    assert "$defs" not in str(submit["parameters"])
    assert "$ref" not in str(submit["parameters"])


def test_submission_stores_complete_hermes_job_spec_and_stable_script_path():
    task_id = "record:planner:canonical-job"
    session = _begin(task_id)
    try:
        result = json.loads(
            planner.submit_draft({"plan": _script_plan()}, task_id=task_id)
        )
        assert result["success"] is True
        assert session.submitted == planner.finish(task_id)
    finally:
        planner.finish(task_id)

    spec = session.submitted["job_spec"]
    assert spec["name"] == "Inbox cleanup"
    assert spec["schedule"] == "0 9 * * *"
    assert spec["deliver"] == "local"
    assert spec["script"] == "record/workflow123/run.py"
    assert spec["no_agent"] is True


def test_parallel_planning_contract_is_rejected():
    plan = _agent_plan()
    plan["planning"] = {"candidates": []}
    with pytest.raises(CompileError, match="planning: Extra inputs are not permitted"):
        validate_draft_plan(
            plan,
            job_defaults={"name": "Job", "schedule": "0 9 * * *"},
            script_namespace="record/job",
        )


def test_author_workflow_uses_host_agent_and_only_pins_submission_tool(
    tmp_path, monkeypatch
):
    calls = []

    def fake_run_agent(**kwargs):
        calls.append(kwargs)
        result = planner.submit_draft(
            {"plan": _agent_plan()},
            task_id=kwargs["task_id"],
        )
        assert json.loads(result)["success"] is True
        return {"completed": True, "failed": False, "final_response": ""}

    monkeypatch.setattr(drafting, "_require_active_home", lambda home: home)
    monkeypatch.setattr(drafting, "_recording_index", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(runtime, "run_agent", fake_run_agent)

    result = drafting.author_workflow(
        "Archive the message",
        {
            "workflow_id": "inbox",
            "name": "Inbox cleanup",
            "requested_schedule": "0 9 * * *",
            "steps": [],
        },
        recording_dir=tmp_path,
        home=tmp_path,
    )

    assert calls[0]["required_tools"] == ["record_submit_draft"]
    assert calls[0]["full_capabilities"] is False
    assert calls[0]["max_iterations"] == 24
    assert result["job_spec"]["schedule"] == "0 9 * * *"
    assert result["job_spec"]["name"] == "Inbox cleanup"


def test_submit_draft_rejects_invalid_contract_without_crossing_boundary():
    task_id = "record:planner:invalid-draft"
    session = _begin(task_id)
    try:
        result = planner.submit_draft(
            {"plan": {"steps": ["Only a view"]}},
            task_id=task_id,
        )
        assert json.loads(result)["success"] is False
        assert "violates the Record draft contract" in result
        assert session.submitted is None
    finally:
        planner.finish(task_id)


def test_author_workflow_falls_back_when_agent_returns_plain_text(tmp_path, monkeypatch):
    monkeypatch.setattr(drafting, "_require_active_home", lambda home: home)
    monkeypatch.setattr(drafting, "_recording_index", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        runtime,
        "run_agent",
        lambda **_kwargs: {
            "completed": True,
            "failed": False,
            "final_response": "Here is the workflow in prose.",
        },
    )

    result = drafting.author_workflow(
        "Run and verify the task",
        {
            "workflow_id": "plain-text",
            "name": "Plain text",
            "requested_schedule": "0 9 * * *",
            "steps": [],
        },
        recording_dir=tmp_path,
        home=tmp_path,
    )

    assert result["job_spec"]["name"] == "Plain text"
    assert result["job_spec"]["schedule"] == "0 9 * * *"
    assert result["job_spec"]["no_agent"] is False
    assert result["warnings"][0]["kind"] == "conservative_fallback"
