from __future__ import annotations

import getpass
import hashlib
import json
from pathlib import Path
from typing import Any

from ..recording_bundle import write_json
from .trace_schema import utc_now


class ReviewReceiptError(ValueError):
    pass


ARTIFACT_MANIFEST_FILENAME = "artifacts.json"
COMPILE_SUMMARY_FILENAME = "compile-summary.json"
REVIEW_ENVELOPE_FILENAME = "review-envelope.json"
REVIEW_RECEIPT_FILENAME = "review-receipt.json"


def payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def write_artifact_manifest(build_dir: Path, artifacts: list[dict[str, Any]]) -> Path:
    """Record the compiled files covered by the operator's review receipt."""

    build_dir = build_dir.expanduser().resolve()
    normalized = [_normalized_artifact(build_dir, artifact) for artifact in artifacts]
    normalized.sort(key=lambda item: (item["destination"], item["source"]))
    path = build_dir / ARTIFACT_MANIFEST_FILENAME
    write_json(path, {"version": 1, "artifacts": normalized})
    return path


def artifact_bundle_hash(build_dir: Path) -> str:
    """Verify compiled artifacts and return the review-bound bundle digest."""

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
        raise ReviewReceiptError(f"Compiled artifact is missing: {path}")
    digest.update(b"directory\0")
    for child in sorted(path.rglob("*"), key=lambda item: item.relative_to(path).as_posix()):
        relative = child.relative_to(path).as_posix().encode("utf-8")
        if child.is_dir():
            digest.update(b"dir\0" + relative + b"\0")
        elif child.is_file():
            digest.update(b"file\0" + relative + b"\0")
            _update_digest_from_file(digest, child)
        else:
            raise ReviewReceiptError(f"Unsupported compiled artifact entry: {child}")
    return digest.hexdigest()


def load_payload(build_dir: Path) -> dict[str, Any]:
    return _read_json_object(build_dir / "cronjob-payload.json", "compiled payload")


def load_compile_summary(build_dir: Path) -> dict[str, Any]:
    return _read_json_object(build_dir / COMPILE_SUMMARY_FILENAME, "compile summary")


def verify_review_envelope(
    build_dir: Path,
    *,
    expected_sha256: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Prove that the reviewed envelope describes this exact compiled build."""

    build_dir = build_dir.expanduser().resolve()
    path = build_dir / REVIEW_ENVELOPE_FILENAME
    actual_sha256 = _review_envelope_hash(build_dir)
    if expected_sha256 is not None and expected_sha256 != actual_sha256:
        raise ReviewReceiptError("Review envelope changed before publication")
    envelope = _read_json_object(path, "review envelope")
    if envelope.get("version") != 1:
        raise ReviewReceiptError("Unsupported review envelope")

    payload = load_payload(build_dir)
    payload_sha256 = payload_hash(payload)
    summary = _verified_compile_summary(
        build_dir,
        payload_sha256=payload_sha256,
    )
    artifacts = load_compiled_artifacts(build_dir)
    artifacts_sha256 = artifact_bundle_hash(build_dir)
    script_source, script_sha256 = _compiled_script_review(build_dir, payload)
    if (
        envelope.get("payload") != payload
        or envelope.get("payload_sha256") != payload_sha256
        or envelope.get("provenance") != summary.get("provenance")
        or envelope.get("artifacts") != artifacts
        or envelope.get("artifacts_sha256") != artifacts_sha256
    ):
        raise ReviewReceiptError(
            "Review envelope does not match the compiled payload and artifacts"
        )
    if (
        envelope.get("script_source") != script_source
        or envelope.get("script_sha256") != script_sha256
    ):
        raise ReviewReceiptError(
            "Review envelope does not contain the exact compiled script"
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
        raise ReviewReceiptError("Compile summary does not match the compiled payload")
    return summary


def record_review_receipt(
    build_dir: Path,
    *,
    reviewed_by: str | None = None,
    note: str | None = None,
    review_envelope_sha256: str,
) -> dict[str, Any]:
    build_dir = build_dir.expanduser().resolve()
    payload = load_payload(build_dir)
    payload_sha256 = payload_hash(payload)
    summary = _verified_compile_summary(build_dir, payload_sha256=payload_sha256)
    _, verified_envelope_sha256 = verify_review_envelope(
        build_dir,
        expected_sha256=review_envelope_sha256,
    )
    receipt = {
        "reviewed": True,
        "reviewed_at": utc_now(),
        "reviewed_by": reviewed_by or getpass.getuser(),
        "payload_sha256": payload_sha256,
        "compile_summary_sha256": payload_hash(summary),
        "review_envelope_sha256": verified_envelope_sha256,
        "artifacts_sha256": artifact_bundle_hash(build_dir),
    }
    if note:
        receipt["note"] = note
    path = build_dir / REVIEW_RECEIPT_FILENAME
    write_json(path, receipt)
    return receipt


def load_reviewed_payload(build_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    build_dir = build_dir.expanduser().resolve()
    payload = load_payload(build_dir)
    receipt_path = build_dir / REVIEW_RECEIPT_FILENAME
    receipt = _read_json_object(receipt_path, "review receipt")
    if receipt.get("reviewed") is not True:
        raise ReviewReceiptError("Review receipt does not mark the build reviewed")
    expected = receipt.get("payload_sha256")
    actual = payload_hash(payload)
    if expected != actual:
        raise ReviewReceiptError("Compiled payload changed after review")
    summary = _verified_compile_summary(build_dir, payload_sha256=actual)
    expected_summary = receipt.get("compile_summary_sha256")
    if not isinstance(expected_summary, str) or not expected_summary:
        raise ReviewReceiptError("Compile summary is not covered by review")
    if expected_summary != payload_hash(summary):
        raise ReviewReceiptError("Compile summary changed after review")
    expected_artifacts = receipt.get("artifacts_sha256")
    actual_artifacts = artifact_bundle_hash(build_dir)
    if not isinstance(expected_artifacts, str) or not expected_artifacts:
        raise ReviewReceiptError("Compiled artifacts are not covered by review")
    if expected_artifacts != actual_artifacts:
        raise ReviewReceiptError("Compiled artifacts changed after review")
    expected_envelope = receipt.get("review_envelope_sha256")
    if not isinstance(expected_envelope, str) or not expected_envelope:
        raise ReviewReceiptError("Review envelope is not covered by the receipt")
    _, actual_envelope = verify_review_envelope(build_dir)
    if expected_envelope != actual_envelope:
        raise ReviewReceiptError("Review envelope changed after review")
    return payload, receipt


def _read_artifact_manifest(build_dir: Path) -> list[dict[str, Any]]:
    path = build_dir.expanduser().resolve() / ARTIFACT_MANIFEST_FILENAME
    value = _read_json_object(path, "artifact manifest")
    if value.get("version") != 1:
        raise ReviewReceiptError("Unsupported compiled artifact manifest")
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, list) or not all(isinstance(item, dict) for item in artifacts):
        raise ReviewReceiptError("Compiled artifact manifest must contain an artifacts list")
    return [dict(item) for item in artifacts]


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise ReviewReceiptError(f"Missing {label}: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ReviewReceiptError(f"Invalid {label} JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReviewReceiptError(f"{label.capitalize()} must be a JSON object")
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
        raise ReviewReceiptError(f"Compiled artifact is missing: {source}")
    if kind == "directory" and not source_path.is_dir():
        raise ReviewReceiptError(f"Compiled artifact directory is missing: {source}")
    if kind not in {"file", "directory"}:
        raise ReviewReceiptError("artifact.kind must be file or directory")
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
            raise ReviewReceiptError(
                "Compiled artifact changed after compilation: "
                + normalized["source"]
            )
    return verified


def _review_envelope_hash(build_dir: Path) -> str:
    path = build_dir / REVIEW_ENVELOPE_FILENAME
    if not path.is_file():
        raise ReviewReceiptError("Missing review envelope")
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
        raise ReviewReceiptError("Compiled script escapes the staged scripts directory") from exc
    if not path.is_file():
        raise ReviewReceiptError("Compiled script is missing from the staged runtime")
    data = path.read_bytes()
    try:
        source = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReviewReceiptError("Compiled script source is not UTF-8") from exc
    return source, hashlib.sha256(data).hexdigest()


def _relative_artifact_path(value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ReviewReceiptError(f"{field} must be a relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ReviewReceiptError(f"{field} must be a relative path")
    return path


def _artifact_source_path(build_dir: Path, relative: Path) -> Path:
    root = build_dir.expanduser().resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ReviewReceiptError("Compiled artifact source escapes the build directory") from exc
    return path


def _update_digest_from_file(digest: Any, path: Path) -> None:
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
