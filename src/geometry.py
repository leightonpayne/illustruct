"""Shared projection and curve math for the unified molecular renderer.

All illustrations use Publication envelopes and flat vector backbone strokes.
Captured arrays are read-only; camera transforms and curve fitting are detached.
"""
from math import isfinite, tan, radians

import numpy as np

_EPS = 1e-12

def _number(value, fallback):
    if isinstance(value, (bool, np.bool_)):
        return fallback
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return value if isfinite(value) else fallback

def _unit(value, fallback):
    return min(1.0, max(0.0, _number(value, fallback)))

def _abstraction(value, fallback):
    return min(1.7, max(0.0, _number(value, fallback)))

def _smoothing(value, fallback):
    return min(2.0, max(0.0, _number(value, fallback)))

def _coordinates(value):
    if value is None:
        return np.empty((0, 3), dtype=np.float64)
    points = np.asarray(value, dtype=np.float64)
    if points.size == 0:
        return np.empty((0, 3), dtype=np.float64)
    if points.ndim != 2 or points.shape[1] != 3:
        raise ValueError("Molecular coordinates must have shape (number of atoms, 3).")
    return points

def _camera_parameters(camera, match_projection):
    camera = camera or {}
    matrix = np.asarray(camera.get("scene_to_camera", np.eye(4)), dtype=np.float64)
    if matrix.shape not in ((3, 4), (4, 4)) or not np.isfinite(matrix).all():
        raise ValueError("The scene-to-camera transform must be a finite 3×4 or 4×4 matrix.")
    if matrix.shape == (4, 4) and not np.allclose(matrix[3], (0, 0, 0, 1)):
        raise ValueError("The scene-to-camera transform must be an affine transform.")
    matrix = matrix[:3].copy()
    perspective = bool(match_projection and camera.get("projection") == "perspective")
    field_of_view = _number(camera.get("field_of_view"), 45.0)
    if not 0 < field_of_view < 179:
        field_of_view = 45.0
    field_width = _number(camera.get("field_width"), 100.0)
    if field_width <= _EPS:
        field_width = 100.0
    aspect = _number(camera.get("aspect"), 1.0)
    if aspect <= _EPS:
        aspect = 1.0
    near_clip = max(_number(camera.get("near_clip"), 0.01), 1e-6)
    parameters = {
        "matrix": matrix, "match": bool(match_projection),
        "perspective": perspective, "tan_half_fov": tan(radians(field_of_view) / 2),
        "field_width": field_width, "aspect": aspect, "near_clip": near_clip,
    }
    key = (tuple(matrix.ravel()), bool(match_projection), perspective,
           field_of_view, field_width, aspect, near_clip)
    return parameters, key

def _transform(points, matrix):
    with np.errstate(over="ignore", invalid="ignore"):
        return points @ matrix[:, :3].T + matrix[:, 3]

def _split_finite(points):
    """Invalid records break traces; removing them must not create new bonds."""
    if not len(points):
        return []
    mask = np.isfinite(points).all(axis=1)
    changes = np.flatnonzero(np.diff(np.r_[False, mask, False]))
    return [points[start:end] for start, end in changes.reshape(-1, 2)]

def _clip_segment(points, near_clip):
    """Clip each polyline edge against camera Z <= -near_clip."""
    if len(points) == 1:
        return [points] if points[0, 2] <= -near_clip else []
    result, current = [], []
    for a, b in zip(points[:-1], points[1:]):
        a_inside, b_inside = a[2] <= -near_clip, b[2] <= -near_clip
        if a_inside and not current:
            current.append(a)
        if a_inside and b_inside:
            current.append(b)
        elif a_inside != b_inside:
            fraction = (-near_clip - a[2]) / (b[2] - a[2])
            intersection = a + fraction * (b - a)
            intersection[2] = -near_clip
            current.append(intersection)
            if b_inside:
                current.append(b)
            else:
                result.append(np.asarray(current))
                current = []
        elif current:
            result.append(np.asarray(current))
            current = []
    if current:
        result.append(np.asarray(current))
    return result

def _project(points, camera):
    result = points[:, :2].copy()
    result[:, 1] *= -1
    if camera["perspective"]:
        result /= (-points[:, 2] * camera["tan_half_fov"])[:, None]
    elif camera["match"]:
        result *= 2.0 / camera["field_width"]
    return result

def _deduplicate_line(points):
    if len(points) < 2:
        return points
    return points[np.r_[True, np.any(np.diff(points, axis=0) != 0, axis=1)]]

def _signed_area(ring):
    # Translate first to avoid cancellation for a small shape far from origin.
    shifted = ring - ring[0]
    following = np.roll(shifted, -1, axis=0)
    return float(np.sum(shifted[:, 0] * following[:, 1] - shifted[:, 1] * following[:, 0]) / 2)

def _segment_distances(points, start, end):
    direction = end - start
    length_squared = float(direction @ direction)
    if length_squared <= 0:
        return np.linalg.norm(points - start, axis=1)
    t = np.clip((points - start) @ direction / length_squared, 0, 1)
    return np.linalg.norm(points - start - t[:, None] * direction, axis=1)

def _simplify_line(points, tolerance):
    if len(points) < 3 or tolerance <= 0:
        return points.copy()
    keep = np.zeros(len(points), dtype=bool)
    keep[0] = keep[-1] = True
    pending = [(0, len(points) - 1)]
    while pending:
        start, end = pending.pop()
        if end - start < 2:
            continue
        distances = _segment_distances(points[start + 1:end], points[start], points[end])
        local_index = int(np.argmax(distances))
        if distances[local_index] > tolerance:
            index = start + 1 + local_index
            keep[index] = True
            pending.extend(((start, index), (index, end)))
    return points[keep]

def _simplify_ring(ring, tolerance):
    if len(ring) <= 4 or tolerance <= 0:
        return ring.copy()
    farthest = int(np.argmax(np.linalg.norm(ring - ring[0], axis=1)))
    a = _simplify_line(ring[:farthest + 1], tolerance)
    b = _simplify_line(np.vstack((ring[farthest:], ring[:1])), tolerance)
    result = np.vstack((a[:-1], b[:-1]))
    if len(result) < 3 or np.sign(_signed_area(result)) != np.sign(_signed_area(ring)):
        return ring.copy()
    return result

def _cubic_ring(ring, smoothing, max_deviation):
    """Fit a closed silhouette with shared, bounded tangents at every knot.

    A common handle on both sides of a knot makes the entire contour C1
    continuous, including its closing seam. Limiting each handle's normal
    distance from both incident edges bounds the fit without adding samples
    or allowing long, uneven polygon edges to produce loops and bulges.
    """
    ring = np.asarray(ring, dtype=float)
    following = np.roll(ring, -1, axis=0)
    delta = following-ring
    if smoothing <= 0 or len(ring) < 3:
        return {"start": ring[0], "segments": np.concatenate(
            (ring+delta/3, ring+2*delta/3, following), axis=1)}
    length = np.linalg.norm(delta, axis=1)
    direction = delta/np.maximum(length[:, None], _EPS)
    previous = np.roll(direction, 1, axis=0)
    tangent = previous+direction
    tangent /= np.maximum(np.linalg.norm(tangent, axis=1)[:, None], _EPS)
    normal = np.maximum(np.abs(tangent[:, 0]*direction[:, 1]-tangent[:, 1]*direction[:, 0]),
                        np.abs(tangent[:, 0]*previous[:, 1]-tangent[:, 1]*previous[:, 0]))
    handle = np.minimum(length, np.roll(length, 1))/3
    handle *= min(1., smoothing/.65)
    handle = np.minimum(handle, max_deviation/np.maximum(normal, _EPS))
    tangent *= handle[:, None]
    return {"start": ring[0], "segments": np.concatenate(
        (ring+tangent, following-np.roll(tangent, -1, axis=0), following), axis=1)}

def _bounds(paths):
    nonempty = [path for path in paths if len(path)]
    if not nonempty:
        return [0.0, 0.0, 0.0, 0.0]
    minimum = np.min([np.min(path, axis=0) for path in nonempty], axis=0)
    maximum = np.max([np.max(path, axis=0) for path in nonempty], axis=0)
    return [float(minimum[0]), float(minimum[1]), float(maximum[0]), float(maximum[1])]

def _nucleic_curve(points, smoothing, abstraction):
    """Interpolate residue anchors in 3D, before perspective and clipping.

    Centripetal Catmull–Rom tangents use distances, so camera rotations cannot
    change the molecular curve. Every anchor is retained, including termini
    and base attachment points. Simplification changes sampling, not residues.
    """
    if len(points) < 3 or smoothing <= 0:
        return points.copy()
    extended = np.vstack((2 * points[0] - points[1], points,
                          2 * points[-1] - points[-2]))
    knots = np.r_[0.0, np.cumsum(np.sqrt(np.linalg.norm(np.diff(extended, axis=0), axis=1)))]
    samples = max(4, round(12 - 8 * abstraction))
    u = np.linspace(0, 1, samples + 1)[:-1, None]
    u2, u3 = u * u, u * u * u
    # Limit tangent length to avoid loops while retaining every base anchor.
    strength = 0.5 + 0.5 * min(smoothing, 1.)
    pieces = []
    for index in range(len(points) - 1):
        p0, p1, p2, p3 = extended[index:index + 4]
        t0, t1, t2, t3 = knots[index:index + 4]
        if min(t1 - t0, t2 - t1, t3 - t2) <= _EPS:
            pieces.append((1 - u) * p1 + u * p2)
            continue
        tangent1 = p2 - p1 + (t2 - t1) * ((p1 - p0) / (t1 - t0) - (p2 - p0) / (t2 - t0))
        tangent2 = p2 - p1 + (t2 - t1) * ((p3 - p2) / (t3 - t2) - (p3 - p1) / (t3 - t1))
        pieces.append((2*u3 - 3*u2 + 1) * p1 + (u3 - 2*u2 + u) * strength * tangent1
                      + (-2*u3 + 3*u2) * p2 + (u3 - u2) * strength * tangent2)
    return np.vstack((*pieces, points[-1:]))


def _backbone_curves(part, smoothing, abstraction):
    curves = []
    for source in part.get("segments", ()):
        for finite in _split_finite(_coordinates(source)):
            points = _deduplicate_line(finite)
            if len(points):
                curves.append(_nucleic_curve(points, smoothing, abstraction))
    return curves


def _project_backbone(curves, camera):
    result = []
    for source in curves:
        points = _transform(source, camera["matrix"])
        for finite in _split_finite(points):
            pieces = _clip_segment(finite, camera["near_clip"]) if camera["perspective"] else [finite]
            for piece in pieces:
                if len(piece):
                    result.append(np.column_stack((_project(piece, camera), piece[:, 2])))
    return result


class GeometryEngine:
    """Cached unified renderer, independent of the native UI."""
    def __init__(self):
        from .publication import PublicationEngine
        self._publication = PublicationEngine()

    def clear(self):
        self._publication.clear()

    def compute(self, parts, camera=None, settings=None):
        return self._publication.compute(parts, camera, settings)

    def publication_meshes(self, parts, settings, camera=None):
        """Detached envelope meshes; backbones are vector curves, not tubes."""
        return self._publication.publication_meshes(parts, settings, camera)


def compute_geometry(parts, camera=None, settings=None):
    return GeometryEngine().compute(parts, camera, settings)
