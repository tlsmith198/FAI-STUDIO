from __future__ import annotations

import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from ..db import ConflictError, LockedError

ORANGE = "#FF6A00"
CHARCOAL = "#080B0B"
ACCEPT_BG = QColor("#E3F4E8")
ACCEPT_FG = QColor("#146C2E")
REJECT_BG = QColor("#FDE2E1")
REJECT_FG = QColor("#B42318")
NA_FG = QColor("#6B6F72")
REMOTE_FLASH = QColor("#FFF4D6")

STATUS_COLORS = {"Draft": "#6B6F72", "In Review": "#B26B00", "Approved": "#146C2E", "Rejected": "#B42318"}


class LiveBus(QObject):
    """Bridge from the LISTEN thread into the Qt main thread."""
    event = Signal(dict)
    status = Signal(bool, str)


def app_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(CHARCOAL))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(2, 2, 60, 60, 12, 12)
    p.setBrush(QColor("white"))
    p.setPen(QColor(ORANGE))
    pen = p.pen()
    pen.setWidth(5)
    p.setPen(pen)
    p.drawEllipse(14, 14, 36, 36)
    p.setPen(QColor(ORANGE))
    f = QFont("Segoe UI", 18, QFont.Bold)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignCenter, "FA")
    p.end()
    return QIcon(pm)


def guard(parent: QWidget | None, fn: Callable[[], Any], on_conflict: Callable[[ConflictError], None] | None = None,
          quiet: bool = False) -> Any:
    """Run a DB action and turn errors into friendly dialogs."""
    try:
        QApplication.setOverrideCursor(Qt.WaitCursor)
        return fn()
    except ConflictError as e:
        QApplication.restoreOverrideCursor()
        if on_conflict:
            on_conflict(e)
        if not quiet:
            QMessageBox.warning(parent, "Changed by another user", str(e) + "\n\nThe screen has been refreshed "
                                "with the latest data. Re-enter your change if it is still needed.")
    except LockedError as e:
        QApplication.restoreOverrideCursor()
        QMessageBox.information(parent, "Report locked", str(e))
    except PermissionError as e:
        QApplication.restoreOverrideCursor()
        QMessageBox.information(parent, "Needs the server", str(e))
    except Exception as e:  # noqa: BLE001
        QApplication.restoreOverrideCursor()
        traceback.print_exc()
        QMessageBox.critical(parent, "Error", f"{e.__class__.__name__}: {e}")
    finally:
        while QApplication.overrideCursor() is not None:
            QApplication.restoreOverrideCursor()
    return None


STYLE = f"""
QWidget {{ color: #111417; }}
QMainWindow, QDialog {{ background: #F7F7F8; }}
QLabel, QCheckBox, QRadioButton, QGroupBox {{ color: #111417; }}
QComboBox QAbstractItemView {{ background: white; color: #111417; selection-background-color: #FFE3CC;
                               selection-color: black; }}
QMenu {{ background: white; color: #111417; }}
QMenu::item:selected {{ background: #FFE3CC; }}
QHeaderView::section {{ color: white; }}
QToolBar {{ background: {CHARCOAL}; border: none; spacing: 4px; padding: 3px; }}
QToolBar QToolButton {{ color: white; padding: 5px 9px; border-radius: 4px; }}
QToolBar QToolButton:hover {{ background: #2a2f31; }}
QToolBar QToolButton:disabled {{ color: #5f6467; }}
QToolBar QLabel {{ color: white; }}
QTabWidget::pane {{ border: 1px solid #D0D3D6; background: white; }}
QTabBar::tab {{ padding: 6px 14px; background: #ECEDEE; border: 1px solid #D0D3D6; border-bottom: none;
               margin-right: 2px; border-top-left-radius: 4px; border-top-right-radius: 4px; }}
QTabBar::tab:selected {{ background: white; border-top: 3px solid {ORANGE}; }}
QPushButton {{ padding: 5px 11px; border: 1px solid #B9BDC1; border-radius: 4px; background: white; color: #111417; }}
QPushButton:hover {{ border-color: {ORANGE}; }}
QPushButton:disabled {{ color: #9A9EA2; background: #F0F0F0; }}
QPushButton#primary {{ background: {ORANGE}; color: white; border-color: {ORANGE}; font-weight: bold; }}
QPushButton#primary:hover {{ background: #E65F00; }}
QGroupBox {{ font-weight: bold; border: 1px solid #D0D3D6; border-radius: 5px; margin-top: 10px; padding-top: 6px;
            background: white; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 4px; color: {CHARCOAL}; }}
QTableView {{ gridline-color: #DADDE0; selection-background-color: #FFE3CC; selection-color: black;
              alternate-background-color: #FAFAFB; }}
QHeaderView::section {{ background: #2A2F31; color: white; padding: 4px; border: none;
                        border-right: 1px solid #444; font-weight: bold; }}
QLineEdit, QComboBox, QPlainTextEdit, QSpinBox, QDoubleSpinBox {{ padding: 3px; border: 1px solid #C4C8CC;
                                                                  border-radius: 3px; background: white; }}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus {{ border: 1px solid {ORANGE}; }}
QLineEdit[readOnly="true"] {{ background: #F2F3F4; }}
QStatusBar {{ background: #ECEDEE; }}
"""


# ---------------------------------------------------------------------------- screen scaling
def available_geometry(w: QWidget | None = None):
    """Usable area of the screen the widget (or its parent / the app) is on - excludes the taskbar."""
    from PySide6.QtGui import QGuiApplication
    scr = None
    for x in (w, w.parentWidget() if w is not None else None, QApplication.activeWindow()):
        if x is not None and x.screen() is not None:
            scr = x.screen()
            break
    scr = scr or QGuiApplication.primaryScreen()
    return scr.availableGeometry()


def fit_to_screen(w: QWidget, width: int, height: int, frac: float = 0.92) -> None:
    """Resize to the preferred size, but never larger than `frac` of the usable screen (laptops, 1366x768,
    scaled 4K displays ...), and keep it on-screen."""
    g = available_geometry(w)
    ww, hh = min(width, int(g.width() * frac)), min(height, int(g.height() * frac))
    w.resize(ww, hh)
    w.setMaximumSize(16777215, 16777215)


def is_small_screen(w: QWidget | None = None) -> bool:
    g = available_geometry(w)
    return g.width() < 1500 or g.height() < 860


def multiline_delegate(parent):
    """Item delegate with a multi-line editor (Enter = new line) for QTableWidget address / note cells."""
    from PySide6.QtWidgets import QPlainTextEdit, QStyledItemDelegate

    class _D(QStyledItemDelegate):
        def createEditor(self, p, option, index):
            ed = QPlainTextEdit(p)
            ed.setTabChangesFocus(True)
            return ed

        def setEditorData(self, ed, index):
            ed.setPlainText(str(index.data(Qt.EditRole) or ""))

        def setModelData(self, ed, model, index):
            model.setData(index, ed.toPlainText().strip(), Qt.EditRole)

        def updateEditorGeometry(self, ed, option, index):
            r = option.rect
            ed.setGeometry(r.x(), r.y(), max(r.width(), 240), max(r.height(), 90))
    return _D(parent)


# ---------------------------------------------------------------------------- responsive layouts
from PySide6.QtCore import QPoint, QRect, QSize  # noqa: E402
from PySide6.QtWidgets import QGridLayout, QLabel, QLayout, QSizePolicy, QVBoxLayout  # noqa: E402


class FlowLayout(QLayout):
    """Lays widgets out left-to-right and wraps onto the next line when the window is narrow
    (toolbars / button rows that must not push the window wider than a laptop screen)."""

    def __init__(self, parent=None, spacing=6):
        super().__init__(parent)
        self._items = []
        self._sp = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return self._do(QRect(0, 0, w, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        s = QSize()
        for it in self._items:
            s = s.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return s + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do(self, rect, test):
        m = self.contentsMargins()
        r = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_h = r.x(), r.y(), 0
        for it in self._items:
            if it.widget() is not None and not it.widget().isVisibleTo(it.widget().parentWidget()):
                continue
            hint = it.sizeHint()
            nx = x + hint.width() + self._sp
            if nx - self._sp > r.right() + 1 and line_h > 0:
                x, y = r.x(), y + line_h + self._sp
                nx = x + hint.width() + self._sp
                line_h = 0
            if not test:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x, line_h = nx, max(line_h, hint.height())
        return y + line_h - rect.y() + m.bottom()


class FlowRow(QWidget):
    """A wrapping row of buttons."""

    def __init__(self, parent=None, spacing=6):
        super().__init__(parent)
        self.flow = FlowLayout(self, spacing)
        sp = QSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)

    def addWidget(self, w):
        self.flow.addWidget(w)

    def addStretch(self, *_):          # API-compatible with QHBoxLayout call sites
        pass


class ResponsiveGrid(QWidget):
    """Places widgets in as many columns as fit (1 .. max_cols) and re-flows when the window is resized."""

    def __init__(self, min_col_width=380, max_cols=3, parent=None, stretches=None):
        super().__init__(parent)
        self.min_col_width, self.max_cols = min_col_width, max_cols
        self.stretches = list(stretches or [1] * max_cols)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self._widgets: list[tuple[QWidget, int]] = []
        self._cols = 0

    def add(self, w: QWidget, span=1):
        self._widgets.append((w, span))
        self._cols = 0
        self._relayout(self.width() or 1200)

    def columns_for(self, width):
        return max(1, min(self.max_cols, width // max(1, self.min_col_width)))

    def _relayout(self, width):
        n = self.columns_for(width)
        if n == self._cols:
            return
        self._cols = n
        for w, _ in self._widgets:
            self.grid.removeWidget(w)
        for c in range(self.max_cols):
            self.grid.setColumnStretch(c, (self.stretches[c] if n > 1 else 1) if c < n else 0)
        r = c = 0
        for w, span in self._widgets:
            span = min(span, n)
            if c + span > n:
                r, c = r + 1, 0
            self.grid.addWidget(w, r, c, 1, span)
            c += span
            if c >= n:
                r, c = r + 1, 0

    def resizeEvent(self, e):
        self._relayout(e.size().width())
        super().resizeEvent(e)


class ResponsiveForm(QWidget):
    """Label / field pairs laid out in 1..4 column pairs depending on the width available."""

    def __init__(self, host: QWidget | None = None, min_col_width=330, max_cols=4):
        super().__init__(host)
        self.min_col_width, self.max_cols = min_col_width, max_cols
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(4, 4, 4, 4)
        self.grid.setHorizontalSpacing(8)
        self._pairs: list[tuple[QLabel, QWidget]] = []
        self._cols = 0
        if host is not None:
            lay = host.layout() or QVBoxLayout(host)
            lay.addWidget(self)

    def add_row(self, label: str, w: QWidget):
        lbl = QLabel(label)
        lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._pairs.append((lbl, w))
        self._cols = 0
        self._relayout(self.width() or 1200)

    def _relayout(self, width):
        n = max(1, min(self.max_cols, width // max(1, self.min_col_width)))
        if n == self._cols:
            return
        self._cols = n
        for lbl, w in self._pairs:
            self.grid.removeWidget(lbl)
            self.grid.removeWidget(w)
        for c in range(self.max_cols * 2):
            self.grid.setColumnStretch(c, (1 if c % 2 else 0) if c < n * 2 else 0)
        rows = (len(self._pairs) + n - 1) // n
        for i, (lbl, w) in enumerate(self._pairs):
            col, row = divmod(i, rows)                     # fill column by column, like a paper form
            self.grid.addWidget(lbl, row, col * 2)
            self.grid.addWidget(w, row, col * 2 + 1)

    def resizeEvent(self, e):
        self._relayout(e.size().width())
        super().resizeEvent(e)
