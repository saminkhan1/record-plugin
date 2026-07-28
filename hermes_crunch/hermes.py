from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from hermes_constants import reset_hermes_home_override, set_hermes_home_override


class HermesInstallError(RuntimeError):
    pass


@contextmanager
def _profile_cron_scope(hermes_home: Path) -> Iterator[Path]:
    """Run Hermes cron code against one profile's HERMES_HOME."""

    home = hermes_home.expanduser().resolve()
    token = set_hermes_home_override(str(home))
    from agent.secret_scope import (
        build_profile_secret_scope,
        reset_secret_scope,
        set_secret_scope,
    )

    secret_token = set_secret_scope(build_profile_secret_scope(home))
    try:
        from cron.jobs import use_cron_store

        with use_cron_store(home):
            yield home
    finally:
        reset_secret_scope(secret_token)
        reset_hermes_home_override(token)


def install_cron_job(
    payload: dict[str, Any],
    *,
    hermes_home: Path,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with _profile_cron_scope(hermes_home):
        from cron.jobs import create_reserved_job
        from .authorization import PROVIDER_ID

        params = dict(payload)
        authorization = params.pop("authorization", None)
        params.setdefault("prompt", None)
        origin = (
            dict(params["origin"])
            if isinstance(params.get("origin"), dict)
            else {}
        )
        origin["provenance"] = dict(provenance or {})
        params["origin"] = origin
        install_key = str(
            (provenance or {}).get("install_idempotency_key") or ""
        ).strip()
        if not install_key:
            raise HermesInstallError("Approved Cron install is missing its key")
        job = create_reserved_job(
            params,
            idempotency_key=install_key,
            authorization=(
                {"provider": PROVIDER_ID, "policy": authorization}
                if authorization is not None
                else None
            ),
        )
        return {
            "success": True,
            "job_id": str(job.get("id") or ""),
            "idempotent_replay": bool(job.get("_idempotent_replay")),
            "job": job,
        }


def activate_cron_job(job_id: str, install_key: str, *, hermes_home: Path) -> dict[str, Any]:
    with _profile_cron_scope(hermes_home):
        from cron.jobs import activate_reserved_job

        job = activate_reserved_job(job_id, idempotency_key=install_key)
        if job is None:
            raise HermesInstallError("Reserved Cron job disappeared before activation")
        return job


def abort_cron_job(job_id: str, install_key: str, *, hermes_home: Path) -> bool:
    """Remove only a still-disabled reservation after an install failure."""

    with _profile_cron_scope(hermes_home):
        from cron.jobs import abort_reserved_job

        return abort_reserved_job(job_id, idempotency_key=install_key)


def install_webhook_trigger(
    *,
    hermes_home: Path,
    name: str,
    job_id: str,
    description: str,
    events: list[str],
    filters: list[dict[str, Any]] | dict[str, Any] | None = None,
) -> dict[str, Any]:
    from agent.secret_scope import is_multiplex_active
    from gateway.config import load_gateway_config
    from hermes_constants import get_hermes_home
    from hermes_cli.webhook import install_job_trigger_subscription

    target_home = hermes_home.expanduser().resolve()
    listener_home = get_hermes_home().expanduser().resolve()
    if target_home != listener_home:
        raise HermesInstallError(
            "Webhook routes can be installed only for the profile owned by "
            "this running Hermes gateway"
        )
    listener_config = load_gateway_config()
    if is_multiplex_active() or listener_config.multiplex_profiles:
        raise HermesInstallError(
            "Webhook routes cannot be installed on a shared multiplex listener"
        )

    with _profile_cron_scope(target_home):
        return install_job_trigger_subscription(
            hermes_home=target_home,
            name=name,
            job_id=job_id,
            description=description,
            events=events,
            filters=filters,
        )


def remove_webhook_trigger(
    *,
    hermes_home: Path,
    name: str,
    job_id: str,
) -> bool:
    """Remove only a route still owned by the expected Crunch job."""

    from hermes_cli.webhook import remove_job_trigger_subscription

    with _profile_cron_scope(hermes_home):
        return remove_job_trigger_subscription(
            hermes_home=hermes_home,
            name=name,
            job_id=job_id,
        )
