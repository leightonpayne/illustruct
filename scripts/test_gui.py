"""Check preview rotation, exports, and session restore in ChimeraX."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import traceback
import xml.etree.ElementTree as ET


def verify(session):
    import numpy as np
    from Qt.QtCore import QEvent, QPointF, QTimer, Qt
    from Qt.QtGui import QImage, QMouseEvent
    from Qt.QtWidgets import QApplication
    from chimerax.atomic import AtomicStructure
    from chimerax.core.commands import run

    if not session.ui.is_gui or session.models.list(type=AtomicStructure) or any(
            t.__class__.__name__ == "IllustructTool" for t in session.tools.list()):
        raise RuntimeError("Use a fresh graphical session without structures or Illustruct.")
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--source", action="store_true")
    mode.add_argument("--installed", action="store_true", help="Test the installed bundle (default)")
    parser.add_argument("--exit-after", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=root / "qa/gui")
    args, _ = parser.parse_known_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if args.source:
        name = "chimerax.illustruct"
        for module in tuple(sys.modules):
            if module == name or module.startswith(name + "."):
                del sys.modules[module]
        spec = importlib.util.spec_from_file_location(name, root / "src/__init__.py",
                                                      submodule_search_locations=[str(root / "src")])
        package = importlib.util.module_from_spec(spec)
        sys.modules[name] = package
        spec.loader.exec_module(package)
        from chimerax.core.commands.cli import deregister
        from chimerax.illustruct.commands import register_commands
        try:
            deregister("illustruct")
        except RuntimeError:
            pass
        register_commands(session.logger)
    from chimerax.illustruct import tool as tool_module
    from chimerax.illustruct.tool import IllustructTool

    if not args.source:
        assert not Path(tool_module.__file__).resolve().is_relative_to(root / "src")
    checks, state = [], {"finished": False}
    run(session, f"open {json.dumps(str(root / 'tests/hemoglobin.pdb'))}; color bychain; view; illustruct")
    tool, = (instance for instance in session.tools.list() if isinstance(instance, IllustructTool))
    model = session.models.list(type=AtomicStructure)[0]
    original_coords, original_positions = model.atoms.coords.copy(), model.positions.array().copy()

    def finish(error=None):
        if state["finished"]:
            return
        state["finished"] = True
        report = {"status": "failed" if error else "pass", "checks": checks,
                  "mode": "source" if args.source else "installed", "tool_source": tool_module.__file__}
        if error is not None:
            report.update(error=str(error) or type(error).__name__, traceback=traceback.format_exc())
            session.logger.error(report["error"])
        (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print("ILLUSTRUCT_GUI_" + report["status"].upper(), json.dumps(report), flush=True)
        if args.exit_after:
            os._exit(1 if error else 0)

    def later(callback, delay=25):
        def guarded():
            if not state["finished"]:
                try:
                    callback()
                except Exception as error:
                    finish(error)
        QTimer.singleShot(delay, guarded)

    def ready(callback, deadline=None, color=None):
        deadline = deadline or time.monotonic() + 45
        source_ready = color is None or (tool.scene and tool.scene["parts"]
                                         and {p["color"] for p in tool.scene["parts"]} == {color})
        if tool.svg_button.isEnabled() and source_ready:
            callback()
        elif time.monotonic() >= deadline:
            raise AssertionError("Preview timed out: " + tool.status.text())
        else:
            later(lambda: ready(callback, deadline, color))

    def initial():
        checks.append("The illustruct command opens one ready panel")
        state["camera"] = session.main_view.camera.position.matrix.copy()
        start = tool.preview._image_rect().center()
        end = start + QPointF(35, 18)
        for event_type, point, button, buttons in (
                (QEvent.MouseButtonPress, start, Qt.LeftButton, Qt.LeftButton),
                (QEvent.MouseMove, end, Qt.NoButton, Qt.LeftButton),
                (QEvent.MouseButtonRelease, end, Qt.LeftButton, Qt.NoButton)):
            event = QMouseEvent(event_type, point, QPointF(tool.preview.mapToGlobal(point.toPoint())),
                                button, buttons, Qt.NoModifier)
            QApplication.sendEvent(tool.preview, event)
        ready(rotated)

    def rotated():
        assert not np.allclose(state["camera"], session.main_view.camera.position.matrix)
        np.testing.assert_allclose(tool.scene["camera"]["scene_to_camera"],
                                   session.main_view.camera.position.inverse().matrix)
        np.testing.assert_array_equal(model.atoms.coords, original_coords)
        np.testing.assert_array_equal(model.positions.array(), original_positions)
        checks.append("Preview dragging updates the camera and export readiness without changing atoms")
        tool.settings.update(size_mode="pixels", width=640, height=480,
                             model_ids=[tuple(model.id)])
        run(session, f"color #{model.id_string} #4477aa target ac")
        ready(hide_component, color="#4477aa")

    def hide_component():
        item = tool.parts_list.item(0)
        assert item.checkState() == Qt.Checked
        state["part_id"] = item.data(Qt.UserRole)
        item.setCheckState(Qt.Unchecked)
        ready(export_and_save)

    def export_and_save():
        for extension in ("svg", "png", "pdf"):
            run(session, f"illustruct export {json.dumps(str(output / ('figure.' + extension)))}")
        svg = output / "figure.svg"
        assert ET.parse(svg).getroot().get("width") == "640"
        assert "#4477aa" in svg.read_text(encoding="utf-8")
        image = QImage(str(output / "figure.png"))
        assert not image.isNull() and (image.width(), image.height()) == (640, 480)
        assert (output / "figure.pdf").read_bytes().startswith(b"%PDF-")
        checks.append("The installed command exports SVG, PNG, and PDF using the panel settings")
        path = output / "session.cxs"
        run(session, f"save {json.dumps(str(path))}")
        tool.settings["width"] = 900
        tool.styles.clear()
        run(session, f"color #{model.id_string} #aa7744 target ac")
        run(session, f"open {json.dumps(str(path))}")
        later(restored)

    def restored():
        nonlocal tool
        matches = [t for t in session.tools.list() if isinstance(t, IllustructTool)]
        assert len(matches) == 1, "Session restoration did not produce exactly one panel"
        tool = matches[0]
        ready(check_restored, color="#4477aa")

    def check_restored():
        assert tool.settings["width"] == 640 and tool.settings["height"] == 480
        item = next(tool.parts_list.item(row) for row in range(tool.parts_list.count())
                    if tool.parts_list.item(row).data(Qt.UserRole) == state["part_id"])
        assert item.checkState() == Qt.Unchecked
        assert len(session.models.list(type=AtomicStructure)) == 1
        checks.append("Reopening a session restores the structure, source colors, panel dimensions, and visibility")
        finish()

    later(lambda: ready(initial))


if "session" in globals():
    exit_after = "--exit-after" in sys.argv
    try:
        verify(session)
    except Exception:
        traceback.print_exc()
        if exit_after:
            sys.stderr.flush()
            os._exit(1)
        raise
elif __name__ == "__main__":
    raise SystemExit("Run in ChimeraX: --notools --script 'scripts/test_gui.py --exit-after'")
