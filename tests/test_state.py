"""Illustration settings, visibility, and session state."""

import importlib.util
import math
from pathlib import Path
import unittest


_source = Path(__file__).resolve().parents[1] / "src" / "state.py"
_spec = importlib.util.spec_from_file_location("illustruct_state_tests", _source)
state = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(state)


class StateTests(unittest.TestCase):
    def test_print_sizing_preserves_physical_size_and_stroke(self):
        settings = state.settings_with({"print_width_mm": 89, "dpi": 600, "stroke_width_pt": 5})
        self.assertEqual(settings["width"], round(89 / 25.4 * 600))
        self.assertAlmostEqual(settings["stroke_width"], 5 * 600 / 72)
        pixels = state.settings_with(state.sizing_mode(settings, "pixels"))
        self.assertEqual((pixels["width"], pixels["height"]), (settings["width"], settings["height"]))
        self.assertEqual(state.settings_with(state.sizing_mode(pixels, "print")), settings)

    def test_invalid_settings_and_oversized_exports_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "opacity"):
            state.settings_with({"opacity": math.inf})
        with self.assertRaisesRegex(ValueError, "64 million"):
            state.settings_with({"size_mode": "pixels", "width": 8001, "height": 8000})

    def test_group_visibility_keeps_individual_choices_and_source_colours(self):
        parts = [
            {"id": "a", "kind": "protein", "label": "Chain A", "color": "#123456"},
            {"id": "b", "kind": "protein", "label": "Chain B", "color": "#654321"},
            {"id": "ion", "kind": "other", "category": "ions", "label": "Ion", "color": "#987654"},
        ]
        styles = {"a": {"visible": False}}
        settings = state.settings_with({"show_ions": True})

        def visible(style):
            return {key for key, part in style.items() if part["visible"]}

        self.assertEqual(visible(state.effective_styles(parts, styles, settings)), {"b", "ion"})
        self.assertEqual(
            visible(state.effective_styles(parts, styles, {**settings, "show_chains": False})),
            {"ion"},
        )
        parts[0]["color"] = "#abcdef"
        updated = state.effective_styles(parts, styles, settings)
        self.assertEqual(visible(updated), {"b", "ion"})
        self.assertEqual(updated["a"]["color"], "#abcdef")

    def test_colour_split_inherits_parent_visibility(self):
        region = {"id": "region", "parent_id": "chain", "kind": "protein",
                  "label": "Region", "color": "#cee6f6"}
        styles = state.inherit_region_styles([region], {"chain": {"visible": False}})
        self.assertFalse(state.effective_styles([region], styles)["region"]["visible"])
        styles["region"]["visible"] = True
        self.assertEqual(styles["chain"], {"visible": False})

    def test_session_merge_remaps_exact_model_ids(self):
        old = "model:1.2:instance:0:chain:A:protein"
        new = "model:4.2:instance:0:chain:A:protein"
        other = "model:1.20:instance:0:chain:A:protein"
        settings = {"model_ids": [(1, 2), (1, 20)]}
        styles = {old: {"visible": False}, other: {"visible": True}}
        restored, paint = state.remap_model_ids(settings, styles, {(1, 2): (4, 2)})
        self.assertEqual(restored["model_ids"], [(4, 2), (1, 20)])
        self.assertEqual(paint, {new: {"visible": False}, other: {"visible": True}})
        self.assertEqual(settings["model_ids"], [(1, 2), (1, 20)])
        self.assertNotIn(new, styles)


if __name__ == "__main__":
    unittest.main()
