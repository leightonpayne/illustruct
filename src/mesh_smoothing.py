"""Smooth detached envelope meshes without changing their topology or placement."""
import math

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components


def _native_functions():
    try:
        from chimerax.surface import smooth_vertex_positions, calculate_vertex_normals
    except ImportError:
        return None
    return smooth_vertex_positions, calculate_vertex_normals


def _neighbor_graph(triangles, count):
    edges = np.concatenate((triangles[:, (0, 1)], triangles[:, (1, 2)], triangles[:, (2, 0)]))
    rows = np.concatenate((edges[:, 0], edges[:, 1]))
    columns = np.concatenate((edges[:, 1], edges[:, 0]))
    # Preserve repeated face neighbors to match the native smoother, including
    # boundary vertices where incident edges do not all have equal weights.
    return csr_matrix((np.ones(len(rows), dtype=np.float32), (rows, columns)), shape=(count, count))


def _centers_and_radii(vertices, labels, counts):
    centers = np.column_stack([np.bincount(labels, weights=vertices[:, axis], minlength=len(counts))/counts
                               for axis in range(3)])
    offsets = vertices-centers[labels]
    squared = np.einsum('ij,ij->i', offsets, offsets)
    radii = np.sqrt(np.bincount(labels, weights=squared, minlength=len(counts))/counts)
    return centers, radii


def _fallback_normals(vertices, triangles):
    normals = np.zeros_like(vertices)
    face_normals = np.cross(vertices[triangles[:, 1]]-vertices[triangles[:, 0]],
                            vertices[triangles[:, 2]]-vertices[triangles[:, 0]])
    for corner in range(3):
        np.add.at(normals, triangles[:, corner], face_normals)
    return normals


def smooth_mesh(vertices, normals, triangles, amount):
    """Return detached vertices/normals after up to forty Taubin smoothing pairs.

    ``amount`` ranges from zero to four, with ten pairs per unit. Surface connectivity is unchanged;
    each connected component keeps its center and RMS radius independently.
    Call on cached scene geometry, before projection and depth testing.
    """
    strength = float(amount)
    if not math.isfinite(strength):
        raise ValueError("Mesh smoothing must be finite.")
    strength = min(4., max(0., strength))
    source = np.array(vertices, dtype=np.float32, order='C', copy=True)
    old_normals = np.array(normals, dtype=np.float32, order='C', copy=True)
    topology = np.asarray(triangles, dtype=np.int32, order='C')
    if source.ndim != 2 or source.shape[1] != 3 or old_normals.shape != source.shape:
        raise ValueError("Mesh vertices and normals must have shape (count, 3).")
    if topology.ndim != 2 or topology.shape[1] != 3:
        raise ValueError("Mesh triangles must have shape (count, 3).")
    if not np.isfinite(source).all():
        raise ValueError("Mesh vertices must be finite.")
    if len(topology) and (topology.min() < 0 or topology.max() >= len(source)):
        raise ValueError("Mesh triangle indices must refer to existing vertices.")
    if not strength or not len(source) or not len(topology):
        return source, old_normals

    graph = _neighbor_graph(topology, len(source))
    components, labels = connected_components(graph, directed=False)
    counts = np.bincount(labels, minlength=components)
    centers, radii = _centers_and_radii(source, labels, counts)
    native = _native_functions()
    if native is None:
        degrees = np.asarray(graph.sum(axis=1)).ravel()
        connected = degrees > 0

        def step(values, factor):
            averages = values.copy()
            averages[connected] = graph.dot(values)[connected]/degrees[connected, None]
            values += factor*(averages-values)
    else:
        native_step, native_normals = native

        def step(values, factor):
            native_step(values, topology, factor, 1)

    smoothed = source.copy()
    passes = 10*strength
    for index in range(math.ceil(passes)):
        fraction = min(1., passes-index)
        previous = smoothed.copy() if fraction < 1 else None
        step(smoothed, .5)
        step(smoothed, -.53)
        if previous is not None:
            smoothed = previous+fraction*(smoothed-previous)

    # Paired smoothing limits shrinkage. Restore scale and drift separately for
    # disconnected pieces so small fragments cannot collapse or move together.
    new_centers, new_radii = _centers_and_radii(smoothed, labels, counts)
    scales = np.divide(radii, new_radii, out=np.ones_like(radii), where=new_radii > 1e-12)
    smoothed = ((smoothed-new_centers[labels])*scales[labels, None]+centers[labels]).astype(np.float32)
    collapsed = (new_radii <= 1e-12) & (radii > 1e-12)
    smoothed[collapsed[labels]] = source[collapsed[labels]]
    result_normals = _fallback_normals(smoothed, topology) if native is None else native_normals(smoothed, topology)
    lengths = np.linalg.norm(result_normals, axis=1)
    missing = lengths <= 1e-12
    result_normals[missing] = old_normals[missing]
    lengths = np.linalg.norm(result_normals, axis=1)
    result_normals[lengths <= 1e-12] = (0., 0., 1.)
    result_normals /= np.linalg.norm(result_normals, axis=1)[:, None]
    return smoothed, result_normals
