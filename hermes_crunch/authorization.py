"""Fail-closed Cron authorization provider for reviewed Crunch runs."""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, NoReturn

from .gateway_approval import make_gateway_approval_notifier
from .policy import (
    declarative_filter_matches,
    execution_contract_sha256,
    execution_policy_requires_human,
    normalize_execution_policy,
    tool_call_is_observational,
)
from .workflow_approvals import complete_run, create_request, record_decision


PROVIDER_ID = "hermes-crunch"
log = logging.getLogger(__name__)
_LEGACY_POLICY_ERROR = (
    "This Crunch job uses a legacy unprotected authorization policy; "
    "rebuild and review the workflow before running it"
)
_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "password",
        "secret",
        "token",
    }
)


@dataclass
class _RunAuthorization:
    context: Any
    policy: dict[str, Any]
    session_key: str
    notifier: Any
    request_index: int = 0
    lock: threading.RLock = field(default_factory=threading.RLock)
    denied: threading.Event = field(default_factory=threading.Event)
    failure_reason: str = ""


_ACTIVE_RUN: contextvars.ContextVar[_RunAuthorization | None] = (
    contextvars.ContextVar("hermes_crunch_authorization_run", default=None)
)


def _latch_failure(active: _RunAuthorization, reason: str) -> None:
    """Record the first terminal authorization failure for this occurrence."""

    with active.lock:
        if active.denied.is_set():
            return
        active.failure_reason = str(reason or "Crunch authorization failed")
        active.denied.set()


def _context_value(context: Any, name: str, default: Any = None) -> Any:
    if isinstance(context, dict):
        return context.get(name, default)
    return getattr(context, name, default)


def _redact(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return "[truncated]"
    if isinstance(value, dict):
        return {
            str(key): (
                "[redacted]"
                if str(key).strip().lower() in _SENSITIVE_KEYS
                else _redact(item, depth=depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, depth=depth + 1) for item in value[:50]]
    if isinstance(value, str) and len(value) > 4000:
        return value[:4000] + "…"
    return value


def _adapter_for_platform(adapters: Any, platform_name: str) -> Any:
    if not adapters:
        return None
    normalized = str(platform_name or "").strip().lower()
    try:
        from gateway.config import Platform

        adapter = adapters.get(Platform(normalized))
        if adapter is not None:
            return adapter
    except (AttributeError, KeyError, ValueError):
        pass
    try:
        entries = adapters.items()
    except AttributeError:
        return None
    for key, adapter in entries:
        key_name = getattr(key, "value", key)
        if str(key_name or "").strip().lower() == normalized:
            return adapter
    return None


def _approval_platform_is_capable(platform_name: str) -> bool:
    normalized = str(platform_name or "").strip().lower()
    if not normalized:
        return False
    try:
        from cron.scheduler import cron_delivery_targets

        return any(
            isinstance(item, dict)
            and str(item.get("id") or "").strip().lower() == normalized
            and item.get("supports_exec_approval") is True
            for item in cron_delivery_targets()
        )
    except Exception:
        log.debug(
            "Could not resolve Cron approval capability for %s",
            normalized,
            exc_info=True,
        )
        return False


def _resolve_live_route(
    context: Any,
    policy: dict[str, Any],
) -> tuple[dict[str, Any] | None, Any]:
    route = policy.get("approval_route")
    if not isinstance(route, dict):
        return None, None
    loop = _context_value(context, "loop")
    if loop is None or not getattr(loop, "is_running", lambda: False)():
        return None, None
    platform_name = str(route.get("platform") or "")
    if not _approval_platform_is_capable(platform_name):
        return None, None
    adapter = _adapter_for_platform(
        _context_value(context, "adapters"),
        platform_name,
    )
    if adapter is None or not callable(
        getattr(adapter, "send_exec_approval", None)
    ):
        return None, None
    return dict(route), adapter


def _reviewed_script_is_current(
    context: Any,
    policy: dict[str, Any],
) -> bool | str:
    expected_digest = policy.get("script_sha256")
    if not expected_digest:
        return True
    job = _context_value(context, "job")
    script = job.get("script") if isinstance(job, dict) else None
    if not isinstance(script, str) or not script.strip():
        return "The reviewed Crunch script is missing from the scheduled job"

    from hermes_constants import get_hermes_home

    scripts_root = (get_hermes_home() / "scripts").resolve()
    raw_path = Path(script).expanduser()
    try:
        script_path = (
            raw_path.resolve(strict=True)
            if raw_path.is_absolute()
            else (scripts_root / raw_path).resolve(strict=True)
        )
        script_path.relative_to(scripts_root)
    except (OSError, RuntimeError, ValueError):
        return "The reviewed Crunch script does not resolve inside the profile scripts directory"
    if not script_path.is_file():
        return "The reviewed Crunch script is not a regular file"
    try:
        actual_digest = hashlib.sha256(script_path.read_bytes()).hexdigest()
    except OSError:
        return "The reviewed Crunch script could not be read"
    if actual_digest != expected_digest:
        return "The installed Crunch script no longer matches the reviewed build"
    return True


def _reviewed_execution_is_current(
    context: Any,
    policy: dict[str, Any],
) -> bool | str:
    expected_digest = policy.get("execution_sha256")
    if not expected_digest:
        return True
    job = _context_value(context, "job")
    if not isinstance(job, dict):
        return "The reviewed Crunch execution contract is missing"
    try:
        actual_digest = execution_contract_sha256(job)
    except ValueError:
        return "The installed Crunch execution contract is invalid"
    if actual_digest != expected_digest:
        return "The installed Crunch execution no longer matches the reviewed build"
    return True


def _permanent_unavailable(reason: str) -> Any:
    from cron.authorization import AuthorizationDecision

    return AuthorizationDecision(
        allowed=False,
        reason=reason,
        retryable=False,
    )


def _retryable_unavailable(reason: str) -> Any:
    from cron.authorization import AuthorizationDecision

    return AuthorizationDecision(
        allowed=False,
        reason=reason,
        retryable=True,
    )


def _raise_permanent_unavailable(reason: str) -> NoReturn:
    from cron.authorization import AuthorizationUnavailable

    raise AuthorizationUnavailable(_permanent_unavailable(reason))


def _raise_retryable_unavailable(reason: str) -> NoReturn:
    from cron.authorization import AuthorizationUnavailable

    raise AuthorizationUnavailable(_retryable_unavailable(reason))


def _policy_integrity_error(
    context: Any,
    policy: dict[str, Any],
) -> str | None:
    if not policy.get("execution_sha256"):
        return (
            "The Crunch job is missing its reviewed execution digest; "
            "rebuild and review the workflow"
        )
    job = _context_value(context, "job")
    has_job_script = bool(
        isinstance(job, dict)
        and isinstance(job.get("script"), str)
        and job["script"].strip()
    )
    has_script_boundary = isinstance(policy.get("script"), dict)
    if has_job_script and not has_script_boundary:
        return (
            "The Crunch job script is missing its exact authorization.script "
            "boundary; rebuild and review the workflow"
        )
    if has_script_boundary and not has_job_script:
        return (
            "The Crunch authorization.script boundary has no scheduled script; "
            "rebuild and review the workflow"
        )
    if has_job_script and not policy.get("script_sha256"):
        return (
            "The Crunch job is missing its reviewed script digest; "
            "rebuild and review the workflow"
        )
    script_status = _reviewed_script_is_current(context, policy)
    if script_status is not True:
        return str(script_status)
    execution_status = _reviewed_execution_is_current(context, policy)
    if execution_status is not True:
        return str(execution_status)
    return None


def _authorization_available(context: Any) -> Any:
    """Prove the exact persisted route before Cron claims the occurrence."""

    try:
        policy = normalize_execution_policy(_context_value(context, "policy"))
    except (TypeError, ValueError) as exc:
        return _permanent_unavailable(
            f"Invalid Hermes Crunch authorization policy: {exc}"
        )
    if policy is None:
        return _permanent_unavailable(_LEGACY_POLICY_ERROR)
    integrity_error = _policy_integrity_error(context, policy)
    if integrity_error:
        return _permanent_unavailable(integrity_error)
    if not execution_policy_requires_human(policy):
        return True
    route, adapter = _resolve_live_route(context, policy)
    if route is None or adapter is None:
        return _retryable_unavailable(
            "The exact reviewed approval route is not connected to a "
            "fresh-human-capable gateway adapter"
        )
    return True


def _request_human_decision(
    *,
    operation_id: str,
    effect: str,
    description: str,
    proposal: str,
) -> dict[str, Any]:
    active = _ACTIVE_RUN.get()
    if active is None:
        return {
            "approved": False,
            "message": "Crunch authorization is not active for this run.",
        }
    run_id = str(_context_value(active.context, "run_id") or "").strip()
    job = _context_value(active.context, "job")
    if not run_id or not isinstance(job, dict):
        return {
            "approved": False,
            "message": "Crunch authorization is missing its exact run identity.",
        }

    # One gateway session resolves its oldest pending queue entry. Keep the
    # complete request lifecycle serial so concurrent tool calls cannot display
    # two buttons whose decisions could bind to the wrong reviewed operation.
    with active.lock:
        if active.denied.is_set():
            return {
                "approved": False,
                "message": active.failure_reason,
            }
        active.request_index += 1
        action_index = active.request_index
        run_digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:16]
        pattern_key = (
            f"{PROVIDER_ID}:{run_digest}:"
            f"{active.policy['policy_id']}:{operation_id}"
        )
        approval_data = {
            "command": proposal,
            "description": f"{description}\nEffect: {effect}",
            "pattern_key": pattern_key,
            "pattern_keys": [pattern_key],
            "require_fresh_human": True,
            "allow_permanent": False,
            "allow_session": False,
        }
        try:
            from tools.approval import (
                get_approval_timeout,
                request_fresh_gateway_approval,
            )

            request = create_request(
                job=job,
                policy=active.policy,
                session_key=active.session_key,
                run_id=run_id,
                operation_id=operation_id,
                approval_data=approval_data,
                action_index=action_index,
                timeout_seconds=get_approval_timeout(),
            )
            if request["status"] != "pending":
                message = (
                    "This exact protected action already has a terminal or "
                    "claimed receipt; it was not replayed."
                )
                _latch_failure(active, message)
                return {
                    "approved": False,
                    "message": message,
                }
            decision = request_fresh_gateway_approval(
                active.session_key,
                active.notifier,
                approval_data,
                surface="hermes-crunch",
            )
            persisted = record_decision(
                request["id"],
                run_id=run_id,
                decision={
                    "resolved": decision.get("outcome")
                    not in {
                        "timeout",
                        "notify_failed",
                        "unavailable",
                        "error",
                    },
                    "choice": decision.get("choice")
                    or ("once" if decision.get("approved") else "deny"),
                    "reason": decision.get("reason"),
                    "notify_failed": decision.get("outcome")
                    == "notify_failed",
                },
            )
        except Exception as exc:
            log.exception("Hermes Crunch approval gate failed")
            message = f"Crunch approval gate failed closed: {exc}"
            _latch_failure(active, message)
            return {
                "approved": False,
                "message": message,
            }
        approved = persisted["approved"] is True
        if not approved:
            _latch_failure(
                active,
                str(
                    persisted.get("message")
                    or "A human did not approve this Crunch operation"
                ),
            )
        return {
            "approved": approved,
            "message": persisted.get("message"),
            "request_id": request["id"],
        }


def _authorization_directive(
    *,
    tool_name: str = "",
    args: Any = None,
    **_kwargs: Any,
) -> dict[str, str] | None:
    active = _ACTIVE_RUN.get()
    if active is None:
        return None
    if active.denied.is_set():
        if tool_call_is_observational(tool_name, args):
            return None
        return {
            "action": "block",
            "message": "BLOCKED: " + active.failure_reason,
        }
    try:
        return _active_authorization_directive(
            active,
            tool_name=tool_name,
            args=args,
        )
    except Exception:
        log.exception("Hermes Crunch tool authorization failed closed")
        _latch_failure(
            active,
            "Hermes Crunch could not verify a tool call against the reviewed "
            "authorization policy",
        )
        return {
            "action": "block",
            "message": (
                "BLOCKED: Hermes Crunch could not verify this tool call "
                "against the reviewed authorization policy"
            ),
        }


def _active_authorization_directive(
    active: _RunAuthorization,
    *,
    tool_name: str,
    args: Any,
) -> dict[str, str] | None:
    tool_operations = [
        operation
        for operation in active.policy["operations"]
        if operation["tool"] == tool_name
    ]
    matches = [
        operation
        for operation in tool_operations
        if operation["enabled"]
        and declarative_filter_matches(operation["arguments"], args)
    ]
    if not matches:
        if any(
            not operation["enabled"]
            and declarative_filter_matches(operation["arguments"], args)
            for operation in tool_operations
        ):
            return None
        if tool_call_is_observational(tool_name, args):
            return None
        message = (
            "This non-observational tool call is outside the reviewed Crunch "
            "authorization operations"
        )
        _latch_failure(active, message)
        return {
            "action": "block",
            "message": "BLOCKED: " + message,
        }
    if len(matches) != 1:
        message = (
            "This tool call matches multiple reviewed Crunch authorization "
            "operations"
        )
        _latch_failure(active, message)
        return {
            "action": "block",
            "message": "BLOCKED: " + message,
        }

    operation = matches[0]
    review_arguments = json.dumps(
        _redact(args if isinstance(args, dict) else {}),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(review_arguments) > 6000:
        review_arguments = review_arguments[:6000] + "…"
    result = _request_human_decision(
        operation_id=operation["id"],
        effect=operation["effect"],
        description=operation["description"],
        proposal=f"Tool: {tool_name}\nArguments: {review_arguments}",
    )
    if result["approved"]:
        return None
    _latch_failure(
        active,
        str(
            result.get("message")
            or "A human did not approve this Crunch operation"
        ),
    )
    return {
        "action": "block",
        "message": str(
            result.get("message")
            or "BLOCKED: a human did not approve this Crunch operation"
        ),
    }


@contextlib.contextmanager
def _authorization_scope(context: Any) -> Iterator[dict[str, Any]]:
    try:
        policy = normalize_execution_policy(_context_value(context, "policy"))
    except (TypeError, ValueError) as exc:
        _raise_permanent_unavailable(
            f"Invalid Hermes Crunch authorization policy: {exc}"
        )
    if policy is None:
        _raise_permanent_unavailable(_LEGACY_POLICY_ERROR)
    integrity_error = _policy_integrity_error(context, policy)
    if integrity_error:
        _raise_permanent_unavailable(integrity_error)
    requires_human = execution_policy_requires_human(policy)
    run_id = str(_context_value(context, "run_id") or "").strip()
    route, adapter = (
        _resolve_live_route(context, policy)
        if requires_human
        else (None, None)
    )
    if requires_human and (not run_id or route is None or adapter is None):
        _raise_retryable_unavailable(
            "The reviewed Crunch approval route is not live for this run"
        )
    session_key = (
        f"crunch:{str(_context_value(context, 'job_id') or 'workflow')}:"
        f"{run_id}:{uuid.uuid4().hex}"
    )
    if requires_human:
        metadata = {}
        if route.get("thread_id") is not None:
            metadata["thread_id"] = str(route["thread_id"])
        notifier = make_gateway_approval_notifier(
            adapter=adapter,
            loop=_context_value(context, "loop"),
            chat_id=str(route["chat_id"]),
            session_key=session_key,
            metadata=metadata,
            logger=log,
        )
    else:
        def notifier(_request: Any) -> None:
            return None
    active = _RunAuthorization(
        context=context,
        policy=policy,
        session_key=session_key,
        notifier=notifier,
    )
    token = _ACTIVE_RUN.set(active)

    def finish_run(*, success: bool, error: str) -> None:
        if requires_human:
            complete_run(run_id, success=success, error=error)

    def completion_check() -> Any:
        from cron.authorization import AuthorizationDecision

        if active.denied.is_set():
            return AuthorizationDecision(
                allowed=False,
                reason=active.failure_reason,
                retryable=False,
            )
        return AuthorizationDecision(allowed=True)

    try:
        script_approval = policy.get("script")
        if (
            isinstance(script_approval, dict)
            and script_approval.get("enabled") is not False
        ):
            script_status = _reviewed_script_is_current(context, policy)
            execution_status = _reviewed_execution_is_current(context, policy)
            if script_status is not True or execution_status is not True:
                message = str(
                    script_status
                    if script_status is not True
                    else execution_status
                )
                finish_run(success=False, error=message)
                _raise_permanent_unavailable(message)
            job = _context_value(context, "job")
            script_path = (
                str(job.get("script") or "")
                if isinstance(job, dict)
                else ""
            )
            result = _request_human_decision(
                operation_id=f"script:{script_approval['id']}",
                effect="execute",
                description=(
                    "Execute the exact reviewed script. "
                    + str(script_approval["description"])
                ),
                proposal=(
                    f"Script: {script_path}\n"
                    f"Script SHA-256: {policy.get('script_sha256')}\n"
                    f"Execution SHA-256: {policy.get('execution_sha256')}"
                ),
            )
            if not result.get("approved"):
                message = str(
                    result.get("message")
                    or "A human did not approve the exact reviewed script"
                )
                _latch_failure(active, message)
                finish_run(success=False, error=message)
                from cron.authorization import (
                    AuthorizationDecision,
                    AuthorizationUnavailable,
                )

                raise AuthorizationUnavailable(
                    AuthorizationDecision(
                        allowed=False,
                        reason=message,
                        retryable=False,
                    )
                )
            script_status = _reviewed_script_is_current(context, policy)
            execution_status = _reviewed_execution_is_current(context, policy)
            if script_status is not True or execution_status is not True:
                message = str(
                    script_status
                    if script_status is not True
                    else execution_status
                )
                finish_run(success=False, error=message)
                _raise_permanent_unavailable(message)
        bindings = {"completion_check": completion_check}
        if isinstance(script_approval, dict):
            bindings["script_sha256"] = policy["script_sha256"]
        try:
            yield bindings
        except BaseException as exc:
            finish_run(success=False, error=str(exc))
            raise
        else:
            bindings = _context_value(context, "bindings")
            outcome = (
                bindings.get("run_outcome")
                if isinstance(bindings, dict)
                else None
            )
            success = getattr(outcome, "success", None)
            if not isinstance(success, bool):
                message = (
                    "Cron did not report a trustworthy run outcome to "
                    "Hermes Crunch authorization"
                )
                finish_run(success=False, error=message)
                raise RuntimeError(message)
            finish_run(
                success=success,
                error=str(getattr(outcome, "error", "") or ""),
            )
    finally:
        cleanup = getattr(notifier, "_approval_cleanup", None)
        if callable(cleanup):
            cleanup()
        _ACTIVE_RUN.reset(token)


def register_authorization(ctx: Any) -> None:
    """Register against the generic Cron contract when the host provides it."""

    try:
        from cron.authorization import (
            register_authorization_provider,
            unregister_authorization_provider,
        )
    except ImportError:
        log.warning(
            "Hermes Crunch authorization is unavailable: this Hermes build "
            "does not provide cron.authorization"
        )
        return

    provider_created = register_authorization_provider(
        PROVIDER_ID,
        is_available=_authorization_available,
        scope=_authorization_scope,
    )
    try:
        ctx.register_hook("pre_tool_call", _authorization_directive)
    except BaseException:
        if provider_created:
            unregister_authorization_provider(PROVIDER_ID)
        raise
