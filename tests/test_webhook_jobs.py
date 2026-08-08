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
def test_desktop_start_mode_keeps_webhooks_out_of_cron(run_mode, schedule, expected):
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
    monkeypatch.setattr(hermes, "_webhook_live_route_ready", lambda *_args: True)
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
    assert route["filters"] == [{"field": "customer.tier", "equals": "managed"}]
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


def test_replay_migrates_released_webhook_receipt_without_state(tmp_path, monkeypatch):
    hermes = _load_plugin_module("record.hermes")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(hermes, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(hermes, "_webhook_live_route_ready", lambda *_args: True)
    payload = {
        "name": "Paid invoice workflow",
        "prompt": "Handle the paid invoice.",
        "schedule": "",
        "deliver": "local",
        "no_agent": False,
        "skills": [],
    }
    provenance = {
        "workflow_id": "released-paid-invoice",
        "install_idempotency_key": "9" * 64,
    }
    installed = hermes.install_workflow(
        payload,
        hermes_home=tmp_path,
        provenance=provenance,
        webhook_trigger=_supported_trigger(),
    )
    subscriptions_path = tmp_path / "webhook_subscriptions.json"
    subscriptions_before = json.loads(subscriptions_path.read_text(encoding="utf-8"))
    receipt_path = tmp_path / "record" / "install-receipts" / f"{'9' * 64}.json"
    released_receipt = dict(installed["receipt"])
    released_receipt.pop("state")
    receipt_path.write_text(
        json.dumps(released_receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    replayed = hermes.install_workflow(
        payload,
        hermes_home=tmp_path,
        provenance=provenance,
        webhook_trigger=_supported_trigger(),
    )

    assert replayed["action"] == "replayed"
    assert replayed["idempotent_replay"] is True
    assert json.loads(subscriptions_path.read_text(encoding="utf-8")) == (
        subscriptions_before
    )
    migrated_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert migrated_receipt == {**released_receipt, "state": "installed"}


def test_receipt_failure_rolls_back_native_subscription(tmp_path, monkeypatch):
    hermes = _load_plugin_module("record.hermes")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(hermes, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(hermes, "_webhook_live_route_ready", lambda *_args: True)
    monkeypatch.setattr(
        hermes,
        "_replace_install_receipt",
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
    assert not (tmp_path / "record" / "install-receipts" / f"{'b' * 64}.json").exists()


def test_final_webhook_readback_failure_rolls_back_when_route_is_unchanged(
    tmp_path, monkeypatch
):
    hermes = _load_plugin_module("record.hermes")
    import hermes_cli.webhook as host_webhook

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(hermes, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(hermes, "_webhook_live_route_ready", lambda *_args: True)
    original_mutation = host_webhook.mutate_subscriptions
    calls = 0

    def fail_final_readback(mutator, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise host_webhook.WebhookSubscriptionsError(
                "simulated final readback failure"
            )
        return original_mutation(mutator, **kwargs)

    monkeypatch.setattr(host_webhook, "mutate_subscriptions", fail_final_readback)

    with pytest.raises(hermes.HermesInstallError, match="could not be read back"):
        hermes.install_workflow(
            {
                "name": "Readback workflow",
                "prompt": "Handle the event.",
                "schedule": "",
                "no_agent": False,
            },
            hermes_home=tmp_path,
            provenance={
                "workflow_id": "readback-workflow",
                "install_idempotency_key": "e" * 64,
            },
            webhook_trigger=_supported_trigger(),
        )

    subscriptions = json.loads(
        (tmp_path / "webhook_subscriptions.json").read_text(encoding="utf-8")
    )
    assert subscriptions == {}
    assert not (tmp_path / "record" / "install-receipts" / f"{'e' * 64}.json").exists()


def test_final_webhook_readback_failure_preserves_unproved_install_intent(
    tmp_path, monkeypatch
):
    hermes = _load_plugin_module("record.hermes")
    import hermes_cli.webhook as host_webhook

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(hermes, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(hermes, "_webhook_live_route_ready", lambda *_args: True)
    original_mutation = host_webhook.mutate_subscriptions
    calls = 0

    def fail_readback_and_rollback(mutator, **kwargs):
        nonlocal calls
        calls += 1
        if calls >= 3:
            raise host_webhook.WebhookSubscriptionsError(
                "simulated unresolved subscription state"
            )
        return original_mutation(mutator, **kwargs)

    monkeypatch.setattr(
        host_webhook,
        "mutate_subscriptions",
        fail_readback_and_rollback,
    )

    with pytest.raises(
        hermes.NativeReconciliationRequired,
        match="rollback requires reconciliation",
    ):
        hermes.install_workflow(
            {
                "name": "Uncertain webhook workflow",
                "prompt": "Handle the event.",
                "schedule": "",
                "no_agent": False,
            },
            hermes_home=tmp_path,
            provenance={
                "workflow_id": "uncertain-webhook-workflow",
                "install_idempotency_key": "f" * 64,
            },
            webhook_trigger=_supported_trigger(),
        )

    subscriptions = json.loads(
        (tmp_path / "webhook_subscriptions.json").read_text(encoding="utf-8")
    )
    assert len(subscriptions) == 1
    receipt_path = tmp_path / "record" / "install-receipts" / f"{'f' * 64}.json"
    interrupted = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert interrupted["state"] == "installing"
    assert interrupted["reconciliation_required"] is True


def test_installed_route_is_loaded_and_accepts_signed_event(tmp_path, monkeypatch):
    hermes = _load_plugin_module("record.hermes")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(hermes, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(hermes, "_webhook_live_route_ready", lambda *_args: True)
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
            signature = (
                "sha256="
                + hmac.new(
                    secret,
                    body,
                    hashlib.sha256,
                ).hexdigest()
            )
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


def test_retry_reconciles_route_persisted_before_webhook_receipt(tmp_path, monkeypatch):
    hermes = _load_plugin_module("record.hermes")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(hermes, "get_hermes_home", lambda: tmp_path)

    def crash_after_route(_base_url, _route_name):
        raise SystemExit("simulated webhook process crash")

    monkeypatch.setattr(hermes, "_webhook_live_route_ready", crash_after_route)
    payload = {
        "name": "Paid invoice workflow",
        "prompt": "Handle the paid invoice.",
        "schedule": "",
        "deliver": "local",
        "no_agent": False,
        "skills": [],
    }
    provenance = {
        "workflow_id": "paid-invoice-crash",
        "install_idempotency_key": "d" * 64,
    }

    with pytest.raises(SystemExit, match="simulated webhook process crash"):
        hermes.install_workflow(
            payload,
            hermes_home=tmp_path,
            provenance=provenance,
            webhook_trigger=_supported_trigger(),
        )

    receipt_path = tmp_path / "record" / "install-receipts" / f"{'d' * 64}.json"
    interrupted = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert interrupted["state"] == "installing"
    subscriptions_path = tmp_path / "webhook_subscriptions.json"
    before = json.loads(subscriptions_path.read_text(encoding="utf-8"))
    assert len(before) == 1

    monkeypatch.setattr(hermes, "_webhook_live_route_ready", lambda *_args: True)
    recovered = hermes.install_workflow(
        payload,
        hermes_home=tmp_path,
        provenance=provenance,
        webhook_trigger=_supported_trigger(),
    )

    after = json.loads(subscriptions_path.read_text(encoding="utf-8"))
    assert after == before
    assert recovered["action"] == "replayed"
    assert recovered["idempotent_replay"] is True
    finalized = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert finalized["state"] == "installed"
    assert finalized["action"] == "recovered"


@pytest.mark.parametrize("route_changed_after_intent", [False, True])
def test_webhook_update_recovery_compares_durable_prior_route(
    tmp_path, monkeypatch, route_changed_after_intent
):
    hermes = _load_plugin_module("record.hermes")
    import hermes_cli.webhook as host_webhook

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(hermes, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(hermes, "_webhook_live_route_ready", lambda *_args: True)
    workflow_id = "paid-invoice-update-crash"
    initial_payload = {
        "name": "Paid invoice workflow",
        "prompt": "Handle the original paid invoice workflow.",
        "schedule": "",
        "deliver": "local",
        "no_agent": False,
        "skills": [],
    }
    updated_payload = {
        **initial_payload,
        "prompt": "Handle the revised paid invoice workflow.",
    }
    initial_key = "1" * 64
    updated_key = "2" * 64
    hermes.install_workflow(
        initial_payload,
        hermes_home=tmp_path,
        provenance={
            "workflow_id": workflow_id,
            "install_idempotency_key": initial_key,
        },
        webhook_trigger=_supported_trigger(),
    )
    subscriptions_path = tmp_path / "webhook_subscriptions.json"
    subscriptions_before = json.loads(subscriptions_path.read_text(encoding="utf-8"))
    route_name = hermes._webhook_route_name(workflow_id)
    original_route = subscriptions_before[route_name]
    original_install = hermes.install_webhook_trigger

    def crash_after_intent_before_route(**kwargs):
        assert kwargs["expected_previous"] == original_route
        assert (
            kwargs["attempted_route"]["record"]["install_idempotency_key"]
            == updated_key
        )
        raise SystemExit("simulated crash before webhook mutation")

    monkeypatch.setattr(
        hermes,
        "install_webhook_trigger",
        crash_after_intent_before_route,
    )
    provenance = {
        "workflow_id": workflow_id,
        "install_idempotency_key": updated_key,
    }
    with pytest.raises(SystemExit, match="before webhook mutation"):
        hermes.install_workflow(
            updated_payload,
            hermes_home=tmp_path,
            provenance=provenance,
            webhook_trigger=_supported_trigger(),
        )

    receipt_path = tmp_path / "record" / "install-receipts" / f"{updated_key}.json"
    intent = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert intent["state"] == "installing"
    assert intent["route_name"] == route_name
    assert intent["previous_route"] == original_route
    assert (
        json.loads(subscriptions_path.read_text(encoding="utf-8"))[route_name]
        == original_route
    )

    monkeypatch.setattr(hermes, "install_webhook_trigger", original_install)
    if route_changed_after_intent:
        external_route = {**original_route, "description": "Externally changed"}

        def change_route(subscriptions):
            subscriptions[route_name] = external_route

        host_webhook.mutate_subscriptions(change_route)
        with pytest.raises(
            hermes.NativeReconciliationRequired,
            match="changed after this install intent",
        ):
            hermes.install_workflow(
                updated_payload,
                hermes_home=tmp_path,
                provenance=provenance,
                webhook_trigger=_supported_trigger(),
            )
        assert (
            json.loads(subscriptions_path.read_text(encoding="utf-8"))[route_name]
            == external_route
        )
        interrupted = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert interrupted["reconciliation_required"] is True
        return

    recovered = hermes.install_workflow(
        updated_payload,
        hermes_home=tmp_path,
        provenance=provenance,
        webhook_trigger=_supported_trigger(),
    )

    installed_route = json.loads(subscriptions_path.read_text(encoding="utf-8"))[
        route_name
    ]
    assert installed_route == intent["attempted_route"]
    assert installed_route["prompt"].startswith(updated_payload["prompt"])
    assert installed_route["secret"] == original_route["secret"]
    assert recovered["action"] == "replayed"
    assert recovered["idempotent_replay"] is True
    finalized = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert finalized["state"] == "installed"
    assert finalized["action"] == "recovered"
    assert "secret" not in finalized["webhook"]
