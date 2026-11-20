"""Native SVG preview and pointer gestures; imported only by the GUI tool."""
from Qt.QtCore import Qt, Signal, QRectF, QByteArray, QEvent
from Qt.QtGui import QColor, QPainter, QPixmap
from Qt.QtWidgets import QWidget

from .export import svg_renderer_class


class Preview(QWidget):
    drag_started = Signal()
    rotated = Signal(float, float, bool)
    drag_finished = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.renderer = svg_renderer_class()(self)
        self.message = "Open a structure in ChimeraX to begin."
        self.ratio = 4 / 3
        self._drag_position = None
        self._checkerboard = QPixmap(24, 24)
        self._checkerboard.fill(QColor("#ffffff"))
        painter = QPainter(self._checkerboard)
        painter.fillRect(12, 0, 12, 12, QColor("#f0f1f2"))
        painter.fillRect(0, 12, 12, 12, QColor("#f0f1f2"))
        painter.end()
        self.setMinimumSize(260, 140)
        self.setAccessibleName("Illustration preview")
        self.setToolTip("Drag to rotate. Shift-drag to spin.")

    def set_svg(self, svg, ratio):
        self.ratio = ratio
        if svg:
            self.renderer.load(QByteArray(svg.encode("utf-8")))
        else:
            self.renderer.load(QByteArray())
        if not self.renderer.isValid():
            self._end_drag()
        self.setCursor(Qt.ClosedHandCursor if self._drag_position is not None else
                       Qt.OpenHandCursor if self.renderer.isValid() else Qt.ArrowCursor)
        self.update()

    def _image_rect(self):
        width = max(0, min(self.width() - 16, (self.height() - 16) * self.ratio))
        height = width / self.ratio
        return QRectF((self.width() - width) / 2, (self.height() - height) / 2,
                      width, height)

    def mousePressEvent(self, event):
        if (event.button() == Qt.LeftButton and self.renderer.isValid()
                and self._image_rect().contains(event.position())):
            self._drag_position = event.position()
            self.setCursor(Qt.ClosedHandCursor)
            self.drag_started.emit()
            event.accept()
        else:
            super().mousePressEvent(event)

    def _drag_to(self, event):
        position = event.position()
        delta = position - self._drag_position
        self._drag_position = position
        if delta.x() or delta.y():
            self.rotated.emit(delta.x(), delta.y(), bool(event.modifiers() & Qt.ShiftModifier))

    def mouseMoveEvent(self, event):
        if self._drag_position is None:
            super().mouseMoveEvent(event)
            return
        if event.buttons() & Qt.LeftButton:
            self._drag_to(event)
        else:
            self._end_drag()
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._drag_position is not None:
            self._drag_to(event)
            self._end_drag()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def _end_drag(self):
        if self._drag_position is not None:
            self._drag_position = None
            self.setCursor(Qt.OpenHandCursor if self.renderer.isValid() else Qt.ArrowCursor)
            self.drag_finished.emit()

    def hideEvent(self, event):
        self._end_drag()
        super().hideEvent(event)

    def event(self, event):
        if event.type() in (QEvent.UngrabMouse, QEvent.WindowDeactivate):
            self._end_drag()
        return super().event(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().window())
        rect = self._image_rect()
        painter.setClipRect(rect)
        # A quiet checkerboard makes transparent output unambiguous.
        painter.drawTiledPixmap(rect, self._checkerboard)
        if self.renderer.isValid():
            self.renderer.render(painter, rect)
        else:
            painter.setPen(QColor("#59636a"))
            painter.drawText(rect.adjusted(18, 18, -18, -18),
                             Qt.AlignCenter | Qt.TextWordWrap, self.message)
        painter.end()
