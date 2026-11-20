"""Command integration tests for the headless ChimeraX runner."""

from copy import deepcopy
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import numpy as np


TEST_SESSION = None
FIXTURES = Path(__file__).resolve().parent
NS = {"svg": "http://www.w3.org/2000/svg"}


class CommandIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if TEST_SESSION is None or TEST_SESSION.ui.is_gui:
            raise unittest.SkipTest("Run scripts/test_headless.py inside headless ChimeraX")
        cls.session = TEST_SESSION
        from chimerax.core.commands import run
        from chimerax.core.errors import UserError
        cls.run_command = staticmethod(run)
        cls.UserError = UserError

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.opened = []
        self.addCleanup(self.session.models.close, self.opened)
        self.model = self.open_fixture("crambin.pdb")
        self.specifier = f"#{self.model.id_string}"
        self.command(f"hide {self.specifier} atoms; show {self.specifier} cartoons")

    def command(self, command):
        return self.run_command(self.session, command, log=False)

    def open_fixture(self, name):
        existing = set(self.session.models.list())
        self.command(f"open {json.dumps(str(FIXTURES / name))}")
        models = [model for model in self.session.models.list() if model not in existing]
        self.opened.extend(models)
        return next(model for model in models if hasattr(model, "atoms"))

    def export(self, name, options=""):
        path = self.directory / name
        geometry = self.command(f"illustruct export {json.dumps(str(path))} {options}")
        return path, geometry

    def test_explicit_selection_exports_hidden_atoms_without_changing_selection(self):
        self.command(f"select {self.specifier}:1-5; hide {self.specifier} models")
        selected = self.model.atoms.selected.copy()
        with self.assertRaisesRegex(self.UserError, "No atoms"):
            self.export("visible.svg")
        self.assertFalse((self.directory / "visible.svg").exists())

        path, _ = self.export("selection.svg", "atoms sel splitSelection true")
        titles = [element.text for element in ET.parse(path).getroot().findall("svg:g/svg:title", NS)]
        self.assertTrue(titles)
        self.assertTrue(all("selected residues" in title for title in titles))
        np.testing.assert_array_equal(self.model.atoms.selected, selected)

    def test_export_uses_current_colors_and_camera_without_changing_atoms(self):
        self.command(f"color {self.specifier}:1-15 #cee6f6 target ac; "
                     f"color {self.specifier}:16-46 #bbc5d4 target ac")
        coords = self.model.atoms.coords.copy()
        colors = self.model.residues.ribbon_colors.copy()
        options = "shading false framing camera width 600 height 600"
        first, _ = self.export("first.svg", options)
        root = ET.parse(first).getroot()
        camera = json.loads(root.find("svg:metadata", NS).text)["camera"]
        span = max(2, 2 / camera["aspect"])
        np.testing.assert_allclose(tuple(map(float, root.get("viewBox").split())),
                                   (-span / 2, -span / 2, span, span), rtol=1e-6)
        self.assertEqual(self.fills(root), {"#cee6f6", "#bbc5d4"})
        np.testing.assert_array_equal(self.model.atoms.coords, coords)
        np.testing.assert_array_equal(self.model.residues.ribbon_colors, colors)

        self.command(f"turn y 35; color {self.specifier} #13579b target ac")
        second, _ = self.export("second.svg", options)
        root = ET.parse(second).getroot()
        current_camera = json.loads(root.find("svg:metadata", NS).text)["camera"]
        self.assertNotEqual(camera["scene_to_camera"], current_camera["scene_to_camera"])
        self.assertEqual(self.fills(root), {"#13579b"})

    @staticmethod
    def fills(root):
        return {path.get("fill") for path in root.findall("svg:g/svg:path", NS)
                if path.get("data-role") == "surface"}

    def test_explicit_atoms_and_components_override_panel_filters(self):
        from chimerax.illustruct.scene import capture_scene
        from chimerax.illustruct.state import settings_with

        panel = type("IllustructTool", (), {})()
        panel.settings = settings_with({"show_chains": False, "show_proteins": False,
                                        "fixed_frame": True, "match_projection": True})
        panel.styles = {part["id"]: {"visible": False}
                        for part in capture_scene(self.session)["parts"]}
        panel.frame = [1000, 1000, 2000, 2000]
        baseline, _ = self.export("baseline.svg", "framing fit")

        with patch.object(self.session.tools, "list", return_value=[panel]):
            saved = deepcopy((panel.settings, panel.styles, panel.frame))
            path, _ = self.export("explicit.svg", f"atoms {self.specifier} components protein framing fit")
        self.assertEqual(path.read_bytes(), baseline.read_bytes())
        self.assertEqual((panel.settings, panel.styles, panel.frame), saved)

    def test_invalid_output_requests_do_not_write_files(self):
        for name, options in (("bad.jpeg", ""), ("large.svg", "width 16000 height 16000"),
                              ("units.svg", "width 320 heightMm 60"), ("opacity.svg", "opacity 101")):
            with self.subTest(name=name):
                with self.assertRaises(self.UserError):
                    self.export(name, options)
                self.assertFalse((self.directory / name).exists())

    def test_export_formats_use_requested_dimensions(self):
        from Qt.QtGui import QImage
        from Qt.QtWidgets import QApplication

        self.__class__.qt_application = QApplication.instance() or QApplication(["illustruct-tests", "-platform", "offscreen"])
        print_options = "widthMm 80 heightMm 60 dpi 254"
        svg, _ = self.export("figure.svg", print_options)
        root = ET.parse(svg).getroot()
        self.assertEqual((root.get("width"), root.get("height")), ("80mm", "60mm"))
        self.assertTrue(root.findall(".//svg:path", NS))

        pdf, _ = self.export("figure.pdf", print_options)
        data = pdf.read_bytes()
        self.assertTrue(data.startswith(b"%PDF-"))
        self.assertNotIn(b"/Subtype /Image", data)
        box = re.search(rb"/MediaBox\s*\[([^]]+)\]", data)
        self.assertIsNotNone(box)
        _, _, width, height = map(float, box.group(1).split())
        self.assertAlmostEqual(width * 25.4 / 72, 80, places=5)
        self.assertAlmostEqual(height * 25.4 / 72, 60, places=5)

        png, _ = self.export("figure.png", "width 320 height 240 dpi 254")
        image = QImage(str(png))
        self.assertFalse(image.isNull())
        self.assertEqual((image.width(), image.height()), (320, 240))
        self.assertAlmostEqual(image.dotsPerMeterX() * .0254, 254, delta=.02)

    def test_nucleic_style_and_type_filter_reach_the_exporter(self):
        self.open_fixture("dna.pdb")
        for representation, mode in (("backbone", "line"), ("silhouette", "blob")):
            with self.subTest(representation=representation):
                _, geometry = self.export(f"dna-{representation}.svg",
                                          f"components nucleic nucleicStyle {representation}")
                self.assertTrue(geometry["items"])
                self.assertTrue(all(item["id"].endswith(":nucleic") and item["mode"] == mode
                                    for item in geometry["items"]))


if __name__ == "__main__":
    unittest.main()
