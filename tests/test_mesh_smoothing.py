"""Surface smoothing shape checks."""
import importlib.util
from pathlib import Path
import unittest

import numpy as np
from scipy.spatial import ConvexHull


_path = Path(__file__).resolve().parents[1] / "src" / "mesh_smoothing.py"
_spec = importlib.util.spec_from_file_location("illustruct_mesh_smoothing_test", _path)
smoothing = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoothing)


def jagged_sphere(count=400):
    index = np.arange(count)
    z = 1-2*(index+.5)/count
    angle = index*np.pi*(3-np.sqrt(5))
    radius = np.sqrt(1-z*z)
    normals = np.column_stack((radius*np.cos(angle), radius*np.sin(angle), z)).astype(np.float32)
    triangles = ConvexHull(normals).simplices.astype(np.int32)
    points = normals[triangles]
    inward = np.einsum('ij,ij->i', np.cross(points[:, 1]-points[:, 0], points[:, 2]-points[:, 0]),
                      points.mean(axis=1)) < 0
    triangles[inward] = triangles[inward][:, [0, 2, 1]]
    vertices = normals*(1+.12*np.where(index % 2, 1, -1))[:, None]
    return vertices.astype(np.float32), normals, triangles


def radii(vertices):
    return np.linalg.norm(vertices - vertices.mean(axis=0), axis=1)


class MeshSmoothingTests(unittest.TestCase):

    def test_smoothing_reduces_jaggedness_without_moving_or_shrinking_components(self):
        vertices, normals, triangles = jagged_sphere(160)
        count = len(vertices)
        vertices = np.vstack((vertices, .3*vertices+np.array([6., -3., 4.], dtype=np.float32)))
        normals = np.vstack((normals, normals))
        triangles = np.vstack((triangles, triangles+count))
        originals = tuple(array.copy() for array in (vertices, normals, triangles))
        result, result_normals = smoothing.smooth_mesh(vertices, normals, triangles, 1)
        for indices in (slice(0, count), slice(count, None)):
            np.testing.assert_allclose(result[indices].mean(axis=0), vertices[indices].mean(axis=0), atol=2e-6)
            before, after = radii(vertices[indices]), radii(result[indices])
            self.assertLess(after.std(), .5 * before.std())
            self.assertAlmostEqual(np.mean(after ** 2), np.mean(before ** 2), places=6)
        for actual, original in zip((vertices, normals, triangles), originals):
            np.testing.assert_array_equal(actual, original)
        np.testing.assert_allclose(np.linalg.norm(result_normals, axis=1), 1, atol=2e-7)


if __name__ == "__main__":
    unittest.main()
