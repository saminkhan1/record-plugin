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
from datetime import datetime, timezone
from pathlib import Path


HOST_NAME = "com.hermes.chrome_capture"
CHROME_WEB_STORE_UPDATE_URL = "https://clients2.google.com/service/update2/crx"
_EXTENSION_ID_RE = re.compile(r"^[a-p]{32}$")


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


def extension_id_from_manifest(manifest_path: Path) -> str:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    key = str(manifest.get("key") or "").strip()
    if not key:
        raise SystemExit(f"Chrome extension manifest has no stable public key: {manifest_path}")
    try:
        digest = hashlib.sha256(base64.b64decode(key, validate=True)).digest()[:16]
    except Exception as exc:
        raise SystemExit(f"Chrome extension manifest key is invalid: {exc}") from exc
    return "".join(chr(ord("a") + int(nibble, 16)) for nibble in digest.hex())


def extension_version_from_manifest(manifest_path: Path) -> str:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    version = str(manifest.get("version") or "").strip()
    if not re.fullmatch(r"\d+(?:\.\d+){0,3}", version):
        raise SystemExit(f"Chrome extension manifest has an invalid version: {manifest_path}")
    return version


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def validate_distribution(
    path: Path,
    *,
    extension_id: str,
    extension_version: str,
    native_host: Path,
) -> None:
    try:
        distribution = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Hermes Chrome provider distribution metadata is unreadable: {exc}") from exc
    relative_host = str(distribution.get("nativeHostRelativePath") or "")
    if (
        distribution.get("schemaVersion") != 2
        or distribution.get("extensionId") != extension_id
        or distribution.get("extensionVersion") != extension_version
        or distribution.get("updateUrl") != CHROME_WEB_STORE_UPDATE_URL
        or relative_host != "chrome_capture/native_host.py"
        or (path.parent / relative_host).resolve() != native_host.resolve()
    ):
        raise SystemExit("Hermes Chrome provider distribution metadata does not match the packaged payload.")


def install_customer_host(
    *,
    extension_id: str,
    python: Path,
    runtime_root: Path,
    native_host: Path | None,
    hermes_home: Path,
    install_root: Path,
    host_dir: Path,
    external_dir: Path,
) -> dict:
    """Register the Web Store provider and a durable native-host launcher.

    Stable Chrome on macOS refuses local CRX and unpacked installs. The
    customer path therefore registers the reviewed Chrome Web Store identity,
    while the native host runs from the installed Hermes runtime rather than a
    developer checkout or a transient build directory.
    """

    if not _EXTENSION_ID_RE.fullmatch(extension_id):
        raise SystemExit("Chrome extension id must be 32 characters in the range a-p.")

    python = python.expanduser().resolve()
    runtime_root = runtime_root.expanduser().resolve()
    hermes_home = hermes_home.expanduser().resolve()
    install_root = install_root.expanduser().resolve()
    native_host = (
        native_host.expanduser().resolve()
        if native_host is not None
        else runtime_root / "chrome_capture" / "native_host.py"
    )
    if not python.is_file():
        raise SystemExit(f"Hermes runtime Python is missing: {python}")
    if not native_host.is_file():
        raise SystemExit(f"Hermes Chrome native host is missing from the installed runtime: {native_host}")

    install_root.mkdir(parents=True, exist_ok=True)
    launcher = install_root / HOST_NAME
    launcher_source = (
        "#!/bin/sh\n"
        f"export HERMES_HOME={shlex.quote(str(hermes_home))}\n"
        f"exec {shlex.quote(str(python))} {shlex.quote(str(native_host))}\n"
    )
    temporary_launcher = launcher.with_name(f".{launcher.name}.tmp-{os.getpid()}")
    temporary_launcher.write_text(launcher_source, encoding="utf-8")
    temporary_launcher.chmod(0o700)
    temporary_launcher.replace(launcher)

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
        "schemaVersion": 1,
        "extensionId": extension_id,
        "installedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "runtimeRoot": str(runtime_root),
        "nativeHost": str(native_host),
        "python": str(python),
        "launcher": str(launcher),
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
        help="Register the Chrome Web Store extension and durable installed-runtime host on macOS.",
    )
    parser.add_argument("--python", default=sys.executable, help="Installed Hermes runtime Python.")
    parser.add_argument("--runtime-root", default="", help="Installed Hermes runtime root.")
    parser.add_argument("--native-host", default="", help="Bundled native_host.py path.")
    parser.add_argument("--distribution", default="", help="Bundled Chrome provider distribution.json path.")
    parser.add_argument("--hermes-home", default="", help="Hermes state root exported to the native host.")
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

    if args.customer_install:
        runtime_root = (
            Path(args.runtime_root).expanduser()
            if args.runtime_root
            else Path(__file__).resolve().parents[1]
        )
        hermes_home = Path(args.hermes_home).expanduser() if args.hermes_home else runtime_root.parent
        install_root = (
            Path(args.install_root).expanduser()
            if args.install_root
            else Path.home() / "Library" / "Application Support" / "Hermes" / "crunch-chrome-provider"
        )
        native_host = Path(args.native_host) if args.native_host else None
        if args.distribution:
            if native_host is None:
                raise SystemExit("--distribution requires --native-host.")
            validate_distribution(
                Path(args.distribution),
                extension_id=extension_id,
                extension_version=expected_extension_version,
                native_host=native_host,
            )
        receipt = install_customer_host(
            extension_id=extension_id,
            python=Path(args.python),
            runtime_root=runtime_root,
            native_host=native_host,
            hermes_home=hermes_home,
            install_root=install_root,
            host_dir=Path(args.out_dir).expanduser() if args.out_dir else chrome_host_dir(),
            external_dir=Path(args.external_dir).expanduser() if args.external_dir else chrome_external_extension_dir(),
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
    out_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
