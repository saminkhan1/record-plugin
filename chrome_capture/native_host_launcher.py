#!/usr/bin/env python3
"""Select a receipt-authorized Record native host and run it."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
from pathlib import Path


_PLUGIN_NAME = "record"
_PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_VERSION_RE = re.compile(r"^\d+(?:\.\d+){0,3}$")
_EXTENSION_ID_RE = re.compile(r"^[a-p]{32}$")
_UPDATE_URL = "https://clients2.google.com/service/update2/crx"


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _safe_resolve(path: Path) -> Path | None:
    try:
        return path.expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _read_object(path: Path) -> dict | None:
    try:
        if path.stat().st_size > 256 * 1024:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _valid_version(value: object) -> str:
    version = str(value or "").strip()
    if len(version) > 32 or not _VERSION_RE.fullmatch(version):
        return ""
    parts = version.split(".")
    try:
        if any(len(part) > 5 or int(part) > 65535 for part in parts):
            return ""
    except ValueError:
        return ""
    return version


def _extension_identity(plugin_root: Path) -> tuple[str, str] | None:
    manifest_path = (
        plugin_root / "chrome_capture" / "extension" / "manifest.json"
    )
    resolved_manifest = _safe_resolve(manifest_path)
    if (
        resolved_manifest is None
        or not _within(resolved_manifest, plugin_root)
    ):
        return None
    manifest = _read_object(resolved_manifest)
    if manifest is None:
        return None
    key = str(manifest.get("key") or "").strip()
    version = _valid_version(manifest.get("version"))
    if not key or not version:
        return None
    try:
        digest = hashlib.sha256(base64.b64decode(key, validate=True)).digest()[:16]
    except (ValueError, TypeError):
        return None
    extension_id = "".join(
        chr(ord("a") + int(nibble, 16))
        for nibble in digest.hex()
    )
    return extension_id, version


def _fallback_root_for_home(configured_home: Path) -> Path:
    return (
        configured_home.parent.parent
        if configured_home.parent.name == "profiles"
        else configured_home
    )


def _canonical_root_for_home(configured_home: Path) -> Path:
    configured = _safe_resolve(configured_home)
    if configured is None:
        raise SystemExit(f"Hermes home is unreadable: {configured_home}")
    fallback = _fallback_root_for_home(configured)
    try:
        from hermes_constants import get_default_hermes_root

        canonical = _safe_resolve(Path(get_default_hermes_root()))
    except Exception:
        canonical = None
    if canonical is not None and (
        configured == canonical
        or (
            configured.parent.name == "profiles"
            and configured.parent.parent == canonical
        )
    ):
        return canonical
    return fallback


def _registered_extension_identity(hermes_root: Path) -> tuple[str, str] | None:
    receipt_path = (
        hermes_root / "record" / "chrome-provider" / "install-receipt.json"
    )
    if (
        receipt_path.is_symlink()
        or _safe_resolve(receipt_path) != receipt_path
    ):
        return None
    receipt = _read_object(receipt_path)
    if (
        receipt is None
        or receipt.get("schemaVersion") not in (2, 3)
        or receipt.get("updateUrl") != _UPDATE_URL
    ):
        return None
    extension_id = str(receipt.get("extensionId") or "").strip()
    version = _valid_version(receipt.get("extensionVersion"))
    if not _EXTENSION_ID_RE.fullmatch(extension_id) or not version:
        return None
    return extension_id, version


def _active_profile_home(hermes_root: Path) -> Path | None:
    active_path = hermes_root / "record" / "chrome-capture-active.json"
    if active_path.is_symlink() or _safe_resolve(active_path) != active_path:
        return None
    value = _read_object(active_path)
    if value is None or value.get("active") is not True:
        return None
    raw = str(value.get("profile_home") or "").strip()
    profile_home = _safe_resolve(Path(raw)) if raw else None
    if profile_home == hermes_root:
        return profile_home
    if (
        profile_home is not None
        and profile_home.parent.parent == hermes_root
        and profile_home.parent.name == "profiles"
        and _PROFILE_NAME_RE.fullmatch(profile_home.name)
    ):
        return profile_home
    return None


def _candidate_native_host(
    *,
    hermes_root: Path,
    profile_name: str,
    profile_home: Path,
    extension_identity: tuple[str, str],
) -> tuple[Path, Path] | None:
    try:
        resolved_home = _safe_resolve(profile_home)
        if resolved_home is None:
            return None
        if profile_name == "default":
            if resolved_home != hermes_root:
                return None
        elif (
            profile_home.is_symlink()
            or resolved_home.parent.parent != hermes_root
            or resolved_home.parent.name != "profiles"
            or resolved_home.name != profile_name
        ):
            return None

        receipt_path = resolved_home / "record" / "installation.json"
        if (
            receipt_path.is_symlink()
            or _safe_resolve(receipt_path) != receipt_path
        ):
            return None
        receipt = _read_object(receipt_path)
        if (
            receipt is None
            or receipt.get("schemaVersion") not in (2, 3)
            or receipt.get("release") is not True
            or receipt.get("platform") != "darwin"
            or receipt.get("arch") != "arm64"
            or receipt.get("profile") != profile_name
            or receipt.get("chromeExtensionId") != extension_identity[0]
            or receipt.get("chromeExtensionVersion") != extension_identity[1]
        ):
            return None

        receipt_home = _safe_resolve(
            Path(str(receipt.get("hermesHome") or ""))
        )
        expected_plugin_path = (
            resolved_home / "plugins" / _PLUGIN_NAME
        )
        if expected_plugin_path.is_symlink():
            return None
        plugin_root = _safe_resolve(expected_plugin_path)
        receipt_plugin = _safe_resolve(
            Path(str(receipt.get("pythonPlugin") or ""))
        )
        if (
            receipt_home != resolved_home
            or plugin_root is None
            or receipt_plugin != plugin_root
            or not plugin_root.is_dir()
            or not _within(plugin_root, resolved_home)
        ):
            return None

        native_host = _safe_resolve(
            plugin_root / "chrome_capture" / "native_host.py"
        )
        if (
            native_host is None
            or not native_host.is_file()
            or not _within(native_host, plugin_root)
            or _extension_identity(plugin_root) != extension_identity
        ):
            return None
        return resolved_home, native_host
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def select_native_host(hermes_root: Path) -> Path:
    """Return the receipt-authorized host for the active Desktop recording."""

    resolved_root = _safe_resolve(hermes_root)
    if resolved_root is None:
        raise SystemExit(f"Hermes root is unreadable: {hermes_root}")
    extension_identity = _registered_extension_identity(resolved_root)
    if extension_identity is None:
        raise SystemExit(
            "Hermes Chrome provider has no valid machine-wide registration receipt"
        )

    active_home = _active_profile_home(resolved_root)
    if active_home is None:
        raise SystemExit("Hermes Chrome provider has no active Desktop recording")
    profile_name = "default" if active_home == resolved_root else active_home.name
    candidate = _candidate_native_host(
        hermes_root=resolved_root,
        profile_name=profile_name,
        profile_home=active_home,
        extension_identity=extension_identity,
    )
    if candidate is None:
        extension_id, version = extension_identity
        raise SystemExit(
            "The active Desktop recording has no compatible receipt-authorized "
            f"{_PLUGIN_NAME} native host for {extension_id} {version}"
        )
    return candidate[1]


def main() -> int:
    configured_home = Path(
        os.environ.get("HERMES_HOME") or Path.home() / ".hermes"
    ).expanduser()
    native_host = select_native_host(_canonical_root_for_home(configured_home))
    os.execv(sys.executable, [sys.executable, str(native_host)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
