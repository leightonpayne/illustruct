"""Native shape control checks."""
import importlib
from pathlib import Path
import sys
import types
import unittest

import numpy as np


_package = types.ModuleType("illustruct_publication_range_test")
_package.__path__ = [str(Path(__file__).resolve().parents[1] / "src")]
sys.modules.setdefault(_package.__name__, _package)
publication = importlib.import_module(_package.__name__ + ".publication")
state = importlib.import_module(_package.__name__ + ".state")
try:
    gaussian = importlib.import_module("chimerax.surface.gaussian")
except ImportError:
    gaussian = None

CAMERA = {"scene_to_camera": np.eye(4), "projection": "orthographic"}


def protein(identifier="A", **overrides):
    coords = np.array([(x, y, z) for x in (-4, 0, 4)
                       for y in (-4, 0, 4) for z in (-4, 0, 4)], dtype=float)
    return {"id": identifier, "kind": "protein", "mode": "blob", "segments": [],
            "coords": coords, "atom_radii": np.full(len(coords), 1.7), **overrides}


@unittest.skipIf(gaussian is None, "Native ChimeraX Gaussian surface implementation is required")
class PublicationControlRangeTests(unittest.TestCase):
    def test_simplification_broadens_proteins_while_small_components_stay_compact(self):
        polymer = protein()
        ion = protein("ion", kind="other", coords=np.array([[7., -2., 3.]]), atom_radii=np.array([1.7]))
        parts = [polymer, ion]
        engine = publication.PublicationEngine()
        settings = state.settings_with({"abstraction": state.simplification_abstraction(.5),
                                        "smoothing": state.smoothing_amount(0.)})
        low, small = engine.publication_meshes(parts, settings, CAMERA)
        high, same_ion = engine.publication_meshes(
            parts, {**settings, "abstraction": state.simplification_abstraction(1.)}, CAMERA)
        self.assertTrue(np.all(np.ptp(high["vertices"], axis=0) > np.ptp(low["vertices"], axis=0)))
        np.testing.assert_array_equal(small["vertices"], same_ion["vertices"])


if __name__ == "__main__":
    unittest.main()
