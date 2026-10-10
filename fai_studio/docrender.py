"""Render PDF / image attachments (incl. multi-page TIFF) to raster pages."""
from __future__ import annotations

import io
from pathlib import Path

import pymupdf
from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = 400_000_000   # large-format scanned drawings

IMAGE_EXT = {".tif", ".tiff", ".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
PDF_EXT = {".pdf"}


def is_pdf(filename: str) -> bool:
    return Path(filename).suffix.lower() in PDF_EXT


def is_image(filename: str) -> bool:
    return Path(filename).suffix.lower() in IMAGE_EXT


def is_viewable(filename: str) -> bool:
    return is_pdf(filename) or is_image(filename)


def page_count_for(data: bytes, filename: str) -> int:
    try:
        if is_pdf(filename):
            with pymupdf.open(stream=data, filetype="pdf") as doc:
                return doc.page_count
        if is_image(filename):
            with Image.open(io.BytesIO(data)) as im:
                return getattr(im, "n_frames", 1) or 1
    except Exception:
        pass
    return 1


def open_image_page(data: bytes, page: int = 0) -> Image.Image:
    im = Image.open(io.BytesIO(data))
    if getattr(im, "n_frames", 1) > 1:
        im.seek(min(page, im.n_frames - 1))
    im = ImageOps.exif_transpose(im) if page == 0 else im
    if im.mode not in ("RGB", "L"):
        if im.mode in ("1", "I;16", "I"):
            im = im.convert("L")
        else:
            bg = Image.new("RGB", im.size, "white")
            rgba = im.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[-1])
            im = bg
    return im.copy()


_render_cache: dict[tuple, bytes] = {}


def render_page(data: bytes, filename: str, page: int = 0, dpi: int = 144, key: str = "") -> bytes:
    """Return PNG bytes for a page. Cached by (key, page, dpi)."""
    ck = (key or id(data), page, dpi)
    if key and ck in _render_cache:
        return _render_cache[ck]
    if is_pdf(filename):
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            pg = doc[min(page, doc.page_count - 1)]
            pix = pg.get_pixmap(dpi=dpi, alpha=False)
            out = pix.tobytes("png")
    else:
        im = open_image_page(data, page)
        # keep previews to a sane size (very large scans)
        max_side = 6000
        if max(im.size) > max_side:
            im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        out = buf.getvalue()
    if key:
        if len(_render_cache) > 40:
            _render_cache.pop(next(iter(_render_cache)))
        _render_cache[ck] = out
    return out


def page_size_points(data: bytes, filename: str, page: int = 0) -> tuple[float, float]:
    if is_pdf(filename):
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            r = doc[min(page, doc.page_count - 1)].rect
            return r.width, r.height
    im = open_image_page(data, page)
    dpi = im.info.get("dpi", (150, 150))[0] or 150
    return im.size[0] * 72.0 / dpi, im.size[1] * 72.0 / dpi


def pdf_text(data: bytes) -> str:
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return "\n".join(p.get_text() for p in doc)
