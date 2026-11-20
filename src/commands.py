"""The illustruct command also works without a graphical display."""
from math import isfinite
from pathlib import Path
from chimerax.core.errors import UserError
from .version import VERSION


def show_tool(session):
    if not session.ui.is_gui:
        raise UserError("Use 'illustruct export filename.svg' in headless mode.")
    from .tool import IllustructTool
    return IllustructTool.get_singleton(session)


def _export_sizing(settings, width, height, width_mm, height_mm, dpi,
                   stroke_width, stroke_width_pt):
    from .state import settings_with

    pixels = width is not None or height is not None
    physical = width_mm is not None or height_mm is not None
    if pixels and physical:
        raise ValueError("Use width/height or widthMm/heightMm, not both.")
    if stroke_width is not None and stroke_width_pt is not None:
        raise ValueError("Use strokeWidth or strokeWidthPt, not both.")
    previous_mode = settings["size_mode"]
    # Physical dimensions avoid pixel-rounding drift in the default 4:3 canvas.
    if previous_mode == "print":
        aspect = settings["print_width_mm"] / settings["print_height_mm"]
    else:
        aspect = settings["width"] / settings["height"]
    if pixels:
        settings["size_mode"] = "pixels"
        settings["width"] = width if width is not None else round(height * aspect)
        settings["height"] = height if height is not None else round(width / aspect)
    elif physical:
        settings["size_mode"] = "print"
        settings["print_width_mm"] = width_mm if width_mm is not None else height_mm * aspect
        settings["print_height_mm"] = height_mm if height_mm is not None else width_mm / aspect
    if dpi is not None:
        if not isfinite(dpi) or not 72 <= dpi <= 1200:
            raise ValueError("dpi must be between 72 and 1200")
        settings["dpi"] = round(dpi)
    if settings["size_mode"] == "print":
        if stroke_width is not None:
            settings["stroke_width_pt"] = stroke_width * 72 / settings["dpi"]
        elif stroke_width_pt is not None:
            settings["stroke_width_pt"] = stroke_width_pt
        elif previous_mode == "pixels":
            settings["stroke_width_pt"] = settings["stroke_width"] * 72 / settings["dpi"]
    else:
        if stroke_width is not None:
            settings["stroke_width"] = stroke_width
        elif stroke_width_pt is not None:
            settings["stroke_width_pt"] = stroke_width_pt
            settings["stroke_width"] = stroke_width_pt * settings["dpi"] / 72
    return settings_with(settings)


def export_illustration(session, filename, atoms=None, components=None,
                        simplification=None, smoothing=None, minimum_piece_size=None,
                        opacity=None, transparent=None, outline=None, shading=None,
                        highlight_size=None, shadow_size=None, dashed_boundaries=None,
                        split_selection=None, framing=None, nucleic_style=None,
                        width=None, height=None, width_mm=None, height_mm=None,
                        dpi=None, stroke_width=None, stroke_width_pt=None,
                        margin=None, tinted_outline=None):
    from .state import (settings_with, compute_scene, effective_styles,
                        simplification_abstraction, smoothing_amount, inherit_region_styles)
    from .geometry import GeometryEngine
    from .scene import capture_scene
    from .export import export_svg, export_png, export_pdf
    tool = next((t for t in session.tools.list()
                 if t.__class__.__name__ == "IllustructTool"), None)
    styles = tool.styles if tool else {}
    path = Path(filename).expanduser()
    if path.suffix.lower() not in (".svg", ".png", ".pdf"):
        raise UserError("Use a .svg, .pdf, or .png filename.")
    try:
        settings = settings_with(tool.settings if tool else None)
        overrides = {"transparent": transparent, "outline": outline, "shading": shading,
                     "dashed_boundaries": dashed_boundaries, "selected_domain": split_selection,
                     "nucleic_style": nucleic_style, "tinted_outline": tinted_outline}
        settings.update({key: value for key, value in overrides.items() if value is not None})
        if simplification is not None:
            settings["abstraction"] = simplification_abstraction(simplification / 100)
        if smoothing is not None:
            settings["smoothing"] = smoothing_amount(smoothing / 100)
        for key, value in (("minimum_piece_size", minimum_piece_size), ("opacity", opacity),
                           ("highlight_size", highlight_size), ("shadow_size", shadow_size),
                           ("margin_fraction", margin)):
            if value is not None:
                settings[key] = value / 100
        if framing is not None:
            if framing not in ("fit", "camera"):
                raise ValueError("Framing must be fit or camera.")
            settings.update(match_projection=framing == "camera", fixed_frame=False)
            settings.pop("frame", None)
        if atoms is not None:
            settings.update(scope="visible", model_ids=None)
        if atoms is not None or components is not None:
            categories = {"protein": "proteins", "nucleic": "nucleic",
                          "ligand": "ligands", "ion": "ions"}
            included = set(components) if components is not None else set(categories)
            if not included or included - categories.keys():
                raise ValueError("Components must be a list of protein, nucleic, ligand, or ion.")
            settings.update(show_chains=True, show_other=True)
            settings.update({"show_" + category: kind in included
                             for kind, category in categories.items()})
            styles = {}
        settings = _export_sizing(settings, width, height, width_mm, height_mm, dpi,
                                  stroke_width, stroke_width_pt)
        scene = capture_scene(session, scope=settings["scope"],
                              model_ids=settings["model_ids"],
                              selected_domain=settings["selected_domain"], atoms=atoms)
        styles = inherit_region_styles(scene["parts"], styles)
        if settings["match_projection"]:
            aspect = scene["camera"].get("aspect", 1)
            settings["frame"] = [-1, -1/aspect, 1, 1/aspect]
        elif tool and settings["fixed_frame"] and tool.frame is not None:
            settings["frame"] = tool.frame
        settings["_render_quality"] = "export"
        engine = GeometryEngine()
        geometry = compute_scene(scene, settings, styles, engine)
        paint = effective_styles(scene["parts"], styles, settings)
        writer = {".svg": export_svg, ".png": export_png, ".pdf": export_pdf}[path.suffix.lower()]
        writer(str(path), geometry, settings, paint,
               metadata={"application": "Illustruct", "version": VERSION,
                         "source": "ChimeraX", "camera": scene["camera"],
                         "warnings": scene.get("warnings", [])})
    except (ValueError, OSError, RuntimeError) as error:
        raise UserError(str(error)) from error
    for warning in scene.get("warnings", []):
        session.logger.warning(warning)
    session.logger.info(f"Saved illustration to {path}")
    return geometry


def register_commands(logger):
    from .state import NUCLEIC_STYLES
    from chimerax.atomic import AtomsArg
    from chimerax.core.commands import (register, CmdDesc, SaveFileNameArg,
                                       IntArg, FloatArg, BoolArg, EnumOf, ListOf, Bounded)
    register("illustruct", CmdDesc(synopsis="Open the molecular illustration panel"),
             show_tool, logger=logger)
    register("illustruct export", CmdDesc(required=[("filename", SaveFileNameArg)],
        keyword=[("atoms", AtomsArg),
                 ("components", ListOf(EnumOf(["protein", "nucleic", "ligand", "ion"]), min_size=1)),
                 ("simplification", Bounded(FloatArg, 0, 100)),
                 ("smoothing", Bounded(FloatArg, 0, 100)),
                 ("minimum_piece_size", Bounded(FloatArg, 0, 100)),
                 ("opacity", Bounded(FloatArg, 0, 100)),
                 ("transparent", BoolArg), ("outline", BoolArg), ("shading", BoolArg),
                 ("dashed_boundaries", BoolArg),
                 ("highlight_size", Bounded(FloatArg, 0, 200)),
                 ("shadow_size", Bounded(FloatArg, 0, 200)),
                 ("split_selection", BoolArg), ("framing", EnumOf(["fit", "camera"])),
                 ("nucleic_style", EnumOf(list(NUCLEIC_STYLES))),
                 ("width", Bounded(IntArg, 64, 16384)), ("height", Bounded(IntArg, 64, 16384)),
                 ("width_mm", Bounded(FloatArg, 5, 1000)), ("height_mm", Bounded(FloatArg, 5, 1000)),
                 ("dpi", Bounded(IntArg, 72, 1200)),
                 ("stroke_width", Bounded(FloatArg, 0, 64)), ("stroke_width_pt", Bounded(FloatArg, 0, 10)),
                 ("margin", Bounded(FloatArg, 0, 25)), ("tinted_outline", BoolArg)],
        synopsis="Export the current view as SVG, PDF, or PNG"),
        export_illustration, logger=logger)
