"""Read-only tools exposed only to the isolated Crunch drafting agent."""

from __future__ import annotations

import contextvars
import json
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


TOOLSET = "hermes-crunch-drafting"
_MAX_TEXT_ARTIFACT_BYTES = 128 * 1024
_TEXT_SUFFIXES = frozenset({".json", ".jsonl", ".md", ".txt", ".yaml", ".yml"})
_EVIDENCE_ROOT: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "hermes_crunch_drafting_evidence_root",
    default=None,
)


@contextmanager
def drafting_evidence_scope(root: Path) -> Iterator[None]:
    token = _EVIDENCE_ROOT.set(root.expanduser().resolve())
    try:
        yield
    finally:
        _EVIDENCE_ROOT.reset(token)


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False)


def _read_evidence(relative_path: str) -> str:
    root = _EVIDENCE_ROOT.get()
    if root is None:
        return _json({"success": False, "error": "No recording evidence is active"})
    requested = Path(str(relative_path or ""))
    if requested.is_absolute() or not requested.parts:
        return _json(
            {
                "success": False,
                "error": "Use a relative path from the recording bundle index",
            }
        )
    try:
        resolved = (root / requested).resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return _json({"success": False, "error": "Evidence path is outside the recording"})
    if (
        not resolved.is_file()
        or resolved.suffix.lower() not in _TEXT_SUFFIXES
    ):
        return _json(
            {
                "success": False,
                "error": "Evidence must be an indexed text artifact",
            }
        )
    try:
        size = resolved.stat().st_size
        if size > _MAX_TEXT_ARTIFACT_BYTES:
            return _json(
                {
                    "success": False,
                    "error": (
                        "Evidence artifact exceeds the 128 KiB drafting limit; "
                        "use the compact evidence summary"
                    ),
                }
            )
        content = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return _json({"success": False, "error": f"Could not read evidence: {exc}"})
    return _json(
        {
            "success": True,
            "path": requested.as_posix(),
            "bytes": size,
            "content": content,
        }
    )


def _list_skills(category: str | None = None, *, task_id: str | None = None) -> str:
    from tools.skills_tool import skills_list

    return skills_list(category=category or None, task_id=task_id)


def _view_skill(
    name: str,
    file_path: str | None = None,
    *,
    task_id: str | None = None,
) -> str:
    from tools.skills_tool import skill_view

    return skill_view(
        name=str(name or ""),
        file_path=str(file_path) if file_path else None,
        task_id=task_id,
    )


def _check_requirements(
    commands: Any,
    apps: Any,
) -> str:
    command_names = [
        str(item).strip()
        for item in commands
        if isinstance(item, str) and str(item).strip()
    ] if isinstance(commands, list) else []
    app_names = [
        str(item).strip()
        for item in apps
        if isinstance(item, str) and str(item).strip()
    ] if isinstance(apps, list) else []
    if len(command_names) > 32 or len(app_names) > 32:
        return _json({"success": False, "error": "Check at most 32 commands and apps"})

    command_status = {}
    for command in command_names:
        if Path(command).name != command:
            command_status[command] = {"available": False, "reason": "name_only"}
            continue
        resolved = shutil.which(command)
        command_status[command] = {
            "available": resolved is not None,
            "path": resolved,
        }

    app_roots = (Path("/Applications"), Path.home() / "Applications")
    app_status = {}
    for app in app_names:
        if "/" in app or "\\" in app or app in {".", ".."}:
            app_status[app] = {"available": False, "reason": "name_only"}
            continue
        bundle = app if app.lower().endswith(".app") else f"{app}.app"
        found = next(
            (str(root / bundle) for root in app_roots if (root / bundle).is_dir()),
            None,
        )
        app_status[app] = {"available": found is not None, "path": found}

    return _json(
        {
            "success": True,
            "commands": command_status,
            "apps": app_status,
        }
    )


def register_drafting_tools(ctx: Any) -> None:
    ctx.register_tool(
        name="crunch_read_evidence",
        toolset=TOOLSET,
        schema={
            "name": "crunch_read_evidence",
            "description": (
                "Read one bounded text artifact from the active recording bundle."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "relative_path": {
                        "type": "string",
                        "description": "Relative path from the recording bundle index.",
                    }
                },
                "required": ["relative_path"],
            },
        },
        handler=lambda args, **_kwargs: _read_evidence(
            str(args.get("relative_path") or "")
        ),
        description="Read active recording evidence",
        emoji="🔎",
    )
    ctx.register_tool(
        name="crunch_skills_list",
        toolset=TOOLSET,
        schema={
            "name": "crunch_skills_list",
            "description": "List installed skills available to a future Cron run.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Optional exact skill category.",
                    }
                },
            },
        },
        handler=lambda args, **kwargs: _list_skills(
            args.get("category"),
            task_id=kwargs.get("task_id"),
        ),
        description="List available runtime skills",
        emoji="📚",
    )
    ctx.register_tool(
        name="crunch_skill_view",
        toolset=TOOLSET,
        schema={
            "name": "crunch_skill_view",
            "description": "Read one installed skill or one linked skill file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Exact skill name from the skill listing.",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Optional linked path within that skill.",
                    },
                },
                "required": ["name"],
            },
        },
        handler=lambda args, **kwargs: _view_skill(
            str(args.get("name") or ""),
            args.get("file_path"),
            task_id=kwargs.get("task_id"),
        ),
        description="Read an available runtime skill",
        emoji="📚",
    )
    ctx.register_tool(
        name="crunch_check_requirements",
        toolset=TOOLSET,
        schema={
            "name": "crunch_check_requirements",
            "description": (
                "Check whether named commands and macOS applications are installed "
                "without executing them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "commands": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "apps": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
        handler=lambda args, **_kwargs: _check_requirements(
            args.get("commands"),
            args.get("apps"),
        ),
        description="Check non-executing runtime prerequisites",
        emoji="✓",
    )
