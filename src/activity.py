"""A small activity indicator that keeps its toolbar space while idle."""

from Qt.QtCore import Qt, QRectF, QTimer
from Qt.QtGui import QPainter, QPalette, QPen
from Qt.QtWidgets import QWidget


class ActivityIndicator(QWidget):
    """Animate only after sustained work; never hide or resize the widget."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(18, 18)
        self.setFocusPolicy(Qt.NoFocus)
        self._busy = False
        self._active = False
        self._angle = 0
        self._delay = QTimer(self)
        self._delay.setSingleShot(True)
        self._delay.setInterval(150)
        self._delay.timeout.connect(self._start)
        self._animation = QTimer(self)
        self._animation.setInterval(80)
        self._animation.timeout.connect(self._advance)

    @property
    def active(self):
        return self._active

    def set_busy(self, busy):
        busy = bool(busy)
        if busy == self._busy:
            return
        self._busy = busy
        if busy:
            self._delay.start()
        else:
            self._delay.stop()
            self._animation.stop()
            self._active = False
            self._angle = 0
            self.setToolTip("")
            self.setAccessibleName("")
            self.update()

    def _start(self):
        if not self._busy:
            return
        self._active = True
        self.setToolTip("Updating illustration")
        self.setAccessibleName("Updating illustration")
        self._animation.start()
        self.update()

    def _advance(self):
        self._angle = (self._angle + 30) % 360
        self.update()

    def paintEvent(self, event):
        if not self._active:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(self.palette().color(QPalette.Highlight))
        pen.setWidthF(1.5)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.drawArc(QRectF(1.75, 1.75, 14.5, 14.5), -self._angle * 16, 250 * 16)
