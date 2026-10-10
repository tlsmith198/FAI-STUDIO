"""Drawing / image viewer with interactive balloons."""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QGraphicsEllipseItem, QGraphicsItem, QGraphicsPixmapItem,
                               QGraphicsRectItem, QRubberBand,
                               QGraphicsScene, QGraphicsSimpleTextItem, QGraphicsView, QHBoxLayout, QLabel, QMenu,
                               QSpinBox, QToolButton, QVBoxLayout, QWidget)

from ..docrender import is_pdf, render_page

BALLOON_COLORS = {"Accept": "#146C2E", "Reject": "#B42318", "N/A": "#6B6F72", "": "#FF6A00"}


class BalloonItem(QGraphicsEllipseItem):
    def __init__(self, viewer: "DrawingViewer", char: dict, r: float):
        super().__init__(-r, -r, 2 * r, 2 * r)
        self.viewer = viewer
        self.char_id = char["id"]
        self.r = r
        self.setZValue(10)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)
        self.setCursor(Qt.PointingHandCursor)
        self.text = QGraphicsSimpleTextItem(self)
        self.text.setZValue(11)
        self.setAcceptHoverEvents(True)
        self._press_pos = None
        self.update_char(char)

    def update_char(self, char: dict):
        self.char = char
        color = QColor(BALLOON_COLORS.get(char.get("disposition") or "", "#FF6A00"))
        self.setPen(QPen(color, max(2.0, self.r * 0.16)))
        self.setBrush(QBrush(QColor(255, 255, 255, 235)))
        txt = str(char.get("char_no", ""))
        f = QFont("Arial")
        f.setBold(True)
        f.setPixelSize(int(self.r * (1.1 if len(txt) <= 2 else 0.82)))
        self.text.setFont(f)
        self.text.setBrush(color)
        self.text.setText(txt)
        br = self.text.boundingRect()
        self.text.setPos(-br.width() / 2, -br.height() / 2)
        req = char.get("requirement") or ""
        self.setToolTip(f"#{txt}  {req}\nResult: {char.get('results') or '—'}  {char.get('disposition') or ''}")

    def set_highlight(self, on: bool):
        color = QColor(BALLOON_COLORS.get(self.char.get("disposition") or "", "#FF6A00"))
        if on:
            self.setBrush(QBrush(QColor("#FFE3CC")))
            self.setPen(QPen(QColor("#0B5CAD"), max(3.0, self.r * 0.26)))
        else:
            self.setBrush(QBrush(QColor(255, 255, 255, 235)))
            self.setPen(QPen(color, max(2.0, self.r * 0.16)))

    def mousePressEvent(self, e):
        self._press_pos = self.pos()
        self.viewer.balloonClicked.emit(self.char_id)
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e):
        super().mouseReleaseEvent(e)
        if self._press_pos is not None and (self.pos() - self._press_pos).manhattanLength() > 1:
            self.viewer._balloon_moved(self)
        self._press_pos = None

    def contextMenuEvent(self, e):
        self.viewer.balloonContext.emit(self.char_id, e.screenPos())


class _View(QGraphicsView):
    clicked = Signal(QPointF)

    def __init__(self, scene, owner):
        super().__init__(scene)
        self.owner = owner
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setBackgroundBrush(QColor("#5A5F63"))

    def wheelEvent(self, e):
        f = 1.2 if e.angleDelta().y() > 0 else 1 / 1.2
        self.owner.zoom_by(f)

    areaSelected = Signal(QRectF)

    def mousePressEvent(self, e):
        item = self.itemAt(e.position().toPoint())
        if self.owner.mode == "area" and e.button() == Qt.LeftButton:
            from PySide6.QtCore import QRect, QSize
            self._origin = e.position().toPoint()
            if not hasattr(self, "_band"):
                self._band = QRubberBand(QRubberBand.Rectangle, self.viewport())
            self._band.setGeometry(QRect(self._origin, QSize()))
            self._band.show()
            return
        if self.owner.mode == "add" and e.button() == Qt.LeftButton and not isinstance(item, (BalloonItem, QGraphicsSimpleTextItem)):
            self.clicked.emit(self.mapToScene(e.position().toPoint()))
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self.owner.mode == "area" and getattr(self, "_band", None) is not None and self._band.isVisible():
            from PySide6.QtCore import QRect
            self._band.setGeometry(QRect(self._origin, e.position().toPoint()).normalized())
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self.owner.mode == "area" and getattr(self, "_band", None) is not None and self._band.isVisible():
            g = self._band.geometry()
            self._band.hide()
            if g.width() > 8 and g.height() > 8:
                r = QRectF(self.mapToScene(g.topLeft()), self.mapToScene(g.bottomRight()))
                self.areaSelected.emit(r)
            return
        super().mouseReleaseEvent(e)


class DrawingViewer(QWidget):
    balloonClicked = Signal(int)
    balloonMoved = Signal(int, float, float)                 # char id, x, y normalized
    addBalloonAt = Signal(int, int, float, float)            # attachment id, page, x, y
    autoBalloonRequested = Signal(int, int)                  # attachment id, page
    balloonContext = Signal(int, object)
    documentChanged = Signal()
    areaBalloonRequested = Signal(int, int, object)        # attachment id, page, (x0,y0,x1,y1) normalised
    balloonPartRequested = Signal()
    ocrSetupRequested = Signal(int, int)                     # attachment id, page

    def __init__(self, loader: Callable[[int], bytes], parent=None):
        super().__init__(parent)
        self.loader = loader
        self.docs: list[dict] = []
        self.chars: list[dict] = []
        self.balloons: dict[int, BalloonItem] = {}
        self.mode = "select"
        self.locked = False
        self.scale_factor = 1.0
        self.balloon_scale = 1.0
        self.page_item: Optional[QGraphicsPixmapItem] = None
        self.page_w = self.page_h = 1
        self._selected: Optional[int] = None

        self.scene = QGraphicsScene(self)
        self.view = _View(self.scene, self)
        self.view.clicked.connect(self._on_click)
        self.view.areaSelected.connect(self._on_area)
        self.regions: dict = {}                   # (attachment id, page) -> [normalised rects]
        self.region_items = []

        self.doc_combo = QComboBox()
        self.doc_combo.setMinimumWidth(140)
        self.doc_combo.setMaximumWidth(280)
        self.doc_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.doc_combo.setMinimumContentsLength(14)
        self.doc_combo.currentIndexChanged.connect(self._doc_changed)
        self.page_spin = QSpinBox()
        self.page_spin.setPrefix("Sheet ")
        self.page_spin.setMinimum(1)
        self.page_spin.valueChanged.connect(lambda _: self.render())
        self.page_lbl = QLabel("")

        def tb(text, tip, fn, checkable=False):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.setCheckable(checkable)
            b.clicked.connect(fn)
            return b

        self.btn_select = tb("✋ Pan/Select", "Pan the drawing, click or drag balloons", lambda: self.set_mode("select"), True)
        self.btn_add = tb("➕ Add Balloon", "Click on the drawing to place a balloon.\n"
                                           "If the selected characteristic has no balloon it is placed for that row,\n"
                                           "otherwise a new characteristic is created.", lambda: self.set_mode("add"), True)
        self.btn_select.setChecked(True)
        self.btn_auto = tb("⚡ Whole Sheet", "Auto-balloon every callout on this sheet (vector PDF text or OCR)",
                           self._auto)
        self.btn_part = tb("🎯 Balloon Part…", "Choose which part number on this drawing to balloon, then auto-balloon "
                                              "only that part's views", lambda: self.balloonPartRequested.emit())
        self.btn_area = tb("⬚ Balloon Area", "Drag a box around a view: every callout inside it is ballooned",
                           lambda: self.set_mode("area"), True)
        self.btn_ocr = tb("🧪 OCR", "OCR preprocessing (OpenCV) for scanned drawings — preview on this sheet",
                          self._ocr_setup)
        zoom_in = tb("＋", "Zoom in", lambda: self.zoom_by(1.25))
        zoom_out = tb("－", "Zoom out", lambda: self.zoom_by(0.8))
        fit = tb("Fit", "Fit sheet to window", self.fit)
        self.size_spin = QDoubleSpinBox()
        self.size_spin.setRange(0.4, 4.0)
        self.size_spin.setSingleStep(0.1)
        self.size_spin.setValue(1.0)
        self.size_spin.setPrefix("Balloon ×")
        self.size_spin.valueChanged.connect(self._size_changed)

        from PySide6.QtWidgets import QMenu
        from .common import FlowRow
        # compact: Whole Sheet / Balloon Area / OCR live in the Balloon Part menu, short labels + tooltips
        self.btn_part.setPopupMode(QToolButton.MenuButtonPopup)
        pm = QMenu(self.btn_part)
        pm.addAction("🎯 Balloon a part number…", lambda: self.balloonPartRequested.emit())
        pm.addAction("⚡ Balloon the whole sheet", self._auto)
        pm.addAction("⬚ Balloon an area (drag a box)", lambda: self.set_mode("area"))
        pm.addSeparator()
        pm.addAction("🧪 OCR setup for scans…", self._ocr_setup)
        self.btn_part.setMenu(pm)
        self.btn_select.setText("✋")
        self.btn_add.setText("➕ Balloon")
        self.btn_area.setText("⬚ Area")
        self.page_spin.setPrefix("Sh ")
        self.size_spin.setPrefix("Size ×")
        self.btn_auto.hide()
        self.btn_ocr.hide()
        top = FlowRow(spacing=3)                 # wraps onto a second line only when the drawing pane is narrow
        top.flow.setContentsMargins(4, 3, 4, 0)
        for w in (self.doc_combo, self.page_spin, self.page_lbl, self.btn_part, self.btn_select, self.btn_add,
                  self.btn_area, zoom_out, zoom_in, fit, self.size_spin):
            top.addWidget(w)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(top)
        lay.addWidget(self.view, 1)
        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color:#4a4f52; padding:2px 6px;")
        lay.addWidget(self.hint)
        self._update_hint()

    # ------------------------------------------------------------------ public API
    def set_documents(self, docs: list[dict]):
        cur = self.current_doc()
        cur_id = cur["id"] if cur else None
        self.docs = docs
        self.doc_combo.blockSignals(True)
        self.doc_combo.clear()
        for d in docs:
            self.doc_combo.addItem(f"[{d['kind']}] {d['filename']}", d["id"])
        idx = next((i for i, d in enumerate(docs) if d["id"] == cur_id), 0)
        self.doc_combo.setCurrentIndex(idx if docs else -1)
        self.doc_combo.blockSignals(False)
        if (self.current_doc() or {}).get("id") != cur_id or self.page_item is None:
            self._doc_changed()

    def current_doc(self) -> Optional[dict]:
        i = self.doc_combo.currentIndex()
        return self.docs[i] if 0 <= i < len(self.docs) else None

    def current_page(self) -> int:
        return self.page_spin.value() - 1

    def set_characteristics(self, chars: list[dict]):
        self.chars = chars
        self._sync_balloons()

    def update_characteristic(self, char: dict):
        for i, c in enumerate(self.chars):
            if c["id"] == char["id"]:
                self.chars[i] = char
                break
        else:
            self.chars.append(char)
        self._sync_balloons()

    def remove_characteristic(self, char_id: int):
        self.chars = [c for c in self.chars if c["id"] != char_id]
        self._sync_balloons()

    def set_locked(self, locked: bool):
        self.locked = locked
        for b in (self.btn_add, self.btn_auto, self.btn_part, self.btn_area):
            b.setEnabled(not locked)
        if locked:
            self.set_mode("select")
        for b in self.balloons.values():
            b.setFlag(QGraphicsItem.ItemIsMovable, not locked)

    def select_char(self, char_id: Optional[int], center: bool = True):
        self._selected = char_id
        c = next((c for c in self.chars if c["id"] == char_id), None)
        if c and c.get("balloon_attachment_id") and c.get("balloon_x") is not None:
            doc = self.current_doc()
            if not doc or doc["id"] != c["balloon_attachment_id"] or self.current_page() != (c["balloon_page"] or 0):
                i = next((i for i, d in enumerate(self.docs) if d["id"] == c["balloon_attachment_id"]), -1)
                if i >= 0:
                    self.doc_combo.setCurrentIndex(i)
                    self.page_spin.setValue((c["balloon_page"] or 0) + 1)
        for cid, b in self.balloons.items():
            b.set_highlight(cid == char_id)
        if center and char_id in self.balloons:
            self.view.centerOn(self.balloons[char_id])

    def set_mode(self, mode: str):
        self.mode = mode
        self.btn_select.setChecked(mode == "select")
        self.btn_add.setChecked(mode == "add")
        self.btn_area.setChecked(mode == "area")
        self.view.setDragMode(QGraphicsView.ScrollHandDrag if mode == "select" else QGraphicsView.NoDrag)
        self.view.viewport().setCursor(Qt.CrossCursor if mode in ("add", "area") else Qt.OpenHandCursor)
        self._update_hint()

    # ------------------------------------------------------------------ rendering
    def _doc_changed(self):
        doc = self.current_doc()
        self.page_spin.blockSignals(True)
        self.page_spin.setMaximum(max(1, (doc or {}).get("page_count") or 1))
        self.page_spin.setValue(1)
        self.page_spin.blockSignals(False)
        self.page_lbl.setText(f"of {(doc or {}).get('page_count') or 1}")
        self.render(fit=True)
        self.documentChanged.emit()

    def render(self, fit: bool = False):
        self.scene.clear()
        self.balloons.clear()
        self.page_item = None
        doc = self.current_doc()
        if not doc:
            t = self.scene.addText("Upload a drawing (PDF / TIFF / JPEG) on the Attachments tab,\n"
                                   "or drag-and-drop files onto this window.")
            t.setDefaultTextColor(QColor("white"))
            return
        try:
            data = self.loader(doc["id"])
            page = self.current_page()
            dpi = 144
            if is_pdf(doc["filename"]):
                import pymupdf
                with pymupdf.open(stream=data, filetype="pdf") as d:
                    r = d[page].rect
                    long_in = max(r.width, r.height) / 72.0
                    dpi = int(max(96, min(300, 4200 / max(long_in, 1))))
            png = render_page(data, doc["filename"], page, dpi=dpi, key=f"{doc['sha256']}")
            img = QImage.fromData(png)
            pm = QPixmap.fromImage(img)
        except Exception as e:  # noqa: BLE001
            t = self.scene.addText(f"Cannot display {doc['filename']}:\n{e}")
            t.setDefaultTextColor(QColor("white"))
            return
        self.page_item = self.scene.addPixmap(pm)
        self.page_item.setZValue(0)
        self.page_w, self.page_h = pm.width(), pm.height()
        self.scene.setSceneRect(QRectF(-40, -40, self.page_w + 80, self.page_h + 80))
        self._sync_balloons()
        self._draw_regions()
        if fit:
            self.fit()

    def _radius(self, c=None):
        """Balloon radius: sized from the callout text when the balloon was placed automatically."""
        size = (c or {}).get("balloon_size") or 0.0085
        size = min(max(size, 0.0035), 0.02)
        return max(self.page_w, self.page_h) * size * self.balloon_scale

    def _sync_balloons(self):
        if self.page_item is None:
            return
        doc = self.current_doc()
        page = self.current_page()
        want = {c["id"]: c for c in self.chars
                if doc and c.get("balloon_attachment_id") == doc["id"] and (c.get("balloon_page") or 0) == page
                and c.get("balloon_x") is not None}
        for cid in list(self.balloons):
            if cid not in want:
                self.scene.removeItem(self.balloons.pop(cid))
        for cid, c in want.items():
            r = self._radius(c)
            b = self.balloons.get(cid)
            if b is None or abs(b.r - r) > 0.01:
                if b is not None:
                    self.scene.removeItem(b)
                b = BalloonItem(self, c, r)
                self.scene.addItem(b)
                self.balloons[cid] = b
            else:
                b.update_char(c)
            b.setFlag(QGraphicsItem.ItemIsMovable, not self.locked)
            b.setPos(c["balloon_x"] * self.page_w, c["balloon_y"] * self.page_h)
            b.set_highlight(cid == self._selected)

    def fit(self):
        if self.page_item:
            self.view.fitInView(self.page_item, Qt.KeepAspectRatio)
            self.scale_factor = self.view.transform().m11()

    def zoom_by(self, f: float):
        s = self.view.transform().m11() * f
        if 0.02 < s < 12:
            self.view.scale(f, f)

    def _size_changed(self, v):
        self.balloon_scale = v
        self._sync_balloons()

    # ------------------------------------------------------------------ interactions
    def _on_click(self, pt: QPointF):
        doc = self.current_doc()
        if not doc or self.page_item is None or self.locked:
            return
        x = min(max(pt.x() / self.page_w, 0), 1)
        y = min(max(pt.y() / self.page_h, 0), 1)
        self.addBalloonAt.emit(doc["id"], self.current_page(), x, y)

    def _balloon_moved(self, b: BalloonItem):
        x = min(max(b.pos().x() / self.page_w, 0), 1)
        y = min(max(b.pos().y() / self.page_h, 0), 1)
        self.balloonMoved.emit(b.char_id, x, y)

    def set_regions(self, regions: dict):
        self.regions = regions or {}
        self._draw_regions()

    def _draw_regions(self):
        for it in self.region_items:
            try:
                self.scene.removeItem(it)
            except Exception:
                pass
        self.region_items = []
        doc = self.current_doc()
        if not doc or self.page_item is None:
            return
        for r in self.regions.get((doc["id"], self.current_page()), []):
            it = QGraphicsRectItem(r[0] * self.page_w, r[1] * self.page_h, (r[2] - r[0]) * self.page_w,
                                   (r[3] - r[1]) * self.page_h)
            it.setPen(QPen(QColor(11, 92, 173, 150), max(1.5, self.page_w * 0.0008), Qt.DashLine))
            it.setBrush(QBrush(QColor(11, 92, 173, 22)))
            it.setZValue(1)
            it.setAcceptedMouseButtons(Qt.NoButton)
            self.scene.addItem(it)
            self.region_items.append(it)

    def _on_area(self, r: QRectF):
        doc = self.current_doc()
        if not doc or self.page_item is None or self.locked:
            return
        box = (max(0.0, r.left() / self.page_w), max(0.0, r.top() / self.page_h),
               min(1.0, r.right() / self.page_w), min(1.0, r.bottom() / self.page_h))
        self.areaBalloonRequested.emit(doc["id"], self.current_page(), box)

    def _auto(self):
        doc = self.current_doc()
        if doc:
            self.autoBalloonRequested.emit(doc["id"], self.current_page())

    def _ocr_setup(self):
        doc = self.current_doc()
        self.ocrSetupRequested.emit(doc["id"] if doc else -1, self.current_page() if doc else 0)

    def _update_hint(self):
        if self.mode == "add":
            self.hint.setText("ADD BALLOON MODE — click the drawing to place a balloon. Press ✋ to go back to pan/select.")
        elif self.mode == "area":
            self.hint.setText("BALLOON AREA MODE — drag a box around a view; every callout inside it is found and "
                              "offered for ballooning. Press ✋ to go back.")
        else:
            self.hint.setText("Mouse wheel = zoom • drag = pan • click a balloon to select its row • drag a balloon to move it"
                              " • right-click a balloon for options")
