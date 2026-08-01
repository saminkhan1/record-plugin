from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home


class HermesInstallError(RuntimeError):
    pass


log = logging.getLogger(__name__)

_CRON_TOOL_FIELDS = (
    "prompt",
    "schedule",
    "name",
    "deliver",
    "skills",
    "script",
    "context_from",
    "enabled_toolsets",
    "workdir",
    "no_agent",
)


def _require_active_profile_home(hermes_home: Path) -> Path:
    """Reject attempts to cross the profile boundary chosen by Hermes."""

    home = hermes_home.expanduser().resolve()
    active = get_hermes_home().expanduser().resolve()
    if home != active:
        raise HermesInstallError(
            "Record cannot install a job outside the active Hermes profile"
        )
    return home


def _cron_tool_arguments(
    action: str,
    params: dict[str, Any] | None = None,
    *,
    job_id: str = "",
) -> dict[str, Any]:
    arguments: dict[str, Any] = {"action": action}
    if job_id:
        arguments["job_id"] = job_id
    for field in _CRON_TOOL_FIELDS:
        if params is None or field not in params:
            continue
        value = params[field]
        if action == "create" and value is None:
            continue
        if action == "update" and value is None:
            if field in {"prompt", "script", "workdir"}:
                value = ""
            elif field in {"skills", "context_from", "enabled_toolsets"}:
                value = []
            else:
                continue
        arguments[field] = value
    return arguments


def _dispatch_cron_tool(arguments: dict[str, Any]) -> dict[str, Any]:
    from .runtime import dispatch_tool

    raw = dispatch_tool("cronjob", arguments)
    if isinstance(raw, str):
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HermesInstallError(
                "Hermes Cron returned an unreadable result"
            ) from exc
    elif isinstance(raw, dict):
        result = raw
    else:
        raise HermesInstallError("Hermes Cron returned an unsupported result")
    if result.get("success") is not True:
        raise HermesInstallError(
            str(result.get("error") or "Hermes Cron rejected the job")
        )
    return result


def _job_id_from_result(result: dict[str, Any], fallback: str = "") -> str:
    job = result.get("job") if isinstance(result.get("job"), dict) else {}
    return str(
        result.get("job_id")
        or job.get("job_id")
        or job.get("id")
        or fallback
    ).strip()


def _install_receipts_dir(hermes_home: Path) -> Path:
    return hermes_home.expanduser().resolve() / "record" / "install-receipts"


def _read_install_receipt(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _workflow_job_ids_from_receipts(
    *,
    hermes_home: Path,
    workflow_id: str,
    jobs_by_id: dict[str, dict[str, Any]],
) -> set[str]:
    matches: set[str] = set()
    receipts_dir = _install_receipts_dir(hermes_home)
    if not receipts_dir.is_dir():
        return matches
    for path in receipts_dir.glob("*.json"):
        receipt = _read_install_receipt(path)
        provenance = (
            receipt.get("provenance")
            if isinstance(receipt, dict)
            and isinstance(receipt.get("provenance"), dict)
            else {}
        )
        job_id = str(receipt.get("job_id") or "") if receipt else ""
        if (
            receipt
            and receipt.get("source") == "record"
            and provenance.get("workflow_id") == workflow_id
            and job_id in jobs_by_id
        ):
            matches.add(job_id)
    return matches


def _previous_cron_values(
    job: dict[str, Any],
    changed: dict[str, Any],
) -> dict[str, Any]:
    previous: dict[str, Any] = {}
    for field in changed:
        if field not in _CRON_TOOL_FIELDS:
            continue
        if field == "schedule":
            previous[field] = job.get("schedule_display")
        elif field == "skills":
            previous[field] = list(job.get("skills") or [])
        else:
            previous[field] = job.get(field)
    return previous


def webhook_runtime_status(*, hermes_home: Path) -> dict[str, Any]:
    """Return the host-owned, non-secret webhook receiver status."""

    home = _require_active_profile_home(hermes_home)
    try:
        from hermes_cli.webhook import webhook_runtime_status as host_status
    except ImportError as exc:
        raise HermesInstallError(
            "This Hermes version does not support Cron webhook triggers"
        ) from exc
    return host_status(hermes_home=home)


def _webhook_route_name(workflow_id: str, job_id: str) -> str:
    workflow = "".join(
        character
        if character.isalnum() or character in {"-", "_"}
        else "-"
        for character in workflow_id.strip().lower()
    ).strip("-_")
    workflow = workflow or "workflow"
    return f"record-{workflow[:32]}-{job_id.strip().lower()[:12]}"


def install_webhook_trigger(
    *,
    hermes_home: Path,
    workflow_id: str,
    job_id: str,
    trigger: dict[str, Any],
) -> dict[str, Any]:
    """Bind one reviewed event definition to its host-owned Cron job."""

    home = _require_active_profile_home(hermes_home)
    try:
        from hermes_cli.webhook import install_job_trigger_subscription
    except ImportError as exc:
        raise HermesInstallError(
            "This Hermes version does not support Cron webhook triggers"
        ) from exc
    return install_job_trigger_subscription(
        hermes_home=home,
        name=_webhook_route_name(workflow_id, job_id),
        job_id=job_id,
        description=str(
            trigger.get("description") or "Reviewed Record workflow"
        ),
        events=[
            str(item)
            for item in trigger.get("events") or []
            if str(item).strip()
        ],
        filters=trigger.get("filters"),
    )


def remove_webhook_trigger(
    *,
    hermes_home: Path,
    name: str,
    job_id: str,
) -> bool:
    """Remove only the route still bound to the expected Cron job."""

    home = _require_active_profile_home(hermes_home)
    try:
        from hermes_cli.webhook import remove_job_trigger_subscription
    except ImportError as exc:
        raise HermesInstallError(
            "This Hermes version does not support Cron webhook triggers"
        ) from exc
    return remove_job_trigger_subscription(
        hermes_home=home,
        name=name,
        job_id=job_id,
    )


def _rollback_cron_change(
    *,
    action: str,
    job_id: str,
    previous: dict[str, Any] | None,
    previous_values: dict[str, Any] | None,
) -> None:
    if action == "created" and job_id:
        _dispatch_cron_tool({"action": "remove", "job_id": job_id})
    elif action == "updated" and job_id and previous_values is not None:
        _dispatch_cron_tool(
            _cron_tool_arguments(
                "update",
                previous_values,
                job_id=job_id,
            )
        )
        if (
            previous is not None
            and previous.get("enabled") is False
            and previous.get("state") != "paused"
        ):
            _dispatch_cron_tool({"action": "pause", "job_id": job_id})


def install_cron_job(
    payload: dict[str, Any],
    *,
    hermes_home: Path,
    provenance: dict[str, Any] | None = None,
    webhook_trigger: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create or update one canonical Hermes Cron job from a reviewed build.

    Record keeps only provenance and a review receipt. Hermes remains the
    authority for job identity, schedule parsing, persistence, execution, run
    sessions, delivery, and provider/model resolution.
    """

    hermes_home = _require_active_profile_home(hermes_home)
    params = {
        key: value
        for key, value in dict(payload).items()
        if key in _CRON_TOOL_FIELDS
    }
    if not str(params.get("schedule") or "").strip():
        raise HermesInstallError("Reviewed build is missing its Hermes schedule")
    params.setdefault("prompt", None)
    params.setdefault("deliver", "local")

    provenance_value = dict(provenance or {})
    install_key = str(
        provenance_value.get("install_idempotency_key") or ""
    ).strip()
    if (
        len(install_key) != 64
        or any(character not in "0123456789abcdef" for character in install_key)
    ):
        raise HermesInstallError(
            "Reviewed Cron install is missing its SHA-256 key"
        )
    workflow_id = str(provenance_value.get("workflow_id") or "").strip()
    if not workflow_id:
        raise HermesInstallError("Reviewed Cron install is missing its workflow ID")
    from cron import get_job, list_jobs

    jobs = list_jobs(include_disabled=True)
    jobs_by_id = {
        str(candidate.get("id") or ""): candidate
        for candidate in jobs
        if str(candidate.get("id") or "")
    }
    receipt_path = _install_receipts_dir(hermes_home) / f"{install_key}.json"
    existing_receipt = (
        _read_install_receipt(receipt_path)
        if receipt_path.is_file()
        else None
    )
    if receipt_path.is_file() and existing_receipt is None:
        raise HermesInstallError("The existing install receipt is unreadable")
    if existing_receipt is not None:
        existing_job_id = str(existing_receipt.get("job_id") or "")
        if (
            existing_receipt.get("source") != "record"
            or existing_receipt.get("install_idempotency_key") != install_key
            or existing_receipt.get("provenance") != provenance_value
        ):
            raise HermesInstallError(
                "This reviewed build key conflicts with an existing receipt"
            )
        existing_job = jobs_by_id.get(existing_job_id)
        if existing_job is None:
            raise HermesInstallError(
                "The Hermes job installed for this reviewed build was removed; "
                "create a new reviewed revision before reinstalling"
            )
        webhook = (
            install_webhook_trigger(
                hermes_home=hermes_home,
                workflow_id=workflow_id,
                job_id=existing_job_id,
                trigger=webhook_trigger,
            )
            if webhook_trigger is not None
            else None
        )
        return {
            "success": True,
            "action": "replayed",
            "job_id": existing_job_id,
            "idempotent_replay": True,
            "job": existing_job,
            "receipt": existing_receipt,
            "webhook": webhook,
        }

    workflow_job_ids = _workflow_job_ids_from_receipts(
        hermes_home=hermes_home,
        workflow_id=workflow_id,
        jobs_by_id=jobs_by_id,
    )
    # Recognize jobs produced before receipts became the sole Record
    # provenance store, but never write new plugin metadata into Cron.
    for candidate in jobs:
        candidate_origin = candidate.get("origin")
        candidate_provenance = (
            candidate_origin.get("provenance")
            if isinstance(candidate_origin, dict)
            else None
        )
        if (
            not isinstance(candidate_origin, dict)
            or candidate_origin.get("source") != "record"
            or not isinstance(candidate_provenance, dict)
        ):
            continue
        if candidate_provenance.get("workflow_id") == workflow_id:
            candidate_job_id = str(candidate.get("id") or "")
            if candidate_job_id:
                workflow_job_ids.add(candidate_job_id)
    if len(workflow_job_ids) > 1:
        raise HermesInstallError(
            "More than one Record job claims this workflow"
        )

    if workflow_job_ids:
        job_id = next(iter(workflow_job_ids))
        previous = jobs_by_id[job_id]
        previous_values = _previous_cron_values(previous, params)
        result = _dispatch_cron_tool(
            _cron_tool_arguments("update", params, job_id=job_id)
        )
        job_id = _job_id_from_result(result, job_id)
        job = get_job(job_id)
        if not isinstance(job, dict):
            raise HermesInstallError(
                "The updated Hermes job could not be read back"
            )
        action = "updated"
    else:
        previous = None
        previous_values = None
        result = _dispatch_cron_tool(
            _cron_tool_arguments("create", params)
        )
        job_id = _job_id_from_result(result)
        job = get_job(job_id) if job_id else None
        if not isinstance(job, dict):
            raise HermesInstallError(
                "The created Hermes job could not be read back"
            )
        action = "created"

    receipt = {
        "schema_version": 1,
        "source": "record",
        "action": action,
        "job_id": str(job.get("id") or job_id),
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "install_idempotency_key": install_key,
        "provenance": provenance_value,
    }
    webhook = None
    try:
        if webhook_trigger is not None:
            webhook = install_webhook_trigger(
                hermes_home=hermes_home,
                workflow_id=workflow_id,
                job_id=receipt["job_id"],
                trigger=webhook_trigger,
            )
            receipt["webhook"] = {
                key: webhook.get(key)
                for key in ("name", "url", "events", "job_id")
                if webhook.get(key) is not None
            }
        _write_install_receipt(
            hermes_home=hermes_home,
            install_key=install_key,
            receipt=receipt,
        )
    except Exception:
        try:
            if (
                isinstance(webhook, dict)
                and not webhook.get("idempotent_replay")
                and webhook.get("name")
            ):
                remove_webhook_trigger(
                    hermes_home=hermes_home,
                    name=str(webhook["name"]),
                    job_id=receipt["job_id"],
                )
            _rollback_cron_change(
                action=action,
                job_id=receipt["job_id"],
                previous=previous,
                previous_values=previous_values,
            )
        except Exception as rollback_error:
            raise HermesInstallError(
                "Webhook or receipt installation failed and the Hermes job rollback failed"
            ) from rollback_error
        raise
    if action == "updated" and previous_values is not None:
        _remove_unreferenced_record_script(
            hermes_home=hermes_home,
            previous_script=previous_values.get("script"),
            current_script=job.get("script"),
        )
    return {
        "success": True,
        "action": action,
        "job_id": receipt["job_id"],
        "idempotent_replay": False,
        "job": job,
        "receipt": receipt,
        "webhook": webhook,
    }


def _remove_unreferenced_record_script(
    *,
    hermes_home: Path,
    previous_script: Any,
    current_script: Any,
) -> None:
    old = str(previous_script or "").strip()
    current = str(current_script or "").strip()
    if not old or old == current:
        return
    relative = Path(old)
    if relative.is_absolute() or not relative.parts or relative.parts[0] != "record":
        return
    try:
        from cron import list_jobs

        if any(
            str(candidate.get("script") or "").strip() == old
            for candidate in list_jobs(include_disabled=True)
        ):
            return
        scripts_root = (
            hermes_home.expanduser().resolve() / "scripts"
        ).resolve()
        target = (scripts_root / relative).resolve()
        target.relative_to(scripts_root / "record")
        target.unlink(missing_ok=True)
        try:
            target.parent.rmdir()
        except OSError:
            pass
    except Exception:
        # The reviewed job update already succeeded. Orphan cleanup is
        # recoverable and must not turn a valid Hermes state change into a
        # misleading install failure.
        log.warning(
            "Could not remove unreferenced Record script %s",
            old,
            exc_info=True,
        )


def _write_install_receipt(
    *,
    hermes_home: Path,
    install_key: str,
    receipt: dict[str, Any],
) -> None:
    receipts_dir = (
        hermes_home.expanduser().resolve()
        / "record"
        / "install-receipts"
    )
    receipts_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = receipts_dir / f"{install_key}.json"
    if destination.exists() or destination.is_symlink():
        raise HermesInstallError(
            "An install receipt already exists for this reviewed build"
        )
    fd, temporary = tempfile.mkstemp(
        prefix=f".{install_key}.",
        suffix=".tmp",
        dir=str(receipts_dir),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                receipt,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
