"""Backbone traces and their projection."""
import json
import unittest

import numpy as np

from test_publication import CAMERA, SETTINGS, geometry


def part(coords, **changes):
    return {'id': 'A', 'coords': np.asarray(coords), 'segments': [np.asarray(coords)], 'kind': 'nucleic', 'mode': 'line', **changes}


class BackboneTests(unittest.TestCase):
    def compute(self, parts, settings=None, camera=None):
        return geometry.compute_geometry(parts, camera or CAMERA, {**SETTINGS, **(settings or {})})

    def test_disconnected_traces_singletons_and_invalid_records_never_gain_bridges(self):
        segments = [np.array([[-5., 0., 0.], [-3., 1., 0.]]), np.array([[3., 0., 0.], [np.nan, 0., 0.], [5., 0., 0.]])]
        result = self.compute([part(np.vstack(segments), segments=segments)], {'smoothing': 2., 'abstraction': 1.7})
        lines = result['items'][0]['lines']
        self.assertEqual(len(lines), 3)
        np.testing.assert_allclose(np.asarray(lines[0])[[0, -1]], [[-5., 0.], [-3., -1.]])
        self.assertEqual(lines[1:], [[[3., 0.]], [[5., 0.]]])
        json.dumps(result, allow_nan=False)

    def test_smoothed_backbone_retains_every_residue_anchor(self):
        points = np.array([[0., 0., 0.], [3., 4., 1.], [6., 0., 2.], [9., -3., 5.], [12., 2., 8.]])
        original = points.copy()
        curve = self.compute([part(points)], {'smoothing': 2., 'abstraction': 1.7})['items'][0]['lines'][0]
        curve = np.asarray(curve)
        for anchor in points:
            self.assertLess(np.linalg.norm(curve-anchor[:2]*[1, -1], axis=1).min(), 1e-12)
        np.testing.assert_array_equal(curve[[0, -1]], points[[0, -1], :2]*[1, -1])
        np.testing.assert_array_equal(points, original)

    def test_perspective_clip_preserves_separate_visible_pieces_and_singletons(self):
        points = [[-2., 0., -2.], [0., 0., 2.], [2., 0., -2.]]
        segments = [points, [[8., 0., -2.]], [[0., 0., 2.]]]
        camera = {**CAMERA, 'projection': 'perspective', 'field_of_view': 90, 'near_clip': .5}
        result = self.compute([part(points, segments=segments)], {'match_projection': True}, camera)
        lines = result['items'][0]['lines']
        self.assertEqual(len(lines), 3)
        np.testing.assert_allclose(lines[0], [[-1, 0], [-2.5, 0]])
        np.testing.assert_allclose(lines[1], [[2.5, 0], [1, 0]])
        np.testing.assert_allclose(lines[2], [[4, 0]])
        invisible = self.compute([part([[0, 0, 2], [1, 0, 3]])], {'match_projection': True}, camera)
        self.assertEqual(invisible['items'][0]['lines'], [])
        self.assertEqual(invisible['draw_primitives'], [])


if __name__ == '__main__':
    unittest.main()
