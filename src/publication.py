"""Detached 3D envelopes, CPU visibility, and editable publication contours.

Only geometry is rasterized internally. The returned artwork consists entirely
of finite vector paths; no raster image is embedded in Publication exports.
"""


from collections import OrderedDict, defaultdict


from functools import lru_cache


from hashlib import blake2b


import numpy as np


from scipy import ndimage


def _geometry():
    from . import geometry
    return geometry


def _finite_coordinates(value):
    return _geometry()._coordinates(value)


@lru_cache(maxsize=16)
def _unit_sphere(sides, rows):
    theta = np.linspace(0, np.pi, rows + 1)
    phi = np.arange(sides) * 2 * np.pi / sides
    normals = np.array([[np.sin(t)*np.cos(p), np.sin(t)*np.sin(p), np.cos(t)] for t in theta for p in phi])
    triangles = []
    for row in range(rows):
        for side in range(sides):
            a, b = row*sides+side, row*sides+(side+1) % sides
            triangles.extend(((a, b, a+sides), (b, b+sides, a+sides)))
    return normals, np.asarray(triangles, dtype=np.int32)


def _sphere(center, radius, sides=10, rows=6):
    normals, triangles = _unit_sphere(sides, rows)
    # Endpoint caps share topology, but each detached mesh owns its arrays.
    return normals*radius + center, normals.copy(), triangles.copy()


def _combine(meshes):
    vertices, normals, triangles, patches = [], [], [], []
    offset = 0
    for mesh in meshes:
        vertices.append(mesh['vertices']); normals.append(mesh['normals'])
        triangles.append(mesh['triangles'] + offset)
        patches.append(mesh['patch_ids'])
        offset += len(mesh['vertices'])
    if not vertices:
        return np.empty((0, 3)), np.empty((0, 3)), np.empty((0, 3), dtype=np.int32), np.empty(0, dtype=np.int32)
    return np.vstack(vertices), np.vstack(normals), np.vstack(triangles), np.concatenate(patches)


def _mesh(identifier, role, vertices, normals, triangles, patches=0):
    triangles = np.asarray(triangles, dtype=np.int32)
    return {'id': identifier, 'role': role, 'vertices': np.asarray(vertices, dtype=np.float32),
            'normals': np.asarray(normals, dtype=np.float32), 'triangles': triangles,
            'patch_ids': np.full(len(triangles), patches, dtype=np.int32) if np.isscalar(patches) else np.asarray(patches, dtype=np.int32)}


def _mesh_bounds(meshes, camera):
    g = _geometry()
    if not meshes:
        return g._bounds([])
    # Transform all envelope vertices in one batch. Triangle clipping is only
    # needed when a perspective envelope straddles the camera's near plane.
    vertices = np.concatenate([mesh['vertices'] for mesh in meshes])
    points = g._transform(vertices, camera['matrix'])
    if camera['perspective']:
        inside = points[:, 2] <= -camera['near_clip']
        if not inside.all():
            if not inside.any():
                points = points[:0]
            else:
                triangles, offset = [], 0
                for mesh in meshes:
                    triangles.append(mesh['triangles']+offset)
                    offset += len(mesh['vertices'])
                triangles = np.concatenate(triangles)
                clipped, _ = _clip_triangles(points, triangles, np.arange(len(triangles)), camera['near_clip'])
                points = clipped.reshape(-1, 3)
    return g._bounds([g._project(points, camera)])


def _frame(bounds, settings, camera):
    explicit = settings.get('frame')
    if explicit is not None:
        frame = np.asarray(explicit, dtype=float)
        if frame.shape == (4,) and np.isfinite(frame).all() and np.all(frame[2:] > frame[:2]):
            bounds = frame
    elif camera['match']:
        bounds = [-1., -1/camera['aspect'], 1., 1/camera['aspect']]
    width = max(1., _geometry()._number(settings.get('width'), 1600))
    height = max(1., _geometry()._number(settings.get('height'), 1200))
    stroke = max(0., _geometry()._number(settings.get('stroke_width'), 2.5)) if settings.get('outline', True) else 0.
    margin = min(.25, max(0., _geometry()._number(settings.get('margin_fraction'), 0)))
    padding = min(min(width, height)/4, stroke/2+2) + margin*min(width, height)
    padding = 0. if camera['match'] else min(min(width, height)/2-.5, padding)
    x0, y0, x1, y1 = bounds
    units = max(max(x1-x0, .001)/max(1., width-2*padding), max(y1-y0, .001)/max(1., height-2*padding))
    cx, cy = (x0+x1)/2, (y0+y1)/2
    return [cx-width*units/2, cy-height*units/2, cx+width*units/2, cy+height*units/2]


def _resolution(frame, settings, parts):
    requested = int(settings.get('publication_resolution', 640 if settings.get('_render_quality') != 'export' else 1500))
    longest = max(128, min(2048, requested))
    # Depth+patch maps are four bytes each. Bound aggregate maps, including
    # assemblies with hundreds of independent editable parts.
    longest = min(longest, max(128, int(np.sqrt(18_000_000/max(1, parts)))))
    width, height = max(frame[2]-frame[0], 1e-8), max(frame[3]-frame[1], 1e-8)
    scale = longest/max(width, height)
    return max(16, int(np.ceil(width*scale))), max(16, int(np.ceil(height*scale)))


def _clip_triangles(vertices, triangles, patches, near):
    camera_triangles = vertices[triangles]
    inside = camera_triangles[:, :, 2] <= -near
    accepted = camera_triangles[inside.all(axis=1)]
    ids = patches[inside.all(axis=1)]
    added, added_ids = [], []
    for triangle, patch in zip(camera_triangles[inside.any(axis=1) & ~inside.all(axis=1)], patches[inside.any(axis=1) & ~inside.all(axis=1)]):
        polygon = []
        for a, b in zip(triangle, np.roll(triangle, -1, axis=0)):
            ia, ib = a[2] <= -near, b[2] <= -near
            if ia:
                polygon.append(a)
            if ia != ib:
                point = a + ((-near-a[2])/(b[2]-a[2]))*(b-a)
                point[2] = -near
                polygon.append(point)
        for i in range(1, len(polygon)-1):
            added.append((polygon[0], polygon[i], polygon[i+1])); added_ids.append(patch)
    if added:
        accepted = np.concatenate((accepted, np.asarray(added)))
        ids = np.r_[ids, added_ids]
    return accepted, ids


@lru_cache(maxsize=1)
def _native_depth_renderer():
    """Use ChimeraX's compiled scan converter without a graphics context."""
    try:
        from chimerax.mask.mask_cpp import surface_z_depth
    except ImportError:
        return None
    return surface_z_depth


def _rasterize(meshes, camera, frame, width, height):
    """Batched triangle rasterization with perspective-correct depth."""
    g = _geometry()
    depth = np.full((height, width), -np.inf, dtype=np.float32)
    labels = np.full((height, width), -1, dtype=np.int32)
    faces = np.full((height, width), -1, dtype=np.int32)
    vertices, _, triangles, patches = _combine(meshes)
    if not len(triangles):
        return depth, labels, faces, (vertices, triangles)
    transformed = g._transform(vertices, camera['matrix'])
    if camera['perspective']:
        xyz, face_ids = _clip_triangles(transformed, triangles, np.arange(len(triangles)), camera['near_clip'])
    else:
        xyz = transformed[triangles]
        face_ids = np.arange(len(triangles))
    if not len(xyz):
        return depth, labels, faces, (vertices, triangles)
    finite = np.isfinite(xyz).all(axis=(1, 2))
    xyz, face_ids = xyz[finite], face_ids[finite]
    if not len(xyz):
        return depth, labels, faces, (transformed, triangles)
    xy = g._project(xyz.reshape(-1, 3), camera).reshape(-1, 3, 2)
    xy = (xy - np.asarray(frame[:2])) * np.asarray([width/(frame[2]-frame[0]), height/(frame[3]-frame[1])])
    renderer = _native_depth_renderer()
    if renderer is not None:
        # The native renderer samples integer coordinates and finds minimum
        # depth. Shift by half a cell to match our pixel centers. Reciprocal
        # camera z interpolates linearly after perspective projection and
        # preserves near/far ordering; orthographic cameras use negated z.
        projected = np.empty((len(xyz)*3, 3), dtype=np.float64)
        projected[:, :2] = xy.reshape(-1, 2)-.5
        projected[:, 2] = ((1/xyz[:, :, 2]) if camera['perspective'] else -xyz[:, :, 2]).ravel()
        native_triangles = np.arange(len(projected), dtype=np.int32).reshape(-1, 3)
        depth.fill(np.inf)
        renderer(projected, native_triangles, depth, faces)
        valid = faces >= 0
        faces[valid] = face_ids[faces[valid]]
        labels[valid] = patches[faces[valid]]
        if camera['perspective']:
            np.reciprocal(depth, out=depth, where=valid)
        else:
            np.negative(depth, out=depth)
        depth[~valid] = -np.inf
        return depth, labels, faces, (transformed, triangles)
    minimum = np.maximum(np.floor(xy.min(axis=1)).astype(np.int64), (0, 0))
    maximum = np.minimum(np.ceil(xy.max(axis=1)).astype(np.int64), (width-1, height-1))
    sizes = maximum-minimum+1
    good = np.all(sizes > 0, axis=1) & np.isfinite(xy).all(axis=(1, 2))
    xy, xyz, face_ids, minimum, maximum, sizes = (a[good] for a in (xy, xyz, face_ids, minimum, maximum, sizes))
    buckets = np.ceil(np.log2(np.maximum(sizes, 1))).astype(int)
    groups = defaultdict(list)
    for index, shape in enumerate(buckets):
        groups[tuple(shape)].append(index)
    flat_depth, flat_faces = depth.ravel(), faces.ravel()
    for (bx, by), indices in groups.items():
        bw, bh = 1 << int(bx), 1 << int(by)
        gy, gx = np.mgrid[:bh, :bw]
        gx, gy = gx.ravel(), gy.ravel()
        batch_size = max(1, 180_000//len(gx))
        for begin in range(0, len(indices), batch_size):
            select = np.asarray(indices[begin:begin+batch_size])
            x = minimum[select, 0, None]+gx; y = minimum[select, 1, None]+gy
            a, b, c = (xy[select, i] for i in range(3))
            denominator = (b[:, 1]-c[:, 1])*(a[:, 0]-c[:, 0]) + (c[:, 0]-b[:, 0])*(a[:, 1]-c[:, 1])
            valid_face = np.abs(denominator) > 1e-10
            denominator = np.where(valid_face, denominator, 1)[:, None]
            w0 = ((b[:, 1]-c[:, 1])[:, None]*(x+.5-c[:, 0, None]) + (c[:, 0]-b[:, 0])[:, None]*(y+.5-c[:, 1, None]))/denominator
            w1 = ((c[:, 1]-a[:, 1])[:, None]*(x+.5-c[:, 0, None]) + (a[:, 0]-c[:, 0])[:, None]*(y+.5-c[:, 1, None]))/denominator
            w2 = 1-w0-w1
            mask = (valid_face[:, None] & (w0 >= -1e-7) & (w1 >= -1e-7) & (w2 >= -1e-7)
                    & (x <= maximum[select, 0, None]) & (y <= maximum[select, 1, None]))
            if not mask.any():
                continue
            z = xyz[select, :, 2]
            if camera['perspective']:
                values = 1/(w0/z[:, 0, None]+w1/z[:, 1, None]+w2/z[:, 2, None])
            else:
                values = w0*z[:, 0, None]+w1*z[:, 1, None]+w2*z[:, 2, None]
            row, col = np.nonzero(mask)
            pixels = (y[row, col]*width+x[row, col]).astype(np.int64)
            values = values[row, col].astype(np.float32)
            np.maximum.at(flat_depth, pixels, values)
            winners = values == flat_depth[pixels]
            flat_faces[pixels[winners]] = face_ids[select[row[winners]]]
    valid = faces >= 0
    labels[valid] = patches[faces[valid]]
    return depth, labels, faces, (transformed, triangles)


def _visible_masks(depth_maps, line_flags, opaque_flags, shape):
    opaque = np.full(shape, -np.inf, dtype=np.float32)
    for depth, is_opaque in zip(depth_maps, opaque_flags):
        if is_opaque:
            np.maximum(opaque, depth, out=opaque)
    masks = [np.isfinite(depth) & (True if is_line else depth >= opaque-1e-6)
             for depth, is_line in zip(depth_maps, line_flags)]
    return opaque, masks


def _remove_small_pieces(depth_maps, masks, opaque, line_flags, opaque_flags, minimum):
    """Remove detached visible islands without leaving invisible occluders.

    Area is relative to each component's largest visible island. Four-neighbor
    connectivity matches the contour tracer, and strict comparison preserves
    every largest island even at a threshold of one. Backbones are untouched.
    """
    if minimum <= 0:
        return opaque, masks
    pending = [i for i, is_line in enumerate(line_flags) if not is_line]
    while pending:
        changed_opaque = False
        for i in pending:
            labels, count = ndimage.label(masks[i])
            if count < 2:
                continue
            sizes = np.bincount(labels.ravel())
            sizes[0] = 0
            rejected = sizes < minimum*sizes.max()
            rejected[0] = False
            if not rejected.any():
                continue
            discarded = rejected[labels]
            depth_maps[i][discarded] = -np.inf
            masks[i][discarded] = False
            changed_opaque |= opaque_flags[i]
        if not changed_opaque:
            break
        # Clearing depth reveals the actual surfaces behind a removed piece.
        # Revisit only components whose visible area changed. A later pass may
        # expose more small islands, so finish at a fixed point rather than
        # imposing an arbitrary layer limit. Each changing pass permanently
        # removes finite depth pixels; the process cannot cycle.
        opaque, revealed = _visible_masks(depth_maps, line_flags, opaque_flags, opaque.shape)
        pending = [i for i, is_line in enumerate(line_flags)
                   if not is_line and not np.array_equal(masks[i], revealed[i])]
        masks = revealed
    return opaque, masks


def _mask_rings(mask):
    """Trace cell boundaries, retaining holes and diagonal-only components."""
    rows, columns = np.flatnonzero(mask.any(axis=1)), np.flatnonzero(mask.any(axis=0))
    if not len(rows):
        return []
    y0, x0 = rows[0], columns[0]
    mask = mask[y0:rows[-1]+1, x0:columns[-1]+1]
    height, width = mask.shape
    above = np.zeros_like(mask); above[1:] = mask[:-1]
    below = np.zeros_like(mask); below[:-1] = mask[1:]
    left = np.zeros_like(mask); left[:, 1:] = mask[:, :-1]
    right = np.zeros_like(mask); right[:, :-1] = mask[:, 1:]
    starts, ends, directions = [], [], []
    stride = width+1
    for condition, offset, direction in ((mask & ~above, (0, 0), (1, 0)), (mask & ~right, (1, 0), (0, 1)),
                                          (mask & ~below, (1, 1), (-1, 0)), (mask & ~left, (0, 1), (0, -1))):
        y, x = np.nonzero(condition)
        start = (y+offset[1])*stride+x+offset[0]
        starts.append(start)
        ends.append(start+direction[0]+direction[1]*stride)
        directions.append(np.full(len(start), len(directions), dtype=np.int8))
    starts, ends, directions = map(np.concatenate, (starts, ends, directions))
    if not len(starts):
        return []
    # Each boundary vertex has one outgoing edge, or two at a diagonal
    # contact. Resolve connectivity in arrays rather than constructing several
    # Python tuples and dictionary entries for every edge on every frame.
    order = np.argsort(starts, kind='stable')
    ordered_starts = starts[order]
    first = np.searchsorted(ordered_starts, ends)
    last = np.searchsorted(ordered_starts, ends, side='right')
    successors = order[first]
    alternatives = np.full(len(starts), -1, dtype=np.int64)
    junctions = np.flatnonzero(last-first == 2)
    if len(junctions):
        other = order[first[junctions]+1]
        preference = np.array([1, 0, 3, 2])
        a = preference[(directions[successors[junctions]]-directions[junctions]) % 4]
        b = preference[(directions[other]-directions[junctions]) % 4]
        preferred = np.where(a <= b, successors[junctions], other)
        alternatives[junctions] = np.where(a <= b, other, successors[junctions])
        successors[junctions] = preferred
    used = np.zeros(len(starts), dtype=bool)
    rings = []
    for first in range(len(starts)):
        if used[first]:
            continue
        current, ring = first, []
        while not used[current]:
            used[current] = True
            ring.append(starts[current])
            if ends[current] == starts[first]:
                break
            next_edge = successors[current]
            if used[next_edge]:
                next_edge = alternatives[current]
                if next_edge < 0 or used[next_edge]:
                    ring = []
                    break
            current = next_edge
        if len(ring) >= 3:
            ids = np.asarray(ring)
            rings.append(np.column_stack((ids % stride+x0, ids // stride+y0)).astype(float))
    return rings


def _cubic_ring(ring, smoothing=.65):
    """Fit smooth, continuous tangents instead of rounding tiny grid corners."""
    g = _geometry()
    amount = g._smoothing(smoothing, .65)
    return g._cubic_ring(ring, amount, .5+1.5*amount)


def _smooth_boundary(ring, smoothing, resolution):
    """Remove raster knots before fitting, with a bounded image-space change."""
    if len(ring) <= 8:
        return ring
    filtered = .5*ring+.25*np.roll(ring, 1, axis=0)+.25*np.roll(ring, -1, axis=0)
    amount = _geometry()._smoothing(smoothing, .65)
    scale = resolution/640.
    sigma = 2.2*amount*scale
    # Leave small cavities, islands and narrow fragments alone. The window
    # scales with the render grid so settling/exporting does not reveal knots
    # which were absent while dragging at a lower preview resolution.
    if sigma <= .25 or len(ring) <= 24 or np.min(np.ptp(ring, axis=0)) <= max(4., 4*sigma):
        return filtered
    softened = ndimage.gaussian_filter1d(filtered, sigma, axis=0, mode='wrap')
    displacement = softened-filtered
    distance = np.linalg.norm(displacement, axis=1)
    limit = 1.2*amount*scale
    return filtered+displacement*np.minimum(1., limit/np.maximum(distance, 1e-15))[:, None]


def _partition_curve(ring, smoothing):
    """Keep artificial paint boundaries conservative and free of new bulges.

    These un-stroked regions divide a translucent surface by depth order.
    Interpolating their edges with silhouette tangents would overlap adjacent
    regions and apply alpha twice. Keep their established small corner cuts;
    actual outlines are fitted separately from the complete silhouette.
    """
    before, after = np.roll(ring, 1, axis=0), np.roll(ring, -1, axis=0)
    dl, dr = before-ring, after-ring
    amount = _geometry()._unit(smoothing, .65)
    left = ring+dl*np.minimum(amount/np.maximum(np.linalg.norm(dl, axis=1), 1e-15), .25)[:, None]
    right = ring+dr*np.minimum(amount/np.maximum(np.linalg.norm(dr, axis=1), 1e-15), .25)[:, None]
    end = np.roll(left, -1, axis=0)
    straight = np.concatenate((right+(end-right)/3, right+2*(end-right)/3, end), axis=1)
    if not amount:
        return {'start': left[0], 'segments': straight}
    rounded = np.concatenate((left+(ring-left)*2/3, right+(ring-right)*2/3, right), axis=1)
    segments = np.empty((2*len(ring), 6), dtype=straight.dtype)
    segments[::2], segments[1::2] = rounded, straight
    return {'start': left[0], 'segments': segments}


def _paths(mask, frame, width, height, smoothing=.65, partition=False):
    g = _geometry()
    # Remove subpixel staircase noise before corner fitting. A tolerance below
    # half a pixel preserves essentially every grid turn and can overflow Qt's
    # path parser on a moderately sized nucleic acid illustration.
    pixel_rings = []
    for ring in _mask_rings(mask):
        filtered = _smooth_boundary(ring, 0. if partition else smoothing, max(width, height))
        pixel_rings.append(g._simplify_ring(filtered, .8))
    scale = np.asarray([(frame[2]-frame[0])/width, (frame[3]-frame[1])/height])
    origin = np.asarray(frame[:2])
    rings, cubics = [], []
    for ring in pixel_rings:
        rings.append((ring*scale+origin).tolist())
        curve = _partition_curve(ring, smoothing) if partition else _cubic_ring(ring, smoothing)
        cubics.append({'start': (curve['start']*scale+origin).tolist(),
                       'segments': (curve['segments'].reshape(-1, 3, 2)*scale+origin).reshape(-1, 6).tolist()})
    return rings, cubics, pixel_rings


def _outline_rank_lookup(rank_map):
    """Assign fitted outline samples to their nearest visible paint region."""
    height, width = rank_map.shape
    boundary_tree = boundary_ranks = None

    def lookup(samples):
        nonlocal boundary_tree, boundary_ranks
        x, y = np.floor(samples).astype(int).T
        ranks = np.full(len(samples), -1, dtype=np.int16)
        for dx, dy in ((-1, -1), (0, -1), (-1, 0), (0, 0)):
            xx, yy = x+dx, y+dy
            valid = (xx >= 0) & (xx < width) & (yy >= 0) & (yy < height)
            ranks[valid] = np.maximum(ranks[valid], rank_map[yy[valid], xx[valid]])
        missing = ranks < 0
        if missing.any():
            # Smoothing and simplification can place a valid silhouette just
            # outside its raster mask. Its outline must remain continuous.
            # Query boundary pixels only, and construct the spatial index at
            # most once per part, instead of a whole-frame distance transform.
            if boundary_tree is None:
                from scipy.spatial import cKDTree
                mask = rank_map >= 0
                yy, xx = np.nonzero(mask & ~ndimage.binary_erosion(mask))
                boundary_ranks = rank_map[yy, xx]
                boundary_tree = cKDTree(np.column_stack((xx+.5, yy+.5)))
            if len(boundary_ranks):
                _, nearest = boundary_tree.query(samples[missing])
                ranks[missing] = boundary_ranks[nearest]
        return ranks
    return lookup


def _cubic_interval(points, start, end):
    """Keep a parameter interval of a cubic without approximating its shape."""
    def split(control, t):
        a = control[:-1]*(1-t)+control[1:]*t
        b = a[:-1]*(1-t)+a[1:]*t
        c = b[0]*(1-t)+b[1]*t
        return np.array([control[0], a[0], b[0], c]), np.array([c, b[1], a[2], control[3]])
    if end < 1:
        points = split(points, end)[0]
    if start > 0:
        points = split(points, start/end)[1]
    return points


def _ranked_curves(pixel_rings, rank_map, frame, width, height, smoothing, rank_lookup=None):
    """Partition the original smooth silhouette, retaining its exact cubics.

    Pixel samples determine paint order only. At each change of order, De
    Casteljau subdivision cuts the curve without turning it into a polyline.
    """
    grouped = defaultdict(list)
    rank_lookup = rank_lookup or _outline_rank_lookup(rank_map)
    scale = np.asarray([(frame[2]-frame[0])/width, (frame[3]-frame[1])/height])
    origin = np.asarray(frame[:2])
    for ring in pixel_rings:
        curve = _cubic_ring(ring, smoothing)
        segments = curve['segments'].reshape(-1, 3, 2)
        starts = np.vstack((curve['start'], segments[:-1, -1]))
        controls = np.concatenate((starts[:, None], segments), axis=1)
        # The control polygon bounds the curve length. Batch the rank lookups
        # so opacity does not add a Python loop for every outline pixel.
        counts = np.maximum(1, np.ceil(np.linalg.norm(np.diff(controls, axis=1), axis=2).sum(axis=1)).astype(int))
        offsets = np.r_[0, np.cumsum(counts)]
        owners = np.repeat(np.arange(len(controls)), counts)
        t = ((np.arange(offsets[-1])-offsets[owners]+.5)/counts[owners])[:, None]
        q = controls[owners]
        samples = (1-t)**3*q[:, 0]+3*(1-t)**2*t*q[:, 1]+3*(1-t)*t**2*q[:, 2]+t**3*q[:, 3]
        ranks = rank_lookup(samples)
        runs = []
        current, current_rank = [], -1
        for index, control in enumerate(controls):
            local = ranks[offsets[index]:offsets[index+1]]
            changes = np.r_[0, np.flatnonzero(local[1:] != local[:-1])+1, len(local)]
            for first, last in zip(changes[:-1], changes[1:]):
                rank = int(local[first])
                if rank != current_rank:
                    if current:
                        runs.append((current_rank, current))
                    current, current_rank = [], rank
                current.append(_cubic_interval(control, first/len(local), last/len(local)))
        if current:
            runs.append((current_rank, current))
        closed = len(runs) == 1
        # A ring's arbitrary start must not create an extra cap in a stroke.
        if len(runs) > 1 and runs[0][0] == runs[-1][0]:
            runs[0] = (runs[0][0], runs[-1][1]+runs[0][1])
            runs.pop()
        for rank, controls in runs:
            if rank < 0:
                continue
            controls = np.asarray(controls)
            grouped[rank].append({'start': (controls[0, 0]*scale+origin).tolist(),
                                  'segments': (controls[:, 1:]*scale+origin).reshape(-1, 6).tolist(),
                                  'closed': closed})
    return grouped


def _internal_boundary_lookup(index, masks, owners, siblings):
    """Classify exact silhouette edges before fitting smooth outline curves.

    The outward pixel identifies what is across each edge.  Reciprocal edges
    between two visible regions have one owner; a translucent outline covered
    by a sibling is its own boundary and must not disappear merely because the
    sibling's part index is larger.
    """
    from scipy.spatial import cKDTree
    mask = masks[index]
    height, width = mask.shape
    points, styles = [], []
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        adjacent = np.zeros_like(mask)
        ys = slice(max(0, -dy), min(height, height-dy))
        xs = slice(max(0, -dx), min(width, width-dx))
        adjacent[ys, xs] = mask[slice(max(0, dy), min(height, height+dy)),
                                 slice(max(0, dx), min(width, width+dx))]
        yy, xx = np.nonzero(mask & ~adjacent)
        if not len(yy):
            continue
        points.append(np.column_stack((xx+.5+.5*dx, yy+.5+.5*dy)))
        labels = np.zeros(len(yy), dtype=np.int8)  # 0 solid, 1 dashed, 2 omit duplicate
        nx, ny = xx+dx, yy+dy
        valid = (nx >= 0) & (nx < width) & (ny >= 0) & (ny < height)
        neighbours = np.full(len(yy), -1, dtype=np.int32)
        neighbours[valid] = owners[ny[valid], nx[valid]]
        for sibling in siblings:
            match = neighbours == sibling
            if not match.any():
                continue
            labels[match] = 1
            # The neighbouring outline repeats this exact pixel edge only
            # when its mask is absent on our side and present on its side.
            if sibling > index:
                reciprocal = ~masks[sibling][yy[match], xx[match]]
                labels[np.flatnonzero(match)[reciprocal]] = 2
        styles.append(labels)
    if not points:
        return lambda samples: np.zeros(len(samples), dtype=np.int8)
    tree = cKDTree(np.concatenate(points))
    labels = np.concatenate(styles)

    def lookup(samples):
        _, nearest = tree.query(samples)
        return labels[nearest]
    return lookup


def _backbone_depth(lines, frame, width, height, radius, perspective):
    """Depth only: screen-space capsules enclosing the complete vector stroke.

    The proxy is never painted. Its round joins include a small guard band;
    clipping labels are extended beyond it before export, so proxy facets can
    never trim the true constant-width stroke or its rounded ends.
    """
    points = np.concatenate(lines) if lines else np.empty((0, 3))
    if not len(points):
        return np.full((height, width), -np.inf, dtype=np.float32)
    offsets = np.cumsum([0]+[len(line) for line in lines])
    edges = np.concatenate([np.column_stack((np.arange(start, end-1), np.arange(start+1, end)))
                            for start, end in zip(offsets[:-1], offsets[1:])])
    z = -1/points[:, 2] if perspective else points[:, 2]
    radius += 2*max((frame[2]-frame[0])/width, (frame[3]-frame[1])/height)
    sides = 8
    angles = np.arange(sides)*2*np.pi/sides
    unit = np.column_stack((np.cos(angles), np.sin(angles)))
    xy = points[:, None, :2]+radius/np.cos(np.pi/sides)*unit
    centers = np.column_stack((points[:, 0], -points[:, 1], z))
    circles = np.concatenate((xy*np.array([1., -1.]), np.broadcast_to(z[:, None, None], (len(points), sides, 1))), axis=2)
    vertices = [np.concatenate((centers[:, None], circles), axis=1).reshape(-1, 3)]
    base = np.arange(len(points))[:, None]*(sides+1)
    triangles = [np.stack((np.broadcast_to(base, (len(points), sides)), base+1+np.arange(sides),
                          base+1+(np.arange(sides)+1)%sides), axis=2).reshape(-1, 3)]
    if len(edges):
        a, b = points[edges[:, 0]], points[edges[:, 1]]
        direction = b[:, :2]-a[:, :2]
        length = np.linalg.norm(direction, axis=1)
        normal = np.column_stack((-direction[:, 1], direction[:, 0]))/np.maximum(length[:, None], 1e-15)*radius
        xy = np.stack((a[:, :2]+normal, a[:, :2]-normal, b[:, :2]-normal, b[:, :2]+normal), axis=1)
        depth = z[edges[:, [0, 0, 1, 1]]]
        rectangles = np.concatenate((xy*np.array([1., -1.]), depth[:, :, None]), axis=2).reshape(-1, 3)
        offset = len(vertices[0])+np.arange(len(edges))*4
        triangles.append((offset[:, None, None]+np.array([[0, 1, 2], [0, 2, 3]])).reshape(-1, 3))
        vertices.append(rectangles)
    mesh = _mesh('backbone-depth', 'surface', np.concatenate(vertices), np.empty((0, 3)), np.concatenate(triangles))
    camera, _ = _geometry()._camera_parameters(None, False)
    depth = _rasterize([mesh], camera, frame, width, height)[0]
    if perspective:
        valid = np.isfinite(depth)
        depth[valid] = -1/depth[valid]
    return depth


def _clip_rings(rank_map, rank, frame, width, height):
    """Exact shared partition edges, without an artificial outer stroke edge."""
    missing = rank_map < 0
    if missing.any() and not missing.all():
        nearest = ndimage.distance_transform_edt(missing, return_distances=False, return_indices=True)
        rank_map = rank_map[tuple(nearest)]
    scale = np.asarray([(frame[2]-frame[0])/width, (frame[3]-frame[1])/height])
    origin = np.asarray(frame[:2])
    result = []
    for ring in _mask_rings(rank_map == rank):
        # Remove only exactly collinear grid points. Neighboring clip regions
        # retain identical shared edges; approximating them separately can
        # create gaps or double alpha at a depth-order change.
        before, after = ring-np.roll(ring, 1, axis=0), np.roll(ring, -1, axis=0)-ring
        keep = before[:, 0]*after[:, 1] != before[:, 1]*after[:, 0]
        result.append((ring[keep]*scale+origin).tolist())
    return result


def _lighting_masks(mask, highlight_size=1., shadow_size=1.):
    """Directional shadow rim and upper streak, before scene occlusion."""
    if highlight_size <= 0 and shadow_size <= 0:
        return []
    rows, columns = np.flatnonzero(mask.any(axis=1)), np.flatnonzero(mask.any(axis=0))
    if not len(rows):
        return []
    y0, y1, x0, x1 = rows[0], rows[-1]+1, columns[0], columns[-1]+1
    original = mask[y0:y1, x0:x1]
    filled = ndimage.binary_fill_holes(original)
    # Holes do not create extra glossy rings. An edge cut by the camera frame
    # is not a molecule contour either; extend only those border samples.
    padded = np.pad(filled, 1)
    if y0 == 0:
        padded[0, 1:-1] = filled[0]
    if y1 == mask.shape[0]:
        padded[-1, 1:-1] = filled[-1]
    if x0 == 0:
        padded[1:-1, 0] = filled[:, 0]
    if x1 == mask.shape[1]:
        padded[1:-1, -1] = filled[:, -1]
    boundary = filled & ~ndimage.binary_erosion(padded)[1:-1, 1:-1]
    if not boundary.any():
        return []
    distance = ndimage.distance_transform_edt(~boundary)
    distance[~filled] = 0
    radius = float(distance.max())
    if radius < 6:
        return []
    softened = ndimage.gaussian_filter(distance, max(.8, .025*radius), mode='nearest')
    dy, dx = np.gradient(softened)
    facing = (.35*dx+.94*dy)/np.maximum(np.hypot(dx, dy), 1e-8)
    # Keep the main colour untouched. Only surfaces facing away from the
    # highlight receive a solid shadow overlay, tapering around the sides.
    shadow_width = shadow_size*.24*radius*np.clip(-facing, 0., 1.)**1.2
    # Smooth the interior light/shadow boundary before tracing it. Filtering
    # this field, rather than the finished path, leaves the molecule contour,
    # holes and subsequent depth partitions constrained by their own masks.
    shadow = np.zeros_like(original)
    if shadow_size > 0:
        shadow_field = ndimage.gaussian_filter(shadow_width-distance, max(1., .06*radius), mode='nearest')
        shadow = original & (shadow_field > 0)
    strength = np.clip((facing-.85)/.15, 0., 1.)
    strength = strength*strength*(3-2*strength)
    upper = np.clip((.65*len(original)-np.arange(len(original)))/max(1., .15*len(original)), 0., 1.)
    half_width = highlight_size*max(.55, .035*radius)*strength*upper[:, None]
    highlight = original & (np.abs(softened-.12*radius) < half_width)
    labels, _ = ndimage.label(highlight, np.ones((3, 3), dtype=bool))
    sizes = np.bincount(labels.ravel())
    sizes[0] = 0
    highlight &= sizes[labels] >= max(6, .5*radius)
    result = []
    for role, local in (('shadow', shadow), ('highlight', highlight)):
        if local.any():
            complete = np.zeros_like(mask)
            complete[y0:y1, x0:x1] = local
            result.append((role, complete))
    return result


class PublicationEngine:
    """One renderer: flat molecular envelopes and editable backbone strokes."""
    def __init__(self):
        self.clear()

    def clear(self):
        self._parts = None
        self._mesh_cache = OrderedDict()
        self._curves = OrderedDict()
        self._surface_shapes = OrderedDict()
        self._results = OrderedDict()

    def _surface(self, part, abstraction):
        coords = _finite_coordinates(part.get('coords'))
        finite = np.isfinite(coords).all(axis=1)
        radii = np.asarray(part.get('atom_radii', np.full(len(coords), 1.7)), dtype=float)
        if radii.shape != (len(coords),):
            radii = np.full(len(coords), 1.7)
        coords = coords[finite]; radii = np.clip(np.nan_to_num(radii[finite], nan=1.7), .4, 4.)
        if not len(coords):
            return []
        center = (coords.min(axis=0)+coords.max(axis=0))/2
        centered = (coords-center).astype(np.float32)
        # Polymer envelopes broaden with simplification; small ligands and
        # ions retain compact envelopes.
        polymer = part.get('kind') != 'other'
        abstraction = min(1.7 if polymer else 1., max(0., abstraction))
        extra = min(1., max(0., (abstraction-.65)/.35)) if polymer else 0.
        extended = max(0., (abstraction-1.)/.7) if polymer else 0.
        resolution = 3.2 + (14 if polymer else 5)*min(abstraction, 1.) + 16.8*extra**2 + 36*extended
        signature = (blake2b(np.round(centered, 3).tobytes()+radii.astype(np.float32).tobytes(), digest_size=16).digest(), resolution)
        cached = self._surface_shapes.get(signature)
        if cached is None:
            try:
                from chimerax.surface.gaussian import gaussian_surface
                # Broad envelopes need more samples as their silhouette loses
                # molecular detail: otherwise each mesh edge becomes visible
                # in print. Grow sampling and its bounded grid budget together
                # with increasing simplification; the scene mesh is cached.
                spacing = resolution/(6+8*extra+14*extended)
                lengths = np.ptp(centered, axis=0)+6*resolution
                cells = np.prod(np.maximum(lengths/spacing, 1))
                cell_budget = 1_500_000+4_500_000*extended
                spacing *= max(1., (cells/cell_budget)**(1/3))
                vertices, normals, triangles, _ = gaussian_surface(centered, (radii/1.7).astype(np.float32)**3,
                                                                   resolution, grid_spacing=spacing)
                cached = (vertices, normals, triangles)
            except ImportError:
                # A deterministic 3D atom envelope for pure NumPy/SciPy hosts.
                spheres = [_mesh(part['id'], 'surface', *_sphere(p, r + abstraction*.6)) for p, r in zip(centered, radii)]
                vertices, normals, triangles, _ = _combine(spheres)
                cached = (vertices, normals, triangles)
            self._surface_shapes[signature] = cached
            if len(self._surface_shapes) > 64:
                self._surface_shapes.popitem(last=False)
        vertices, normals, triangles = cached
        return [_mesh(part['id'], 'surface', vertices+center, normals, triangles)]

    def publication_meshes(self, parts, settings, camera=None):
        if parts is not self._parts:
            self._parts = parts
            self._mesh_cache.clear(); self._curves.clear(); self._results.clear()
        g = _geometry()
        meshes = []
        for part in parts:
            if part.get('mode') == 'line' or not part.get('render_visible', part.get('visible', True)) or float(part.get('render_opacity', 1)) <= 0:
                continue
            abstraction = g._abstraction(settings.get('abstraction'), .65)
            smoothing = g._smoothing(settings.get('smoothing'), .65)
            if part.get('kind') == 'other':
                abstraction, smoothing = min(abstraction, 1.), min(smoothing, 1.)
            key = (part['id'], abstraction, smoothing)
            if key not in self._mesh_cache:
                built = self._surface(part, abstraction)
                if part.get('kind') != 'other' and smoothing > .65:
                    from .mesh_smoothing import smooth_mesh
                    rounded = []
                    for mesh in built:
                        vertices, normals = smooth_mesh(mesh['vertices'], mesh['normals'], mesh['triangles'], (smoothing-.65)/.35)
                        rounded.append(dict(mesh, vertices=vertices, normals=normals))
                    built = rounded
                self._mesh_cache[key] = built
                if len(self._mesh_cache) > max(64, 2*len(parts)):
                    self._mesh_cache.popitem(last=False)
            meshes.extend(self._mesh_cache[key])
        return meshes

    def compute(self, parts, camera=None, settings=None):
        settings = settings or {}
        g = _geometry()
        parameters, camera_key = g._camera_parameters(camera, settings.get('match_projection', False))
        meshes = self.publication_meshes(parts, settings, camera)
        visible = [part for part in parts if part.get('render_visible', part.get('visible', True)) and float(part.get('render_opacity', 1)) > 0]
        from .state import BACKBONE_WIDTH
        line_width = BACKBONE_WIDTH
        outlines = max(0., g._number(settings.get('stroke_width'), 2.5)) if settings.get('outline', True) else 0.
        projected = {}
        for part in visible:
            if part.get('mode') != 'line':
                continue
            key = (part['id'], g._abstraction(settings.get('abstraction'), .65), g._smoothing(settings.get('smoothing'), .65))
            if key not in self._curves:
                self._curves[key] = g._backbone_curves(part, key[2], key[1])
                if len(self._curves) > max(64, 2*len(parts)):
                    self._curves.popitem(last=False)
            projected[part['id']] = g._project_backbone(self._curves[key], parameters)
        paths = [line[:, :2] for lines in projected.values() for line in lines]
        mesh_bounds = _mesh_bounds(meshes, parameters) if meshes else None
        extents = ([np.asarray(mesh_bounds).reshape(2, 2)] if mesh_bounds is not None else [])+paths
        bounds = g._bounds(extents)
        frame = _frame(bounds, settings, parameters)
        if paths and not parameters['match'] and settings.get('frame') is None:
            line_bounds = np.asarray(g._bounds(paths))
            for _ in range(4):
                unit = max((frame[2]-frame[0])/max(1., settings.get('width', 1600)), (frame[3]-frame[1])/max(1., settings.get('height', 1200)))
                radius = unit*(line_width/2+outlines)
                expanded = line_bounds+[-radius, -radius, radius, radius]
                bounds = g._bounds(([np.asarray(mesh_bounds).reshape(2, 2)] if mesh_bounds is not None else [])+[expanded.reshape(2, 2)])
                frame = _frame(bounds, settings, parameters)
        width, height = _resolution(frame, settings, len(visible))
        highlight_size = max(0., min(2., g._number(settings.get('highlight_size'), 1.)))
        shadow_size = max(0., min(2., g._number(settings.get('shadow_size'), 1.)))
        minimum_piece_size = max(0., min(1., g._number(settings.get('minimum_piece_size'), 0.)))
        dashed = bool(settings.get('dashed_boundaries', False))
        boundary_key = tuple((p.get('boundary_group'), str(p.get('render_color', p.get('color', ''))).lower())
                             for p in visible) if dashed else ()
        key = (camera_key, tuple(frame), width, height, tuple((p['id'], p.get('mode'), p.get('render_opacity', 1)) for p in visible),
               settings.get('abstraction', .65), settings.get('smoothing', .65), line_width, outlines,
               settings.get('width', 1600), settings.get('height', 1200), highlight_size, shadow_size, minimum_piece_size,
               dashed, boundary_key)
        if key in self._results:
            return self._results[key]
        grouped = defaultdict(list)
        for mesh in meshes:
            grouped[mesh['id']].append(mesh)
        line_flags = [part.get('mode') == 'line' for part in visible]
        line_count = sum(line_flags)
        step = line_count+1
        depth_maps = []
        unit = max((frame[2]-frame[0])/max(1., settings.get('width', 1600)), (frame[3]-frame[1])/max(1., settings.get('height', 1200)))
        for part, is_line in zip(visible, line_flags):
            if is_line:
                depth = _backbone_depth(projected[part['id']], frame, width, height, unit*(line_width/2+outlines), parameters['perspective'])
            else:
                depth = _rasterize(grouped[part['id']], parameters, frame, width, height)[0]
            depth_maps.append(depth)
        opaque_flags = [not is_line and float(part.get('render_opacity', 1)) >= .999999
                        for part, is_line in zip(visible, line_flags)]
        opaque, masks = _visible_masks(depth_maps, line_flags, opaque_flags, (height, width))
        opaque, masks = _remove_small_pieces(depth_maps, masks, opaque, line_flags, opaque_flags, minimum_piece_size)
        siblings = {}
        owners = None
        if dashed:
            for i, (group, color) in enumerate(boundary_key):
                if group is not None and not line_flags[i]:
                    neighbours = [j for j, (other_group, other_color) in enumerate(boundary_key)
                                  if j != i and not line_flags[j] and other_group == group
                                  and color != other_color]
                    if neighbours:
                        siblings[i] = neighbours
            if siblings:
                owners = np.full((height, width), -1, dtype=np.int32)
                front = np.full((height, width), -np.inf, dtype=np.float32)
                for j, (depth, mask, is_line) in enumerate(zip(depth_maps, masks, line_flags)):
                    if is_line:
                        continue
                    wins = mask & (depth >= front)
                    owners[wins] = j
                    front[wins] = depth[wins]
        items, draws = [], []
        for i, part in enumerate(visible):
            is_line = line_flags[i]
            depth = depth_maps[i]
            mask = masks[i]
            rank_map = np.full((height, width), -1, dtype=np.int32)
            yy, xx = np.nonzero(mask)
            ranks = np.full(len(yy), 0 if is_line else line_count, dtype=np.int32)
            for j, other in enumerate(depth_maps):
                if j == i or (line_flags[j] and not is_line):
                    continue
                other_z = other[yy, xx]
                behind = (other_z < depth[yy, xx]) | ((other_z == depth[yy, xx]) & (j < i))
                contributes = np.isfinite(other_z)
                if not line_flags[j]:
                    contributes &= other_z >= opaque[yy, xx]-1e-6
                ranks += (contributes & behind)*(1 if line_flags[j] else step)
            rank_map[yy, xx] = ranks
            unique_ranks = np.unique(ranks)
            if is_line:
                lines = [line[:, :2].tolist() for line in projected[part['id']]]
                item = {'id': part['id'], 'mode': 'line', 'lines': lines, 'rings': [], 'primitives': [],
                        'depth': float(np.mean(depth[mask])) if mask.any() else 0.,
                        'bounds': g._bounds([line[:, :2] for line in projected[part['id']]])}
                # A single paint region is the exact Simple stroke, unclipped.
                # Otherwise extend labels outside the proxy before extracting
                # clips; only depth-order boundaries can touch the stroke.
                if len(unique_ranks) > 1:
                    nearest = ndimage.distance_transform_edt(~mask, return_distances=False, return_indices=True)
                    clip_map = rank_map[tuple(nearest)]
                for rank in unique_ranks:
                    primitive = {'id': part['id'], 'mode': 'line', 'role': 'backbone', 'lines': lines,
                                 'rings': [], 'clip_rings': _clip_rings(clip_map, rank, frame, width, height) if len(unique_ranks) > 1 else None,
                                 '_rank': int(rank), '_part': i}
                    item['primitives'].append(primitive); draws.append(primitive)
                items.append(item)
                continue
            rings, cubics, pixel_rings = _paths(mask, frame, width, height, settings.get('smoothing', .65))
            # Compute lighting before scene occlusion; an overlapping part
            # must only hide the overlays, never move them to its cut edge.
            lighting_masks = _lighting_masks(np.isfinite(depth), highlight_size, shadow_size) if mask.any() else []
            item = {'id': part['id'], 'mode': 'blob', 'rings': rings, 'lines': [], 'outline_rings': rings,
                    'cubic_outline_rings': cubics, 'primitives': [], 'depth': float(np.mean(depth[np.isfinite(depth)])) if np.isfinite(depth).any() else 0.,
                    'bounds': g._bounds([np.asarray(ring) for ring in rings])}
            rank_lookup = _outline_rank_lookup(rank_map) if len(unique_ranks) > 1 else None
            outline_runs = _ranked_curves(pixel_rings, rank_map, frame, width, height, settings.get('smoothing', .65), rank_lookup) if len(unique_ranks) > 1 else {}
            internal_runs = {}
            if i in siblings:
                rank_lookup = rank_lookup or _outline_rank_lookup(rank_map)
                boundary_lookup = _internal_boundary_lookup(i, masks, owners, siblings[i])
                def classified(samples):
                    ranks = rank_lookup(samples)
                    return np.where(ranks < 0, -1, 3*ranks+boundary_lookup(samples))
                classified_runs = _ranked_curves(pixel_rings, rank_map, frame, width, height,
                    settings.get('smoothing', .65), classified)
                outline_runs = {label//3: curves for label, curves in classified_runs.items() if label % 3 == 0}
                internal_runs = {label//3: curves for label, curves in classified_runs.items() if label % 3 == 1}
                item.update(outline_rings=[], cubic_outline_rings=[],
                            cubic_outline_lines=[curve for curves in outline_runs.values() for curve in curves],
                            cubic_internal_lines=[curve for curves in internal_runs.values() for curve in curves])
            for rank in unique_ranks:
                region = mask & (rank_map == rank)
                fill_rings, fill_cubics, _ = (rings, cubics, pixel_rings) if len(unique_ranks) == 1 else _paths(region, frame, width, height, settings.get('smoothing', .65), partition=True)
                lighting = []
                for role, light_mask in lighting_masks:
                    patch_mask = region & light_mask
                    if not patch_mask.any():
                        continue
                    # The shadow shares the exterior contour, so fit it at
                    # the same smoothness. Keep the thin highlight conservative
                    # to preserve its narrow tapered tips.
                    light_smoothing = settings.get('smoothing', .65)
                    if role == 'highlight':
                        light_smoothing = min(light_smoothing, .65)
                    light_rings, light_curves, _ = _paths(patch_mask, frame, width, height,
                        light_smoothing, partition=len(unique_ranks) > 1)
                    if light_rings:
                        lighting.append({'role': role, 'rings': light_rings, 'cubic_rings': light_curves})
                primitive = {'id': part['id'], 'mode': 'blob', 'role': 'surface', 'rings': fill_rings, 'cubic_rings': fill_cubics,
                             'lighting': lighting,
                             'outline_rings': rings if len(unique_ranks) == 1 else [], 'cubic_outline_rings': cubics if len(unique_ranks) == 1 else [],
                             'cubic_outline_lines': outline_runs.get(int(rank), []), '_rank': int(rank), '_part': i}
                if i in siblings:
                    primitive.update(outline_rings=[], cubic_outline_rings=[],
                                     cubic_internal_lines=internal_runs.get(int(rank), []))
                item['primitives'].append(primitive); draws.append(primitive)
            items.append(item)
        draws.sort(key=lambda p: (p['_rank'], p['_part']))
        for primitive in draws:
            primitive.pop('_rank'); primitive.pop('_part')
        result = {'items': items, 'bounds': bounds, 'publication_frame': frame, 'draw_primitives': draws, 'publication_resolution': [width, height]}
        self._results[key] = result
        if len(self._results) > 3:
            self._results.popitem(last=False)
        return result
