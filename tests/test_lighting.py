"""Lighting behaviour for neutral and coloured source swatches."""

import importlib
from pathlib import Path
import sys
import types
import unittest


_package = types.ModuleType("illustruct_lighting_tests")
_package.__path__ = [str(Path(__file__).resolve().parents[1] / "src")]
sys.modules.setdefault(_package.__name__, _package)
lighting = importlib.import_module(_package.__name__ + ".lighting")
palettes = importlib.import_module(_package.__name__ + ".palettes")


def rgb(colour):
    return tuple(int(colour[index:index + 2], 16) for index in (1, 3, 5))


def luminance(colour):
    def linear(channel):
        value = channel / 255
        return value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4
    return sum(weight * linear(channel) for weight, channel in
               zip((.2126, .7152, .0722), rgb(colour)))


class LightingColourTests(unittest.TestCase):
    def test_lighting_keeps_neutral_colours_and_changes_brightness(self):
        for base in ("#000000", "#808080", "#ffffff", "#ff0000"):
            with self.subTest(base=base):
                highlight, shadow = lighting.lighting_colors(base)
                if len(set(rgb(base))) == 1:
                    self.assertEqual(len(set(rgb(highlight))), 1)
                    self.assertEqual(len(set(rgb(shadow))), 1)
                if base != "#ffffff":
                    self.assertGreater(luminance(highlight), luminance(base))
                if base != "#000000":
                    self.assertLess(luminance(shadow), luminance(base))

    def test_palette_swatch_uses_its_neighbouring_colours(self):
        _, families = palettes.NATURE_PALETTE_GROUPS[0]
        _, colours = families[0]
        self.assertEqual(lighting.lighting_colors(colours[1]), (colours[0], colours[2]))


if __name__ == "__main__":
    unittest.main()
