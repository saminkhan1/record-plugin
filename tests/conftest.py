from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_PACKAGE = "hermes_plugins.record"


def _load_plugin_package() -> None:
    """Mirror Hermes' directory-plugin namespace for standalone tests."""

    if "hermes_plugins" not in sys.modules:
        namespace = types.ModuleType("hermes_plugins")
        namespace.__path__ = []
        namespace.__package__ = "hermes_plugins"
        sys.modules["hermes_plugins"] = namespace
    if PLUGIN_PACKAGE in sys.modules:
        return

    spec = importlib.util.spec_from_file_location(
        PLUGIN_PACKAGE,
        PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load Record from {PLUGIN_ROOT}")
    module = importlib.util.module_from_spec(spec)
    module.__package__ = PLUGIN_PACKAGE
    module.__path__ = [str(PLUGIN_ROOT)]
    sys.modules[PLUGIN_PACKAGE] = module
    spec.loader.exec_module(module)


_load_plugin_package()
