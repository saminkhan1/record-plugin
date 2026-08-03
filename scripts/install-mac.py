#!/usr/bin/env python3
"""Install the signed Record macOS package with only Python stdlib.

The customer entry point is the signed Record Installer.app launcher.
That launcher resolves Hermes Desktop's managed Python and invokes this file.
This module deliberately does not import Hermes internals: it uses the managed
runtime only through ``python -m hermes_cli.main`` and the packaged Chrome-host
installer, keeping profile/config ownership in stock Hermes.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator


PLUGIN_NAME = "record"
PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
RECORDER_CLIENT_PATH = Path("hermes-recorder")
RECORDER_SERVICE_PATH = Path(
    "Hermes Recorder Service.app/Contents/MacOS/hermes-recorder-service"
)
RECORDER_SERVICE_IDENTIFIER = "com.hermes.recorder.service"
LAUNCH_SERVICES_REGISTER = Path(
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
    "LaunchServices.framework/Support/lsregister"
)
REQUIRED_CAPABILITIES = {
    "plugin_llm",
    "plugin_profile",
    "plugin_tool_dispatch",
    "builtin_tool_discovery",
    "plugin_hooks",
}
ARM64_CPU_TYPE = 0x0100000C


class InstallError(RuntimeError):
    pass


@dataclass
class PreparedDirectory:
    target: Path
    temporary: Path
    backup: Path | None = None


@dataclass(frozen=True)
class FileSnapshot:
    target: Path
    exists: bool
    data: bytes | None
    mode: int


@contextmanager
def _install_lock() -> Iterator[None]:
    lock_root = (
        Path.home()
        / "Library"
        / "Application Support"
        / "Hermes"
    )
    lock_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = lock_root / "record-install.lock"
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise InstallError(
            f"Unable to open the Record install lock: {error}"
        ) from error

    handle = os.fdopen(descriptor, "r+", encoding="utf-8")
    try:
        target_stat = os.fstat(handle.fileno())
        if not stat.S_ISREG(target_stat.st_mode):
            raise InstallError(
                f"Install lock is not a regular file: {lock_path}"
            )
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise InstallError(
                "Another Record installation is already running"
            ) from error
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        os.fsync(handle.fileno())
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _regular_files(root: Path, *, exclude_manifest: bool = False) -> list[Path]:
    result: list[Path] = []
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in directories:
            target = current_path / name
            if target.is_symlink():
                raise InstallError(
                    f"Package contains an unsupported filesystem entry: {target}"
                )
        for name in files:
            target = current_path / name
            if target.is_symlink() or not target.is_file():
                raise InstallError(
                    f"Package contains an unsupported filesystem entry: {target}"
                )
            relative = target.relative_to(root)
            if exclude_manifest and relative == Path("manifest.json"):
                continue
            result.append(relative)
    return sorted(result, key=lambda item: item.as_posix())


def _run(
    command: Path | str,
    arguments: list[str],
    *,
    env: dict[str, str] | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [str(command), *arguments],
        env=env,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if result.returncode != 0:
        detail = ""
        if capture:
            detail = (result.stderr or result.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise InstallError(
            f"{command} exited with status {result.returncode}{suffix}"
        )
    return result


def _recorder_build_manifest(
    root: Path, *, allow_legacy: bool = False
) -> dict[str, Any] | None:
    manifest = _read_json(root / "recorder-manifest.json")
    if not manifest:
        return None
    schema_version = manifest.get("schemaVersion")
    files = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
    client = files.get("client") if isinstance(files.get("client"), dict) else {}
    service = files.get("service") if isinstance(files.get("service"), dict) else {}
    identifiers = (
        manifest.get("identifiers")
        if isinstance(manifest.get("identifiers"), dict)
        else {}
    )
    if (
        schema_version not in {1, 2}
        or (not allow_legacy and schema_version != 2)
        or (
            schema_version == 2
            and manifest.get("product") != "record"
        )
        or manifest.get("platform") != "darwin"
        or manifest.get("arch") != "arm64"
        or not isinstance(manifest.get("teamId"), str)
        or not manifest.get("teamId")
        or identifiers.get("client") != "com.hermes.recorder.client"
        or identifiers.get("service") != RECORDER_SERVICE_IDENTIFIER
        or client.get("path") != RECORDER_CLIENT_PATH.as_posix()
        or service.get("path") != RECORDER_SERVICE_PATH.as_posix()
    ):
        return None
    for entry in (client, service):
        relative = entry.get("path")
        digest = entry.get("sha256")
        if not isinstance(relative, str) or not isinstance(digest, str):
            return None
        target = root / relative
        if not target.is_file() or _sha256(target) != digest:
            return None
    return manifest


def _recorder_install_receipt(root: Path) -> dict[str, Any] | None:
    try:
        root_stat = root.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(root_stat.st_mode) or root.is_symlink():
        return None
    receipt = _read_json(root / "installation.json")
    build_manifest = _recorder_build_manifest(root)
    if (
        not receipt
        or receipt.get("schemaVersion") != 1
        or receipt.get("product") != "record"
        or receipt.get("component") != "native-recorder"
        or receipt.get("teamId")
        != (build_manifest or {}).get("teamId")
        or receipt.get("recorderManifestSha256")
        != _sha256(root / "recorder-manifest.json")
    ):
        return None
    return receipt


def _recorder_matches_package(
    package_manifest: dict[str, Any], target: Path
) -> bool:
    if not _recorder_install_receipt(target):
        return False
    recorder_client = Path(str(package_manifest["recorderClient"]))
    relative_root = recorder_client.parent
    expected = sorted(
        (
            relative.relative_to(relative_root),
            digest,
        )
        for name, digest in package_manifest["files"].items()
        if (
            (relative := Path(name)) != relative_root
            and relative_root in relative.parents
        )
    )
    actual = [
        item
        for item in _regular_files(target)
        if item != Path("installation.json")
    ]
    if [item for item, _ in expected] != actual:
        return False
    return all(_sha256(target / relative) == digest for relative, digest in expected)


def _write_recorder_install_receipt(
    target: Path, package_manifest: dict[str, Any]
) -> dict[str, Any]:
    build_manifest = _recorder_build_manifest(target)
    if not build_manifest:
        raise InstallError(f"Packaged recorder metadata is invalid: {target}")
    identifiers = {
        name: build_manifest["identifiers"][name]
        for name in ("client", "service")
    }
    files = {
        name: build_manifest["files"][name]
        for name in ("client", "service")
    }
    receipt = {
        "schemaVersion": 1,
        "product": "record",
        "component": "native-recorder",
        "version": package_manifest["version"],
        "installedAt": _utc_now(),
        "teamId": build_manifest["teamId"],
        "signingIdentity": build_manifest.get("signingIdentity"),
        "identifiers": identifiers,
        "recorderManifestSha256": _sha256(
            target / "recorder-manifest.json"
        ),
        "files": files,
    }
    destination = target / "installation.json"
    destination.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    destination.chmod(0o600)
    return receipt


def _running_recorder_service_pids() -> list[int]:
    result = subprocess.run(
        [
            "/usr/bin/pgrep",
            "-f",
            "hermes-recorder-service([[:space:]]|$)",
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode == 1:
        return []
    if result.returncode != 0:
        raise InstallError(
            "Unable to inspect running recorder services: "
            + (result.stderr or result.stdout or "").strip()
        )
    return [
        value
        for token in result.stdout.split()
        if (value := int(token)) > 0
    ]


def _recorder_service_app(root: Path) -> Path:
    return root / "Hermes Recorder Service.app"


def _register_recorder(root: Path) -> None:
    _run(
        LAUNCH_SERVICES_REGISTER,
        ["-f", str(_recorder_service_app(root))],
    )


def _unregister_recorder(root: Path) -> None:
    app = _recorder_service_app(root)
    if app.exists():
        _run(LAUNCH_SERVICES_REGISTER, ["-u", str(app)])


def _unregister_other_recorder_registrations(
    canonical_root: Path,
    rollback_registrations: list[Path],
) -> None:
    canonical_app = _recorder_service_app(canonical_root).resolve()
    output = _run(
        LAUNCH_SERVICES_REGISTER,
        ["-dump"],
        capture=True,
    ).stdout
    registered: set[Path] = set()
    for block in output.split(
        "--------------------------------------------------------------------------------"
    ):
        if f"identifier:                 {RECORDER_SERVICE_IDENTIFIER}" not in block:
            continue
        match = re.search(
            r"^path:\s+(.+?)\s+\(0x[0-9a-f]+\)\s*$",
            block,
            flags=re.IGNORECASE | re.MULTILINE,
        )
        if match:
            registered.add(Path(match.group(1)).resolve())
    for app in sorted(registered):
        if app == canonical_app:
            continue
        # Record the path before invoking lsregister. If lsregister changes
        # state and then exits nonzero, rollback still re-registers this app.
        rollback_registrations.append(app)
        _run(LAUNCH_SERVICES_REGISTER, ["-u", str(app)])


def _require_arm64_macho(path: Path) -> None:
    header = path.read_bytes()[:8]
    if len(header) < 8:
        raise InstallError(f"Packaged executable has no Mach-O header: {path}")
    magic = header[:4]
    if magic == bytes.fromhex("cffaedfe"):
        cpu_type = int.from_bytes(header[4:8], "little")
    elif magic == bytes.fromhex("feedfacf"):
        cpu_type = int.from_bytes(header[4:8], "big")
    else:
        raise InstallError(
            f"Packaged executable is not an arm64-only Mach-O binary: {path}"
        )
    if cpu_type != ARM64_CPU_TYPE:
        raise InstallError(
            f"Packaged executable is not arm64-only: {path}"
        )


def _codesign_details(path: Path) -> str:
    result = _run(
        "/usr/bin/codesign",
        ["-d", "--verbose=4", str(path)],
        capture=True,
    )
    return f"{result.stdout or ''}\n{result.stderr or ''}"


def _verify_package(package_root: Path) -> dict[str, Any]:
    manifest_path = package_root / "manifest.json"
    manifest = _read_json(manifest_path)
    capabilities = (
        set(manifest.get("requiredHermesCapabilities") or [])
        if manifest
        else set()
    )
    files = manifest.get("files") if manifest else None
    if (
        not manifest
        or manifest.get("schemaVersion") != 3
        or manifest.get("name") != PLUGIN_NAME
        or manifest.get("platform") != "darwin"
        or manifest.get("arch") != "arm64"
        or manifest.get("release") is not True
        or not isinstance(manifest.get("teamId"), str)
        or not manifest.get("teamId")
        or not REQUIRED_CAPABILITIES.issubset(capabilities)
        or manifest.get("installer") != "scripts/install-mac.py"
        or not isinstance(manifest.get("chromeExtension"), str)
        or not manifest.get("chromeExtension")
        or not isinstance(manifest.get("chromeExtensionId"), str)
        or not manifest.get("chromeExtensionId")
        or not isinstance(manifest.get("chromeExtensionVersion"), str)
        or not manifest.get("chromeExtensionVersion")
        or not isinstance(files, dict)
    ):
        raise InstallError(
            f"Invalid Record package manifest: {manifest_path}"
        )

    actual_files = _regular_files(package_root, exclude_manifest=True)
    declared_files = sorted(
        (Path(name) for name in files),
        key=lambda item: item.as_posix(),
    )
    if actual_files != declared_files:
        raise InstallError(
            "Package file inventory does not match manifest.json"
        )
    for relative in actual_files:
        if _sha256(package_root / relative) != files[relative.as_posix()]:
            raise InstallError(f"Package checksum mismatch: {relative}")

    extension_manifest = _read_json(
        package_root / manifest["chromeExtension"] / "manifest.json"
    )
    if (
        not extension_manifest
        or extension_manifest.get("version")
        != manifest["chromeExtensionVersion"]
    ):
        raise InstallError(
            "Packaged Chrome extension version does not match manifest.json"
        )

    client = package_root / manifest["recorderClient"]
    service_app = _recorder_service_app(client.parent)
    service = client.parent / RECORDER_SERVICE_PATH
    if (
        manifest["recorderClient"] not in files
        or manifest["installer"] not in files
        or (_recorder_build_manifest(client.parent) or {}).get("version")
        != manifest.get("version")
    ):
        raise InstallError(
            "Package recorder or installer metadata is incomplete"
        )
    for target in (client, service):
        _require_arm64_macho(target)
    _run(
        "/usr/bin/codesign",
        ["--verify", "--strict", "--verbose=4", str(client)],
    )
    _run(
        "/usr/bin/codesign",
        ["--verify", "--deep", "--strict", "--verbose=4", str(service_app)],
    )
    for target in (client, service):
        details = _codesign_details(target)
        if (
            "flags=0x10000(runtime)" not in details
            or f"TeamIdentifier={manifest['teamId']}" not in details
            or not re.search(r"^Timestamp=.+$", details, flags=re.MULTILINE)
        ):
            raise InstallError(
                f"Recorder release signature is incomplete: {target}"
            )
    return manifest


def _require_customer_distribution(
    package_root: Path,
    manifest: dict[str, Any],
    installer_app: Path | None,
) -> None:
    if installer_app is None:
        raise InstallError(
            "Customer installation must run through the signed "
            "Record Installer.app"
        )
    installer_app = installer_app.resolve()
    resources = installer_app / "Contents" / "Resources"
    try:
        package_root.resolve().relative_to(resources.resolve())
    except ValueError as exc:
        raise InstallError(
            "The package payload does not belong to this Installer.app"
        ) from exc
    _run(
        "/usr/bin/codesign",
        ["--verify", "--deep", "--strict", "--verbose=4", str(installer_app)],
    )
    details = _codesign_details(installer_app)
    if (
        "flags=0x10000(runtime)" not in details
        or f"TeamIdentifier={manifest['teamId']}" not in details
        or not re.search(r"^Timestamp=.+$", details, flags=re.MULTILINE)
    ):
        raise InstallError(
            "Installer.app is not release-signed by the recorder package team"
        )
    _run(
        "/usr/sbin/spctl",
        ["--assess", "--type", "execute", "--verbose=4", str(installer_app)],
    )
    client = package_root / manifest["recorderClient"]
    service_app = _recorder_service_app(client.parent)
    # Gatekeeper assesses the distributable application bundles. The raw
    # recorder client is already verified as a hardened-runtime, timestamped,
    # team-matched Mach-O in _load_package_manifest.
    _run(
        "/usr/sbin/spctl",
        [
            "--assess",
            "--type",
            "execute",
            "--verbose=4",
            str(service_app),
        ],
    )


def _current_plugin_name(target: Path) -> str:
    try:
        text = (target / "plugin.yaml").read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(
        r"""^\s*name:\s*["']?([^"'#\s]+)["']?\s*$""",
        text,
        flags=re.MULTILINE,
    )
    return match.group(1) if match else ""


def _is_record_desktop_plugin(target: Path) -> bool:
    try:
        text = (target / "plugin.js").read_text(encoding="utf-8")
    except OSError:
        return False
    return "const ID = 'record'" in text and "export default" in text


def _directories_match(source: Path, target: Path) -> bool:
    try:
        target_stat = target.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(target_stat.st_mode) or target.is_symlink():
        return False
    source_files = _regular_files(source)
    target_files = _regular_files(target)
    return source_files == target_files and all(
        _sha256(source / relative) == _sha256(target / relative)
        for relative in source_files
    )


def _desktop_profile_preference() -> str | None:
    config = _read_json(
        Path.home()
        / "Library"
        / "Application Support"
        / "Hermes"
        / "active-profile.json"
    )
    raw = (config or {}).get("profile")
    value = raw.strip() if isinstance(raw, str) else ""
    return (
        value
        if value == "default" or PROFILE_RE.fullmatch(value)
        else None
    )


def _sticky_active_profile(hermes_root: Path) -> str:
    active_path = hermes_root / "active_profile"
    try:
        active_state = active_path.lstat()
    except FileNotFoundError:
        return "default"
    except OSError as error:
        raise InstallError(
            f"Unable to read Hermes active profile: {active_path}: {error}"
        ) from error
    if (
        not stat.S_ISREG(active_state.st_mode)
        or active_path.is_symlink()
        or active_state.st_size > 256
    ):
        raise InstallError(
            f"Hermes active profile is not a safe regular file: {active_path}"
        )
    try:
        value = active_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as error:
        raise InstallError(
            f"Unable to read Hermes active profile: {active_path}: {error}"
        ) from error
    if not value or value == "default":
        return "default"
    if not PROFILE_RE.fullmatch(value):
        raise InstallError(
            f"Hermes active profile is invalid: {value!r}"
        )
    return value


def _profile_home(hermes_root: Path, profile_name: str) -> Path:
    if profile_name == "default":
        return hermes_root
    profile_home = hermes_root / "profiles" / profile_name
    try:
        profile_state = profile_home.lstat()
    except FileNotFoundError as error:
        raise InstallError(
            f"Hermes profile does not exist: {profile_name}"
        ) from error
    if (
        not stat.S_ISDIR(profile_state.st_mode)
        or profile_home.is_symlink()
    ):
        raise InstallError(
            f"Hermes profile is not a safe directory: {profile_home}"
        )
    return profile_home


def _profile_for_home(hermes_home: Path, hermes_root: Path) -> str:
    if hermes_home == hermes_root:
        return "default"
    profiles_root = hermes_root / "profiles"
    try:
        relative = hermes_home.relative_to(profiles_root)
    except ValueError as exc:
        raise InstallError(
            "--hermes-home must target the default Hermes home or one "
            "standard named profile"
        ) from exc
    value = relative.as_posix()
    if len(relative.parts) != 1 or not PROFILE_RE.fullmatch(value):
        raise InstallError(
            "--hermes-home must target the default Hermes home or one "
            "standard named profile"
        )
    return value


def _hermes_root_for_home(hermes_home: Path) -> Path:
    if (
        hermes_home.parent.name == "profiles"
        and PROFILE_RE.fullmatch(hermes_home.name)
    ):
        return hermes_home.parent.parent
    return hermes_home


def _resolve_managed_python(explicit: str | None, hermes_root: Path) -> Path:
    candidates = (
        [Path(explicit).expanduser().resolve()]
        if explicit
        else [hermes_root / "hermes-agent" / "venv" / "bin" / "python"]
    )
    probe_env = {**os.environ, "HERMES_HOME": str(hermes_root)}
    probe_env.pop("PYTHONHOME", None)
    probe_env.pop("PYTHONPATH", None)
    probe_env.pop("VIRTUAL_ENV", None)
    for candidate in candidates:
        if not candidate.is_file():
            continue
        probe = subprocess.run(
            [
                str(candidate),
                "-c",
                "import hermes_constants; import hermes_cli.main; "
                "import hermes_cli.plugins",
            ],
            env=probe_env,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if probe.returncode == 0:
            return candidate
    if explicit:
        raise InstallError(
            f"The requested Python is not a usable Hermes runtime: {explicit}"
        )
    raise InstallError(
        "Hermes Desktop's managed runtime is not ready at "
        f"{candidates[0]}. Open Hermes Desktop and finish setup, then run "
        "Record Installer again."
    )


def _prepare_directory(source: Path, target: Path) -> PreparedDirectory:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.parent / (
        f".{target.name}.install-{os.getpid()}-{time.time_ns()}"
    )
    if temporary.exists():
        shutil.rmtree(temporary)
    # ditto preserves app-bundle metadata and notarization tickets while also
    # handling ordinary plugin directories.
    _run("/usr/bin/ditto", [str(source), str(temporary)])
    return PreparedDirectory(target=target, temporary=temporary)


def _commit_directory(prepared: PreparedDirectory) -> None:
    if prepared.target.exists() or prepared.target.is_symlink():
        prepared.backup = prepared.target.parent / (
            f".{prepared.target.name}.backup-{os.getpid()}-{time.time_ns()}"
        )
        prepared.target.rename(prepared.backup)
    try:
        prepared.temporary.rename(prepared.target)
    except BaseException:
        if prepared.backup:
            prepared.backup.rename(prepared.target)
        raise


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _rollback_directory(prepared: PreparedDirectory) -> None:
    _remove_path(prepared.temporary)
    if prepared.backup is None:
        _remove_path(prepared.target)
        return
    _remove_path(prepared.target)
    prepared.backup.rename(prepared.target)
    prepared.backup = None


def _finish_directory(prepared: PreparedDirectory) -> None:
    _remove_path(prepared.temporary)
    if prepared.backup:
        _remove_path(prepared.backup)
        prepared.backup = None


def _snapshot_file(target: Path) -> FileSnapshot:
    try:
        target_stat = target.lstat()
    except FileNotFoundError:
        return FileSnapshot(target=target, exists=False, data=None, mode=0)
    if not stat.S_ISREG(target_stat.st_mode) or target.is_symlink():
        raise InstallError(f"Refusing to replace non-file state: {target}")
    return FileSnapshot(
        target=target,
        exists=True,
        data=target.read_bytes(),
        mode=stat.S_IMODE(target_stat.st_mode),
    )


def _restore_file(snapshot: FileSnapshot) -> None:
    if not snapshot.exists:
        snapshot.target.unlink(missing_ok=True)
        return
    snapshot.target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{snapshot.target.name}.restore-",
        dir=snapshot.target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(snapshot.data or b"")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(snapshot.mode)
        os.replace(temporary, snapshot.target)
    finally:
        temporary.unlink(missing_ok=True)


def _parse_arguments(argv: list[str]) -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    packaged_payload = (
        repo_root
        / "dist"
        / "macos-arm64"
        / "Record Installer.app"
        / "Contents"
        / "Resources"
        / "payload"
    )
    default_package_root = (
        repo_root
        if (repo_root / "manifest.json").is_file()
        else packaged_payload
    )
    parser = argparse.ArgumentParser(
        description=(
            "Install Record into Hermes' existing default home or an "
            "already-created Hermes profile. Record never creates profiles."
        )
    )
    parser.add_argument("--package", default=str(default_package_root))
    parser.add_argument("--hermes-home")
    parser.add_argument("--python")
    parser.add_argument("--installer-app")
    parser.add_argument("--chrome", choices=("store", "none"), default="store")
    parser.add_argument("--no-enable", action="store_true")
    return parser.parse_args(argv)


def _install_transaction(argv: list[str]) -> dict[str, Any]:
    arguments = _parse_arguments(argv)
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise InstallError(
            "Record installs on Apple-silicon macOS only"
        )

    package_root = Path(arguments.package).expanduser().resolve()
    default_hermes_root = Path.home() / ".hermes"
    desktop_profile = _desktop_profile_preference()
    desktop_home = _profile_home(
        default_hermes_root,
        desktop_profile or "default",
    )
    requested_home = arguments.hermes_home or os.environ.get("HERMES_HOME")
    if requested_home:
        hermes_home = Path(requested_home).expanduser().resolve()
    else:
        hermes_home = _profile_home(
            default_hermes_root,
            desktop_profile
            or _sticky_active_profile(default_hermes_root),
        )
    hermes_root = _hermes_root_for_home(hermes_home)
    profile_name = _profile_for_home(hermes_home, hermes_root)
    installer_app = (
        Path(arguments.installer_app).expanduser().resolve()
        if arguments.installer_app
        else None
    )
    if (
        installer_app is None
        and package_root.name == "payload"
        and package_root.parent.name == "Resources"
        and package_root.parent.parent.name == "Contents"
        and package_root.parent.parent.parent.suffix == ".app"
    ):
        installer_app = package_root.parent.parent.parent

    manifest = _verify_package(package_root)
    if (
        arguments.chrome == "store"
        and manifest.get("chromeStoreStatus") != "published"
    ):
        raise InstallError(
            "Chrome Web Store installation is not release-approved; publish "
            "the assigned extension before customer install"
        )
    _require_customer_distribution(
        package_root,
        manifest,
        installer_app,
    )

    python_source = package_root / manifest["pythonPlugin"]
    desktop_source = package_root / manifest["desktopPlugin"]
    recorder_source = (
        package_root / manifest["recorderClient"]
    ).parent
    python_target = hermes_home / "plugins" / PLUGIN_NAME
    desktop_target = desktop_home / "desktop-plugins" / PLUGIN_NAME
    recorder_target = hermes_root / "record" / "native-recorder"
    legacy_profile_recorder = (
        python_target / "native" / "hermes-recorder"
    )
    receipt_path = hermes_home / "record" / "installation.json"

    recorder_target_state = None
    try:
        recorder_target_state = recorder_target.lstat()
    except FileNotFoundError:
        pass
    if recorder_target_state and (
        not stat.S_ISDIR(recorder_target_state.st_mode)
        or recorder_target.is_symlink()
    ):
        raise InstallError(
            f"Refusing to replace non-directory recorder state: {recorder_target}"
        )
    if recorder_target_state and not _recorder_install_receipt(recorder_target):
        raise InstallError(
            f"Refusing to replace an unrecognized recorder install: {recorder_target}"
    )
    recorder_already_current = bool(
        recorder_target_state
        and _recorder_matches_package(manifest, recorder_target)
    )
    install_recorder = not recorder_already_current
    if install_recorder:
        pids = _running_recorder_service_pids()
        if pids:
            raise InstallError(
                "Recorder service is running (PID "
                + ", ".join(str(pid) for pid in pids)
                + "); stop recording and quit it before install"
            )

    desktop_already_current = (
        _is_record_desktop_plugin(desktop_target)
        and _directories_match(desktop_source, desktop_target)
    )
    install_desktop = not desktop_already_current
    for target, owned in (
        (python_target, _current_plugin_name(python_target) == PLUGIN_NAME),
        *(
            ((desktop_target, _is_record_desktop_plugin(desktop_target)),)
            if install_desktop
            else ()
        ),
    ):
        try:
            target_state = target.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(target_state.st_mode) or target.is_symlink():
            raise InstallError(
                f"Refusing to replace non-directory install state: {target}"
            )
        if not owned:
            raise InstallError(
                f"Refusing to replace an unrecognized install: {target}"
            )

    hermes_python = _resolve_managed_python(arguments.python, hermes_root)
    runtime_env = {**os.environ, "HERMES_HOME": str(hermes_root)}
    runtime_env.pop("PYTHONHOME", None)
    runtime_env.pop("PYTHONPATH", None)
    runtime_env.pop("VIRTUAL_ENV", None)
    profile_env = {**runtime_env, "HERMES_HOME": str(hermes_home)}
    capability_source = "; ".join(
        (
            "import json",
            "from cron import get_job, list_jobs",
            "from hermes_cli.plugins import "
            "PluginContext, PluginManager, PluginManifest, invoke_hook",
            "from tools.registry import discover_builtin_tools",
            "assert all(hasattr(PluginContext, item) for item in "
            "('llm', 'profile_name', 'dispatch_tool'))",
            "assert all(callable(item) for item in "
            "(get_job, list_jobs, PluginContext.dispatch_tool, "
            "discover_builtin_tools, invoke_hook))",
            "discover_builtin_tools()",
            "probe = PluginContext("
            "PluginManifest(name='record-capability-probe'), "
            "PluginManager())",
            "assert json.loads(probe.dispatch_tool("
            "'cronjob', {'action': 'list'})).get('success') is True",
        )
    )
    capability_probe = subprocess.run(
        [str(hermes_python), "-c", capability_source],
        env=profile_env,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if capability_probe.returncode != 0:
        raise InstallError(
            "This Hermes build does not provide the standard plugin LLM, "
            "profile, lifecycle, and Cron capabilities required by Record"
        )

    chrome_install_root = hermes_root / "record" / "chrome-provider"
    chrome_state_snapshots = [
        _snapshot_file(chrome_install_root / "com.hermes.chrome_capture"),
        _snapshot_file(chrome_install_root / "native_host_launcher.py"),
    ]
    if arguments.chrome == "store":
        chrome_state_snapshots.extend(
            (
                _snapshot_file(
                    Path.home()
                    / "Library"
                    / "Application Support"
                    / "Google"
                    / "Chrome"
                    / "NativeMessagingHosts"
                    / "com.hermes.chrome_capture.json"
                ),
                _snapshot_file(
                    Path.home()
                    / "Library"
                    / "Application Support"
                    / "Google"
                    / "Chrome"
                    / "External Extensions"
                    / f"{manifest['chromeExtensionId']}.json"
                ),
                _snapshot_file(chrome_install_root / "install-receipt.json"),
            )
        )
    installation_receipt_snapshot = _snapshot_file(receipt_path)
    plugin_enable_state_snapshot = (
        None
        if arguments.no_enable
        else _snapshot_file(hermes_home / "config.yaml")
    )

    prepared: list[PreparedDirectory] = []
    committed: list[PreparedDirectory] = []
    recorder_prepared: PreparedDirectory | None = None
    legacy_recorder_owned = _recorder_build_manifest(
        legacy_profile_recorder,
        allow_legacy=True,
    )
    legacy_recorder_unregistered = False
    removed_recorder_registrations: list[Path] = []
    installation_receipt: dict[str, Any] | None = None

    try:
        if install_recorder:
            recorder_prepared = _prepare_directory(
                recorder_source,
                recorder_target,
            )
            _write_recorder_install_receipt(
                recorder_prepared.temporary,
                manifest,
            )
            prepared.append(recorder_prepared)
        prepared.append(_prepare_directory(python_source, python_target))
        if install_desktop:
            prepared.append(
                _prepare_directory(desktop_source, desktop_target)
            )
        if legacy_recorder_owned:
            # Treat the registration as rollback-owned before calling
            # lsregister because it may update state before returning an error.
            legacy_recorder_unregistered = True
            _unregister_recorder(legacy_profile_recorder)
        for item in prepared:
            _commit_directory(item)
            committed.append(item)

        _register_recorder(recorder_target)
        _unregister_other_recorder_registrations(
            recorder_target,
            removed_recorder_registrations,
        )
        chrome_arguments = [
            str(
                python_target
                / "chrome_capture"
                / "install_native_host.py"
            ),
            (
                "--customer-install"
                if arguments.chrome == "store"
                else "--refresh-bootstrap"
            ),
            "--python",
            str(hermes_python),
            "--plugin-root",
            str(python_target),
            "--native-host",
            str(python_target / "chrome_capture" / "native_host.py"),
            "--hermes-home",
            str(hermes_home),
            "--install-root",
            str(chrome_install_root),
        ]
        _run(hermes_python, chrome_arguments, env=runtime_env)

        if not arguments.no_enable:
            _run(
                hermes_python,
                [
                    "-m",
                    "hermes_cli.main",
                    "--profile",
                    profile_name,
                    "plugins",
                    "enable",
                    PLUGIN_NAME,
                    "--no-allow-tool-override",
                ],
                env=runtime_env,
            )

        installation_receipt = {
            "schemaVersion": 3,
            "installedAt": _utc_now(),
            "version": manifest["version"],
            "platform": manifest["platform"],
            "arch": manifest["arch"],
            "release": manifest["release"],
            "teamId": manifest["teamId"],
            "profile": profile_name,
            "hermesHome": str(hermes_home),
            "pythonPlugin": str(python_target),
            "desktopPlugin": str(desktop_target),
            "recorderClient": str(
                recorder_target / RECORDER_CLIENT_PATH
            ),
            "recorderClientSha256": _sha256(
                recorder_target / RECORDER_CLIENT_PATH
            ),
            "recorderManifestSha256": _sha256(
                recorder_target / "recorder-manifest.json"
            ),
            "installerApp": str(installer_app) if installer_app else None,
            "chromeMode": arguments.chrome,
            "chromeExtensionId": manifest["chromeExtensionId"],
            "chromeExtensionVersion": manifest["chromeExtensionVersion"],
            "chromeStoreStatus": manifest["chromeStoreStatus"],
            "pluginEnableAction": (
                "skipped" if arguments.no_enable else "enabled"
            ),
        }
        receipt_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{receipt_path.name}.",
            dir=receipt_path.parent,
        )
        temporary_receipt = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    installation_receipt,
                    handle,
                    indent=2,
                    sort_keys=True,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary_receipt.chmod(0o600)
            os.replace(temporary_receipt, receipt_path)
        finally:
            temporary_receipt.unlink(missing_ok=True)
    except BaseException as error:
        rollback_errors: list[BaseException] = []

        def attempt(label: str, callback: Callable[[], None]) -> None:
            try:
                callback()
            except BaseException as rollback_error:
                rollback_errors.append(
                    InstallError(f"{label}: {rollback_error}")
                )

        if recorder_prepared and recorder_prepared in committed:
            attempt(
                f"unregister {recorder_target}",
                lambda: _unregister_recorder(recorder_target),
            )
        for snapshot in reversed(chrome_state_snapshots):
            attempt(
                f"restore {snapshot.target}",
                lambda snapshot=snapshot: _restore_file(snapshot),
            )
        if plugin_enable_state_snapshot:
            attempt(
                f"restore {plugin_enable_state_snapshot.target}",
                lambda: _restore_file(plugin_enable_state_snapshot),
            )
        attempt(
            f"restore {installation_receipt_snapshot.target}",
            lambda: _restore_file(installation_receipt_snapshot),
        )
        for item in reversed(committed):
            attempt(
                f"restore {item.target}",
                lambda item=item: _rollback_directory(item),
            )
        if recorder_prepared and recorder_target.exists():
            attempt(
                f"register restored {recorder_target}",
                lambda: _register_recorder(recorder_target),
            )
        if legacy_recorder_unregistered and legacy_profile_recorder.exists():
            attempt(
                f"register restored {legacy_profile_recorder}",
                lambda: _register_recorder(legacy_profile_recorder),
            )
        for app in removed_recorder_registrations:
            if app.exists():
                attempt(
                    f"register restored {app}",
                    lambda app=app: _run(
                        LAUNCH_SERVICES_REGISTER,
                        ["-f", str(app)],
                    ),
                )
        for item in prepared:
            attempt(
                f"remove {item.temporary}",
                lambda item=item: _remove_path(item.temporary),
            )
        if rollback_errors:
            details = "; ".join(str(item) for item in rollback_errors)
            raise InstallError(
                "Record installation failed and rollback was "
                f"incomplete: {error}; {details}"
            ) from error
        raise

    for item in prepared:
        try:
            _finish_directory(item)
        except BaseException as cleanup_error:
            print(
                "Installed successfully but could not remove backup "
                f"{item.backup}: {cleanup_error}",
                file=sys.stderr,
            )

    result = {
        "receiptPath": str(receipt_path),
        **(installation_receipt or {}),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    print(
        "Restart the backend for the target Hermes profile so the Python "
        "plugin routes are reloaded."
    )
    return result


def install(argv: list[str]) -> dict[str, Any]:
    with _install_lock():
        return _install_transaction(argv)


def main() -> int:
    try:
        install(sys.argv[1:])
    except KeyboardInterrupt:
        print("Record installation cancelled.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"Record installation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
