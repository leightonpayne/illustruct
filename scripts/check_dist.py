"""Verify the wheel and source archive against the checkout."""

import argparse
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def one_file(directory, pattern):
    files = sorted(directory.glob(pattern))
    require(len(files) == 1, f"Expected one {pattern} in {directory}, found {len(files)}")
    return files[0]


def source_file(path):
    return (path.is_file() and ".DS_Store" not in path.parts
            and "__pycache__" not in path.parts and path.suffix != ".pyc")


def check(directory, test_sdist=False):
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    wheel = one_file(directory, "*.whl")
    sdist = one_file(directory, "*.tar.gz")
    require(wheel.name == f"illustruct-{project['version']}-py3-none-any.whl",
            "Wheel filename must be illustruct-VERSION-py3-none-any.whl")
    license_bytes = (ROOT / "LICENSE").read_bytes()
    source_files = [path for path in (ROOT / "src").rglob("*") if source_file(path)]
    with ZipFile(wheel) as archive:
        names = archive.namelist()
        require(not any(".DS_Store" in PurePosixPath(name).parts for name in names),
                "Wheel includes Finder metadata (.DS_Store)")
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        require(len(metadata_names) == 1, "Wheel must contain one METADATA file")
        metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
        for field, expected in (("Name", project["name"]), ("Version", project["version"]),
                                ("License-Expression", project["license"])):
            require(metadata[field] == expected, f"Wheel {field} differs from pyproject.toml")
        require(metadata.get_all("License-File") == ["LICENSE"], "Wheel omits the license declaration")
        license_path = metadata_names[0].removesuffix("METADATA") + "licenses/LICENSE"
        require(archive.read(license_path) == license_bytes, "Wheel license text differs")
        dependencies = {value.replace(" ", "") for value in metadata.get_all("Requires-Dist", [])}
        require(dependencies == set(project["dependencies"]), "Wheel dependencies differ, including host requirements")
        classifiers = metadata.get_all("Classifier", [])
        for prefix in ("ChimeraX :: Bundle ::", "ChimeraX :: Tool :: Illustruct ::",
                       "ChimeraX :: Command :: illustruct ::"):
            require(any(value.startswith(prefix) for value in classifiers), f"Missing registration: {prefix}")
        expected_members = {"chimerax/illustruct/" + path.relative_to(ROOT / "src").as_posix()
                            for path in source_files}
        actual_members = {name for name in names
                          if name.startswith("chimerax/illustruct/") and not name.endswith("/")}
        require(actual_members == expected_members,
                "Wheel source files differ from the checkout; use a clean build directory")
        for path in source_files:
            member = "chimerax/illustruct/" + path.relative_to(ROOT / "src").as_posix()
            require(member in names, f"Wheel omits {path.relative_to(ROOT)}")
            require(archive.read(member) == path.read_bytes(), f"Wheel has stale {member}")
        require(not any(name.startswith(("tests/", "qa/", "scripts/")) for name in names),
                "Wheel includes development-only inputs")
    print(f"Validated {wheel.name}: source, help, license, dependencies, registration", flush=True)

    with tempfile.TemporaryDirectory(prefix="illustruct-sdist-") as temporary:
        with tarfile.open(sdist) as archive:
            members = archive.getmembers()
            require(not any(".DS_Store" in PurePosixPath(member.name).parts for member in members),
                    "Source archive includes Finder metadata (.DS_Store)")
            archive.extractall(temporary, members=members, filter="data")
        roots = list(Path(temporary).iterdir())
        require(len(roots) == 1 and roots[0].is_dir(), "Source archive must have one root directory")
        extracted = roots[0]
        required = ["LICENSE", "README.md", "MANIFEST.in", "pyproject.toml", "bundle_info.xml",
                    "setup.py", "uv.lock", "zensical.toml", "package.json", "aube-lock.yaml"]
        for directory_name in ("src", "scripts", "tests", "docs", ".github"):
            required.extend(p.relative_to(ROOT).as_posix() for p in (ROOT / directory_name).rglob("*")
                            if source_file(p))
        for name in required:
            path = extracted / name
            require(path.is_file(), f"Source archive omits {name}")
            require(path.read_bytes() == (ROOT / name).read_bytes(), f"Source archive has stale {name}")
        require(not (extracted / "qa").exists(), "Generated qa/ belongs outside the source archive")
        print(f"Validated {sdist.name}: complete source, docs, scripts, local fixtures", flush=True)
        if test_sdist:
            subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                           cwd=extracted, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, nargs="?", default=ROOT / "dist")
    parser.add_argument("--test-sdist", action="store_true")
    args = parser.parse_args()
    check(args.directory.resolve(), args.test_sdist)


if __name__ == "__main__":
    main()
