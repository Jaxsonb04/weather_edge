"""Zero-dependency test runner for the forecaster package.

Mirrors ``trading/tests/run_tests.py``: discovers ``test_*`` functions in
``test_*.py`` files, runs each with no arguments, and exits non-zero on any
failure. Pure standard library so it slots into the same CI gate idiom. Tests
must be network-free (use in-memory SQLite and injected payloads).
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
import traceback
from collections.abc import Callable, Iterator
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
FORECASTER_ROOT = TEST_DIR.parent
sys.path.insert(0, str(FORECASTER_ROOT))
sys.path.insert(0, str(TEST_DIR))


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module


def _iter_tests() -> Iterator[Callable[[], None]]:
    for path in sorted(TEST_DIR.glob("test_*.py")):
        module = _load_module(path)
        for name, test in sorted(inspect.getmembers(module, inspect.isfunction)):
            if name.startswith("test_") and test.__module__ == module.__name__:
                yield test


def main() -> int:
    failures = 0
    tests = list(_iter_tests())
    for test in tests:
        try:
            test()
            print(f"PASS {test.__module__}.{test.__name__}")
        except Exception:  # noqa: BLE001 - report and continue across all tests
            failures += 1
            print(f"FAIL {test.__module__}.{test.__name__}")
            traceback.print_exc()
    print(f"\nran {len(tests)} tests, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
