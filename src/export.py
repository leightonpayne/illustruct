"""Standalone SVG and native Qt PNG/PDF export for molecular illustrations.

SVG generation uses only Python's standard library. PNG export rasterizes
the SVG with native Qt; PDF preserves editable vector artwork.
"""

from __future__ import annotations

import importlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from .lighting import lighting_colors
from .state import BACKBONE_WIDTH


_DEFAULT_COLOR = "#64748b"
_DEFAULT_OUTLINE = "#263d35"
_XML_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?\Z")
_RENDERER_CLASS = None


def _xml(value):
    return escape(_XML_CONTROLS.sub("\ufffd", str(value)), {'"': "&quot;", "'": "&apos;"})


def _number(value):
    return format(float(value), ".12g")


def _finite(value, fallback):
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return fallback
    return number if math.isfinite(number) else fallback


def _dimension(value, fallback):
    number = _finite(value, None)
    if number is None:
        if value is not None:
            raise ValueError("Export dimensions must be finite numbers.")
        number = fallback
    number = round(number)
    if not 1 <= number <= 16384:
        raise ValueError("Export dimensions must be between 1 and 16384 pixels.")
    return number


def _color(value, fallback):
    if isinstance(value, str) and _HEX_COLOR.fullmatch(value):
        return value
    return fallback


def _shade_color(color, shade):
    color = _color(color, _DEFAULT_COLOR).lstrip("#")
    if len(color) == 3:
        color = "".join(component * 2 for component in color)
    shade = max(-1, min(1, _finite(shade, 0)))
    rgb = [int(color[index:index + 2], 16) for index in (0, 2, 4)]
    return "#" + "".join(f"{round(component * (1 + shade) if shade < 0 else component + (255 - component) * shade):02x}" for component in rgb)


def _output_spec(settings):
    width = _dimension(settings.get("width"), 1600)
    height = _dimension(settings.get("height"), 1200)
    is_print = settings.get("size_mode") == "print"
    dpi = _finite(settings.get("dpi"), 300 if is_print else 96)
    if not 1 <= dpi <= 9600:
        raise ValueError("Export resolution must be between 1 and 9600 DPI.")
    width_mm = _finite(settings.get("print_width_mm"), None) if is_print else width * 25.4 / dpi
    height_mm = _finite(settings.get("print_height_mm"), None) if is_print else height * 25.4 / dpi
    if width_mm is None or height_mm is None or not (0 < width_mm <= 5000 and 0 < height_mm <= 5000):
        raise ValueError("Print width and height must be finite positive millimeter dimensions.")
    return width, height, dpi, width_mm, height_mm, is_print


def _cubic_paths(paths):
    result = []
    for curve in paths or ():
        try:
            start = _paths([[curve["start"]]], 1)[0][0]
            segments = []
            for segment in curve.get("segments", ()):
                if len(segment) != 6:
                    raise ValueError("Cubic segments need two controls and an endpoint.")
                controls = _paths([[segment[:2], segment[2:4], segment[4:6]]], 3)[0]
                segments.append(tuple(component for point in controls for component in point))
        except (TypeError, KeyError, IndexError) as error:
            raise ValueError("Illustration cubic paths contain an invalid curve.") from error
        if segments:
            result.append((start, segments))
    return result


def _cubic_data(curves, closed=True):
    return " ".join("M" + ",".join(map(_number, start)) + " " + " ".join(
        "C" + ",".join(map(_number, segment)) for segment in segments) + ("Z" if closed else "")
        for start, segments in curves)


def _curve_points(curves):
    return [[start] + [tuple(segment[index:index + 2]) for segment in segments for index in (0, 2, 4)]
            for start, segments in curves]


def _bounds(value):
    try:
        values = tuple(float(component) for component in value)
    except (ValueError, TypeError, OverflowError):
        return None
    if len(values) != 4 or not all(math.isfinite(component) for component in values):
        return None
    return values if values[0] <= values[2] and values[1] <= values[3] else None


def _paths(paths, minimum):
    result = []
    for path in paths if paths is not None else ():
        points = []
        for point in path:
            try:
                x, y = float(point[0]), float(point[1])
            except (ValueError, TypeError, IndexError, OverflowError) as error:
                raise ValueError("Illustration paths contain an invalid point.") from error
            if not math.isfinite(x) or not math.isfinite(y):
                raise ValueError("Illustration coordinates must be finite.")
            points.append((x, y))
        if len(points) >= minimum:
            result.append(points)
    return result


def _path_data(paths, closed):
    return " ".join("M" + "L".join(f"{_number(x)},{_number(y)}" for x, y in path) + ("Z" if closed else "") for path in paths)


def _stroke_paths(paths, role, width, color, outline, outline_width, units_per_pixel):
    if not paths or width <= 0:
        return []
    # QtSvg drops zero-length edges even with round caps. Explicit circles keep
    # isolated residues visible without adding an artificial connecting edge.
    points = [path[0] for path in paths if all(point == path[0] for point in path)]
    path = _path_data([path for path in paths if any(point != path[0] for point in path)], False)
    paints = []
    if outline != "none" and outline_width > 0:
        paints.append((role + "-outline", outline, width + 2 * outline_width))
    paints.append((role, color, width))
    output = []
    for name, paint, size in paints:
        if path:
            output.append(f'<path data-role="{name}" d="{path}" fill="none" stroke="{paint}" stroke-width="{_number(size * units_per_pixel)}" stroke-linecap="round" stroke-linejoin="round"/>')
        for x, y in points:
            output.append(f'<circle data-role="{name}" cx="{_number(x)}" cy="{_number(y)}" r="{_number(size * units_per_pixel / 2)}" fill="{paint}"/>')
    return output


def _group_id(identifier, index):
    name = "".join(character if re.fullmatch(r"[a-zA-Z0-9_.-]", character) else f"_{ord(character):x}_" for character in str(identifier))
    return f"part-{index}-{name}"


def _metadata_value(value):
    # Camera matrices and scalar counts commonly come from NumPy. Avoid an
    # import-time NumPy dependency so the SVG writer also works in plain Python.
    converter = getattr(value, "tolist", None)
    if callable(converter):
        return converter()
    converter = getattr(value, "item", None)
    if callable(converter):
        return converter()
    raise TypeError(f"Metadata value of type {type(value).__name__} is not JSON serializable.")


def build_svg(geometry, settings, styles=None, metadata=None):
    """Return a portable SVG with even-odd holes and output-pixel stroke widths.

    Coordinates are already projected with positive Y downward. A supplied
    frame locks the view; otherwise only visible geometry determines framing.
    Per-part opacity multiplies the overall opacity setting.
    """
    if not isinstance(geometry, dict) or not isinstance(geometry.get("items"), (list, tuple)):
        raise ValueError("No illustration geometry is available to export.")
    settings = settings or {}
    styles = styles if styles is not None else settings.get("styles", {})
    width, height, dpi, width_mm, height_mm, is_print = _output_spec(settings)
    maximum_width = 400 if is_print else 100
    stroke_width = max(0, min(maximum_width, _finite(settings.get("stroke_width"), 2.5)))
    line_width = BACKBONE_WIDTH
    global_opacity = max(0, min(1, _finite(settings.get("opacity"), 1)))
    default_outline = _color(settings.get("outline_color"), _DEFAULT_OUTLINE)
    items = []
    extent = [math.inf, math.inf, -math.inf, -math.inf]
    maximum_stroke = 0
    source_items = geometry["items"]
    global_order = isinstance(geometry.get("draw_primitives"), (list, tuple))
    if global_order:
        by_id = {item["id"]: item for item in source_items}
        source_items = [{**by_id.get(primitive["id"], {}), **primitive, "primitives": [primitive],
                         **{field: primitive.get(field, []) for field in
                            ("outline_rings", "cubic_outline_rings", "cubic_outline_lines", "cubic_internal_lines", "outline_lines", "occlusion_lines")}}
                        for primitive in geometry["draw_primitives"]]
    for item in source_items:
        style = styles.get(item["id"], {})
        if style.get("visible", True) is False:
            continue
        is_line = item.get("mode") == "line"
        paths = _paths(item.get("lines" if is_line else "rings"), 1 if is_line else 3)
        silhouette_curves = [] if is_line else _cubic_paths(item.get("cubic_rings"))
        publication = "primitives" in item and not is_line
        clip_rings = _paths(item.get("clip_rings"), 3)
        primitives, outlines, cubic_outlines, cubic_lines, occlusion = [], [], [], [], []
        internal_closed, internal_open = [], []
        if publication:
            for primitive in item["primitives"]:
                layers = [primitive]
                if settings.get("shading", True):
                    layers.extend(primitive.get("lighting", ()))
                for layer in layers:
                    curves = _cubic_paths(layer.get("cubic_rings"))
                    rings = [] if curves else _paths(layer.get("rings"), 3)
                    role = layer.get("role", "surface")
                    if role not in ("surface", "backbone", "shadow", "highlight"):
                        role = "surface"
                    primitives.append((role, rings, curves))
            cubic_outlines = _cubic_paths(item.get("cubic_outline_rings"))
            outlines = [] if cubic_outlines else _paths(item.get("outline_rings"), 3)
            for curve in item.get("cubic_outline_lines") or ():
                validated = _cubic_paths([curve])
                (cubic_outlines if curve.get("closed", False) else cubic_lines).extend(validated)
            for curve in item.get("cubic_internal_lines") or ():
                validated = _cubic_paths([curve])
                (internal_closed if curve.get("closed", False) else internal_open).extend(validated)
            occlusion = _paths(item.get("occlusion_lines"), 2) + _paths(item.get("outline_lines"), 2)
        color = _color(style.get("color"), _DEFAULT_COLOR)
        automatic_outline = _shade_color(color, -0.55) if settings.get("tinted_outline") else default_outline
        outline_setting = style.get("outline", settings.get("outline", True))
        outline = "none" if outline_setting is False or outline_setting == "none" else _color(outline_setting, automatic_outline)
        if publication:
            painted_width = stroke_width if outline != "none" else 0
        elif is_line:
            core_width = line_width if paths else 0
            painted_width = core_width + 2 * stroke_width if core_width > 0 and outline != "none" else core_width
        else:
            painted_width = stroke_width if outline != "none" else 0
        maximum_stroke = max(maximum_stroke, painted_width)
        frame_paths = paths + _curve_points(silhouette_curves)
        if publication:
            frame_paths = (outlines + _curve_points(cubic_outlines) + _curve_points(cubic_lines)
                           + _curve_points(internal_closed) + _curve_points(internal_open) + occlusion)
            for _, rings, curves in primitives:
                frame_paths += rings + _curve_points(curves)
        for path in frame_paths:
            for x, y in path:
                extent[0] = min(extent[0], x)
                extent[1] = min(extent[1], y)
                extent[2] = max(extent[2], x)
                extent[3] = max(extent[3], y)
        items.append((item, style, paths, is_line, outline, publication,
                      primitives, outlines, cubic_outlines, cubic_lines, occlusion,
                      internal_closed, internal_open, silhouette_curves, clip_rings))

    # The geometry engine preserves source order; camera Z increases toward the
    # viewer, so paint the farthest parts first.
    if not global_order:
        items.sort(key=lambda entry: _finite(entry[0].get("depth"), 0))
    frame = settings.get("frame")
    if frame is not None and _bounds(frame) is None:
        raise ValueError("The illustration frame is invalid.")
    publication_frame = geometry.get("publication_frame")
    if publication_frame is not None and _bounds(publication_frame) is None:
        raise ValueError("The publication frame is invalid.")
    bounds = _bounds(frame) or _bounds(extent) or _bounds(geometry.get("bounds")) or (-1, -1, 1, 1)
    margin = max(0, min(0.25, _finite(settings.get("margin_fraction"), 0)))
    padding = min(min(width, height) / 4, maximum_stroke / 2 + 2) + margin * min(width, height)
    padding = 0 if settings.get("match_projection") and frame is not None else min(min(width, height) / 2 - 0.5, padding)
    span_x, span_y = max(bounds[2] - bounds[0], 0.001), max(bounds[3] - bounds[1], 0.001)
    units_per_pixel = max(span_x / max(1, width - 2 * padding), span_y / max(1, height - 2 * padding))
    view_width, view_height = width * units_per_pixel, height * units_per_pixel
    x = (bounds[0] + bounds[2] - view_width) / 2
    y = (bounds[1] + bounds[3] - view_height) / 2
    if publication_frame is not None:
        left, top, right, bottom = _bounds(publication_frame)
        x, y = left, top
        view_width, view_height = max(right - left, 0.001), max(bottom - top, 0.001)
        units_per_pixel = max(view_width / width, view_height / height)
    view_box = " ".join(map(_number, (x, y, view_width, view_height)))
    output = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_number(width_mm) + "mm" if is_print else width}" height="{_number(height_mm) + "mm" if is_print else height}" viewBox="{view_box}" role="img">',
        '<title>Molecular illustration</title>',
    ]
    if is_print or "dpi" in settings:
        metadata = dict(metadata) if isinstance(metadata, dict) else {"details": metadata} if metadata is not None else {}
        metadata["export"] = {"width_px": width, "height_px": height, "dpi": dpi,
                              "width_mm": width_mm, "height_mm": height_mm,
                              "size_mode": "print" if is_print else "pixels"}
    if metadata is not None:
        output.append(f'<metadata>{_xml(json.dumps(metadata, ensure_ascii=False, allow_nan=False, default=_metadata_value))}</metadata>')
    if settings.get("transparent", True) is False:
        background = _color(settings.get("background"), "#ffffff")
        output.append(f'<rect x="{_number(x)}" y="{_number(y)}" width="{_number(view_width)}" height="{_number(view_height)}" fill="{background}"/>')
    match_frame = settings.get("match_projection") and frame is not None
    if match_frame:
        left, top, right, bottom = bounds
        output.append('<defs><clipPath id="illustruct-camera-frame">'
                      f'<rect x="{_number(left)}" y="{_number(top)}" width="{_number(right - left)}" height="{_number(bottom - top)}"/>'
                      '</clipPath></defs>')
    for index, entry in enumerate(items):
        if entry[-1]:
            camera_clip = ' clip-path="url(#illustruct-camera-frame)"' if match_frame else ''
            output.append(f'<defs><clipPath id="illustruct-depth-{index}"><path d="{_path_data(entry[-1], True)}" fill-rule="evenodd" clip-rule="evenodd" shape-rendering="crispEdges"{camera_clip}/></clipPath></defs>')
    for index, (item, style, paths, is_line, outline, publication,
                primitives, outlines, cubic_outlines, cubic_lines, occlusion,
                internal_closed, internal_open, silhouette_curves, clip_rings) in enumerate(items):
        identifier = item["id"]
        name = style.get("label") or style.get("name") or item.get("label") or item.get("name") or str(identifier)
        opacity = global_opacity * max(0, min(1, _finite(style.get("opacity"), 1)))
        color = _color(style.get("color"), _DEFAULT_COLOR)
        clip = f' clip-path="url(#illustruct-depth-{index})"' if clip_rings else ' clip-path="url(#illustruct-camera-frame)"' if match_frame else ''
        output.append(f'<g id="{_group_id(identifier, index)}" data-part="{_xml(identifier)}" opacity="{_number(opacity)}"{clip}><title>{_xml(name)}</title>')
        if publication:
            highlight, shadow = lighting_colors(color) if settings.get("shading", True) else (color, color)
            for role, rings, curves in primitives:
                path = _cubic_data(curves) if curves else _path_data(rings, True)
                if path:
                    # Lighting is separate opaque artwork over the unchanged
                    # component colour. Apply transparency once to the group.
                    fill = {"surface": color, "highlight": highlight, "shadow": shadow}.get(role, color)
                    output.append(f'<path data-role="{role}" d="{path}" fill="{fill}" fill-rule="evenodd" clip-rule="evenodd" stroke="none"/>')
            if outline != "none" and stroke_width > 0:
                boundary = _cubic_data(cubic_outlines) if cubic_outlines else _path_data(outlines, True)
                boundary = " ".join(filter(None, (boundary, _path_data(occlusion, False))))
                if boundary:
                    output.append(f'<path data-role="outline" d="{boundary}" fill="none" stroke="{outline}" stroke-width="{_number(stroke_width * units_per_pixel)}" stroke-linecap="round" stroke-linejoin="round"/>')
                if cubic_lines:
                    # These endpoints cut an otherwise continuous silhouette
                    # at a depth-order change. Round caps would overlap and
                    # darken when neighboring fragments are translucent.
                    boundary = _cubic_data(cubic_lines, closed=False)
                    output.append(f'<path data-role="outline" d="{boundary}" fill="none" stroke="{outline}" stroke-width="{_number(stroke_width * units_per_pixel)}" stroke-linecap="butt" stroke-linejoin="round"/>')
                internal = " ".join(filter(None, (_cubic_data(internal_closed),
                                                 _cubic_data(internal_open, closed=False))))
                if internal:
                    size = stroke_width * units_per_pixel
                    dashed = bool(settings.get("dashed_boundaries", False))
                    # The two colour fills are fitted independently. Cover a
                    # subpixel seam with the owning region's base colour so
                    # dash gaps reveal the molecule rather than the canvas.
                    if dashed:
                        output.append(f'<path data-role="internal-boundary-fill" d="{internal}" fill="none" stroke="{color}" stroke-width="{_number(size)}" stroke-linecap="butt" stroke-linejoin="round"/>')
                    dash = f' stroke-dasharray="{_number(4 * size)} {_number(3 * size)}"' if dashed else ''
                    role = "internal-outline" if dashed else "outline"
                    output.append(f'<path data-role="{role}" d="{internal}" fill="none" stroke="{outline}" stroke-width="{_number(size)}" stroke-linecap="butt" stroke-linejoin="round"{dash}/>')
        elif is_line:
            output.extend(_stroke_paths(paths, "backbone", line_width, color, outline, stroke_width, units_per_pixel))
        else:
            path = _cubic_data(silhouette_curves) if silhouette_curves else _path_data(paths, True)
            if path:
                output.append(f'<path d="{path}" fill="{color}" fill-rule="evenodd" clip-rule="evenodd" stroke="{outline}" stroke-width="{_number(stroke_width * units_per_pixel)}" stroke-linejoin="round"/>')
        output.append('</g>')
    output.append('</svg>')
    return "\n".join(output) + "\n"


def _atomic_save(path, writer):
    destination = Path(path).expanduser()
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent))
    os.close(descriptor)
    try:
        writer(temporary)
        with open(temporary, "rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return destination


def export_svg(path, geometry, settings, styles=None, metadata=None):
    """Atomically write the illustration SVG and return its destination Path."""
    svg = build_svg(geometry, settings, styles=styles, metadata=metadata)
    return _atomic_save(path, lambda temporary: Path(temporary).write_text(svg, encoding="utf-8"))


def svg_renderer_class():
    """Return a native SVG renderer with camera clipping and complete paths.

    QtSvg does not implement SVG clipPath. The native painter applies the same
    rectangular clip for previews and PNGs, while exported SVG remains standard.
    Oversized generated paths use the numeric QPainter renderer to avoid QtSvg
    silently truncating complex artwork.
    """
    global _RENDERER_CLASS
    if _RENDERER_CLASS is not None:
        return _RENDERER_CLASS
    try:
        from Qt.QtSvg import QSvgRenderer
    except ImportError:
        # ChimeraX 1.10's Qt shim omits QtSvg; use its selected native binding.
        import Qt
        for flag, binding in (("using_pyqt6", "PyQt6"), ("using_pyside6", "PySide6"), ("using_pyqt5", "PyQt5"), ("using_pyside2", "PySide2")):
            if getattr(Qt, flag, False):
                QSvgRenderer = importlib.import_module(f"{binding}.QtSvg").QSvgRenderer
                break
        else:
            raise ImportError("No Qt SVG renderer is available.")
    from Qt.QtCore import QByteArray, QRectF, Qt
    from Qt.QtGui import QColor

    class IllustrationSvgRenderer(QSvgRenderer):
        def __init__(self, contents=None, parent=None):
            self._camera_frame = None
            self._background = None
            self._native_svg = None
            if isinstance(contents, (bytes, bytearray, QByteArray)):
                super().__init__(parent)
                self.load(contents)
            else:
                super().__init__(contents)
            self.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)

        def load(self, contents):
            self._camera_frame = None
            self._background = None
            self._native_svg = None
            if isinstance(contents, (bytes, bytearray, QByteArray)) and contents:
                try:
                    root = ET.fromstring(bytes(contents))
                    namespace = {"svg": "http://www.w3.org/2000/svg"}
                    clip = root.find("svg:defs/svg:clipPath[@id='illustruct-camera-frame']/svg:rect", namespace)
                    if clip is not None:
                        self._camera_frame = tuple(float(clip.attrib[key]) for key in ("x", "y", "width", "height"))
                        background = root.find("svg:rect", namespace)
                        if background is not None:
                            self._background = _color(background.get("fill"), "#ffffff")
                    # Qt 6.8 truncates a path after 32767 QPainterPath
                    # elements; one cubic consumes three. Keep the complete
                    # compound path (and its holes) in our numeric renderer.
                    # Never feed an oversized path to QtSvg first: isValid()
                    # still returns True after that silent partial rendering.
                    if root.find("svg:defs/svg:clipPath/svg:path", namespace) is not None or any(
                            _path_element_count(path.get("d", "")) > 32000 for path in root.findall(".//svg:path", namespace)):
                        self._native_svg = bytes(contents)
                        contents = QByteArray(ET.tostring(ET.Element(root.tag, root.attrib)))
                except (ET.ParseError, ValueError, KeyError):
                    pass
            return super().load(contents)

        def render(self, painter, *arguments):
            if self._native_svg is not None and (not arguments or
                    len(arguments) == 1 and isinstance(arguments[0], QRectF)):
                target = arguments[0] if arguments else QRectF(painter.viewport())
                return _paint_vector_pdf(painter, self._native_svg, target, raster_groups=True)
            if self._camera_frame is None or len(arguments) != 1 or not isinstance(arguments[0], QRectF):
                return super().render(painter, *arguments)
            target = arguments[0]
            view = self.viewBoxF()
            if view.width() <= 0 or view.height() <= 0:
                return
            x, y, width, height = self._camera_frame
            scale = min(target.width() / view.width(), target.height() / view.height())
            offset_x = target.x() + (target.width() - view.width() * scale) / 2
            offset_y = target.y() + (target.height() - view.height() * scale) / 2
            clip = QRectF(offset_x + (x - view.x()) * scale,
                          offset_y + (y - view.y()) * scale,
                          width * scale, height * scale)
            painter.save()
            try:
                if self._background is not None:
                    painter.fillRect(target, QColor(self._background))
                painter.setClipRect(clip, Qt.ClipOperation.IntersectClip)
                return super().render(painter, target)
            finally:
                painter.restore()

    _RENDERER_CLASS = IllustrationSvgRenderer
    return _RENDERER_CLASS


def _path_element_count(path):
    # Generated SVG uses only these absolute commands. Count conservatively:
    # closing a path may add an element, even when an explicit segment closes it.
    return sum(path.count(command) * count for command, count in (("M", 1), ("L", 1), ("C", 3), ("Z", 1)))


def _qt_shape(element):
    """Read only the numeric SVG primitives emitted by this module."""
    from Qt.QtCore import QRectF, Qt
    from Qt.QtGui import QPainterPath
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.OddEvenFill if element.get("fill-rule") == "evenodd" else Qt.FillRule.WindingFill)
    tag = element.tag.rsplit("}", 1)[-1]
    if tag == "circle":
        x, y, radius = (float(element.get(key, 0)) for key in ("cx", "cy", "r"))
        path.addEllipse(QRectF(x - radius, y - radius, 2 * radius, 2 * radius))
    elif tag == "rect":
        path.addRect(QRectF(*(float(element.get(key, 0)) for key in ("x", "y", "width", "height"))))
    elif tag == "path":
        tokens = re.findall(r"[MLCZ]|[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?", element.get("d", ""))
        index = 0
        while index < len(tokens):
            command = tokens[index]
            index += 1
            count = {"M": 2, "L": 2, "C": 6, "Z": 0}.get(command)
            if count is None:
                raise ValueError("Unsupported path command in illustration PDF.")
            values = [float(value) for value in tokens[index:index + count]]
            index += count
            if command == "M":
                path.moveTo(*values)
            elif command == "L":
                path.lineTo(*values)
            elif command == "C":
                path.cubicTo(*values)
            else:
                path.closeSubpath()
    return path


def _qt_pen(element):
    from Qt.QtCore import Qt
    from Qt.QtGui import QColor, QPen
    stroke = element.get("stroke", "none")
    width = float(element.get("stroke-width", 0))
    if stroke == "none" or width <= 0:
        return QPen(Qt.PenStyle.NoPen)
    pen = QPen(QColor(stroke))
    pen.setWidthF(width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap if element.get("stroke-linecap") == "round" else Qt.PenCapStyle.FlatCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin if element.get("stroke-linejoin") == "round" else Qt.PenJoinStyle.MiterJoin)
    dash = element.get("stroke-dasharray", "none").strip()
    if dash and dash != "none":
        values = [float(value) for value in re.split(r"[\s,]+", dash)]
        if not values or any(not math.isfinite(value) or value < 0 for value in values) or sum(values) <= 0:
            raise ValueError("Stroke dash lengths must be finite, nonnegative, and not all zero.")
        # SVG uses scene units; QPen uses multiples of the pen width. SVG
        # repeats an odd-length list twice to obtain a complete dash/gap cycle.
        if len(values) % 2:
            values *= 2
        pen.setDashPattern([value / width for value in values])
        offset = float(element.get("stroke-dashoffset", 0))
        if not math.isfinite(offset):
            raise ValueError("Stroke dash offset must be finite.")
        pen.setDashOffset(offset / width)
    return pen


def _qt_gradients(root, namespace):
    """Read the opaque directional gradients in shared scene coordinates."""
    from Qt.QtGui import QBrush, QColor, QLinearGradient
    brushes = {}
    for element in root.findall("svg:defs/svg:linearGradient", namespace):
        gradient = QLinearGradient(*(float(element.get(key)) for key in ("x1", "y1", "x2", "y2")))
        for stop in element.findall("svg:stop", namespace):
            gradient.setColorAt(float(stop.get("offset")), QColor(stop.get("stop-color")))
        brush = QBrush(gradient)
        brushes[f"url(#{element.get('id')})"] = brush
    return brushes


def _paint_pdf_group(painter, elements, opacity, gradients=None):
    from Qt.QtCore import Qt
    from Qt.QtGui import QBrush, QColor, QPainterPath, QPainterPathStroker, QLinearGradient
    if opacity <= 0:
        return
    gradients = gradients or {}
    def brush(paint):
        return gradients[paint] if paint in gradients else QBrush(QColor(paint))
    operations = []
    for element in elements:
        if element.tag.rsplit("}", 1)[-1] not in ("path", "circle", "rect"):
            continue
        path = _qt_shape(element)
        fill = element.get("fill", "none")
        pen = _qt_pen(element)
        if opacity >= 1:
            painter.setPen(pen)
            painter.setBrush(brush(fill) if fill != "none" else QBrush(Qt.BrushStyle.NoBrush))
            painter.drawPath(path)
            continue
        if fill != "none":
            operations.append((path, fill))
        if pen.style() != Qt.PenStyle.NoPen:
            stroker = QPainterPathStroker(pen)
            operations.append((stroker.createStroke(path), element.get("stroke")))
    if opacity >= 1:
        return
    # QtSvg rasterizes translucent groups when painting to PDF. Resolve their
    # overlapping fill/stroke regions as vector paths instead, then apply alpha
    # exactly once. Opaque groups retain their original editable cubic paths.
    covered = QPainterPath()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setOpacity(opacity)
    for path, color in reversed(operations):
        visible = path if covered.isEmpty() else path.subtracted(covered)
        if not visible.isEmpty():
            paint = brush(color)
            if color in gradients:
                # QPdfWriter's shading operator ignores painter opacity for
                # opaque gradient stops. Carry uniform alpha in the stops so
                # the PDF's vector soft mask matches solid paths and PNG.
                gradient = QLinearGradient(paint.gradient())
                for position, stop in gradient.stops():
                    stop.setAlphaF(opacity)
                    gradient.setColorAt(position, stop)
                translucent = QBrush(gradient)
                translucent.setTransform(paint.transform())
                paint = translucent
                painter.setOpacity(1)
            else:
                painter.setOpacity(opacity)
            painter.setBrush(paint)
            painter.drawPath(visible)
        covered = path if covered.isEmpty() else covered.united(path)


def _paint_raster_group(painter, elements, opacity, canvas, force_buffer=False, gradients=None):
    """Composite group alpha once without QtSvg's path-element limit."""
    if opacity >= 1 and not force_buffer:
        return _paint_pdf_group(painter, elements, 1, gradients)
    if opacity <= 0 or canvas.isEmpty():
        return
    from Qt.QtCore import QRectF
    from Qt.QtGui import QImage, QPainter, QTransform
    # The target has already been mapped to physical device pixels. A temporary
    # group image preserves SVG alpha at overlaps and holes; only PNG/preview
    # use this branch. PDF continues to resolve alpha as editable vector paths.
    image = QImage(canvas.width(), canvas.height(), QImage.Format.Format_ARGB32_Premultiplied)
    if image.isNull():
        raise RuntimeError("The illustration transparency buffer could not be allocated.")
    image.fill(0)
    group_painter = QPainter(image)
    try:
        transform = painter.deviceTransform()
        group_painter.setWorldTransform(QTransform(transform.m11(), transform.m12(), transform.m13(),
            transform.m21(), transform.m22(), transform.m23(),
            transform.dx() - canvas.left(), transform.dy() - canvas.top(), transform.m33()))
        group_painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        _paint_pdf_group(group_painter, elements, 1, gradients)
    finally:
        group_painter.end()
    painter.save()
    try:
        alpha = painter.opacity()
        painter.resetTransform()
        # QWidget painting retains a backing-store offset after resetting the
        # user transform. Convert the physical buffer rectangle back through
        # that device transform, including its offset and high-DPI scale.
        # Dividing only by DPR applies a nested widget's offset twice and can
        # move clipped backbones completely outside their visible paint area.
        device_to_logical, _ = painter.deviceTransform().inverted()
        painter.setOpacity(alpha * opacity)
        painter.drawImage(device_to_logical.mapRect(QRectF(canvas)), image)
    finally:
        painter.restore()


def _paint_vector_pdf(painter, svg, target, raster_groups=False):
    from Qt.QtCore import QRectF, Qt
    from Qt.QtGui import QPainter
    root = ET.fromstring(svg)
    x, y, width, height = map(float, root.get("viewBox").split())
    scale = min(target.width() / width, target.height() / height)
    canvas = painter.deviceTransform().mapRect(target).toAlignedRect() if raster_groups else None
    painter.save()
    try:
        painter.translate(target.x() + (target.width() - width * scale) / 2,
                          target.y() + (target.height() - height * scale) / 2)
        painter.scale(scale, scale)
        painter.translate(-x, -y)
        namespace = {"svg": "http://www.w3.org/2000/svg"}
        gradients = _qt_gradients(root, namespace)
        background = root.find("svg:rect", namespace)
        if background is not None:
            _paint_pdf_group(painter, [background], 1)
        clips = {}
        for definition in root.findall("svg:defs/svg:clipPath", namespace):
            from Qt.QtGui import QPainterPath
            shape = QPainterPath()
            shape.setFillRule(Qt.FillRule.OddEvenFill)
            for element in definition:
                path = _qt_shape(element)
                parent_clip = clips.get(element.get('clip-path'))
                shape.addPath(path.intersected(parent_clip) if parent_clip is not None else path)
            clips[f"url(#{definition.get('id')})"] = shape
        for group in root.findall("svg:g", namespace):
            painter.save()
            try:
                clip = clips.get(group.get("clip-path"))
                if clip is not None:
                    # These clips partition one complete vector stroke. A
                    # fractional clip mask would composite both half-covered
                    # fragments independently and leave an internal hairline.
                    # Clip pixels once; the painted stroke itself remains AA.
                    antialias = painter.testRenderHint(QPainter.RenderHint.Antialiasing)
                    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
                    painter.setClipPath(clip, Qt.ClipOperation.IntersectClip)
                    if not raster_groups:
                        painter.setRenderHint(QPainter.RenderHint.Antialiasing, antialias)
                opacity = float(group.get("opacity", 1))
                if raster_groups:
                    # Composite fill and outline before clipping, even at
                    # full opacity. Applying a fractional clip independently
                    # to those paints leaves a dark outline-colored seam.
                    _paint_raster_group(painter, list(group), opacity, canvas, force_buffer=clip is not None, gradients=gradients)
                else:
                    _paint_pdf_group(painter, list(group), opacity, gradients)
            finally:
                painter.restore()
    finally:
        painter.restore()


def _exact_pdf_page(path, width_mm, height_mm):
    # QPdfWriter quantizes its page boxes to whole points. Replace those numeric
    # fields in place so physical print dimensions stay exact without changing
    # stream lengths or cross-reference offsets.
    data = Path(path).read_bytes()
    if not re.search(rb"/MediaBox\s*\[([^]]+)\]", data):
        raise RuntimeError("Qt did not write a valid PDF page.")
    box = f"0 0 {width_mm * 72 / 25.4:.6f} {height_mm * 72 / 25.4:.6f}".encode("ascii")
    def replace_box(match):
        if len(box) > len(match.group(2)):
            raise RuntimeError("Qt wrote an unsupported PDF page size representation.")
        return match.group(1) + box.ljust(len(match.group(2))) + b"]"
    data = re.sub(rb"(/(?:MediaBox|TrimBox|CropBox)\s*\[)([^]]+)\]", replace_box, data)
    Path(path).write_bytes(data)


def export_pdf(path, geometry, settings, styles=None, metadata=None):
    """Atomically write a physical-size PDF with editable vector artwork.

    Native QPainter paths preserve translucent parts as vector artwork.
    """
    settings = settings or {}
    svg = build_svg(geometry, settings, styles=styles, metadata=metadata)
    width, height, dpi, width_mm, height_mm, _ = _output_spec(settings)
    try:
        from Qt.QtCore import QByteArray, QMarginsF, QRectF, QSizeF
        from Qt.QtGui import QGuiApplication, QPageLayout, QPageSize, QPainter, QPdfWriter
    except ImportError as error:
        raise RuntimeError("PDF export needs ChimeraX's native Qt support. Export SVG instead.") from error
    if not isinstance(QGuiApplication.instance(), QGuiApplication):
        raise RuntimeError("PDF export requires a running Qt application. Export SVG in a headless session.")

    def save(temporary):
        writer = QPdfWriter(temporary)
        writer.setResolution(round(dpi))
        writer.setCreator("Illustruct — editable vector artwork")
        writer.setTitle("Molecular illustration")
        details = dict(metadata) if isinstance(metadata, dict) else {}
        details.update(width_px=width, height_px=height, width_mm=width_mm, height_mm=height_mm, dpi=dpi,
                       content_type="vector")
        xmp = ('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
               f'<rdf:Description rdf:about="" xmlns:illustruct="urn:illustruct:export:1" illustruct:dpi="{_number(dpi)}" '
               f'illustruct:widthMm="{_number(width_mm)}" illustruct:heightMm="{_number(height_mm)}" '
               'illustruct:contentType="vector">'
               f'<illustruct:details>{_xml(json.dumps(details, ensure_ascii=False, allow_nan=False, default=_metadata_value))}</illustruct:details>'
               '</rdf:Description></rdf:RDF></x:xmpmeta>')
        writer.setDocumentXmpMetadata(QByteArray(xmp.encode("utf-8")))
        page = QPageSize(QSizeF(width_mm, height_mm), QPageSize.Unit.Millimeter,
                         "Illustration", QPageSize.SizeMatchPolicy.ExactMatch)
        writer.setPageSize(page)
        writer.setPageMargins(QMarginsF(0, 0, 0, 0), QPageLayout.Unit.Millimeter)
        layout = writer.pageLayout()
        layout.setMode(QPageLayout.Mode.FullPageMode)
        writer.setPageLayout(layout)
        painter = QPainter(writer)
        if not painter.isActive():
            raise RuntimeError("Qt could not begin the PDF document.")
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            # Compensate for the native page's rounded top origin before its
            # MediaBox is corrected to the requested fractional point size.
            rounded_height = page.sizePoints().height()
            painter.translate(0, (rounded_height - height_mm * 72 / 25.4) * writer.resolution() / 72)
            target = QRectF(0, 0, width_mm * writer.resolution() / 25.4,
                            height_mm * writer.resolution() / 25.4)
            _paint_vector_pdf(painter, svg, target)
        finally:
            painter.end()
            del writer
        _exact_pdf_page(temporary, width_mm, height_mm)

    return _atomic_save(path, save)


def export_png(path, geometry, settings, styles=None, metadata=None):
    """Atomically rasterize the illustration SVG with native QtSvg.

    Call from the Qt application thread. The caller owns the running Qt
    application; a headless command can always export SVG without Qt.
    """
    svg = build_svg(geometry, settings, styles=styles, metadata=metadata)
    width, height, dpi, _, _, _ = _output_spec(settings or {})
    if width * height > 67_108_864:
        raise ValueError("PNG dimensions are too large. Choose an image under 64 megapixels.")
    try:
        from Qt.QtCore import QByteArray, QRectF
        from Qt.QtGui import QGuiApplication, QImage, QPainter
        QSvgRenderer = svg_renderer_class()
    except ImportError as error:
        raise RuntimeError("PNG export needs ChimeraX's Qt SVG support. Export SVG instead.") from error
    if not isinstance(QGuiApplication.instance(), QGuiApplication):
        raise RuntimeError("PNG export requires a running Qt application. Export SVG in a headless session.")
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    if image.isNull():
        raise RuntimeError("The PNG image could not be allocated. Choose smaller dimensions.")
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    if not renderer.isValid():
        raise RuntimeError("Qt could not read the illustration SVG.")
    image.fill(0)
    painter = QPainter(image)
    if not painter.isActive():
        raise RuntimeError("Qt could not render the PNG image.")
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        renderer.render(painter, QRectF(0, 0, width, height))
    finally:
        painter.end()
    image.setDotsPerMeterX(round(dpi / 0.0254))
    image.setDotsPerMeterY(round(dpi / 0.0254))

    def save(temporary):
        if not image.save(temporary, "PNG"):
            raise OSError("Qt could not write the PNG image.")

    return _atomic_save(path, save)
