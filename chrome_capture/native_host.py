#!/usr/bin/env python3
"""Native Messaging entrypoint for the Hermes Chrome provider."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_NAME = "_record_native"
_PACKAGE_SPEC = importlib.util.spec_from_file_location(
    _PACKAGE_NAME,
    PLUGIN_ROOT / "__init__.py",
    submodule_search_locations=[str(PLUGIN_ROOT)],
)
if _PACKAGE_SPEC is None or _PACKAGE_SPEC.loader is None:
    raise ImportError(f"Cannot load Record plugin package from {PLUGIN_ROOT}")
_PACKAGE = importlib.util.module_from_spec(_PACKAGE_SPEC)
sys.modules[_PACKAGE_NAME] = _PACKAGE
try:
    _PACKAGE_SPEC.loader.exec_module(_PACKAGE)
except Exception:
    sys.modules.pop(_PACKAGE_NAME, None)
    raise

from _record_native.chrome_capture import native_host_main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(native_host_main())
