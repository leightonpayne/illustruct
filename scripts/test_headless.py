"""Run the suite against the checkout in ChimeraX with offscreen Qt."""

import importlib.util
import os
from pathlib import Path
import sys
import unittest


def run_tests(session):
    if session.ui.is_gui:
        raise RuntimeError("Run this test script in a fresh ChimeraX --nogui process.")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    repository = Path(__file__).resolve().parents[1]
    source = repository / "src"
    package_name = "chimerax.illustruct"
    for name in tuple(sys.modules):
        if name == package_name or name.startswith(package_name + "."):
            del sys.modules[name]
    spec = importlib.util.spec_from_file_location(package_name, source / "__init__.py", submodule_search_locations=[str(source)])
    package = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package
    spec.loader.exec_module(package)
    from chimerax.core.commands.cli import deregister
    try:
        deregister("illustruct")
    except RuntimeError:
        pass
    from chimerax.illustruct.commands import register_commands
    register_commands(session.logger)
    directory = repository / "tests"
    sys.path.insert(0, str(directory))
    # Make the ChimeraX session available before host-dependent test decorators run.
    for filename in sorted(directory.glob("test_*.py")):
        spec = importlib.util.spec_from_file_location(filename.stem, filename)
        module = importlib.util.module_from_spec(spec)
        module.session = session
        module._ILLUSTRUCT_TEST_DISCOVERY = True
        sys.modules[filename.stem] = module
        spec.loader.exec_module(module)
        if hasattr(module, "TEST_SESSION"):
            module.TEST_SESSION = session
    suite = unittest.defaultTestLoader.discover(str(directory), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful():
        print(f"ILLUSTRUCT_NATIVE_TESTS_PASS: {result.testsRun} tests")
    else:
        print(f"ILLUSTRUCT_NATIVE_TESTS_FAILED: {len(result.failures)} failures, {len(result.errors)} errors", file=sys.stderr)
    sys.stdout.flush()
    sys.stderr.flush()
    if not result.wasSuccessful():
        # ChimeraX reports script exceptions but can still exit with status 0.
        os._exit(1)


if "session" in globals():
    run_tests(session)
elif __name__ == "__main__":
    raise SystemExit("Use: ChimeraX --nogui --notools --exit --script scripts/test_headless.py")
