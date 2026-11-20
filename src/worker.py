"""Single-worker preview rendering with queued delivery to the GUI thread."""
from concurrent.futures import ThreadPoolExecutor
from time import perf_counter

from Qt.QtCore import QObject, Signal


class RenderWorker(QObject):
    result = Signal(object)

    def __init__(self, parent):
        super().__init__(parent)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="Illustruct")
        self.compute_seconds = 0
        self._closed = False

    def submit(self, *, revision, content_revision, scene, settings, styles,
               paint, paint_revision, engine, compute_scene, build_svg):
        """Render a detached scene on the preview worker."""
        future = self.executor.submit(self._compute, scene, settings, styles,
                                      paint, paint_revision, engine, compute_scene, build_svg)
        future.add_done_callback(lambda result: self._complete(
            result, revision, content_revision, scene))

    def _compute(self, scene, settings, styles, paint, paint_revision, engine,
                 compute_scene, build_svg):
        started = perf_counter()
        try:
            # Engines cache read-only results. The preview payload lives in a
            # separate outer dictionary; all path/string assembly runs here.
            geometry = dict(compute_scene(scene, settings, styles, engine))
            paint_settings = settings
            if settings["fixed_frame"] and "frame" not in settings:
                paint_settings = dict(settings, frame=list(geometry["bounds"]))
            geometry["_preview_svg"] = (
                paint_revision, build_svg(geometry, paint_settings, paint),
                settings["width"] / settings["height"],
                any(style["visible"] for style in paint.values()))
            return geometry
        finally:
            self.compute_seconds = perf_counter() - started

    def _complete(self, future, revision, content_revision, scene):
        try:
            geometry, error = future.result(), None
        except Exception as exc:
            geometry, error = None, exc
        if not self._closed:
            try:
                self.result.emit((revision, content_revision, scene, geometry, error))
            except RuntimeError:
                pass  # The tool window was deleted while its worker completed.

    def close(self):
        self._closed = True
        self.executor.shutdown(wait=False, cancel_futures=True)
