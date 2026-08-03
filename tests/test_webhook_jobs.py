from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib
import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


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
            "schedule": "",
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
        ("webhook", "", ("webhook", "")),
    ],
)
def test_desktop_start_mode_keeps_webhooks_out_of_cron(
    run_mode, schedule, expected
):
    plugin_api = _load_plugin_module("dashboard.plugin_api")

    assert plugin_api._start_mode(run_mode, schedule) == expected


def test_desktop_webhook_status_reports_host_capability_gap(tmp_path, monkeypatch):
    plugin_api = _load_plugin_module("dashboard.plugin_api")
    unavailable = types.SimpleNamespace(
        webhook_runtime_status=lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("Webhook subscriptions are unavailable")
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
    assert status["reason"] == "Webhook subscriptions are unavailable"


def test_install_creates_native_subscription_without_cron(tmp_path, monkeypatch):
    hermes = _load_plugin_module("record.hermes")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(hermes, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        hermes,
        "_dispatch_cron_tool",
        lambda _arguments: pytest.fail("webhook install must not call Cron"),
    )

    result = hermes.install_workflow(
        {
            "name": "Paid invoice workflow",
            "prompt": "Handle the paid invoice.",
            "schedule": "",
            "deliver": "local",
            "no_agent": False,
            "skills": [],
        },
        hermes_home=tmp_path,
        provenance={
            "workflow_id": "paid-invoice",
            "install_idempotency_key": "a" * 64,
        },
        webhook_trigger=_supported_trigger(),
    )

    assert result["job_id"] is None
    assert result["webhook"]["secret"]
    subscriptions = json.loads(
        (tmp_path / "webhook_subscriptions.json").read_text(encoding="utf-8")
    )
    route = subscriptions[result["webhook"]["name"]]
    assert route["events"] == ["invoice.paid"]
    assert route["filters"] == [
        {"field": "customer.tier", "equals": "managed"}
    ]
    assert route["deliver"] == "log"
    assert "{__raw__}" in route["prompt"]
    assert route["record"]["workflow_id"] == "paid-invoice"
    receipt = json.loads(
        (tmp_path / "record" / "install-receipts" / f"{'a' * 64}.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt["kind"] == "webhook"
    assert receipt["webhook"]["url"].endswith(result["webhook"]["name"])
    assert "secret" not in receipt["webhook"]


def test_receipt_failure_rolls_back_native_subscription(tmp_path, monkeypatch):
    hermes = _load_plugin_module("record.hermes")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(hermes, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        hermes,
        "_write_install_receipt",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("receipt failed")),
    )

    with pytest.raises(RuntimeError, match="receipt failed"):
        hermes.install_workflow(
            {
                "name": "Rollback workflow",
                "prompt": "Handle the event.",
                "schedule": "",
                "no_agent": False,
            },
            hermes_home=tmp_path,
            provenance={
                "workflow_id": "rollback-workflow",
                "install_idempotency_key": "b" * 64,
            },
            webhook_trigger=_supported_trigger(),
        )

    subscriptions_path = tmp_path / "webhook_subscriptions.json"
    assert json.loads(subscriptions_path.read_text(encoding="utf-8")) == {}
    assert not (
        tmp_path / "record" / "install-receipts" / f"{'b' * 64}.json"
    ).exists()


def test_installed_route_is_loaded_and_accepts_signed_event(tmp_path, monkeypatch):
    hermes = _load_plugin_module("record.hermes")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(hermes, "get_hermes_home", lambda: tmp_path)
    result = hermes.install_workflow(
        {
            "name": "Paid invoice workflow",
            "prompt": "Handle the paid invoice.",
            "schedule": "",
            "deliver": "local",
            "no_agent": False,
            "skills": [],
        },
        hermes_home=tmp_path,
        provenance={
            "workflow_id": "paid-invoice",
            "install_idempotency_key": "c" * 64,
        },
        webhook_trigger=_supported_trigger(),
    )

    from gateway.config import PlatformConfig
    from gateway.platforms.webhook import WebhookAdapter

    async def exercise_route():
        adapter = WebhookAdapter(
            PlatformConfig(
                enabled=True,
                extra={"host": "127.0.0.1", "port": 0, "routes": {}},
            )
        )
        adapter._reload_dynamic_routes()
        adapter.handle_message = AsyncMock()
        app = web.Application(client_max_size=adapter._max_body_bytes)
        app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            body = json.dumps(
                {
                    "event_type": "invoice.paid",
                    "customer": {"tier": "managed"},
                    "invoice_id": "inv-123",
                }
            ).encode()
            secret = result["webhook"]["secret"].encode()
            signature = "sha256=" + hmac.new(
                secret,
                body,
                hashlib.sha256,
            ).hexdigest()
            response = await client.post(
                f"/webhooks/{result['webhook']['name']}",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": signature,
                    "X-GitHub-Event": "invoice.paid",
                    "X-GitHub-Delivery": "delivery-123",
                },
            )
            assert response.status == 202
            assert (await response.json())["status"] == "accepted"
            await asyncio.sleep(0)
            event = adapter.handle_message.await_args.args[0]
            assert "Handle the paid invoice." in event.text
            assert '"invoice_id": "inv-123"' in event.text
        finally:
            await client.close()

    asyncio.run(exercise_route())
