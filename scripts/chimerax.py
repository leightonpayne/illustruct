"""Build Illustruct or run checks in a separate ChimeraX process."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def executable(explicit=None):
    """Find the host application without importing it into the uv environment."""
    requested = explicit or os.environ.get("CHIMERAX_EXECUTABLE")
    if requested:
        candidate = shutil.which(requested) or str(Path(requested).expanduser())
        if Path(candidate).is_file():
            return candidate
        raise SystemExit(f"ChimeraX executable not found: {requested}")
    for name in ("ChimeraX", "chimerax"):
        if candidate := shutil.which(name):
            return candidate
    for candidate in (
        Path("/Applications/ChimeraX.app/Contents/bin/ChimeraX"),
        *sorted(Path("/Applications").glob("ChimeraX-*.app/Contents/bin/ChimeraX"), reverse=True),
    ):
        if candidate.is_file():
            return str(candidate)
    raise SystemExit("ChimeraX was not found. Set CHIMERAX_EXECUTABLE or pass --executable PATH.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "test", "install", "gui"))
    parser.add_argument("--executable", help="Path to the ChimeraX executable")
    args = parser.parse_args()
    if args.action == "build":
        return subprocess.call(["uv", "build", "--wheel"], cwd=ROOT)
    host = executable(args.executable)
    if args.action == "install":
        # Use ChimeraX's native bundle builder and installer. The source tree
        # remains portable; uv supplies development tools, not host libraries.
        from json import dumps
        command = f"devel install {dumps(str(ROOT))}"
        return subprocess.call([host, "--nogui", "--notools", "--exit", "--cmd", command], cwd=ROOT)
    script = "test_headless.py" if args.action == "test" else "test_gui.py"
    command = [host, "--notools"]
    if args.action == "test":
        command += ["--nogui", "--exit"]
    # ChimeraX splits --script with shlex; quote paths that contain spaces.
    from shlex import quote
    script_arg = quote(str(ROOT / "scripts" / script))
    if args.action == "gui":
        script_arg += " --exit-after"
    return subprocess.call(command + ["--script", script_arg], cwd=ROOT)


if __name__ == "__main__":
    sys.exit(main())
