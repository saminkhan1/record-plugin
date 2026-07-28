from __future__ import annotations

import getpass
import hashlib
import json
from pathlib import Path
from typing import Any

from recording_bundle import write_json
from .trace_schema import utc_now


class ApprovalError(ValueError):
    pass


ARTIFACT_MANIFEST_FILENAME = "artifacts.json"
COMPILE_SUMMARY_FILENAME = "compile-summary.json"


def payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def write_artifact_manifest(build_dir: Path, artifacts: list[dict[str, Any]]) -> Path:
    """Record the staged runtime files that must be promoted after approval."""

    build_dir = build_dir.expanduser().resolve()
    normalized = [_normalized_artifact(build_dir, artifact) for artifact in artifacts]
    normalized.sort(key=lambda item: (item["destination"], item["source"]))
    path = build_dir / ARTIFACT_MANIFEST_FILENAME
    write_json(path, {"version": 1, "artifacts": normalized})
    return path


def artifact_bundle_hash(build_dir: Path) -> str:
    """Verify staged artifacts and return the approval-bound bundle digest."""

    manifest = _read_artifact_manifest(build_dir)
    verified = _verified_artifacts(build_dir, manifest)
    encoded = json.dumps(verified, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_compiled_artifacts(build_dir: Path) -> list[dict[str, str]]:
    """Return verified staged artifact declarations."""

    manifest = _read_artifact_manifest(build_dir)
    return _verified_artifacts(build_dir, manifest)


def artifact_digest(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_file():
        digest.update(b"file\0")
        _update_digest_from_file(digest, path)
        return digest.hexdigest()
    if not path.is_dir():
        raise ApprovalError(f"Compiled artifact is missing: {path}")
    digest.update(b"directory\0")
    for child in sorted(path.rglob("*"), key=lambda item: item.relative_to(path).as_posix()):
        relative = child.relative_to(path).as_posix().encode("utf-8")
        if child.is_dir():
            digest.update(b"dir\0" + relative + b"\0")
        elif child.is_file():
            digest.update(b"file\0" + relative + b"\0")
            _update_digest_from_file(digest, child)
        else:
            raise ApprovalError(f"Unsupported compiled artifact entry: {child}")
    return digest.hexdigest()


def load_payload(build_dir: Path) -> dict[str, Any]:
    return _read_json_object(build_dir / "cronjob-payload.json", "compiled payload")


def load_compile_summary(build_dir: Path) -> dict[str, Any]:
    return _read_json_object(build_dir / COMPILE_SUMMARY_FILENAME, "compile summary")


def verify_approval_envelope(
    build_dir: Path,
    *,
    expected_sha256: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Prove that the reviewed envelope describes this exact compiled build."""

    build_dir = build_dir.expanduser().resolve()
    path = build_dir / "approval-envelope.json"
    actual_sha256 = _approval_envelope_hash(build_dir)
    if expected_sha256 is not None and expected_sha256 != actual_sha256:
        raise ApprovalError("Approval envelope changed before approval")
    envelope = _read_json_object(path, "approval envelope")
    if envelope.get("version") != 1:
        raise ApprovalError("Unsupported approval envelope")

    payload = load_payload(build_dir)
    payload_sha256 = payload_hash(payload)
    summary = _verified_compile_summary(
        build_dir,
        payload_sha256=payload_sha256,
    )
    artifacts = load_compiled_artifacts(build_dir)
    artifacts_sha256 = artifact_bundle_hash(build_dir)
    script_source, script_sha256 = _compiled_script_review(build_dir, payload)
    authorization = (
        payload.get("authorization")
        if isinstance(payload.get("authorization"), dict)
        else {}
    )
    if (
        envelope.get("payload") != payload
        or envelope.get("payload_sha256") != payload_sha256
        or envelope.get("provenance") != summary.get("provenance")
        or envelope.get("artifacts") != artifacts
        or envelope.get("artifacts_sha256") != artifacts_sha256
    ):
        raise ApprovalError(
            "Approval envelope does not match the compiled payload and artifacts"
        )
    if (
        envelope.get("script_source") != script_source
        or envelope.get("script_sha256") != script_sha256
        or envelope.get("execution_sha256")
        != authorization.get("execution_sha256")
        or (
            authorization.get("mode") == "human_in_loop"
            and script_sha256 is not None
            and authorization.get("script_sha256") != script_sha256
        )
    ):
        raise ApprovalError(
            "Approval envelope does not contain the exact compiled script "
            "and reviewed execution digests"
        )

    mirrored_payload_fields = {
        "prompt": payload.get("prompt"),
        "script_destination": payload.get("script"),
        "skills": payload.get("skills") or [],
        "enabled_toolsets": payload.get("enabled_toolsets") or [],
        "schedule": payload.get("schedule"),
        "delivery_route": payload.get("deliver"),
        "authorization": payload.get("authorization"),
    }
    if any(
        envelope.get(field) != value
        for field, value in mirrored_payload_fields.items()
    ):
        raise ApprovalError(
            "Approval envelope does not describe the compiled payload"
        )
    return envelope, actual_sha256


def _verified_compile_summary(
    build_dir: Path, *, payload_sha256: str
) -> dict[str, Any]:
    summary = load_compile_summary(build_dir)
    summary_payload = summary.get("payload")
    if (
        summary.get("payload_sha256") != payload_sha256
        or not isinstance(summary_payload, dict)
        or payload_hash(summary_payload) != payload_sha256
    ):
        raise ApprovalError("Compile summary does not match the compiled payload")
    return summary


def approve_build(
    build_dir: Path,
    *,
    approved_by: str | None = None,
    note: str | None = None,
    approval_envelope_sha256: str | None = None,
) -> dict[str, Any]:
    build_dir = build_dir.expanduser().resolve()
    payload = load_payload(build_dir)
    payload_sha256 = payload_hash(payload)
    summary = _verified_compile_summary(build_dir, payload_sha256=payload_sha256)
    verified_envelope_sha256 = None
    if approval_envelope_sha256 is not None:
        _, verified_envelope_sha256 = verify_approval_envelope(
            build_dir,
            expected_sha256=approval_envelope_sha256,
        )
    approval = {
        "approved": True,
        "approved_at": utc_now(),
        "approved_by": approved_by or getpass.getuser(),
        "payload_sha256": payload_sha256,
        "compile_summary_sha256": payload_hash(summary),
    }
    if verified_envelope_sha256 is not None:
        approval["approval_envelope_sha256"] = verified_envelope_sha256
    approval["artifacts_sha256"] = artifact_bundle_hash(build_dir)
    if note:
        approval["note"] = note
    path = build_dir / "approval.json"
    write_json(path, approval)
    return approval


def load_approved_payload(build_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    build_dir = build_dir.expanduser().resolve()
    payload = load_payload(build_dir)
    approval_path = build_dir / "approval.json"
    approval = _read_json_object(approval_path, "approval file")
    if approval.get("approved") is not True:
        raise ApprovalError("Approval file does not mark the build approved")
    expected = approval.get("payload_sha256")
    actual = payload_hash(payload)
    if expected != actual:
        raise ApprovalError("Compiled payload changed after approval")
    summary = _verified_compile_summary(build_dir, payload_sha256=actual)
    expected_summary = approval.get("compile_summary_sha256")
    if not isinstance(expected_summary, str) or not expected_summary:
        raise ApprovalError("Compile summary is not covered by approval")
    if expected_summary != payload_hash(summary):
        raise ApprovalError("Compile summary changed after approval")
    expected_artifacts = approval.get("artifacts_sha256")
    actual_artifacts = artifact_bundle_hash(build_dir)
    if not isinstance(expected_artifacts, str) or not expected_artifacts:
        raise ApprovalError("Compiled artifacts are not covered by approval")
    if expected_artifacts != actual_artifacts:
        raise ApprovalError("Compiled artifacts changed after approval")
    expected_envelope = approval.get("approval_envelope_sha256")
    if expected_envelope is not None:
        _, actual_envelope = verify_approval_envelope(build_dir)
        if expected_envelope != actual_envelope:
            raise ApprovalError("Approval envelope changed after approval")
    return payload, approval


def _read_artifact_manifest(build_dir: Path) -> list[dict[str, Any]]:
    path = build_dir.expanduser().resolve() / ARTIFACT_MANIFEST_FILENAME
    value = _read_json_object(path, "artifact manifest")
    if value.get("version") != 1:
        raise ApprovalError("Unsupported compiled artifact manifest")
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, list) or not all(isinstance(item, dict) for item in artifacts):
        raise ApprovalError("Compiled artifact manifest must contain an artifacts list")
    return [dict(item) for item in artifacts]


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise ApprovalError(f"Missing {label}: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ApprovalError(f"Invalid {label} JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ApprovalError(f"{label.capitalize()} must be a JSON object")
    return value


def _normalized_artifact(
    build_dir: Path,
    artifact: dict[str, Any],
) -> dict[str, str]:
    source = _relative_artifact_path(artifact.get("source"), "artifact.source")
    destination = _relative_artifact_path(
        artifact.get("destination"),
        "artifact.destination",
    )
    kind = str(artifact.get("kind") or "").strip()
    source_path = _artifact_source_path(build_dir, source)
    if kind == "file" and not source_path.is_file():
        raise ApprovalError(f"Compiled artifact is missing: {source}")
    if kind == "directory" and not source_path.is_dir():
        raise ApprovalError(f"Compiled artifact directory is missing: {source}")
    if kind not in {"file", "directory"}:
        raise ApprovalError("artifact.kind must be file or directory")
    return {
        "source": source.as_posix(),
        "destination": destination.as_posix(),
        "kind": kind,
        "sha256": artifact_digest(source_path),
    }


def _verified_artifacts(
    build_dir: Path,
    manifest: list[dict[str, Any]],
) -> list[dict[str, str]]:
    verified = [_normalized_artifact(build_dir, artifact) for artifact in manifest]
    for artifact, normalized in zip(manifest, verified, strict=True):
        if str(artifact.get("sha256") or "").strip() != normalized["sha256"]:
            raise ApprovalError(
                "Compiled artifact changed after compilation: "
                + normalized["source"]
            )
    return verified


def _approval_envelope_hash(build_dir: Path) -> str:
    path = build_dir / "approval-envelope.json"
    if not path.is_file():
        raise ApprovalError("Missing approval envelope")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _compiled_script_review(
    build_dir: Path,
    payload: dict[str, Any],
) -> tuple[str | None, str | None]:
    raw_script = payload.get("script")
    if raw_script in (None, ""):
        return None, None
    relative = _relative_artifact_path(raw_script, "payload.script")
    root = (build_dir.expanduser().resolve() / "staged-runtime" / "scripts").resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ApprovalError("Compiled script escapes the staged scripts directory") from exc
    if not path.is_file():
        raise ApprovalError("Compiled script is missing from the staged runtime")
    data = path.read_bytes()
    try:
        source = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ApprovalError("Compiled script source is not UTF-8") from exc
    return source, hashlib.sha256(data).hexdigest()


def _relative_artifact_path(value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ApprovalError(f"{field} must be a relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ApprovalError(f"{field} must be a relative path")
    return path


def _artifact_source_path(build_dir: Path, relative: Path) -> Path:
    root = build_dir.expanduser().resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ApprovalError("Compiled artifact source escapes the build directory") from exc
    return path


def _update_digest_from_file(digest: Any, path: Path) -> None:
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
