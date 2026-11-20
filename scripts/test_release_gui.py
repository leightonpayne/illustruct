"""Check model replacement, session merging, and export races in ChimeraX."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
import time
import traceback
import xml.etree.ElementTree as ET


def verify(session):
    from Qt.QtCore import Qt, QTimer
    from Qt.QtWidgets import QFileDialog
    from chimerax.atomic import AtomicStructure
    from chimerax.core.commands import run

    if (not session.ui.is_gui or session.models.list(type=AtomicStructure)
            or any(t.__class__.__name__ == "IllustructTool" for t in session.tools.list())):
        raise RuntimeError("Use a fresh graphical ChimeraX session without open structures or Illustruct.")
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--source", action="store_true")
    mode.add_argument("--installed", action="store_true", help="Test the installed bundle (default)")
    parser.add_argument("--exit-after", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=root / "qa/release-gui")
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
    from chimerax.illustruct import tool as tool_module
    from chimerax.illustruct.tool import IllustructTool

    if not args.source:
        assert not Path(tool_module.__file__).resolve().is_relative_to(root / "src")
    run(session, f"open {json.dumps(str(root / 'tests/crambin.pdb'))}; color #1 #a12345 target ac; view")
    tool = IllustructTool.get_singleton(session)
    original_compute = tool_module.compute_scene
    original_dialog = tool_module.QFileDialog
    released, started = threading.Event(), threading.Event()
    checks, state = [], {"finished": False}

    def compute(*compute_args, **kwargs):
        if threading.current_thread() is not threading.main_thread() and state.pop("hold_next", False):
            started.set()
            if not released.wait(30):
                raise AssertionError("Test did not release the render worker")
        return original_compute(*compute_args, **kwargs)

    tool_module.compute_scene = compute

    def finish(error=None):
        if state["finished"]:
            return
        state["finished"] = True
        released.set()
        tool_module.compute_scene = original_compute
        tool_module.QFileDialog = original_dialog
        report = {"status": "failed" if error else "pass", "checks": checks,
                  "mode": "source" if args.source else "installed",
                  "tool_source": tool_module.__file__}
        if error is not None:
            report.update(error=str(error) or type(error).__name__, traceback=traceback.format_exc())
        (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print("ILLUSTRUCT_RELEASE_GUI_" + report["status"].upper(), json.dumps(report), flush=True)
        if args.exit_after:
            os._exit(1 if error else 0)
        if error is not None:
            session.logger.error(report["error"])

    def later(callback, delay=25):
        def guarded():
            if state["finished"]:
                return
            try:
                callback()
            except Exception as error:
                finish(error)
        QTimer.singleShot(delay, guarded)

    def wait_for(condition, callback, deadline=None):
        deadline = deadline or time.monotonic() + 30
        if condition():
            callback()
        elif time.monotonic() >= deadline:
            raise AssertionError("Release GUI check timed out: " + tool.status.text())
        else:
            later(lambda: wait_for(condition, callback, deadline))

    def ready(color=None):
        return (tool._export_ready() and tool.svg_button.isEnabled()
                and (color is None or (tool.scene and tool.scene["parts"]
                     and {p["color"] for p in tool.scene["parts"]} == {color})))

    def hold_render():
        started.clear()
        released.clear()
        state["hold_next"] = True
        tool.request_update(force=True, scene_changed=False)
        tool._timer.stop()
        tool._start_compute()
        assert tool._busy

    def replace_model():
        model, = session.models.list(type=AtomicStructure)
        part, = tool.scene["parts"]
        model_id, part_id = model.id, part["id"]
        item = tool.parts_list.item(0)
        assert item.data(Qt.UserRole) == part_id and item.checkState() == Qt.Checked
        item.setCheckState(Qt.Unchecked)
        assert tool.styles[part_id]["visible"] is False
        tool.live.setChecked(False)

        run(session, f"close #{model.id_string}; open {json.dumps(str(root / 'tests/hemoglobin.pdb'))}; "
                     "color #1 #123456 target ac; view")
        replacement, = session.models.list(type=AtomicStructure)
        assert replacement is not model and replacement.id == model_id
        assert part_id not in tool.styles, "Visibility survived the model closing"
        wait_for(lambda: ready("#123456"), lambda: replacement_ready(part_id))

    def replacement_ready(part_id):
        item = next(tool.parts_list.item(row) for row in range(tool.parts_list.count())
                    if tool.parts_list.item(row).data(Qt.UserRole) == part_id)
        assert item.checkState() == Qt.Checked, "The replacement chain inherited hidden visibility"
        assert not tool.live.isChecked()
        checks.append("A replacement structure using a closed model's ID starts with fresh component visibility")
        tool.live.setChecked(True)
        prepare_session()

    def prepare_session():
        model_id = tuple(session.models.list(type=AtomicStructure)[0].id)
        tool.settings["model_ids"] = [model_id]
        tool.settings.update(size_mode="pixels", width=640, height=480)
        state["saved_id"] = model_id
        run(session, "color #" + ".".join(map(str, model_id)) + " #a12345 target ac")
        wait_for(lambda: ready("#a12345"), save_session)

    def save_session():
        part_id = tool.scene["parts"][0]["id"]
        tool.styles[part_id] = {"visible": False}
        session_path = output / "merge.cxs"
        run(session, f"save {json.dumps(str(session_path))}")
        # The existing tool must survive a merge even with an old render in flight.
        tool.settings["width"] = 900
        tool.styles[part_id]["visible"] = True
        run(session, "color #" + ".".join(map(str, state["saved_id"])) + " #123456 target ac")
        hold_render()
        wait_for(started.is_set, lambda: merge_session(session_path))

    def merge_session(session_path):
        run(session, f"open {json.dumps(str(session_path))} combine true")
        matches = [t for t in session.tools.list() if isinstance(t, IllustructTool)]
        assert matches == [tool], "Merging a session created another Illustruct panel"
        assert tool.settings["width"] == 640
        restored_id = tool.settings["model_ids"][0]
        assert restored_id != state["saved_id"]
        released.set()
        wait_for(lambda: ready("#a12345"), lambda: merged_ready(restored_id))

    def merged_ready(restored_id):
        assert {part["model_id"] for part in tool.scene["parts"]} == {restored_id}
        assert any(tool.parts_list.item(row).checkState() == Qt.Unchecked
                   for row in range(tool.parts_list.count()))
        checks.append("Merging reuses the panel and restores the selected model and component visibility")
        test_busy_dialog()

    class TestDialog(QFileDialog):
        def __init__(self, *dialog_args, **kwargs):
            super().__init__(*dialog_args, **kwargs)
            self.setOption(QFileDialog.Option.DontUseNativeDialog, True)

        def exec(self):
            self.selectFile(str(state["dialog_path"]))
            timeout = QTimer(self)
            timeout.setSingleShot(True)
            timeout.timeout.connect(lambda: finish(AssertionError("Save dialog did not close")))
            timeout.start(15000)
            later(lambda: state["dialog_callback"](self), delay=0)
            try:
                return super().exec()
            finally:
                try:
                    timeout.stop()
                except RuntimeError:
                    pass  # The tool can close while the dialog is open.

    def test_busy_dialog():
        path = output / "changed-during-dialog.svg"
        path.write_text("existing artwork", encoding="utf-8")
        state.update(dialog_path=path, dialog_callback=invalidate_dialog)
        tool_module.QFileDialog = TestDialog
        tool._save("svg")
        if state["finished"]:
            return
        assert path.read_text(encoding="utf-8") == "existing artwork"
        checks.append("A view change during the save dialog preserves the existing artwork")
        released.set()
        wait_for(ready, test_ready_dialog)

    def invalidate_dialog(dialog):
        run(session, "turn y 15")
        hold_render()
        dialog.setOption(QFileDialog.Option.DontConfirmOverwrite, True)
        wait_for(started.is_set, dialog.accept)

    def test_ready_dialog():
        path = output / "ready.svg"
        state.update(dialog_path=path, dialog_callback=accept_dialog)
        tool._save("svg")
        if state["finished"]:
            return
        assert ET.parse(path).getroot().get("width") == "640"
        assert "#a12345" in path.read_text(encoding="utf-8")
        checks.append("Retrying after the preview settles exports the restored illustration")
        test_closed_dialog()

    def test_closed_dialog():
        path = output / "closed-during-dialog.svg"
        path.write_text("existing artwork", encoding="utf-8")
        state.update(dialog_path=path, dialog_callback=close_during_render)
        tool._save("svg")
        if state["finished"]:
            return
        assert path.read_text(encoding="utf-8") == "existing artwork"
        released.set()
        wait_for(lambda: all(not thread.is_alive() for thread in tool._worker.executor._threads), closed_ready)

    def close_during_render(_):
        hold_render()
        wait_for(started.is_set, tool.delete)

    def closed_ready():
        assert not any(isinstance(t, IllustructTool) for t in session.tools.list())
        checks.append("Closing the panel during a save releases the render worker and preserves artwork")
        finish()

    def accept_dialog(dialog):
        dialog.setOption(QFileDialog.Option.DontConfirmOverwrite, True)
        dialog.accept()

    later(lambda: wait_for(lambda: ready("#a12345"), replace_model))


if "session" in globals():
    try:
        verify(session)
    except Exception:
        traceback.print_exc()
        if "--exit-after" in sys.argv:
            sys.stderr.flush()
            os._exit(1)
        raise
elif __name__ == "__main__":
    raise SystemExit("Run in a fresh ChimeraX GUI with --notools --script 'scripts/test_release_gui.py --exit-after'")
