"""Build the ChimeraX bundle with standard Python packaging tools.

Native ``devel build`` continues to use bundle_info.xml. Both build routes use its
registration and package data. The release wheel declares the validated platform.
"""

import ast
from pathlib import Path
import tomllib
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parent


def bundle_setup_arguments(root=ROOT):
    """Return setuptools options without importing ChimeraX or the bundle."""
    root = Path(root)
    bundle = ET.parse(root / "bundle_info.xml").getroot()
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    dependencies = [entry.attrib["name"] + entry.attrib["version"]
                    for entry in bundle.findall("Dependencies/Dependency")]
    author = {"name": bundle.findtext("Author", "")}
    if email := bundle.findtext("Email", ""):
        author["email"] = email
    expected = {
        "name": bundle.attrib["name"],
        "version": bundle.attrib["version"],
        "description": bundle.findtext("Synopsis", ""),
        "readme": {"text": bundle.findtext("Description", ""), "content-type": "text/plain"},
        "authors": [author],
        "license": bundle.findtext("License", ""),
    }
    mismatches = [key for key, value in expected.items() if project.get(key) != value]
    if project.get("urls", {}).get("Homepage") != bundle.findtext("URL", ""):
        mismatches.append("urls.Homepage")
    if sorted(project.get("dependencies", [])) != sorted(dependencies):
        mismatches.append("dependencies")
    if mismatches:
        raise ValueError("pyproject.toml and bundle_info.xml disagree on: "
                         + ", ".join(mismatches))

    runtime = ast.parse((root / "src/version.py").read_text(encoding="utf-8"))
    versions = [ast.literal_eval(node.value) for node in runtime.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets)]
    if versions != [project["version"]]:
        raise ValueError("src/version.py and pyproject.toml disagree on: version")

    package = bundle.attrib["package"]
    classifiers = [
        "Framework :: ChimeraX",
        "Intended Audience :: Science/Research",
        "Programming Language :: Python :: 3",
        "Topic :: Scientific/Engineering :: Visualization",
        "Topic :: Scientific/Engineering :: Chemistry",
        "Topic :: Scientific/Engineering :: Bio-Informatics",
    ]
    classifiers.extend(entry.text for entry in bundle.findall("Classifiers/PythonClassifier"))
    classifiers.extend([
        "Environment :: MacOS X :: Aqua",
        "Operating System :: MacOS :: MacOS X",
    ])
    categories = ",".join(entry.attrib["name"] for entry in bundle.findall("Categories/Category"))
    sessions = bundle.attrib["minSessionVersion"] + "," + bundle.attrib["maxSessionVersion"]
    classifiers.append("ChimeraX :: Bundle :: " + " :: ".join([
        categories, sessions, package, "", bundle.get("customInit", ""),
    ]))
    for entry in bundle.findall("Classifiers/ChimeraXClassifier"):
        classifier = entry.text or ""
        classifiers.append(classifier if classifier.startswith("ChimeraX") else "ChimeraX :: " + classifier)

    source = root / "src"
    packages = [package]
    packages.extend(package + "." + ".".join(path.parent.relative_to(source).parts)
                    for path in sorted(source.rglob("__init__.py")) if path.parent != source)
    data_files = [entry.text.strip() for entry in bundle.findall("DataFiles/DataFile")]
    return {
        "url": bundle.findtext("URL", ""),
        "classifiers": classifiers,
        "packages": packages,
        "package_dir": {package: "src"},
        "package_data": {package: data_files},
        "include_package_data": False,
    }


if __name__ == "__main__":
    from setuptools import setup
    setup(**bundle_setup_arguments())
