"""Narrow access to the Hermes-owned services exposed to this plugin."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from threading import RLock
import time
from typing import Any
import uuid


_LOCK = RLock()
_PLUGIN_CONTEXT: Any = None
log = logging.getLogger(__name__)


def set_plugin_context(context: Any) -> None:
    """Bind the current Hermes PluginContext during plugin registration."""

    global _PLUGIN_CONTEXT
    with _LOCK:
        _PLUGIN_CONTEXT = context


def _plugin_context() -> Any:
    with _LOCK:
        context = _PLUGIN_CONTEXT
    if context is None:
        from hermes_cli.plugins import discover_plugins

        discover_plugins()
        with _LOCK:
            context = _PLUGIN_CONTEXT
    if context is None:
        raise RuntimeError(
            "Record is not registered with this Hermes process. "
            "Enable the plugin and restart the active profile."
        )
    return context


def complete_structured(
    *,
    trace_scope: str,
    **kwargs: Any,
) -> Any:
    """Run one ``ctx.llm`` call through Hermes' standard lifecycle hooks.

    ``ctx.llm`` deliberately owns provider routing and credentials. Hermes
    does not yet emit lifecycle events for plugin-owned completions itself, so
    Record publishes the same public ``pre_api_request`` /
    ``post_api_request`` events around its own call. The enabled stock
    observability plugin remains the only trace backend.
    """

    context = _plugin_context()
    profile = str(context.profile_name or "default")
    from hermes_constants import get_hermes_home

    profile_home = str(get_hermes_home().expanduser().resolve())
    purpose = str(kwargs.get("purpose") or "plugin.completion")
    request_id = uuid.uuid4().hex
    scope = str(trace_scope or "workflow").strip() or "workflow"
    task_id = f"record:{profile}:{purpose}:{request_id}"
    session_id = (
        f"record:profile={_trace_token(profile, 48)}:"
        f"workflow={_trace_token(scope, 96)}"
    )
    started = time.monotonic()
    try:
        result = context.llm.complete_structured(**kwargs)
    except Exception:
        # Stock Langfuse does not consume a terminal plugin-request error hook.
        # Do not synthesize a success-shaped trace for a failed completion.
        log.debug("Record ctx.llm call failed before trace publication", exc_info=True)
        raise

    usage = getattr(result, "usage", None)
    _publish_llm_lifecycle(
        kwargs=kwargs,
        task_id=task_id,
        session_id=session_id,
        profile=profile,
        profile_home=profile_home,
        request_id=request_id,
        provider=str(getattr(result, "provider", "") or ""),
        model=str(getattr(result, "model", "") or ""),
        duration=time.monotonic() - started,
        text=str(getattr(result, "text", "") or ""),
        usage={
            key: value
            for key in (
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "cost_usd",
            )
            if (value := getattr(usage, key, None)) is not None
        },
        finish_reason="stop",
    )
    return result


def _publish_llm_lifecycle(
    *,
    kwargs: dict[str, Any],
    task_id: str,
    session_id: str,
    profile: str,
    profile_home: str,
    request_id: str,
    provider: str,
    model: str,
    duration: float,
    text: str,
    usage: dict[str, Any],
    finish_reason: str,
) -> None:
    """Publish one completed plugin call without owning an observer client."""

    try:
        from hermes_cli.plugins import invoke_hook

        common = {
            "task_id": task_id,
            "session_id": session_id,
            "profile_id": profile,
            "profile_home": profile_home,
            "turn_id": request_id,
            "api_request_id": request_id,
            "api_call_count": 1,
            "platform": "plugin",
            "provider": provider,
            "model": model,
            "api_mode": "plugin",
        }
        invoke_hook(
            "pre_api_request",
            **common,
            request_messages=_trace_messages(kwargs),
            message_count=2,
            max_tokens=kwargs.get("max_tokens"),
        )
        trace_text = text or "[empty response]"
        invoke_hook(
            "post_api_request",
            **common,
            api_duration=max(0.0, duration),
            finish_reason=finish_reason,
            usage=usage,
            assistant_response=trace_text,
            assistant_content_chars=len(trace_text),
            assistant_tool_call_count=0,
        )
    except Exception:
        # Observability is fail-open in Hermes. A trace backend must never
        # change whether the reviewed workflow can be drafted.
        return


def _trace_token(value: str, max_chars: int) -> str:
    """Return one bounded ASCII correlation token for Langfuse IDs."""

    raw = str(value or "unknown")
    normalized = "".join(
        character if character.isascii() and (character.isalnum() or character in "._-")
        else "-"
        for character in raw
    ).strip("-") or "unknown"
    if len(normalized) <= max_chars:
        return normalized
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{normalized[: max_chars - len(digest) - 1]}-{digest}"


def _trace_messages(kwargs: dict[str, Any]) -> list[dict[str, Any]]:
    """Represent structured inputs without copying binary evidence to hooks."""

    inputs: list[str] = []
    for item in kwargs.get("input") or []:
        if not isinstance(item, dict):
            inputs.append(f"[{type(item).__name__} input]")
            continue
        if item.get("type") == "text":
            inputs.append(str(item.get("text") or ""))
            continue
        if item.get("type") == "image":
            label = str(item.get("file_name") or "image")
            inputs.append(f"[image input: {label}]")
            continue
        inputs.append(f"[{str(item.get('type') or 'structured')} input]")
    return [
        {
            "role": "system",
            "content": str(kwargs.get("instructions") or ""),
        },
        {
            "role": "user",
            "content": "\n\n".join(inputs),
        },
    ]


def dispatch_tool(name: str, arguments: dict[str, Any]) -> Any:
    """Dispatch one stock Hermes tool through the public plugin context."""

    context = _plugin_context()
    # A plugin-only Desktop backend can start before Hermes' agent loop has
    # imported the built-in tool modules. Use Hermes' own registry discovery
    # once when needed; Record does not keep a tool map or dispatch a tool
    # outside the public PluginContext facade.
    from tools.registry import discover_builtin_tools, registry

    if registry.get_entry(name) is None:
        discover_builtin_tools()
    return context.dispatch_tool(name, arguments)


def plugin_profile() -> tuple[str, Path]:
    """Return the profile identity already selected by this Hermes backend."""

    from hermes_constants import get_hermes_home

    context = _plugin_context()
    return str(context.profile_name), get_hermes_home().expanduser().resolve()
