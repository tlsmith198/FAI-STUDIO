"""Small pictures of each detected callout, cut from the drawing, for the review list.

Everything stays local (rendered from the attachment bytes already in the database cache)."""
from __future__ import annotations

from typing import Optional

import pymupdf
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap, QTransform

THUMB_H = 44          # displayed height of a horizontal callout, px
MAX_W = 300


class CalloutThumbs:
    def __init__(self, data: bytes, filename: str = ""):
        self.data = data
        self.is_pdf = data[:5] == b"%PDF-" or filename.lower().endswith(".pdf")
        self._doc = None
        self._dl: dict[int, object] = {}
        self._img: dict[int, object] = {}

    # --------------------------------------------------------------- page sources (cached)
    def _pdf_page(self, page: int):
        if self._doc is None:
            self._doc = pymupdf.open(stream=self.data, filetype="pdf")
        pg = self._doc[page]
        if page not in self._dl:
            self._dl[page] = pg.get_displaylist()
        return pg, self._dl[page]

    def _image(self, page: int):
        if page not in self._img:
            from ..docrender import open_image_page
            self._img[page] = open_image_page(self.data, page)
        return self._img[page]

    # --------------------------------------------------------------- one callout
    def image(self, page: int, nbbox: tuple, rot: int = 0, large: bool = False) -> Optional[QImage]:
        if not nbbox or len(nbbox) != 4:
            return None
        vertical = rot in (90, 270)
        x0, y0, x1, y1 = nbbox
        w, h = max(x1 - x0, 1e-4), max(y1 - y0, 1e-4)
        th = w if vertical else h                    # text height (normalised)
        pad_long, pad_short = (3.0, 0.9) if large else (1.2, 0.5)
        if vertical:
            x0, x1 = x0 - th * pad_short, x1 + th * pad_short
            y0, y1 = y0 - th * pad_long, y1 + th * pad_long
        else:
            x0, x1 = x0 - th * pad_long, x1 + th * pad_long
            y0, y1 = y0 - th * pad_short, y1 + th * pad_short
        x0, y0, x1, y1 = max(0.0, x0), max(0.0, y0), min(1.0, x1), min(1.0, y1)
        target = (THUMB_H * 3) if large else THUMB_H
        try:
            if self.is_pdf:
                pg, dl = self._pdf_page(page)
                R = pg.rect
                clip = pymupdf.Rect(R.x0 + x0 * R.width, R.y0 + y0 * R.height,
                                    R.x0 + x1 * R.width, R.y0 + y1 * R.height)
                short = clip.width if vertical else clip.height
                z = min(max(target / max(short, 1e-3), 0.5), 12.0)
                pix = dl.get_pixmap(matrix=pymupdf.Matrix(z, z), clip=clip, alpha=False)
                img = QImage(pix.samples, pix.width, pix.height, pix.stride,
                             QImage.Format_RGB888 if pix.n == 3 else QImage.Format_Grayscale8).copy()
            else:
                im = self._image(page)
                W, H = im.size
                crop = im.crop((int(x0 * W), int(y0 * H), max(int(x1 * W), int(x0 * W) + 1),
                                max(int(y1 * H), int(y0 * H) + 1))).convert("RGB")
                data = crop.tobytes()
                img = QImage(data, crop.width, crop.height, crop.width * 3, QImage.Format_RGB888).copy()
        except Exception:
            return None
        if img.isNull():
            return None
        if vertical:                                  # turn vertical dims so they read left to right
            img = img.transformed(QTransform().rotate(90 if rot == 90 else -90), Qt.SmoothTransformation)
        img = img.scaledToHeight(target, Qt.SmoothTransformation)
        if not large and img.width() > MAX_W:
            img = img.scaled(MAX_W, target, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        return img

    def pixmap(self, page, nbbox, rot=0, large=False) -> Optional[QPixmap]:
        img = self.image(page, nbbox, rot, large)
        return QPixmap.fromImage(img) if img is not None else None

    def close(self):
        if self._doc is not None:
            self._doc.close()
            self._doc = None
