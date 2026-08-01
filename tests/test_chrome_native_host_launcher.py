from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def _load_launcher():
    module_path = PLUGIN_ROOT / "chrome_capture" / "native_host_launcher.py"
    module_name = f"hermes_plugins.record._launcher_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return module


def _receipt(**overrides):
    receipt = {
        "schemaVersion": 2,
        "release": False,
        "chromeMode": "local",
        "signingIdentity": "adhoc",
        "notarizationStatus": "not-applicable-dev",
        "profile": "default",
        "chromeExtensionId": "anaccpcogeinfllkciaheolngknaabbb",
        "chromeExtensionVersion": "0.2.1",
    }
    receipt.update(overrides)
    return receipt


def test_local_dev_receipt_can_select_native_host():
    mod = _load_launcher()
    identity = ("anaccpcogeinfllkciaheolngknaabbb", "0.2.1")

    assert mod._runtime_receipt_allowed(_receipt(), "default", identity)


def test_release_receipt_can_select_native_host():
    mod = _load_launcher()
    identity = ("anaccpcogeinfllkciaheolngknaabbb", "0.2.1")

    assert mod._runtime_receipt_allowed(
        _receipt(
            release=True,
            chromeMode="store",
            signingIdentity="Developer ID Application: Wexpro Labs (TEAMID)",
            notarizationStatus="approved",
        ),
        "default",
        identity,
    )


def test_unmarked_nonrelease_receipt_is_rejected():
    mod = _load_launcher()
    identity = ("anaccpcogeinfllkciaheolngknaabbb", "0.2.1")

    assert not mod._runtime_receipt_allowed(
        _receipt(notarizationStatus="not-applicable-dev", chromeMode="store"),
        "default",
        identity,
    )
