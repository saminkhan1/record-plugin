"""Record standalone plugin."""

from inspect import signature


def _bind_agent_run_contract(run) -> None:
    """Prove the host facade accepts Record's exact drafting call shape."""

    signature(run).bind(
        user_message="host compatibility probe",
        system_prompt="host compatibility probe",
        task_id="record:host-compatibility",
        max_iterations=1,
        tool_progress_callback=None,
        status_callback=None,
        required_tools=["record_submit_draft"],
        allowed_tools=["record_submit_draft"],
        full_capabilities=False,
        purpose="workflow.draft",
    )


def _require_host_features(ctx) -> None:
    """Fail closed when the installed host lacks Record's concrete APIs."""

    try:
        agent = ctx.agent
        from hermes_cli.webhook import mutate_subscriptions
    except (AttributeError, ImportError) as exc:
        raise RuntimeError(
            "Record requires a newer Wexpro Agent host with plugin agent runs "
            "and atomic webhook subscription updates. Update Wexpro Agent "
            "before enabling Record."
        ) from exc
    run = getattr(agent, "run", None)
    if not callable(run) or not callable(mutate_subscriptions):
        raise RuntimeError(
            "Record cannot use the installed Wexpro Agent host APIs. Update "
            "Wexpro Agent before enabling Record."
        )
    try:
        _bind_agent_run_contract(run)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "Record cannot use the installed Wexpro Agent host APIs. Update "
            "Wexpro Agent before enabling Record."
        ) from exc


def register(ctx):
    """Bind Hermes-owned plugin services used by the Record backend."""

    _require_host_features(ctx)
    from .record.runtime import set_plugin_context
    from .record.planner import register as register_planner

    set_plugin_context(ctx)
    register_planner(ctx)
