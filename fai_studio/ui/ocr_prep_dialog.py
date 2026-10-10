"""OCR Preprocessing (OpenCV) settings with a live before/after preview on a real drawing sheet.

The settings are shared by every workstation (app_meta 'ocr_preprocess') and are part of the OCR cache key:
after a change, scanned drawings are re-read with the new clean-up the next time they are analysed."""
from __future__ import annotations

import io
import json
import os
from typing import Optional

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QGridLayout, QGroupBox, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
                               QPushButton, QScrollArea, QSpinBox, QSplitter, QVBoxLayout, QWidget)

from .. import ocr
from .common import fit_to_screen

META_KEY = "ocr_preprocess"
CROP_W, CROP_H = 1000, 700          # preview cut-out, in OCR-resolution pixels


def load_shared(db) -> dict:
    """Read the shared setting from the database and activate it in this workstation."""
    try:
        ocr.set_preprocess(db.get_meta().get(META_KEY, ""))
    except Exception:
        pass
    return ocr.get_preprocess()


def _pil_to_qimage(im) -> QImage:
    im = im.convert("RGB")
    data = im.tobytes()
    return QImage(data, im.width, im.height, im.width * 3, QImage.Format_RGB888).copy()


class _Overview(QLabel):
    """Whole-sheet thumbnail; click to choose the area shown in the preview."""
    picked = Signal(float, float)

    def __init__(self):
        super().__init__()
        self.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.setCursor(Qt.CrossCursor)
        self.base: Optional[QPixmap] = None
        self.box = None

    def set_sheet(self, pm: QPixmap):
        self.base = pm
        self._draw()

    def set_box(self, box):
        self.box = box
        self._draw()

    def _draw(self):
        if self.base is None:
            self.clear()
            return
        pm = QPixmap(self.base)
        if self.box:
            p = QPainter(pm)
            p.setPen(QPen(QColor("#FF6A00"), 2))
            x0, y0, x1, y1 = self.box
            p.drawRect(int(x0 * pm.width()), int(y0 * pm.height()),
                       int((x1 - x0) * pm.width()), int((y1 - y0) * pm.height()))
            p.end()
        self.setPixmap(pm)

    def mousePressEvent(self, e):
        if self.base is None:
            return
        pos: QPoint = e.position().toPoint() if hasattr(e, "position") else e.pos()
        self.picked.emit(min(1, max(0, pos.x() / self.base.width())), min(1, max(0, pos.y() / self.base.height())))


class OcrPrepDialog(QDialog):
    def __init__(self, db, parent=None, data: Optional[bytes] = None, filename: str = "", page: int = 0):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("OCR Preprocessing (OpenCV) — shared by all workstations")
        fit_to_screen(self, 1400, 860)
        self.saved_opts = load_shared(db)
        self._busy = False
        self.sheet = None                 # PIL image of the current sheet at OCR resolution
        self.data, self.filename, self.page = None, "", 0
        self.center = (0.5, 0.5)

        # ---------------------------------------------------------------- settings
        self.profile = QComboBox()
        for k in ("off", "standard", "heavy", "faint", "custom"):
            self.profile.addItem(ocr.PROFILE_NAMES[k], k)
        self.cb_deskew = QCheckBox("Deskew (straighten a crooked scan)")
        self.cb_lines = QCheckBox("Remove long ruling lines (grid paper only – can clip boxed text)")
        self.cb_sharpen = QCheckBox("Sharpen (unsharp mask)")
        self.cb_background = QCheckBox("Flatten background (yellowed paper, shadows, copier streaks)")
        self.cb_contrast = QCheckBox("Boost local contrast (CLAHE) for faint text")
        self.cb_thicken = QCheckBox("Thicken thin / broken strokes")
        self.despeckle = QComboBox()
        self.despeckle.addItems(["Off", "Light (dust)", "Strong (heavy speckle / fax noise)"])
        self.denoise = QComboBox()
        self.denoise.addItems(["Off", "Median 3×3 (fast)", "Non-local means (strong, slow on big sheets)"])
        self.binarize = QComboBox()
        for label, v in (("Off (keep greyscale)", "off"), ("Otsu (clean, even scans)", "otsu"),
                         ("Adaptive (uneven lighting / stains)", "adaptive")):
            self.binarize.addItem(label, v)
        self.upscale = QComboBox()
        for label, v in (("1× (native)", 1.0), ("1.5× (small lettering)", 1.5), ("2× (very small lettering)", 2.0)):
            self.upscale.addItem(label, v)

        g = QGroupBox("Filters (applied in this order before OCR)")
        f = QFormLayout(g)
        f.addRow("Enlarge", self.upscale)
        f.addRow(self.cb_background)
        f.addRow("Denoise", self.denoise)
        f.addRow(self.cb_contrast)
        f.addRow(self.cb_deskew)
        f.addRow("Black && white", self.binarize)
        f.addRow("Despeckle", self.despeckle)
        f.addRow(self.cb_lines)
        f.addRow(self.cb_thicken)
        f.addRow(self.cb_sharpen)

        self._controls = (self.cb_deskew, self.cb_lines, self.cb_sharpen, self.cb_background, self.cb_contrast,
                          self.cb_thicken, self.despeckle, self.denoise, self.binarize, self.upscale)
        self._apply_to_controls(self.saved_opts)
        self.profile.currentIndexChanged.connect(self._profile_changed)
        for w in self._controls:
            (w.toggled if isinstance(w, QCheckBox) else w.currentIndexChanged).connect(self._control_changed)

        note = QLabel("These settings are saved in the database and used by <b>every workstation</b>. "
                      "Scanned drawings are re-read with the new settings the next time they are analysed "
                      "(results for each setting are cached separately, so switching back is instant). "
                      "Vector CAD PDFs are not affected. Everything runs locally — nothing leaves this PC.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#4a4f52")

        left = QVBoxLayout()
        pf = QFormLayout()
        pf.addRow(QLabel("<b>Profile:</b>"), self.profile)
        left.addLayout(pf)
        left.addWidget(g)
        left.addWidget(note)
        left.addStretch(1)
        leftw = QWidget()
        leftw.setLayout(left)
        leftw.setMaximumWidth(470)

        # ---------------------------------------------------------------- preview
        self.load_btn = QPushButton("Open a drawing to preview…")
        self.load_btn.clicked.connect(self._pick_file)
        self.page_spin = QSpinBox()
        self.page_spin.setPrefix("Sheet ")
        self.page_spin.setMinimum(1)
        self.page_spin.valueChanged.connect(lambda v: self._load(self.data, self.filename, v - 1))
        self.src_lbl = QLabel("No drawing loaded")
        self.test_btn = QPushButton("🔍 Test read this area")
        self.test_btn.setToolTip("Run OCR on the area before and after preprocessing and compare")
        self.test_btn.clicked.connect(self._test_read)
        self.overview = _Overview()
        self.overview.picked.connect(self._pick_area)
        self.before = QLabel()
        self.after = QLabel()
        for lab in (self.before, self.after):
            lab.setAlignment(Qt.AlignCenter)
            lab.setStyleSheet("background:white; border:1px solid #D0D3D6")
        self.skew_lbl = QLabel("")
        self.read_before = QPlainTextEdit()
        self.read_after = QPlainTextEdit()
        for t in (self.read_before, self.read_after):
            t.setReadOnly(True)
            t.setMaximumHeight(150)
            t.setPlaceholderText("Click “Test read this area”")

        bar = QHBoxLayout()
        for w in (self.load_btn, self.page_spin, self.src_lbl):
            bar.addWidget(w)
        bar.addStretch(1)
        bar.addWidget(self.test_btn)
        grid = QGridLayout()
        grid.addWidget(QLabel("<b>Original scan</b>"), 0, 0)
        grid.addWidget(QLabel("<b>After preprocessing</b> (what the OCR sees)"), 0, 1)
        sa, sb = QScrollArea(), QScrollArea()
        self.sa, self.sb = sa, sb
        sa.setWidget(self.before)
        sb.setWidget(self.after)
        for s_ in (sa, sb):
            s_.setWidgetResizable(True)
        # keep both views scrolled together
        sa.horizontalScrollBar().valueChanged.connect(sb.horizontalScrollBar().setValue)
        sa.verticalScrollBar().valueChanged.connect(sb.verticalScrollBar().setValue)
        sb.horizontalScrollBar().valueChanged.connect(sa.horizontalScrollBar().setValue)
        sb.verticalScrollBar().valueChanged.connect(sa.verticalScrollBar().setValue)
        grid.addWidget(sa, 1, 0)
        grid.addWidget(sb, 1, 1)
        grid.addWidget(self.read_before, 2, 0)
        grid.addWidget(self.read_after, 2, 1)
        grid.setRowStretch(1, 1)
        ov_box = QVBoxLayout()
        ov_box.addWidget(QLabel("Whole sheet — click to choose the area"))
        ov_box.addWidget(self.overview)
        ov_box.addWidget(self.skew_lbl)
        ov_box.addStretch(1)
        prev = QHBoxLayout()
        prev.addLayout(ov_box)
        prev.addLayout(grid, 1)
        rightw = QWidget()
        rl = QVBoxLayout(rightw)
        rl.addLayout(bar)
        rl.addLayout(prev, 1)

        split = QSplitter()
        split.addWidget(leftw)
        split.addWidget(rightw)
        split.setStretchFactor(1, 1)

        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Save).setText("Save for all workstations")
        bb.button(QDialogButtonBox.Save).setObjectName("primary")
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(split, 1)
        lay.addWidget(bb)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._update_preview)
        if data:
            self._load(data, filename, page)

    # ---------------------------------------------------------------- settings <-> controls
    def _apply_to_controls(self, o: dict):
        self._busy = True
        try:
            self.profile.setCurrentIndex(max(0, self.profile.findData(o.get("profile", "standard"))))
            self.cb_deskew.setChecked(bool(o["deskew"]))
            self.cb_lines.setChecked(bool(o["remove_lines"]))
            self.cb_sharpen.setChecked(bool(o["sharpen"]))
            self.cb_background.setChecked(bool(o["background"]))
            self.cb_contrast.setChecked(bool(o["contrast"]))
            self.cb_thicken.setChecked(bool(o["thicken"]))
            self.despeckle.setCurrentIndex(int(o["despeckle"]))
            self.denoise.setCurrentIndex(int(o["denoise"]))
            self.binarize.setCurrentIndex(max(0, self.binarize.findData(o["binarize"])))
            self.upscale.setCurrentIndex(max(0, self.upscale.findData(float(o["upscale"]))))
        finally:
            self._busy = False

    def options(self) -> dict:
        d = dict(deskew=self.cb_deskew.isChecked(), remove_lines=self.cb_lines.isChecked(),
                 sharpen=self.cb_sharpen.isChecked(), background=self.cb_background.isChecked(),
                 contrast=self.cb_contrast.isChecked(), thicken=self.cb_thicken.isChecked(),
                 despeckle=self.despeckle.currentIndex(), denoise=self.denoise.currentIndex(),
                 binarize=self.binarize.currentData(), upscale=float(self.upscale.currentData()))
        prof = self.profile.currentData()
        if prof != "custom" and any(d[k] != ocr.PROFILES[prof][k] for k in ocr.PREP_KEYS):
            prof = "custom"
        d["profile"] = prof
        return ocr.normalize_prep(d)

    def _profile_changed(self):
        if self._busy:
            return
        k = self.profile.currentData()
        if k in ocr.PROFILES:
            self._apply_to_controls(dict(profile=k, **ocr.PROFILES[k]))
        self._schedule()

    def _control_changed(self):
        if self._busy:
            return
        o = self.options()
        if o["profile"] != self.profile.currentData():
            self._busy = True
            self.profile.setCurrentIndex(self.profile.findData(o["profile"]))
            self._busy = False
        self._schedule()

    # ---------------------------------------------------------------- preview source
    def _pick_file(self):
        fn, _ = QFileDialog.getOpenFileName(self, "Drawing to preview", "",
                                            "Drawings (*.pdf *.tif *.tiff *.jpg *.jpeg *.png *.bmp)")
        if fn:
            with open(fn, "rb") as fh:
                self._load(fh.read(), os.path.basename(fn), 0)

    def _load(self, data, filename, page):
        if not data:
            return
        from PIL import Image
        try:
            self.setCursor(Qt.WaitCursor)
            if data[:5] == b"%PDF-":
                import pymupdf
                with pymupdf.open(stream=data, filetype="pdf") as doc:
                    n = doc.page_count
                    page = min(max(page, 0), n - 1)
                    pg = doc[page]
                    long_in = max(pg.rect.width, pg.rect.height) / 72.0
                    dpi = int(max(150, min(600, 6600 / max(long_in, 1))))    # same resolution the OCR uses
                    dpi = int(min(dpi, 9000 / max(long_in, 1)))
                    pix = pg.get_pixmap(dpi=dpi, alpha=False)
                    im = Image.open(io.BytesIO(pix.tobytes("png")))
            else:
                from ..docrender import open_image_page
                with Image.open(io.BytesIO(data)) as im0:
                    n = getattr(im0, "n_frames", 1) or 1
                page = min(max(page, 0), n - 1)
                im = open_image_page(data, page)
        except Exception as e:  # noqa: BLE001
            self.unsetCursor()
            QMessageBox.warning(self, "Preview", f"Could not open that file:\n{e}")
            return
        self.unsetCursor()
        self.data, self.filename, self.page, self.sheet = data, filename, page, im.convert("L")
        self.page_spin.blockSignals(True)
        self.page_spin.setMaximum(n)
        self.page_spin.setValue(page + 1)
        self.page_spin.blockSignals(False)
        self.src_lbl.setText(f"{filename}  ({im.width}×{im.height} px)")
        ov = self.sheet.copy()
        ov.thumbnail((300, 300))
        self.overview.set_sheet(QPixmap.fromImage(_pil_to_qimage(ov)))
        try:
            import numpy as np
            ang = ocr._deskew_angle(np.array(self.sheet))
            self.skew_lbl.setText(f"Detected skew: {ang:+.2f}°" + ("  (deskew will correct it)" if abs(ang) > 0.12
                                                                     else "  (straight)"))
        except Exception:
            self.skew_lbl.setText("")
        self._pick_area(*self._busiest_area())

    def _busiest_area(self):
        """Start on the area with the most lettering (ruling lines ignored), away from border and title block."""
        try:
            import cv2
            import numpy as np
            g = np.array(self.sheet)
            f = 2400 / max(g.shape)
            if f < 1:
                g = cv2.resize(g, (int(g.shape[1] * f), int(g.shape[0] * f)), interpolation=cv2.INTER_AREA)
            ink = (g < 160).astype(np.uint8) * 255
            hl, vl = ocr.line_masks(g, int(max(g.shape) * 0.02))
            ink[cv2.dilate(cv2.bitwise_or(hl, vl), np.ones((5, 5), np.uint8)) > 0] = 0
            d = cv2.resize(ink.astype(np.float32), (60, 40), interpolation=cv2.INTER_AREA)
            d = cv2.blur(d, (7, 7))
            d[:3, :] = d[-3:, :] = 0
            d[:, :3] = d[:, -3:] = 0
            d[-9:, 30:] = 0                                        # title block / revision band (bottom right)
            d[:8, 36:] = 0                                         # revision block (top right)
            if d.max() <= 0:
                return 0.5, 0.5
            y, x = np.unravel_index(int(d.argmax()), d.shape)
            return (x + 0.5) / 60, (y + 0.5) / 40
        except Exception:
            return 0.5, 0.5

    def _crop_box(self):
        W, H = self.sheet.size
        cx, cy = self.center
        x0 = int(min(max(cx * W - CROP_W / 2, 0), max(W - CROP_W, 0)))
        y0 = int(min(max(cy * H - CROP_H / 2, 0), max(H - CROP_H, 0)))
        return x0, y0, min(W, x0 + CROP_W), min(H, y0 + CROP_H)

    def _pick_area(self, fx, fy):
        if self.sheet is None:
            return
        self.center = (fx, fy)
        W, H = self.sheet.size
        x0, y0, x1, y1 = self._crop_box()
        self.overview.set_box((x0 / W, y0 / H, x1 / W, y1 / H))
        self.read_before.clear()
        self.read_after.clear()
        self._schedule()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.sheet is not None:
            self._schedule()

    # ---------------------------------------------------------------- processing
    def _schedule(self):
        self._timer.start(200)

    def _processed(self):
        """(original crop, processed crop) — processed with a margin so filters see context."""
        W, H = self.sheet.size
        x0, y0, x1, y1 = self._crop_box()
        m = 120
        X0, Y0, X1, Y1 = max(0, x0 - m), max(0, y0 - m), min(W, x1 + m), min(H, y1 + m)
        region = self.sheet.crop((X0, Y0, X1, Y1))
        o = self.options()
        o_prev = dict(o, deskew=False)                  # skew is measured on the whole sheet (shown at left)
        clean, _ = ocr.clean_for_ocr(region, o_prev, ref_size=max(W, H))
        sc = o["upscale"] if o["upscale"] > 1 and max(W, H) * o["upscale"] <= 12000 else 1.0
        cx0, cy0 = int((x0 - X0) * sc), int((y0 - Y0) * sc)
        clean = clean.crop((cx0, cy0, cx0 + int((x1 - x0) * sc), cy0 + int((y1 - y0) * sc)))
        if sc != 1.0:
            clean = clean.resize((x1 - x0, y1 - y0))      # show at the same scale as the original
        return self.sheet.crop((x0, y0, x1, y1)), clean, region, (x0 - X0, y0 - Y0, x1 - x0, y1 - y0)

    def _update_preview(self):
        if self.sheet is None:
            return
        self.setCursor(Qt.WaitCursor)
        try:
            orig, clean, _, _ = self._processed()
            w = max(200, self.sa.viewport().width() - 4)
            for lab, im in ((self.before, orig), (self.after, clean)):
                pm = QPixmap.fromImage(_pil_to_qimage(im))
                if pm.width() > w:                  # fit the pane width; scroll vertically if needed
                    pm = pm.scaledToWidth(w, Qt.SmoothTransformation)
                lab.setPixmap(pm)
        except Exception as e:  # noqa: BLE001
            self.after.setText(f"Preview failed: {e}")
        finally:
            self.unsetCursor()

    def _test_read(self):
        if self.sheet is None:
            QMessageBox.information(self, "Test read", "Open a drawing first.")
            return
        if not ocr.available():
            QMessageBox.information(self, "Test read", "No OCR engine is installed on this workstation.")
            return
        from .workers import run_with_progress
        orig, _, region, (ox, oy, w, h) = self._processed()
        o = dict(self.options(), deskew=False)
        W, H = self.sheet.size

        def work(cb):
            cb("Reading the original…", 0.1)
            a = ocr.ocr_image(orig.convert("RGB"))
            cb("Reading the preprocessed image…", 0.55)
            clean, back = ocr.clean_for_ocr(region, o, ref_size=max(W, H))
            b = []
            for x0, y0, x1, y1, t, k in ocr.ocr_image(clean):
                X, Y = back((x0 + x1) / 2, (y0 + y1) / 2)
                if ox <= X <= ox + w and oy <= Y <= oy + h:
                    b.append((X - ox, Y - oy, t, k))
            return [((x0 + x1) / 2, (y0 + y1) / 2, t, k) for x0, y0, x1, y1, t, k in a], b
        res = run_with_progress(self, "Test read", work)
        if not res:
            return
        from ..drawing_intel import norm_text

        def fmt(items):
            items = sorted(items, key=lambda r: (round(r[1] / 25), r[0]))
            if not items:
                return "(nothing read)"
            avg = sum(r[3] for r in items) / len(items)
            lines = [f"{len(items)} pieces of text · average confidence {avg:.0%}"]
            lines += [f"{norm_text(t, ocr=True):<32} {k:.0%}" for _, _, t, k in items]
            return "\n".join(lines)
        self.read_before.setPlainText(fmt(res[0]))
        self.read_after.setPlainText(fmt(res[1]))

    # ---------------------------------------------------------------- save
    def _save(self):
        o = self.options()
        self.db.set_meta({META_KEY: json.dumps(o)})
        ocr.set_preprocess(o)
        from ..drawing_intel import _CACHE
        _CACHE.clear()
        self.accept()
