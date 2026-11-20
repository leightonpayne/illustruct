"""Publication geometry checks; run with NumPy/SciPy, without a Qt context."""
import importlib
from pathlib import Path
import sys
import types
import unittest

import numpy as np


_package = types.ModuleType('illustruct_publication_test')
_package.__path__ = [str(Path(__file__).resolve().parents[1] / 'src')]
sys.modules.setdefault(_package.__name__, _package)
publication = importlib.import_module(_package.__name__ + '.publication')
geometry = importlib.import_module(_package.__name__ + '.geometry')

CAMERA = {'scene_to_camera': np.eye(4), 'projection': 'orthographic'}
SETTINGS = {'publication_resolution': 160,
            'width': 400, 'height': 400, 'smoothing': 0, 'abstraction': 0,
            'frame': [-6, -6, 6, 6], 'stroke_width': 1}


def plane(identifier, x0, y0, x1, y1, z=0, slope=0):
    vertices = np.array([[x0, y0, z+slope*x0], [x1, y0, z+slope*x1],
                         [x1, y1, z+slope*x1], [x0, y1, z+slope*x0]], dtype=float)
    return publication._mesh(identifier, 'surface', vertices, np.tile([0, 0, 1], (4, 1)), [[0, 1, 2], [0, 2, 3]])


def part(identifier='A', meshes=None, **kwargs):
    meshes = meshes or [plane(identifier, -4, -4, 4, 4)]
    return {'id': identifier, 'coords': np.concatenate([m['vertices'] for m in meshes]),
            'mode': 'blob', 'kind': 'protein', 'segments': [], 'meshes': meshes, **kwargs}


class SyntheticEngine(publication.PublicationEngine):
    def _surface(self, value, abstraction):
        return value['meshes']


def inside(rings, point):
    x, y = point
    result = False
    for ring in rings:
        for a, b in zip(ring, ring[1:] + ring[:1]):
            if (a[1] > y) != (b[1] > y) and x < (b[0]-a[0])*(y-a[1])/(b[1]-a[1])+a[0]:
                result = not result
    return result


def painted_at(result, point):
    return [p['id'] for p in result['draw_primitives'] if inside(p['rings'], point)]


class PublicationGeometryTests(unittest.TestCase):
    def compute(self, parts, settings=None, camera=None):
        return SyntheticEngine().compute(parts, camera or CAMERA, {**SETTINGS, **(settings or {})})

    def test_backbone_crosses_a_surface_without_cutting_its_fill(self):
        line = np.array([[-5., 0., -2.], [5., 0., 2.]])
        for opacity in (1., .45):
            parts = [part('D', coords=line, segments=[line], kind='nucleic', mode='line', render_opacity=opacity),
                     part('P', [plane('P', -4, -3, 4, 3)], render_opacity=opacity)]
            result = self.compute(parts)
            protein = next(item for item in result['items'] if item['id'] == 'P')
            self.assertEqual(len(protein['primitives']), 1, 'A backbone must not raster-cut the protein fill underneath it.')
            self.assertTrue(inside(protein['rings'], (-2, 0)))
            self.assertTrue(inside(protein['rings'], (2, 0)))
            for x, expected in ((-2., ['D', 'P']), (2., ['P', 'D'])):
                actual = [primitive['id'] for primitive in result['draw_primitives']
                          if inside(primitive['clip_rings'] if primitive.get('mode') == 'line' else primitive['rings'], (x, 0))]
                self.assertEqual(actual, expected)

    def test_local_depth_switches_front_part_inside_same_overlap(self):
        parts = [part('A', [plane('A', -4, -3, 4, 3, slope=1)]),
                 part('B', [plane('B', -4, -3, 4, 3, slope=-1)])]
        result = self.compute(parts)
        self.assertEqual(painted_at(result, (-2, 0)), ['B'])
        self.assertEqual(painted_at(result, (2, 0)), ['A'])
        self.assertEqual(len(result['items']), 2)

    def test_dashes_are_confined_to_shared_borders_between_colours_in_a_chain(self):
        group = ((1,), 0, 'A', 'protein')
        blue = part('blue', [plane('blue', -4, -3, 2, 3)],
                    boundary_group=group, render_color='#cee6f6')
        green = part('green', [plane('green', -1, -2, 4, 2, z=1)],
                     boundary_group=group, render_color='#b8dda5')
        settings = {'dashed_boundaries': True}
        result = self.compute([blue, green], settings)
        primitives = result['draw_primitives']
        borders = [(p['id'], curve) for p in primitives for curve in p.get('cubic_internal_lines', [])]
        self.assertEqual({identifier for identifier, _ in borders}, {'green'})
        self.assertEqual({p['id'] for p in primitives if p.get('cubic_outline_lines')}, {'blue', 'green'})
        for _, curve in borders:
            points = np.array([curve['start'], *(segment[-2:] for segment in curve['segments'])])
            self.assertGreaterEqual(points[:, 0].min(), -1.1)
            self.assertLessEqual(points[:, 0].max(), 2.1)
        for override in ({'boundary_group': ((1,), 0, 'B', 'protein')}, {'render_color': '#cee6f6'}):
            result = self.compute([blue, {**green, **override}], settings)
            self.assertFalse(any(p.get('cubic_internal_lines') for p in result['draw_primitives']))

    def test_hidden_or_zero_opacity_parts_do_not_occlude(self):
        for changes in ({'render_visible': False}, {'render_opacity': 0}):
            parts = [part('A'), part('B', [plane('B', -4, -4, 4, 4, z=5)], **changes)]
            result = self.compute(parts)
            self.assertEqual(painted_at(result, (0, 0)), ['A'])
            self.assertEqual([p['id'] for p in result['items']], ['A'])

    def test_lighting_stays_in_place_when_an_overlapping_component_hides_a_contour(self):
        parts = [part('A', [plane('A', -4, -3, 4, 3)]),
                 part('B', [plane('B', 0, -4, 5, 4, z=2)])]
        engine = SyntheticEngine()
        hidden = engine.compute(parts, CAMERA, SETTINGS)['items'][0]
        self.assertLess(hidden['bounds'][2], .1, 'The right half must really be occluded')

        def roles_at(item, point):
            return {layer['role'] for primitive in item['primitives'] for layer in primitive['lighting']
                    if inside(layer['rings'], point)}

        # Compiled and NumPy rasterizers place the thin crescent up to half
        # a pixel apart. Sample its upper region rather than one edge pixel.
        self.assertTrue(any('highlight' in roles_at(hidden, (-2, y))
                            for y in np.linspace(-2.9, -2.3, 25)))
        self.assertEqual(roles_at(hidden, (-2, 2.7)), {'shadow'})
        self.assertEqual(roles_at(hidden, (-.2, 0)), set(), 'An occlusion cut must not create a new shadow rim')
        for changes in ({'render_opacity': .5}, {'render_visible': False}):
            parts[1].update(changes)
            revealed = engine.compute(parts, CAMERA, SETTINGS)['items'][0]
            self.assertGreater(revealed['bounds'][2], 3.9)
            for x in (-3, -2, -1):
                for y in (-2.9, -2.7, -2.4, 0, 2.5, 2.9):
                    self.assertEqual(roles_at(revealed, (x, y)), roles_at(hidden, (x, y)))

    def test_cavity_exposes_surface_behind_it(self):
        meshes = [plane('A', -4, -4, 4, -2), plane('A', -4, 2, 4, 4),
                  plane('A', -4, -2, -2, 2), plane('A', 2, -2, 4, 2)]
        background = part('B', [plane('B', -5, -5, 5, 5, z=-1)])
        result = self.compute([background, part(meshes=meshes)])
        self.assertEqual(painted_at(result, (0, 0)), ['B'])
        self.assertEqual(painted_at(result, (3, 0)), ['A'])

    def test_minimum_piece_size_filters_by_area_within_each_component(self):
        pieces = [plane('A', -5, -2, -1, 2), plane('A', 1, -1, 3, 1),
                  plane('A', 4, -.5, 5, .5)]
        parts = [part('A', pieces), part('small', [plane('small', 0, 4, .5, 4.5)])]
        for fraction, expected in ((0, [True, True, True]), (.1, [True, True, False]),
                                   (.4, [True, False, False]), (1, [True, False, False])):
            with self.subTest(fraction=fraction):
                result = self.compute(parts, {'minimum_piece_size': fraction})
                self.assertEqual([bool(painted_at(result, point)) for point in ((-3, 0), (2, 0), (4.5, 0))], expected)
                self.assertEqual(painted_at(result, (.25, -4.25)), ['small'],
                                 'A small whole component must remain visible.')

    def test_piece_filter_rechecks_newly_revealed_layers_until_stable(self):
        parts = [part('background', [plane('background', -5.5, -5, 5.5, 5, z=-2)]),
                 part('behind', [plane('behind', -5, 2.5, -1, 4.5, z=1),
                                 plane('behind', 3, -.5, 4, .5, z=1)]),
                 part('front', [plane('front', -5, -2, -1, 2, z=2),
                                plane('front', 3, -.5, 4, .5, z=2)])]
        result = self.compute(parts, {'minimum_piece_size': .4})
        self.assertEqual(painted_at(result, (3.5, 0)), ['background'],
                         'The newly exposed small piece behind the first must also be removed.')
        self.assertEqual(painted_at(result, (-3, 0)), ['front'])
        self.assertEqual(painted_at(result, (-3, -3.5)), ['behind'])

    def test_atom_coordinates_produce_a_finite_envelope(self):
        coords = np.array([[x, y, z] for x in (-2, 0, 2) for y in (-2, 0, 2) for z in (-2, 0, 2)], dtype=float)
        parts = [part(coords=coords, atom_radii=np.full(len(coords), 1.7))]
        result = geometry.compute_geometry(parts, CAMERA, SETTINGS)
        rings = result['items'][0]['rings']
        self.assertTrue(rings)
        self.assertTrue(np.isfinite(np.concatenate(rings)).all())
        self.assertTrue(inside(rings, (2, 0)))
        self.assertFalse(inside(rings, (5, 0)))


if __name__ == '__main__':
    unittest.main()
