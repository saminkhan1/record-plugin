"""Reusable gateway notifier for Hermes' existing approval queue."""

from __future__ import annotations

import logging
from typing import Any, Callable

from agent.async_utils import safe_schedule_threadsafe


def make_gateway_approval_notifier(
    *,
    adapter: Any,
    loop: Any,
    chat_id: str,
    session_key: str,
    metadata: dict[str, Any] | None = None,
    logger: logging.Logger | None = None,
) -> Callable[[dict[str, Any]], None]:
    """Build the sync callback expected by ``register_gateway_notify``.

    This is the same adapter contract used by interactive agent approvals.  A
    button-capable adapter receives the exact queue session key, so its inbound
    button callback resolves the blocked cron worker directly.
    """

    log = logger or logging.getLogger(__name__)
    route_metadata = dict(metadata or {})

    def notify(approval_data: dict[str, Any]) -> None:
        request_metadata = dict(route_metadata)
        # The approval gate owns this decision.  Do not let static route
        # metadata broaden a reviewed workflow checkpoint into a reusable
        # session/permanent approval in the adapter UI.
        request_metadata["require_fresh_human"] = (
            approval_data.get("require_fresh_human") is True
        )

        pause = getattr(adapter, "pause_typing_for_chat", None)
        if callable(pause):
            pause(chat_id)

        command = str(approval_data.get("command") or "")
        description = str(
            approval_data.get("description") or "workflow action"
        )
        send_approval = getattr(adapter, "send_exec_approval", None)
        if callable(send_approval):
            future = safe_schedule_threadsafe(
                send_approval(
                    chat_id=chat_id,
                    command=command,
                    session_key=session_key,
                    description=description,
                    metadata=request_metadata,
                    allow_permanent=False,
                    allow_session=False,
                ),
                loop,
                logger=log,
                log_message="workflow approval scheduling error",
            )
            if future is None:
                raise RuntimeError("workflow approval gateway loop is unavailable")
            try:
                result = future.result(timeout=15)
            except Exception:
                # The loop task can outlive the sync bridge timeout and create
                # its adapter mapping after the queue already cleaned up. Run
                # cleanup again when that late task finishes so teardown has
                # no send/registration race.
                try:
                    future.add_done_callback(lambda _future: cleanup())
                except Exception:
                    pass
                raise
            if getattr(result, "success", False):
                return
            log.warning(
                "Workflow approval button send failed: %s",
                getattr(result, "error", "unknown adapter error"),
            )

        # Typed /approve cannot safely address a synthetic cron-run queue from
        # every platform/session-isolation mode.  Tell the operator why this run
        # was blocked, then fail the notify so the approval gate unwinds now
        # instead of hanging until its five-minute timeout.
        message = (
            "⚠️ This workflow reached a protected action, but this gateway "
            "adapter cannot present an approval control for scheduled runs. "
            "The action was not executed."
        )
        future = safe_schedule_threadsafe(
            adapter.send(chat_id, message, metadata=request_metadata),
            loop,
            logger=log,
            log_message="workflow approval fallback send error",
        )
        if future is not None:
            future.result(timeout=15)
        raise RuntimeError("adapter has no usable scheduled-run approval control")

    def cleanup() -> None:
        """Release adapter-local callback IDs for this synthetic queue."""

        clear = getattr(adapter, "clear_exec_approval", None)
        if not callable(clear):
            return
        try:
            clear(session_key)
        except Exception:
            log.debug(
                "Workflow approval control cleanup failed for %s",
                session_key,
                exc_info=True,
            )

    # ``tools.approval`` owns the queue lifetime and invokes this generic
    # callback after each terminal decision and again on session teardown.
    # Keeping the hook on the notifier avoids platform imports in the queue.
    setattr(notify, "_approval_cleanup", cleanup)

    return notify
