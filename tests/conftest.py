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
    loaded = sys.modules.get(PLUGIN_PACKAGE)
    loaded_file = getattr(loaded, "__file__", None)
    if loaded_file and Path(loaded_file).resolve() == (
        PLUGIN_ROOT / "__init__.py"
    ).resolve():
        return
    for name in tuple(sys.modules):
        if name == PLUGIN_PACKAGE or name.startswith(f"{PLUGIN_PACKAGE}."):
            sys.modules.pop(name, None)

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


def pytest_collectstart(collector) -> None:
    """Undo host imports that discover an installed Record during collection."""

    if collector.__class__.__name__ == "Module":
        _load_plugin_package()
