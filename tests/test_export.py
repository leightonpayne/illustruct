"""Run with Python unittest; PNG/PDF checks use ChimeraX's native Qt."""

import importlib.util
import json
import os
import re
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zlib


MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "export.py"
PACKAGE = types.ModuleType("illustruct_export_tests")
PACKAGE.__path__ = [str(MODULE_PATH.parent)]
sys.modules.setdefault(PACKAGE.__name__, PACKAGE)
SPEC = importlib.util.spec_from_file_location(PACKAGE.__name__ + ".export", MODULE_PATH)
export = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(export)

NS = {"svg": "http://www.w3.org/2000/svg"}
GEOMETRY = {"items": [
    {"id": "A", "mode": "blob", "rings": [
        [[0, 0], [10, 0], [10, 10], [0, 10]],
        [[3, 3], [7, 3], [7, 7], [3, 7]],
    ]},
    {"id": "B", "mode": "line", "lines": [[[20, 0], [30, 10]]]},
], "bounds": [0, 0, 30, 10]}

CUBIC = {"start": [0, 0], "segments": [[0, -2, 10, -2, 10, 0], [10, 10, 0, 10, 0, 0]]}
PUBLICATION = {"items": [{"id": "A", "mode": "blob", "rings": [],
    "primitives": [{"role": "surface", "cubic_rings": [CUBIC]}], "cubic_outline_rings": [CUBIC]}]}


def _straight_curve(points):
    return {"start": points[0], "segments": [
        [a[0] + (b[0] - a[0])/3, a[1] + (b[1] - a[1])/3,
         a[0] + 2*(b[0] - a[0])/3, a[1] + 2*(b[1] - a[1])/3, *b]
        for a, b in zip(points, points[1:])]}


COLOUR_BOUNDARY = {"publication_frame": [0, 0, 100, 100],
    "items": [{"id": "blue"}, {"id": "green"}], "draw_primitives": [
        {"id": "blue", "role": "surface", "rings": [[[10, 10], [50, 10], [50, 90], [10, 90]]],
         "cubic_outline_lines": [_straight_curve([[50, 10], [10, 10], [10, 90], [50, 90]])]},
        {"id": "green", "role": "surface", "rings": [[[50, 10], [90, 10], [90, 90], [50, 90]]],
         "cubic_outline_lines": [_straight_curve([[50, 90], [90, 90], [90, 10], [50, 10]])],
         "cubic_internal_lines": [_straight_curve([[50, 10], [50, 90]])]}]}
BOUNDARY_STYLES = {"blue": {"color": "#cee6f6"}, "green": {"color": "#b8dda5"}}
BOUNDARY_SETTINGS = {"width": 200, "height": 200, "stroke_width": 4,
                     "outline_color": "#000000", "shading": False, "dashed_boundaries": True}


class SVGExportTests(unittest.TestCase):
    def test_only_internal_colour_boundaries_receive_editable_dash_patterns(self):
        root = ET.fromstring(export.build_svg(COLOUR_BOUNDARY, BOUNDARY_SETTINGS, BOUNDARY_STYLES))
        internal = root.findall("svg:g/svg:path[@data-role='internal-outline']", NS)
        exterior = root.findall("svg:g/svg:path[@data-role='outline']", NS)
        self.assertEqual(len(internal), 1, "The shared seam must be painted once")
        self.assertEqual(len(exterior), 2)
        self.assertTrue(all(path.get("stroke-dasharray") is None for path in exterior))
        seam = internal[0]
        self.assertTrue(seam.get("stroke-dasharray"))
        self.assertIn("C", seam.get("d"))

        solid = ET.fromstring(export.build_svg(COLOUR_BOUNDARY,
            {**BOUNDARY_SETTINGS, "dashed_boundaries": False}, BOUNDARY_STYLES))
        strokes = [path for path in solid.findall("svg:g/svg:path", NS)
                   if path.get("stroke", "none") != "none"]
        self.assertTrue(all(path.get("stroke-dasharray") is None for path in strokes))
        self.assertTrue(any(path.get("d") == seam.get("d") for path in strokes),
                        "The solid border must follow the same seam")

    def test_print_svg_preserves_physical_size_and_editable_holes(self):
        settings = {"size_mode": "print", "width": 1200, "height": 900, "print_width_mm": 101.6,
                    "print_height_mm": 76.2, "dpi": 300, "stroke_width": 120}
        root = ET.fromstring(export.build_svg(GEOMETRY, settings, metadata={"source": "native"}))
        self.assertEqual((root.attrib["width"], root.attrib["height"]), ("101.6mm", "76.2mm"))
        metadata = json.loads(root.find("svg:metadata", NS).text)
        self.assertEqual(metadata["export"]["dpi"], 300)
        self.assertEqual(metadata["source"], "native")
        view_width = float(root.attrib["viewBox"].split()[2])
        path = root.find("svg:g/svg:path", NS)
        self.assertAlmostEqual(float(path.attrib["stroke-width"]) * 1200 / view_width, 120)
        self.assertEqual(path.get("fill-rule"), "evenodd")
        self.assertEqual(path.get("d").count("M"), 2)
        self.assertFalse(root.findall(".//svg:image", NS))

    def test_xml_names_metadata_and_paint_are_safe(self):
        import numpy as np
        name = 'Protein <A> & "B"\x00'
        metadata = {"source": "</metadata><script>text</script>", "camera": np.eye(3), "atoms": np.int64(42)}
        svg = export.build_svg(GEOMETRY, {}, {"A": {"name": name, "color": 'url("https://example.com")'}}, metadata)
        root = ET.fromstring(svg)
        self.assertEqual(root.find("svg:g/svg:title", NS).text, name[:-1] + "\ufffd")
        decoded = json.loads(root.find("svg:metadata", NS).text)
        self.assertEqual(decoded, {"source": metadata["source"], "camera": np.eye(3).tolist(), "atoms": 42})
        self.assertNotIn("<script>", svg)
        self.assertNotIn("https://example.com", svg)

    def test_svg_atomic_write_and_failed_replace_preserves_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "illustration.svg"
            result = export.export_svg(destination, GEOMETRY, {})
            self.assertEqual(result, destination)
            ET.fromstring(destination.read_text())
            destination.write_text("original")
            with patch.object(export.os, "replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    export.export_svg(destination, GEOMETRY, {})
            self.assertEqual(destination.read_text(), "original")
            self.assertEqual(list(Path(temporary).iterdir()), [destination])


class NativeExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        try:
            from Qt.QtGui import QImage
            from Qt.QtWidgets import QApplication
            export.svg_renderer_class()
        except ImportError as error:
            raise unittest.SkipTest("ChimeraX QtSvg is not available in this Python") from error
        cls.application = QApplication.instance() or QApplication([])
        cls.QImage = QImage

    def test_mixed_surface_backbone_pixels_follow_local_depth_at_both_opacities(self):
        import numpy as np
        from test_publication import CAMERA, SyntheticEngine, part, plane
        line = np.array([[-5., 0., -2.], [5., 0., 2.]])
        for opacity in (1., .5):
            settings = {'frame': [-6, -6, 6, 6], 'width': 240, 'height': 240, 'publication_resolution': 240,
                        'smoothing': 0, 'abstraction': 0, 'stroke_width': 1,
                        'opacity': opacity, 'transparent': False, 'background': '#ffffff', 'shading': False}
            parts = [part('D', coords=line, segments=[line], kind='nucleic', mode='line', render_opacity=opacity),
                     part('P', [plane('P', -4, -3, 4, 3)], render_opacity=opacity)]
            result = SyntheticEngine().compute(parts, CAMERA, settings)
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary)/'mixed.png'
                export.export_png(path, result, settings, {'D': {'color': '#ff0000'}, 'P': {'color': '#0000ff'}})
                image = self.QImage(str(path))
                expected = ((0, 0, 255), (255, 0, 0)) if opacity == 1 else ((128, 64, 191), (191, 64, 128))
                for x, rgb in zip((80, 160), expected):
                    actual = image.pixelColor(x, 120)
                    for got, wanted in zip((actual.red(), actual.green(), actual.blue()), rgb):
                        self.assertAlmostEqual(got, wanted, delta=2)

    def test_oversized_cubic_paths_keep_late_fills_holes_group_alpha_and_camera_clip(self):
        corners = [(10, 10), (70, 10), (70, 80), (10, 80), (10, 10)]
        segments = []
        for start, end in zip(corners, corners[1:]):
            for step in range(3000):
                points = [[start[axis] + (end[axis] - start[axis]) * (step + t) / 3000
                           for axis in (0, 1)] for t in (1/3, 2/3, 1)]
                segments.append([coordinate for point in points for coordinate in point])
        geometry = {"publication_frame": [0, 0, 100, 100], "items": [{"id": "A", "primitives": [
            {"cubic_rings": [{"start": corners[0], "segments": segments},
                _straight_curve([(20, 20), (60, 20), (60, 60), (20, 60), (20, 20)]),
                _straight_curve([(80, 30), (90, 30), (90, 40), (80, 40), (80, 30)])]},
            {"rings": [[[40, 70], [60, 70], [60, 90], [40, 90]]]}]}]}
        settings = {"width": 100, "height": 100, "opacity": 0.5, "outline": False,
                    "match_projection": True, "frame": [0, 0, 100, 85]}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "large.png"
            export.export_png(path, geometry, settings, {"A": {"color": "#123456"}})
            image = self.QImage(str(path))
            self.assertFalse(image.isNull())
            for x, y, alpha in ((85, 35, 128), (25, 25, 0), (50, 75, 128), (50, 88, 0)):
                self.assertAlmostEqual(image.pixelColor(x, y).alpha(), alpha, delta=1)

    def test_native_png_rasterizes_holes_alpha_dimensions_and_color(self):
        geometry = {"items": [GEOMETRY["items"][0]], "bounds": [0, 0, 10, 10]}
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "illustration.png"
            export.export_png(destination, geometry, {"width": 240, "height": 240, "outline": False, "dpi": 450}, {"A": {"color": "#ff0000"}})
            image = self.QImage(str(destination))
            self.assertFalse(image.isNull())
            self.assertEqual((image.width(), image.height()), (240, 240))
            self.assertAlmostEqual(image.dotsPerMeterX() * 0.0254, 450, delta=0.02)
            self.assertAlmostEqual(image.dotsPerMeterY() * 0.0254, 450, delta=0.02)
            self.assertEqual(image.pixelColor(120, 120).alpha(), 0, "The central ring hole must remain transparent")
            self.assertEqual(image.pixelColor(0, 0).alpha(), 0, "Background must remain transparent")
            self.assertEqual(image.pixelColor(40, 120).name(), "#ff0000")
            self.assertEqual(image.pixelColor(40, 120).alpha(), 255)


    def test_print_pdf_preserves_cubic_vectors_page_size_and_opacity(self):
        settings = {"width": 600, "height": 480, "size_mode": "print", "dpi": 300,
                    "print_width_mm": 50, "print_height_mm": 40, "opacity": .5}
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "illustration.pdf"
            self.assertEqual(export.export_pdf(destination, PUBLICATION, settings), destination)
            data = destination.read_bytes()
            self.assertTrue(data.startswith(b"%PDF-"))
            self.assertNotIn(b"/Subtype /Image", data, "Vector export must not embed a screenshot")
            box = re.search(rb"/MediaBox\s*\[([^]]+)\]", data)
            self.assertIsNotNone(box)
            values = list(map(float, box.group(1).split()))
            self.assertAlmostEqual(values[2] * 25.4 / 72, 50, places=5)
            self.assertAlmostEqual(values[3] * 25.4 / 72, 40, places=5)
            self.assertEqual(re.search(rb"/TrimBox\s*\[([^]]+)\]", data).group(1), box.group(1))
            alpha = [float(value) for value in re.findall(rb"/ca\s+([\d.]+)", data)]
            self.assertTrue(any(abs(value - .5) < .005 for value in alpha), alpha)
            streams = []
            for match in re.finditer(rb"stream\n(.*?)\nendstream", data, re.S):
                try:
                    streams.append(zlib.decompress(match.group(1)))
                except zlib.error:
                    streams.append(match.group(1))
            self.assertTrue(any(re.search(rb"\bc\b", stream) for stream in streams), "PDF must contain editable cubic operators")


if __name__ == "__main__":
    unittest.main()
