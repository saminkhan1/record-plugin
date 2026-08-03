from __future__ import annotations

import json

import pytest

from hermes_plugins.record.record import hermes


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
        job = {
            key: value
            for key, value in arguments.items()
            if key not in {"action", "schedule"}
        }
        job["id"] = "normalcreate1"
        job["schedule_display"] = arguments["schedule"]
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


def test_retry_recovers_job_persisted_before_final_receipt(tmp_path, monkeypatch):
    import cron
    from cron.jobs import parse_schedule

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
        job = {
            key: value
            for key, value in arguments.items()
            if key not in {"action", "schedule"}
        }
        job["id"] = "crashwindow1"
        job["schedule_display"] = parse_schedule(arguments["schedule"])["display"]
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


def test_interrupted_install_with_ambiguous_jobs_fails_closed(tmp_path, monkeypatch):
    import cron

    monkeypatch.setattr(hermes, "get_hermes_home", lambda: tmp_path)
    payload = _payload()
    matching = {
        **payload,
        "schedule_display": payload["schedule"],
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
    monkeypatch.setattr(cron, "get_job", lambda _job_id: None)

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
