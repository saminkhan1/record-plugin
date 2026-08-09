from __future__ import annotations

import copy
import json

import pytest

from hermes_plugins.record.record import compiler, hermes


def _payload() -> dict:
    return {
        "name": "Daily inbox cleanup",
        "prompt": "Archive the reviewed messages.",
        "schedule": "0 9 * * *",
        "deliver": "local",
        "skills": [],
        "no_agent": False,
    }


def _provenance() -> dict:
    return {
        "workflow_id": "daily-inbox-cleanup",
        "install_idempotency_key": "a" * 64,
        "draft_revision": 1,
    }


def _script_workflow(body: str) -> dict:
    return compiler.validate_draft_plan(
        {
            "steps": ["Run the reviewed script.", "Verify its result."],
            "trigger": None,
            "job_spec": {
                "prompt": "",
                "skills": [],
                "no_agent": True,
            },
            "script_source": {"path": "run.py", "body": body},
            "warnings": [],
        },
        job_defaults={
            "name": "Content-addressed script",
            "schedule": "0 9 * * *",
            "deliver": "local",
        },
        script_namespace="record/workflow123",
    )


def _persisted_job(arguments: dict, job_id: str) -> dict:
    from cron.jobs import parse_schedule

    schedule = parse_schedule(arguments["schedule"])
    job = {
        key: value
        for key, value in arguments.items()
        if key not in {"action", "schedule"}
    }
    job.update(
        {
            "id": job_id,
            "schedule": schedule,
            "schedule_display": schedule["display"],
            "repeat": {
                "times": 1 if schedule.get("kind") == "once" else None,
                "completed": 0,
            },
            "enabled": True,
            "state": "scheduled",
        }
    )
    return job


def test_create_persists_intent_before_cron_and_finalizes_it(tmp_path, monkeypatch):
    import cron

    jobs = []
    receipt_path = (
        tmp_path / "record" / "install-receipts" / f"{'a' * 64}.json"
    )

    monkeypatch.setattr(hermes, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        cron,
        "list_jobs",
        lambda include_disabled=True: list(jobs),
    )
    monkeypatch.setattr(
        cron,
        "get_job",
        lambda job_id: next(
            (job for job in jobs if job["id"] == job_id),
            None,
        ),
    )

    def create(arguments):
        intent = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert intent["state"] == "installing"
        job = _persisted_job(arguments, "normalcreate1")
        jobs.append(job)
        return {"success": True, "job_id": job["id"], "job": job}

    monkeypatch.setattr(hermes, "_dispatch_cron_tool", create)

    result = hermes.install_workflow(
        _payload(),
        hermes_home=tmp_path,
        provenance=_provenance(),
    )

    assert result["action"] == "created"
    assert result["idempotent_replay"] is False
    finalized = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert finalized["state"] == "installed"
    assert finalized["job_id"] == "normalcreate1"


def test_create_adopts_one_matching_job_after_malformed_host_result(
    tmp_path, monkeypatch
):
    import cron

    jobs = []
    monkeypatch.setattr(hermes, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        cron,
        "list_jobs",
        lambda include_disabled=True: list(jobs),
    )
    monkeypatch.setattr(
        cron,
        "get_job",
        lambda job_id: next(
            (job for job in jobs if job["id"] == job_id),
            None,
        ),
    )

    def persist_with_bad_result(arguments):
        jobs.append(_persisted_job(arguments, "actualcreate1"))
        return {"success": True, "job_id": "wrong-result-id"}

    monkeypatch.setattr(hermes, "_dispatch_cron_tool", persist_with_bad_result)

    result = hermes.install_workflow(
        _payload(),
        hermes_home=tmp_path,
        provenance=_provenance(),
    )

    assert result["action"] == "created"
    assert result["job_id"] == "actualcreate1"
    assert len(jobs) == 1
    receipt_path = (
        tmp_path / "record" / "install-receipts" / f"{'a' * 64}.json"
    )
    finalized = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert finalized["state"] == "installed"
    assert finalized["job_id"] == "actualcreate1"


def test_create_preserves_intent_when_bad_result_leaves_ambiguous_jobs(
    tmp_path, monkeypatch
):
    import cron

    jobs = []
    monkeypatch.setattr(hermes, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        cron,
        "list_jobs",
        lambda include_disabled=True: list(jobs),
    )
    monkeypatch.setattr(
        cron,
        "get_job",
        lambda job_id: next(
            (job for job in jobs if job["id"] == job_id),
            None,
        ),
    )

    def persist_ambiguous_jobs(arguments):
        jobs.append(_persisted_job(arguments, "matchingcreate1"))
        jobs.append(
            _persisted_job(
                {**arguments, "name": "Unexpected second job"},
                "unexpectedcreate2",
            )
        )
        return {"success": True, "job_id": "wrong-result-id"}

    monkeypatch.setattr(hermes, "_dispatch_cron_tool", persist_ambiguous_jobs)

    with pytest.raises(
        hermes.NativeReconciliationRequired,
        match="outcome is ambiguous",
    ):
        hermes.install_workflow(
            _payload(),
            hermes_home=tmp_path,
            provenance=_provenance(),
        )

    assert len(jobs) == 2
    receipt_path = (
        tmp_path / "record" / "install-receipts" / f"{'a' * 64}.json"
    )
    interrupted = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert interrupted["state"] == "installing"
    assert interrupted["reconciliation_required"] is True


def test_update_ambiguity_survives_reconciliation_marker_failure(
    tmp_path, monkeypatch
):
    import cron

    old_payload = _payload()
    new_payload = {**old_payload, "prompt": "Archive only reviewed invoices."}
    old_provenance = _provenance()
    new_provenance = {
        **old_provenance,
        "install_idempotency_key": "b" * 64,
        "draft_revision": 2,
    }
    job = _persisted_job(
        hermes._cron_tool_arguments("create", old_payload),
        "existingjob1",
    )
    jobs = [job]
    monkeypatch.setattr(hermes, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        cron,
        "list_jobs",
        lambda include_disabled=True: list(jobs),
    )
    monkeypatch.setattr(
        cron,
        "get_job",
        lambda job_id: next(
            (
                copy.deepcopy(candidate)
                for candidate in jobs
                if candidate["id"] == job_id
            ),
            None,
        ),
    )
    hermes._write_install_receipt(
        hermes_home=tmp_path,
        install_key="a" * 64,
        receipt=hermes._cron_receipt(
            action="created",
            job_id=job["id"],
            install_key="a" * 64,
            provenance=old_provenance,
        ),
    )

    def mutate_then_fail(arguments):
        assert arguments["action"] == "update"
        job["prompt"] = arguments["prompt"]
        raise RuntimeError("simulated uncertain update result")

    monkeypatch.setattr(hermes, "_dispatch_cron_tool", mutate_then_fail)
    monkeypatch.setattr(
        hermes,
        "_replace_install_receipt",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("marker write failed")),
    )

    with pytest.raises(
        hermes.NativeReconciliationRequired,
        match="update outcome requires reconciliation",
    ):
        hermes.install_workflow(
            new_payload,
            hermes_home=tmp_path,
            provenance=new_provenance,
        )

    assert job["prompt"] == new_payload["prompt"]
    intent_path = (
        tmp_path / "record" / "install-receipts" / f"{'b' * 64}.json"
    )
    intent = json.loads(intent_path.read_text(encoding="utf-8"))
    assert intent["state"] == "installing"
    assert "reconciliation_required" not in intent


def test_retry_recovers_job_persisted_before_final_receipt(tmp_path, monkeypatch):
    import cron

    jobs = []
    dispatches = []
    payload = _payload()
    payload["schedule"] = "every 1h"

    monkeypatch.setattr(hermes, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        cron,
        "list_jobs",
        lambda include_disabled=True: list(jobs),
    )
    monkeypatch.setattr(
        cron,
        "get_job",
        lambda job_id: next(
            (job for job in jobs if job["id"] == job_id),
            None,
        ),
    )

    def persist_then_crash(arguments):
        dispatches.append(dict(arguments))
        job = _persisted_job(arguments, "crashwindow1")
        jobs.append(job)
        raise SystemExit("simulated process crash")

    monkeypatch.setattr(hermes, "_dispatch_cron_tool", persist_then_crash)

    with pytest.raises(SystemExit, match="simulated process crash"):
        hermes.install_workflow(
            payload,
            hermes_home=tmp_path,
            provenance=_provenance(),
        )

    receipt_path = (
        tmp_path / "record" / "install-receipts" / f"{'a' * 64}.json"
    )
    interrupted = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert interrupted["state"] == "installing"
    assert len(jobs) == 1

    monkeypatch.setattr(
        hermes,
        "_dispatch_cron_tool",
        lambda arguments: dispatches.append(dict(arguments)),
    )
    recovered = hermes.install_workflow(
        payload,
        hermes_home=tmp_path,
        provenance=_provenance(),
    )

    assert recovered["action"] == "replayed"
    assert recovered["idempotent_replay"] is True
    assert recovered["job_id"] == "crashwindow1"
    assert len(jobs) == 1
    assert len(dispatches) == 1
    finalized = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert finalized["state"] == "installed"
    assert finalized["action"] == "created"
    assert finalized["job_id"] == "crashwindow1"


def test_missing_installed_job_rejects_same_revision_without_cron_dispatch(
    tmp_path,
    monkeypatch,
):
    import cron

    monkeypatch.setattr(hermes, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(cron, "list_jobs", lambda include_disabled=True: [])
    monkeypatch.setattr(cron, "get_job", lambda _job_id: None)
    hermes._write_install_receipt(
        hermes_home=tmp_path,
        install_key="a" * 64,
        receipt=hermes._cron_receipt(
            action="created",
            job_id="removed-job",
            install_key="a" * 64,
            provenance=_provenance(),
        ),
    )
    monkeypatch.setattr(
        hermes,
        "_dispatch_cron_tool",
        lambda _arguments: pytest.fail("same revision dispatched Cron"),
    )

    with pytest.raises(
        hermes.HermesInstallError,
        match="removed; create a new reviewed revision",
    ):
        hermes.install_workflow(
            _payload(),
            hermes_home=tmp_path,
            provenance=_provenance(),
        )


def test_cron_presence_read_failure_is_unknown(monkeypatch):
    import cron

    monkeypatch.setattr(
        cron,
        "get_job",
        lambda _job_id: (_ for _ in ()).throw(OSError("store unavailable")),
    )

    assert hermes.cron_job_presence("job-123") == "unknown"


def test_process_death_before_cron_update_cannot_change_old_job_script_bytes(
    tmp_path, monkeypatch
):
    import cron

    old_workflow = _script_workflow("print('old revision')\n")
    new_workflow = _script_workflow("print('new revision')\n")
    old_build = tmp_path / "old-build"
    new_build = tmp_path / "new-build"
    old_compiled = compiler.compile_workflow(old_workflow, tmp_path, old_build)
    new_compiled = compiler.compile_workflow(new_workflow, tmp_path, new_build)
    old_script = old_compiled.payload["script"]
    new_script = new_compiled.payload["script"]
    assert old_script != new_script

    old_promotion = compiler.promote_compiled_artifacts(old_build, tmp_path)
    assert old_promotion.commit() == []
    old_script_path = tmp_path / "scripts" / old_script
    new_script_path = tmp_path / "scripts" / new_script
    job = _persisted_job(
        hermes._cron_tool_arguments("create", old_compiled.payload),
        "existingjob1",
    )
    jobs = [job]
    monkeypatch.setattr(hermes, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        cron,
        "list_jobs",
        lambda include_disabled=True: list(jobs),
    )
    monkeypatch.setattr(
        cron,
        "get_job",
        lambda job_id: next(
            (candidate for candidate in jobs if candidate["id"] == job_id),
            None,
        ),
    )
    hermes._write_install_receipt(
        hermes_home=tmp_path,
        install_key="a" * 64,
        receipt=hermes._cron_receipt(
            action="created",
            job_id=job["id"],
            install_key="a" * 64,
            provenance=_provenance(),
        ),
    )

    # Publication promotes reviewed bytes before asking Cron to update. A
    # process death at that boundary must leave the old job's target immutable.
    compiler.promote_compiled_artifacts(new_build, tmp_path)
    assert old_script_path.read_text(encoding="utf-8") == "print('old revision')\n"
    assert new_script_path.read_text(encoding="utf-8") == "print('new revision')\n"

    def die_before_cron_update(arguments):
        assert arguments["action"] == "update"
        assert job["script"] == old_script
        assert old_script_path.read_text(encoding="utf-8") == "print('old revision')\n"
        raise SystemExit("simulated process death before Cron update")

    monkeypatch.setattr(hermes, "_dispatch_cron_tool", die_before_cron_update)
    with pytest.raises(SystemExit, match="before Cron update"):
        hermes.install_workflow(
            new_compiled.payload,
            hermes_home=tmp_path,
            provenance={
                **_provenance(),
                "install_idempotency_key": "b" * 64,
                "draft_revision": 2,
            },
        )

    assert job["script"] == old_script
    assert old_script_path.read_text(encoding="utf-8") == "print('old revision')\n"
    assert new_script_path.read_text(encoding="utf-8") == "print('new revision')\n"


def test_content_addressed_script_cleanup_waits_until_old_path_is_unreferenced(
    tmp_path, monkeypatch
):
    import cron

    old_workflow = _script_workflow("print('old revision')\n")
    new_workflow = _script_workflow("print('new revision')\n")
    old_compiled = compiler.compile_workflow(
        old_workflow,
        tmp_path,
        tmp_path / "old-build",
    )
    new_compiled = compiler.compile_workflow(
        new_workflow,
        tmp_path,
        tmp_path / "new-build",
    )
    for build_dir in (tmp_path / "old-build", tmp_path / "new-build"):
        promotion = compiler.promote_compiled_artifacts(build_dir, tmp_path)
        assert promotion.commit() == []

    old_script = old_compiled.payload["script"]
    new_script = new_compiled.payload["script"]
    old_script_path = tmp_path / "scripts" / old_script
    new_script_path = tmp_path / "scripts" / new_script
    jobs = [{"script": old_script}, {"script": new_script}]
    monkeypatch.setattr(
        cron,
        "list_jobs",
        lambda include_disabled=True: list(jobs),
    )

    hermes._remove_unreferenced_record_script(
        hermes_home=tmp_path,
        previous_script=old_script,
        current_script=new_script,
    )
    assert old_script_path.is_file()

    jobs[:] = [{"script": new_script}]
    hermes._remove_unreferenced_record_script(
        hermes_home=tmp_path,
        previous_script=old_script,
        current_script=new_script,
    )
    assert not old_script_path.exists()
    assert not old_script_path.parent.exists()
    assert new_script_path.is_file()


def test_content_addressed_promotion_never_overwrites_conflicting_bytes(tmp_path):
    workflow = _script_workflow("print('reviewed revision')\n")
    build_dir = tmp_path / "build"
    compiled = compiler.compile_workflow(workflow, tmp_path, build_dir)
    promotion = compiler.promote_compiled_artifacts(build_dir, tmp_path)
    assert promotion.commit() == []
    script_path = tmp_path / "scripts" / compiled.payload["script"]
    script_path.write_text("print('unexpected bytes')\n", encoding="utf-8")

    with pytest.raises(
        compiler.CompileError,
        match="does not match the reviewed content",
    ):
        compiler.promote_compiled_artifacts(build_dir, tmp_path)

    assert script_path.read_text(encoding="utf-8") == "print('unexpected bytes')\n"


def test_interrupted_install_with_ambiguous_jobs_fails_closed(tmp_path, monkeypatch):
    import cron

    monkeypatch.setattr(hermes, "get_hermes_home", lambda: tmp_path)
    payload = _payload()
    matching = {
        **payload,
        "schedule_display": payload["schedule"],
        "repeat": {"times": None, "completed": 0},
        "enabled": True,
        "state": "scheduled",
    }
    matching.pop("schedule")
    jobs = [
        {**matching, "id": "duplicate001"},
        {**matching, "id": "duplicate002"},
    ]
    monkeypatch.setattr(
        cron,
        "list_jobs",
        lambda include_disabled=True: list(jobs),
    )
    monkeypatch.setattr(
        cron,
        "get_job",
        lambda job_id: next(
            (job for job in jobs if job["id"] == job_id),
            None,
        ),
    )

    receipt_path = (
        tmp_path / "record" / "install-receipts" / f"{'a' * 64}.json"
    )
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_text(
        json.dumps(
            hermes._cron_install_intent(
                install_key="a" * 64,
                provenance=_provenance(),
                job_ids_before=set(),
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        hermes,
        "_dispatch_cron_tool",
        lambda _arguments: pytest.fail("ambiguous recovery must not create"),
    )

    with pytest.raises(
        hermes.HermesInstallError,
        match="More than one Hermes job matches",
    ):
        hermes.install_workflow(
            payload,
            hermes_home=tmp_path,
            provenance=_provenance(),
        )

    assert len(jobs) == 2


def test_replay_never_resumes_a_user_paused_job(tmp_path, monkeypatch):
    import cron

    payload = _payload()
    persisted = _persisted_job(
        hermes._cron_tool_arguments("create", payload),
        "pausedjob001",
    )
    persisted.update({"enabled": False, "state": "paused"})
    jobs = [persisted]
    dispatches = []
    monkeypatch.setattr(hermes, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        cron,
        "list_jobs",
        lambda include_disabled=True: list(jobs),
    )
    monkeypatch.setattr(
        cron,
        "get_job",
        lambda job_id: next(
            (job for job in jobs if job["id"] == job_id),
            None,
        ),
    )
    receipt = hermes._cron_receipt(
        action="created",
        job_id=persisted["id"],
        install_key="a" * 64,
        provenance=_provenance(),
    )
    hermes._write_install_receipt(
        hermes_home=tmp_path,
        install_key="a" * 64,
        receipt=receipt,
    )
    monkeypatch.setattr(
        hermes,
        "_dispatch_cron_tool",
        lambda arguments: dispatches.append(dict(arguments)),
    )

    with pytest.raises(hermes.HermesInstallError, match="is paused"):
        hermes.install_workflow(
            payload,
            hermes_home=tmp_path,
            provenance=_provenance(),
        )

    assert dispatches == []
    assert persisted["enabled"] is False
    assert persisted["state"] == "paused"


def test_real_host_cron_rollback_ignores_only_recomputed_next_run(
    tmp_path, monkeypatch
):
    from cron import get_job
    from cron.jobs import create_job, use_cron_store
    from tools.cronjob_tools import cronjob

    def dispatch(arguments):
        result = json.loads(cronjob(**arguments))
        assert result["success"] is True
        return result

    monkeypatch.setattr(hermes, "_dispatch_cron_tool", dispatch)
    with use_cron_store(tmp_path):
        previous = create_job(
            prompt="Original reviewed prompt",
            schedule="every 1h",
            name="Rollback proof",
            deliver="local",
            skills=[],
        )
        update = {
            "name": "Updated reviewed prompt",
            "prompt": "Updated reviewed prompt",
            "schedule": "every 2h",
            "deliver": "local",
            "skills": [],
            "no_agent": False,
        }
        previous_values = hermes._previous_cron_values(previous, update)
        dispatch(
            hermes._cron_tool_arguments(
                "update",
                update,
                job_id=previous["id"],
            )
        )

        hermes._rollback_cron_change(
            action="updated",
            job_id=previous["id"],
            previous=previous,
            previous_values=previous_values,
        )
        restored = get_job(previous["id"])

    assert isinstance(restored, dict)
    assert restored["next_run_at"] != previous["next_run_at"]
    assert hermes._cron_job_matches_previous(restored, previous)
