"""Check that uv and the native builder describe the same ChimeraX bundle."""

from pathlib import Path
import runpy
import shutil
import tempfile
import tomllib
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
bundle_setup_arguments = runpy.run_path(str(ROOT / "setup.py"))["bundle_setup_arguments"]
TEST_SESSION = None


class PackagingTests(unittest.TestCase):

    def test_build_rejects_conflicting_bundle_or_runtime_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copyfile(ROOT / "pyproject.toml", root / "pyproject.toml")
            (root / "src").mkdir()
            shutil.copyfile(ROOT / "src/version.py", root / "src/version.py")
            bundle = ET.parse(ROOT / "bundle_info.xml")
            bundle.getroot().set("version", "9.0.0")
            bundle.write(root / "bundle_info.xml")
            with self.assertRaisesRegex(ValueError, "disagree on: version"):
                bundle_setup_arguments(root)

            shutil.copyfile(ROOT / "bundle_info.xml", root / "bundle_info.xml")
            (root / "src/version.py").write_text('VERSION = "not-the-package-version"\n')
            with self.assertRaisesRegex(ValueError, "src/version.py.*disagree on: version"):
                bundle_setup_arguments(root)

    def test_native_xml_builder_matches_standard_builder_registration_and_data(self):
        if TEST_SESSION is None:
            self.skipTest("Native builder comparison runs inside ChimeraX")
        from chimerax.bundle_builder import BundleBuilder
        native = BundleBuilder(TEST_SESSION.logger, str(ROOT))
        native._make_setup_arguments()
        standard = bundle_setup_arguments()
        # ChimeraX adds its own cross-platform classifiers to all pure Python bundles.
        platform_prefixes = ("Environment :: ", "Operating System :: ")
        release_classifiers = [value for value in standard["classifiers"]
                               if not value.startswith(platform_prefixes)]
        native_classifiers = [value for value in native.setup_arguments["classifiers"]
                              if not value.startswith(platform_prefixes)]
        self.assertEqual(release_classifiers, native_classifiers)
        self.assertEqual(standard["url"], native.setup_arguments["url"])
        self.assertEqual({name: set(files) for name, files in standard["package_data"].items()},
                         {name: set(files) for name, files in native.setup_arguments["package_data"].items()})
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        self.assertEqual(set(project["dependencies"]),
                         {requirement.replace(" ", "") for requirement in native.setup_arguments["install_requires"]})
        self.assertEqual(project["name"], native.setup_arguments["name"])
        self.assertEqual(project["version"], native.setup_arguments["version"])
        self.assertEqual(project["license"], native.setup_arguments["license"])


if __name__ == "__main__":
    unittest.main()
