#!/usr/bin/env python3
"""Run the test suite with no test-runner dependency.

    python run_tests.py            # all hermetic tests
    python run_tests.py persona    # only files matching a substring

Works under pytest too (``pytest tests/``); this exists so the suite runs on a
bare interpreter, which is how the plugin is usually deployed.
"""

from __future__ import annotations

import importlib.util
import sys
import traceback
from pathlib import Path

TESTS_DIR = Path(__file__).parent / "tests"


def load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    pattern = sys.argv[1] if len(sys.argv) > 1 else ""
    files = sorted(p for p in TESTS_DIR.glob("test_*.py") if pattern in p.stem)
    if not files:
        print(f"no test files match {pattern!r}")
        return 1

    passed = failed = 0
    for path in files:
        try:
            module = load(path)
        except Exception:
            print(f"✗ {path.name}: import failed")
            traceback.print_exc()
            failed += 1
            continue

        setup = getattr(module, "setup_function", None)
        for name, fn in sorted(vars(module).items()):
            if not name.startswith("test_") or not callable(fn):
                continue
            try:
                if setup:
                    setup(fn)
                fn()
                passed += 1
                print(f"✓ {path.stem}::{name}")
            except Exception:
                failed += 1
                print(f"✗ {path.stem}::{name}")
                traceback.print_exc()

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
