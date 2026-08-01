"""Shared workflow trace recording bundle helpers."""

from __future__ import annotations

import json
import os
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List

from fastapi import HTTPException


REQUIRED_RECORDING_FILES = ("metadata.json", "session.json", "trace.jsonl")
OPTIONAL_RECOVERY_JSON_FILES = ("workflow.json", "review-conversation.json")


def record_root(home: Path) -> Path:
    root = home / "record"
    root.mkdir(parents=True, exist_ok=True)
    return root


def builds_root(home: Path) -> Path:
    root = record_root(home) / "builds"
    root.mkdir(parents=True, exist_ok=True)
    return root


def recordings_root(home: Path) -> Path:
    root = record_root(home) / "recordings"
    root.mkdir(parents=True, exist_ok=True)
    return root


def is_trace_recording_dir(path: Path) -> bool:
    return all((path / name).is_file() for name in REQUIRED_RECORDING_FILES)


def recording_key(home: Path, recording_dir: Path) -> str:
    root = recordings_root(home).resolve()
    try:
        return recording_dir.resolve().relative_to(root).as_posix()
    except ValueError:
        return recording_dir.name


def safe_recording_dir(home: Path, raw: str, *, require_bundle: bool = True) -> Path:
    root = recordings_root(home).resolve()
    if not raw.strip():
        raise HTTPException(status_code=400, detail="recording_dir is required")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="recording_dir must be under the profile Record recordings directory") from exc
    if not path.is_dir():
        raise HTTPException(status_code=404, detail="Hermes workflow trace recording directory not found")
    if require_bundle and not is_trace_recording_dir(path):
        raise HTTPException(status_code=404, detail="Hermes workflow trace recording bundle not found")
    return path


def resolve_recording_ref(home: Path, raw: str, *, require_bundle: bool = True) -> Path:
    value = urllib.parse.unquote((raw or "").strip())
    if not value:
        raise HTTPException(status_code=400, detail="recording_key is required")
    if "/" in value or "\\" in value or Path(value).is_absolute():
        return safe_recording_dir(home, value, require_bundle=require_bundle)
    root = recordings_root(home).resolve()
    matches = [
        path
        for path in root.glob(f"*/{value}")
        if path.is_dir() and (not require_bundle or is_trace_recording_dir(path))
    ]
    if len(matches) == 1:
        return matches[0].resolve()
    if len(matches) > 1:
        raise HTTPException(status_code=409, detail="recording_key is ambiguous; use the full recording path")
    return safe_recording_dir(home, value, require_bundle=require_bundle)


def recording_bundle_diagnostic(home: Path, recording_dir: Path) -> Dict[str, Any]:
    """Inspect one bundle without allowing corruption to abort enumeration."""

    root = recordings_root(home).resolve()
    path = recording_dir.expanduser().resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return {
            "recording_key": path.name,
            "healthy": False,
            "errors": ["path_outside_recordings_root"],
        }

    errors: List[str] = []
    lifecycle: Dict[str, Any] = {}
    recovery_files = [
        *REQUIRED_RECORDING_FILES,
        *(
            name
            for name in OPTIONAL_RECOVERY_JSON_FILES
            if (path / name).is_file()
        ),
    ]
    for name in recovery_files:
        target = path / name
        if not target.is_file():
            errors.append(f"missing:{name}")
            continue
        if name.endswith(".json"):
            try:
                value = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                errors.append(f"invalid:{name}:{type(exc).__name__}")
                continue
            if not isinstance(value, dict):
                errors.append(f"invalid:{name}:not_object")
                continue
            if name == "metadata.json":
                lifecycle["name"] = str(
                    value.get("name") or value.get("workflow_id") or path.parent.name
                )
                lifecycle["profile"] = str(value.get("profile") or "")
                lifecycle["state"] = str(value.get("state") or value.get("status") or "")
            elif not lifecycle.get("state"):
                lifecycle["state"] = str(value.get("status") or value.get("state") or "")
        elif name.endswith(".jsonl"):
            try:
                handle = target.open("r", encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                errors.append(f"invalid:{name}:{type(exc).__name__}")
                continue
            try:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        errors.append(f"invalid:{name}:line_{line_number}")
                        break
                    if not isinstance(value, dict):
                        errors.append(
                            f"invalid:{name}:line_{line_number}_not_object"
                        )
                        break
            except UnicodeError as exc:
                errors.append(f"invalid:{name}:{type(exc).__name__}")
            finally:
                handle.close()
    return {
        "recording_key": recording_key(home, path),
        "recording_dir": str(path),
        "healthy": not errors,
        "errors": errors,
        **lifecycle,
    }


def read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Missing required workflow trace recording file: {path.name}") from exc
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON in workflow trace recording file: {path.name}") from exc
    except (UnicodeError, OSError) as exc:
        raise HTTPException(status_code=400, detail=f"Unreadable workflow trace recording file: {path.name}") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail=f"Workflow trace recording file must be a JSON object: {path.name}")
    return value


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.is_file():
        return rows
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
    return rows


def write_json(path: Path, value: Dict[str, Any]) -> None:
    """Durably replace lifecycle JSON without exposing partial content."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, sort_keys=True, default=str) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
