from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
)
from pydantic.json_schema import SkipJsonSchema
from hermes_cli.web_models import CronJobCreate

from ..recording_bundle import write_json
from .review_receipt import (
    load_compiled_artifacts,
    payload_hash,
    write_artifact_manifest,
)
from .evidence import capture_acceptance_blockers


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
    "record/recordings/",
)


class CompileError(ValueError):
    pass


@dataclass(frozen=True)
class CompileResult:
    payload: dict[str, Any]


NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _DraftContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DraftScript(_DraftContract):
    path: NonEmptyString
    body: NonEmptyString


class DraftJobSpec(CronJobCreate):
    """The Hermes ``create_job`` arguments reviewed by the operator.

    Hermes owns the field contract. Record only tightens extra-field handling
    and makes its host-owned name/schedule optional in the drafting tool call.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    schedule: str = ""
    name: str = ""
    enabled_toolsets: list[NonEmptyString] | None = Field(
        default=None,
        max_length=0,
    )


class DraftWarning(_DraftContract):
    kind: NonEmptyString
    severity: Literal["info", "review"]
    message: NonEmptyString
    step: int | SkipJsonSchema[None] = Field(default=None, ge=1)
    verify: NonEmptyString | SkipJsonSchema[None] = None


class DraftTrigger(_DraftContract):
    requested: bool
    feasibility: Literal["not_requested", "supported", "unsupported"]
    description: str
    source: NonEmptyString | SkipJsonSchema[None] = None
    events: list[NonEmptyString]
    filters: (
        dict[str, Any]
        | list[dict[str, Any]]
        | SkipJsonSchema[None]
    ) = None
    reason: NonEmptyString


class DraftPlan(_DraftContract):
    steps: list[NonEmptyString] = Field(min_length=1, max_length=6)
    trigger: DraftTrigger | SkipJsonSchema[None] = None
    job_spec: DraftJobSpec
    script_source: DraftScript | SkipJsonSchema[None] = None
    warnings: list[DraftWarning]


def draft_submission_schema() -> dict[str, Any]:
    """Return the tool schema generated from the canonical draft contract."""

    plan_schema = _inline_contract_schema(
        DraftPlan.model_json_schema(mode="validation")
    )
    return {
        "type": "object",
        "properties": {"plan": plan_schema},
        "required": ["plan"],
        "additionalProperties": False,
    }


def _inline_contract_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline Pydantic definitions for Hermes' cross-provider tool subset."""

    definitions = schema.pop("$defs", {})

    def resolve(node: Any, stack: tuple[str, ...] = ()) -> Any:
        if isinstance(node, list):
            return [resolve(item, stack) for item in node]
        if not isinstance(node, dict):
            return node
        reference = node.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            name = reference.removeprefix("#/$defs/")
            if name in stack or name not in definitions:
                raise RuntimeError(f"Cannot inline draft schema reference: {reference}")
            siblings = {key: value for key, value in node.items() if key != "$ref"}
            return {
                **resolve(definitions[name], (*stack, name)),
                **resolve(siblings, stack),
            }
        return {key: resolve(value, stack) for key, value in node.items()}

    return resolve(schema)


def _contract_error(subject: str, exc: ValidationError) -> CompileError:
    details = []
    for error in exc.errors(include_input=False, include_url=False):
        location = ".".join(str(item) for item in error["loc"])
        details.append(f"{location}: {error['msg']}" if location else error["msg"])
    return CompileError(f"{subject} violates the Record draft contract: " + "; ".join(details))


def validate_draft_plan(
    plan: Any,
    *,
    job_defaults: dict[str, str] | None = None,
    script_namespace: str = "",
) -> dict[str, Any]:
    """Return one complete, compiler-safe Hermes job proposal.

    Raw model data crosses exactly one typed boundary here. Record injects the
    immutable values it already owns and stores the resulting ``job_spec`` as
    the authority used for both review and publication.
    """

    try:
        contract = DraftPlan.model_validate(plan)
    except ValidationError as exc:
        raise _contract_error("Draft", exc) from exc

    plan = contract.model_dump(mode="python")
    job_spec = dict(plan["job_spec"])
    trigger = plan.get("trigger")
    webhook_requested = (
        isinstance(trigger, dict) and trigger.get("requested") is True
    )
    for key, value in (job_defaults or {}).items():
        if key in {"name", "schedule", "deliver"}:
            job_spec[key] = str(value or "").strip()
    if webhook_requested:
        job_spec["schedule"] = ""

    script_source = _draft_script(plan.get("script_source"))
    if script_source is None:
        job_spec["script"] = None
    else:
        namespace = _safe_relative_path(script_namespace, "script namespace")
        source_path = _safe_relative_path(
            script_source["path"],
            "script_source.path",
        )
        job_spec["script"] = (namespace / source_path).as_posix()

    job_spec = validate_job_spec(
        job_spec,
        require_schedule=not webhook_requested,
    )
    if webhook_requested:
        _validate_webhook_execution_fields(job_spec)
    no_agent = job_spec["no_agent"]
    prompt = _optional_text(job_spec.get("prompt"), "job_spec.prompt")
    skills = _string_list(job_spec.get("skills"), "job_spec.skills")
    toolsets = _string_list(
        job_spec.get("enabled_toolsets"),
        "job_spec.enabled_toolsets",
    )

    if no_agent:
        if webhook_requested:
            raise CompileError(
                "Webhook workflows must use a Hermes agent run"
            )
        if script_source is None:
            raise CompileError("No-agent jobs require script_source")
        if prompt or skills or toolsets:
            raise CompileError(
                "No-agent jobs cannot include prompt, skills, or enabled_toolsets"
            )
    else:
        if prompt is None:
            raise CompileError("Agent jobs require job_spec.prompt")

    _reject_recording_evidence_runtime_references(
        prompt=prompt,
        script=script_source,
    )
    steps = _draft_steps(plan.get("steps"))

    warnings = plan["warnings"]
    normalized_warnings: list[dict[str, Any]] = []
    for warning in warnings:
        message = warning["message"]
        severity = warning["severity"]
        kind = warning["kind"]
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
        normalized_warning = {
            "kind": kind,
            "severity": severity,
            "message": message,
        }
        if kind == "capture_quality_gap" and has_step:
            normalized_warning["step"] = step
            normalized_warning["verify"] = verify.strip()
        normalized_warnings.append(normalized_warning)

    return {
        "steps": steps,
        "trigger": _draft_trigger(plan.get("trigger")),
        "job_spec": job_spec,
        "script_source": script_source,
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
        raise CompileError(
            "trigger.feasibility must be not_requested, supported, or unsupported"
        )
    description = str(value.get("description") or "").strip()
    source = str(value.get("source") or "").strip() or None
    reason = str(value.get("reason") or "").strip()
    events = _string_list(value.get("events"), "trigger.events")
    filters = value.get("filters")
    if filters in (None, [], {}):
        filters = None
    else:
        try:
            from .webhook_filters import normalize_webhook_filter

            filters = normalize_webhook_filter(
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


def _draft_script(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise CompileError("script_source must be an object or null")
    path = _safe_relative_path(_required_str(value, "path"), "script_source.path")
    if path.suffix.lower() not in {".py", ".sh", ".bash"}:
        raise CompileError("script_source.path must end in .py, .sh, or .bash")
    body = value.get("body")
    if not isinstance(body, str) or not body.strip():
        raise CompileError("script_source.body is required")
    _validate_script_syntax(path, body)
    return {"path": path.as_posix(), "body": body}


def _validate_script_syntax(path: Path, body: str) -> None:
    """Parse a proposed script without executing it against a live system."""

    try:
        with tempfile.NamedTemporaryFile("w", suffix=path.suffix, encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            if path.suffix.lower() == ".py":
                import py_compile

                py_compile.compile(handle.name, doraise=True)
            else:
                completed = subprocess.run(
                    ["bash", "-n", handle.name],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if completed.returncode:
                    raise CompileError(
                        "script_source has invalid shell syntax: "
                        + (completed.stderr.strip() or "bash -n failed")
                    )
    except CompileError:
        raise
    except Exception as exc:
        raise CompileError(f"script_source has invalid syntax: {exc}") from exc


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
            "job_spec must not reference local recording evidence at runtime: " + leaked
        )


def review_blockers(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only host-proven capture failures that prevent publication.

    Model-authored warnings are review information, never lifecycle gates. A
    compiler-valid Hermes job proposal is always shown after a usable capture.
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
    return blockers


def record_install_guard(workflow: dict[str, Any]) -> dict[str, Any]:
    """Return the non-bypassable pre-install review guard."""

    blockers = review_blockers(workflow)
    return {
        "blockers": blockers,
        "requires_review_resolution": bool(blockers),
    }


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CompileError(f"{field} must be a string or null")
    return value.strip() or None


def _validate_webhook_execution_fields(job_spec: dict[str, Any]) -> None:
    unsupported = [
        field
        for field in (
            "model",
            "provider",
            "base_url",
            "context_from",
            "enabled_toolsets",
            "workdir",
        )
        if job_spec.get(field) not in (None, "", [])
    ]
    if unsupported:
        raise CompileError(
            "Webhook workflows use the active Hermes profile and cannot "
            "override: " + ", ".join(unsupported)
        )


def validate_job_spec(
    value: Any,
    *,
    require_schedule: bool = True,
) -> dict[str, Any]:
    """Validate the reviewed execution fields with Hermes' maintained type."""

    try:
        canonical = DraftJobSpec.model_validate(value).model_dump(
            mode="python",
            exclude_none=True,
        )
    except ValidationError as exc:
        raise _contract_error("Job specification", exc) from exc
    if not str(canonical.get("name") or "").strip():
        raise CompileError("job_spec.name is required")
    if require_schedule and not str(canonical.get("schedule") or "").strip():
        raise CompileError("job_spec.schedule is required")
    if canonical.get("enabled_toolsets") == []:
        canonical.pop("enabled_toolsets")
    return canonical


@dataclass
class PromotedArtifacts:
    """Reversible promotion of a reviewed build into one profile's runtime."""

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
    trigger = workflow.get("trigger")
    webhook_requested = (
        isinstance(trigger, dict) and trigger.get("requested") is True
    )
    job_spec = validate_job_spec(
        workflow.get("job_spec"),
        require_schedule=not webhook_requested,
    )
    if webhook_requested:
        _validate_webhook_execution_fields(job_spec)
    no_agent = job_spec["no_agent"]
    script_source = _draft_script(workflow.get("script_source"))
    script_name = job_spec.get("script")
    if bool(script_source) != bool(script_name):
        raise CompileError(
            "job_spec.script and script_source must either both be present or both be absent"
        )
    prompt = _optional_text(job_spec.get("prompt"), "job_spec.prompt")
    skills = _string_list(job_spec.get("skills"), "job_spec.skills")
    toolsets = _string_list(
        job_spec.get("enabled_toolsets"),
        "job_spec.enabled_toolsets",
    )
    if no_agent:
        if webhook_requested:
            raise CompileError(
                "Webhook workflows must use a Hermes agent run"
            )
        if not script_source or prompt or skills or toolsets:
            raise CompileError(
                "No-agent jobs require only script_source and cannot include prompt, skills, or enabled_toolsets"
            )
    elif prompt is None:
        raise CompileError("Agent jobs require job_spec.prompt")
    _reject_recording_evidence_runtime_references(
        prompt=prompt,
        script=script_source,
    )

    out_dir = out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    runtime_root = out_dir / "staged-runtime"
    shutil.rmtree(runtime_root, ignore_errors=True)
    scripts_dir = runtime_root / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    artifacts: list[dict[str, Any]] = []
    payload: dict[str, Any] = {"action": "create", **job_spec}
    if script_source and isinstance(script_name, str):
        written_name = _write_script(
            script_source,
            scripts_dir,
            required=True,
            destination_prefix=None,
            destination=script_name,
        )
        if written_name != script_name:
            raise CompileError("Staged script destination changed unexpectedly")
        artifacts.append(_script_artifact(script_name))

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
        "type": "record",
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
    destination: str | None = None,
) -> str | None:
    if script is None:
        if required:
            raise CompileError("script_source is required for no-agent jobs")
        return None
    if not isinstance(script, dict):
        raise CompileError("script must be an object")
    script_path = _safe_relative_path(_required_str(script, "path"), "script.path")
    body = script.get("body")
    final_path = (
        _safe_relative_path(destination, "job_spec.script")
        if destination is not None
        else (destination_prefix / script_path if destination_prefix else script_path)
    )
    target = scripts_dir / final_path
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(body, str):
        target.write_text(_ensure_trailing_newline(body), encoding="utf-8")
    else:
        raise CompileError("script requires body")
    return final_path.as_posix()


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
    """Promote reviewed staged files, with rollback for a failed Cron install."""

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
            temporary = destination.with_name(f".{destination.name}.record-{token}.tmp")
            backup = destination.with_name(f".{destination.name}.record-{token}.backup")
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
        "This is the exact Hermes job that will be created when you approve it.",
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
        (
            "- Exact-script build review: `required`"
            if payload.get("script")
            else "- Exact-script build review: `not required`"
        ),
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
                    "The install-time review envelope and receipt bind this "
                    "exact source, its SHA-256, and the canonical Cron payload."
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
        "## Reviewed Record Build Payload",
        "",
        "This is the canonical payload Record submits to Hermes Cron.",
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
