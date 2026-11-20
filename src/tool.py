"""A compact native Qt tool. Structure manipulation stays in ChimeraX."""
from copy import deepcopy
import json
from math import floor, hypot
from pathlib import Path

from Qt.QtCore import Qt, QTimer, QByteArray
from Qt.QtGui import QColor, QIcon, QPixmap, QPalette
from Qt.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGridLayout,
    QLabel, QPushButton, QComboBox, QCheckBox, QSlider, QSpinBox,
    QDoubleSpinBox, QGroupBox, QListWidget, QListWidgetItem,
    QFileDialog, QScrollArea, QAbstractItemView, QSplitter, QMessageBox, QSizePolicy)
from chimerax.atomic import Structure
from chimerax.core.tools import ToolInstance
from chimerax.ui import MainToolWindow

from .state import (settings_with, compute_scene, effective_styles, remap_model_ids,
                    part_group, part_category, component_visible, simplification_abstraction,
                    simplification_value, smoothing_amount, smoothing_value,
                    inherit_region_styles)
from .state import NUCLEIC_STYLES
from .scene import capture_scene, camera_snapshot, list_structure_models
from .geometry import GeometryEngine
from .export import build_svg, export_svg, export_png, export_pdf
from .preview import Preview
from .worker import RenderWorker
from .activity import ActivityIndicator
from .version import VERSION


COMPONENT_TYPES = (("proteins", "Proteins"), ("nucleic", "DNA/RNA"),
                   ("ligands", "Ligands"), ("ions", "Ions"))
VISIBILITY_KEYS = ("show_chains", "show_other", *("show_" + key for key, _ in COMPONENT_TYPES))


class _PartItem(QListWidgetItem):
    def __lt__(self, other):
        return (self.text().casefold(), self.data(Qt.UserRole)) < (
            other.text().casefold(), other.data(Qt.UserRole))


class IllustructTool(ToolInstance):
    SESSION_SAVE = True
    help = "help:user/tools/illustruct.html"

    @classmethod
    def get_singleton(cls, session):
        existing = next((t for t in session.tools.list() if isinstance(t, cls)), None)
        if existing:
            existing.tool_window.shown = True
            return existing
        return cls(session)

    def __init__(self, session, tool_name="Illustruct"):
        super().__init__(session, tool_name)
        self.settings = settings_with()
        self.styles = {}
        self.frame = None
        self.scene = None
        self.geometry = None
        self._closed = False
        self._export_dialog_open = False
        self._busy = False
        self._pending = False
        self._revision = 0
        self._content_revision = 0
        self._completed_revision = -1
        self._paint_revision = 0
        self._compute_snapshot_key = None
        self._compute_settings = self._compute_styles = self._compute_paint = None
        self._preview_dragging = False
        self._preview_drag_moved = False
        self._camera_motion = False
        self._interaction_resolution = 320
        self._active_interactive = False
        self._scene_dirty = True
        self._captured_scene = None
        self._last_camera = None
        self._handlers = []
        self._populating = False
        self._engine = GeometryEngine()
        self.tool_window = MainToolWindow(self)
        self._worker = RenderWorker(self.tool_window.ui_area)
        self._worker.result.connect(self._finished)
        self._timer = QTimer(self.tool_window.ui_area)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._start_compute)
        self._settle_timer = QTimer(self.tool_window.ui_area)
        self._settle_timer.setSingleShot(True)
        self._settle_timer.timeout.connect(self._finish_camera_motion)
        self._build_ui()
        self._watch_session()
        self.tool_window.manage("side")
        self.request_update()

    def _build_ui(self):
        parent = self.tool_window.ui_area
        parent.setMinimumWidth(340)
        layout = QVBoxLayout(parent)
        layout.setContentsMargins(10, 8, 10, 8)
        source_row = QHBoxLayout()
        self.source = QComboBox()
        self.source.setAccessibleName("Structure source")
        self.source.currentIndexChanged.connect(self._source_changed)
        source_row.addWidget(self.source, 1)
        self.refresh_button = QPushButton("Update")
        self.refresh_button.clicked.connect(lambda: self.request_update(force=True))
        source_row.addWidget(self.refresh_button)
        self.activity = ActivityIndicator(parent)
        source_row.addWidget(self.activity)
        layout.addLayout(source_row)
        row = QHBoxLayout()
        self.selection_only = QCheckBox("Selection only")
        self.selection_only.setToolTip("Use selected atoms or residues, including hidden selections.")
        self.selection_only.toggled.connect(self._selection_changed)
        self.live = QCheckBox("Follow view")
        self.live.setToolTip("Update automatically when the ChimeraX view changes.")
        self.live.setChecked(True)
        self.live.toggled.connect(lambda v: self._change("live", v))
        row.addWidget(self.selection_only)
        row.addStretch()
        row.addWidget(self.live)
        layout.addLayout(row)
        self.show_chains = QCheckBox("Chains")
        self.show_chains.setToolTip("Show protein and nucleic acid chains, including selected domains.")
        self.show_other = QCheckBox("Other")
        self.show_other.setToolTip("Show ligands, ions, and other non-polymer parts.")
        for checkbox, key in ((self.show_chains, "show_chains"), (self.show_other, "show_other")):
            checkbox.setChecked(self.settings[key])
            checkbox.toggled.connect(lambda value, k=key: self._change(k, value, geometry=False))
        self.splitter = QSplitter(Qt.Vertical, parent)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(8)
        self.splitter.setAccessibleName("Preview and settings divider")
        self.splitter.setStyleSheet("QSplitter::handle:vertical { background: palette(mid); border-radius: 3px; margin: 2px 0; }")
        preview_area = QWidget()
        preview_layout = QVBoxLayout(preview_area)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        self.preview = Preview(preview_area)
        self.preview.drag_started.connect(self._preview_drag_started)
        self.preview.rotated.connect(self._preview_rotate)
        self.preview.drag_finished.connect(self._preview_drag_finished)
        preview_layout.addWidget(self.preview, 1)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        self.status.hide()
        preview_layout.addWidget(self.status)
        self.splitter.addWidget(preview_area)
        layout.addWidget(self.splitter, 1)

        self.controls = controls = QWidget()
        controls.setMaximumWidth(480)
        controls.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(10)
        controls_layout.setAlignment(Qt.AlignTop)
        appearance, appearance_layout = self._section("Appearance", controls_layout)
        form = self._form()
        self.nucleic_style = QComboBox()
        self.nucleic_style.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.nucleic_style.setAccessibleName("DNA/RNA rendering")
        self.nucleic_style.setToolTip("Render DNA/RNA as filled silhouettes or simple backbones.")
        for key, title in NUCLEIC_STYLES.items():
            self.nucleic_style.addItem(title, key)
        self.nucleic_style.currentIndexChanged.connect(
            lambda index: self._change("nucleic_style", self.nucleic_style.itemData(index)))
        form.addRow("DNA/RNA", self.nucleic_style)
        self.opacity = self._slider(100)
        self.opacity.setAccessibleName("Overall opacity")
        self.opacity.valueChanged.connect(lambda v: self._change("opacity", v/100, geometry=False))
        form.addRow("Opacity", self._slider_field(self.opacity))
        self.simplification = self._slider(floor(100 * simplification_value(self.settings["abstraction"]) + 0.5))
        self.simplification.setAccessibleName("Global simplification")
        self.simplification.setToolTip("Higher values produce broader, simpler shapes. 100% gives the most abstract silhouette.")
        self.simplification.valueChanged.connect(lambda v: self._change("abstraction", simplification_abstraction(v / 100)))
        form.addRow("Simplification", self._slider_field(self.simplification))
        self.smoothing = self._slider(floor(100 * smoothing_value(self.settings["smoothing"]) + 0.5))
        self.smoothing.setAccessibleName("Shape smoothing")
        self.smoothing.setToolTip("Smooth silhouettes and DNA/RNA backbones. 100% gives the strongest smoothing.")
        self.smoothing.valueChanged.connect(lambda v: self._change("smoothing", smoothing_amount(v / 100)))
        form.addRow("Smoothing", self._slider_field(self.smoothing))
        self.minimum_piece_size = self._slider(floor(100 * self.settings["minimum_piece_size"] + 0.5))
        self.minimum_piece_size.setAccessibleName("Minimum piece size")
        self.minimum_piece_size.setToolTip("Hide disconnected silhouette pieces whose visible area is below this percentage of the largest piece in their component. 0% keeps all; 100% keeps only the largest pieces. Every component keeps its largest piece. DNA/RNA backbones are unchanged.")
        self.minimum_piece_size.valueChanged.connect(lambda v: self._change("minimum_piece_size", v / 100))
        form.addRow("Minimum piece size", self._slider_field(self.minimum_piece_size))
        self.shading = QCheckBox("Highlights && shadows")
        self.shading.setAccessibleName("Highlights and shadows")
        self.shading.setToolTip("Overlay lighter highlights and darker shadows derived from each component color. Backbones stay flat.")
        self.shading.setChecked(self.settings.get("shading", True))
        self.shading.toggled.connect(lambda v: self._change("shading", v, geometry=False))
        form.addRow(self.shading)
        for key, label in (("highlight_size", "Highlight size"), ("shadow_size", "Shadow size")):
            slider = self._slider(floor(100 * self.settings[key] + 0.5), maximum=200)
            setattr(self, key, slider)
            slider.setAccessibleName(label)
            slider.setToolTip(f"Adjust the {label.lower()} independently. 0% hides it; 100% is the default width; 200% doubles it.")
            slider.valueChanged.connect(lambda value, key=key: self._change(key, value / 100))
            form.addRow(label, self._slider_field(slider))
        self.outline = QCheckBox("Outline")
        self.outline.setChecked(True)
        self.outline.toggled.connect(lambda v: self._change("outline", v, geometry=False))
        self.stroke_width = self._number(self.settings["stroke_width"])
        self.stroke_width.setAccessibleName("Outline width in pixels")
        self.stroke_width.valueChanged.connect(lambda v: self._width_changed("stroke_width", v))
        form.addRow(self.outline, self.stroke_width)
        self.dashed_boundaries = QCheckBox("Dashed colour boundaries")
        self.dashed_boundaries.setChecked(self.settings.get("dashed_boundaries", False))
        self.dashed_boundaries.setToolTip("Dash borders between differently coloured regions of the same protein chain. Outer contours and borders between separate chains stay solid.")
        self.dashed_boundaries.toggled.connect(lambda v: self._change("dashed_boundaries", v))
        form.addRow(self.dashed_boundaries)
        self.tinted_outline = QCheckBox("Tint outline to match")
        self.tinted_outline.setToolTip("Use a darker shade of each component's fill for its outline.")
        self.tinted_outline.toggled.connect(lambda v: self._change("tinted_outline", v, geometry=False))
        form.addRow(self.tinted_outline)
        appearance_layout.addLayout(form)

        parts_box, parts_layout = self._section("Components", controls_layout)
        visibility_row = QHBoxLayout()
        visibility_row.setSpacing(8)
        visibility_row.addWidget(self.show_chains)
        visibility_row.addWidget(self.show_other)
        visibility_row.addStretch()
        self.domain = QCheckBox("Split selection")
        self.domain.setToolTip("Additionally separate selected residues into their own parts. Protein colour regions are already separated automatically. Selection updates live.")
        self.domain.toggled.connect(lambda v: self._change("selected_domain", v))
        visibility_row.addWidget(self.domain)
        parts_layout.addLayout(visibility_row)
        type_grid = QGridLayout()
        type_grid.setHorizontalSpacing(12)
        self.type_checks = {}
        for index, (category, label) in enumerate(COMPONENT_TYPES):
            checkbox = QCheckBox(label)
            checkbox.setAccessibleName(label + " visibility")
            checkbox.setChecked(self.settings["show_" + category])
            checkbox.toggled.connect(lambda value, key="show_" + category:
                                     self._change(key, value, geometry=False))
            self.type_checks[category] = checkbox
            type_grid.addWidget(checkbox, index % 2, index // 2)
        parts_layout.addLayout(type_grid)
        list_row = QHBoxLayout()
        list_row.setSpacing(6)
        self.part_filter = QComboBox()
        self.part_filter.setAccessibleName("Component list filter")
        self.part_filter.setToolTip("Filter the list without changing the illustration.")
        self.part_filter.addItem("All types", None)
        for category, label in COMPONENT_TYPES:
            self.part_filter.addItem(label, category)
        self.part_filter.currentIndexChanged.connect(self._populate_parts)
        list_row.addWidget(self.part_filter, 1)
        self.show_listed_button = QPushButton("Show")
        self.hide_listed_button = QPushButton("Hide")
        self.show_listed_button.setAccessibleName("Show listed components")
        self.hide_listed_button.setAccessibleName("Hide listed components")
        self.show_listed_button.setToolTip("Show every component in this list, enabling its type and group if needed.")
        self.hide_listed_button.setToolTip("Hide every component in this list. Other types keep their visibility.")
        self.show_listed_button.clicked.connect(lambda: self._set_listed_visibility(True))
        self.hide_listed_button.clicked.connect(lambda: self._set_listed_visibility(False))
        list_row.addWidget(self.show_listed_button)
        list_row.addWidget(self.hide_listed_button)
        parts_layout.addLayout(list_row)
        self.parts_list = QListWidget()
        self.parts_list.setAccessibleName("Illustration components")
        self.parts_list.setSortingEnabled(True)
        self.parts_list.setFixedHeight(190)
        self.parts_list.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.parts_list.itemChanged.connect(self._part_changed)
        parts_layout.addWidget(self.parts_list)

        output, output_layout = self._section("Export", controls_layout)
        output_form = self._form()
        self.size_mode = QComboBox()
        self.size_mode.addItem("Pixels", "pixels")
        self.size_mode.addItem("Print", "print")
        self.size_mode.setAccessibleName("Export sizing")
        self.size_mode.activated.connect(lambda i: self._change("size_mode", self.size_mode.itemData(i), geometry=False))
        output_form.addRow("Sizing", self.size_mode)
        size_row = QHBoxLayout()
        size_row.setSpacing(6)
        self.width = QSpinBox()
        self.height = QSpinBox()
        for spin, key in ((self.width, "width"), (self.height, "height")):
            spin.setRange(64, 16384)
            spin.setValue(self.settings[key])
            spin.valueChanged.connect(lambda v, k=key: self._change(k, v, geometry=False))
        self.width.setAccessibleName("Export width in pixels")
        self.height.setAccessibleName("Export height in pixels")
        self.width.setMaximumWidth(100)
        self.height.setMaximumWidth(100)
        size_row.addWidget(self.width)
        size_row.addWidget(QLabel("×"))
        size_row.addWidget(self.height)
        size_row.addStretch()
        output_form.addRow("Size (px)", size_row)
        self.print_width = QDoubleSpinBox()
        self.print_height = QDoubleSpinBox()
        print_row = QHBoxLayout()
        print_row.setSpacing(6)
        for spin, key in ((self.print_width, "print_width_mm"), (self.print_height, "print_height_mm")):
            spin.setRange(5, 1000)
            spin.setDecimals(2)
            spin.setMaximumWidth(100)
            spin.setAccessibleName("Print " + ("width" if key == "print_width_mm" else "height") + " in millimetres")
            spin.valueChanged.connect(lambda v, k=key: self._change(k, v, geometry=False))
        print_row.addWidget(self.print_width)
        print_row.addWidget(QLabel("×"))
        print_row.addWidget(self.print_height)
        print_row.addStretch()
        output_form.addRow("Size (mm)", print_row)
        self.dpi = QSpinBox()
        self.dpi.setRange(72, 1200)
        self.dpi.setSingleStep(50)
        self.dpi.setMaximumWidth(100)
        self.dpi.setAccessibleName("Export resolution in dots per inch")
        self.dpi.valueChanged.connect(lambda v: self._change("dpi", v, geometry=False))
        output_form.addRow("DPI", self.dpi)
        self.margin = self._slider(6)
        self.margin.setMaximum(25)
        self.margin.setAccessibleName("Illustration margin")
        self.margin.valueChanged.connect(lambda v: self._change("margin_fraction", v / 100, geometry=False))
        output_form.addRow("Margin", self._slider_field(self.margin))
        output_layout.addLayout(output_form)
        self.transparent = QCheckBox("Transparent background")
        self.transparent.setChecked(True)
        self.transparent.toggled.connect(lambda v: self._change("transparent", v, geometry=False))
        output_layout.addWidget(self.transparent)
        self.projection = QCheckBox("Match camera")
        self.projection.setToolTip("Use the ChimeraX camera's perspective and framing instead of fitting the illustration.")
        self.fixed = QCheckBox("Lock frame")
        self.fixed.setToolTip("Keep the current illustration frame. Rotating may crop components.")
        frame_row = QHBoxLayout()
        frame_row.setSpacing(8)
        for checkbox, key in ((self.projection, "match_projection"), (self.fixed, "fixed_frame")):
            checkbox.setChecked(self.settings[key])
            checkbox.toggled.connect(lambda v, k=key: self._change(k, v))
            frame_row.addWidget(checkbox)
        frame_row.addStretch()
        output_layout.addLayout(frame_row)
        self.controls_scroll = scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(controls)
        scroll.setMinimumHeight(100)
        self.splitter.addWidget(scroll)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([360, 420])
        self.splitter.handle(1).setCursor(Qt.SplitVCursor)
        self.splitter.handle(1).setToolTip("Drag to resize the preview and settings")
        export_row = QHBoxLayout()
        self.svg_button = QPushButton("SVG…")
        self.svg_button.setToolTip("Editable vector artwork with named parts.")
        self.png_button = QPushButton("PNG…")
        self.pdf_button = QPushButton("PDF…")
        self.pdf_button.setToolTip("Publication artwork at its specified physical size.")
        self.svg_button.clicked.connect(lambda: self._save("svg"))
        self.png_button.clicked.connect(lambda: self._save("png"))
        self.pdf_button.clicked.connect(lambda: self._save("pdf"))
        export_row.addWidget(QLabel("Export"))
        export_row.addWidget(self.svg_button)
        export_row.addWidget(self.pdf_button)
        export_row.addWidget(self.png_button)
        layout.addLayout(export_row)
        self._enable_export(False)
        self._update_models()
        self._sync_controls()

    def _width_changed(self, key, value):
        if self.settings["size_mode"] == "print":
            key += "_pt"
        self._change(key, value, geometry=False)

    @staticmethod
    def _slider(value, maximum=100):
        slider = QSlider(Qt.Horizontal)
        slider.setRange(0, maximum)
        slider.setValue(value)
        return slider

    @staticmethod
    def _number(value):
        spin = QDoubleSpinBox()
        spin.setRange(0, 64)
        spin.setSingleStep(0.5)
        spin.setSuffix(" px")
        spin.setMaximumWidth(104)
        spin.setValue(value)
        return spin

    @staticmethod
    def _slider_field(slider):
        field = QWidget()
        field.setFocusProxy(slider)
        row = QHBoxLayout(field)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        readout = QLabel(f"{slider.value()}%")
        readout.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        readout.setFixedWidth(readout.fontMetrics().horizontalAdvance(f"{slider.maximum()}%") + 2)
        slider._value_label = readout
        slider.valueChanged.connect(lambda value: readout.setText(f"{value}%"))
        row.addWidget(slider, 1)
        row.addWidget(readout)
        return field

    @staticmethod
    def _form():
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(6)
        form.setFormAlignment(Qt.AlignTop | Qt.AlignLeft)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        return form

    @staticmethod
    def _section(title, layout):
        content = QGroupBox(title)
        content.setAccessibleName(title)
        content.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        inner = QVBoxLayout(content)
        inner.setContentsMargins(8, 10, 8, 10)
        inner.setSpacing(8)
        inner.setAlignment(Qt.AlignTop)
        layout.addWidget(content)
        return content, inner

    def _watch_session(self):
        self._model_owners = {model.id: model for model in self.session.models.list(type=Structure)}
        for name in ("add models", "remove models", "model display changed", "model position changed"):
            self._handlers.append(self.session.triggers.add_handler(name, self._models_changed))
        self._handlers.append(self.session.triggers.add_handler("selection changed", self._selection_event))
        self._handlers.append(self.session.triggers.add_handler("graphics update", self._graphics_changed))
        from chimerax.atomic import get_triggers
        self._handlers.append(get_triggers().add_handler("changes done", self._atomic_changed))

    def _models_changed(self, name, _):
        if self._closed:
            return
        refresh = name in ("add models", "remove models") and self._discard_retired_models()
        self._update_models()
        self.request_update(force=refresh)

    def _discard_retired_models(self):
        # ChimeraX clears removed model IDs before notifying us and can reuse
        # them before the next capture. Compare the owners of the numeric IDs.
        owners = {model.id: model for model in self.session.models.list(type=Structure)}
        retired = {model_id for model_id, model in self._model_owners.items()
                   if owners.get(model_id) is not model}
        self._model_owners = owners
        if not retired:
            return False
        prefixes = tuple(f"model:{'.'.join(map(str, model_id))}:" for model_id in retired)
        self.styles = {key: style for key, style in self.styles.items() if not key.startswith(prefixes)}
        if retired.intersection(self.settings["model_ids"] or ()):
            self.settings["model_ids"] = None

        # Retire the component rows immediately so they cannot write visibility
        # back under the reused ID while a new capture is pending.
        if self.scene and any(part["model_id"] in retired for part in self.scene["parts"]):
            self.scene = None
            self._populate_parts()
            return True
        return bool(self._captured_scene and any(
            part["model_id"] in retired for part in self._captured_scene["parts"]))

    def _atomic_changed(self, *args):
        if not self._closed:
            self.request_update()

    def _selection_event(self, *args):
        if self.settings["scope"] == "selection" or self.settings["selected_domain"]:
            self.request_update()

    def _graphics_changed(self, *args):
        if self._closed or not self.settings["live"]:
            return
        camera = camera_snapshot(self.session)
        key = json.dumps(camera, sort_keys=True, default=lambda x: x.tolist())
        if key != self._last_camera:
            self._last_camera = key
            self.request_update(scene_changed=False, camera_only=True)

    def _preview_drag_started(self):
        self._preview_dragging = True
        self._preview_drag_moved = False

    def _preview_rotate(self, dx, dy, spin):
        if self._closed:
            return
        angle = -0.5 * dx if spin else 0.5 * hypot(dx, dy)
        if not angle:
            return
        view = self.session.main_view
        axis = (0, 0, 1) if spin else (dy, dx, 0)
        # Match native mouse rotation. With no drawings, rotate moves the
        # camera about ChimeraX's rotation center, leaving models untouched.
        view.rotate(view.camera.position.transform_vector(axis), angle)
        self._preview_drag_moved = True
        self._last_camera = json.dumps(camera_snapshot(self.session), sort_keys=True,
                                       default=lambda x: x.tolist())
        self.request_update(force=True, scene_changed=False, camera_only=True)

    def _preview_drag_finished(self):
        self._preview_dragging = False
        if self._closed:
            return
        if self._preview_drag_moved:
            self._preview_drag_moved = False
            self.request_update(force=True, scene_changed=False, camera_only=True)
            self._finish_camera_motion()
            self._timer.start(0)

    def _finish_camera_motion(self):
        self._settle_timer.stop()
        moving, self._camera_motion = self._camera_motion, False
        if self._closed or not moving:
            self._sync_activity()
            return
        # Refine the final view without recapturing atoms or rebuilding rows.
        # A busy worker finishes its current frame, then takes this newest job.
        self._revision += 1
        self._pending = True
        self._enable_export(False)
        if not self._busy:
            self._timer.start(0)
        self._sync_activity()

    def _update_models(self):
        current = self.settings["model_ids"]
        self.source.blockSignals(True)
        self.source.clear()
        self.source.addItem("All visible structures", None)
        chosen = 0
        for model in list_structure_models(self.session):
            self.source.addItem(model["label"], tuple(model["id"]))
            if current and tuple(current[0]) == tuple(model["id"]):
                chosen = self.source.count() - 1
        self.source.setCurrentIndex(chosen)
        self.source.blockSignals(False)
        if current and not chosen:
            self.settings["model_ids"] = None

    def _source_changed(self, index):
        value = self.source.currentData()
        self.settings["model_ids"] = [tuple(value)] if value else None
        self.frame = None
        self.request_update(force=True)

    def _selection_changed(self, value):
        self._change("scope", "selection" if value else "visible")

    def _sync_lighting_controls(self):
        enabled = self.settings.get("shading", True)
        for slider in (self.highlight_size, self.shadow_size):
            slider.setEnabled(enabled)
            slider._value_label.setEnabled(enabled)

    def _change(self, key, value, geometry=True):
        previous = self.settings.get(key)
        if key == "size_mode":
            from .state import sizing_mode
            self.settings = sizing_mode(self.settings, value)
        else:
            self.settings[key] = value
        if key == "shading":
            self._sync_lighting_controls()
        if key == "outline":
            self.dashed_boundaries.setEnabled(bool(value))
        sizing = {"size_mode", "print_width_mm", "print_height_mm", "dpi", "width", "height",
                  "stroke_width_pt"}
        if key in sizing:
            try:
                self.settings = settings_with(self.settings)
            except ValueError as error:
                self.settings[key] = previous
                self._sync_controls()
                self._report_error(error)
                return
            self._sync_controls()
        if key in (
                *VISIBILITY_KEYS, "opacity", "stroke_width",
                "outline", "margin_fraction", *sizing):
            geometry = True
        if key in ("fixed_frame", "match_projection", "scope", "selected_domain"):
            self.frame = None
        if key == "match_projection":
            self._sync_controls()
        if key == "live" and not value:
            return
        if geometry:
            if key in VISIBILITY_KEYS:
                self._populate_parts()
            self.request_update(force=True, scene_changed=key in ("scope", "selected_domain", "live"))
        else:
            if key in VISIBILITY_KEYS:
                self._populate_parts()
            self._render()

    def request_update(self, force=False, scene_changed=True, camera_only=False):
        if self._closed or (not force and not self.settings["live"]):
            return
        if not camera_only:
            self._camera_motion = False
            self._settle_timer.stop()
        self._revision += 1
        if not camera_only:
            self._content_revision += 1
        self._scene_dirty = self._scene_dirty or scene_changed
        self._pending = True
        self._enable_export(False)
        if camera_only:
            self._camera_motion = True
            self._settle_timer.start(180)
            # Render the newest camera as soon as the single worker is free.
            # Repeated movements coalesce instead of queuing old orientations.
            if not self._busy and not self._timer.isActive():
                self._timer.start(0)
        else:
            self._timer.start(60 if force and not scene_changed else 160)
        self._sync_activity()

    def _start_compute(self):
        if self._closed or self._busy:
            self._sync_activity()
            return
        revision = self._revision
        content_revision = self._content_revision
        try:
            if self._scene_dirty or self._captured_scene is None:
                self._captured_scene = capture_scene(self.session, scope=self.settings["scope"],
                    model_ids=self.settings["model_ids"], selected_domain=self.settings["selected_domain"])
                self.styles = inherit_region_styles(self._captured_scene["parts"], self.styles)
                self._scene_dirty = False
            scene = self._captured_scene
            # Refresh only the small camera record for orbit/zoom changes.
            # Never mutate the camera used by an in-flight worker.
            scene = dict(scene, camera=camera_snapshot(self.session))
            self._captured_scene = scene
            # Camera frames share an immutable appearance snapshot. Validation,
            # and copying per-component visibility only need
            # to run when content or paint changes, not for each mouse event.
            snapshot_key = (content_revision, self._paint_revision, id(scene["parts"]))
            if snapshot_key != self._compute_snapshot_key:
                self._compute_settings = settings_with(self.settings)
                self._compute_styles = deepcopy(self.styles)
                self._compute_paint = effective_styles(scene["parts"], self._compute_styles,
                                                        self._compute_settings)
                self._compute_snapshot_key = snapshot_key
            settings = dict(self._compute_settings)
            settings["_render_quality"] = "preview"
            self._active_interactive = self._camera_motion
            if self._active_interactive:
                settings["publication_resolution"] = self._interaction_resolution
                settings["_interactive_preview"] = True
            if settings["match_projection"]:
                aspect = scene["camera"].get("aspect", 1)
                settings["frame"] = [-1, -1/aspect, 1, 1/aspect]
            elif settings["fixed_frame"] and self.frame is not None:
                settings["frame"] = self.frame
            if not scene["parts"]:
                self.scene = scene
                self.geometry = None
                self._pending = False
                self.preview.message = ("Select atoms or residues in ChimeraX." if settings["scope"] == "selection"
                                        else "Open or show a structure in ChimeraX.")
                self.preview.set_svg(None, settings["width"] / settings["height"])
                self._set_status()
                self._populate_parts()
                self._sync_activity()
                return
        except Exception as error:
            self._report_error(error)
            return
        self._pending = False
        self._busy = True
        self._sync_activity()
        try:
            self._worker.submit(revision=revision, content_revision=content_revision,
                                scene=scene, settings=settings, styles=self._compute_styles,
                                paint=self._compute_paint, paint_revision=self._paint_revision,
                                engine=self._engine, compute_scene=compute_scene, build_svg=build_svg)
        except Exception as error:
            self._busy = False
            self._report_error(error)

    def _finished(self, result):
        if self._closed:
            return
        self._busy = False
        if self._active_interactive:
            levels = (192, 256, 320)
            index = levels.index(self._interaction_resolution)
            render_time = self._worker.compute_seconds
            if render_time > .08:
                self._interaction_resolution = levels[max(0, index-1)]
            elif render_time < .025:
                self._interaction_resolution = levels[min(len(levels)-1, index+1)]
        revision, content_revision, scene, geometry, error = result
        outdated = revision != self._revision
        # Display completed camera frames from either view while immediately
        # computing the newest camera. Discard outdated content/settings, and
        # keep exports blocked until the final camera frame is ready.
        if outdated and (error is not None or content_revision != self._content_revision):
            self._timer.start(0)
            self._sync_activity()
            return
        if error:
            self._report_error(error)
            return
        # Geometry settings reuse the captured parts. Their completion must
        # not reset the component list, its selection, or accessibility rows.
        parts_changed = self.scene is None or self.scene["parts"] is not scene["parts"]
        self.scene, self.geometry = scene, geometry
        self._completed_revision = revision
        if self.settings["fixed_frame"] and self.frame is None:
            self.frame = list(geometry["bounds"])
        if parts_changed:
            self._populate_parts()
        if outdated:
            self._timer.start(0)
        self._sync_activity()
        self._render(geometry.pop("_preview_svg", None), invalidate=False)

    def _export_settings(self):
        settings = settings_with(self.settings)
        if settings["match_projection"] and self.scene:
            aspect = self.scene["camera"].get("aspect", 1)
            settings["frame"] = [-1, -1/aspect, 1, 1/aspect]
        elif settings["fixed_frame"] and self.frame is not None:
            settings["frame"] = self.frame
        return settings

    def _render(self, prepared=None, invalidate=True):
        if invalidate:
            # Paint-only edits do not require geometry, but a worker already
            # computing a camera frame must not replace the new colors later.
            self._paint_revision += 1
        if not self.geometry or not self.scene:
            return
        try:
            if prepared is not None and prepared[0] == self._paint_revision:
                _, svg, ratio, any_visible = prepared
            else:
                settings = self._export_settings()
                styles = effective_styles(self.scene["parts"], self.styles, settings)
                settings["_render_quality"] = "preview"
                svg = build_svg(self.geometry, settings, styles)
                ratio = settings["width"] / settings["height"]
                any_visible = any(style["visible"] for style in styles.values())
            self.preview.set_svg(svg, ratio)
        except Exception as error:
            self._report_error(error)
            return
        ready = self._export_ready()
        self._enable_export(ready)
        if ready:
            warnings = self.scene.get("warnings", [])
            text = warnings[0] if warnings else ""
            if not any_visible:
                text = "No components shown."
            self._set_status(text, "\n".join(warnings))

    def _set_status(self, text="", detail=""):
        self.status.setText(text)
        self.status.setToolTip(detail)
        self.status.setVisible(bool(text))

    def _report_error(self, error):
        self._pending = False
        self._camera_motion = False
        self._timer.stop()
        self._settle_timer.stop()
        self._sync_activity()
        self._enable_export(False)
        self._set_status(str(error))
        self.session.logger.warning(f"Illustruct: {error}")

    def _sync_activity(self):
        self.activity.set_busy(not self._closed and (
            self._busy or self._pending or self._camera_motion
            or self._timer.isActive() or self._settle_timer.isActive()))

    def _enable_export(self, enabled):
        self.svg_button.setEnabled(enabled)
        self.png_button.setEnabled(enabled)
        self.pdf_button.setEnabled(enabled)

    def _populate_parts(self, *args):
        parts = {p["id"]: p for p in self.scene["parts"]} if self.scene else {}
        # List checkboxes retain individual visibility even while a group
        # is hidden, while swatches show the current ChimeraX colors.
        styles = effective_styles(list(parts.values()), self.styles,
                                  dict(self.settings, **dict.fromkeys(VISIBILITY_KEYS, True)))
        category = self.part_filter.currentData()
        listed = sorted((part_id for part_id, part in parts.items()
                         if category in (None, part_category(part))),
                        key=lambda part_id: (styles[part_id]["label"].casefold(), part_id))
        items = [self.parts_list.item(row) for row in range(self.parts_list.count())]
        same_rows = [item.data(Qt.UserRole) for item in items] == listed
        self._populating = True
        try:
            if not same_rows:
                selected = self.parts_list.currentItem()
                selected_id = selected.data(Qt.UserRole) if selected else None
                scroll = self.parts_list.verticalScrollBar().value()
                self.parts_list.clear()
                items = []
                for part_id in listed:
                    item = _PartItem(styles[part_id]["label"])
                    item.setData(Qt.UserRole, part_id)
                    item.setFlags((item.flags() | Qt.ItemIsUserCheckable) & ~Qt.ItemIsEditable)
                    items.append(item)
            for item, part_id in zip(items, listed):
                part, style = parts[part_id], styles[part_id]
                if item.text() != style["label"]:
                    item.setText(style["label"])
                checked = Qt.Checked if style.get("visible", True) else Qt.Unchecked
                if item.checkState() != checked:
                    item.setCheckState(checked)
                group_visible = component_visible(part, self.settings)
                foreground = self.parts_list.palette().brush(
                    QPalette.Active if group_visible else QPalette.Disabled, QPalette.Text)
                if item.foreground() != foreground:
                    item.setForeground(foreground)
                tooltip = part.get("description", "")
                if not group_visible:
                    hidden_by = (part_group(part).title() if not self.settings["show_" + part_group(part)]
                                 else dict(COMPONENT_TYPES)[part_category(part)])
                    tooltip += "\nHidden by " + hidden_by + "; its individual checkbox is remembered."
                if item.toolTip() != tooltip:
                    item.setToolTip(tooltip)
                # A fresh identical QIcon still emits model dataChanged. Keep
                # the colour outside Qt's model so unchanged rows emit nothing.
                if getattr(item, "_swatch_color", None) != style["color"]:
                    swatch = QPixmap(14, 14)
                    swatch.fill(QColor(style["color"]))
                    item.setIcon(QIcon(swatch))
                    item._swatch_color = style["color"]
                if not same_rows:
                    self.parts_list.addItem(item)
                    if part_id == selected_id:
                        self.parts_list.setCurrentItem(item)
            if not same_rows:
                self.parts_list.doItemsLayout()
                self.parts_list.verticalScrollBar().setValue(scroll)
        finally:
            self._populating = False
        self._update_component_controls()

    def _update_component_controls(self):
        parts = self.scene["parts"] if self.scene else []
        styles = effective_styles(parts, self.styles, self.settings)
        for category, label in COMPONENT_TYPES:
            members = [part for part in parts if part_category(part) == category]
            visible = sum(bool(styles[part["id"]]["visible"]) for part in members)
            checkbox = self.type_checks[category]
            checkbox.blockSignals(True)
            checkbox.setChecked(self.settings["show_" + category])
            checkbox.blockSignals(False)
            checkbox.setText(f"{label} ({visible}/{len(members)})")
            group = "chains" if category in ("proteins", "nucleic") else "other"
            checkbox.setEnabled(bool(members) and self.settings["show_" + group])
            checkbox.setToolTip(f"{visible} of {len(members)} shown. Individual choices are remembered."
                                + (" Enable " + group.title() + " to show this type."
                                   if not self.settings["show_" + group] else ""))
        for button in (self.show_listed_button, self.hide_listed_button):
            button.setEnabled(self.parts_list.count() > 0)

    def _set_listed_visibility(self, visible):
        if not self.scene:
            return
        category = self.part_filter.currentData()
        parts = [part for part in self.scene["parts"]
                 if category is None or part_category(part) == category]
        if visible:
            targets = {part_category(part) for part in parts}
            groups = {part_group(part) for part in parts}
            # Opening a shared master must not reveal a type outside the list.
            # Transfer that parent's mask to any untargeted sibling type.
            for sibling, _ in COMPONENT_TYPES:
                group = "chains" if sibling in ("proteins", "nucleic") else "other"
                if group in groups and not self.settings["show_" + group] and sibling not in targets:
                    self.settings["show_" + sibling] = False
        for part in parts:
            self.styles.setdefault(part["id"], {})["visible"] = visible
            if visible:
                self.settings["show_" + part_group(part)] = True
                self.settings["show_" + part_category(part)] = True
        for checkbox, key in ((self.show_chains, "show_chains"), (self.show_other, "show_other")):
            checkbox.blockSignals(True)
            checkbox.setChecked(self.settings[key])
            checkbox.blockSignals(False)
        self._populate_parts()
        self.request_update(force=True, scene_changed=False)

    def _part_changed(self, item):
        if self._populating:
            return
        style = self.styles.setdefault(item.data(Qt.UserRole), {})
        was_visible = style.get("visible", True)
        style["visible"] = item.checkState() == Qt.Checked
        self._update_component_controls()
        if was_visible != style["visible"]:
            self.request_update(force=True, scene_changed=False)
        else:
            self._render()

    def _export_ready(self):
        return (not self._closed and self.scene is not None and bool(self.geometry)
                and not self._busy and not self._pending and not self._camera_motion
                and self._completed_revision == self._revision)

    def _save(self, extension):
        if self._export_dialog_open or not self._export_ready():
            return
        dialog = QFileDialog(self.tool_window.ui_area, "Export illustration")
        dialog.setAcceptMode(QFileDialog.AcceptSave)
        dialog.setNameFilter(f"{extension.upper()} artwork (*.{extension})")
        dialog.setDefaultSuffix(extension)
        dialog.selectFile(f"illustration.{extension}")
        self._export_dialog_open = True
        try:
            accepted = dialog.exec()
        finally:
            self._export_dialog_open = False
        if not accepted or self._closed:
            return
        # The modal dialog runs an event loop. Models, camera frames, or a
        # restored session can change while it is open, including starting a
        # worker that owns the render engine. Never export from that state.
        if not self._export_ready():
            self._set_status("The illustration changed. Wait for the preview, then export again.")
            return
        path = dialog.selectedFiles()[0]
        try:
            writer = {"svg": export_svg, "png": export_png, "pdf": export_pdf}[extension]
            settings = self._export_settings()
            settings["_render_quality"] = "export"
            styles = effective_styles(self.scene["parts"], self.styles, settings)
            geometry = compute_scene(self.scene, settings, self.styles, self._engine)
            writer(path, geometry, settings, styles,
                   metadata={"application": "Illustruct", "version": VERSION,
                             "camera": self.scene["camera"], "warnings": self.scene.get("warnings", [])})
            self._set_status(f"Saved {Path(path).name}", path)
            self.session.logger.info(f"Saved illustration to {path}")
        except Exception as error:
            self._report_error(error)

    def take_snapshot(self, session, flags):
        from chimerax.atomic import AtomicStructure
        return {"version": 1, "base": super().take_snapshot(session, flags),
                "settings": deepcopy(self.settings), "styles": deepcopy(self.styles),
                "frame": self.frame,
                "splitter_state": bytes(self.splitter.saveState()),
                "models": [(tuple(m.id), m) for m in session.models.list(type=AtomicStructure)]}

    @classmethod
    def restore_snapshot(cls, session, data):
        if not session.ui.is_gui:
            return None
        if data["version"] != 1:
            raise ValueError("Unsupported Illustruct session version.")
        # Session merges keep existing tools alive. Match ToolInstance's
        # singleton restore behavior so launch and commands use this state.
        tool = cls.get_singleton(session)
        id_map = {tuple(old): tuple(model.id) for old, model in data["models"]
                  if model is not None and not model.deleted and model.id is not None}
        saved_settings, saved_styles = remap_model_ids(data["settings"], data["styles"], id_map)
        tool.settings = settings_with(saved_settings)
        tool.styles = saved_styles
        tool.frame = data["frame"]
        tool._sync_controls()
        tool.set_state_from_snapshot(session, data["base"])
        splitter_state = data["splitter_state"]
        if isinstance(splitter_state, (bytes, bytearray)):
            tool.splitter.restoreState(QByteArray(bytes(splitter_state)))
        tool.request_update(force=True)
        return tool

    def _sync_controls(self):
        print_size = self.settings["size_mode"] == "print"
        for widget, key in ((self.nucleic_style, "nucleic_style"),
                            (self.size_mode, "size_mode")):
            widget.blockSignals(True)
            widget.setCurrentIndex(widget.findData(self.settings[key]))
            widget.blockSignals(False)
        for widget, name, maximum in ((self.stroke_width, "Outline width", 10),):
            widget.blockSignals(True)
            widget.setSuffix(" pt" if print_size else " px")
            widget.setMaximum(maximum if print_size else 64)
            widget.setSingleStep(0.05 if print_size else 0.5)
            widget.setDecimals(2 if print_size else 1)
            widget.setAccessibleName(name + (" in points" if print_size else " in pixels"))
            widget.blockSignals(False)
        mapping = ((self.simplification, "abstraction", 100), (self.smoothing, "smoothing", 100),
                   (self.minimum_piece_size, "minimum_piece_size", 100),
                   (self.highlight_size, "highlight_size", 100), (self.shadow_size, "shadow_size", 100),
                   (self.opacity, "opacity", 100), (self.margin, "margin_fraction", 100),
                   (self.stroke_width, "stroke_width_pt" if print_size else "stroke_width", 1),
                   (self.width, "width", 1), (self.height, "height", 1),
                   (self.print_width, "print_width_mm", 1), (self.print_height, "print_height_mm", 1),
                   (self.dpi, "dpi", 1))
        for widget, key, factor in mapping:
            widget.blockSignals(True)
            value = self.settings[key] * factor
            if key == "abstraction":
                value = simplification_value(self.settings[key]) * factor
            elif key == "smoothing":
                value = smoothing_value(self.settings[key]) * factor
            widget.setValue(floor(value + 0.5) if isinstance(widget, (QSlider, QSpinBox)) else value)
            if isinstance(widget, QSlider):
                widget._value_label.setText(f"{widget.value()}%")
            widget.blockSignals(False)
        for widget, key in ((self.outline, "outline"), (self.transparent, "transparent"),
                (self.dashed_boundaries, "dashed_boundaries"),
                (self.tinted_outline, "tinted_outline"),
                (self.fixed, "fixed_frame"), (self.projection, "match_projection"),
                (self.domain, "selected_domain"), (self.live, "live"),
                (self.show_chains, "show_chains"), (self.show_other, "show_other")):
            widget.blockSignals(True)
            widget.setChecked(self.settings[key])
            widget.blockSignals(False)
        self.dashed_boundaries.setEnabled(bool(self.settings["outline"]))
        self.shading.blockSignals(True)
        self.shading.setChecked(self.settings.get("shading", True))
        self.shading.blockSignals(False)
        self._sync_lighting_controls()
        for widget in (self.width, self.height):
            widget.setEnabled(not print_size)
        for widget in (self.print_width, self.print_height):
            widget.setEnabled(print_size)
        self.margin.setEnabled(not self.settings["match_projection"])
        self.margin._value_label.setEnabled(not self.settings["match_projection"])
        self.margin.setToolTip("Match camera uses the exact viewport; margin applies when fitting the illustration.")
        self.smoothing.setToolTip("Smooth silhouettes and DNA/RNA backbones. 100% gives the strongest smoothing.")
        self.tinted_outline.setToolTip("Use a darker shade of each component color.")
        for widget in (self.svg_button, self.pdf_button):
            widget.setToolTip("Export editable vector artwork.")
        self.selection_only.blockSignals(True)
        self.selection_only.setChecked(self.settings["scope"] == "selection")
        self.selection_only.blockSignals(False)
        self._update_models()
        self._update_component_controls()

    def delete(self):
        self._closed = True
        self._timer.stop()
        self._settle_timer.stop()
        self.activity.set_busy(False)
        for handler in self._handlers:
            handler.remove()
        self._handlers.clear()
        self._worker.close()
        super().delete()
