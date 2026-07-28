"""Plugin-owned validation for reviewed Crunch authorization policies."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping


PROTECTED_EFFECTS = (
    "create",
    "update",
    "delete",
    "send",
    "publish",
    "purchase",
    "execute",
)
ALLOWED_EFFECTS = frozenset(PROTECTED_EFFECTS)
DECLARATIVE_FILTER_OPERATORS = (
    "exists",
    "missing",
    "equals",
    "not_equals",
    "contains",
    "in",
    "regex",
)
_FILTER_MISSING = object()
_OBSERVATIONAL_TOOLS = frozenset(
    {
        "browser_get_images",
        "browser_snapshot",
        "browser_vision",
        "read_file",
        "search_files",
        "skill_view",
        "skills_list",
        "video_analyze",
        "vision_analyze",
        "web_search",
    }
)
_OBSERVATIONAL_COMPUTER_USE_ACTIONS = frozenset(
    {"capture", "list_apps", "list_windows"}
)


def execution_contract_sha256(value: Mapping[str, Any]) -> str:
    """Bind approval to the job fields that can change what executes."""

    def optional_text(name: str) -> str | None:
        item = value.get(name)
        return item if isinstance(item, str) and item else None

    def text_list(name: str) -> list[str]:
        item = value.get(name)
        if item is None:
            return []
        if not isinstance(item, list) or any(
            not isinstance(entry, str) for entry in item
        ):
            raise ValueError(f"{name} must be a list of strings")
        return list(item)

    context_from = value.get("context_from")
    if isinstance(context_from, str):
        normalized_context = [context_from] if context_from else []
    elif context_from is None:
        normalized_context = []
    elif isinstance(context_from, list) and all(
        isinstance(entry, str) for entry in context_from
    ):
        normalized_context = list(context_from)
    else:
        raise ValueError("context_from must be a string or list of strings")

    contract = {
        "prompt": optional_text("prompt"),
        "skills": text_list("skills"),
        "enabled_toolsets": text_list("enabled_toolsets"),
        "script": optional_text("script"),
        "no_agent": value.get("no_agent") is True,
        "workdir": optional_text("workdir"),
        "context_from": normalized_context,
        "model": optional_text("model"),
        "provider": optional_text("provider"),
        "base_url": optional_text("base_url"),
        "deliver": optional_text("deliver"),
        "attach_to_session": (
            value.get("attach_to_session")
            if isinstance(value.get("attach_to_session"), bool)
            else None
        ),
    }
    return hashlib.sha256(
        json.dumps(
            contract,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def normalize_declarative_filter(
    value: Any,
    *,
    subject: str = "declarative filter",
    sequence_is_all: bool = False,
) -> dict[str, Any]:
    """Validate one filter and return its canonical object form."""

    if sequence_is_all and isinstance(value, list):
        if not value:
            raise ValueError(f"{subject} must be a non-empty list or filter object")
        return {
            "all": [
                normalize_declarative_filter(item, subject=subject)
                for item in value
            ]
        }
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{subject} must be a declarative filter object")

    groups = [key for key in ("all", "any", "not") if key in value]
    if groups:
        if len(groups) != 1 or len(value) != 1:
            raise ValueError(f"{subject} has an invalid nested filter")
        group = groups[0]
        nested = value[group]
        if group == "not":
            return {
                "not": normalize_declarative_filter(nested, subject=subject)
            }
        if not isinstance(nested, list) or not nested:
            raise ValueError(f"{subject}.{group} must be a non-empty list")
        return {
            group: [
                normalize_declarative_filter(item, subject=subject)
                for item in nested
            ]
        }

    field = value.get("field")
    if not isinstance(field, str) or not field.strip():
        raise ValueError(f"{subject} requires a non-empty field")
    operators = [key for key in DECLARATIVE_FILTER_OPERATORS if key in value]
    if len(operators) != 1 or set(value) != {"field", *operators}:
        raise ValueError(f"{subject} requires exactly one supported filter operator")
    operator = operators[0]
    operand = value[operator]
    if operator == "exists" and not isinstance(operand, bool):
        raise ValueError(f"{subject}.exists must be a boolean")
    if operator == "missing" and operand is not True:
        raise ValueError(f"{subject}.missing must be true")
    if operator == "in" and not isinstance(operand, list):
        raise ValueError(f"{subject}.in must be a list")
    if operator == "regex":
        if not isinstance(operand, str):
            raise ValueError(f"{subject}.regex must be a string")
        try:
            re.compile(operand)
        except re.error as exc:
            raise ValueError(f"{subject}.regex is invalid") from exc
    return {"field": field.strip(), operator: operand}


def _normalize_entry(
    raw: Any,
    *,
    kind: str,
    seen_ids: set[str],
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"authorization {kind} entries must be objects")
    entry_id = str(raw.get("id") or "").strip()
    if not entry_id or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", entry_id
    ):
        raise ValueError(f"authorization {kind} id is missing or invalid")
    if entry_id in seen_ids:
        raise ValueError(f"duplicate authorization {kind} id: {entry_id}")
    effect = str(raw.get("effect") or "").strip().lower()
    if effect not in ALLOWED_EFFECTS:
        raise ValueError(
            f"authorization {kind} {entry_id!r} uses an unsupported effect"
        )
    description = str(raw.get("description") or "").strip()
    if not description:
        raise ValueError(f"authorization {kind} {entry_id!r} requires a description")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(
            f"authorization {kind} {entry_id!r} enabled must be a boolean"
        )
    seen_ids.add(entry_id)
    return {
        "id": entry_id,
        "effect": effect,
        "description": description,
        "enabled": enabled,
    }


def normalize_execution_policy(value: Any) -> dict[str, Any] | None:
    """Validate the policy stored inside a Crunch job extension."""

    if value in (None, {}, False):
        return None
    if not isinstance(value, dict):
        raise ValueError("authorization must be an object or null")
    mode = str(value.get("mode") or "none").strip().lower()
    if mode == "none":
        return None
    if mode != "human_in_loop":
        raise ValueError("authorization.mode must be none or human_in_loop")

    raw_effects = value.get("effects")
    if not isinstance(raw_effects, list):
        raise ValueError("authorization.effects must be a list")
    effects: list[str] = []
    for raw_effect in raw_effects:
        effect = str(raw_effect or "").strip().lower()
        if effect not in ALLOWED_EFFECTS:
            raise ValueError(
                "authorization.effects contains unsupported effect: "
                + repr(effect)
            )
        if effect not in effects:
            effects.append(effect)
    if effects:
        raise ValueError(
            "authorization.effects blanket gates are unsupported; use explicit "
            "authorization.operations or one exact authorization.script approval"
        )

    target = str(value.get("target") or "origin").strip() or "origin"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}", target):
        raise ValueError("authorization.target is invalid")

    raw_route = value.get("approval_route")
    approval_route = None
    if raw_route is not None:
        if not isinstance(raw_route, dict):
            raise ValueError("authorization.approval_route must be an object")
        platform = str(raw_route.get("platform") or "").strip().lower()
        chat_id = str(raw_route.get("chat_id") or "").strip()
        raw_thread_id = raw_route.get("thread_id")
        thread_id = (
            str(raw_thread_id).strip()
            if raw_thread_id not in (None, "")
            else None
        )
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", platform):
            raise ValueError("authorization.approval_route.platform is invalid")
        if not chat_id or len(chat_id) > 1024 or any(
            ord(character) < 32 or ord(character) == 127
            for character in chat_id
        ):
            raise ValueError("authorization.approval_route.chat_id is invalid")
        if thread_id is not None and (
            len(thread_id) > 1024
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in thread_id
            )
        ):
            raise ValueError("authorization.approval_route.thread_id is invalid")
        if target != "origin" and platform != target.split(":", 1)[0].lower():
            raise ValueError(
                "authorization.approval_route.platform must match authorization.target"
            )
        approval_route = {
            "platform": platform,
            "chat_id": chat_id,
            "thread_id": thread_id,
        }

    raw_operations = value.get("operations", [])
    if not isinstance(raw_operations, list):
        raise ValueError("authorization.operations must be a list")
    operations = []
    seen_operation_ids: set[str] = set()
    for raw in raw_operations:
        operation = _normalize_entry(
            raw,
            kind="operation",
            seen_ids=seen_operation_ids,
        )
        operation_id = operation["id"]
        tool = str(raw.get("tool") or "").strip()
        if (
            not tool
            or len(tool) > 256
            or any(ord(character) < 32 or ord(character) == 127 for character in tool)
        ):
            raise ValueError(
                f"authorization operation {operation_id!r} tool is missing or invalid"
            )
        if "arguments" not in raw:
            raise ValueError(
                f"authorization operation {operation_id!r} requires arguments"
            )
        arguments = raw["arguments"]
        if arguments is not None:
            arguments = normalize_declarative_filter(
                arguments,
                subject=f"authorization operation {operation_id!r} arguments",
            )
        operations.append({**operation, "tool": tool, "arguments": arguments})

    raw_checkpoints = value.get("checkpoints") or []
    if not isinstance(raw_checkpoints, list):
        raise ValueError("authorization.checkpoints must be a list")
    if raw_checkpoints:
        raise ValueError(
            "Inline script checkpoints are unsupported because they cannot "
            "prove which later effect they guard; approve the exact reviewed "
            "script as one authorization.script boundary"
        )

    raw_script = value.get("script")
    script = None
    if raw_script is not None:
        script = _normalize_entry(
            raw_script,
            kind="script",
            seen_ids=set(),
        )
        if script["effect"] != "execute":
            raise ValueError("authorization.script.effect must be execute")

    policy_id = str(value.get("policy_id") or "").strip()
    if not policy_id:
        stable = json.dumps(
            {
                "effects": effects,
                "target": target,
                "approval_route": approval_route,
                "operations": operations,
                "script": script,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        policy_id = hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]

    script_sha256 = str(value.get("script_sha256") or "").strip().lower()
    if script_sha256 and not re.fullmatch(r"[0-9a-f]{64}", script_sha256):
        raise ValueError("authorization.script_sha256 must be a SHA-256 digest")
    execution_sha256 = str(value.get("execution_sha256") or "").strip().lower()
    if execution_sha256 and not re.fullmatch(r"[0-9a-f]{64}", execution_sha256):
        raise ValueError("authorization.execution_sha256 must be a SHA-256 digest")

    normalized = {
        "mode": "human_in_loop",
        "policy_id": policy_id,
        "effects": effects,
        "target": target,
        "operations": operations,
        "checkpoints": [],
        "script": script,
    }
    if approval_route is not None:
        normalized["approval_route"] = approval_route
    if script_sha256:
        normalized["script_sha256"] = script_sha256
    if execution_sha256:
        normalized["execution_sha256"] = execution_sha256
    return normalized


def execution_policy_requires_human(value: Any) -> bool:
    policy = normalize_execution_policy(value)
    return bool(
        policy
        and (
            any(item["enabled"] for item in policy["operations"])
            or (
                isinstance(policy.get("script"), dict)
                and policy["script"]["enabled"]
            )
        )
    )


def declarative_filter_matches(spec: dict[str, Any] | None, value: Any) -> bool:
    """Evaluate one normalized filter against final tool arguments."""

    if spec is None:
        return True
    if "all" in spec:
        return all(declarative_filter_matches(item, value) for item in spec["all"])
    if "any" in spec:
        return any(declarative_filter_matches(item, value) for item in spec["any"])
    if "not" in spec:
        return not declarative_filter_matches(spec["not"], value)

    resolved = value
    for part in (item for item in spec["field"].split(".") if item):
        if isinstance(resolved, dict):
            resolved = resolved.get(part, _FILTER_MISSING)
        elif isinstance(resolved, list) and part.isdigit():
            index = int(part)
            resolved = (
                resolved[index]
                if 0 <= index < len(resolved)
                else _FILTER_MISSING
            )
        else:
            resolved = _FILTER_MISSING
        if resolved is _FILTER_MISSING:
            break

    if "exists" in spec:
        return (resolved is not _FILTER_MISSING) is spec["exists"]
    if "missing" in spec:
        return resolved is _FILTER_MISSING
    if "equals" in spec:
        return resolved is not _FILTER_MISSING and resolved == spec["equals"]
    if "not_equals" in spec:
        return resolved is _FILTER_MISSING or resolved != spec["not_equals"]
    if "contains" in spec:
        if resolved is _FILTER_MISSING:
            return False
        needle = spec["contains"]
        if isinstance(resolved, (list, tuple, set, dict)):
            return needle in resolved
        return str(needle) in _stringify(resolved)
    if "in" in spec:
        return resolved in spec["in"]
    if "regex" in spec:
        return (
            resolved is not _FILTER_MISSING
            and re.search(spec["regex"], _stringify(resolved)) is not None
        )
    return False


def tool_call_is_observational(tool_name: Any, args: Any) -> bool:
    """Return whether an exact tool call is safe to run without an operation.

    Hermes tool metadata does not currently declare read/write effects, so the
    protected-workflow boundary keeps this allowlist deliberately small.
    Unknown tools and mixed read/write tools fail closed.
    """

    name = str(tool_name or "").strip()
    if name in _OBSERVATIONAL_TOOLS:
        return True
    if not isinstance(args, Mapping):
        return False
    if name == "computer_use":
        return (
            str(args.get("action") or "").strip().lower()
            in _OBSERVATIONAL_COMPUTER_USE_ACTIONS
        )
    if name == "browser_console":
        return not args.get("expression") and args.get("clear") is not True
    return False


def _stringify(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return "" if value is _FILTER_MISSING else str(value)
