from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import crunch_recorder
from hermes_crunch import hermes


def test_built_dashboard_never_emits_unprotected_authorization_mode():
    source = (
        PLUGIN_ROOT / "dashboard" / "src" / "index.js"
    ).read_text(encoding="utf-8")
    built = (
        PLUGIN_ROOT / "dashboard" / "dist" / "index.js"
    ).read_text(encoding="utf-8")

    for content in (source, built):
        assert 'mode: "none"' not in content
        assert "hasBoundaryDefinitions" not in content
        assert 'mode: "human_in_loop"' in content


def test_configured_approval_target_requires_exec_approval_capability(
    tmp_path,
    monkeypatch,
):
    import agent.secret_scope as secret_scope
    import cron.scheduler as scheduler

    monkeypatch.setattr(
        scheduler,
        "cron_delivery_targets",
        lambda: [
            {
                "id": "signal",
                "home_env_var": "SIGNAL_HOME",
                "supports_exec_approval": False,
            },
            {
                "id": "telegram",
                "home_env_var": "TELEGRAM_HOME",
                "supports_exec_approval": True,
            },
        ],
    )
    monkeypatch.setattr(
        secret_scope,
        "get_secret",
        lambda key, default="": {
            "SIGNAL_HOME": "signal-room",
            "TELEGRAM_HOME": "telegram-room",
        }.get(key, default),
    )

    assert (
        crunch_recorder._configured_approval_target(
            home=tmp_path,
            expected_target="signal",
        )
        is None
    )
    assert crunch_recorder._configured_approval_target(
        home=tmp_path,
        expected_target="telegram",
    ) == (
        "telegram",
        {
            "platform": "telegram",
            "chat_id": "telegram-room",
            "thread_id": None,
        },
    )


def test_secondary_profile_webhook_is_rejected_without_blocking_schedule(
    tmp_path,
    monkeypatch,
):
    active_home = tmp_path / "active"
    selected_home = tmp_path / "secondary"
    monkeypatch.setenv("HERMES_HOME", str(active_home))
    monkeypatch.setattr(
        crunch_recorder,
        "_configured_approval_target",
        lambda **_kwargs: None,
    )

    status = crunch_recorder._webhook_install_status(home=selected_home)
    scheduled = crunch_recorder.crunch_install_readiness(
        home=selected_home,
        authorization=None,
        run_mode="schedule",
    )
    webhook = crunch_recorder.crunch_install_readiness(
        home=selected_home,
        authorization=None,
        run_mode="webhook",
    )

    assert status["ready"] is False
    assert "profile owned by this running Hermes gateway" in status["reason"]
    assert scheduled["ready"] is True
    assert webhook["ready"] is False
    assert [item["code"] for item in webhook["blockers"]] == [
        "webhook_receiver_unavailable"
    ]


def test_external_scheduler_rejects_human_authorized_workflow(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        crunch_recorder,
        "_authorization_scheduler_status",
        lambda **_kwargs: {
            "ready": False,
            "provider": "chronos",
            "reason": (
                "Protected Crunch workflows require Hermes' in-process "
                "Cron scheduler because external scheduler callbacks do not "
                "carry live gateway approval adapters."
            ),
        },
    )
    monkeypatch.setattr(
        crunch_recorder,
        "_configured_approval_target",
        lambda **_kwargs: (
            "telegram",
            {
                "platform": "telegram",
                "chat_id": "review-room",
                "thread_id": None,
            },
        ),
    )
    authorization = {
        "mode": "human_in_loop",
        "effects": [],
        "operations": [
            {
                "id": "send-result",
                "effect": "send",
                "description": "Send the reviewed result.",
                "tool": "terminal",
                "arguments": {"field": "command", "contains": "send-result"},
            }
        ],
        "checkpoints": [],
    }

    status = crunch_recorder.crunch_install_readiness(
        home=tmp_path,
        authorization=authorization,
    )

    assert status["ready"] is False
    assert status["requires_persistent_gateway"] is True
    assert status["persistent_gateway_ready"] is False
    assert status["authorization_scheduler"]["provider"] == "chronos"
    assert [item["code"] for item in status["blockers"]] == [
        "authorization_scheduler_unsupported"
    ]


def test_builtin_scheduler_without_live_gateway_rejects_protected_workflow(
    tmp_path,
    monkeypatch,
):
    import cron.scheduler_provider as scheduler_provider
    import gateway.status as gateway_status

    monkeypatch.setattr(
        scheduler_provider,
        "resolve_cron_scheduler",
        lambda: SimpleNamespace(name="builtin"),
    )
    monkeypatch.setattr(
        gateway_status,
        "resolve_gateway_liveness",
        lambda **_kwargs: SimpleNamespace(
            running=False,
            source="none",
        ),
    )

    status = crunch_recorder._authorization_scheduler_status(home=tmp_path)

    assert status["ready"] is False
    assert status["provider"] == "builtin"
    assert status["gateway_running"] is False
    assert "live Hermes messaging gateway" in status["reason"]


def test_webhook_install_rejects_secondary_profile_before_scoping(
    tmp_path,
    monkeypatch,
):
    from hermes_cli import webhook

    active_home = tmp_path / "active"
    secondary_home = tmp_path / "secondary"
    monkeypatch.setenv("HERMES_HOME", str(active_home))
    calls = []
    monkeypatch.setattr(
        webhook,
        "install_job_trigger_subscription",
        lambda **kwargs: calls.append(kwargs),
    )

    try:
        hermes.install_webhook_trigger(
            hermes_home=secondary_home,
            name="route",
            job_id="job-1",
            description="event",
            events=["event"],
        )
    except hermes.HermesInstallError as exc:
        assert "profile owned" in str(exc)
    else:
        raise AssertionError("secondary profile webhook install was accepted")
    assert calls == []


def test_webhook_install_rejects_shared_multiplex_listener(
    tmp_path,
    monkeypatch,
):
    from agent.secret_scope import set_multiplex_active
    from hermes_cli import webhook

    active_home = tmp_path / "active"
    monkeypatch.setenv("HERMES_HOME", str(active_home))
    calls = []
    monkeypatch.setattr(
        webhook,
        "install_job_trigger_subscription",
        lambda **kwargs: calls.append(kwargs),
    )
    set_multiplex_active(True)
    try:
        try:
            hermes.install_webhook_trigger(
                hermes_home=active_home,
                name="route",
                job_id="job-1",
                description="event",
                events=["event"],
            )
        except hermes.HermesInstallError as exc:
            assert "multiplex" in str(exc)
        else:
            raise AssertionError("multiplex webhook install was accepted")
    finally:
        set_multiplex_active(False)
    assert calls == []
