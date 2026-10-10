"""OCR for scanned drawings.

Engine order: RapidOCR (pip-installable, no admin rights, best on drawings) -> Tesseract (if installed).
Results are cached on disk per (file hash, page, dpi) so re-analysing a drawing is instant.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
from typing import Callable, Optional

from .config import cache_dir

_rapid = None


def _rapid_kind() -> Optional[str]:
    """'v3' = new `rapidocr` package (Python 3.9–3.14), 'v1' = legacy `rapidocr_onnxruntime` (Python ≤ 3.12)."""
    try:
        import rapidocr  # noqa: F401
        return "v3"
    except Exception:
        pass
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return "v1"
    except Exception:
        return None


def engine_name() -> Optional[str]:
    if _rapid_kind():
        return "RapidOCR"
    try:
        import pytesseract  # noqa: F401
        if shutil.which("tesseract") or _win_tesseract():
            return "Tesseract"
    except Exception:
        pass
    return None


def available() -> bool:
    return engine_name() is not None


def _win_tesseract():
    for p in (r"C:\Program Files\Tesseract-OCR\tesseract.exe", r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"):
        if os.path.exists(p):
            return p
    return None


def _cache_key(data_hash: str, page: int, dpi: int, engine: str) -> str:
    return hashlib.sha1(f"{data_hash}:{page}:{dpi}:{engine}:v6:{prep_key()}".encode()).hexdigest()


# ------------------------------------------------------------------------------------------ image cleanup
def _gray_np(im):
    import numpy as np
    return np.array(im.convert("L"))


def line_masks(gray, min_len_px: int):
    """Binary masks of horizontal and vertical ruling lines (dimension lines, borders, frames, boxes)."""
    import cv2
    import numpy as np
    inv = (gray < 150).astype(np.uint8) * 255
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, min_len_px), 1))
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(3, min_len_px)))
    return cv2.morphologyEx(inv, cv2.MORPH_OPEN, hk), cv2.morphologyEx(inv, cv2.MORPH_OPEN, vk)


def _deskew_angle(gray) -> float:
    """Skew of a scanned sheet from its long straight lines (border, title block), in degrees."""
    import cv2
    import numpy as np
    small = gray
    f = 1.0
    if max(gray.shape) > 3000:
        f = 3000 / max(gray.shape)
        small = cv2.resize(gray, (int(gray.shape[1] * f), int(gray.shape[0] * f)), interpolation=cv2.INTER_AREA)
    edges = cv2.Canny(small, 50, 150)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 1800, threshold=200, minLineLength=int(0.25 * max(small.shape)),
                            maxLineGap=8)
    if lines is None:
        return 0.0
    angs = []
    for x1, y1, x2, y2 in lines[:, 0]:
        a = np.degrees(np.arctan2(y2 - y1, x2 - x1))
        a = (a + 90) % 180 - 90                      # -90..90
        if abs(a) < 3:
            angs.append(a)
        elif abs(abs(a) - 90) < 3:
            angs.append(a - 90 * np.sign(a))
    return float(np.median(angs)) if len(angs) >= 3 else 0.0


# ------------------------------------------------------------------------------------------ preprocessing options
# Shared by all workstations (stored in app_meta "ocr_preprocess" as JSON) and part of every OCR cache key,
# so changing them makes scanned drawings be re-read with the new settings.
PREP_KEYS = ("deskew", "despeckle", "remove_lines", "sharpen", "background", "contrast", "denoise",
             "binarize", "thicken", "upscale")
PROFILES: dict[str, dict] = {
    "off":      dict(deskew=False, despeckle=0, remove_lines=False, sharpen=False, background=False,
                     contrast=False, denoise=0, binarize="off", thicken=False, upscale=1.0),
    "standard": dict(deskew=True, despeckle=1, remove_lines=False, sharpen=True, background=False,
                     contrast=False, denoise=0, binarize="off", thicken=False, upscale=1.0),
    "heavy":    dict(deskew=True, despeckle=2, remove_lines=False, sharpen=False, background=True,
                     contrast=False, denoise=1, binarize="adaptive", thicken=False, upscale=1.0),
    "faint":    dict(deskew=True, despeckle=1, remove_lines=False, sharpen=True, background=True,
                     contrast=True, denoise=1, binarize="off", thicken=True, upscale=1.5),
}
PROFILE_NAMES = {"off": "Off – read the scan as-is",
                 "standard": "Standard – deskew, despeckle, sharpen (recommended)",
                 "heavy": "Heavy clean-up – dirty / yellowed / shadowed / faxed copies",
                 "faint": "Faint or thin text – light pencil, faded prints, small lettering",
                 "custom": "Custom"}
_PREP: dict = dict(profile="standard", **PROFILES["standard"])


def normalize_prep(d: Optional[dict]) -> dict:
    d = dict(d or {})
    prof = d.get("profile", "standard")
    base = dict(PROFILES.get(prof if prof in PROFILES else "standard"))
    if prof == "custom" or prof not in PROFILES:
        base.update({k: d[k] for k in PREP_KEYS if k in d})
        prof = "custom"
    base["despeckle"] = int(base["despeckle"])
    base["denoise"] = int(base["denoise"])
    base["upscale"] = float(base["upscale"])
    base["profile"] = prof
    return base


def set_preprocess(d) -> None:
    """Accepts a dict or the JSON string stored in app_meta."""
    global _PREP
    if isinstance(d, str):
        try:
            d = json.loads(d) if d.strip() else None
        except Exception:
            d = None
    _PREP = normalize_prep(d)


def get_preprocess() -> dict:
    return dict(_PREP)


def prep_key(opts: Optional[dict] = None) -> str:
    o = normalize_prep(opts) if opts is not None else _PREP
    return hashlib.sha1(json.dumps({k: o[k] for k in PREP_KEYS}, sort_keys=True).encode()).hexdigest()[:10]


def local_filters_on(opts: Optional[dict] = None) -> bool:
    """True when filters that also help close-up re-reads are switched on."""
    o = opts or _PREP
    return bool(o["background"] or o["contrast"] or o["denoise"] or o["binarize"] != "off" or o["thicken"])


def clean_for_ocr(im, opts: Optional[dict] = None, local: bool = False, info: Optional[dict] = None,
                  ref_size: Optional[int] = None):
    """OpenCV preprocessing of a scanned drawing before OCR, driven by the preprocessing options.
    local=True (close-up crops): skip deskew, line stripping and upscaling.
    Returns (clean PIL RGB image, to_original(x, y) mapping)."""
    o = normalize_prep(opts) if opts is not None else _PREP
    try:
        import cv2
        import numpy as np
        from PIL import Image
    except Exception:
        return im, (lambda x, y: (x, y))
    g = _gray_np(im)
    sc = 1.0
    if not local and o["upscale"] > 1.0 and max(g.shape) * o["upscale"] <= 12000:
        sc = o["upscale"]
        g = cv2.resize(g, (int(g.shape[1] * sc), int(g.shape[0] * sc)), interpolation=cv2.INTER_CUBIC)
    H, W = g.shape
    REF = int(ref_size * sc) if ref_size else max(W, H)      # full-sheet size (previews work on a cut-out)
    if o["background"]:                         # flatten uneven paper: yellowing, shadows, copier gradients
        bg = cv2.GaussianBlur(g, (0, 0), max(15.0, REF / 120.0))
        g = cv2.divide(g, np.maximum(bg, 1), scale=255)
    if o["denoise"] == 1:
        g = cv2.medianBlur(g, 3)
    elif o["denoise"] >= 2:
        g = cv2.fastNlMeansDenoising(g, None, h=12, templateWindowSize=7, searchWindowSize=21)
    if o["contrast"]:                           # local contrast (CLAHE) for faint lettering; the light blur
        g = cv2.GaussianBlur(g, (3, 3), 0)      # first stops it amplifying paper grain
        g = cv2.createCLAHE(clipLimit=1.6, tileGridSize=(8, 8)).apply(g)
    M_inv = None
    ang = 0.0
    if o["deskew"] and not local:
        ang = _deskew_angle(g)
        if abs(ang) > 0.12:
            M = cv2.getRotationMatrix2D((W / 2, H / 2), ang, 1.0)
            g = cv2.warpAffine(g, M, (W, H), flags=cv2.INTER_LINEAR, borderValue=255)
            M_inv = cv2.invertAffineTransform(M)
    if info is not None:
        info["skew"] = ang
    inv = (g < 150).astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(inv, connectivity=8)
    hs = st[1:, cv2.CC_STAT_HEIGHT][(st[1:, cv2.CC_STAT_AREA] > 12) & (st[1:, cv2.CC_STAT_HEIGHT] < REF * 0.05)]
    char_h = float(np.median(hs)) if len(hs) else 20.0
    if o["binarize"] == "otsu":
        _, g = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    elif o["binarize"] == "adaptive":
        blk = int(max(15, char_h * 2)) | 1
        ad = cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, blk, 15)
        # safeguard: a pixel is ink only if it is also reasonably dark overall, so paper grain never becomes ink
        t, _ = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        g = np.where((ad == 0) & (g < min(250, t + 25)), 0, 255).astype(np.uint8)
    if o["despeckle"]:                          # drop isolated dots smaller than a period
        inv = (g < 150).astype(np.uint8)
        n, lab, st, _ = cv2.connectedComponentsWithStats(inv, connectivity=8)
        lim = max(2, int(REF / 3000)) if o["despeckle"] == 1 else max(6, int(REF / 1200), int(char_h * char_h * 0.02))
        tiny = np.where(st[:, cv2.CC_STAT_AREA] <= lim)[0]
        tiny = tiny[tiny > 0]
        if len(tiny):
            g[np.isin(lab, tiny)] = 255
    if o["remove_lines"] and not local:
        # strip long ruling lines (grid paper, heavy rulings). Opt-in only: on normal drawings it clips
        # characters that touch boxes and leader lines, and the recogniser copes with lines by itself.
        hl, vl = line_masks(g, int(max(REF * 0.03, 8.0 * char_h)))
        g[cv2.bitwise_or(hl, vl) > 0] = 255
    if o["thicken"]:                            # fatten thin / broken strokes
        g = cv2.erode(g, np.ones((2, 2), np.uint8), iterations=1)
    if o["sharpen"] and o["binarize"] == "off":
        blur = cv2.GaussianBlur(g, (0, 0), 1.2)
        g = cv2.addWeighted(g, 1.6, blur, -0.6, 0)
    out = Image.fromarray(g).convert("RGB")

    def back(x, y, M=M_inv, sc=sc):
        if M is not None:
            x, y = (M[0, 0] * x + M[0, 1] * y + M[0, 2], M[1, 0] * x + M[1, 1] * y + M[1, 2])
        return x / sc, y / sc
    return out, back


def ocr_drawing(im, progress: Optional[Callable[[float], None]] = None, rotations: bool = True) -> list[tuple]:
    """Full drawing OCR: cleanup + normal pass + 90°/270° passes for vertical dimensions.
    Returns [(x0, y0, x1, y1, text, conf, rot)] in ORIGINAL image pixels; rot = 0 / 90 / 270."""
    import re
    from PIL.Image import Transpose as _T
    clean, back = clean_for_ocr(im)
    W, H = clean.size
    passes = [0, 90, 270] if rotations else [0]
    out = []
    for i, rot in enumerate(passes):
        def prog(f, i=i):
            if progress:
                progress((i + f) / len(passes))
        if rot == 0:
            img = clean
        elif rot == 90:
            img = clean.transpose(_T.ROTATE_270)      # 90° clockwise: bottom-to-top text becomes readable
        else:
            img = clean.transpose(_T.ROTATE_90)       # 90° counter-clockwise: top-to-bottom text
        for a, b, c, d, t, k in ocr_image(img, progress=prog):
            if rot == 0:
                pts = [(a, b), (c, d)]
            elif rot == 90:                          # rotated (x', y') -> clean (y', H-1-x')
                pts = [(b, H - 1 - a), (d, H - 1 - c)]
            else:                                    # rotated (x', y') -> clean (W-1-y', x')
                pts = [(W - 1 - b, a), (W - 1 - d, c)]
            pts = [back(x, y) for x, y in pts]
            x0, x1 = sorted([pts[0][0], pts[1][0]])
            y0, y1 = sorted([pts[0][1], pts[1][1]])
            if rot and (not re.search(r"\d", t) or (y1 - y0) < 1.3 * (x1 - x0)):
                continue                             # rotated passes only add genuinely vertical callouts
            out.append((x0, y0, x1, y1, t, k, rot))
    # merge: wide horizontal reads win; for vertical callouts the dedicated rotated read wins
    def ov(a, b):
        return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])
    wide = [o for o in out if o[6] == 0 and (o[2] - o[0]) >= 0.8 * (o[3] - o[1])]
    tall0 = [o for o in out if o[6] == 0 and (o[2] - o[0]) < 0.8 * (o[3] - o[1])]
    rot = sorted([o for o in out if o[6]], key=lambda o: -o[5])
    keep_rot = []
    for o in rot:
        cx, cy = (o[0] + o[2]) / 2, (o[1] + o[3]) / 2
        if any(w[0] <= cx <= w[2] and w[1] <= cy <= w[3] for w in wide):
            continue
        if any(ov(o, k) for k in keep_rot):
            continue                                  # 90 vs 270 duplicate: keep the more confident one
        keep_rot.append(o)
    wide = [w for w in wide if not (len(w[4].strip()) <= 4 and any(
        r[0] <= (w[0] + w[2]) / 2 <= r[2] and r[1] <= (w[1] + w[3]) / 2 <= r[3] for r in keep_rot))]
    keep = wide + keep_rot + [t for t in tall0 if not any(ov(t, r) for r in keep_rot)]
    return keep


def ocr_image(im, tile: int = 1600, overlap: int = 220,
              progress: Optional[Callable[[float], None]] = None) -> list[tuple]:
    """im: PIL image. Returns [(x0, y0, x1, y1, text, conf)] in image pixels."""
    eng = engine_name()
    if eng == "RapidOCR":
        return _rapid_ocr(im, tile, overlap, progress)
    if eng == "Tesseract":
        return _tess_ocr(im)
    return []


def _rapid_ocr(im, tile, overlap, progress):
    global _rapid
    import numpy as np
    kind = _rapid_kind()
    if _rapid is None:
        import logging
        logging.getLogger("RapidOCR").setLevel(logging.WARNING)
        if kind == "v3":
            from rapidocr import RapidOCR
        else:
            from rapidocr_onnxruntime import RapidOCR
        _rapid = RapidOCR()
    arr = np.array(im.convert("RGB"))
    H, W = arr.shape[:2]
    out = []
    ys = list(range(0, max(H - overlap, 1), tile - overlap))
    xs = list(range(0, max(W - overlap, 1), tile - overlap))
    n, k = len(ys) * len(xs), 0
    for y in ys:
        for x in xs:
            if kind == "v3":
                r = _rapid(arr[y:y + tile, x:x + tile])
                res = list(zip(r.boxes, r.txts, r.scores)) if getattr(r, "boxes", None) is not None else []
            else:
                res, _ = _rapid(arr[y:y + tile, x:x + tile])
            for box, txt, conf in (res or []):
                bx = [p[0] for p in box]
                by = [p[1] for p in box]
                out.append((min(bx) + x, min(by) + y, max(bx) + x, max(by) + y, str(txt), float(conf)))
            k += 1
            if progress:
                progress(k / n)
    return _dedupe(out)


def _dedupe(items):
    """Remove duplicates produced by overlapping tiles (keep the longest / most confident)."""
    items = sorted(items, key=lambda t: (-(len(t[4])), -t[5]))
    kept = []
    for it in items:
        x0, y0, x1, y1 = it[:4]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        dup = False
        for k in kept:
            if k[0] - 2 <= cx <= k[2] + 2 and k[1] - 2 <= cy <= k[3] + 2:
                a = it[4].replace(" ", "")
                b = k[4].replace(" ", "")
                if a in b or b in a:
                    dup = True
                    break
        if not dup:
            kept.append(it)
    return kept


def _tess_ocr(im):
    import pytesseract
    exe = _win_tesseract()
    if exe:
        pytesseract.pytesseract.tesseract_cmd = exe
    d = pytesseract.image_to_data(im, config="--psm 11", output_type=pytesseract.Output.DICT)
    lines: dict = {}
    for i, t in enumerate(d["text"]):
        t = (t or "").strip()
        if not t:
            continue
        key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
        x, y, w, h = d["left"][i], d["top"][i], d["width"][i], d["height"][i]
        lines.setdefault(key, []).append((x, y, x + w, y + h, t, float(d["conf"][i]) / 100))
    out = []
    for ws in lines.values():
        ws.sort(key=lambda w: w[0])
        # split a tesseract "line" where there are big gaps (sparse text mode joins far-apart words)
        cur = [ws[0]]
        for w in ws[1:]:
            h = max(1, w[3] - w[1])
            if w[0] - cur[-1][2] > 2.5 * h:
                out.append(_merge(cur))
                cur = [w]
            else:
                cur.append(w)
        out.append(_merge(cur))
    return out


def _merge(ws):
    return (min(w[0] for w in ws), min(w[1] for w in ws), max(w[2] for w in ws), max(w[3] for w in ws),
            " ".join(w[4] for w in ws), sum(w[5] for w in ws) / len(ws))


def ocr_pdf_page(data_hash: str, page, dpi: int = 150,
                 progress: Optional[Callable[[float], None]] = None) -> list[tuple]:
    """OCR a PyMuPDF page. Returns [(x0,y0,x1,y1,text,conf)] in PAGE POINTS (visible orientation)."""
    eng = engine_name()
    if not eng:
        return []
    key = _cache_key(data_hash, page.number, dpi, eng + "pdf")
    cf = cache_dir() / f"ocr_{key}.json"
    if cf.exists():
        try:
            return [tuple(x) for x in json.loads(cf.read_text())]
        except Exception:
            pass
    from PIL import Image
    # aim for ~6600 px on the long side: small (letter-size) prints of big drawings need a high DPI,
    # E-size sheets must not explode in memory
    long_in = max(page.rect.width, page.rect.height) / 72.0
    dpi = int(max(dpi, min(600, 6600 / max(long_in, 1))))
    dpi = int(min(dpi, 9000 / max(long_in, 1)))
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    im = Image.open(io.BytesIO(pix.tobytes("png")))
    s = 72.0 / dpi
    res = [(a * s, b * s, c * s, d * s, t, cf_, r) for a, b, c, d, t, cf_, r in ocr_drawing(im, progress=progress)]
    try:
        cf.write_text(json.dumps(res))
    except Exception:
        pass
    return res


def ocr_pil_page(data_hash: str, im, page_no: int, dpi_hint: int = 150, progress=None) -> list[tuple]:
    """OCR a PIL image (TIFF/JPEG drawing). Returns boxes in image pixels."""
    eng = engine_name()
    if not eng:
        return []
    key = _cache_key(data_hash, page_no, dpi_hint, eng + "img")
    cf = cache_dir() / f"ocr_{key}.json"
    if cf.exists():
        try:
            return [tuple(x) for x in json.loads(cf.read_text())]
        except Exception:
            pass
    scale = 1.0
    if max(im.size) > 9000:
        scale = 9000 / max(im.size)
        im = im.resize((int(im.size[0] * scale), int(im.size[1] * scale)))
    res = [(a / scale, b / scale, c / scale, d / scale, t, k, r) for a, b, c, d, t, k, r in ocr_drawing(im, progress=progress)]
    try:
        cf.write_text(json.dumps(res))
    except Exception:
        pass
    return res


def ocr_clip(page_or_image, rect, dpi: int = 400, rotate: int = 0, clean: bool = False) -> list[tuple]:
    """High-resolution OCR of a small region (x0,y0,x1,y1 in page points / image pixels).
    rotate=90 reads bottom-to-top text, 270 top-to-bottom. Boxes are returned in page units."""
    if not available():
        return []
    from PIL import Image
    x0, y0, x1, y1 = rect
    if hasattr(page_or_image, "get_pixmap"):
        import pymupdf
        pix = page_or_image.get_pixmap(dpi=dpi, clip=pymupdf.Rect(x0, y0, x1, y1), alpha=False)
        im = Image.open(io.BytesIO(pix.tobytes("png")))
        s = 72.0 / dpi
    else:
        im = page_or_image.crop((int(x0), int(y0), int(x1), int(y1)))
        f = max(1.0, 60.0 / max(1, min(im.size)))               # upscale small crops of scans
        if f > 1:
            im = im.resize((int(im.size[0] * f), int(im.size[1] * f)), Image.LANCZOS)
        s = 1.0 / f
    if clean:
        im, _ = clean_for_ocr(im, local=True)
    W, H = im.size
    from PIL.Image import Transpose as _T
    if rotate == 90:
        im = im.transpose(_T.ROTATE_270)
    elif rotate == 270:
        im = im.transpose(_T.ROTATE_90)
    res = ocr_image(im, tile=2400, overlap=200)
    out = []
    for a, b, c, d, t, k in res:
        if rotate == 90:
            a, b, c, d = b, H - 1 - c, d, H - 1 - a
        elif rotate == 270:
            a, b, c, d = W - 1 - d, a, W - 1 - b, c
        out.append((x0 + min(a, c) * s, y0 + min(b, d) * s, x0 + max(a, c) * s, y0 + max(b, d) * s, t, k))
    return out
