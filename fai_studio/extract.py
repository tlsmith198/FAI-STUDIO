"""Auto-balloon: find dimension callouts on a drawing page.

Vector PDFs use the embedded text layer. Scanned PDFs / TIFF / JPEG fall back to
Tesseract OCR when it is installed (optional).
"""
from __future__ import annotations

import io
import re
import shutil
from dataclasses import dataclass

import pymupdf

from .docrender import is_pdf, open_image_page

NUM = r"(?:\d+\.\d+|\.\d+)"
TOL = rf"(?:\s*(?:±|\+/-|\+-)\s*{NUM}°?|\s*\+\s*{NUM}\s*/?\s*-\s*{NUM}|\s*-\s*{NUM}\s*/?\s*\+\s*{NUM})"
GDT_RE = re.compile(
    rf"(?P<dim>[⌖⏥⏤○⌭⌒⌓⟂⊥∠∥◎⌯↗⌰\x00\ufffd\ue000-\uf8ff]\s*[Ø⌀]?\s*{NUM}(?:\s*[ⓂⓁⓈⓅⓉⓊ])?(?:\s+[A-Z](?:\s*[ⓂⓁⓈ])?){{0,3}})")
DIM_RE = re.compile(
    rf"(?<![A-Za-z0-9.])(?P<dim>(?:\(?\d+\s*[Xx×]\s*\)?\s*)?(?:Ø|⌀|SØ|R|SR|CR)?\s*{NUM}°?"
    rf"(?:{TOL}|\s*(?:-|–)\s*{NUM}(?!\d)|\s*(?:MAX|MIN|BSC|REF|THRU|DEEP)\b)?)")
STACK_RE = re.compile(rf"^[+-]\s*{NUM}$")
KEEP_WORDS = {"MAX", "MIN", "BSC", "REF", "THRU", "DEEP", "TYP", "PLACES", "PL", "EQ", "SP", "CSK", "CBORE", "DIA",
              "ALL", "AROUND", "OVER"}
SKIP_LINE_RE = re.compile(r"^\s*\d{1,2}\s*[.)]\s+[A-Z]|UNLESS OTHERWISE|TOLERANCES?\b|DO NOT SCALE|SHEET\s+\d|"
                          r"\bREV\b|DWG\s*NO|\bSCALE\b|\bDATE\b|\bNOTES?\b", re.I)
ANGLE_RE = re.compile(r"(?<![A-Za-z0-9.])(?P<dim>\d{1,3}(?:\.\d+)?°(?:\s*±\s*\d+(?:\.\d+)?°?)?)")


@dataclass
class Candidate:
    page: int
    requirement: str
    x: float          # normalized balloon position 0..1
    y: float
    bbox: tuple       # normalized bbox of the callout
    source: str = "text"
    selected: bool = True


def _words_pdf(data: bytes, page: int):
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        pg = doc[page]
        W, H = pg.rect.width, pg.rect.height          # visible (rotated) size — what the viewer shows
        mat = pg.rotation_matrix                       # unrotated text coords -> visible coords
        out = []
        for w in pg.get_text("words"):
            r = pymupdf.Rect(w[:4]) * mat if pg.rotation else pymupdf.Rect(w[:4])
            out.append((r.x0 / W, r.y0 / H, r.x1 / W, r.y1 / H, w[4], (w[5], w[6])))
        return out, W, H


def ocr_available() -> bool:
    try:
        import pytesseract  # noqa: F401
    except Exception:
        return False
    return shutil.which("tesseract") is not None or _win_tesseract() is not None


def _win_tesseract():
    import os
    for p in (r"C:\Program Files\Tesseract-OCR\tesseract.exe", r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"):
        if os.path.exists(p):
            return p
    return None


def _words_ocr(data: bytes, filename: str, page: int):
    import pytesseract
    exe = _win_tesseract()
    if exe:
        pytesseract.pytesseract.tesseract_cmd = exe
    if is_pdf(filename):
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            pix = doc[page].get_pixmap(dpi=300)
            from PIL import Image
            im = Image.open(io.BytesIO(pix.tobytes("png")))
    else:
        im = open_image_page(data, page)
    W, H = im.size
    d = pytesseract.image_to_data(im, config="--psm 11", output_type=pytesseract.Output.DICT)
    out = []
    for i, t in enumerate(d["text"]):
        t = (t or "").strip()
        if not t:
            continue
        x, y, w, h = d["left"][i], d["top"][i], d["width"][i], d["height"][i]
        key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
        out.append((x / W, y / H, (x + w) / W, (y + h) / H, t, key))
    return out, W, H


def _bbox_union(ws):
    return (min(w[0] for w in ws), min(w[1] for w in ws), max(w[2] for w in ws), max(w[3] for w in ws))


def find_candidates(data: bytes, filename: str, page: int, use_ocr_if_needed: bool = True) -> tuple[list[Candidate], str]:
    source = "text"
    words = []
    if is_pdf(filename):
        words, _, _ = _words_pdf(data, page)
    if len([w for w in words if re.search(r"\d", w[4])]) < 3:
        if use_ocr_if_needed and ocr_available():
            words, _, _ = _words_ocr(data, filename, page)
            source = "ocr"
        elif not words:
            return [], "none"

    # group into lines
    lines: dict = {}
    for w in words:
        lines.setdefault(w[5], []).append(w)
    used_words = set()
    cands: list[Candidate] = []

    for key, ws in lines.items():
        ws.sort(key=lambda w: w[0])
        text = ""
        spans = []
        for w in ws:
            if text:
                text += " "
            start = len(text)
            text += w[4]
            spans.append((start, len(text), w))
        # skip sentence-like lines (general notes, title block text)
        alpha = [t for t in re.findall(r"[A-Za-z]{3,}", text) if t.upper() not in KEEP_WORDS]
        if SKIP_LINE_RE.search(text) or len(alpha) >= 3:
            for sp in spans:
                used_words.add(id(sp[2]))
            continue
        if source == "ocr":
            # OCR usually reads ± as +; a lone +tol right after a nominal is treated as ±
            text = re.sub(rf"({NUM})(\s+)\+(\s*{NUM})(?![\d.])(?!\s*/?\s*-)", lambda m: m.group(1) + m.group(2) + "±" + m.group(3), text)
        for rx in (GDT_RE, DIM_RE, ANGLE_RE):
            for m in rx.finditer(text):
                s, e = m.span("dim")
                req = re.sub(r"[\x00\ufffd\ue000-\uf8ff]", "FCF", m.group("dim")).strip()
                req = re.sub(r"\s+", " ", req)
                if not re.search(r"\d", req):
                    continue
                if s > 0 and text[s - 1] in "+-±/":
                    continue            # tolerance fragment, handled by the stacked-tolerance pass
                if rx is DIM_RE:
                    # value followed only by datum letters -> feature control frame whose symbol didn't extract
                    tail = re.match(r"^\s*(?:[ⓂⓁⓈ\x00\ufffd]\s*)?((?:[A-Z]\b\s*){1,3})$", text[e:])
                    if tail and not re.search(r"MAX|MIN|BSC|REF", req):
                        req = f"FCF {req} {' '.join(tail.group(1).split())}"
                        e = len(text)
                hit = [sp[2] for sp in spans if sp[0] < e and sp[1] > s]
                if not hit or any(id(h) in used_words for h in hit):
                    continue
                for h in hit:
                    used_words.add(id(h))
                cands.append(Candidate(page, req, 0, 0, _bbox_union(hit), source))

    # stacked tolerances (+.005 over -.002 to the right of the nominal)
    stack_words = [w for w in words if STACK_RE.match(w[4].replace(" ", "")) and id(w) not in used_words]
    for c in cands:
        if re.search(r"±|\+|MAX|MIN|BSC|REF|-\s*\d", c.requirement):
            continue
        x0, y0, x1, y1 = c.bbox
        h = y1 - y0
        near = [w for w in stack_words
                if x1 - 0.002 <= w[0] <= x1 + 4 * h and (y0 - h) <= (w[1] + w[3]) / 2 <= (y1 + h) and id(w) not in used_words]
        plus = [w for w in near if w[4].lstrip().startswith("+")]
        minus = [w for w in near if w[4].lstrip().startswith("-")]
        if plus and minus:
            c.requirement = f"{c.requirement} {plus[0][4].replace(' ', '')}/{minus[0][4].replace(' ', '')}"
            used_words.add(id(plus[0]))
            used_words.add(id(minus[0]))
            c.bbox = _bbox_union([c.bbox + ("", None), plus[0], minus[0]])

    # balloon position: just up-right of the callout
    for c in cands:
        x0, y0, x1, y1 = c.bbox
        c.x = min(0.99, x1 + 0.012)
        c.y = max(0.01, y0 - 0.008)
    # reading order: top-to-bottom, left-to-right in bands
    cands.sort(key=lambda c: (round(c.bbox[1] / 0.04), c.bbox[0]))
    return cands, source
