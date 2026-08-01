#!/usr/bin/env python3
"""Install the Chrome Native Messaging host manifest for Hermes Capture."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


HOST_NAME = "com.hermes.chrome_capture"
CHROME_WEB_STORE_UPDATE_URL = "https://clients2.google.com/service/update2/crx"
_EXTENSION_ID_RE = re.compile(r"^[a-p]{32}$")
_VERSION_RE = re.compile(r"^\d+(?:\.\d+){0,3}$")


def chrome_host_dir() -> Path:
    if os.sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome" / "NativeMessagingHosts"
    if os.sys.platform == "win32":
        raise SystemExit("Windows native host registration needs registry setup and is not supported by this helper yet.")
    return Path.home() / ".config" / "google-chrome" / "NativeMessagingHosts"


def chrome_external_extension_dir() -> Path:
    if os.sys.platform != "darwin":
        raise SystemExit("Customer Chrome registration is currently supported on macOS only.")
    return Path.home() / "Library" / "Application Support" / "Google" / "Chrome" / "External Extensions"


def _manifest_object(manifest_path: Path) -> dict:
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise SystemExit(
            f"Chrome extension manifest is unreadable: {manifest_path}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise SystemExit(f"Chrome extension manifest is not an object: {manifest_path}")
    return value


def extension_id_from_manifest(manifest_path: Path) -> str:
    manifest = _manifest_object(manifest_path)
    key = str(manifest.get("key") or "").strip()
    if not key:
        raise SystemExit(f"Chrome extension manifest has no stable public key: {manifest_path}")
    try:
        digest = hashlib.sha256(base64.b64decode(key, validate=True)).digest()[:16]
    except Exception as exc:
        raise SystemExit(f"Chrome extension manifest key is invalid: {exc}") from exc
    return "".join(chr(ord("a") + int(nibble, 16)) for nibble in digest.hex())


def extension_version_from_manifest(manifest_path: Path) -> str:
    manifest = _manifest_object(manifest_path)
    version = str(manifest.get("version") or "").strip()
    if (
        len(version) > 32
        or not _VERSION_RE.fullmatch(version)
        or any(len(part) > 5 or int(part) > 65535 for part in version.split("."))
    ):
        raise SystemExit(f"Chrome extension manifest has an invalid version: {manifest_path}")
    return version


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _atomic_write_bytes(path: Path, value: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp-",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
        temporary.chmod(mode)
        os.replace(temporary, path)
        path.chmod(mode)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _write_json(path: Path, value: dict, *, mode: int = 0o600) -> None:
    _atomic_write_bytes(
        path,
        (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        mode=mode,
    )


def _fallback_root_for_home(profile_home: Path) -> Path:
    return (
        profile_home.parent.parent
        if profile_home.parent.name == "profiles"
        else profile_home
    )


def _canonical_root_for_home(profile_home: Path) -> Path:
    profile_home = profile_home.expanduser().resolve()
    fallback = _fallback_root_for_home(profile_home)
    try:
        from hermes_constants import get_default_hermes_root

        canonical = Path(get_default_hermes_root()).expanduser().resolve()
    except Exception:
        return fallback
    if (
        profile_home == canonical
        or (
            profile_home.parent.name == "profiles"
            and profile_home.parent.parent == canonical
        )
    ):
        return canonical
    return fallback


def refresh_customer_bootstrap(
    *,
    extension_id: str,
    extension_version: str,
    python: Path,
    plugin_root: Path,
    native_host: Path | None,
    hermes_home: Path,
    install_root: Path,
) -> dict:
    """Refresh only the stable machine-wide launcher; do not register Chrome."""

    if not _EXTENSION_ID_RE.fullmatch(extension_id):
        raise SystemExit("Chrome extension id must be 32 characters in the range a-p.")

    # Preserve the Hermes-managed venv path instead of resolving through to
    # its uv/base interpreter. Hermes can replace that venv in place during an
    # update without invalidating Chrome's launcher.
    python = Path(os.path.abspath(os.path.expanduser(str(python))))
    plugin_root = plugin_root.expanduser().resolve()
    profile_home = hermes_home.expanduser().resolve()
    hermes_root = _canonical_root_for_home(profile_home)
    install_root = install_root.expanduser().resolve()
    native_host = (
        native_host.expanduser().resolve()
        if native_host is not None
        else (plugin_root / "chrome_capture" / "native_host.py").resolve()
    )
    launcher_source_path = (
        plugin_root / "chrome_capture" / "native_host_launcher.py"
    ).resolve()

    if not python.is_file():
        raise SystemExit(f"Hermes Python interpreter is missing: {python}")
    manifest_path = plugin_root / "plugin.yaml"
    try:
        plugin_manifest_lines = manifest_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise SystemExit(
            f"Standalone Record plugin is invalid: {plugin_root}: {exc}"
        ) from exc
    if not any(
        re.fullmatch(r"""name:\s*['"]?record['"]?""", line.strip())
        for line in plugin_manifest_lines
    ):
        raise SystemExit(f"Standalone Record plugin is invalid: {plugin_root}")
    plugin_extension_manifest = (
        plugin_root / "chrome_capture" / "extension" / "manifest.json"
    )
    if (
        extension_id_from_manifest(plugin_extension_manifest) != extension_id
        or extension_version_from_manifest(plugin_extension_manifest)
        != extension_version
    ):
        raise SystemExit(
            "Installed plugin Chrome identity does not match the reviewed package."
        )
    if not native_host.is_file():
        raise SystemExit(
            f"Hermes Chrome native host is missing from the installed plugin: {native_host}"
        )
    if not launcher_source_path.is_file():
        raise SystemExit(
            "Hermes Chrome native-host launcher is missing from the installed plugin: "
            f"{launcher_source_path}"
        )
    if (
        not _within(native_host, plugin_root)
        or not _within(launcher_source_path, plugin_root)
    ):
        raise SystemExit(
            "Chrome native-host files must live inside the installed Record plugin."
        )

    install_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    launcher = install_root / HOST_NAME
    native_host_bootstrap = install_root / "native_host_launcher.py"
    _atomic_write_bytes(
        native_host_bootstrap,
        launcher_source_path.read_bytes(),
        mode=0o700,
    )
    shell_source = (
        "#!/bin/sh\n"
        f"export HERMES_HOME={shlex.quote(str(hermes_root))}\n"
        f"exec {shlex.quote(str(python))} {shlex.quote(str(native_host_bootstrap))}\n"
    )
    _atomic_write_bytes(launcher, shell_source.encode("utf-8"), mode=0o700)
    return {
        "pluginRoot": str(plugin_root),
        "hermesHome": str(hermes_root),
        "installedFromProfileHome": str(profile_home),
        "registeredPluginNativeHost": str(native_host),
        "python": str(python),
        "launcher": str(launcher),
        "nativeHostBootstrap": str(native_host_bootstrap),
    }


def install_customer_host(
    *,
    extension_id: str,
    extension_version: str,
    python: Path,
    plugin_root: Path,
    native_host: Path | None,
    hermes_home: Path,
    install_root: Path,
    host_dir: Path,
    external_dir: Path,
) -> dict:
    """Register the Web Store extension and a durable native-host launcher.

    Stable Chrome on macOS refuses local CRX and unpacked installs. The
    customer path therefore registers the reviewed Chrome Web Store identity,
    while the native host runs from the installed standalone plugin. Hermes'
    active Python is only the interpreter; no Hermes runtime is copied.
    """

    bootstrap = refresh_customer_bootstrap(
        extension_id=extension_id,
        extension_version=extension_version,
        python=python,
        plugin_root=plugin_root,
        native_host=native_host,
        hermes_home=hermes_home,
        install_root=install_root,
    )
    install_root = Path(bootstrap["nativeHostBootstrap"]).parent
    launcher = Path(bootstrap["launcher"])

    native_manifest_path = host_dir / f"{HOST_NAME}.json"
    external_manifest_path = external_dir / f"{extension_id}.json"
    _write_json(
        native_manifest_path,
        {
            "name": HOST_NAME,
            "description": "Hermes adaptive Chrome provider native host",
            "path": str(launcher),
            "type": "stdio",
            "allowed_origins": [f"chrome-extension://{extension_id}/"],
        },
    )
    _write_json(external_manifest_path, {"external_update_url": CHROME_WEB_STORE_UPDATE_URL})

    receipt = {
        "schemaVersion": 3,
        "extensionId": extension_id,
        "extensionVersion": extension_version,
        "installedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        **bootstrap,
        "nativeHostManifest": str(native_manifest_path),
        "externalExtensionManifest": str(external_manifest_path),
        "updateUrl": CHROME_WEB_STORE_UPDATE_URL,
    }
    _write_json(install_root / "install-receipt.json", receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description="Install Hermes Chrome provider native host manifest.")
    parser.add_argument("--extension-id", default="", help="Installed Chrome extension id.")
    parser.add_argument("--host-path", default="", help="Override native host executable path.")
    parser.add_argument("--out-dir", default="", help="Override NativeMessagingHosts directory.")
    parser.add_argument(
        "--customer-install",
        action="store_true",
        help="Register the Chrome Web Store extension and durable standalone-plugin host on macOS.",
    )
    parser.add_argument(
        "--refresh-bootstrap",
        action="store_true",
        help="Refresh only the machine-wide launcher without changing Chrome registration.",
    )
    parser.add_argument("--python", default=sys.executable, help="Active Hermes Python interpreter.")
    parser.add_argument("--plugin-root", default="", help="Installed standalone Record plugin root.")
    parser.add_argument("--native-host", default="", help="Installed plugin native_host.py path.")
    parser.add_argument(
        "--hermes-home",
        default="",
        help="Selected Hermes profile home; the machine-wide launcher uses its Hermes root.",
    )
    parser.add_argument("--install-root", default="", help="Durable native-host launcher directory.")
    parser.add_argument("--external-dir", default="", help="Override Chrome External Extensions directory.")
    args = parser.parse_args()

    extension_manifest = Path(__file__).with_name("extension") / "manifest.json"
    expected_extension_id = extension_id_from_manifest(extension_manifest)
    expected_extension_version = extension_version_from_manifest(extension_manifest)
    extension_id = args.extension_id.strip() or expected_extension_id
    if extension_id != expected_extension_id:
        raise SystemExit(
            "Chrome extension id does not match the reviewed manifest key "
            f"({extension_id} != {expected_extension_id})."
        )

    if args.customer_install and args.refresh_bootstrap:
        raise SystemExit("--customer-install and --refresh-bootstrap are mutually exclusive.")

    if args.customer_install or args.refresh_bootstrap:
        plugin_root = (
            Path(args.plugin_root).expanduser()
            if args.plugin_root
            else Path(__file__).resolve().parents[1]
        )
        hermes_home = (
            Path(args.hermes_home).expanduser()
            if args.hermes_home
            else Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()
        )
        hermes_root = _canonical_root_for_home(hermes_home)
        install_root = (
            Path(args.install_root).expanduser()
            if args.install_root
            else hermes_root / "record" / "chrome-provider"
        )
        native_host = Path(args.native_host) if args.native_host else None
        common = {
            "extension_id": extension_id,
            "extension_version": expected_extension_version,
            "python": Path(args.python),
            "plugin_root": plugin_root,
            "native_host": native_host,
            "hermes_home": hermes_home,
            "install_root": install_root,
        }
        if args.refresh_bootstrap:
            receipt = refresh_customer_bootstrap(**common)
        else:
            receipt = install_customer_host(
                **common,
                host_dir=Path(args.out_dir).expanduser() if args.out_dir else chrome_host_dir(),
                external_dir=(
                    Path(args.external_dir).expanduser()
                    if args.external_dir
                    else chrome_external_extension_dir()
                ),
            )
        print(json.dumps(receipt, sort_keys=True))
        return 0

    host_path = Path(args.host_path).expanduser().resolve() if args.host_path else Path(__file__).with_name("native_host.py").resolve()
    out_dir = Path(args.out_dir).expanduser().resolve() if args.out_dir else chrome_host_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": HOST_NAME,
        "description": "Hermes adaptive Chrome provider native host",
        "path": str(host_path),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{extension_id}/"],
    }
    out_path = out_dir / f"{HOST_NAME}.json"
    _write_json(out_path, manifest)
    print(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
