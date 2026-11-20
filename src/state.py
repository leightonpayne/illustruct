"""Small, serializable illustration state shared by the tool and commands."""
from copy import deepcopy
import math

DEFAULTS = {
    "abstraction": 1.0, "smoothing": 0.65, "minimum_piece_size": 0.0,
    "match_projection": False, "fixed_frame": False,
    "width": 1577, "height": 1183, "transparent": True,
    "background": "#ffffff", "stroke_width": 2.5,
    "outline": True, "outline_color": "#3c4650", "opacity": 1.0, "shading": True,
    "dashed_boundaries": False,
    "highlight_size": 1.0, "shadow_size": 1.0,
    "scope": "visible", "selected_domain": False,
    "model_ids": None, "live": True, "show_chains": True, "show_other": True,
    "show_proteins": True, "show_nucleic": False, "show_ligands": False, "show_ions": False,
    "nucleic_style": "backbone", "tinted_outline": True, "margin_fraction": 0.06,
    "size_mode": "print", "print_width_mm": 89.0, "print_height_mm": 66.75,
    "dpi": 450, "stroke_width_pt": 0.4,
}

NUCLEIC_STYLES = {"silhouette": "Silhouette", "backbone": "Backbone"}
BACKBONE_WIDTH = 12.0
_RENDER_SETTINGS = {"frame", "_render_quality", "_interactive_preview", "publication_resolution"}


def simplification_abstraction(value):
    """Map the user-facing 0–1 control onto the complete geometry range."""
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("Simplification must be between 0 and 1")
    # Avoid floating-point drift in geometry cache keys.
    return round(0.3 + 1.4 * value, 12)


def simplification_value(abstraction):
    """Display persisted geometry settings on the 0–1 simplification scale."""
    abstraction = float(abstraction)
    if not math.isfinite(abstraction) or not 0.3 <= abstraction <= 1.7:
        raise ValueError("Simplification geometry must be between 0.3 and 1.7")
    return (abstraction - 0.3) / 1.4


def smoothing_amount(value):
    """Map the user-facing 0–1 control onto the complete smoothing range."""
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("Smoothing must be between 0 and 1")
    return round(2 * value, 12)


def smoothing_value(amount):
    """Display persisted smoothing settings without changing their appearance."""
    amount = float(amount)
    if not math.isfinite(amount) or not 0 <= amount <= 2:
        raise ValueError("Smoothing geometry must be between 0 and 2")
    return amount / 2


def sizing_mode(settings, mode):
    """Switch units while keeping effective widths inside the target controls."""
    result = deepcopy(settings)
    result["size_mode"] = mode
    if mode == "pixels" and settings.get("size_mode") == "print":
        result["stroke_width"] = min(64, result["stroke_width"])
    return result


def settings_with(overrides=None):
    """Copy and validate current controls and optional render parameters."""
    state = deepcopy(DEFAULTS)
    if overrides:
        unknown = overrides.keys() - DEFAULTS.keys() - _RENDER_SETTINGS
        if unknown:
            raise ValueError("Unknown illustration settings: " + ", ".join(sorted(unknown)))
        state.update(deepcopy(overrides))
    if not isinstance(state["shading"], bool):
        raise ValueError("Shading must be true or false.")
    if not isinstance(state["dashed_boundaries"], bool):
        raise ValueError("Dashed boundaries must be true or false.")
    if state["nucleic_style"] not in NUCLEIC_STYLES:
        raise ValueError("Choose Silhouette or Backbone for DNA/RNA.")
    if state["size_mode"] not in ("pixels", "print"):
        raise ValueError("Export size must use pixels or print dimensions.")
    for key, lower, upper in (("print_width_mm", 5, 1000), ("print_height_mm", 5, 1000),
                              ("dpi", 72, 1200), ("stroke_width_pt", 0, 10),
                              ("margin_fraction", 0, 0.25)):
        value = float(state[key])
        if not math.isfinite(value) or not lower <= value <= upper:
            raise ValueError(f"{key} must be between {lower} and {upper}")
        state[key] = value
    state["dpi"] = round(state["dpi"])
    if state["size_mode"] == "print":
        state.update(width=round(state["print_width_mm"] / 25.4 * state["dpi"]),
                     height=round(state["print_height_mm"] / 25.4 * state["dpi"]),
                     stroke_width=state["stroke_width_pt"] * state["dpi"] / 72)
    for key, lower, upper in (("abstraction", 0.3, 1.7), ("smoothing", 0, 2),
                              ("minimum_piece_size", 0, 1), ("opacity", 0, 1),
                              ("highlight_size", 0, 2), ("shadow_size", 0, 2)):
        value = float(state[key])
        if not math.isfinite(value) or not lower <= value <= upper:
            raise ValueError(f"{key} must be between {lower} and {upper}")
        state[key] = value
    for key in ("width", "height"):
        value = int(state[key])
        if not 64 <= value <= 16384:
            raise ValueError(f"{key} must be between 64 and 16384 pixels")
        state[key] = value
    if state["width"] * state["height"] > 64_000_000:
        raise ValueError("Image size must be at most 64 million pixels")
    for key in ("stroke_width",):
        value = float(state[key])
        limit = 400 if state["size_mode"] == "print" else 64
        if not math.isfinite(value) or not 0 <= value <= limit:
            raise ValueError(f"{key} must be between 0 and {limit} pixels")
        state[key] = value
    if state["scope"] not in ("visible", "selection"):
        raise ValueError("Scope must be visible or selection")
    return state


def prepared_parts(parts, styles, settings=None):
    """Geometry consumes independent arrays captured on the GUI thread."""
    result = []
    settings = settings or {}
    paint = effective_styles(parts, styles, settings)
    for part in parts:
        p = dict(part)
        p["mode"] = "line" if p["kind"] == "nucleic" and settings.get("nucleic_style", "backbone") == "backbone" else "blob"
        p["render_visible"] = p["visible"] = paint[p["id"]]["visible"]
        p["render_opacity"] = settings.get("opacity", 1)
        p["render_color"] = paint[p["id"]]["color"] if settings.get("dashed_boundaries", False) else None
        # All regions of one protein chain share a boundary group, including
        # manually selected domains. Distinct models and assembly copies do not.
        if p["kind"] == "protein" and all(key in p for key in ("model_id", "instance_index", "chain_id")):
            p["boundary_group"] = (tuple(p["model_id"]), p["instance_index"], p["chain_id"], "protein")
        else:
            p["boundary_group"] = p.get("boundary_group") if p["kind"] == "protein" else None
        p["render_outline"] = settings.get("outline", True)
        result.append(p)
    return result


def part_group(part):
    return "chains" if part["kind"] in ("protein", "nucleic") else "other"


def part_category(part):
    """Return the visibility category without changing persisted part identity."""
    if part["kind"] == "protein":
        return "proteins"
    if part["kind"] == "nucleic":
        return "nucleic"
    return "ions" if part.get("category") == "ions" else "ligands"


def component_visible(part, settings):
    """Master and type masks, independent of the remembered per-item choice."""
    return bool(settings.get("show_" + part_group(part), True)
                and settings.get("show_" + part_category(part), True))


def inherit_region_styles(parts, overrides):
    """Preserve visibility choices when live recoloring splits a chain."""
    result = dict(overrides)
    for part in parts:
        parent_id = part.get("parent_id")
        if parent_id and part["id"] not in result:
            parent = overrides.get(parent_id, {})
            result[part["id"]] = {"visible": parent["visible"]} if "visible" in parent else {}
    return result


def effective_styles(parts, overrides, settings=None):
    settings = settings or {}
    result = {}
    for part in parts:
        override = overrides.get(part["id"], {})
        style = {"color": part["color"], "opacity": 1,
                 "label": part["label"], "visible": override.get("visible", True)}
        # Group/type switches are masks: turning them back on must not undo
        # individual part visibility choices or modify the ChimeraX structure.
        style["visible"] = bool(style["visible"] and component_visible(part, settings))
        result[part["id"]] = style
    return result


def remap_model_ids(settings, styles, model_id_map):
    """Preserve style identity when a session merge renumbers its models."""
    settings, styles = deepcopy(settings), deepcopy(styles)
    prefixes = {"model:" + ".".join(map(str, old)) + ":":
                "model:" + ".".join(map(str, new)) + ":"
                for old, new in model_id_map.items()}
    def remap(part_id):
        for old, new in prefixes.items():
            if part_id.startswith(old):
                return new + part_id[len(old):]
        return part_id
    def remap_records(records):
        # Closed models may leave stale IDs behind. A live restored model's
        # remapped state takes precedence over an unchanged stale target ID.
        entries = [(key, remap(key), value) for key, value in records.items()]
        targets = {new for old, new, value in entries if old != new}
        return {new: value for old, new, value in entries if old != new or new not in targets}
    if settings.get("model_ids") is not None:
        settings["model_ids"] = [model_id_map.get(tuple(i), tuple(i)) for i in settings["model_ids"]]
    return settings, remap_records(styles)


def compute_scene(scene, settings, styles=None, engine=None):
    from .geometry import GeometryEngine
    if not scene["parts"]:
        raise ValueError("No atoms to illustrate. Open a structure or change the source selection.")
    engine = engine or GeometryEngine()
    # A scene is an immutable capture. Keep this list identity stable so the
    # engine can reuse its projections and triangulations during detail edits.
    parts = scene.get("_prepared_parts")
    prepared = prepared_parts(scene["parts"], styles or {}, settings)
    if parts is None:
        parts = scene["_prepared_parts"] = prepared
    else:
        for old, new in zip(parts, prepared):
            old.clear()
            old.update(new)
    return engine.compute(parts, scene["camera"], settings)
