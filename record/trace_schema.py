from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

LEGACY_SCHEMA_VERSION = "hermes-workflow-trace-v2"
SCHEMA_VERSION = "hermes-workflow-trace-v3"
SUPPORTED_SCHEMA_VERSIONS = {LEGACY_SCHEMA_VERSION, SCHEMA_VERSION}
SECRET_QUERY_MARKERS = ("token", "auth", "code", "key", "secret", "session", "sid", "jwt", "password", "passwd", "pwd")

TRACE_ACTION_KINDS = {
    "click", "context_menu", "double_click", "type", "change", "submit", "reset",
    "navigate", "upload", "download", "scroll", "key", "shortcut",
    "select", "copy", "cut", "paste", "drag", "drop",
}
TRACE_CONTEXT_KINDS = {
    "session.started", "session.ended", "app.changed", "window.changed",
    "chrome.segment.started", "chrome.segment.heartbeat", "chrome.segment.stopped",
    "chrome.active_target.changed", "chrome.environment",
    "chrome.provider.finalized",
}
TRACE_EVIDENCE_KINDS = {
    "native.chrome_fallback", "network.summary", "dialog.opened", "dialog.closed",
    "screenshot", "capture.gap", "capture.error", "chrome_debugger_unavailable",
    "active_surface.changed", "clipboard.captured", "capture.sealed",
}
TRACE_SOURCES = {"native", "chrome", "system"}
TRACE_TYPES = {"action", "context", "evidence"}
PAYLOAD_GROUPS = {
    "session", "app", "window", "browser", "provider", "environment", "meta",
    "target", "input", "effect",
}
_URL_KEYS = {"url", "origin", "href", "before_url", "after_url", "frame_url", "top_url", "final_url"}
_TARGET_IDENTIFIERS = {
    "selector", "selectors", "id", "test_id", "name", "label", "role", "ax", "coordinates",
}
_TARGET_REQUIRED_ACTION_KINDS = {
    "click", "double_click", "context_menu", "type", "change", "paste", "key",
    "shortcut", "submit", "reset", "upload", "drop", "scroll", "select", "copy",
    "cut", "drag", "native.chrome_fallback",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def supports_schema_version(value: object) -> bool:
    """Return whether a recording can be safely read by this Record version."""

    return str(value or "").strip() in SUPPORTED_SCHEMA_VERSIONS


def url_origin(value: object) -> str:
    """Return the URL's scheme and authority, or an empty string."""

    parsed = urlsplit(str(value or ""))
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""


def http_url_origin(value: object) -> str:
    """Return a canonical HTTP(S) origin, or an empty string."""

    parsed = urlsplit(str(value or ""))
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return f"{scheme}://{parsed.netloc.lower()}"


def redact_url(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlsplit(text)
    if not parsed.scheme and not parsed.netloc:
        return text
    hostname = parsed.hostname or ""
    netloc = hostname
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    query = urlencode(
        [
            (key, "[REDACTED]" if any(marker in key.lower() for marker in SECRET_QUERY_MARKERS) else val)
            for key, val in parse_qsl(parsed.query, keep_blank_values=True)
        ],
        doseq=True,
    )
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))


def compact_text(value: object, *, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def normalize_trace_row(row: dict[str, object]) -> dict[str, object]:
    normalized: dict[str, object] = dict(row)
    row_type = str(normalized.get("type") or "").strip()
    source = str(normalized.get("source") or "").strip()
    kind = str(normalized.get("kind") or "").strip()
    if row_type not in TRACE_TYPES:
        row_type = "evidence"
        _add_warning(normalized, "invalid_type")
    if source not in TRACE_SOURCES:
        source = "system"
        _add_warning(normalized, "invalid_source")
    if not kind:
        kind = "capture.error"
        row_type = "evidence"
        _add_warning(normalized, "missing_kind")
    if row_type == "action" and kind not in TRACE_ACTION_KINDS:
        row_type = "evidence"
        _add_warning(normalized, "invalid_action_kind")
    elif row_type == "context" and kind not in TRACE_CONTEXT_KINDS:
        row_type = "evidence"
        _add_warning(normalized, "invalid_context_kind")
    elif row_type == "evidence" and kind not in TRACE_EVIDENCE_KINDS and kind not in TRACE_ACTION_KINDS:
        _add_warning(normalized, "unknown_evidence_kind")

    payload = normalized.get("payload") if isinstance(normalized.get("payload"), dict) else {}
    payload = _redact_payload(_filter_payload_groups(payload))
    normalized["payload"] = payload
    normalized["type"] = row_type
    normalized["source"] = source
    normalized["kind"] = kind
    normalized["ts"] = str(normalized.get("ts") or utc_now())
    try:
        normalized["id"] = int(normalized.get("id") or 0)
    except (TypeError, ValueError):
        normalized["id"] = 0

    quality = normalized.get("quality") if isinstance(normalized.get("quality"), dict) else {}
    normalized["quality"] = _normalize_quality(quality)
    _validate_required_payload(normalized)
    _reconcile_missing_target_warning(normalized)
    _compute_match_key(normalized)
    return normalized


def privacy_for_step(step: dict[str, object]) -> str:
    target = step.get("target") if isinstance(step.get("target"), dict) else {}
    input_payload = step.get("input") if isinstance(step.get("input"), dict) else {}
    if target.get("secure_input") or target.get("redacted") or input_payload.get("secure_input") or input_payload.get("redacted"):
        return "secret_redacted"
    if any(input_payload.get(key) not in (None, "", [], {}) for key in ("text", "value", "selected_text", "selected_items", "files")):
        return "user_content"
    if any(target.get(key) not in (None, "", [], {}) for key in ("value", "text", "value_preview")):
        return "user_content"
    return "public"


def readiness_for_step(step: dict[str, object], preconditions: dict[str, object]) -> dict[str, object]:
    waits: list[dict[str, object]] = []
    assertions: list[dict[str, object]] = []
    warnings: list[str] = []
    surface = str(step.get("surface") or "")
    action = str(step.get("action") or "")
    target = step.get("target") if isinstance(step.get("target"), dict) else {}

    if surface == "browser":
        selector = str(target.get("selector") or "").strip()
        browser_url = str(step.get("browser_url") or step.get("browser_top_url") or step.get("browser_frame_url") or "").strip()
        origin = str(step.get("browser_origin") or "").strip() or url_origin(browser_url)
        title = str(step.get("browser_title") or "").strip()
        if action in {"click", "type", "submit", "upload", "change", "paste", "select"}:
            if selector:
                waits.append({"type": "dom_selector", "selector": selector})
            elif origin or browser_url:
                waits.append(_compact({"type": "url", "origin": origin, "path": _path(browser_url)}))
            else:
                warnings.append("missing_browser_readiness")
        elif action == "navigate":
            waits.append(_compact({"type": "url", "origin": origin, "path": _path(browser_url), "title": title}))
        if preconditions.get("browser_profile_hint") == "current-profile":
            warnings.append("browser_profile_unverified")
    elif surface == "desktop":
        app = step.get("app") or target.get("app") or target.get("bundle_id")
        window_title = step.get("window_title") or target.get("window_title")
        if app:
            waits.append(_compact({"type": "app", "bundle_id": target.get("bundle_id"), "name": app}))
        if window_title:
            waits.append({"type": "window_title", "title": window_title})
        if not waits:
            warnings.append("missing_desktop_readiness")

    for file_item in _step_files(step):
        waits.append({"type": "file_exists", "path": file_item})
    if action in {"download"}:
        assertions.append({"type": "download_complete"})
    if privacy_for_step(step) == "secret_redacted":
        warnings.append("secret_redacted")
    return _compact({"waits": waits, "assertions": assertions, "warnings": warnings})


def _normalize_quality(value: dict[str, object]) -> dict[str, object]:
    confidence = str(value.get("confidence") or "medium").strip().lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "medium"
    warnings = value.get("warnings") if isinstance(value.get("warnings"), list) else []
    return {"confidence": confidence, "warnings": [str(item) for item in warnings if str(item).strip()]}


def _filter_payload_groups(payload: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in payload.items() if key in PAYLOAD_GROUPS and isinstance(value, (dict, list, str, int, float, bool))}


def _redact_payload(value: object, key: str = "") -> object:
    if isinstance(value, dict):
        return {item_key: _redact_payload(item_value, item_key) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_redact_payload(item, key) for item in value]
    if key.lower() in _URL_KEYS or key.lower().endswith("url"):
        return redact_url(value)
    if key.lower() in {"message_preview", "label", "name", "title", "description", "placeholder", "value_preview"}:
        return compact_text(value)
    return value


def _validate_required_payload(row: dict[str, object]) -> None:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    row_type = str(row.get("type") or "")
    kind = str(row.get("kind") or "")
    input_payload = payload.get("input") if isinstance(payload.get("input"), dict) else {}
    effect = payload.get("effect") if isinstance(payload.get("effect"), dict) else {}
    browser = payload.get("browser") if isinstance(payload.get("browser"), dict) else {}
    session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
    app = payload.get("app") if isinstance(payload.get("app"), dict) else {}
    window = payload.get("window") if isinstance(payload.get("window"), dict) else {}
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    provider = payload.get("provider") if isinstance(payload.get("provider"), dict) else {}
    environment = payload.get("environment") if isinstance(payload.get("environment"), dict) else {}
    if row_type == "action" or kind == "native.chrome_fallback":
        if action_requires_target(row) and not has_replayable_target(row):
            _add_warning(row, "missing_target")
            _set_low_confidence(row)
        if kind in {"type", "change", "paste"} and not (input_payload.get("redacted") or input_payload.get("secure_input") or input_payload.get("value") or input_payload.get("text")):
            _missing_replay_data(row)
        if kind in {"upload", "drop"} and not input_payload.get("files"):
            _missing_replay_data(row)
        if kind == "download" and not (effect.get("download") or effect.get("url") or effect.get("final_url")):
            _missing_replay_data(row)
        if kind == "navigate":
            _warn_missing(row, browser, ("url", "origin", "title", "frame_id", "top_url"), "missing_browser")
            if not effect.get("after_url"):
                _add_warning(row, "missing_after_url")
                _set_low_confidence(row)

    if row_type == "context":
        if kind == "session.started":
            _warn_missing(row, session, ("id", "maximum_duration_seconds"), "missing_session")
        elif kind == "session.ended":
            _warn_missing(row, session, ("id", "end_reason"), "missing_session")
        elif kind in {"app.changed", "window.changed"}:
            if not app:
                _add_warning(row, "missing_app")
                _set_low_confidence(row)
            if not window:
                _add_warning(row, "missing_window")
                _set_low_confidence(row)
        elif kind.startswith("chrome."):
            _warn_missing(row, browser, ("active_target_id", "tab_id", "window_id", "url", "title", "profile_hint"), "missing_browser")
            if kind.startswith("chrome.segment.") or kind == "chrome.environment":
                _warn_missing(
                    row,
                    provider,
                    ("desired", "effective", "attached", "heartbeat_at", "content_script_ready"),
                    "missing_provider",
                )
            if kind == "chrome.environment":
                _warn_missing(row, environment, ("user_agent", "platform", "language", "timezone", "online"), "missing_environment")

    if row_type == "evidence":
        if kind == "network.summary":
            _warn_missing(row, browser, ("url",), "missing_browser")
            _warn_missing(row, effect, ("method", "url", "status", "duration_ms"), "missing_network_summary")
            for forbidden in ("headers", "request_headers", "response_headers", "request_body", "response_body", "cookies", "authorization", "remote_address"):
                if forbidden in effect:
                    effect.pop(forbidden, None)
                    _add_warning(row, "removed_sensitive_network_detail")
        elif kind in {"dialog.opened", "dialog.closed"}:
            _warn_missing(row, browser, ("url",), "missing_browser")
            _warn_missing(row, meta, ("dialog_type", "message_preview"), "missing_dialog_detail")
        elif kind == "screenshot":
            if not effect.get("artifact_path"):
                _add_warning(row, "missing_artifact_path")
                _set_low_confidence(row)
            if not browser and not (app and window):
                _add_warning(row, "missing_visual_context")
                _set_low_confidence(row)
        elif kind in {"capture.gap", "capture.error", "chrome_debugger_unavailable"}:
            if not meta.get("message_preview") and not effect.get("error_text"):
                _add_warning(row, "missing_error_detail")
                _set_low_confidence(row)


def action_requires_target(row: dict[str, object]) -> bool:
    """Return whether an action needs an element/surface target to replay."""

    kind = str(row.get("kind") or "")
    if kind not in _TARGET_REQUIRED_ACTION_KINDS:
        return False
    if kind == "native.chrome_fallback":
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        if str(meta.get("original_kind") or "") in {"keyboard.shortcut", "selection.changed"}:
            return False
    if kind == "select" and str(row.get("source") or "") == "native":
        return False
    if kind == "scroll" and _is_chrome_page_scroll(row):
        return False
    return True


def _is_chrome_page_scroll(row: dict[str, object]) -> bool:
    if str(row.get("source") or "") != "chrome" or str(row.get("kind") or "") != "scroll":
        return False
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    browser = payload.get("browser") if isinstance(payload.get("browser"), dict) else {}
    input_payload = payload.get("input") if isinstance(payload.get("input"), dict) else {}
    has_page_location = any(browser.get(key) not in (None, "", [], {}) for key in ("url", "origin"))
    has_scroll_offsets = all(input_payload.get(key) not in (None, "", [], {}) for key in ("scroll_x", "scroll_y"))
    return has_page_location and has_scroll_offsets


def has_replayable_target(row: dict[str, object]) -> bool:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    target = payload.get("target") if isinstance(payload.get("target"), dict) else {}
    if any(target.get(key) not in (None, "", [], {}) for key in _TARGET_IDENTIFIERS):
        return True
    return _is_chrome_page_scroll(row)


def _warn_missing(row: dict[str, object], group: dict[str, object], fields: tuple[str, ...], warning: str) -> None:
    if any(group.get(field) in (None, "", [], {}) for field in fields):
        _add_warning(row, warning)
        _set_low_confidence(row)


def _missing_replay_data(row: dict[str, object]) -> None:
    row["type"] = "evidence"
    _add_warning(row, "missing_replay_data")
    _set_low_confidence(row)


def _compute_match_key(row: dict[str, object]) -> None:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    if meta.get("match_key"):
        meta = dict(meta)
        meta.setdefault("match_key_inferred", False)
        payload = dict(payload)
        payload["meta"] = meta
        row["payload"] = payload
        return
    target = payload.get("target") if isinstance(payload.get("target"), dict) else {}
    browser = payload.get("browser") if isinstance(payload.get("browser"), dict) else {}
    window = payload.get("window") if isinstance(payload.get("window"), dict) else {}
    parts = [
        str(row.get("kind") or meta.get("original_kind") or ""),
        str(browser.get("active_target_id") or browser.get("tab_id") or window.get("id") or ""),
        str(target.get("selector") or target.get("id") or target.get("test_id") or target.get("name") or target.get("label") or target.get("role") or ""),
    ]
    if target.get("coordinates") and not parts[-1]:
        coords = target.get("coordinates") if isinstance(target.get("coordinates"), dict) else {}
        parts[-1] = f"{coords.get('x','')}:{coords.get('y','')}"
    meta["match_key"] = "|".join(parts)
    meta["match_key_inferred"] = True
    payload = dict(payload)
    payload["meta"] = meta
    row["payload"] = payload


def _add_warning(row: dict[str, object], warning: str) -> None:
    quality = row.get("quality") if isinstance(row.get("quality"), dict) else {}
    warnings = quality.get("warnings") if isinstance(quality.get("warnings"), list) else []
    if warning not in warnings:
        warnings = [*warnings, warning]
    quality = dict(quality)
    quality["warnings"] = warnings
    row["quality"] = quality


def _reconcile_missing_target_warning(row: dict[str, object]) -> None:
    row_type = str(row.get("type") or "")
    kind = str(row.get("kind") or "")
    if row_type != "action" and kind != "native.chrome_fallback":
        return
    if action_requires_target(row):
        return
    quality = row.get("quality") if isinstance(row.get("quality"), dict) else {}
    warnings = quality.get("warnings") if isinstance(quality.get("warnings"), list) else []
    if "missing_target" not in warnings:
        return
    quality = dict(quality)
    quality["warnings"] = [warning for warning in warnings if warning != "missing_target"]
    row["quality"] = quality


def _set_low_confidence(row: dict[str, object]) -> None:
    quality = row.get("quality") if isinstance(row.get("quality"), dict) else {}
    quality = dict(quality)
    quality["confidence"] = "low"
    row["quality"] = quality


def _path(url: str) -> str:
    parsed = urlsplit(url)
    return parsed.path or ""


def _compact(value: dict[str, object]) -> dict[str, object]:
    return {key: item for key, item in value.items() if item not in (None, "", [], {})}


def _step_files(step: dict[str, object]) -> list[str]:
    target = step.get("target") if isinstance(step.get("target"), dict) else {}
    files: list[str] = []
    for key in ("path", "file", "folder", "download_path", "upload_path"):
        value = target.get(key)
        if isinstance(value, str) and value.strip():
            files.append(value.strip())
    raw_files = target.get("files")
    if isinstance(raw_files, list):
        for item in raw_files:
            if isinstance(item, dict):
                value = item.get("path") or item.get("name")
                if isinstance(value, str) and value.strip():
                    files.append(value.strip())
    return files
