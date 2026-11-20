"""Read-only, detached snapshots of an open ChimeraX atomic scene.

Call these functions on ChimeraX's UI thread.  Returned coordinates and camera
matrices own their NumPy storage and may subsequently be sent to a worker.  No
Atom, Residue, Model, or Session references are retained in the result.
"""

from collections import defaultdict, deque
from hashlib import blake2b
from math import radians, tan
from urllib.parse import quote

import numpy as np


_WATER_NAMES = frozenset(("HOH", "WAT", "H2O", "DOD", "D2O", "SOL", "TIP", "TIP3", "TIP4"))
_NUCLEIC_ANCHORS = ("C4'", "C4*", "P", "C1'", "C1*", "C3'", "C3*")


def _model_filter(model_ids):
    if model_ids is None:
        return None
    if isinstance(model_ids, tuple) and all(isinstance(i, int) for i in model_ids):
        model_ids = [model_ids]
    ids = set()
    for model_id in model_ids:
        if isinstance(model_id, str):
            model_id = model_id.lstrip("#").split(".")
        ids.add(tuple(int(i) for i in model_id))
    return ids


def _structures(session, model_ids=None):
    from chimerax.atomic import Structure

    allowed = _model_filter(model_ids)
    return sorted(
        (model for model in session.models.list(type=Structure)
         if not model.deleted and model.id is not None
         and (allowed is None or tuple(model.id) in allowed)),
        key=lambda model: tuple(model.id),
    )


def list_structure_models(session):
    """List live atomic/coarse structures, including currently hidden models."""
    return [{"id": tuple(model.id), "label": f"#{model.id_string} {model.name}"}
            for model in _structures(session)]


def camera_snapshot(session):
    """Capture a central camera, with horizontal field of view in degrees.

    Camera coordinates have +X right, +Y up, and -Z forward.  ``aspect`` is
    viewport width / height.  ``field_width`` is the orthographic horizontal
    width, or the perspective width at the center-of-rotation plane.
    """
    view = session.main_view
    camera = view.camera
    matrix = np.array(camera.position.inverse().matrix, dtype=np.float64, copy=True)
    name = str(getattr(camera, "name", "unknown"))
    orthographic = name == "orthographic"
    warnings = []
    if not orthographic and name != "mono":
        if "stereo" in name or getattr(camera, "number_of_views", lambda: 1)() == 2:
            warnings.append("Stereo is illustrated from the central camera; eye-specific offsets are omitted.")
        else:
            warnings.append(f"Camera '{name}' is approximated by a central perspective view. Use mono or orthographic for a matching illustration.")
    width, height = view.window_size
    aspect = float(width) / float(height) if width > 0 and height > 0 else 1.0
    fov = float(getattr(camera, "field_of_view", 30.0))
    if not np.isfinite(fov) or not 0 < fov < 180:
        fov = 30.0
    if orthographic:
        field_width = float(camera.field_width)
    else:
        center = np.asarray(view.center_of_rotation, dtype=np.float64)
        depth = float(-(matrix[:, :3] @ center + matrix[:, 3])[2])
        field_width = 2.0 * max(abs(depth), 1e-6) * tan(radians(fov) / 2.0)
    if not np.isfinite(field_width) or field_width <= 0:
        field_width = 1.0
    clipping = getattr(view, "clip_planes", None)
    if clipping is not None and clipping.planes():
        warnings.append("Scene clipping planes are not applied to the illustration.")
    return {
        "scene_to_camera": matrix,
        "projection": "orthographic" if orthographic else "perspective",
        "field_of_view": fov,
        "field_width": field_width,
        "aspect": aspect,
        "camera_name": name,
        "warnings": warnings,
    }


def _kind(residue):
    from chimerax.atomic import Residue

    return {Residue.PT_AMINO: "protein", Residue.PT_NUCLEIC: "nucleic"}.get(residue.polymer_type, "other")


def _category(residue, kind):
    """Classify the complete residue, never the visible or selected subset.

    Monatomic nonpolymer residues include metal and halide ions. Larger
    nonpolymer residues remain ligands; a residue name alone is insufficient
    evidence to infer a polyatomic ion's charge.
    """
    if kind == "protein":
        return "proteins"
    if kind == "nucleic":
        return "nucleic"
    return "ions" if len(residue.atoms) == 1 else "ligands"


def _positions(model, visible_only):
    """Preserve original instance numbers when some positions are hidden."""
    all_positions = list(model.get_scene_positions(displayed_only=False))
    if not visible_only:
        return list(enumerate(all_positions)), len(all_positions)
    indices = defaultdict(deque)
    for index, position in enumerate(all_positions):
        indices[np.asarray(position.matrix).tobytes()].append(index)
    result = []
    for position in model.get_scene_positions(displayed_only=True):
        key = np.asarray(position.matrix).tobytes()
        if indices[key]:
            result.append((indices[key].popleft(), position))
    return result, len(all_positions)


def _chain_order(model):
    order = {}
    chains = model.chains
    for chain_index, chain in enumerate(chains):
        for sequence_index, residue in enumerate(chain.residues):
            if residue is not None:
                order[residue.cpp_pointer] = (chain_index, sequence_index)
    for index, residue in enumerate(model.residues):
        order.setdefault(residue.cpp_pointer, (len(chains), index))
    return order


def _number_adjacent(previous, residue):
    if residue.number == previous.number + 1:
        return True
    if residue.number != previous.number:
        return False
    def rank(code):
        code = code.strip().upper()
        return 0 if not code else ord(code) - ord("A") + 1 if len(code) == 1 and code.isalpha() else -999
    return rank(residue.insertion_code) - rank(previous.insertion_code) == 1


def _trace_anchor_index(residue, allowed, atom_indices, kind):
    """Choose an observed backbone anchor from the captured atoms."""
    names = ("CA",) if kind == "protein" else _NUCLEIC_ANCHORS
    for name in names:
        atom = residue.find_atom(name)
        index = atom_indices.get(atom.cpp_pointer) if atom is not None else None
        if index in allowed:
            return index
    return None


def _trace_indices(residues, allowed, atom_indices, coords, order, kind):
    """Split at excluded residues, real chain breaks, and absent backbone atoms."""
    segments, current = [], []
    previous = previous_index = None
    used_sparse_fallback = False
    for residue in sorted(residues, key=lambda r: order[r.cpp_pointer]):
        index = _trace_anchor_index(residue, allowed, atom_indices, kind)
        if index is None:
            if current:
                segments.append(current)
            current, previous, previous_index = [], None, None
            continue
        if previous is not None:
            left, right = order[previous.cpp_pointer], order[residue.cpp_pointer]
            sequence_adjacent = left[0] == right[0] and right[1] == left[1] + 1
            distance = float(np.linalg.norm(coords[index] - coords[previous_index]))
            spatially_adjacent = 0.1 < distance <= (6.0 if kind == "protein" else 12.0)
            connected = previous.connects_to(residue)
            # CA-only / P-only structures may lack bonds entirely.  Never use
            # this fallback for a full-atom chain with a genuine missing bond.
            sparse = previous.num_atoms <= 2 and residue.num_atoms <= 2
            fallback = sparse and sequence_adjacent and _number_adjacent(previous, residue) and spatially_adjacent
            contiguous = sequence_adjacent and spatially_adjacent and (connected or fallback)
            if fallback and not connected:
                used_sparse_fallback = True
            if not contiguous:
                if current:
                    segments.append(current)
                current = []
        current.append(index)
        previous, previous_index = residue, index
    if current:
        segments.append(current)
    return segments, used_sparse_fallback


def _representative_color(colors):
    unique, counts = np.unique(np.asarray(colors, dtype=np.uint8), axis=0, return_counts=True)
    rgba = unique[int(np.argmax(counts))]
    return "#" + "".join(f"{int(channel):02x}" for channel in rgba[:3]), float(rgba[3]) / 255.0, len(unique) > 1


def _residue_ranges(residues, order):
    """Describe captured regions without pretending that intervening residues belong."""
    runs, current = [], []
    for residue in sorted(residues, key=lambda r: order[r.cpp_pointer]):
        if current:
            left, right = order[current[-1].cpp_pointer], order[residue.cpp_pointer]
            if left[0] != right[0] or right[1] != left[1] + 1 or not _number_adjacent(current[-1], residue):
                runs.append(current)
                current = []
        current.append(residue)
    if current:
        runs.append(current)
    def name(residue):
        return f"{residue.number}{residue.insertion_code.strip()}"
    return ", ".join(name(run[0]) if len(run) == 1 else f"{name(run[0])}–{name(run[-1])}" for run in runs)


def capture_scene(session, scope="visible", model_ids=None, selected_domain=False, atoms=None):
    """Snapshot the current scene without changing its structures or selection.

    ``visible`` includes individually visible atoms and all heavy atoms of a
    residue represented by a displayed ribbon.  ``selection`` includes exactly
    the selected heavy atoms, even in hidden models.  A selected residue in
    ChimeraX selects its constituent atoms.  ``selected_domain`` separates the
    captured atoms of any partly selected polymer residue into a selected part.
    Explicit ``atoms`` override scope and model filters, including hidden atoms,
    models, and instances without changing selection. An empty collection stays
    empty; selected-domain splitting still follows the current selection.
    Proteins also split into residue-colour components. Their membership is
    determined before visibility filtering so hiding a region does not rename
    the remaining components. Nucleic backbones retain their continuous traces
    and currently use one representative colour per chain/selection part.

    Water/hydrogen atoms are always excluded.  Atom coordinates reflect the
    current coordinate set and active alternate locations already chosen by
    ChimeraX.  ``atom_radii`` contains ChimeraX's chemical default radii rather
    than user-adjusted display radii. All returned arrays are detached.
    Surface-only representations are intentionally outside this scope.
    """
    specified_atoms = atoms
    if specified_atoms is None and scope not in ("visible", "selection"):
        raise ValueError("Scope must be 'visible' or 'selection'.")
    visible_only = specified_atoms is None and scope == "visible"
    if specified_atoms is None:
        models = _structures(session, model_ids)
    else:
        models = sorted((model for model in specified_atoms.unique_structures
                         if not model.deleted and model.id is not None),
                        key=lambda model: tuple(model.id))
    camera = camera_snapshot(session)
    warnings = list(camera["warnings"])
    parts, polymer_chains, colored_parts = [], set(), []
    sparse_trace_used = False
    for model in models:
        if visible_only and not model.visible:
            continue
        atoms = model.atoms
        if not len(atoms):
            continue
        local_coords = np.array(atoms.coords, dtype=np.float64, copy=True)
        default_radii = np.array(atoms.default_radii, dtype=np.float64, copy=True)
        residues = list(model.residues)
        atom_residues = atoms.residues
        residue_names = atom_residues.names
        heavy = atoms.element_numbers != 1
        nonwater = np.array([str(name).upper() not in _WATER_NAMES for name in residue_names], dtype=bool)
        finite = np.isfinite(local_coords).all(axis=1)
        if not finite.all():
            warnings.append(f"#{model.id_string}: atoms with non-finite coordinates were omitted.")
        atom_colors = np.array(atoms.colors, dtype=np.uint8, copy=True)
        atom_displays = np.array(atoms.displays, dtype=bool, copy=True)
        atom_hides = np.array(atoms.hides, copy=True)
        ribbon_colors = np.array(atom_residues.ribbon_colors, dtype=np.uint8, copy=True)
        ribbon_shown = atom_residues.ribbon_displays & (atom_residues.polymer_types != 0)
        ribbon_shown &= ribbon_colors[:, 3] > 0
        if specified_atoms is not None:
            included = atoms.mask(specified_atoms)
        elif scope == "selection":
            included = np.array(atoms.selected, dtype=bool, copy=True)
        else:
            atom_shown = atom_displays & (atom_hides == 0) & (atom_colors[:, 3] > 0)
            included = atom_shown | ribbon_shown
        eligible = heavy & nonwater & finite
        included &= eligible
        if not included.any():
            continue
        effective_colors = atom_colors.copy()
        effective_colors[ribbon_shown] = ribbon_colors[ribbon_shown]
        positions, instance_count = _positions(model, visible_only)
        if not visible_only and instance_count > 1:
            source = ("explicit atoms include every instance" if specified_atoms is not None
                      else "selection includes every instance of the selected atoms")
            warnings.append(f"#{model.id_string}: {source}, including hidden instances.")
        pointer_indices = {int(pointer): index for index, pointer in enumerate(atoms.pointers)}
        order = _chain_order(model)
        grouped = {}
        protein_colors = defaultdict(set)
        residue_ordinals = {r.cpp_pointer: index for index, r in enumerate(residues)}
        # A hidden ribbon region retains its ribbon material when the rest of
        # that protein is still shown as cartoons. This matters when only the
        # cartoon colours were changed and its underlying atoms remain CPK.
        ribbon_chains = {str(residue.chain_id).strip() for residue in residues
                         if _kind(residue) == "protein" and residue.ribbon_display}
        for residue in residues:
            all_indices = [pointer_indices[int(pointer)] for pointer in residue.atoms.pointers
                           if int(pointer) in pointer_indices and eligible[pointer_indices[int(pointer)]]]
            if not all_indices:
                continue
            indices = [index for index in all_indices if included[index]]
            kind = _kind(residue)
            chain_id = str(residue.chain_id).strip()
            selected = bool(selected_domain and kind != "other" and residue.selected)
            if kind == "other":
                key = ("residue", residue_ordinals[residue.cpp_pointer])
            else:
                key = (kind, chain_id, selected)
            base_key = key
            residue_color = None
            if kind == "protein":
                colors = effective_colors[all_indices]
                if (not np.any(ribbon_shown[all_indices]) and chain_id in ribbon_chains
                        and not np.any(atom_displays[all_indices] & (atom_hides[all_indices] == 0))):
                    colors = ribbon_colors[all_indices]
                residue_color = _representative_color(colors)
                # RGBA, rather than RGB alone, also preserves regional opacity.
                color_key = residue_color[:2]
                protein_colors[base_key].add(color_key)
                key += (color_key,)
            elif not indices:
                continue
            if key not in grouped:
                grouped[key] = {"kind": kind, "chain_id": chain_id, "selected": selected,
                                "category": _category(residue, kind),
                                "indices": [], "residues": [], "first": residue,
                                "base_key": base_key, "all_ordinals": [],
                                "source_color": residue_color, "mixed_residue_colors": False}
            grouped[key]["all_ordinals"].append(residue_ordinals[residue.cpp_pointer])
            grouped[key]["indices"].extend(indices)
            if indices:
                grouped[key]["residues"].append(residue)
                if residue_color and residue_color[2]:
                    grouped[key]["mixed_residue_colors"] = True
        templates = []
        for group in grouped.values():
            if not group["indices"]:
                continue
            indices = np.asarray(group["indices"], dtype=np.int64)
            allowed = set(group["indices"])
            kind, residue = group["kind"], group["first"]
            chain_name = group["chain_id"] or "(blank)"
            chain_token = quote(group["chain_id"], safe="")
            if kind == "other":
                suffix = f"residue:{chain_token}:{residue.number}:{quote(residue.insertion_code.strip(), safe='')}:{quote(residue.name, safe='')}:{residue_ordinals[residue.cpp_pointer]}"
                label = f"{residue.name} {residue.number}{residue.insertion_code.strip()}, chain {chain_name}"
                description = f"{model.name} ({'ion' if group['category'] == 'ions' else 'ligand'})"
                trace_indices = []
                parent_suffix = None
            else:
                suffix = f"chain:{chain_token}:{kind}" + (":selected" if group["selected"] else "")
                parent_suffix = None
                label = f"Chain {chain_name}"
                detail = "selected residues" if group["selected"] else ""
                if kind == "protein" and len(protein_colors[group["base_key"]]) > 1:
                    parent_suffix = suffix
                    membership = ",".join(str(i) for i in sorted(group["all_ordinals"]))
                    suffix += ":region:" + blake2b(membership.encode("ascii"), digest_size=10).hexdigest()
                    detail = f"{detail or 'residues'} {_residue_ranges(group['residues'], order)}"
                if detail:
                    label += f" ({detail})"
                description = (getattr(residue.chain, "description", None) or model.name)
                trace_indices, sparse = _trace_indices(group["residues"], allowed, pointer_indices, local_coords, order, kind)
                sparse_trace_used |= sparse
            if kind == "protein":
                color, opacity = group["source_color"][:2]
                multicolor = group["mixed_residue_colors"]
            else:
                color, opacity, multicolor = _representative_color(effective_colors[indices])
            templates.append((group, indices, suffix, parent_suffix, label, description, trace_indices, color, opacity))
            if multicolor:
                colored_parts.append(f"#{model.id_string} {label}")
        for instance_index, position in positions:
            matrix = np.asarray(position.matrix, dtype=np.float64)
            scene_coords = local_coords @ matrix[:, :3].T + matrix[:, 3]
            for group, indices, suffix, parent_suffix, label, description, trace_indices, color, opacity in templates:
                part_id = f"model:{model.id_string}:instance:{instance_index}:{suffix}"
                part_label = f"#{model.id_string} {label}"
                if instance_count > 1:
                    part_label += f" (copy {instance_index + 1})"
                parts.append({
                    "id": part_id, "label": part_label, "description": str(description), "kind": group["kind"],
                    "parent_id": f"model:{model.id_string}:instance:{instance_index}:{parent_suffix}" if parent_suffix else None,
                    "category": group["category"],
                    "coords": np.array(scene_coords[indices], dtype=np.float64, copy=True),
                    "atom_radii": np.array(default_radii[indices], dtype=np.float64, copy=True),
                    "segments": [np.array(scene_coords[segment], dtype=np.float64, copy=True) for segment in trace_indices],
                    "color": color, "opacity": opacity, "model_id": tuple(model.id),
                    "chain_id": group["chain_id"], "instance_index": instance_index,
                    "atom_indices": indices.copy(), "selected_domain": group["selected"],
                    "residue_count": len(group["residues"]),
                })
                if group["kind"] != "other":
                    polymer_chains.add((tuple(model.id), instance_index, group["chain_id"], group["kind"]))
    if colored_parts:
        warnings.append(f"{len(colored_parts)} part{'s have' if len(colored_parts) != 1 else ' has'} multiple source colors within a protein residue or an unsplit nucleic/ligand part; the most common color is used for that residue or part.")
    if sparse_trace_used:
        warnings.append("Sparse backbone traces without bonds use residue order and distance to identify connections.")
    if not parts:
        if specified_atoms is not None:
            warnings.append("No specified non-water heavy atoms.")
        else:
            warnings.append("No selected non-water heavy atoms." if scope == "selection" else "No visible atoms or ribbons containing non-water heavy atoms.")
    fingerprint = blake2b(digest_size=16)
    for part in parts:
        fingerprint.update(part["id"].encode("utf-8"))
        fingerprint.update(part["category"].encode("ascii"))
        fingerprint.update(part["coords"].tobytes())
        fingerprint.update(b"atom_radii")
        fingerprint.update(part["atom_radii"].tobytes())
        fingerprint.update(part["color"].encode("ascii"))
        fingerprint.update(np.float64(part["opacity"]).tobytes())
        for segment in part["segments"]:
            fingerprint.update(np.int64(len(segment)).tobytes())
            fingerprint.update(segment.tobytes())
    return {"parts": parts, "camera": camera, "signature": fingerprint.hexdigest(),
            "atom_count": sum(len(part["coords"]) for part in parts), "chain_count": len(polymer_chains),
            "warnings": list(dict.fromkeys(warnings))}
