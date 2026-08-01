"""Evidence-first recording artifacts used by Record drafting.

The native recorder remains the authority for source data.  This module only
assembles a deterministic, bounded index over that data after capture stops.
It deliberately never creates runtime cron artifacts.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from ..recording_bundle import read_json, read_jsonl, write_json

from .trace_schema import (
    action_requires_target,
    has_replayable_target,
    http_url_origin,
    normalize_trace_row,
    privacy_for_step,
    utc_now,
)


LEGACY_CAPTURE_MANIFEST_SCHEMA = "record-capture-manifest-v1"
CAPTURE_MANIFEST_SCHEMA = "record-capture-manifest-v2"
CAPTURE_ACCEPTANCE_SCHEMA = "record-capture-acceptance-v1"

# These are the only conditions that mean the recording itself cannot be
# trusted. Coverage and correlation gaps are semantic drafting inputs, not
# capture-integrity failures.
_CAPTURE_INTEGRITY_BLOCKER_CODES = frozenset({
    "capture_acceptance_missing",
    "capture_acceptance_invalid",
})

# Only stop-time evidence belongs in the immutable source boundary. Metadata
# and session are lifecycle projections: drafting, scheduling, repair, and
# dismissal legitimately update them after capture has stopped.
_IMMUTABLE_CAPTURE_PATHS = (
    "trace.jsonl",
    "raw/input.jsonl",
    "raw/context.jsonl",
    "raw/active-surface.jsonl",
    "visual/frames.jsonl",
    "evidence-pack.json",
)

# Early v2 seals included this retired Chrome sidecar. Accept it when it is
# already bound by a manifest, but do not add it to new seals.
_V2_SEALED_COMPAT_PATHS = frozenset({"chrome-continuity.json"})

_BLOCKING_WARNINGS = {
    "chrome_provider_continuity_failed",
    "continuity_status_not_persisted",
    "heartbeat_gap_detected",
    "heartbeat_stale",
    "missing_active_surface_visual",
    "missing_heartbeat",
    "missing_raw_input",
    "missing_replay_data",
    "missing_target",
}

_MUTATION_ACTIONS = {
    "change", "cut", "drop", "key", "paste", "reset", "select",
    "shortcut", "submit", "type", "upload",
}

_AX_POST_STATE_WINDOW_SECONDS = 1.0


def build_evidence_pack(recording_dir: Path) -> dict[str, Any]:
    """Build the drafting-only action/effect index for one stopped recording."""

    root = recording_dir.expanduser().resolve()
    metadata = read_json(root / "metadata.json")
    trace = [normalize_trace_row(row) for row in read_jsonl(root / "trace.jsonl")]
    raw_input = read_jsonl(root / "raw" / "input.jsonl")
    raw_context = read_jsonl(root / "raw" / "context.jsonl")
    surfaces = read_jsonl(root / "raw" / "active-surface.jsonl")
    frames = read_jsonl(root / "visual" / "frames.jsonl")

    actions = [
        row
        for row in trace
        if row.get("type") == "action"
    ]
    capsules = [
        _action_capsule(
            action,
            trace=trace,
            raw_input=raw_input,
            raw_context=raw_context,
            surfaces=surfaces,
            frames=frames,
        )
        for action in actions
    ]
    quality = _quality(capsules, metadata, trace=trace, surfaces=surfaces)
    pack = {
        "schemaVersion": "record-evidence-v1",
        "recording_schema": metadata.get("schemaVersion"),
        "session_id": metadata.get("sessionID") or root.name,
        "workflow_id": metadata.get("workflow_id") or root.parent.name,
        "goal": metadata.get("instructions") or metadata.get("goal") or "",
        "capture": {
            "platform": "macos",
            "started_at": metadata.get("startedAt"),
            "ended_at": metadata.get("endedAt"),
            "permission_state": metadata.get("permissionState") or {},
            "capture_state": _read_optional_json(root / "capture-state.json"),
        },
        "summary": _summary(capsules),
        "actions": capsules,
        "active_surfaces": surfaces,
        "artifacts": _artifact_index(root),
        "quality": quality,
    }
    write_json(root / "evidence-pack.json", pack)
    return pack


def finalize_evidence_pack(
    recording_dir: Path,
    evidence_pack: dict[str, Any],
    *,
    verdict: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Publish the one canonical stop-time verdict into the evidence pack."""

    root = recording_dir.expanduser().resolve()
    quality = dict(evidence_pack.get("quality")) if isinstance(evidence_pack.get("quality"), dict) else {}
    verdict = verdict or capture_completeness(root, evidence_pack)
    quality.pop("capture_complete", None)
    quality.pop("reasons", None)
    finalized = {
        **evidence_pack,
        "quality": quality,
        "capture_verdict": verdict,
    }
    finalized.pop("semantic_quality", None)
    write_json(root / "evidence-pack.json", finalized)
    return finalized


def seal_capture(recording_dir: Path, *, evidence_pack: dict[str, Any] | None = None) -> dict[str, Any]:
    """Hash immutable capture artifacts after queues and writers have drained."""

    root = recording_dir.expanduser().resolve()
    evidence_pack = evidence_pack or build_evidence_pack(root)
    if not isinstance(evidence_pack.get("capture_verdict"), dict):
        evidence_pack = finalize_evidence_pack(root, evidence_pack)
    files = _sealed_files(root)
    manifest = {
        "schemaVersion": CAPTURE_MANIFEST_SCHEMA,
        "session_id": evidence_pack.get("session_id") or root.name,
        "created_at": utc_now(),
        "files": [
            {"path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size, "sha256": _sha256(path)}
            for path in files
        ],
    }
    write_json(root / "capture-manifest.json", manifest)
    return manifest


def capture_source_digest(
    recording_dir: Path,
) -> str:
    """Hash the canonical capture inputs independently of their seal envelope."""

    root = recording_dir.expanduser().resolve()
    digest = hashlib.sha256()
    for path in _sealed_files(root):
        relative = path.relative_to(root).as_posix()
        content = path.read_bytes()
        encoded_path = relative.encode("utf-8")
        digest.update(len(encoded_path).to_bytes(8, "big"))
        digest.update(encoded_path)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def capture_acceptance(
    evidence_pack: dict[str, Any],
    *,
    manifest_sha256: str,
) -> dict[str, Any]:
    """Bind a verified seal to its semantic evidence summary.

    This function is called only after the host has verified the immutable
    capture manifest. Missing targets, mutations, visuals, origins, apps, and
    post-state receipts can lower drafting confidence, but they do not make a
    valid sealed recording corrupt. Those signals remain in the evidence pack
    and recording warnings for the drafter to resolve through a better plan,
    clarification, or a controlled test.
    """

    summary = evidence_pack.get("summary") if isinstance(evidence_pack.get("summary"), dict) else {}

    return {
        "schemaVersion": CAPTURE_ACCEPTANCE_SCHEMA,
        "capture_manifest_sha256": manifest_sha256,
        "blockers": [],
        "summary": {
            "action_count": int(summary.get("action_count") or 0),
            "target_evidence_count": int(summary.get("target_evidence_count") or 0),
            "mutation_evidence_count": int(summary.get("mutation_evidence_count") or 0),
            "outcome_evidence_count": int(summary.get("outcome_evidence_count") or 0),
        },
    }


def capture_acceptance_blockers(value: Any) -> list[dict[str, Any]] | None:
    """Return capture-integrity blockers, or ``None`` for an invalid record.

    Older drafts may contain semantic coverage entries in ``blockers``. Keep
    accepting that external JSON shape, but do not promote those advisory
    entries into a re-record requirement.
    """

    if not isinstance(value, dict) or value.get("schemaVersion") != CAPTURE_ACCEPTANCE_SCHEMA:
        return None
    blockers = value.get("blockers")
    if not isinstance(blockers, list) or not all(isinstance(item, dict) for item in blockers):
        return None
    integrity_blockers = [
        dict(item)
        for item in blockers
        if item.get("code") in _CAPTURE_INTEGRITY_BLOCKER_CODES
    ]
    sentinel = len(blockers) == 1 and bool(integrity_blockers)
    manifest_sha256 = str(value.get("capture_manifest_sha256") or "")
    if not sentinel and not re.fullmatch(r"[0-9a-f]{64}", manifest_sha256):
        return None
    return integrity_blockers


def _capture_acceptance_failure(
    code: str,
    message: str,
    *,
    details: list[str] | None = None,
) -> dict[str, Any]:
    blocker: dict[str, Any] = {
        "code": code,
        "message": message,
        "resolution": "rerecord",
    }
    if details:
        blocker["details"] = details
    return {
        "schemaVersion": CAPTURE_ACCEPTANCE_SCHEMA,
        "blockers": [blocker],
    }


def verified_capture_acceptance(recording_dir: Path) -> dict[str, Any]:
    """Return a host-verified acceptance record or a fail-closed replacement."""

    root = recording_dir.expanduser().resolve()
    required = [
        root / "capture-manifest.json",
        root / "evidence-pack.json",
    ]
    if not all(path.is_file() for path in required):
        return _capture_acceptance_failure(
            "capture_acceptance_missing",
            "Hermes could not verify the saved recording. Record the workflow again before scheduling it.",
        )
    errors: list[str] = []
    try:
        manifest_path = root / "capture-manifest.json"
        evidence_path = root / "evidence-pack.json"
        manifest = read_json(manifest_path)
        evidence_pack = read_json(evidence_path)
        manifest_schema = manifest.get("schemaVersion")
        if manifest_schema not in {
            LEGACY_CAPTURE_MANIFEST_SCHEMA,
            CAPTURE_MANIFEST_SCHEMA,
        }:
            errors.append("capture_manifest_schema_invalid")

        listed: set[str] = set()
        for item in manifest.get("files") if isinstance(manifest.get("files"), list) else []:
            if not isinstance(item, dict):
                errors.append("capture_manifest_entry_invalid")
                continue
            relative = str(item.get("path") or "")
            candidate = (root / relative).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                errors.append("capture_manifest_path_invalid")
                continue
            listed.add(relative)
            if (
                not candidate.is_file()
                or candidate.stat().st_size != item.get("bytes")
                or _sha256(candidate) != str(item.get("sha256") or "")
            ):
                errors.append(f"capture_manifest_file_mismatch:{relative}")
        expected_files = (
            _legacy_sealed_files(root)
            if manifest_schema == LEGACY_CAPTURE_MANIFEST_SCHEMA
            else _sealed_files(root)
        )
        expected_paths = {path.relative_to(root).as_posix() for path in expected_files}
        if manifest_schema == CAPTURE_MANIFEST_SCHEMA:
            file_set_matches = (
                expected_paths <= listed
                and listed <= expected_paths | _V2_SEALED_COMPAT_PATHS
            )
        else:
            file_set_matches = listed == expected_paths
        if not file_set_matches:
            errors.append("capture_manifest_file_set_mismatch")

        manifest_sha256 = _sha256(manifest_path)
        expected = capture_acceptance(evidence_pack, manifest_sha256=manifest_sha256)
        if not errors:
            return expected
    except (OSError, TypeError, ValueError, json.JSONDecodeError, HTTPException):
        errors.append("capture_acceptance_unreadable")

    return _capture_acceptance_failure(
        "capture_acceptance_invalid",
        "Hermes could not verify the saved recording. Record the workflow again before scheduling it.",
        details=sorted(set(errors)),
    )


def load_evidence_pack(recording_dir: Path) -> dict[str, Any]:
    root = recording_dir.expanduser().resolve()
    path = root / "evidence-pack.json"
    return read_json(path) if path.is_file() else build_evidence_pack(root)


def capture_completeness(recording_dir: Path, evidence_pack: dict[str, Any]) -> dict[str, Any]:
    """Validate stopped capture indexes without assuming native filenames.

    The native recorder owns media naming. ``visual/frames.jsonl`` is the
    cross-language contract: every frame names the media artifact containing
    it. Following those references keeps segmentation and naming changes from
    silently drifting away from the Python readiness gate.
    """

    root = recording_dir.expanduser().resolve()
    reasons: list[dict[str, Any]] = []
    quality = evidence_pack.get("quality") if isinstance(evidence_pack.get("quality"), dict) else {}
    quality_reasons = quality.get("reasons") if isinstance(quality.get("reasons"), list) else []
    for reason in quality_reasons:
        if isinstance(reason, dict) and reason.get("code"):
            _append_reason(reasons, reason)
    raw_input = root / "raw" / "input.jsonl"
    native_actions = [
        item
        for item in evidence_pack.get("actions", [])
        if isinstance(item, dict) and str(item.get("source") or "") == "native"
    ]
    # Chrome actions are already source receipts from the browser provider.
    # Native actions, however, must retain the immutable OS/Accessibility input
    # row that caused them; otherwise unrelated context can masquerade as a run.
    if native_actions and not _nonempty_file(raw_input):
        _append_reason(reasons, {"code": "missing_raw_input", "path": "raw/input.jsonl"})

    frames_path = root / "visual" / "frames.jsonl"
    frame_rows = read_jsonl(frames_path)
    if not frame_rows:
        _append_reason(reasons, {"code": "missing_frame_index", "path": "visual/frames.jsonl"})

    media_paths: list[str] = []
    invalid_references: list[str] = []
    for row in frame_rows:
        payload = _dict(row.get("payload"))
        reference = str(payload.get("path") or row.get("path") or "").strip()
        if not reference:
            invalid_references.append("<missing>")
            continue
        normalized = _validated_visual_media_reference(root, reference)
        if normalized is None:
            invalid_references.append(reference)
        elif normalized not in media_paths:
            media_paths.append(normalized)
    if invalid_references:
        _append_reason(reasons, {
            "code": "invalid_frame_media_reference",
            "references": invalid_references[:10],
        })
    if frame_rows and not media_paths:
        _append_reason(reasons, {"code": "missing_frame_media_reference", "path": "visual/frames.jsonl"})
    for relative in media_paths:
        if not _nonempty_file(root / relative):
            _append_reason(reasons, {"code": "missing_referenced_media", "path": relative})

    return {
        "ready": not reasons,
        "reasons": reasons,
        "frame_count": len(frame_rows),
        "media_paths": media_paths,
    }


def _action_capsule(
    action: dict[str, Any],
    *,
    trace: list[dict[str, Any]],
    raw_input: list[dict[str, Any]],
    raw_context: list[dict[str, Any]],
    surfaces: list[dict[str, Any]],
    frames: list[dict[str, Any]],
) -> dict[str, Any]:
    payload = _dict(action.get("payload"))
    meta = _dict(payload.get("meta"))
    action_id = str(meta.get("correlation_id") or f"trace-{action.get('id')}")
    raw_rows = [row for row in raw_input if _correlation(row) == action_id]
    raw_attribution = "correlation_id" if raw_rows else ""
    nearby_raw: list[dict[str, Any]] = []
    # Native input is immutable OS source evidence. It intentionally does not
    # borrow a derived action ID. Timestamp proximity only selects candidates;
    # an app/window, target, or segment identity must still attribute them to
    # this action. Unattributed candidates remain diagnostics, never proof.
    if not raw_rows and str(action.get("source")) == "native":
        raw_candidates = _nearby_rows(raw_input, action, seconds=0.6)
        raw_rows = [
            row for row in raw_candidates
            if _row_matches_action_identity(row, action, surfaces=surfaces)
        ]
        nearby_raw = [row for row in raw_candidates if row not in raw_rows]
        if raw_rows:
            raw_attribution = "surface_or_target_identity"
    before = _nearest_before(raw_context, action)
    after = _nearest_after(raw_context, action)
    surface = _surface_for_action(surfaces, action)
    visual, nearby_visual, visual_attribution = _visual_for_action(
        frames,
        action,
        action_id,
        surfaces=surfaces,
    )
    effect_rows, nearby_effect_rows = _effect_rows(trace, action, surfaces=surfaces)
    target = _dict(payload.get("target"))
    input_payload = _dict(payload.get("input"))
    step = {"target": target, "input": input_payload}
    warnings = list(_dict(action.get("quality")).get("warnings") or [])
    if not raw_rows and str(action.get("source")) == "native":
        warnings.append("missing_raw_input")
    if not visual and str(action.get("source")) == "native":
        warnings.append("missing_active_surface_visual")
    if action_requires_target(action) and not has_replayable_target(action):
        warnings.append("missing_target")
    return {
        "id": action_id,
        "trace_id": action.get("id"),
        "timestamp": action.get("ts"),
        "source": action.get("source"),
        "action": action.get("kind"),
        "raw_source": _raw_range(raw_rows),
        "raw_source_attribution": raw_attribution or "unattributed",
        "nearby_raw_diagnostic": _raw_range(nearby_raw),
        "surface": surface,
        "target": target,
        "input": input_payload,
        "pre_state": before,
        "post_state": after,
        "effects": effect_rows,
        "verified_post_state": _verified_post_state(action, trace=trace),
        "nearby_effect_diagnostic": nearby_effect_rows,
        "visual": visual,
        "visual_attribution": visual_attribution or "unattributed",
        "nearby_visual_diagnostic": nearby_visual,
        "privacy": privacy_for_step(step),
        "confidence": _dict(action.get("quality")).get("confidence") or "medium",
        "warnings": _unique(warnings),
    }


def _summary(capsules: list[dict[str, Any]]) -> dict[str, Any]:
    apps = _unique(
        str(
            _dict(_dict(item.get("surface")).get("app")).get("bundle_id")
            or _dict(_dict(item.get("surface")).get("app")).get("bundleIdentifier")
            or ""
        )
        for item in capsules
    )
    actions = _unique(str(item.get("action") or "") for item in capsules)
    blocking = [item["id"] for item in capsules if any(w in _BLOCKING_WARNINGS for w in item.get("warnings", []))]
    mutations = [item for item in capsules if str(item.get("action") or "") in _MUTATION_ACTIONS]
    targeted_mutations = [
        item for item in mutations
        if item.get("target") and "missing_target" not in item.get("warnings", [])
    ]
    causally_evidenced_mutations = [
        item for item in targeted_mutations
        if _has_causal_post_mutation_evidence(item)
    ]
    return {
        "action_count": len(capsules),
        "actions": actions,
        "apps": apps,
        "blocking_action_ids": blocking,
        "target_evidence_count": len(targeted_mutations),
        "mutation_evidence_count": len(mutations),
        "outcome_evidence_count": len(causally_evidenced_mutations),
    }


def _has_causal_post_mutation_evidence(item: dict[str, Any]) -> bool:
    if _dict(item.get("verified_post_state")).get("causal") is True:
        return True
    if any(_dict(effect).get("causal") is True for effect in item.get("effects", [])):
        return True
    if str(item.get("visual_attribution") or "") != "correlation_id":
        return False
    action_time = _time(item.get("timestamp"))
    if action_time is None:
        return False
    return any(
        (frame_time := _time(_dict(frame).get("ts") or _dict(frame).get("timestamp"))) is not None
        and frame_time >= action_time
        for frame in item.get("visual", [])
    )


def _verified_post_state(
    action: dict[str, Any],
    *,
    trace: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return an explicit, causal receipt emitted after a mutation.

    Focused value deltas are self-contained mutation receipts. Native AX
    snapshots may also prove the resulting state, but only when they belong to
    the same app and stable target and arrive strictly after the mutation in a
    short causal window. Generic nearby or pre-mutation rows remain diagnostic.
    """

    payload = _dict(action.get("payload"))
    meta = _dict(payload.get("meta"))
    provenance = _dict(meta.get("provenance"))
    input_payload = _dict(payload.get("input"))
    mutation = _dict(input_payload.get("mutation"))
    if str(action.get("kind") or "") not in _MUTATION_ACTIONS:
        return {}
    if (
        provenance.get("source") == "accessibility_observation"
        and provenance.get("inference") == "focused_value_delta"
        and mutation
    ):
        values = _unique(
            str(value)
            for value in (
                mutation.get("inserted_text"),
                input_payload.get("value"),
                input_payload.get("text"),
            )
            if str(value or "").strip()
        )
        return {
            "source": "accessibility_observation",
            "causal": True,
            "observed_at": action.get("ts"),
            "app": _dict(payload.get("app")),
            "mutation": mutation,
            "values": values,
        }

    return _explicit_ax_post_state(action, trace=trace)


def _explicit_ax_post_state(
    action: dict[str, Any],
    *,
    trace: list[dict[str, Any]],
) -> dict[str, Any]:
    action_time = _time(action.get("ts"))
    action_payload = _dict(action.get("payload"))
    action_apps = _bundle_identity_values(_dict(action_payload.get("app")))
    action_targets = _stable_target_identity_values(action_payload)
    if action_time is None or not action_apps or not action_targets:
        return {}

    action_id = int(action.get("id") or 0)
    for row in trace:
        row_time = _time(row.get("ts"))
        if row_time is None or row_time <= action_time or int(row.get("id") or 0) <= action_id:
            continue
        latency = (row_time - action_time).total_seconds()
        if latency > _AX_POST_STATE_WINDOW_SECONDS:
            continue
        if str(row.get("source") or "") != "native":
            continue
        row_payload = _dict(row.get("payload"))
        if not action_apps & _bundle_identity_values(_dict(row_payload.get("app"))):
            continue
        shared_targets = action_targets & _stable_target_identity_values(row_payload)
        if not shared_targets:
            continue
        if row.get("type") == "action" and str(row.get("kind") or "") in _MUTATION_ACTIONS:
            return {}
        ax = _dict(_dict(row_payload.get("target")).get("ax"))
        values = _unique(
            str(ax.get(key) or "").strip()
            for key in ("value_preview", "value")
            if str(ax.get(key) or "").strip()
        )
        if not values:
            continue
        return {
            "source": "accessibility_post_state_snapshot",
            "causal": True,
            "observed_at": row.get("ts"),
            "receipt_trace_id": row.get("id"),
            "latency_ms": round(latency * 1000),
            "app": _dict(row_payload.get("app")),
            "target_identity": sorted(shared_targets),
            "values": values,
        }
    return {}


def _bundle_identity_values(app: dict[str, Any]) -> set[str]:
    return {
        identity
        for identity in _app_identity_values(app)
        if identity.startswith("bundle:")
    }


def _stable_target_identity_values(payload: dict[str, Any]) -> set[str]:
    stable_prefixes = ("target:selector:", "target:id:", "target:test_id:", "target:identifier:")
    return {
        identity
        for identity in _identity_values(payload, "target")
        if identity.startswith(stable_prefixes)
    }


def _quality(
    capsules: list[dict[str, Any]],
    metadata: dict[str, Any],
    *,
    trace: list[dict[str, Any]] | None = None,
    surfaces: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    blocking: list[dict[str, Any]] = []
    by_trace_id: dict[object, dict[str, Any]] = {}

    def add_blocker(trace_id: object, warnings: list[Any], *, action_id: object | None = None) -> None:
        relevant = _unique(str(warning) for warning in warnings if str(warning) in _BLOCKING_WARNINGS)
        if not relevant:
            return
        blocker = by_trace_id.get(trace_id)
        if blocker is None:
            blocker = {"action_id": action_id if action_id is not None else trace_id, "warnings": []}
            by_trace_id[trace_id] = blocker
            blocking.append(blocker)
        blocker["warnings"] = _unique([*blocker.get("warnings", []), *relevant])

    for row in trace or []:
        add_blocker(row.get("id"), _dict(row.get("quality")).get("warnings") or [])
    for item in capsules:
        trace_id = item.get("trace_id") if item.get("trace_id") is not None else item.get("id")
        add_blocker(trace_id, item.get("warnings", []), action_id=item.get("id"))

    semantic_reasons: list[dict[str, Any]] = []
    if not capsules and not blocking:
        blocking.append({"capture": "actions", "warnings": ["no_meaningful_actions"]})
        semantic_reasons.append({"code": "no_meaningful_actions"})

    expected_origins = instruction_browser_origins(metadata)
    covered_origins = {
        origin
        for row in trace or []
        if row.get("type") == "action"
        and row.get("source") == "chrome"
        for origin in trace_row_origins(row)
    }
    covered_origin_keys = {origin_key(origin) for origin in covered_origins}
    missing_origins = sorted(
        origin for origin in expected_origins if origin_key(origin) not in covered_origin_keys
    )
    if missing_origins:
        blocking.append({
            "capture": "chrome",
            "warnings": ["missing_required_browser_origin"],
            "origins": missing_origins,
        })
        semantic_reasons.append({
            "code": "missing_required_browser_origin",
            "origins": missing_origins,
        })

    chrome_provider = metadata.get("chrome_provider") if isinstance(metadata.get("chrome_provider"), dict) else {}
    continuity_rows = [
        row for row in trace or []
        if row.get("kind") in {"chrome.provider.continuity", "chrome.provider.finalized"}
    ]
    finalization_rows = [
        row for row in continuity_rows
        if row.get("kind") == "chrome.provider.finalized"
    ]
    finalization_payload = (
        _dict(finalization_rows[-1].get("payload"))
        if finalization_rows
        else {}
    )
    host_continuity = _dict(
        _dict(finalization_payload.get("provider")).get("continuity")
    )
    if expected_origins and chrome_provider.get("available") is True and (
        host_continuity.get("ok") is not True
    ):
        blocking.append({
            "capture": "chrome",
            "warnings": ["missing_chrome_provider_finalization"],
        })
        semantic_reasons.append({"code": "missing_chrome_provider_finalization"})
    if host_continuity.get("ok") is False:
        semantic_reasons.append({
            "code": "chrome_provider_continuity_failed",
            "warnings": host_continuity.get("reasons") or [],
        })
    failed_continuity_warnings = _unique(
        warning
        for row in continuity_rows
        for warning in (_dict(row.get("quality")).get("warnings") or [])
        if str(warning) in _BLOCKING_WARNINGS
    )
    if failed_continuity_warnings:
        semantic_reasons.append({
            "code": "chrome_provider_continuity_failed",
            "warnings": failed_continuity_warnings,
        })

    chrome_finalization = metadata.get("chrome_finalization") if isinstance(metadata.get("chrome_finalization"), dict) else {}
    if chrome_finalization.get("ok") is False:
        semantic_reasons.append({
            "code": "chrome_provider_finalization_failed",
            "warning": str(chrome_finalization.get("warning") or "chrome_provider_finalization_failed"),
        })
    required_apps = instruction_required_apps(metadata, trace=trace, surfaces=surfaces)
    covered_apps = {
        app_id
        for row in trace or []
        if row.get("type") == "action"
        for app_id in _trace_row_app_ids(row)
    }
    if any(
        row.get("type") == "action"
        and row.get("source") == "chrome"
        for row in trace or []
    ):
        covered_apps.add("bundle:com.google.chrome")
    missing_apps = sorted(
        alias
        for alias, bundle_ids in required_apps.items()
        if not covered_apps.intersection(bundle_ids)
    )
    if missing_apps:
        blocking.append({
            "capture": "desktop",
            "warnings": ["missing_required_app_action"],
            "apps": missing_apps,
        })
        semantic_reasons.append({
            "code": "missing_required_app_action",
            "apps": missing_apps,
        })

    visual_health = metadata.get("visualCaptureHealth") if isinstance(metadata.get("visualCaptureHealth"), dict) else {}
    visual_error = str(visual_health.get("error") or "").strip()
    # A later filter transition can fail after all actions already have
    # correlated visual evidence. That is recorded in health, but only becomes
    # a drafting block when it leaves a meaningful action without visuals.
    if visual_error and not capsules:
        blocking.append({"capture": "active_surface", "warnings": ["active_surface_visual_unavailable"]})
    return {
        # ``blocking`` is a diagnostic index of individual actions whose replay
        # evidence is weak. Session-level structural failures are represented
        # by ``reasons``; target, mutation, and outcome sufficiency is enforced
        # separately by capture_acceptance.
        "blocking": blocking,
        "reasons": semantic_reasons,
        "capture_health": metadata.get("captureHealth") or metadata.get("captureState") or {},
        "warnings": ["active_surface_visual_error"] if visual_error else [],
    }


def instruction_browser_origins(manifest: dict[str, Any]) -> set[str]:
    """Return browser origins named by the demonstrated goal.

    Full URLs are accepted anywhere.  Bare domains are accepted as HTTPS
    origins too, so ordinary instructions such as ``go to example.com`` do not
    silently disable website-coverage validation.
    """

    text = " ".join(
        str(manifest.get(key) or "").strip()
        for key in ("instructions", "goal")
        if str(manifest.get(key) or "").strip()
    )
    origins: set[str] = set()
    occupied: list[tuple[int, int]] = []
    for match in re.finditer(r"https?://[^\s<>\"']+", text, flags=re.IGNORECASE):
        candidate = match.group(0).rstrip(".,;:!?)]}")
        origin = http_url_origin(candidate)
        if origin:
            origins.add(origin)
        occupied.append(match.span())

    file_suffixes = {
        "avif", "csv", "doc", "docx", "gif", "jpeg", "jpg", "json", "jsonl",
        "md", "mp4", "pdf", "png", "py", "swift", "ts", "tsx", "txt", "webp",
        "xls", "xlsx", "yaml", "yml",
    }
    domain_pattern = re.compile(
        r"(?<![@\w.-])(?:www\.)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
        r"[a-z]{2,63}(?::\d{1,5})?(?:/[^\s<>\"']*)?",
        flags=re.IGNORECASE,
    )
    for match in domain_pattern.finditer(text):
        if any(start <= match.start() < end for start, end in occupied):
            continue
        candidate = match.group(0).rstrip(".,;:!?)]}")
        host_part = candidate.split("/", 1)[0].split(":", 1)[0]
        suffix = host_part.rsplit(".", 1)[-1].lower()
        if suffix in file_suffixes:
            continue
        origin = http_url_origin("https://" + candidate)
        if origin:
            origins.add(origin)
    return origins


def instruction_required_apps(
    manifest: dict[str, Any],
    *,
    trace: list[dict[str, Any]] | None = None,
    surfaces: list[dict[str, Any]] | None = None,
) -> dict[str, set[str]]:
    """Resolve named app requirements from the apps actually demonstrated.

    The recorder cannot maintain a correct catalog of every macOS application,
    and common app names such as ``Mail`` are also ordinary English words.
    Instead, use bundle/name identities emitted by the native recorder and only
    treat one as required when the workflow text names that observed app. This
    works for arbitrary installed business apps without special-casing vendors.
    """

    text = " ".join(
        str(manifest.get(key) or "").strip()
        for key in ("instructions", "goal")
        if str(manifest.get(key) or "").strip()
    )
    requirements: dict[str, set[str]] = {}
    observed_apps: dict[str, dict[str, Any]] = {}
    for row in trace or []:
        for app in _trace_row_apps(row):
            key = _app_primary_identity(app)
            if key:
                observed_apps[key] = {**observed_apps.get(key, {}), **app}
    for row in surfaces or []:
        app = _dict(_dict(row.get("payload")).get("app"))
        key = _app_primary_identity(app)
        if key:
            observed_apps[key] = {**observed_apps.get(key, {}), **app}

    for app in observed_apps.values():
        aliases = _app_text_aliases(app)
        if not aliases or not any(_contains_phrase(text, alias) for alias in aliases):
            continue
        label = str(app.get("name") or app.get("bundleIdentifier") or app.get("bundle_id") or "").strip()
        identities = _app_identity_values(app)
        if label and identities:
            requirements[label] = identities
    return requirements


def trace_row_origins(row: dict[str, Any]) -> set[str]:
    payload = _dict(row.get("payload"))
    browser = _dict(payload.get("browser"))
    effect = _dict(payload.get("effect"))
    values = (
        browser.get("origin"),
        browser.get("url"),
        browser.get("top_url"),
        browser.get("frame_url"),
        effect.get("before_url"),
        effect.get("after_url"),
    )
    origins: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        origin = http_url_origin(text)
        if origin:
            origins.add(origin)
    return origins


def _trace_row_app_ids(row: dict[str, Any]) -> set[str]:
    return {
        identity
        for app in _trace_row_apps(row)
        for identity in _app_identity_values(app)
    }


def _trace_row_apps(row: dict[str, Any]) -> list[dict[str, Any]]:
    payload = _dict(row.get("payload"))
    candidates = [
        _dict(payload.get("app")),
        _dict(_dict(payload.get("surface")).get("app")),
        _dict(_dict(payload.get("context")).get("app")),
    ]
    result: list[dict[str, Any]] = []
    for app in candidates:
        if _app_primary_identity(app) and app not in result:
            result.append(app)
    return result


def _app_primary_identity(app: dict[str, Any]) -> str:
    bundle_id = str(app.get("bundleIdentifier") or app.get("bundle_id") or "").strip()
    if bundle_id:
        return "bundle:" + bundle_id.lower()
    name = _normalized_words(app.get("name"))
    return "name:" + name if name else ""


def _app_text_aliases(app: dict[str, Any]) -> set[str]:
    aliases: set[str] = set()
    name = _normalized_words(app.get("name"))
    if name:
        aliases.add(name)
        words = name.split()
        if len(words) > 1 and len(words[-1]) >= 3:
            aliases.add(words[-1])
    bundle_id = str(app.get("bundleIdentifier") or app.get("bundle_id") or "").strip()
    bundle_leaf = _normalized_words(bundle_id.rsplit(".", 1)[-1])
    if len(bundle_leaf) >= 3:
        aliases.add(bundle_leaf)
    return aliases


def _normalized_words(value: Any) -> str:
    return " ".join(re.findall(r"[\w]+", str(value or "").casefold(), flags=re.UNICODE))


def _contains_phrase(text: str, phrase: str) -> bool:
    if not phrase:
        return False
    return bool(re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", _normalized_words(text)))


def origin_key(origin: str) -> str:
    parsed = urllib.parse.urlparse(str(origin or ""))
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = f":{parsed.port}" if parsed.port else ""
    return host + port


def _append_reason(reasons: list[dict[str, Any]], reason: dict[str, Any]) -> None:
    if reason not in reasons:
        reasons.append(reason)


def _effect_rows(
    trace: list[dict[str, Any]],
    action: dict[str, Any],
    *,
    surfaces: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    action_id = int(action.get("id") or 0)
    action_time = _time(action.get("ts"))
    action_correlation = _correlation(action)
    result: list[dict[str, Any]] = []
    nearby: list[dict[str, Any]] = []
    for row in trace:
        row_id = int(row.get("id") or 0)
        if row_id <= action_id or row_id > action_id + 12:
            continue
        if row.get("type") not in {"context", "evidence"}:
            continue
        row_time = _time(row.get("ts"))
        if action_time is not None and row_time is not None and (row_time - action_time).total_seconds() > 2:
            continue
        payload = _dict(row.get("payload"))
        entry = {
            "trace_id": row_id,
            "kind": row.get("kind"),
            "timestamp": row.get("ts"),
            "app": payload.get("app") or {},
            "window": payload.get("window") or {},
            "effect": payload.get("effect") or {},
        }
        row_meta = _dict(payload.get("meta"))
        after_action_id = str(row_meta.get("after_action_id") or "").strip()
        if action_correlation and (
            _correlation(row) == action_correlation
            or after_action_id == action_correlation
        ):
            attribution = "after_action_id" if after_action_id == action_correlation else "correlation_id"
            result.append({**entry, "attribution": attribution, "causal": True})
        elif _row_matches_action_identity(row, action, surfaces=surfaces):
            nearby.append({**entry, "attribution": "nearby_identity", "causal": False})
    return result, nearby


def _surface_for_action(surfaces: list[dict[str, Any]], action: dict[str, Any]) -> dict[str, Any]:
    before = _nearest_before(surfaces, action)
    if before:
        return _dict(before.get("payload")) or before
    payload = _dict(action.get("payload"))
    return {"app": payload.get("app") or {}, "window": payload.get("window") or {}}


def _visual_for_action(
    frames: list[dict[str, Any]],
    action: dict[str, Any],
    action_id: str,
    *,
    surfaces: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    result = [row for row in frames if _correlation(row) == action_id]
    if result:
        return result, [], "correlation_id"
    timestamp = _time(action.get("ts"))
    if timestamp is None:
        return [], [], ""
    nearby = [
        row
        for row in frames
        if (value := _time(row.get("ts") or row.get("timestamp"))) is not None
        and abs((value - timestamp).total_seconds()) <= 1
    ][:3]
    attributed = [
        row for row in nearby
        if _row_matches_action_identity(row, action, surfaces=surfaces)
    ]
    if attributed:
        return attributed, [row for row in nearby if row not in attributed], "surface_or_segment_identity"
    return [], nearby, ""


def _nearest_before(rows: list[dict[str, Any]], action: dict[str, Any]) -> dict[str, Any]:
    timestamp = _time(action.get("ts"))
    if timestamp is None:
        return {}
    candidates = [row for row in rows if (value := _time(row.get("ts") or row.get("timestamp"))) is not None and value <= timestamp]
    return dict(candidates[-1]) if candidates else {}


def _nearest_after(rows: list[dict[str, Any]], action: dict[str, Any]) -> dict[str, Any]:
    timestamp = _time(action.get("ts"))
    if timestamp is None:
        return {}
    candidates = [row for row in rows if (value := _time(row.get("ts") or row.get("timestamp"))) is not None and value >= timestamp]
    return dict(candidates[0]) if candidates else {}


def _nearby_rows(rows: list[dict[str, Any]], action: dict[str, Any], *, seconds: float) -> list[dict[str, Any]]:
    timestamp = _time(action.get("ts"))
    if timestamp is None:
        return []
    return [
        row
        for row in rows
        if (value := _time(row.get("ts") or row.get("timestamp"))) is not None
        and abs((value - timestamp).total_seconds()) <= seconds
    ]


def _row_matches_action_identity(
    row: dict[str, Any],
    action: dict[str, Any],
    *,
    surfaces: list[dict[str, Any]],
) -> bool:
    """Require a stable identity before promoting nearby data to evidence."""

    if _correlation(row) and _correlation(row) == _correlation(action):
        return True
    row_payload = _dict(row.get("payload"))
    action_payload = _dict(action.get("payload"))

    if _identity_values(row_payload, "segment") & _identity_values(action_payload, "segment"):
        return True
    if _identity_values(row_payload, "target") & _identity_values(action_payload, "target"):
        return True
    row_app_ids = _identity_values(row_payload, "app")
    action_app_ids = _identity_values(action_payload, "app")
    if row_app_ids & action_app_ids:
        return True

    # Frame and low-level CGEvent rows often carry no app themselves. Attribute
    # them through the active-surface timeline at each timestamp, then require
    # an app or window identity match with the action's active surface.
    row_direct_surface_ids = _surface_identity_values(row_payload)
    action_direct_surface_ids = _surface_identity_values(action_payload)
    row_surface_ids = row_direct_surface_ids or _surface_identity_values(_surface_at(surfaces, row))
    action_surface_ids = action_direct_surface_ids or _surface_identity_values(_surface_for_action(surfaces, action))
    return bool(row_surface_ids and action_surface_ids and row_surface_ids & action_surface_ids)


def _surface_at(surfaces: list[dict[str, Any]], item: dict[str, Any]) -> dict[str, Any]:
    timestamp = _time(item.get("ts") or item.get("timestamp"))
    if timestamp is None:
        return {}
    candidates = [
        row
        for row in surfaces
        if (value := _time(row.get("ts") or row.get("timestamp"))) is not None
        and value <= timestamp
    ]
    if not candidates:
        return {}
    selected = max(
        candidates,
        key=lambda value: _time(value.get("ts") or value.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc),
    )
    return _dict(selected.get("payload")) or selected


def _surface_identity_values(surface: dict[str, Any]) -> set[str]:
    app = _dict(surface.get("app"))
    window = _dict(surface.get("window"))
    result = _app_identity_values(app)
    for key in ("windowID", "window_id", "id"):
        value = str(window.get(key) or "").strip()
        if value:
            result.add(f"window:{value}")
    return result


def _identity_values(payload: dict[str, Any], kind: str) -> set[str]:
    if kind == "app":
        return _app_identity_values(_dict(payload.get("app")))
    if kind == "target":
        target = _dict(payload.get("target"))
        ax = _dict(target.get("ax"))
        result: set[str] = set()
        for key in ("selector", "id", "test_id", "identifier"):
            value = str(target.get(key) or ax.get(key) or "").strip()
            if value:
                result.add(f"target:{key}:{value}")
        role = str(target.get("role") or ax.get("role") or "").strip()
        label = str(target.get("label") or target.get("name") or ax.get("title") or "").strip()
        if role and label:
            result.add(f"target:role-label:{role}:{label}")
        return result
    result: set[str] = set()
    browser = _dict(payload.get("browser"))
    provider = _dict(payload.get("provider"))
    meta = _dict(payload.get("meta"))
    for value in (
        meta.get("segment_id"),
        meta.get("segmentId"),
        provider.get("segment_id"),
        provider.get("segmentId"),
        browser.get("active_target_id"),
        browser.get("activeTargetId"),
    ):
        text = str(value or "").strip()
        if text:
            result.add(f"segment:{text}")
    return result


def _app_identity_values(app: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for key, prefix in (
        ("bundle_id", "bundle"),
        ("bundleIdentifier", "bundle"),
        ("processIdentifier", "pid"),
        ("pid", "pid"),
    ):
        value = str(app.get(key) or "").strip().casefold()
        if value:
            result.add(f"{prefix}:{value}")
    name = _normalized_words(app.get("name"))
    if name:
        result.add(f"name:{name}")
    return result


def _raw_range(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {}
    seq = [int(row.get("seq") or row.get("id") or 0) for row in rows]
    return {"count": len(rows), "first_seq": min(seq), "last_seq": max(seq)}


def _artifact_index(root: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in _capture_artifact_files(root):
        entry = {
            "path": path.relative_to(root).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
        if path.parent == root / "visual" and path.suffix.lower() == ".mp4":
            entry["kind"] = "video"
        entries.append(entry)
    return entries


def _sealed_files(root: Path) -> list[Path]:
    paths = [root / relative for relative in _IMMUTABLE_CAPTURE_PATHS]
    return [path for path in paths if path.is_file()] + _capture_artifact_files(root)


def _capture_artifact_files(root: Path) -> list[Path]:
    paths: list[Path] = []
    if (root / "artifacts").is_dir():
        paths.extend((root / "artifacts").rglob("*"))
    if (root / "visual").is_dir():
        paths.extend((root / "visual").glob("*.mp4"))
    return sorted(path for path in paths if path.is_file())


def _legacy_sealed_files(root: Path) -> list[Path]:
    immutable = _sealed_files(root)
    lifecycle = [root / relative for relative in ("metadata.json", "session.json")]
    return [path for path in lifecycle if path.is_file()] + immutable


def _validated_visual_media_reference(root: Path, reference: str) -> str | None:
    relative = Path(reference)
    if relative.is_absolute() or ".." in relative.parts or relative.suffix.lower() != ".mp4":
        return None
    if not relative.parts or relative.parts[0] != "visual":
        return None
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return relative.as_posix()


def _nonempty_file(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_optional_json(path: Path) -> dict[str, Any]:
    try:
        return read_json(path)
    except Exception:
        return {}


def _correlation(row: dict[str, Any]) -> str:
    return str(row.get("correlation_id") or _dict(row.get("payload")).get("meta", {}).get("correlation_id") or "")


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _unique(values: Any) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
