"""Import the plugin as a package regardless of the checkout directory name.

Hermes loads this plugin from a directory called ``live_call``, and the code
uses package-relative imports accordingly. A git checkout is usually named
something else (``hermes-live-call``), which would break ``import live_call``
in the tests — so register the package under its canonical name explicitly.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PACKAGE = "live_call"


def load():
    """Return the imported plugin package (idempotent)."""
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    if spec is None or spec.loader is None:      # pragma: no cover
        raise ImportError(f"cannot load plugin package from {PLUGIN_DIR}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module
