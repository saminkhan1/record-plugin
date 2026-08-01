from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..recording_bundle import is_trace_recording_dir, read_json, read_jsonl
from .trace_schema import (
    normalize_trace_row,
    privacy_for_step,
    readiness_for_step,
    supports_schema_version,
    url_origin,
    utc_now,
)
from .evidence import (
    CAPTURE_MANIFEST_SCHEMA,
    capture_completeness,
    instruction_browser_origins,
    load_evidence_pack,
    origin_key,
    seal_capture,
    trace_row_origins,
    verified_capture_acceptance,
)


_SURFACE_EXECUTION_MODES = {
    "browser": ("browser_dom", "browser_dom"),
    "desktop": ("desktop_computer_use", "desktop_computer_use"),
    "tool": ("hermes_tool", "hermes_tool"),
    "file": ("direct", "file_script"),
    "clipboard": ("direct", "file_script"),
}
_SURFACE_ALIASES = {
    "macos": "desktop",
    "screen": "desktop",
    "computer_use": "desktop",
    "web": "browser",
    "website": "browser",
    "filesystem": "file",
    "local_file": "file",
}


class RecordingError(ValueError):
    pass


@dataclass(frozen=True)
class DraftCandidate:
    """A complete, non-writing draft artifact set ready for atomic publication."""

    recording_dir: Path
    output: Path
    workflow: dict[str, Any]
    review_markdown: str
    metadata: dict[str, Any]


def seal_draft_revision(recording_dir: Path) -> dict[str, Any]:
    """Preserve or upgrade the immutable capture seal across draft revisions."""

    # The stopped evidence pack is itself a sealed capture input. Redrafting
    # must not regenerate semantic evidence or bind mutable lifecycle fields
    # into the capture source. A valid v1 seal can be upgraded before those
    # fields change; an invalid seal always remains failed closed.
    root = recording_dir.expanduser().resolve()
    if not (root / "capture-manifest.json").is_file():
        return {}
    try:
        acceptance = verified_capture_acceptance(root)
        blockers = acceptance.get("blockers") if isinstance(acceptance.get("blockers"), list) else []
        if any(
            isinstance(blocker, dict)
            and blocker.get("code") in {"capture_acceptance_invalid", "capture_acceptance_missing"}
            for blocker in blockers
        ):
            return {}
        manifest = read_json(root / "capture-manifest.json")
        if manifest.get("schemaVersion") == CAPTURE_MANIFEST_SCHEMA:
            return manifest
        evidence_pack = load_evidence_pack(root)
    except Exception:
        # Corrupt captures remain draftable but fail closed.
        return {}
    return seal_capture(root, evidence_pack=evidence_pack)


def build_draft_candidate(
    recording_dir: Path,
    *,
    output: Path,
    name: str,
    instructions: str,
    workflow_author: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> DraftCandidate:
    root = recording_dir.expanduser().resolve()
    manifest = read_json(root / "metadata.json") if (root / "metadata.json").is_file() else {}
    if not is_trace_recording_dir(root):
        raise RecordingError("Hermes workflow trace recording bundle not found.")
    if not supports_schema_version(manifest.get("schemaVersion")):
        raise RecordingError("Recording uses unsupported schema; re-record with the current Record recorder.")
    rows = _rows_through_session_end(
        [normalize_trace_row(row) for row in read_jsonl(root / "trace.jsonl")]
    )

    evidence_warning: dict[str, Any] | None = None
    try:
        evidence_pack = load_evidence_pack(root)
        capture_verdict = (
            evidence_pack.get("capture_verdict")
            if isinstance(evidence_pack.get("capture_verdict"), dict)
            else capture_completeness(root, evidence_pack)
        )
    except Exception as exc:
        # The semantic evidence index is derived from the stopped trace. A
        # corrupt/missing index must not strand an otherwise readable capture;
        # the drafter can still use normalized trace rows and instructions.
        evidence_warning = {
            "step_id": None,
            "kind": "capture_quality_gap",
            "code": "evidence_index_unavailable",
            "severity": "review",
            "requires_review_resolution": False,
            "message": (
                "The derived evidence index could not be read, so this draft uses the stopped trace and instructions. "
                f"Review the affected steps. ({type(exc).__name__})"
            ),
        }
        capture_verdict = {
            "ready": False,
            "reasons": [{"code": "evidence_index_unavailable"}],
            "frame_count": 0,
            "media_paths": [],
        }
        evidence_pack = {
            "schemaVersion": "record-evidence-v1",
            "recording_schema": manifest.get("schemaVersion"),
            "session_id": manifest.get("sessionID") or root.name,
            "workflow_id": manifest.get("workflow_id") or root.parent.name,
            "goal": manifest.get("instructions") or manifest.get("goal") or "",
            "summary": {
                "action_count": sum(1 for row in rows if row.get("type") == "action"),
                "outcome_evidence_count": 0,
            },
            "actions": [],
            "artifacts": [],
            "quality": {},
            "capture_verdict": capture_verdict,
        }

    action_rows = [row for row in rows if row.get("type") == "action"]
    chrome_capture_failure = _chrome_action_capture_failure(rows, manifest)
    desktop_output_failure = _desktop_output_capture_failure(rows, manifest)
    workflow_rows = sorted(action_rows, key=_trace_row_sort_key)
    events = [_trace_row_to_event(row) for row in workflow_rows]
    workflow_events = [event for event in events if not _is_recorder_control_event(event)]

    compacted = compact_trace_actions(workflow_events, manifest=manifest)
    steps = compacted["steps"]
    preconditions = _preconditions_from_trace(rows, steps, manifest)
    for step in steps:
        step["privacy"] = privacy_for_step(step)
        step["readiness"] = readiness_for_step(step, preconditions)
    draft_capture_summary = _draft_capture_summary(capture_verdict, evidence_pack)
    warnings = (
        compacted["warnings"]
        + _trace_warnings(rows, steps, preconditions)
        + ([evidence_warning] if evidence_warning else [])
        + _capture_quality_warnings(
            capture_verdict,
            evidence_pack,
            chrome_capture_warning=chrome_capture_failure,
            desktop_output_warning=desktop_output_failure,
        )
    )
    phases = compacted["phases"]
    inputs = _review_inputs(manifest)
    validations = _review_validations(manifest)
    if workflow_author is None:
        raise RecordingError("Hermes workflow drafting requires the Hermes agent author")
    workflow_id = str(manifest.get("workflow_id") or manifest.get("sessionID") or root.name)
    title = name.strip()
    workflow_instructions = instructions.strip()
    if not title or not workflow_instructions:
        raise RecordingError("Name and workflow instructions are required")
    manifest["name"] = title
    manifest["instructions"] = workflow_instructions
    browser_summary = _trace_browser_summary(preconditions)
    review = _review_metadata(
        manifest,
        steps,
        phases=phases,
        inputs=inputs,
        validations=validations,
    )
    review["preconditions"] = preconditions
    review["capture_acceptance"] = verified_capture_acceptance(root)
    author_context = {
        "recording_dir": str(root),
        "name": title,
        "instructions": workflow_instructions,
        "workflow_id": workflow_id,
        "trigger_requested": manifest.get("trigger_requested") is True,
        "requested_schedule": str(manifest.get("schedule") or ""),
        "steps": steps,
        "phases": phases,
        "inputs": inputs,
        "validations": validations,
        "warnings": warnings,
        "preconditions": preconditions,
        "browser_context": _skill_browser_context({"summary": browser_summary}),
        "evidence_pack": evidence_pack,
        "capture_quality": draft_capture_summary,
    }
    authored = workflow_author(author_context)
    workflow = _workflow_from_cron_plan(
        authored,
        workflow_id=workflow_id,
        title=title,
        instructions=workflow_instructions,
        review=review,
        recording_warnings=warnings,
    )
    output = output.expanduser().resolve()
    manifest["state"] = "drafted"
    manifest["drafted_at"] = utc_now()
    return DraftCandidate(
        recording_dir=root,
        output=output,
        workflow=workflow,
        review_markdown=_draft_review_markdown(
            title,
            review,
            capture_summary=draft_capture_summary,
        ),
        metadata=manifest,
    )


def _workflow_from_cron_plan(
    authored: Any,
    *,
    workflow_id: str,
    title: str,
    instructions: str,
    review: dict[str, Any],
    recording_warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(authored, dict):
        raise RecordingError("Hermes cron drafter returned a non-object")
    execution = authored.get("execution")
    if not isinstance(execution, dict):
        raise RecordingError("Hermes cron drafter did not return execution metadata")
    requirements = execution.get("requirements") if isinstance(execution.get("requirements"), dict) else {}
    raw_warnings = authored.get("warnings")
    if not isinstance(raw_warnings, list) or not all(
        isinstance(warning, dict) for warning in raw_warnings
    ):
        raise RecordingError("Hermes cron drafter returned unvalidated warnings")
    execution_warnings = [dict(warning) for warning in raw_warnings]

    runtime_apps = _unique_strings(requirements.get("apps"))
    runtime_origins = _unique_strings(requirements.get("network_origins"))
    runtime_files = _unique_strings(requirements.get("files"))
    runtime_commands = _unique_strings(requirements.get("commands"))
    runtime_toolsets = _unique_strings(execution.get("enabled_toolsets"))
    checks = ["Recording bundle contains structured desktop/browser/file events"]
    checks.extend(f"Required command available: {command}" for command in runtime_commands)
    checks.extend(f"Required app available: {app}" for app in runtime_apps)
    checks.extend(f"Required network origin reachable: {origin}" for origin in runtime_origins)
    if runtime_toolsets:
        checks.append("Selected Hermes cron toolsets are available")

    review = dict(review)
    review.update({
        "selection_reason": str(authored.get("selection_reason") or ""),
        "llm_policy": str(authored.get("llm_policy") or ""),
        "state_strategy": str(authored.get("state_strategy") or ""),
        "idempotency_strategy": str(authored.get("idempotency_strategy") or ""),
        "execution_steps": _unique_strings(authored.get("steps")),
        "recording_warnings": recording_warnings,
        "warnings": execution_warnings,
        "required_apps": runtime_apps,
        "required_browser_origins": runtime_origins,
        "required_files": runtime_files,
        "required_commands": runtime_commands,
        "preflight_checks": checks,
    })
    workflow: dict[str, Any] = {
        "id": workflow_id,
        "name": title,
        "instructions": instructions,
        "execution": execution,
        "trigger": authored.get("trigger") if isinstance(authored.get("trigger"), dict) else {},
        "review": review,
    }
    return workflow


def _unique_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        normalized = item.strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _trace_row_sort_key(row: dict[str, Any]) -> tuple[datetime, int]:
    return (_parse_time(row.get("ts")) or datetime.min.replace(tzinfo=timezone.utc), int(row.get("id") or 0))


def _rows_through_session_end(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Treat the first session.ended row as a hard recording boundary."""

    bounded: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: int(item.get("id") or 0)):
        bounded.append(row)
        if row.get("kind") == "session.ended":
            break
    return bounded


def _chrome_action_capture_failure(rows: list[dict[str, Any]], manifest: dict[str, Any]) -> str:
    chrome_rows = [row for row in rows if row.get("source") == "chrome"]
    chrome_actions = [row for row in chrome_rows if row.get("type") == "action"]
    business_actions = [
        row
        for row in chrome_actions
        if not _is_recorder_control_event(_trace_row_to_event(row))
    ]

    expected_origins = instruction_browser_origins(manifest)
    covered_origins = {
        origin
        for row in business_actions
        for origin in trace_row_origins(row)
    }
    covered_origin_keys = {origin_key(origin) for origin in covered_origins}
    missing_origins = sorted(
        origin for origin in expected_origins if origin_key(origin) not in covered_origin_keys
    )
    if missing_origins:
        origins = ", ".join(missing_origins[:5])
        return (
            "The instructions mention browser activity for "
            f"{origins}, but the trace has no Chrome action rows for that origin. "
            "This draft uses the remaining evidence and instructions; review the affected browser steps."
        )

    business_chrome_rows = [
        row
        for row in chrome_rows
        if trace_row_origins(row)
    ]
    if not business_chrome_rows:
        return ""
    if business_actions:
        return ""

    browser_origins = {
        origin
        for row in business_chrome_rows
        for origin in trace_row_origins(row)
    }
    browser_origins = {
        origin
        for origin in browser_origins
        if origin
        and origin != "null"
        and not origin.startswith("chrome:")
    }
    if not browser_origins:
        return ""

    native_fallback_or_gap = False
    for row in rows:
        if row.get("kind") == "native.chrome_fallback":
            native_fallback_or_gap = True
            break
        quality = _record(row.get("quality"))
        warnings = quality.get("warnings") if isinstance(quality.get("warnings"), list) else []
        if any(str(warning) == "chrome_provider_unavailable" for warning in warnings):
            native_fallback_or_gap = True
            break
    if not native_fallback_or_gap:
        return ""

    origins = ", ".join(sorted(browser_origins)[:5])
    return (
        "The Chrome provider emitted browser context/evidence "
        f"for {origins}, but it emitted no Chrome action rows; the trace fell back to low-confidence native Chrome events. "
        "This draft uses the lower-confidence evidence; review the affected browser steps."
    )


def _desktop_output_capture_failure(rows: list[dict[str, Any]], manifest: dict[str, Any]) -> str:
    expected_paths = _instruction_local_paths(manifest)
    if not expected_paths:
        return ""

    native_actions = [
        row
        for row in rows
        if row.get("source") == "native"
        and row.get("type") == "action"
        and _trace_action_from_original(str(row.get("kind") or "")) in {"type", "paste", "shortcut", "submit", "click"}
    ]
    missing_paths = [
        path
        for path in expected_paths
        if not _has_native_output_save_evidence(native_actions, path, manifest)
    ]
    if not missing_paths:
        return ""
    paths = ", ".join(missing_paths[:5])
    return (
        "The instructions mention a local output at "
        f"{paths}, but the capture does not confirm both a current output and native edit/save evidence tied to it. "
        "This draft uses the remaining evidence; review the affected output and save steps."
    )


def _draft_capture_summary(
    verdict: dict[str, Any], evidence_pack: dict[str, Any]
) -> dict[str, Any]:
    reasons = [
        dict(reason)
        for reason in verdict.get("reasons", [])
        if isinstance(reason, dict) and reason.get("code")
    ]
    summary = evidence_pack.get("summary") if isinstance(evidence_pack.get("summary"), dict) else {}
    quality = evidence_pack.get("quality") if isinstance(evidence_pack.get("quality"), dict) else {}
    return {
        "reason_codes": _unique_strings([str(reason.get("code")) for reason in reasons]),
        "reasons": reasons,
        "action_count": int(summary.get("action_count") or 0),
        "outcome_evidence_count": int(summary.get("outcome_evidence_count") or 0),
        "warnings": _unique_strings(quality.get("warnings")),
    }


def _capture_quality_warnings(
    verdict: dict[str, Any],
    evidence_pack: dict[str, Any],
    *,
    chrome_capture_warning: str = "",
    desktop_output_warning: str = "",
) -> list[dict[str, Any]]:
    """Turn capture diagnostics into review warnings, never lifecycle gates."""

    warnings: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def append(code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        code = code.strip() or "capture_quality_gap"
        message = message.strip()
        key = (code, message)
        if not message or key in seen:
            return
        seen.add(key)
        warning = {
            "step_id": None,
            "kind": "capture_quality_gap",
            "code": code,
            "severity": "review",
            "requires_review_resolution": False,
            "message": message,
        }
        if details:
            warning["details"] = details
        warnings.append(warning)

    for reason in verdict.get("reasons", []):
        if not isinstance(reason, dict):
            continue
        code = str(reason.get("code") or "capture_quality_gap")
        details = {
            key: reason[key]
            for key in ("path", "paths", "origins", "apps", "warnings", "references")
            if reason.get(key) not in (None, "", [], {})
        }
        label = code.replace("_", " ")
        suffix = f" Details: {json.dumps(details, sort_keys=True)}." if details else ""
        append(
            code,
            f"Capture diagnostic: {label}.{suffix} The draft is best-effort; review any affected step.",
            details=details,
        )

    if chrome_capture_warning:
        append("browser_action_evidence_gap", chrome_capture_warning)
    if desktop_output_warning:
        append("desktop_output_evidence_gap", desktop_output_warning)

    # Preserve producer warnings that do not already have a stop-time verdict
    # code. These remain review context, not proof that drafting must stop.
    quality = evidence_pack.get("quality") if isinstance(evidence_pack.get("quality"), dict) else {}
    for value in quality.get("warnings") if isinstance(quality.get("warnings"), list) else []:
        code = str(value or "").strip()
        if code:
            append(code, f"Capture diagnostic: {code.replace('_', ' ')}. Review any affected step.")
    return warnings


def _instruction_local_paths(manifest: dict[str, Any]) -> list[str]:
    text = " ".join(
        str(manifest.get(key) or "").strip()
        for key in ("instructions",)
        if str(manifest.get(key) or "").strip()
    )
    paths: list[str] = []
    for match in re.finditer(r"(?<![A-Za-z0-9:/])/(?!/)[^\s<>\"']+", text):
        value = match.group(0).rstrip(".,;:!?)]}")
        if value != "/" and value not in paths:
            paths.append(value)
    return paths


def _native_action_references_path(row: dict[str, Any], path: str) -> bool:
    payload = _record(row.get("payload"))
    window = _record(payload.get("window"))
    target = _record(payload.get("target"))
    input_payload = _record(payload.get("input"))
    basename = Path(path).name.lower()
    if not basename:
        return False
    values = [
        window.get("title"),
        window.get("name"),
        window.get("url"),
        target.get("label"),
        target.get("title"),
        target.get("name"),
        target.get("value"),
        target.get("value_preview"),
        target.get("text"),
        input_payload.get("value"),
        input_payload.get("text"),
    ]
    return any(basename in str(value or "").lower() for value in values)


def _has_native_output_save_evidence(
    native_actions: list[dict[str, Any]],
    path: str,
    manifest: dict[str, Any],
) -> bool:
    output_path = Path(path).expanduser()
    if not output_path.exists() or not _output_path_is_current(output_path, manifest):
        return False
    references = [
        (index, row)
        for index, row in enumerate(native_actions)
        if _native_action_references_path(row, path)
    ]
    if not references:
        return False
    for reference_index, reference in references:
        reference_app = _native_action_app_key(reference)
        for candidate in native_actions[reference_index:]:
            if _native_action_app_key(candidate) == reference_app and _is_native_save_action(candidate):
                return True
    return False


def _output_path_is_current(path: Path, manifest: dict[str, Any]) -> bool:
    """Reject a pre-existing file that only happens to match the requested name."""

    if not path.is_file():
        return True
    started_at = _parse_time(manifest.get("startedAt"))
    if started_at is None:
        return True
    try:
        modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return False
    # Filesystems can round mtimes to whole seconds, so retain a small tolerance.
    return modified_at.timestamp() + 2 >= started_at.timestamp()


def _native_action_app_key(row: dict[str, Any]) -> str:
    app = _record(_record(row.get("payload")).get("app"))
    return str(app.get("bundle_id") or app.get("bundleIdentifier") or app.get("name") or "").strip()


def _is_native_save_action(row: dict[str, Any]) -> bool:
    kind = _trace_action_from_original(str(row.get("kind") or ""))
    payload = _record(row.get("payload"))
    input_payload = _record(payload.get("input"))
    target = _record(payload.get("target"))
    if kind == "shortcut":
        modifiers = {str(item).lower() for item in input_payload.get("modifiers") or []}
        keys = {
            str(input_payload.get("key") or "").lower(),
            str(input_payload.get("text") or "").lower(),
            str(input_payload.get("code") or "").lower(),
        }
        if "command" in modifiers and ({"s", "1"} & keys):
            return True
    if kind not in {"click", "submit"}:
        return False
    labels = [target.get("label"), target.get("title"), target.get("name")]
    return any(str(value or "").strip().lower() in {"save", "save as"} for value in labels)


def _trace_action_from_original(kind: str) -> str:
    value = kind.replace("mouse.", "").replace("keyboard.", "").replace("selection.", "").strip()
    return _normalize_action(value) if value else "click"


def _trace_row_to_event(row: dict[str, Any]) -> dict[str, Any]:
    payload = _record(row.get("payload"))
    app = _record(payload.get("app"))
    window = _record(payload.get("window"))
    browser = _record(payload.get("browser"))
    target = dict(_record(payload.get("target")))
    input_payload = dict(_record(payload.get("input")))
    effect = dict(_record(payload.get("effect")))
    bundle_id = app.get("bundle_id") or app.get("bundleIdentifier")
    window_title = window.get("title") or window.get("name") or window.get("windowName")
    if bundle_id and not target.get("bundle_id"):
        target["bundle_id"] = bundle_id
    if window_title and not target.get("window_title"):
        target["window_title"] = window_title
    action = _normalize_action(str(row.get("kind") or "observe"))
    if input_payload.get("value") not in (None, "", [], {}):
        target["value"] = input_payload.get("value")
    if input_payload.get("text") not in (None, "", [], {}):
        target["text"] = input_payload.get("text")
    if input_payload.get("redacted"):
        target["redacted"] = True
    if input_payload.get("secure_input"):
        target["secure_input"] = True
    if input_payload.get("files"):
        target["files"] = input_payload.get("files")
    if input_payload.get("selected_text"):
        target["selected_text"] = input_payload.get("selected_text")
    for key in ("key", "code", "modifiers", "button", "click_count", "delta_x", "delta_y", "scroll_x", "scroll_y"):
        if input_payload.get(key) not in (None, "", [], {}) and not target.get(key):
            target[key] = input_payload.get(key)
    if not target.get("coordinates") and any(input_payload.get(key) not in (None, "", [], {}) for key in ("x", "y")):
        target["coordinates"] = {"x": input_payload.get("x"), "y": input_payload.get("y")}
    if browser.get("url") and not browser.get("origin"):
        browser["origin"] = url_origin(browser.get("url"))
    if not browser and window.get("url"):
        browser = _browser_from_window({"url": window.get("url"), "title": window.get("title")})
    event = {
        "seq": row.get("id"),
        "ts": row.get("ts"),
        "surface": "browser" if row.get("source") == "chrome" or browser else "desktop",
        "action": action,
        "app": app.get("name") or bundle_id,
        "window_title": window_title,
        "browser": browser,
        "target": target,
        "input": input_payload,
        "effect": effect,
        "confidence": _record(row.get("quality")).get("confidence") or "medium",
        "note": "; ".join(str(item) for item in (_record(row.get("quality")).get("warnings") or []) if item),
        "evidence": {
            "trace_id": row.get("id"),
            "source": row.get("source"),
            "kind": row.get("kind"),
            "effect": effect,
        },
    }
    return _compact_dict(event)


def _preconditions_from_trace(rows: list[dict[str, Any]], steps: list[dict[str, Any]], manifest: dict[str, Any]) -> dict[str, Any]:
    required_apps: list[dict[str, Any]] = []
    required_origins: list[str] = []
    required_files: list[dict[str, Any]] = []
    viewport: dict[str, Any] = {}
    browser_profile_hint = "current-profile"
    permissions = manifest.get("permissionState") if isinstance(manifest.get("permissionState"), dict) else {}
    for row in rows:
        payload = _record(row.get("payload"))
        app = _record(payload.get("app"))
        if app:
            entry = _compact_dict({
                "name": app.get("name"),
                "bundle_id": app.get("bundle_id") or app.get("bundleIdentifier"),
            })
            if entry and entry not in required_apps:
                required_apps.append(entry)
        browser = _record(payload.get("browser"))
        if browser.get("profile_hint"):
            browser_profile_hint = str(browser.get("profile_hint") or browser_profile_hint)
        if isinstance(browser.get("viewport"), dict) and browser.get("viewport"):
            viewport = browser["viewport"]
        input_payload = _record(payload.get("input"))
        for item in input_payload.get("files") if isinstance(input_payload.get("files"), list) else []:
            if isinstance(item, dict):
                file_entry = _compact_dict({"name": item.get("name"), "size": item.get("size"), "mime": item.get("mime")})
                if file_entry and file_entry not in required_files:
                    required_files.append(file_entry)
        effect = _record(payload.get("effect"))
        download = _record(effect.get("download"))
        if download:
            file_entry = _compact_dict({"name": download.get("filename"), "size": download.get("file_size") or download.get("total_bytes"), "mime": download.get("mime")})
            if file_entry and file_entry not in required_files:
                required_files.append(file_entry)
    # Runtime origins come from reviewable semantic actions, not every frame
    # that happened to load while the user worked. Modern applications embed
    # auth, contacts, payments, ads, analytics, and editor helpers in child
    # frames; treating a passive frame navigation as a workflow dependency
    # invents phases and auth requirements the operator never demonstrated.
    # A real click/type/select inside an embedded frame remains a step and its
    # origin is therefore retained here.
    for step in steps:
        origin = str(
            step.get("browser_origin")
            or url_origin(
                str(
                    step.get("browser_url")
                    or step.get("browser_top_url")
                    or step.get("browser_frame_url")
                    or ""
                )
            )
        ).strip()
        if origin and origin not in required_origins:
            required_origins.append(origin)
    return {
        "platform": "macos",
        "permissions": permissions,
        "required_apps": required_apps,
        "required_browser": "Chrome",
        "browser_profile_hint": browser_profile_hint,
        "required_origins": required_origins,
        "auth_assumptions": [f"Existing browser auth is available for {origin}" for origin in required_origins],
        "viewport": viewport,
        "required_files": required_files,
        "required_local_state": [],
        "operator_review_required": browser_profile_hint == "current-profile" and bool(required_origins),
    }


def _trace_missing_side_effect_warnings(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    warned_apps: set[str] = set()
    pending: dict[str, Any] | None = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("kind") or "")
        payload = _record(row.get("payload"))
        if _record(payload.get("browser")) or row.get("source") == "chrome":
            app_key = ""
        else:
            app = _record(payload.get("app"))
            app_key = str(app.get("bundle_id") or app.get("bundleIdentifier") or app.get("name") or "").strip()
        if pending and row.get("type") == "action" and kind in {"click", "double_click", "context_menu", "submit", "shortcut"} and app_key == pending.get("app_key"):
            pending = None
        if pending and kind == "window.changed":
            window_title = _trace_window_title(row)
            if app_key != pending.get("app_key") or window_title != pending.get("window_title"):
                pending_app = str(pending.get("app_key") or "")
                if pending_app not in warned_apps:
                    warned_apps.add(pending_app)
                    warnings.append({
                        "step_id": None,
                        "kind": "missing_side_effect_action",
                        "requires_review_resolution": True,
                        "trace_id": pending.get("trace_id"),
                        "message": (
                            "Text was entered in "
                            f"{pending.get('app_name') or pending.get('app_key') or 'a desktop app'}"
                            " before the window changed, but the recording contains no click, submit, "
                            "shortcut, or app-specific action proving the side effect happened."
                        ),
                    })
                pending = None
        input_payload = _record(payload.get("input"))
        if row.get("type") != "action" or kind != "type" or not app_key:
            continue
        if input_payload.get("secure_input") or input_payload.get("redacted"):
            continue
        if input_payload.get("text") is None and input_payload.get("value") is None:
            continue
        app = _record(payload.get("app"))
        pending = {
            "app_key": app_key,
            "app_name": app.get("name"),
            "trace_id": row.get("id"),
            "window_title": _trace_window_title(row),
        }
    if pending and str(pending.get("app_key") or "") not in warned_apps:
        warnings.append({
            "step_id": None,
            "kind": "missing_side_effect_action",
            "requires_review_resolution": True,
            "trace_id": pending.get("trace_id"),
            "message": (
                "Text was entered in "
                f"{pending.get('app_name') or pending.get('app_key') or 'a desktop app'}"
                " before recording ended, but the recording contains no click, submit, shortcut, "
                "or app-specific action proving the side effect happened."
            ),
        })
    return warnings


def _trace_window_title(row: dict[str, Any]) -> str:
    window = _record(_record(row.get("payload")).get("window"))
    return str(window.get("title") or window.get("name") or window.get("windowName") or "").strip()


def _trace_warnings(rows: list[dict[str, Any]], steps: list[dict[str, Any]], preconditions: dict[str, Any]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    trace_warning_ids: dict[str, list[int]] = {}
    for row in rows:
        if row.get("kind") == "native.chrome_fallback":
            continue
        quality = _record(row.get("quality"))
        for warning in quality.get("warnings") if isinstance(quality.get("warnings"), list) else []:
            kind = str(warning).strip()
            if kind:
                trace_warning_ids.setdefault(kind, []).append(int(row.get("id") or 0))
    for kind, trace_ids in trace_warning_ids.items():
        trace_ids = [trace_id for trace_id in trace_ids if trace_id]
        message = (
            f"Trace row {trace_ids[0]} warning: {kind}"
            if len(trace_ids) == 1
            else f"{len(trace_ids)} trace rows ({', '.join(str(trace_id) for trace_id in trace_ids)}) warning: {kind}"
        )
        warnings.append({
            "step_id": None,
            "kind": kind,
            "trace_ids": trace_ids,
            "message": message,
            "requires_review_resolution": kind in {"missing_target", "missing_replay_data", "chrome_provider_unavailable"},
        })
    warnings.extend(_trace_missing_side_effect_warnings(rows))
    if preconditions.get("operator_review_required"):
        warnings.append({
            "step_id": None,
            "kind": "browser_profile_unverified",
            "requires_review_resolution": False,
            "message": (
                "Chrome did not expose a stable browser-profile identity; verify "
                "the intended signed-in Chrome account before scheduling."
            ),
        })
    for step in steps:
        readiness = step.get("readiness") if isinstance(step.get("readiness"), dict) else {}
        for warning in readiness.get("warnings") if isinstance(readiness.get("warnings"), list) else []:
            if warning == "browser_profile_unverified" and preconditions.get("operator_review_required"):
                continue
            warnings.append({
                "step_id": step.get("id"),
                "kind": str(warning),
                "message": f"{step.get('id')}: readiness warning {warning}",
                "requires_review_resolution": str(warning) in {"missing_browser_readiness", "missing_desktop_readiness", "browser_profile_unverified"},
            })
    return warnings


def _trace_browser_summary(preconditions: dict[str, Any]) -> list[str]:
    origins = preconditions.get("required_origins") if isinstance(preconditions.get("required_origins"), list) else []
    if not origins:
        return []
    return ["Browser origins required: " + ", ".join(str(item) for item in origins[:8])]








def _browser_from_window(window: dict[str, Any]) -> dict[str, Any]:
    url = str(window.get("url") or "").strip()
    if not url:
        return {}
    return _compact_dict({
        "url": url,
        "origin": url_origin(url),
        "title": window.get("title") or window.get("windowName"),
    })


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        return datetime.fromtimestamp(float(text) / 1000, tz=timezone.utc)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _compact_dict(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, "", [], {})}


def compact_trace_actions(
    events: list[dict[str, Any]],
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    context: dict[str, Any] = {}
    pending_type: dict[str, Any] | None = None
    pending_scroll: dict[str, Any] | None = None
    pending_click: dict[str, Any] | None = None
    pending_navigate: dict[str, Any] | None = None

    def flush_type() -> None:
        nonlocal pending_type
        if pending_type:
            steps.append(pending_type)
            pending_type = None

    def flush_scroll() -> None:
        nonlocal pending_scroll
        if pending_scroll:
            steps.append(pending_scroll)
            pending_scroll = None

    def flush_click() -> None:
        nonlocal pending_click
        if pending_click:
            steps.append(pending_click)
            pending_click = None

    def flush_navigate() -> None:
        nonlocal pending_navigate
        if pending_navigate:
            steps.append(pending_navigate)
            pending_navigate = None

    for event in events:
        normalized = _normalize_event(event)
        action = str(normalized.get("action") or "")
        target = normalized.get("target") if isinstance(normalized.get("target"), dict) else {}
        if _is_snapshot_observe(normalized):
            continue
        if _is_passive_child_frame_navigation(normalized):
            continue
        if _is_modifier_only_shortcut(normalized):
            continue
        if action in {"app_switch", "window_change"}:
            flush_type()
            flush_scroll()
            flush_click()
            context = {
                "app": normalized.get("app") or target.get("app"),
                "window_title": target.get("window_title"),
                "source_event_seq": normalized.get("seq"),
            }
            continue
        if action == "select" and _is_noop_selection(normalized):
            continue
        if action == "navigate":
            flush_type()
            flush_scroll()
            flush_click()
            key = _coalescing_target_key(normalized, action)
            if pending_navigate and pending_navigate.get("target_key") == key:
                source_ids = [
                    int(value)
                    for value in (
                        pending_navigate.get("source_event_seq"),
                        pending_navigate.get("source_event_seq_end"),
                        normalized.get("seq"),
                    )
                    if isinstance(value, int) or (isinstance(value, str) and value.isdigit())
                ]
                if source_ids:
                    pending_navigate["source_event_seq"] = min(source_ids)
                    pending_navigate["source_event_seq_end"] = max(source_ids)
                pending_navigate["event_count"] = int(pending_navigate.get("event_count") or 1) + 1
                pending_navigate["confidence"] = _lower_confidence(str(pending_navigate.get("confidence") or "medium"), str(normalized.get("confidence") or "medium"))
                pending_navigate["note"] = _join_notes(str(pending_navigate.get("note") or ""), str(normalized.get("note") or ""))
                _merge_step_event_metadata(pending_navigate, normalized)
                continue
            flush_navigate()
            pending_navigate = _step_from_event(normalized, context, action="navigate")
            pending_navigate["target_key"] = key
            pending_navigate["event_count"] = 1
            continue
        if action in {"type", "key", "set_value"}:
            flush_navigate()
            flush_click()
            flush_scroll()
            key = f"{action}|{_coalescing_target_key(normalized, action)}"
            if pending_type and pending_type.get("target_key") == key:
                pending_type["source_event_seq_end"] = normalized.get("seq")
                pending_type["event_count"] = int(pending_type.get("event_count") or 1) + 1
                pending_type["confidence"] = _lower_confidence(str(pending_type.get("confidence") or "medium"), str(normalized.get("confidence") or "medium"))
                pending_type["note"] = _join_notes(str(pending_type.get("note") or ""), str(normalized.get("note") or ""))
                _merge_step_event_metadata(pending_type, normalized)
                continue
            flush_type()
            pending_type = _step_from_event(normalized, context, action="key" if action == "key" else "type")
            pending_type["target_key"] = key
            pending_type["event_count"] = 1
            continue
        if action == "scroll":
            flush_navigate()
            flush_type()
            flush_click()
            key = _target_key(normalized)
            if pending_scroll and pending_scroll.get("target_key") == key:
                pending_scroll["source_event_seq_end"] = normalized.get("seq")
                pending_scroll["event_count"] = int(pending_scroll.get("event_count") or 1) + 1
                pending_scroll["note"] = _join_notes(str(pending_scroll.get("note") or ""), str(normalized.get("note") or ""))
                _merge_step_event_metadata(pending_scroll, normalized)
                continue
            flush_scroll()
            pending_scroll = _step_from_event(normalized, context, action="scroll")
            pending_scroll["target_key"] = key
            pending_scroll["event_count"] = 1
            continue
        if action == "click":
            flush_navigate()
            flush_type()
            flush_scroll()
            key = _target_key(normalized)
            if pending_click and pending_click.get("target_key") == key:
                pending_click["source_event_seq_end"] = normalized.get("seq")
                pending_click["event_count"] = int(pending_click.get("event_count") or 1) + 1
                pending_click["confidence"] = _lower_confidence(str(pending_click.get("confidence") or "medium"), str(normalized.get("confidence") or "medium"))
                pending_click["note"] = _join_notes(str(pending_click.get("note") or ""), str(normalized.get("note") or ""))
                _merge_step_event_metadata(pending_click, normalized)
                continue
            flush_click()
            pending_click = _step_from_event(normalized, context, action="click")
            pending_click["target_key"] = key
            pending_click["event_count"] = 1
            continue

        flush_type()
        flush_scroll()
        flush_click()
        flush_navigate()
        step = _step_from_event(normalized, context)
        steps.append(step)

    flush_type()
    flush_scroll()
    flush_click()
    flush_navigate()

    steps = _collapse_text_entry_steps(steps)
    steps = _collapse_duplicate_selections(steps)
    steps = _collapse_incidental_browser_clicks(steps)

    for index, step in enumerate(steps, start=1):
        step["id"] = f"step-{index}"
        step.pop("target_key", None)
    phases = _build_phases(steps, manifest or {})
    for step in steps:
        warnings.extend(_warnings_for_step(step))

    return {"steps": steps, "warnings": warnings, "phases": phases}


def _is_passive_child_frame_navigation(event: dict[str, Any]) -> bool:
    """Drop frame lifecycle noise while preserving actions inside frames.

    Browser providers report iframe document loads as ``navigate`` events.
    Those loads are implementation details unless the operator actually acts
    inside the frame; click/type/select events from that frame are kept by the
    normal path and still carry its origin and targeting metadata.
    """

    if str(event.get("surface") or "") != "browser" or str(event.get("action") or "") != "navigate":
        return False
    browser = _event_browser(event)
    frame_id = browser.get("frame_id")
    parent_frame_id = browser.get("parent_frame_id")
    if parent_frame_id in (None, ""):
        return False
    return str(frame_id or "0") not in {"0", ""}


def _collapse_text_entry_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    collapsed: list[dict[str, Any] | None] = []
    latest_by_key: dict[str, int] = {}

    for step in steps:
        action = str(step.get("action") or "")
        if action not in {"type", "set_value"} or _text_entry_is_secure(step):
            collapsed.append(step)
            continue
        literal = _text_entry_literal(step)
        if not literal.strip():
            collapsed.append(step)
            continue

        target_index = len(collapsed)
        merged = dict(step)
        key = _text_entry_step_key(step, action)
        exact_index = latest_by_key.get(key)
        if exact_index is not None and exact_index < len(collapsed):
            prior = collapsed[exact_index]
            if isinstance(prior, dict):
                merged = _merge_text_entry_metadata(merged, prior)
                collapsed[exact_index] = None

        if merged.get("surface") != "browser":
            context_key = _text_entry_context_key(merged)
            normalized_literal = _normalize_text_entry_literal(literal)
            for index, prior in enumerate(collapsed):
                if not isinstance(prior, dict):
                    continue
                prior_action = str(prior.get("action") or "")
                if prior_action not in {"type", "set_value"} or _text_entry_is_secure(prior):
                    continue
                if _text_entry_context_key(prior) != context_key:
                    continue
                prior_literal = _normalize_text_entry_literal(_text_entry_literal(prior))
                if not prior_literal:
                    continue
                if prior_literal == normalized_literal or prior_literal in normalized_literal:
                    merged = _merge_text_entry_metadata(merged, prior)
                    collapsed[index] = None
                    latest_by_key[_text_entry_step_key(prior, prior_action)] = target_index

        latest_by_key[key] = target_index
        collapsed.append(merged)

    return [step for step in collapsed if isinstance(step, dict)]


def _collapse_duplicate_selections(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    for step in steps:
        previous = compacted[-1] if compacted else None
        if isinstance(previous, dict) and _same_selection(previous, step):
            previous["source_event_seq_end"] = step.get("source_event_seq_end", step.get("source_event_seq"))
            previous["event_count"] = int(previous.get("event_count") or 1) + int(step.get("event_count") or 1)
            previous["confidence"] = _lower_confidence(
                str(previous.get("confidence") or "medium"),
                str(step.get("confidence") or "medium"),
            )
            previous["note"] = _join_notes(str(previous.get("note") or ""), str(step.get("note") or ""))
            continue
        compacted.append(step)
    return compacted


def _same_selection(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left.get("action") != "select" or right.get("action") != "select":
        return False
    left_target = left.get("target") if isinstance(left.get("target"), dict) else {}
    right_target = right.get("target") if isinstance(right.get("target"), dict) else {}
    selected_text = left_target.get("selected_text")
    return (
        isinstance(selected_text, str)
        and bool(selected_text)
        and selected_text == right_target.get("selected_text")
        and _target_key(left) == _target_key(right)
    )


def _collapse_incidental_browser_clicks(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    for index, step in enumerate(steps):
        previous = steps[index - 1] if index else None
        following = steps[index + 1] if index + 1 < len(steps) else None
        after_following = steps[index + 2] if index + 2 < len(steps) else None
        if (
            _is_nonsemantic_browser_click(step)
            and isinstance(previous, dict)
            and isinstance(following, dict)
            and previous.get("action") == "select"
            and following.get("action") == "copy"
            and _same_browser_context(previous, step)
            and _same_browser_context(step, following)
        ):
            continue
        if (
            _is_nonsemantic_browser_click(step)
            and isinstance(following, dict)
            and isinstance(after_following, dict)
            and following.get("action") == "select"
            and after_following.get("action") == "copy"
            and _same_browser_context(step, following)
            and _same_browser_context(step, after_following)
        ):
            continue
        compacted.append(step)
    return compacted


def _is_modifier_only_shortcut(event: dict[str, Any]) -> bool:
    if str(event.get("action") or "") != "shortcut":
        return False
    target = event.get("target") if isinstance(event.get("target"), dict) else {}
    input_payload = event.get("input") if isinstance(event.get("input"), dict) else {}
    values = {
        str(value or "").strip().lower()
        for value in (
            target.get("key"),
            target.get("code"),
            input_payload.get("key"),
            input_payload.get("code"),
        )
    }
    return bool(values & {
        "meta", "metaleft", "metaright", "control", "controlleft", "controlright",
        "alt", "altleft", "altright", "shift", "shiftleft", "shiftright",
        "os", "fn", "capslock",
    })


def _is_nonsemantic_browser_click(step: dict[str, Any]) -> bool:
    if step.get("surface") != "browser" or step.get("action") != "click":
        return False
    target = step.get("target") if isinstance(step.get("target"), dict) else {}
    if _has_semantic_browser_target(target):
        return False
    return str(target.get("tag") or "").lower() in {"article", "body", "div", "html", "main", "section"}


def _same_browser_context(left: dict[str, Any], right: dict[str, Any]) -> bool:
    def key(step: dict[str, Any]) -> str:
        target = step.get("target") if isinstance(step.get("target"), dict) else {}
        return (
            str(step.get("browser_url") or step.get("browser_top_url") or step.get("browser_frame_url") or "")
            or str(step.get("browser_origin") or "")
            or str(target.get("url") or target.get("origin") or "")
        )
    left_key = key(left)
    return bool(left_key and left_key == key(right))


def _text_entry_step_key(step: dict[str, Any], action: str) -> str:
    target = step.get("target") if isinstance(step.get("target"), dict) else {}
    surface = str(step.get("surface") or "")
    if surface == "browser":
        origin = (
            str(step.get("browser_origin") or "")
            or url_origin(str(step.get("browser_url") or step.get("browser_top_url") or step.get("browser_frame_url") or ""))
            or str(target.get("origin") or "")
            or url_origin(str(target.get("url") or target.get("top_url") or target.get("frame_url") or ""))
        )
        identity = [
            str(target.get("selector") or ""),
            str(target.get("id") or ""),
            str(target.get("name") or ""),
            str(target.get("role") or ""),
            str(target.get("label") or ""),
            str(target.get("tag") or ""),
        ]
        if not any(identity):
            identity = [
                str(target.get("som_index") or target.get("element_index") or target.get("x") or ""),
                str(target.get("y") or ""),
            ]
        return "|".join(["text_entry", surface, origin, *identity])

    app_key = str(target.get("bundle_id") or step.get("bundle_id") or step.get("app") or target.get("app") or "")
    window_title = str(target.get("window_title") or step.get("window_title") or "")
    semantic_label = str(
        target.get("title")
        or target.get("label")
        or target.get("ax_title")
        or target.get("ax_label")
        or target.get("name")
        or step.get("target_label")
        or ""
    )
    coordinates: list[str] = []
    if not semantic_label:
        coordinates = [
            str(target.get("som_index") or target.get("element_index") or target.get("x") or ""),
            str(target.get("y") or ""),
        ]
    if not semantic_label and not any(coordinates):
        coordinates = []
    return "|".join(["text_entry", surface, app_key, window_title, semantic_label, *coordinates])


def _text_entry_context_key(step: dict[str, Any]) -> str:
    target = step.get("target") if isinstance(step.get("target"), dict) else {}
    if step.get("surface") == "browser":
        origin = (
            str(step.get("browser_origin") or "")
            or url_origin(str(step.get("browser_url") or step.get("browser_top_url") or step.get("browser_frame_url") or ""))
            or str(target.get("origin") or "")
            or url_origin(str(target.get("url") or target.get("top_url") or target.get("frame_url") or ""))
        )
        return "|".join(["browser", origin])
    return "|".join([
        str(step.get("surface") or ""),
        str(target.get("bundle_id") or step.get("bundle_id") or step.get("app") or target.get("app") or ""),
        str(target.get("window_title") or step.get("window_title") or ""),
    ])


def _text_entry_literal(step: dict[str, Any]) -> str:
    target = step.get("target") if isinstance(step.get("target"), dict) else {}
    value = target.get("value")
    if isinstance(value, str) and value.strip():
        return value
    text = target.get("text")
    return text if isinstance(text, str) else ""


def _normalize_text_entry_literal(value: str) -> str:
    return " ".join(value.casefold().split())


def _text_entry_is_secure(step: dict[str, Any]) -> bool:
    target = step.get("target") if isinstance(step.get("target"), dict) else {}
    return bool(target.get("secure_input") or target.get("redacted"))


def _merge_text_entry_metadata(retained: dict[str, Any], removed: dict[str, Any]) -> dict[str, Any]:
    merged = dict(retained)
    if removed.get("source_event_seq") is not None:
        merged["source_event_seq"] = removed["source_event_seq"]
    latest_end = retained.get("source_event_seq_end", retained.get("source_event_seq"))
    if latest_end is not None:
        merged["source_event_seq_end"] = latest_end
    merged["event_count"] = int(retained.get("event_count") or 1) + int(removed.get("event_count") or 1)
    merged["confidence"] = _lower_confidence(str(retained.get("confidence") or "medium"), str(removed.get("confidence") or "medium"))
    merged["note"] = _join_notes(str(removed.get("note") or ""), str(retained.get("note") or ""))
    candidates = _union_target_candidates(
        removed.get("target_candidates") if isinstance(removed.get("target_candidates"), list) else [],
        retained.get("target_candidates") if isinstance(retained.get("target_candidates"), list) else [],
    )
    if candidates:
        merged["target_candidates"] = candidates
    return merged


def _union_target_candidates(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for group in groups:
        for candidate in group:
            if not isinstance(candidate, dict):
                continue
            key = json.dumps(candidate, sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            result.append(candidate)
    return result

def _review_metadata(
    manifest: dict[str, Any],
    steps: list[dict[str, Any]],
    *,
    phases: list[dict[str, Any]],
    inputs: list[dict[str, Any]],
    validations: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "summary": _review_summary(manifest, steps),
        "phases": phases,
        "inputs": inputs,
        "validations": validations,
        "steps": steps,
    }


def _skill_browser_context(chrome_context: dict[str, Any]) -> list[str]:
    summary = chrome_context.get("summary") if isinstance(chrome_context.get("summary"), list) else []
    result: list[str] = []
    for item in summary:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text:
            continue
        scrubbed = _scrub_recorder_context_line(text)
        if scrubbed:
            result.append(scrubbed)
    return result


def _scrub_recorder_context_line(text: str) -> str:
    lower = text.lower()
    recorder_markers = (
        "stop and draft",
        "hermes agent recording started",
        "hermes workflows",
        "agent - dashboard",
    )
    if not any(marker in lower for marker in recorder_markers):
        return text
    return ""



def _review_step(event: dict[str, Any]) -> dict[str, Any]:
    seq = event.get("seq")
    target = event.get("target") if isinstance(event.get("target"), dict) else {}
    browser = _event_browser(event)
    evidence = event.get("evidence") if isinstance(event.get("evidence"), dict) else target.get("evidence")
    step: dict[str, Any] = {
        "id": f"step-{seq}" if seq else "step",
        "source_event_seq": seq,
        "executor": _executor_for_surface(str(event.get("surface") or "")),
        "surface": event.get("surface"),
        "action": event.get("action"),
    }
    for key in ("app", "note"):
        if event.get(key):
            step[key] = event[key]
    if browser:
        for key in ("origin", "url", "title", "tab_id", "frame_id", "parent_frame_id", "document_id", "frame_url", "top_url", "active_target_id"):
            if browser.get(key):
                step[f"browser_{key}"] = browser[key]
    for key in ("confidence", "replay_hint"):
        if event.get(key):
            step[key] = event[key]
        elif target.get(key):
            step[key] = target[key]
    if isinstance(evidence, dict) and evidence:
        step["evidence"] = evidence
    for key in ("toolset", "tool"):
        if target.get(key):
            step[key] = target[key]
    if target:
        step["target"] = target
    target_candidates = _target_candidates(event)
    if target_candidates:
        step["target_candidates"] = target_candidates
    return step


def _executor_for_surface(surface: str) -> str:
    return _SURFACE_EXECUTION_MODES.get(surface, ("direct", "human_review"))[0]


def _event_browser(event: dict[str, Any]) -> dict[str, Any]:
    browser = event.get("browser")
    if isinstance(browser, dict):
        return browser
    target = event.get("target")
    if isinstance(target, dict) and isinstance(target.get("browser"), dict):
        return target["browser"]
    return {}


def _normalize_event(event: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(event)
    normalized["surface"] = _normalize_surface(str(normalized.get("surface") or "desktop"))
    normalized["action"] = _normalize_action(str(normalized.get("action") or "observe"))
    target = normalized.get("target") if isinstance(normalized.get("target"), dict) else {}
    normalized_target = dict(target)
    if normalized_target:
        normalized["target"] = normalized_target
    normalized.setdefault("confidence", _confidence_for_event(str(normalized["surface"]), str(normalized["action"]), normalized_target, normalized.get("evidence") if isinstance(normalized.get("evidence"), dict) else None))
    if (
        normalized["surface"] == "browser"
        and normalized["action"] not in {"navigate", "observe"}
        and not _has_semantic_browser_target(normalized_target)
    ):
        normalized["confidence"] = _lower_confidence(str(normalized.get("confidence") or "medium"), "low")
    normalized.setdefault("replay_hint", _replay_hint_for_surface(str(normalized["surface"])))
    return normalized


def _normalize_surface(surface: str) -> str:
    value = surface.strip().lower().replace("-", "_")
    return _SURFACE_ALIASES.get(value, value or "desktop")


def _normalize_action(action: str) -> str:
    value = action.strip().lower().replace("-", "_")
    aliases = {
        "mousedown": "click",
        "mouse_down": "click",
        "left_mouse_down": "click",
        "right_mouse_down": "click",
        "keypress": "key",
        "keydown": "key",
        "key_down": "key",
        "input": "type",
        "text": "type",
        "text_input": "type",
        "change": "type",
        "changed": "select",
        "wheel": "scroll",
        "recording_start": "observe",
        "recording_stop": "observe",
    }
    return aliases.get(value, value or "observe")


def _confidence_for_event(surface: str, action: str, target: dict[str, Any], evidence: dict[str, Any] | None) -> str:
    if target.get("coordinates") or {"x", "y"}.issubset(set(target)):
        return "low"
    if surface == "browser" and _has_semantic_browser_target(target):
        return "high"
    if surface == "desktop" and any(target.get(key) for key in ("ax_role", "ax_label", "role", "name", "som_index", "element_index")):
        return "medium"
    if evidence and evidence.get("success") is False:
        return "low"
    return "medium"


def _has_semantic_browser_target(target: dict[str, Any]) -> bool:
    if any(target.get(key) for key in ("href", "id", "test_id", "name")):
        return True
    if str(target.get("role") or "").strip():
        return True
    return str(target.get("tag") or "").lower() in {"a", "button", "input", "textarea", "select", "option", "summary"}


def _replay_hint_for_surface(surface: str) -> str:
    return _SURFACE_EXECUTION_MODES.get(surface, ("direct", "human_review"))[1]


def _target_key(event: dict[str, Any]) -> str:
    target = event.get("target") if isinstance(event.get("target"), dict) else {}
    browser = _event_browser(event)
    parts = [
        str(event.get("surface") or ""),
        str(event.get("app") or ""),
        str(browser.get("url") or browser.get("origin") or ""),
        str(target.get("selector") or target.get("ax_label") or target.get("label") or target.get("name") or target.get("text") or ""),
        str(target.get("som_index") or target.get("element_index") or target.get("x") or ""),
        str(target.get("y") or ""),
    ]
    return "|".join(parts)


def _coalescing_target_key(event: dict[str, Any], action: str) -> str:
    target = event.get("target") if isinstance(event.get("target"), dict) else {}
    browser = _event_browser(event)
    surface = str(event.get("surface") or "")
    if surface == "browser" and action == "navigate":
        origin = str(browser.get("origin") or "") or url_origin(
            str(browser.get("url") or browser.get("top_url") or browser.get("frame_url") or "")
        )
        return "|".join([
            surface,
            origin,
            str(browser.get("tab_id") or ""),
            str(browser.get("frame_id") or "0"),
        ])
    if surface == "browser" and action in {"type", "key", "set_value"}:
        origin = (
            str(browser.get("origin") or "")
            or url_origin(str(browser.get("url") or browser.get("top_url") or browser.get("frame_url") or ""))
            or str(target.get("origin") or "")
            or url_origin(str(target.get("url") or target.get("top_url") or target.get("frame_url") or ""))
        )
        identity = [
            str(target.get("selector") or ""),
            str(target.get("id") or ""),
            str(target.get("name") or ""),
            str(target.get("role") or ""),
            str(target.get("label") or ""),
            str(target.get("tag") or ""),
        ]
        if not any(identity):
            identity = [
                str(target.get("som_index") or target.get("element_index") or target.get("x") or ""),
                str(target.get("y") or ""),
            ]
        return "|".join([surface, str(event.get("app") or ""), origin, *identity])
    if surface == "desktop" and action in {"type", "key", "set_value"}:
        semantic_label = str(
            target.get("title")
            or target.get("label")
            or target.get("ax_title")
            or target.get("ax_label")
            or target.get("name")
            or ""
        )
        coordinates = []
        if not semantic_label:
            coordinates = [
                str(target.get("som_index") or target.get("element_index") or target.get("x") or ""),
                str(target.get("y") or ""),
            ]
        return "|".join([
            surface,
            str(target.get("bundle_id") or event.get("bundle_id") or event.get("app") or target.get("app") or ""),
            str(target.get("window_title") or event.get("window_title") or ""),
            str(target.get("role") or target.get("ax_role") or ""),
            semantic_label,
            *coordinates,
        ])
    return _target_key(event)


def _merge_step_event_metadata(step: dict[str, Any], event: dict[str, Any]) -> None:
    if event.get("app"):
        step["app"] = event["app"]
    browser = _event_browser(event)
    if browser:
        for key in ("origin", "url", "title", "tab_id", "frame_id", "parent_frame_id", "document_id", "frame_url", "top_url", "active_target_id"):
            if browser.get(key):
                step[f"browser_{key}"] = browser[key]
    target = event.get("target") if isinstance(event.get("target"), dict) else {}
    if target:
        merged_target = dict(step.get("target") if isinstance(step.get("target"), dict) else {})
        merged_target.update(target)
        step["target"] = merged_target
        label = _target_label(event)
        if label:
            step["target_label"] = label
        if target.get("window_title"):
            step["window_title"] = target["window_title"]
    target_candidates = _target_candidates(event)
    if target_candidates:
        step["target_candidates"] = target_candidates


def _step_from_event(event: dict[str, Any], context: dict[str, Any], *, action: str | None = None) -> dict[str, Any]:
    step = _review_step(event)
    step["action"] = action or step.get("action")
    target = event.get("target") if isinstance(event.get("target"), dict) else {}
    step["target_label"] = _target_label(event)
    if context and not step.get("app"):
        step["app"] = context.get("app")
    if context and context.get("window_title") and not target.get("window_title"):
        step["window_title"] = context.get("window_title")
    step["confidence"] = step.get("confidence") or _confidence_for_event(str(step.get("surface") or ""), str(step.get("action") or ""), target, step.get("evidence") if isinstance(step.get("evidence"), dict) else None)
    return step


def _target_label(event: dict[str, Any]) -> str:
    target = event.get("target") if isinstance(event.get("target"), dict) else {}
    browser = _event_browser(event)
    for key in ("label", "name", "text", "ax_label", "ax_title", "window_title", "path", "file", "folder"):
        value = target.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if browser.get("title"):
        return str(browser["title"])
    if browser.get("url"):
        return str(browser["url"])
    if {"x", "y"}.issubset(set(target)):
        return f"coordinates {target.get('x')},{target.get('y')}"
    return ""


def _warnings_for_step(step: dict[str, Any]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    target = step.get("target") if isinstance(step.get("target"), dict) else {}
    candidates = step.get("target_candidates") if isinstance(step.get("target_candidates"), list) else []
    target_label = str(step.get("target_label") or "")
    low_surface = str(step.get("surface") or "")
    if step.get("confidence") == "low":
        warnings.append({
            "step_id": step.get("id"),
            "kind": "low_confidence",
            "requires_review_resolution": False,
            "message": f"{step.get('id')}: review low-confidence {low_surface} action before replay.",
        })
    semantic_candidates = [
        candidate for candidate in candidates
        if isinstance(candidate, dict) and candidate.get("kind") != "coordinate"
    ]
    coordinate_only = _target_has_coordinates(target) and not semantic_candidates
    if coordinate_only or target_label.startswith("coordinates"):
        warnings.append({
            "step_id": step.get("id"),
            "kind": "coordinate_target",
            "requires_review_resolution": True,
            "message": f"{step.get('id')}: coordinate-only target should be confirmed or replaced with an accessibility/DOM target.",
        })
    if str(target.get("surface_type") or "").lower() in {"canvas", "remote_desktop", "custom_rendered"}:
        warnings.append({
            "step_id": step.get("id"),
            "kind": "weak_surface",
            "requires_review_resolution": False,
            "message": f"{step.get('id')}: weak UI surface needs human review before replay.",
        })
    if _step_needs_browser_auth(step):
        warnings.append({
            "step_id": step.get("id"),
            "kind": "browser_auth",
            "requires_review_resolution": False,
            "message": f"{step.get('id')}: captured browser page appears unauthenticated; sign in and re-record this browser step.",
        })
    return warnings


def _step_needs_browser_auth(step: dict[str, Any]) -> bool:
    if step.get("surface") != "browser":
        return False
    target = step.get("target") if isinstance(step.get("target"), dict) else {}
    parts = [
        step.get("browser_url"),
        step.get("browser_title"),
        step.get("target_label"),
        target.get("url"),
        target.get("title"),
        target.get("label"),
        target.get("text"),
    ]
    text = " ".join(str(part or "").lower() for part in parts)
    if not text:
        return False
    auth_markers = (
        "/login",
        "/signin",
        "/sign-in",
        "login",
        "log in",
        "sign in",
        "signin",
        "authenticate",
        "authentication",
    )
    return any(marker in text for marker in auth_markers)


def _is_snapshot_observe(event: dict[str, Any]) -> bool:
    if event.get("action") != "observe":
        return False
    target = event.get("target") if isinstance(event.get("target"), dict) else {}
    evidence = event.get("evidence") if isinstance(event.get("evidence"), dict) else {}
    phase = str(target.get("phase") or "")
    kind = str(evidence.get("kind") or "")
    return bool(phase) or kind in {"desktop_snapshot", "browser_snapshot"}


def _is_noop_selection(event: dict[str, Any]) -> bool:
    if event.get("action") != "select":
        return False
    target = event.get("target") if isinstance(event.get("target"), dict) else {}
    if target.get("selected_text") not in (None, ""):
        return False
    if target.get("selected_items") not in (None, [], {}):
        return False
    selected_range = target.get("selected_range")
    if selected_range in (None, "", [], {}):
        return True
    if isinstance(selected_range, dict):
        length = selected_range.get("length")
        if length in (0, "0"):
            return True
    return False



def _is_recorder_control_event(event: dict[str, Any]) -> bool:
    chunks: list[str] = []

    def collect(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, str):
            chunks.append(value)
            return
        if isinstance(value, (int, float, bool)):
            chunks.append(str(value))
            return
        if isinstance(value, dict):
            for item in value.values():
                collect(item)
            return
        if isinstance(value, list):
            for item in value:
                collect(item)

    target = event.get("target") if isinstance(event.get("target"), dict) else {}
    evidence = event.get("evidence") if isinstance(event.get("evidence"), dict) else {}
    raw_event = evidence.get("raw_event") if isinstance(evidence.get("raw_event"), dict) else {}
    collect(event.get("app"))
    collect(event.get("note"))
    collect(_event_browser(event))
    collect(target)
    collect(raw_event)
    lower = "\n".join(chunks).lower()
    markers = {
        "workflows",
        "record",
        "recording",
        "stop",
        "recording started",
        "recording stopped",
        "stop and draft",
        "draft again",
        "drafting",
        "request permissions & start",
        "request permissions and start",
        "discard recording",
        "hermes workflows",
        "hermes agent",
    }
    if event.get("surface") == "browser":
        return False

    app_chunks: list[str] = []
    for value in (
        event.get("app"),
        target.get("app"),
        target.get("bundle_id"),
        raw_event.get("app") if isinstance(raw_event, dict) else None,
    ):
        before = len(chunks)
        collect(value)
        app_chunks.extend(chunks[before:])
    app_text = "\n".join(app_chunks).lower()
    is_hermes = "hermes" in app_text or "agent - dashboard" in lower
    return is_hermes and any(marker in lower for marker in markers)

def _target_candidates(event: dict[str, Any]) -> list[dict[str, Any]]:
    target = event.get("target") if isinstance(event.get("target"), dict) else {}
    browser = _event_browser(event)
    surface = str(event.get("surface") or "")
    candidates: list[dict[str, Any]] = []

    if surface == "browser" or browser:
        dom = _compact_candidate({
            "kind": "dom",
            "confidence": "high" if any(target.get(key) for key in ("selector", "role", "label", "name", "text")) else "medium",
            "url": target.get("url") or browser.get("url"),
            "origin": target.get("origin") or browser.get("origin"),
            "title": target.get("title") or browser.get("title"),
            "selector": target.get("selector"),
            "role": target.get("role"),
            "label": target.get("label") or target.get("name") or target.get("text"),
            "frame_id": target.get("frame_id") or browser.get("frame_id"),
            "frame_url": target.get("frame_url") or browser.get("frame_url"),
            "top_url": target.get("top_url") or browser.get("top_url"),
        })
        if len(dom) > 2:
            candidates.append(dom)

    if any(target.get(key) for key in ("ax_role", "ax_label", "ax_title", "role", "name", "som_index", "element_index")):
        candidates.append(_compact_candidate({
            "kind": "accessibility",
            "confidence": "medium",
            "role": target.get("ax_role") or target.get("role"),
            "label": target.get("ax_label") or target.get("ax_title") or target.get("name"),
            "som_index": target.get("som_index"),
            "element_index": target.get("element_index"),
            "window_title": target.get("window_title"),
        }))

    for key in ("path", "file", "folder", "download_path", "upload_path"):
        value = target.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append(_compact_candidate({
                "kind": "file",
                "confidence": "high",
                "path": value.strip(),
                "field": key,
            }))

    if target.get("tool") or target.get("toolset"):
        candidates.append(_compact_candidate({
            "kind": "tool",
            "confidence": "high",
            "toolset": target.get("toolset"),
            "tool": target.get("tool"),
        }))

    if _target_has_coordinates(target):
        candidates.append(_compact_candidate({
            "kind": "coordinate",
            "confidence": "low",
            "x": target.get("x"),
            "y": target.get("y"),
            "coordinates": target.get("coordinates"),
        }))

    return [candidate for candidate in candidates if candidate]


def _compact_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in candidate.items() if value not in (None, "", [], {})}


def _target_has_coordinates(target: dict[str, Any]) -> bool:
    return bool(target.get("coordinates")) or {"x", "y"}.issubset(set(target))


def _build_phases(steps: list[dict[str, Any]], manifest: dict[str, Any]) -> list[dict[str, Any]]:
    phases: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    current_key = ""

    for step in steps:
        key = _phase_key(step)
        if not current or key != current_key:
            current_key = key
            current = {
                "id": f"phase-{len(phases) + 1}",
                "title": _phase_title(step, len(phases) + 1),
                "surface": step.get("surface"),
                "app": step.get("app"),
                "browser_origin": step.get("browser_origin"),
                "window_title": step.get("window_title"),
                "step_ids": [],
                "source_event_seq_start": step.get("source_event_seq"),
                "source_event_seq_end": step.get("source_event_seq_end") or step.get("source_event_seq"),
                "confidence": step.get("confidence") or "medium",
                "targeting": [],
                "checkpoint": "Confirm this phase reached the demonstrated state before continuing.",
            }
            phases.append(current)
        step["phase_id"] = current["id"]
        current["step_ids"].append(step.get("id"))
        current["source_event_seq_end"] = step.get("source_event_seq_end") or step.get("source_event_seq")
        current["confidence"] = _lower_confidence(str(current.get("confidence") or "medium"), str(step.get("confidence") or "medium"))
        for candidate in (step.get("target_candidates") if isinstance(step.get("target_candidates"), list) else []):
            if isinstance(candidate, dict):
                kind = candidate.get("kind")
                if isinstance(kind, str) and kind not in current["targeting"]:
                    current["targeting"].append(kind)

    if not phases and manifest:
        phases.append({
            "id": "phase-1",
            "title": str(manifest.get("title") or "Recorded workflow"),
            "step_ids": [],
            "confidence": "medium",
            "targeting": [],
            "checkpoint": "Review the captured evidence before replay.",
        })
    return phases


def _phase_key(step: dict[str, Any]) -> str:
    surface = str(step.get("surface") or "")
    browser_origin = str(step.get("browser_origin") or "")
    app = str(step.get("app") or "")
    if browser_origin:
        return "|".join([surface, browser_origin])
    if app:
        return "|".join([surface, app])
    return "|".join([surface, str(step.get("window_title") or "")])


def _phase_title(step: dict[str, Any], index: int) -> str:
    surface = str(step.get("surface") or "workflow").replace("_", " ").title()
    if step.get("browser_origin"):
        return f"{surface}: {step.get('browser_origin')}"
    if step.get("app"):
        return f"{surface}: {step.get('app')}"
    if step.get("target_label"):
        return f"{surface}: {step.get('target_label')}"
    return f"Phase {index}"


def _review_inputs(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    review = manifest.get("review") if isinstance(manifest.get("review"), dict) else {}
    raw = review.get("inputs") or manifest.get("inputs") or manifest.get("variable_inputs") or []
    if isinstance(raw, list) and all(isinstance(item, dict) for item in raw):
        return [_normalize_input_item(item, index) for index, item in enumerate(raw, start=1)]
    return _review_inputs_from_names(_unique_strings(raw))


def _review_inputs_from_names(names: list[str]) -> list[dict[str, Any]]:
    used: set[str] = set()
    items: list[dict[str, Any]] = []
    for index, name in enumerate(names, start=1):
        key = _input_key(name, used, index)
        used.add(key)
        items.append({
            "key": key,
            "name": name,
            "type": "string",
            "policy": "variable",
            "required": True,
        })
    return items


def _normalize_input_item(item: dict[str, Any], index: int) -> dict[str, Any]:
    key = str(item.get("key") or "").strip()
    name = str(item.get("name") or item.get("title") or key or f"input {index}").strip()
    if not key:
        key = _input_key(name, set(), index)
    return {
        "key": key,
        "name": name,
        "type": str(item.get("type") or "string").strip() or "string",
        "policy": str(item.get("policy") or "variable").strip() or "variable",
        "required": bool(item.get("required", True)),
    }


def _review_validations(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    review = manifest.get("review") if isinstance(manifest.get("review"), dict) else {}
    raw = review.get("validations") or manifest.get("validations") or []
    if isinstance(raw, list) and all(isinstance(item, dict) for item in raw):
        return [_normalize_validation_item(item, index) for index, item in enumerate(raw, start=1)]
    validations = _review_validations_from_text(_unique_strings(raw))
    if validations:
        return validations
    return [{
        "kind": "user_confirmed",
        "description": "Confirm the final screen, file, or record matches the demonstrated outcome.",
        "required": True,
    }]


def _review_validations_from_text(values: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "kind": _validation_kind(value),
            "description": value,
            "required": True,
        }
        for value in values
    ]


def _normalize_validation_item(item: dict[str, Any], index: int) -> dict[str, Any]:
    description = str(item.get("description") or item.get("text") or item.get("name") or f"Validation {index}").strip()
    return {
        "kind": str(item.get("kind") or _validation_kind(description)).strip() or "user_confirmed",
        "description": description,
        "required": bool(item.get("required", True)),
        **({key: item[key] for key in ("path", "text", "selector", "origin") if item.get(key)}),
    }


def _validation_kind(value: str) -> str:
    text = value.lower()
    if "file" in text and any(word in text for word in ("exist", "created", "download")):
        return "file_exists"
    if "page" in text and ("text" in text or "contains" in text):
        return "page_contains_text"
    if "row" in text or "spreadsheet" in text:
        return "row_updated"
    return "user_confirmed"


def _input_key(name: str, used: set[str], index: int) -> str:
    cleaned = re.sub(r"[^a-z0-9_]+", "_", name.strip().lower()).strip("_")
    cleaned = cleaned or f"input_{index}"
    candidate = cleaned
    suffix = 2
    while candidate in used:
        candidate = f"{cleaned}_{suffix}"
        suffix += 1
    return candidate


def _lower_confidence(left: str, right: str) -> str:
    order = {"high": 3, "medium": 2, "low": 1}
    return left if order.get(left, 2) <= order.get(right, 2) else right


def _join_notes(left: str, right: str) -> str:
    if not left:
        return right
    if not right or right in left:
        return left
    return f"{left}; {right}"


def _review_summary(manifest: dict[str, Any], steps: list[dict[str, Any]]) -> str:
    title = str(manifest.get("name") or "workflow")
    instructions = str(manifest.get("instructions") or "").strip()
    surfaces = sorted({str(step.get("surface")) for step in steps if step.get("surface")})
    surface_text = ", ".join(surfaces) if surfaces else "desktop"
    if instructions:
        return f"{title}: {instructions} Captured as {len(steps)} reviewable {surface_text} step(s)."
    return f"{title}: captured as {len(steps)} reviewable {surface_text} step(s)."


def _draft_review_markdown(
    title: str,
    review: dict[str, Any],
    *,
    capture_summary: dict[str, Any],
) -> str:
    acceptance = (
        review.get("capture_acceptance")
        if isinstance(review.get("capture_acceptance"), dict)
        else {}
    )
    acceptance_blockers = (
        acceptance.get("blockers")
        if isinstance(acceptance.get("blockers"), list)
        else []
    )
    lines = [
        f"# Draft Review: {title}",
        "",
        str(review.get("summary") or ""),
        "",
        "## Capture Quality",
        "",
        f"- Capture complete: {'no' if acceptance_blockers else 'yes'}",
        f"- Captured actions: {int(capture_summary.get('action_count') or 0)}",
        f"- Outcome evidence: {int(capture_summary.get('outcome_evidence_count') or 0)}",
        "",
        "## Inputs And Requirements",
        "",
    ]
    if review.get("selection_reason"):
        lines.extend([
            "## Selected Hermes Cron Job",
            "",
            f"- LLM policy: {review.get('llm_policy')}",
            f"- Selection: {review.get('selection_reason')}",
            f"- State: {review.get('state_strategy')}",
            f"- Idempotency: {review.get('idempotency_strategy')}",
            "",
        ])
    for label, key in (
        ("Replay inputs", "inputs"),
        ("Required apps", "required_apps"),
        ("Required browser origins", "required_browser_origins"),
        ("Required files/folders", "required_files"),
        ("Typed/key sequences", "typed_sequences"),
        ("Captured values and parameters", "captured_values_and_parameters"),
    ):
        lines.append(f"### {label}")
        values = review.get(key) if isinstance(review.get(key), list) else []
        if values:
            for value in values:
                lines.append(f"- {json.dumps(value, sort_keys=True) if isinstance(value, dict) else value}")
        else:
            lines.append("- None captured")
        lines.append("")
    lines.extend(["## Phases", ""])
    for phase in review.get("phases") if isinstance(review.get("phases"), list) else []:
        lines.append(
            f"- {phase.get('id')}: {phase.get('title')} "
            f"steps={', '.join(map(str, phase.get('step_ids') or []))} confidence={phase.get('confidence')}"
        )
    if not isinstance(review.get("phases"), list) or not review.get("phases"):
        lines.append("- None drafted")
    lines.append("")
    lines.extend(["## Steps", ""])
    for step in review.get("steps") if isinstance(review.get("steps"), list) else []:
        lines.append(
            f"- {step.get('id')}: {step.get('surface')} {step.get('action')}"
            f" via {step.get('executor')} confidence={step.get('confidence')}"
            f"{' - ' + str(step.get('note')) if step.get('note') else ''}"
        )
    warnings = review.get("warnings") if isinstance(review.get("warnings"), list) else []
    if warnings:
        lines.extend(["", "## Needs Review", ""])
        for warning in warnings:
            lines.append(f"- {warning.get('message')}")
    recording_warnings = review.get("recording_warnings") if isinstance(review.get("recording_warnings"), list) else []
    if recording_warnings:
        lines.extend(["", "## Recording-Only Warnings", ""])
        for warning in recording_warnings:
            lines.append(f"- {warning.get('message')}")
    lines.extend(["", "## Verification", ""])
    for item in review.get("validations") if isinstance(review.get("validations"), list) else []:
        lines.append(f"- {json.dumps(item, sort_keys=True) if isinstance(item, dict) else item}")
    return "\n".join(lines) + "\n"
