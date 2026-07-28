from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from recording_bundle import write_json
from .approval import load_compiled_artifacts, payload_hash, write_artifact_manifest
from .evidence import capture_acceptance_blockers
from .policy import (
    execution_contract_sha256,
    normalize_declarative_filter,
    normalize_execution_policy,
)


_DRAFT_FIELDS = {
    "selection_reason", "state_strategy", "idempotency_strategy", "steps", "approval_steps",
    "execution", "warnings", "trigger",
}
_EXECUTION_FIELDS = {
    "no_agent", "script", "prompt", "skills", "enabled_toolsets", "requirements", "authorization",
}
_RECORDING_EVIDENCE_MARKERS = (
    "evidence-pack.json",
    "capture-manifest.json",
    "raw/input.jsonl",
    "raw/context.jsonl",
    "raw/active-surface.jsonl",
    "visual/frames.jsonl",
    "visual/active.mp4",
    "visual/active-",
    "artifacts/keyframes/",
    "artifacts/clipboard/",
    "crunch/recordings/",
)


class CompileError(ValueError):
    pass


@dataclass(frozen=True)
class CompileResult:
    payload: dict[str, Any]


def validate_draft_plan(
    plan: Any, *, available_toolsets: set[str]
) -> dict[str, Any]:
    """Validate the one schedule-independent plan emitted by the drafter.

    This is the single execution-shape contract shared by drafting and
    compilation.  Scheduling and profile configuration deliberately stay out
    of the model-authored object and are applied only after review.
    """

    if not isinstance(plan, dict):
        raise CompileError("Hermes cron drafter returned a non-object")
    unsupported = sorted(set(plan) - _DRAFT_FIELDS)
    if unsupported:
        raise CompileError("Hermes cron drafter returned unsupported fields: " + ", ".join(unsupported))

    execution = plan.get("execution")
    if not isinstance(execution, dict):
        raise CompileError("Hermes cron drafter must return an execution object")
    unsupported = sorted(set(execution) - _EXECUTION_FIELDS)
    if unsupported:
        raise CompileError("execution contains unsupported fields: " + ", ".join(unsupported))
    no_agent = execution.get("no_agent")
    if not isinstance(no_agent, bool):
        raise CompileError("execution.no_agent must be a boolean")

    script = _draft_script(execution.get("script"))
    prompt = _optional_text(execution.get("prompt"), "execution.prompt")
    skills = _string_list(execution.get("skills"), "execution.skills")
    toolsets = _string_list(
        execution.get("enabled_toolsets"), "execution.enabled_toolsets"
    )
    unknown = sorted(set(toolsets) - available_toolsets)
    if unknown:
        raise CompileError(
            "Drafter selected unavailable cron toolsets: " + ", ".join(unknown)
        )

    if no_agent:
        if script is None:
            raise CompileError("No-agent execution requires execution.script")
        if prompt or skills or toolsets:
            raise CompileError(
                "No-agent execution cannot include prompt, skills, or enabled_toolsets"
            )
        llm_policy = "never"
    else:
        if prompt is None:
            raise CompileError("Agent execution requires execution.prompt")
        llm_policy = "on_change" if script is not None else "every_run"

    requirements = execution.get("requirements")
    if not isinstance(requirements, dict):
        raise CompileError("execution.requirements must be an object")
    normalized_requirements = {
        key: _string_list(requirements.get(key), f"execution.requirements.{key}")
        for key in ("commands", "network_origins", "apps", "files")
    }
    _reject_recording_evidence_runtime_references(prompt=prompt, script=script)
    authorization = _validate_execution_authorization(
        no_agent=no_agent,
        script=script,
        authorization=execution.get("authorization"),
    )
    if any(
        operation.get("enabled") is False
        for operation in (authorization or {}).get("operations") or []
    ):
        raise CompileError(
            "Drafter-authored authorization operations must be enabled; "
            "only the operator may disable a reviewed operation"
        )
    script_approval = (authorization or {}).get("script")
    if (
        isinstance(script_approval, dict)
        and script_approval.get("enabled") is False
    ):
        raise CompileError(
            "Drafter-authored exact script approval must be enabled; "
            "only the operator may disable it"
        )
    _validate_agent_operation_tools(
        authorization=authorization,
        enabled_toolsets=toolsets,
        no_agent=no_agent,
    )
    steps = _draft_steps(plan.get("steps"))
    approval_steps = _draft_approval_steps(
        plan.get("approval_steps"),
        steps=steps,
        authorization=authorization,
        no_agent=no_agent,
        has_script=script is not None,
    )

    warnings = plan.get("warnings") or []
    if isinstance(warnings, str):
        warnings = [warnings]
    if not isinstance(warnings, list):
        raise CompileError("warnings must be a list")
    normalized_warnings: list[dict[str, Any]] = []
    for warning in warnings:
        if isinstance(warning, str) and warning.strip():
            normalized_warnings.append({
                "kind": "execution_warning",
                "severity": "review",
                "message": warning.strip(),
                "requires_explicit_approval": True,
            })
            continue
        if not isinstance(warning, dict):
            raise CompileError("warnings entries must be non-empty strings or objects")
        message = str(warning.get("message") or "").strip()
        if not message:
            raise CompileError("warnings entries require a non-empty message")
        severity = str(warning.get("severity") or "review").strip().lower()
        if severity not in {"info", "review", "blocking"}:
            raise CompileError("warnings severity must be info, review, or blocking")
        requires_approval = warning.get("requires_explicit_approval")
        if requires_approval is None:
            requires_approval = severity in {"review", "blocking"}
        if not isinstance(requires_approval, bool):
            raise CompileError("warnings requires_explicit_approval must be a boolean")
        kind = str(warning.get("kind") or "execution_warning").strip() or "execution_warning"
        step = warning.get("step")
        verify = warning.get("verify")
        has_step = step is not None
        has_verify = verify is not None
        if kind == "capture_quality_gap":
            # Capture diagnostics are advisory input to drafting, never a
            # reason to reject an otherwise safe execution plan. Only promote
            # one into the operator review when it is both step-local and
            # concretely verifiable; malformed or unplaced diagnostics remain
            # in review.capture_quality / recording_warnings instead.
            if (
                not has_step
                or not has_verify
                or isinstance(step, bool)
                or not isinstance(step, int)
                or not 1 <= step <= len(steps)
                or not isinstance(verify, str)
                or not verify.strip()
            ):
                continue
            severity = "info"
            requires_approval = False
        normalized_warning = {
            "kind": kind,
            "severity": severity,
            "message": message,
            "requires_explicit_approval": requires_approval,
        }
        if kind == "capture_quality_gap" and has_step:
            normalized_warning["step"] = step
            normalized_warning["verify"] = verify.strip()
        normalized_warnings.append(normalized_warning)

    return {
        "selection_reason": _required_str(plan, "selection_reason"),
        "llm_policy": llm_policy,
        "state_strategy": _required_str(plan, "state_strategy"),
        "idempotency_strategy": _required_str(plan, "idempotency_strategy"),
        "steps": steps,
        "approval_steps": approval_steps,
        "trigger": _draft_trigger(plan.get("trigger")),
        "execution": {
            "no_agent": no_agent,
            "script": script,
            "prompt": prompt,
            "skills": skills,
            "enabled_toolsets": toolsets,
            "requirements": normalized_requirements,
            "authorization": authorization,
        },
        "warnings": normalized_warnings,
    }


def _draft_trigger(value: Any) -> dict[str, Any]:
    """Validate a model-authored event-trigger recommendation."""

    if value is None:
        return {
            "requested": False,
            "feasibility": "not_requested",
            "description": "",
            "source": None,
            "events": [],
            "filters": None,
            "reason": "No event trigger was requested or inferred.",
        }
    if not isinstance(value, dict):
        raise CompileError("trigger must be an object")
    allowed = {
        "requested", "feasibility", "description", "source", "events", "filters", "reason"
    }
    unsupported = sorted(set(value) - allowed)
    if unsupported:
        raise CompileError("trigger contains unsupported fields: " + ", ".join(unsupported))
    requested = value.get("requested")
    if not isinstance(requested, bool):
        raise CompileError("trigger.requested must be a boolean")
    feasibility = str(value.get("feasibility") or "").strip().lower()
    if feasibility not in {"not_requested", "supported", "unsupported"}:
        raise CompileError("trigger.feasibility must be not_requested, supported, or unsupported")
    description = str(value.get("description") or "").strip()
    source = str(value.get("source") or "").strip() or None
    reason = str(value.get("reason") or "").strip()
    events = _string_list(value.get("events"), "trigger.events")
    filters = value.get("filters")
    if filters in (None, [], {}):
        filters = None
    else:
        try:
            filters = normalize_declarative_filter(
                filters,
                subject="trigger.filters",
                sequence_is_all=True,
            )
        except ValueError as exc:
            raise CompileError(
                "trigger.filters contains an unsupported declarative webhook filter"
            ) from exc

    if feasibility == "not_requested":
        if requested or description or source or events or filters or not reason:
            raise CompileError("not_requested trigger must contain only its explanatory reason")
    elif feasibility == "supported":
        if not requested or not description or not source or not events or not reason:
            raise CompileError(
                "supported trigger requires requested=true, description, source, events, and reason"
            )
    else:
        if not requested or not description or not reason:
            raise CompileError("unsupported trigger requires requested=true, description, and reason")
        if events or filters:
            raise CompileError("unsupported trigger cannot install events or filters")

    return {
        "requested": requested,
        "feasibility": feasibility,
        "description": description,
        "source": source,
        "events": events,
        "filters": filters,
        "reason": reason,
    }


def _draft_steps(value: Any) -> list[str]:
    """Normalize the small, operator-facing outline of the execution plan."""

    if not isinstance(value, list) or not value:
        raise CompileError("steps must be a non-empty list")
    if len(value) > 6:
        raise CompileError("steps may contain at most 6 items")
    steps = _string_list(value, "steps")
    if len(steps) != len(value):
        raise CompileError("steps entries must be non-empty strings")
    return steps


def _draft_approval_steps(
    value: Any,
    *,
    steps: list[str],
    authorization: dict[str, Any] | None,
    no_agent: bool,
    has_script: bool,
) -> list[dict[str, Any]]:
    """Bind protected runtime boundaries to the review step they affect."""

    if value is None:
        return []
    if not isinstance(value, list):
        raise CompileError("approval_steps must be a list")

    operations = {
        str(operation.get("id")): operation
        for operation in (authorization or {}).get("operations") or []
        if isinstance(operation, dict) and operation.get("id")
    }
    script_approval = (authorization or {}).get("script")
    if not isinstance(script_approval, dict):
        script_approval = None
    script_id = (
        str(script_approval.get("id") or "")
        if script_approval is not None and has_script
        else ""
    )
    declared_runtime_keys = {
        *(f"operation:{operation_id}" for operation_id in operations),
        *([f"script:{script_id}"] if script_id else []),
    }
    normalized: list[dict[str, Any]] = []
    seen: dict[tuple[int, str], str | None] = {}
    mapped_runtime_keys: set[str] = set()
    operation_step_by_id: dict[str, int] = {}
    for item in value:
        if not isinstance(item, dict):
            raise CompileError("approval_steps entries must be objects")
        unsupported = sorted(
            set(item)
            - {
                "step",
                "operation_id",
                "script_id",
                "effect",
                "checkpoint_id",
            }
        )
        if unsupported:
            raise CompileError(
                "approval_steps entry contains unsupported fields: "
                + ", ".join(unsupported)
            )
        step = item.get("step")
        if isinstance(step, bool) or not isinstance(step, int) or not 1 <= step <= len(steps):
            raise CompileError(
                f"approval_steps.step must be between 1 and {len(steps)}"
            )
        effect = str(item.get("effect") or "").strip().lower()
        checkpoint_id = str(item.get("checkpoint_id") or "").strip() or None
        raw_script_id = item.get("script_id")
        requested_script_id = (
            str(raw_script_id).strip()
            if isinstance(raw_script_id, str)
            else None
        )
        if raw_script_id is not None and not requested_script_id:
            raise CompileError(
                "approval_steps.script_id must be a non-empty string"
            )
        raw_operation_id = item.get("operation_id")
        operation_id: str | None = None
        if raw_operation_id is not None:
            if not isinstance(raw_operation_id, str) or not raw_operation_id.strip():
                raise CompileError(
                    "approval_steps.operation_id must be a non-empty string"
                )
            operation_id = raw_operation_id.strip()
        references = sum(
            value is not None
            for value in (checkpoint_id, requested_script_id, operation_id)
        )
        if references != 1:
            raise CompileError(
                "approval_steps must reference exactly one authorization "
                "operation or exact script approval"
            )
        if checkpoint_id is not None:
            raise CompileError(
                "approval_steps.checkpoint_id is unsupported; protected "
                "scripts use one exact script approval"
            )
        if requested_script_id is not None:
            if script_approval is None or requested_script_id != script_id:
                raise CompileError(
                    "approval_steps references unknown exact script approval: "
                    + requested_script_id
                )
            if effect != script_approval.get("effect"):
                raise CompileError(
                    "approval_steps effect does not match exact script approval: "
                    + requested_script_id
                )
            runtime_key = f"script:{requested_script_id}"
        elif operation_id is not None:
            operation = operations.get(operation_id)
            if operation is None:
                raise CompileError(
                    "approval_steps references unknown authorization operation: "
                    + operation_id
                )
            if effect != operation.get("effect"):
                raise CompileError(
                    "approval_steps effect does not match authorization operation: "
                    + operation_id
                )
            if no_agent:
                raise CompileError(
                    "Agent authorization operations cannot be used by no-agent execution"
                )
            prior_step = operation_step_by_id.setdefault(operation_id, step)
            if prior_step != step:
                raise CompileError(
                    "Each agent authorization operation must map to exactly one "
                    f"workflow step: {operation_id}"
                )
            runtime_key = f"operation:{operation_id}"
        else:
            raise AssertionError("approval_steps reference validation failed")
        if runtime_key not in declared_runtime_keys:
            raise CompileError(
                "approval_steps references a boundary that is not executable for this "
                f"execution shape: {runtime_key}"
            )
        placement_key = (step, runtime_key)
        if placement_key in seen:
            if seen[placement_key] != operation_id:
                raise CompileError(
                    "approval_steps assigns conflicting operation_id values to "
                    f"{runtime_key} at step {step}"
                )
            continue
        seen[placement_key] = operation_id
        mapped_runtime_keys.add(runtime_key)
        entry: dict[str, Any] = {"step": step, "effect": effect}
        if operation_id is not None:
            entry["operation_id"] = operation_id
        if requested_script_id is not None:
            entry["script_id"] = requested_script_id
        normalized.append(entry)

    missing = sorted(declared_runtime_keys - mapped_runtime_keys)
    if missing:
        raise CompileError(
            "approval_steps must place every executable approval boundary in the review: "
            + ", ".join(missing)
        )
    return normalized


def _draft_script(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise CompileError("execution.script must be an object or null")
    path = _safe_relative_path(_required_str(value, "path"), "execution.script.path")
    if path.suffix.lower() not in {".py", ".sh", ".bash"}:
        raise CompileError("execution.script.path must end in .py, .sh, or .bash")
    body = value.get("body")
    if not isinstance(body, str) or not body.strip():
        raise CompileError("execution.script.body is required")
    return {"path": path.as_posix(), "body": body}


def _reject_recording_evidence_runtime_references(
    *, prompt: str | None, script: dict[str, str] | None
) -> None:
    """Keep local drafting evidence out of the reviewed runtime definition."""

    values = [prompt or ""]
    if script:
        values.extend([str(script.get("path") or ""), str(script.get("body") or "")])
    lowered = "\n".join(values).lower()
    leaked = next((marker for marker in _RECORDING_EVIDENCE_MARKERS if marker in lowered), None)
    if leaked:
        raise CompileError(
            "execution must not reference local recording evidence at runtime: " + leaked
        )


def _authorization(value: Any) -> dict[str, Any] | None:
    try:
        return normalize_execution_policy(value)
    except ValueError as exc:
        raise CompileError(str(exc)) from exc


def _validate_agent_operation_tools(
    *,
    authorization: dict[str, Any] | None,
    enabled_toolsets: list[str],
    no_agent: bool,
) -> None:
    operations = (authorization or {}).get("operations") or []
    if authorization is None:
        return
    if no_agent:
        if not operations:
            return
        raise CompileError(
            "Agent authorization operations cannot be used by no-agent execution"
        )
    try:
        from toolsets import resolve_toolset

        available_tools = {
            tool
            for toolset in enabled_toolsets
            for tool in resolve_toolset(toolset)
        }
    except Exception as exc:
        raise CompileError(
            "Could not resolve tools for authorization operations"
        ) from exc
    if "delegate_task" in available_tools:
        raise CompileError(
            "Human-authorized agent execution cannot enable delegation or "
            "delegate_task because detached child work can outlive the "
            "authorization scope"
        )
    if not operations:
        return
    missing = sorted(
        {
            str(operation.get("tool") or "")
            for operation in operations
            if str(operation.get("tool") or "") not in available_tools
        }
    )
    if missing:
        raise CompileError(
            "Authorization operation references a tool outside execution.enabled_toolsets: "
            + ", ".join(missing)
        )


def effective_approval_boundaries(workflow: dict[str, Any]) -> dict[str, Any]:
    """Return the server-owned, executable approval boundaries for review UI.

    The view is derived only from execution authorization after the same
    validation used by compilation.  `review.approval_steps` may place a
    boundary in the timeline, but it cannot create, split, or weaken a
    runtime boundary. Agent operations retain their stable operation identity.
    """

    raw_execution = workflow.get("execution")
    if not isinstance(raw_execution, dict):
        raise CompileError("execution must be an object")
    no_agent = raw_execution.get("no_agent")
    if not isinstance(no_agent, bool):
        raise CompileError("execution.no_agent must be a boolean")
    script = _draft_script(raw_execution.get("script"))
    authorization = _validate_execution_authorization(
        no_agent=no_agent,
        script=script,
        authorization=raw_execution.get("authorization"),
    ) or {}
    has_script = script is not None
    boundaries: list[dict[str, Any]] = []
    by_key: dict[str, dict[str, Any]] = {}
    if not no_agent:
        for operation in authorization.get("operations") or []:
            if not isinstance(operation, dict):
                continue
            operation_id = str(operation.get("id") or "").strip()
            effect = str(operation.get("effect") or "").strip().lower()
            if not operation_id or not effect:
                continue
            key = f"operation:{operation_id}"
            boundary = {
                "key": key,
                "kind": "operation",
                "operation_id": operation_id,
                "effects": [effect],
                "description": str(
                    operation.get("description") or f"{effect.capitalize()} action"
                ).strip(),
                "enabled": operation.get("enabled") is not False,
                "steps": [],
            }
            boundaries.append(boundary)
            by_key[key] = boundary
    script_approval = authorization.get("script")
    if has_script and isinstance(script_approval, dict):
        script_id = str(script_approval.get("id") or "").strip()
        effect = str(script_approval.get("effect") or "").strip().lower()
        if script_id and effect:
            key = f"script:{script_id}"
            boundary = {
                "key": key,
                "kind": "script",
                "effect": effect,
                "effects": [effect],
                "script_id": script_id,
                "description": str(
                    script_approval.get("description")
                    or "Execute the exact reviewed script"
                ).strip(),
                "enabled": script_approval.get("enabled") is not False,
                "steps": [],
            }
            boundaries.append(boundary)
            by_key[key] = boundary

    review = workflow.get("review") if isinstance(workflow.get("review"), dict) else {}
    rows = review.get("approval_steps") if isinstance(review.get("approval_steps"), list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        step = row.get("step")
        effect = str(row.get("effect") or "").strip().lower()
        script_id = str(row.get("script_id") or "").strip()
        if isinstance(step, bool) or not isinstance(step, int) or step < 1:
            continue
        operation_id = str(row.get("operation_id") or "").strip()
        key = (
            f"script:{script_id}"
            if has_script and script_id
            else f"operation:{operation_id}"
            if operation_id
            else ""
        )
        if not key:
            raise CompileError(
                "review.approval_steps must reference an authorization "
                "operation or exact script approval"
            )
        boundary = by_key.get(key)
        if boundary is None:
            if operation_id:
                return {
                    "boundaries": [],
                    "error": _UNBOUND_REVIEW_OPERATION_MESSAGE,
                }
            raise CompileError(
                "review.approval_steps references unknown effective approval boundary: "
                + key
            )
        if effect not in boundary["effects"]:
            raise CompileError(
                "review.approval_steps effect does not match effective "
                "approval boundary: " + key
            )
        if step not in boundary["steps"]:
            boundary["steps"].append(step)

    for boundary in boundaries:
        boundary["steps"].sort()
    return {"boundaries": boundaries}


_UNBOUND_REVIEW_OPERATION_MESSAGE = (
    "This draft has step-local human checks that are not bound to executable "
    "authorization operations. Redraft it before scheduling."
)

def review_blockers(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    """Return unresolved model-authored execution blockers.

    Recorder warnings describe evidence quality. They remain available to the
    drafter and in review metadata, but their internal kind names do not have
    authority to override a compiler-valid plan. A material ambiguity must be
    represented by the plan as a blocking/explicit draft warning (or fail a
    concrete compile/safety gate) before it can prevent scheduling.
    """

    review = workflow.get("review") if isinstance(workflow.get("review"), dict) else {}
    blockers: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(kind: str, message: str, resolution: str) -> None:
        key = (kind, message, resolution)
        if key in seen:
            return
        seen.add(key)
        blockers.append({
            "kind": kind,
            "message": message,
            "resolution": resolution,
        })

    acceptance = review.get("capture_acceptance")
    if not isinstance(acceptance, dict) and isinstance(review.get("capture_quality"), dict):
        add(
            "capture_acceptance_missing",
            "The draft is not tied to a sealed capture acceptance record.",
            "rerecord",
        )
    elif isinstance(acceptance, dict):
        acceptance_items = capture_acceptance_blockers(acceptance)
        if acceptance_items is None:
            add(
                "capture_acceptance_invalid",
                "The sealed capture acceptance record is invalid.",
                "rerecord",
            )
        for item in acceptance_items or []:
            message = str(item.get("message") or "The recording evidence is incomplete.").strip()
            add(
                str(item.get("code") or "capture_evidence_incomplete"),
                message,
                "rerecord",
            )
    execution = (
        workflow.get("execution")
        if isinstance(workflow.get("execution"), dict)
        else {}
    )
    invalid_authorization = False
    if isinstance(workflow.get("execution"), dict):
        try:
            validate_execution_authorization(
                execution,
                execution.get("authorization"),
            )
        except CompileError as exc:
            invalid_authorization = True
            add(
                "authorization_invalid",
                str(exc),
                "redraft",
            )
    value = review.get("warnings")
    for warning in value if isinstance(value, list) else []:
        if isinstance(warning, str):
            warning = {"kind": "execution_warning", "message": warning}
        if not isinstance(warning, dict):
            continue
        kind = str(warning.get("kind") or "execution_warning").strip().lower()
        message = str(warning.get("message") or "").strip()
        severity = str(warning.get("severity") or "").strip().lower()
        explicit = warning.get("requires_explicit_approval") is True
        if not kind or not message:
            continue
        if not (explicit or severity == "blocking" or kind == "browser_auth"):
            continue
        add(kind, message, "redraft")
    approval_steps = review.get("approval_steps")
    if not invalid_authorization and any(
        isinstance(row, dict)
        and (
            str(row.get("operation_id") or "").strip()
            or str(row.get("script_id") or "").strip()
        )
        for row in (approval_steps if isinstance(approval_steps, list) else [])
    ):
        try:
            boundary_state = effective_approval_boundaries(workflow)
            boundary_error = str(boundary_state.get("error") or "")
        except CompileError as exc:
            boundary_error = str(exc)
        if boundary_error:
            add(
                "authorization_boundary_needs_redraft",
                boundary_error,
                "redraft",
            )
    return blockers


def crunch_install_guard(workflow: dict[str, Any]) -> dict[str, Any]:
    """Return the non-bypassable pre-install review guard."""

    blockers = review_blockers(workflow)
    return {
        "blockers": blockers,
        "requires_review_resolution": bool(blockers),
    }


def validate_execution_authorization(
    execution: Any,
    authorization: Any,
) -> dict[str, Any] | None:
    """Validate an operator-selected policy against the reviewed execution.

    Crunch uses this at both draft validation and final compilation so a
    dashboard override cannot create an operation or exact-script boundary
    that the reviewed execution does not contain.
    """

    if not isinstance(execution, dict):
        raise CompileError("execution must be an object")
    no_agent = execution.get("no_agent")
    if not isinstance(no_agent, bool):
        raise CompileError("execution.no_agent must be a boolean")
    script = _draft_script(execution.get("script"))
    return _validate_execution_authorization(
        no_agent=no_agent,
        script=script,
        authorization=authorization,
    )


def validate_authorization_selection(
    execution: Any,
    authorization: Any,
) -> dict[str, Any] | None:
    """Validate dashboard toggles against the reviewed authorization contract.

    The operator may disable reviewed boundaries, but cannot add a boundary or
    change an operation matcher or exact-script identity through the scheduling
    request. The reviewed workflow remains the sole source of executable policy
    identity.
    """

    if not isinstance(execution, dict):
        raise CompileError("execution must be an object")
    reviewed = validate_execution_authorization(
        execution,
        execution.get("authorization"),
    )
    selected = validate_execution_authorization(execution, authorization)
    if reviewed is None:
        if selected is not None:
            raise CompileError(
                "Scheduling cannot add human checks that are absent from the reviewed workflow"
            )
        return None
    if selected is None:
        if reviewed.get("operations") or reviewed.get("script"):
            raise CompileError(
                "Scheduling must retain reviewed operation/script identities; "
                "disable their enabled flags instead"
            )
        return None

    def immutable_entries(
        policy: dict[str, Any],
        field: str,
        identity_key: str,
    ) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for raw in policy.get(field) or []:
            if not isinstance(raw, dict):
                continue
            entry = {key: value for key, value in raw.items() if key != "enabled"}
            result[str(entry.get(identity_key) or "")] = entry
        return result

    reviewed_operations = immutable_entries(reviewed, "operations", "id")
    selected_operations = immutable_entries(selected, "operations", "id")
    if selected_operations != reviewed_operations:
        raise CompileError(
            "Scheduling cannot add, remove, or change reviewed authorization operations"
        )
    reviewed_script = {
        key: value
        for key, value in (reviewed.get("script") or {}).items()
        if key != "enabled"
    }
    selected_script = {
        key: value
        for key, value in (selected.get("script") or {}).items()
        if key != "enabled"
    }
    if selected_script != reviewed_script:
        raise CompileError(
            "Scheduling cannot add, remove, or change the reviewed exact script approval"
        )
    return selected


def _validate_execution_authorization(
    *,
    no_agent: bool,
    script: dict[str, str] | None,
    authorization: Any,
) -> dict[str, Any] | None:
    normalized = _authorization(authorization)
    script_body = script.get("body", "") if script is not None else ""
    has_inline_checkpoint = any(
        marker in script_body
        for marker in (
            "approval_runtime",
            "require_approval",
            "HERMES_AUTHORIZATION_PYTHON",
            "HERMES_AUTHORIZATION_RUNTIME",
        )
    )
    if has_inline_checkpoint:
        raise CompileError(
            "Inline script checkpoints are unsupported because they cannot "
            "bind a proposal to later effects; remove the inline approval "
            "call and use one exact authorization.script approval"
        )
    if normalized is None:
        raise CompileError(
            (
                "No-agent execution requires one exact authorization.script "
                "approval"
            )
            if no_agent
            else (
                "Agent and hybrid execution requires a human_in_loop "
                "authorization policy, even when it is observational-only"
            )
        )

    operations = normalized.get("operations") or []
    script_approval = normalized.get("script")
    if no_agent and operations:
        raise CompileError(
            "No-agent authorization uses one exact script approval, not agent operations"
        )
    if script_approval is not None and script is None:
        raise CompileError("authorization.script requires execution.script")
    if script is not None and script_approval is None:
        raise CompileError(
            "Every execution.script requires one exact authorization.script "
            "approval boundary"
        )
    return normalized


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CompileError(f"{field} must be a string or null")
    return value.strip() or None


@dataclass
class PromotedArtifacts:
    """Reversible promotion of an approved build into one profile's runtime."""

    records: list[tuple[Path, Path | None]]

    def rollback(self) -> None:
        for destination, backup in reversed(self.records):
            _remove_path(destination)
            if backup and (backup.exists() or backup.is_symlink()):
                os.replace(backup, destination)
        self.records.clear()

    def commit(self) -> None:
        for _destination, backup in self.records:
            if backup:
                _remove_path(backup)
        self.records.clear()


def load_workflow(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CompileError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CompileError("Workflow input must be a JSON object")
    return data


def compile_workflow(
    workflow: dict[str, Any],
    hermes_home: Path,
    out_dir: Path,
    *,
    provenance: dict[str, Any] | None = None,
) -> CompileResult:
    execution = normalize_workflow_execution(workflow)
    no_agent = execution["no_agent"]
    name = _required_str(workflow, "name")
    workflow_scope = str(workflow.get("id") or name)
    build_scope = f"{workflow_scope}\0{out_dir.expanduser().resolve().name}"
    script_namespace = Path("crunch") / hashlib.sha256(
        build_scope.encode("utf-8")
    ).hexdigest()[:16]
    schedule = _required_str(workflow, "schedule")
    deliver = str(workflow.get("deliver") or "local")

    out_dir = out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    runtime_root = out_dir / "staged-runtime"
    shutil.rmtree(runtime_root, ignore_errors=True)
    scripts_dir = runtime_root / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    artifacts: list[dict[str, Any]] = []
    payload: dict[str, Any] = {
        "action": "create",
        "name": name,
        "schedule": schedule,
        "deliver": deliver,
    }
    authorization = (
        dict(execution["authorization"])
        if isinstance(execution.get("authorization"), dict)
        else None
    )
    if authorization is not None:
        payload["authorization"] = authorization

    script_name: str | None = None
    if no_agent:
        if execution["prompt"] or execution["skills"] or execution["enabled_toolsets"]:
            raise CompileError("no-agent execution cannot include prompt, skills, or enabled_toolsets")
        script_name = _write_script(
            execution.get("script"),
            scripts_dir,
            required=True,
            destination_prefix=script_namespace,
        )
        if script_name is None:
            raise CompileError("no-agent execution requires a script")
        payload.update({"script": script_name, "no_agent": True})
        artifacts.append(_script_artifact(script_name))
    else:
        skills = execution["skills"]
        if skills:
            payload["skills"] = skills
        prompt = execution.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise CompileError("execution.prompt is required for agent jobs")
        payload["prompt"] = prompt.strip()
        toolsets = execution["enabled_toolsets"]
        if toolsets:
            payload["enabled_toolsets"] = toolsets
        script_name = _write_script(
            execution.get("script"),
            scripts_dir,
            required=False,
            destination_prefix=script_namespace,
        )
        if script_name:
            payload["script"] = script_name
            artifacts.append(_script_artifact(script_name))
        payload["no_agent"] = False

    if authorization is not None:
        authorization.pop("policy_id", None)
        authorization["execution_sha256"] = execution_contract_sha256(payload)
        if script_name:
            script_path = scripts_dir / script_name
            authorization["script_sha256"] = hashlib.sha256(
                script_path.read_bytes()
            ).hexdigest()
        stable = json.dumps(authorization, sort_keys=True, separators=(",", ":"))
        authorization["policy_id"] = hashlib.sha256(
            f"{build_scope}\0{stable}".encode("utf-8")
        ).hexdigest()[:16]

    write_artifact_manifest(out_dir, artifacts)

    write_json(out_dir / "cronjob-payload.json", payload)

    script_source = (
        (scripts_dir / script_name).read_text(encoding="utf-8")
        if script_name
        else None
    )
    (out_dir / "review.md").write_text(
        _review_markdown(
            workflow,
            payload,
            script_source=script_source,
        ),
        encoding="utf-8",
    )

    review = workflow.get("review") if isinstance(workflow.get("review"), dict) else {}
    compiled_digest = hashlib.sha256(
        json.dumps(workflow, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    compiled_provenance = {
        "type": "hermes-crunch",
        "workflow_id": str(workflow.get("id") or ""),
        "source_digest": str(review.get("source_digest") or ""),
        "draft_digest": compiled_digest,
        "draft_revision": int(review.get("draft_revision") or 1),
        "build_id": out_dir.name,
        "build_digest": compiled_digest,
    }
    if provenance is not None:
        compiled_provenance.update(provenance)
    summary = {
        "payload_sha256": payload_hash(payload),
        "payload": payload,
        "provenance": compiled_provenance,
    }
    write_json(out_dir / "compile-summary.json", summary)

    return CompileResult(payload=payload)


def normalize_workflow_execution(workflow: dict[str, Any]) -> dict[str, Any]:
    """Return the reviewed native cron execution fields."""

    raw = workflow.get("execution")
    if not isinstance(raw, dict):
        raise CompileError("execution must be an object")
    execution = dict(raw)
    no_agent = execution.get("no_agent")
    if not isinstance(no_agent, bool):
        raise CompileError("execution.no_agent must be a boolean")
    script = _draft_script(execution.get("script"))
    execution["script"] = script
    execution["prompt"] = _optional_text(
        execution.get("prompt"),
        "execution.prompt",
    )
    execution["skills"] = _string_list(execution.get("skills"), "execution.skills")
    execution["enabled_toolsets"] = _string_list(
        execution.get("enabled_toolsets"), "execution.enabled_toolsets"
    )
    _reject_recording_evidence_runtime_references(
        prompt=execution["prompt"],
        script=script,
    )
    execution["authorization"] = _validate_execution_authorization(
        no_agent=no_agent,
        script=script,
        authorization=execution.get("authorization"),
    )
    unsupported = sorted(set(execution) - _EXECUTION_FIELDS)
    if unsupported:
        raise CompileError("execution contains unsupported fields: " + ", ".join(unsupported))
    return execution


def _required_str(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CompileError(f"{key} is required")
    return value.strip()


def _safe_relative_path(value: str, field: str) -> Path:
    if not value or value.startswith("~") or Path(value).is_absolute():
        raise CompileError(f"{field} must be a relative path")
    path = Path(value)
    if ".." in path.parts:
        raise CompileError(f"{field} must not contain '..'")
    return path


def _write_script(
    script: Any,
    scripts_dir: Path,
    *,
    required: bool,
    destination_prefix: Path | None = None,
) -> str | None:
    if script is None:
        if required:
            raise CompileError("execution.script is required for no-agent jobs")
        return None
    if not isinstance(script, dict):
        raise CompileError("script must be an object")
    script_path = _safe_relative_path(_required_str(script, "path"), "script.path")
    body = script.get("body")
    destination = (destination_prefix / script_path) if destination_prefix else script_path
    target = scripts_dir / destination
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(body, str):
        target.write_text(_ensure_trailing_newline(body), encoding="utf-8")
    else:
        raise CompileError("script requires body")
    return destination.as_posix()


def _string_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CompileError(f"{field} must be a list of strings")
    result: list[str] = []
    for item in value:
        normalized = item.strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def promote_compiled_artifacts(build_dir: Path, hermes_home: Path) -> PromotedArtifacts:
    """Promote approval-bound staged files, with rollback for a failed cron install."""

    build_dir = build_dir.expanduser().resolve()
    home = hermes_home.expanduser().resolve()
    promotion = PromotedArtifacts(records=[])
    temporary: Path | None = None
    try:
        for artifact in load_compiled_artifacts(build_dir):
            source = (build_dir / artifact["source"]).resolve()
            destination = _promotion_destination(home, artifact["destination"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            token = uuid.uuid4().hex
            temporary = destination.with_name(f".{destination.name}.hermes-crunch-{token}.tmp")
            backup = destination.with_name(f".{destination.name}.hermes-crunch-{token}.backup")
            _remove_path(temporary)
            _remove_path(backup)
            if artifact["kind"] == "directory":
                shutil.copytree(source, temporary)
            else:
                shutil.copy2(source, temporary)
            previous = backup if destination.exists() or destination.is_symlink() else None
            if previous:
                os.replace(destination, previous)
            try:
                os.replace(temporary, destination)
            except Exception:
                if previous and (previous.exists() or previous.is_symlink()):
                    os.replace(previous, destination)
                raise
            promotion.records.append((destination, previous))
            temporary = None
    except Exception:
        if temporary:
            _remove_path(temporary)
        promotion.rollback()
        raise
    return promotion


def _script_artifact(script_name: str) -> dict[str, str]:
    relative = _safe_relative_path(script_name, "script.path")
    return {
        "source": str(Path("staged-runtime") / "scripts" / relative),
        "destination": str(Path("scripts") / relative),
        "kind": "file",
    }


def _promotion_destination(home: Path, raw: str) -> Path:
    relative = _safe_relative_path(raw, "artifact.destination")
    destination = home / relative
    try:
        destination.resolve(strict=False).relative_to(home)
    except ValueError as exc:
        raise CompileError("artifact destination escapes Hermes home") from exc
    return destination


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _ensure_trailing_newline(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


def _review_markdown(
    workflow: dict[str, Any],
    payload: dict[str, Any],
    *,
    script_source: str | None = None,
) -> str:
    mode = "no-agent script" if payload.get("no_agent") else (
        "agent with pre-run script" if payload.get("script") else "agent"
    )
    review = workflow.get("review") if isinstance(workflow.get("review"), dict) else {}
    lines = [
        f"# Review: {payload.get('name', 'Hermes cron job')}",
        "",
        "Approve this build only after checking the schedule, script or prompt, skills, toolsets, prerequisites, and warnings.",
        "",
        "Hermes stores and executes cron scripts from `HERMES_HOME/scripts/`. Use absolute paths inside no-agent scripts when artifacts must land outside that scripts directory.",
        "",
        "## Operator Review",
        "",
        f"- Owner: `{_review_scalar(workflow, review, 'owner')}`",
        f"- Mode: `{mode}`",
        f"- Schedule: `{payload.get('schedule')}`",
        f"- Delivery: `{payload.get('deliver', 'local')}`",
        f"- Toolsets: `{', '.join(payload.get('enabled_toolsets') or [])}`",
        f"- LLM policy: `{review.get('llm_policy', '')}`",
        f"- Runtime authorization: `{(payload.get('authorization') or {}).get('mode', 'missing')}`",
        "",
        "## Inputs And Assumptions",
        "",
        *_review_bullets("Required apps", _review_list(workflow, review, "required_apps")),
        *_review_bullets("Required browser origins", _review_list(workflow, review, "required_browser_origins")),
        *_review_bullets("Required files/folders", _review_list(workflow, review, "required_files")),
        *_review_preconditions(review),
        *_review_bullets("Auth assumptions", _review_list(workflow, review, "auth_assumptions")),
        *_review_bullets("Typed/key sequences", _review_list(workflow, review, "typed_sequences")),
        *_review_bullets("Captured values and parameters", _review_list(workflow, review, "captured_values_and_parameters")),
        *_review_schema(workflow, review),
        "## Phase Plan",
        "",
        *_review_bullets("Phases", _review_list(workflow, review, "phases")),
        "## Step Plan",
        "",
        *_review_bullets("Steps", _review_list(workflow, review, "steps")),
        "## Preflight And Validation",
        "",
        *_review_bullets("Preflight checks", _review_list(workflow, review, "preflight_checks")),
        *_review_bullets("Expected artifacts", _review_list(workflow, review, "expected_artifacts")),
        *_review_bullets("Needs review", _review_list(workflow, review, "warnings")),
        *_review_bullets("Recording-only warnings", _review_list(workflow, review, "recording_warnings")),
        *_review_bullets("Final validations", _review_list(workflow, review, "validations")),
        *(
            [
                "## Exact Script Source",
                "",
                (
                    "Runtime approval authorizes this exact source as one "
                    "script, bound to the script and execution SHA-256 digests."
                ),
                "",
                f"```{Path(str(payload.get('script') or '')).suffix.lstrip('.')}",
                script_source.rstrip("\n"),
                "```",
                "",
            ]
            if script_source is not None
            else []
        ),
        "## Hermes Cron Payload",
        "",
        "```json",
        json.dumps(payload, indent=2, sort_keys=True),
        "```",
        "",
        "## Checklist",
        "",
        f"- Mode: `{mode}`",
        f"- Schedule: `{payload.get('schedule')}`",
        f"- Delivery: `{payload.get('deliver', 'local')}`",
        f"- no_agent: `{payload.get('no_agent')}`",
        f"- Script: `{payload.get('script', '')}`",
        f"- Skills: `{', '.join(payload.get('skills') or [])}`",
        f"- Toolsets: `{', '.join(payload.get('enabled_toolsets') or [])}`",
        "",
        "## Approve",
        "",
        "Approve and schedule this build from the Hermes Agent Workflows tab after review.",
        "",
    ]
    return "\n".join(lines)


def _review_scalar(workflow: dict[str, Any], review: dict[str, Any], key: str) -> str:
    value = review.get(key, workflow.get(key, ""))
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    return json.dumps(value, sort_keys=True)


def _review_list(workflow: dict[str, Any], review: dict[str, Any], key: str) -> list[Any]:
    value = review.get(key, workflow.get(key, []))
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    return [value]


def _review_bullets(title: str, values: list[Any]) -> list[str]:
    lines = [f"### {title}", ""]
    if not values:
        return lines + ["- None declared.", ""]
    lines.extend(f"- {_review_item(value)}" for value in values)
    lines.append("")
    return lines


def _review_item(value: Any) -> str:
    if not isinstance(value, dict):
        return str(value)
    preferred = (
        "id",
        "source_event_seq",
        "executor",
        "surface",
        "action",
        "app",
        "window_title",
        "toolset",
        "tool",
        "path",
        "required",
        "confidence",
        "replay_hint",
        "privacy",
        "readiness",
        "browser_origin",
        "browser_url",
        "browser_title",
        "evidence",
        "note",
    )
    parts = []
    for key in preferred:
        if key in value and value[key] not in (None, "", []):
            parts.append(f"{key}: `{value[key]}`")
    for key in sorted(value):
        if key in preferred or value[key] in (None, "", []):
            continue
        rendered = json.dumps(value[key], sort_keys=True) if isinstance(value[key], (dict, list)) else str(value[key])
        parts.append(f"{key}: `{rendered}`")
    return "; ".join(parts) if parts else json.dumps(value, sort_keys=True)


def _review_preconditions(review: dict[str, Any]) -> list[str]:
    preconditions = review.get("preconditions")
    lines = ["### Trace preconditions", ""]
    if not isinstance(preconditions, dict) or not preconditions:
        return lines + ["- None declared.", ""]
    for key in (
        "platform",
        "permissions",
        "required_apps",
        "required_browser",
        "browser_profile_hint",
        "required_origins",
        "auth_assumptions",
        "viewport",
        "required_files",
        "required_local_state",
        "operator_review_required",
    ):
        value = preconditions.get(key)
        if value in (None, "", [], {}):
            continue
        rendered = json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
        lines.append(f"- {key}: `{rendered}`")
    if len(lines) == 2:
        lines.append("- None declared.")
    lines.append("")
    return lines


def _review_schema(workflow: dict[str, Any], review: dict[str, Any]) -> list[str]:
    schema = review.get("input_schema", workflow.get("input_schema"))
    if schema is None:
        skill = workflow.get("skill") if isinstance(workflow.get("skill"), dict) else {}
        templates = skill.get("templates") if isinstance(skill.get("templates"), dict) else {}
        schema = templates.get("input.schema.json")
    lines = ["### Input schema", ""]
    if schema in (None, ""):
        return lines + ["- None declared.", ""]
    if isinstance(schema, str):
        body = schema
    else:
        body = json.dumps(schema, indent=2, sort_keys=True)
    return lines + ["```json", body, "```", ""]
