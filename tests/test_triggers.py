from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from hermes_crunch import compiler


def _plan(trigger):
    return {
        "selection_reason": "Use the reviewed native execution.",
        "state_strategy": "not_required",
        "idempotency_strategy": "Deduplicate by webhook delivery id.",
        "steps": ["Handle the newly opened issue and verify the result."],
        "approval_steps": [],
        "trigger": trigger,
        "execution": {
            "no_agent": False,
            "script": None,
            "prompt": "Use the current webhook issue payload and verify the result.",
            "skills": [],
            "enabled_toolsets": [],
            "requirements": {"commands": [], "network_origins": [], "apps": [], "files": []},
            "authorization": {
                "mode": "human_in_loop",
                "effects": [],
                "operations": [],
                "checkpoints": [],
            },
        },
        "warnings": [],
    }


def test_supported_webhook_trigger_is_validated_and_preserved():
    trigger = {
        "requested": True,
        "feasibility": "supported",
        "description": "When a GitHub issue is opened",
        "source": "GitHub",
        "events": ["issues"],
        "filters": [{"field": "action", "equals": "opened"}],
        "reason": "GitHub can sign this event with X-Hub-Signature-256.",
    }

    result = compiler.validate_draft_plan(_plan(trigger), available_toolsets=set())

    assert result["trigger"] == {
        **trigger,
        "filters": {"all": trigger["filters"]},
    }


@pytest.mark.parametrize(
    "filters",
    [
        {"field": "action", "exists": "yes"},
        {"field": "action", "missing": False},
        {"field": "action", "regex": "["},
    ],
)
def test_trigger_filters_use_the_runtime_filter_contract(filters):
    trigger = {
        "requested": True,
        "feasibility": "supported",
        "description": "When a GitHub issue is opened",
        "source": "GitHub",
        "events": ["issues"],
        "filters": filters,
        "reason": "GitHub can sign this event with X-Hub-Signature-256.",
    }

    with pytest.raises(compiler.CompileError, match="unsupported declarative"):
        compiler.validate_draft_plan(_plan(trigger), available_toolsets=set())


def test_unsupported_trigger_must_not_smuggle_installable_route_fields():
    trigger = {
        "requested": True,
        "feasibility": "unsupported",
        "description": "When a local Finder folder changes",
        "source": "Finder",
        "events": ["file.changed"],
        "filters": None,
        "reason": "Finder cannot send an authenticated webhook event.",
    }

    with pytest.raises(compiler.CompileError, match="cannot install events or filters"):
        compiler.validate_draft_plan(_plan(trigger), available_toolsets=set())


def test_job_trigger_subscription_is_profile_scoped_and_hmac_protected(tmp_path, monkeypatch):
    from cron.jobs import create_job
    from hermes_cli import webhook

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(webhook, "_is_webhook_enabled", lambda: True)
    monkeypatch.setattr(webhook, "_get_webhook_base_url", lambda: "https://hooks.example.test")
    job = create_job("Handle the event.", "triggered", name="issue workflow")

    installed = webhook.install_job_trigger_subscription(
        hermes_home=tmp_path,
        name="crunch-issue-workflow-abc123",
        job_id=job["id"],
        description="When a GitHub issue is opened",
        events=["issues"],
        filters=[{"field": "action", "equals": "opened"}],
    )

    stored = json.loads((tmp_path / "webhook_subscriptions.json").read_text(encoding="utf-8"))
    route = stored[installed["name"]]
    assert installed["url"] == "https://hooks.example.test/webhooks/crunch-issue-workflow-abc123"
    assert installed["secret"] == route["secret"]
    assert route["trigger_job_id"] == job["id"]
    assert "job_home" not in route
    assert route["filters"] == [{"field": "action", "equals": "opened"}]
    assert (tmp_path / "webhook_subscriptions.json").stat().st_mode & 0o777 == 0o600
