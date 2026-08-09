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
    Record publishes the same public request and terminal events around its
    own call. The enabled stock observability plugin remains the only trace
    backend.
    """

    context = _plugin_context()
    profile = str(getattr(context, "profile_name", "") or "default")
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
    common = {
        "task_id": task_id,
        "session_id": session_id,
        "profile_id": profile,
        "profile_home": profile_home,
        "turn_id": request_id,
        "api_request_id": request_id,
        "api_call_count": 1,
        "platform": "plugin",
        "provider": "",
        "model": "",
        "api_mode": "plugin",
    }
    trace_messages = _trace_messages(kwargs)
    request_body: dict[str, Any] = {"messages": trace_messages}
    if kwargs.get("max_tokens") is not None:
        request_body["max_tokens"] = kwargs["max_tokens"]
    started = time.monotonic()
    lifecycle_opened = _invoke_llm_hook(
        "pre_api_request",
        **common,
        request={"method": "POST", "body": request_body},
        message_count=len(trace_messages),
        tool_count=0,
        max_tokens=kwargs.get("max_tokens"),
    )
    try:
        result = context.llm.complete_structured(**kwargs)
    except Exception as exc:
        if lifecycle_opened:
            _invoke_llm_hook(
                "api_request_error",
                **common,
                api_duration=max(0.0, time.monotonic() - started),
                error={
                    "type": type(exc).__name__,
                    "message": "Plugin LLM request failed",
                },
                reason="plugin_llm_error",
                retryable=False,
            )
            _invoke_llm_hook(
                "on_session_end",
                **_terminal_llm_payload(common),
                completed=False,
                failed=True,
                interrupted=False,
                turn_exit_reason=f"exception:{type(exc).__name__}",
                final_response=None,
            )
        log.debug("Record ctx.llm call failed", exc_info=True)
        raise

    if lifecycle_opened:
        usage = getattr(result, "usage", None)
        common.update(
            provider=str(getattr(result, "provider", "") or ""),
            model=str(getattr(result, "model", "") or ""),
        )
        trace_text = str(getattr(result, "text", "") or "") or "[empty response]"
        _invoke_llm_hook(
            "post_api_request",
            **common,
            api_duration=max(0.0, time.monotonic() - started),
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
            assistant_response=trace_text,
            assistant_content_chars=len(trace_text),
            assistant_tool_call_count=0,
        )
        _invoke_llm_hook(
            "on_session_end",
            **_terminal_llm_payload(common),
            completed=True,
            failed=False,
            interrupted=False,
            turn_exit_reason="plugin_llm_completed",
            final_response=trace_text,
        )
    return result


def run_agent(
    *,
    user_message: Any,
    system_prompt: str,
    task_id: str,
    trace_scope: str = "",
    recording_id: str = "",
    max_iterations: int = 24,
    tool_progress_callback: Any = None,
    status_callback: Any = None,
    required_tools: list[str] | None = None,
    allowed_tools: list[str] | None = None,
    full_capabilities: bool = False,
    purpose: str = "workflow.draft",
) -> dict[str, Any]:
    """Run one host-owned Hermes agent turn with the requested tool scope.

    ``ctx.agent`` resolves the active profile's model, provider, credentials,
    and fallback. Record supplies the ephemeral planning prompt, task
    correlation id, and any explicit host-enforced tool boundary.
    """

    context = _plugin_context()
    profile = str(getattr(context, "profile_name", "") or "default")
    scope = str(trace_scope or "workflow").strip() or "workflow"
    recording = str(recording_id or "").strip()
    correlation = f"{scope}:{recording}" if recording else scope
    result = context.agent.run(
        user_message=user_message,
        system_prompt=system_prompt,
        task_id=task_id,
        max_iterations=max_iterations,
        tool_progress_callback=tool_progress_callback,
        status_callback=status_callback,
        required_tools=required_tools,
        allowed_tools=allowed_tools,
        full_capabilities=full_capabilities,
        purpose=purpose,
        observability_context={
            "name": f"record.{purpose}",
            "session_id": f"record:profile={profile}:workflow={scope}",
            "tags": ["record", f"phase:{purpose}"],
            "metadata": {
                "profile_id": profile,
                "workflow_id": scope,
                "recording_id": recording,
                "correlation_id": correlation,
                "phase": purpose,
            },
        },
    )
    if not isinstance(result, dict):
        raise RuntimeError("Hermes planning agent returned an invalid result")
    return result


def _invoke_llm_hook(name: str, **payload: Any) -> bool:
    """Publish one lifecycle hook without changing the plugin operation."""

    try:
        from hermes_cli.lifecycle import invoke_hook

        invoke_hook(name, **payload)
        return True
    except Exception:
        # Observability is fail-open in Hermes. A trace backend must never
        # change whether the reviewed workflow can be drafted.
        return False


def _terminal_llm_payload(common: dict[str, Any]) -> dict[str, Any]:
    """Return the turn identity used by the host terminal lifecycle."""

    return {
        key: common[key]
        for key in (
            "task_id",
            "session_id",
            "profile_id",
            "profile_home",
            "turn_id",
            "platform",
            "model",
        )
    }


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
