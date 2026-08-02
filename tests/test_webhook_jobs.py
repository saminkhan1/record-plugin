from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "record_test_plugin"


def _load_plugin_module(relative_name: str):
    if PACKAGE_NAME not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            PACKAGE_NAME,
            PLUGIN_ROOT / "__init__.py",
            submodule_search_locations=[str(PLUGIN_ROOT)],
        )
        assert spec is not None and spec.loader is not None
        package = importlib.util.module_from_spec(spec)
        sys.modules[PACKAGE_NAME] = package
        spec.loader.exec_module(package)
    return importlib.import_module(f"{PACKAGE_NAME}.{relative_name}")


def _plan(trigger):
    return {
        "steps": ["Handle the paid invoice.", "Verify the result."],
        "trigger": trigger,
        "job_spec": {
            "name": "Handle paid invoice",
            "schedule": "triggered",
            "deliver": "local",
            "no_agent": False,
            "prompt": "Use the current signed event payload and verify the result.",
            "skills": [],
        },
        "script_source": None,
        "warnings": [],
    }


def _supported_trigger():
    return {
        "requested": True,
        "feasibility": "supported",
        "description": "When an invoice is paid",
        "source": "billing system",
        "events": ["invoice.paid"],
        "filters": [{"field": "customer.tier", "equals": "managed"}],
        "reason": "The sender can sign a JSON webhook request.",
    }


def test_supported_webhook_trigger_normalizes_reviewed_filter():
    compiler = _load_plugin_module("record.compiler")
    trigger = _supported_trigger()

    result = compiler.validate_draft_plan(_plan(trigger))

    assert result["trigger"] == {
        **trigger,
        "filters": {"all": trigger["filters"]},
    }


@pytest.mark.parametrize(
    "run_mode,schedule,expected",
    [
        ("schedule", "weekdays at 9am", ("schedule", "weekdays at 9am")),
        ("webhook", "", ("webhook", "triggered")),
    ],
)
def test_desktop_start_mode_maps_webhooks_to_triggered_cron(
    run_mode, schedule, expected
):
    plugin_api = _load_plugin_module("dashboard.plugin_api")

    assert plugin_api._start_mode(run_mode, schedule) == expected


def test_desktop_webhook_status_reports_host_capability_gap(tmp_path, monkeypatch):
    plugin_api = _load_plugin_module("dashboard.plugin_api")
    unavailable = types.SimpleNamespace(
        webhook_runtime_status=lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("Cron webhook triggers are unavailable")
        )
    )
    monkeypatch.setattr(
        plugin_api,
        "_cron_profile_home",
        lambda _profile: ("default", tmp_path),
    )
    monkeypatch.setattr(
        plugin_api,
        "load_record_modules",
        lambda: {"hermes": unavailable},
    )

    status = plugin_api.get_webhook_status("default")

    assert status["ready"] is False
    assert status["configured"] is False
    assert status["reason"] == "Cron webhook triggers are unavailable"


def test_install_creates_triggered_job_then_route_then_nonsecret_receipt(
    tmp_path, monkeypatch
):
    hermes = _load_plugin_module("record.hermes")
    calls = []
    job = {
        "id": "job-123",
        "name": "Paid invoice workflow",
        "schedule": "triggered",
        "schedule_display": "triggered",
    }

    monkeypatch.setattr(hermes, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        hermes,
        "_dispatch_cron_tool",
        lambda arguments: calls.append(("cron", arguments))
        or {"success": True, "job_id": "job-123", "job": job},
    )
    monkeypatch.setattr(
        hermes,
        "install_webhook_trigger",
        lambda **kwargs: calls.append(("webhook", kwargs))
        or {
            "name": "record-paid-invoice-job-123",
            "url": "https://hooks.example.test/webhooks/record-paid-invoice-job-123",
            "secret": "one-time-secret",
            "events": ["invoice.paid"],
            "job_id": "job-123",
        },
    )
    monkeypatch.setitem(
        sys.modules,
        "cron",
        types.SimpleNamespace(
            list_jobs=lambda include_disabled=True: [],
            get_job=lambda job_id: job if job_id == "job-123" else None,
        ),
    )

    result = hermes.install_cron_job(
        {
            "name": job["name"],
            "prompt": "Handle the paid invoice.",
            "schedule": "triggered",
        },
        hermes_home=tmp_path,
        provenance={
            "workflow_id": "paid-invoice",
            "install_idempotency_key": "a" * 64,
        },
        webhook_trigger=_supported_trigger(),
    )

    assert calls[0][0] == "cron"
    assert calls[0][1]["action"] == "create"
    assert calls[0][1]["schedule"] == "triggered"
    assert calls[1][0] == "webhook"
    assert result["webhook"]["secret"] == "one-time-secret"
    receipt = json.loads(
        (tmp_path / "record" / "install-receipts" / f"{'a' * 64}.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt["webhook"]["url"].startswith("https://hooks.example.test/")
    assert "secret" not in receipt["webhook"]


def test_route_failure_rolls_back_new_cron_job(tmp_path, monkeypatch):
    hermes = _load_plugin_module("record.hermes")
    calls = []
    job = {
        "id": "job-rollback",
        "name": "Rollback workflow",
        "schedule": "triggered",
        "schedule_display": "triggered",
    }

    monkeypatch.setattr(hermes, "get_hermes_home", lambda: tmp_path)

    def dispatch(arguments):
        calls.append(arguments)
        if arguments["action"] == "create":
            return {"success": True, "job_id": job["id"], "job": job}
        return {"success": True}

    monkeypatch.setattr(hermes, "_dispatch_cron_tool", dispatch)
    monkeypatch.setattr(
        hermes,
        "install_webhook_trigger",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("route failed")),
    )
    monkeypatch.setitem(
        sys.modules,
        "cron",
        types.SimpleNamespace(
            list_jobs=lambda include_disabled=True: [],
            get_job=lambda job_id: job if job_id == job["id"] else None,
        ),
    )

    with pytest.raises(RuntimeError, match="route failed"):
        hermes.install_cron_job(
            {
                "name": job["name"],
                "prompt": "Handle the event.",
                "schedule": "triggered",
            },
            hermes_home=tmp_path,
            provenance={
                "workflow_id": "rollback-workflow",
                "install_idempotency_key": "b" * 64,
            },
            webhook_trigger=_supported_trigger(),
        )

    assert [call["action"] for call in calls] == ["create", "remove"]
    assert not (
        tmp_path / "record" / "install-receipts" / f"{'b' * 64}.json"
    ).exists()
