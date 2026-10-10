"""Drawing intelligence: understand an engineering drawing well enough to balloon ONE part.

analyze_drawing(bytes, filename) -> DrawingInfo
    * text layer (vector PDFs) or OCR (scans, outlined-text PDFs, TIFF/JPEG)
    * zone grid, sheet numbers, title block (drawing no / rev / cage / title / default tolerances)
    * parts list on the drawing (find no, part no, nomenclature, sheet/zone, qty-per-assy columns, flag notes)
    * tabulated dash-number tables (DASH NO | A | B | C …)
    * drawing notes + which flag notes apply to which part
    * views: raster segmentation of each sheet + view titles ("003 THREADED ARM", "-501 KIT",
      "SECTION A-A", "DETAIL B", "A / SCALE 2/1") -> which views belong to which part number
info.part_options()                 -> candidate part numbers to balloon
info.balloon(option)                -> characteristics found only in that part's views (tab values substituted)
info.balloon(None, page=, rect=)    -> characteristics inside a user-drawn area
"""
from __future__ import annotations

import hashlib
import io
import math
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Callable, Optional

import pymupdf

from .docrender import is_image, is_pdf, open_image_page

# ----------------------------------------------------------------------------------------------- text
NUM = r"(?:\d+\.\d+|\.\d+)"
SNUM = r"[+-]\s*(?:\d+\.\d*|\.\d+|\d+)"
GDT_CHARS = "⌖⏥⏤○⌭⌒⌓⟂⊥⏊∠∥⫽◎⌯↗⌰"
ZONE_RE = re.compile(r"^[A-HJ-NP-Z]\d{1,2}$")
THREAD_RE = re.compile(r"(?:#\d+|\d+/\d+|\d*\.\d+|\d+|M\d+(?:\.\d+)?)\s*-\s*\d+(?:\.\d+)?\s*(?:UNC|UNF|UNEF|UNJC|UNJF|UNJ|UNS|UN|NPTF|NPT|STI)"
                       r"|\bM\d+(?:\.\d+)?\s*[Xx×]\s*\d*\.\d+", re.I)
DIM_IN_LINE = re.compile(
    rf"(?<![A-Za-z0-9.])(?P<d>(?:\(?\d+\s*[Xx×]\s*\)?\s*)?(?:Ø|SØ|R|SR|CR)?\s*{NUM}°?"
    rf"(?:\s*±\s*{NUM}°?|\s*\+\s*{NUM}\s*/?\s*-\s*{NUM}|\s*-\s*{NUM}\s*/?\s*\+\s*{NUM}"
    rf"|\s*[-–]\s*{NUM}(?!\d)|\s*(?:MAX|MIN|BSC|REF|THRU|DEEP|HEX|TYP)\b)?)")
NUM_M = r"(?:\d+\.\d+|\.\d+|\d+)"          # metric drawings: whole numbers are real dimensions ("10 +0.13/-0.01")


def _dim_in_line(num: str):
    return re.compile(
        rf"(?<![A-Za-z0-9.])(?P<d>(?:\(?\d+\s*[Xx×]\s*\)?\s*)?(?:Ø|SØ|R|SR|CR|↧)?\s*{num}°?"
        rf"(?:\s*±\s*{num}°?|\s*\+\s*{num}\s*/?\s*-\s*{num}|\s*-\s*{num}\s*/?\s*\+\s*{num}"
        rf"|\s*[-–]\s*{num}(?!\d)|\s*(?:MAX|MIN|BSC|REF|THRU|DEEP|HEX|TYP)\b)?)(?![\dA-Za-z])")


DIM_IN_LINE_M = _dim_in_line(NUM_M)
ANGLE_IN_LINE = re.compile(r"(?<![A-Za-z0-9.])(?P<d>\d{1,3}(?:\.\d+)?°(?:\s*±\s*\d*\.?\d+°?)?)")
TITLE_SECT = re.compile(r"^(SECTION|DETAIL|VIEW)\s+([A-Z]{1,2})(?:\s*[-.–]\s*([A-Z]{1,2}))?\b", re.I)
TITLE_FIND = re.compile(r"^\(?(\d{3})\)?\s+([A-Z][A-Z0-9 ,&/\-\.'()]+)$")
TITLE_DASH = re.compile(r"^-(\d{1,4}[A-Z]?)(?:\s+([A-Z][A-Z0-9 ,&/\-\.'()]+))?$")
SCALE_RE = re.compile(r"^SCALE\s*:?\s*(\d+(?:\.\d+)?\s*[/:]\s*\d+(?:\.\d+)?|NONE|NOTED|FULL|\d+X)", re.I)
TB_WORDS = re.compile(r"DWG\.?\s*NO|DRAWING\s*NO|CAGE|SCALE|SHEET|\bSIZE\b|TITLE|UNLESS\s+OTHERWISE|TOLERANC|"
                      r"DIMENSIONS\s+ARE|FRACTION|ANGLE|INTERPRET|PROJECTION|CONTR\b|CONTR:|CONTRACT|DWN|DRAWN|CHECK|APPROV|"
                      r"RESP\s+ENGR|DATE|FINISH|NEXT\s+ASS|USED\s+ON|APPLICATION|RELEASE|HARD\s+COPY|"
                      r"PLACE\s+DEC|\d\s*PL\b|DESIGN\s+ACTIVITY|FSCM|CODE\s+IDENT|DSR|STRESS|ENGR|^METRIC$|DO\s+NOT\s+SCALE|"
                      r"^REV$|^D$|MANUFACTURING|ANALYSIS|THIRD\s+ANGLE|ROCKET\s+ROAD|HAWTHORNE|WWW\.", re.I)
BANNER_RE = re.compile(r"EXPORT\s+CONTROL|PROPRIETARY|EXPORT\s+LICENSE|DOCUMENT\s+(NUMBER|CATEGORY|ID)|"
                       r"NOT\s+VALID\s+AFTER|USER\s+IS\s+RESPONSIBLE|EPDM|STATE:\s*RELEASED|DISTRIBUTION\s+STATEMENT|"
                       r"WARNING\s*-|DESTRUCTION\s+NOTICE", re.I)
COMPANY_RE = re.compile(r"LOCKHEED|BOEING|DOUGLAS|MARTIN|NORTHROP|RAYTHEON|COMPANY|AIRCRAFT|CALIFORNIA|INC\.?$", re.I)


def ocr_fix(t: str) -> str:
    """Correct the usual OCR misreads on engineering drawings, using drawing context."""
    # degree sign read as a lower-case o / º after an angle ("45o ±1o") - must run before O->0
    t = re.sub(r"(?<![\d.])(\d{1,3}(?:\.\d{1,2})?)[oº](?=\s|$|±|/|\))", r"\1°", t)
    # letters inside numbers: O->0, S->5, l/I->1, B->8 (only when surrounded by digits / decimal point)
    for _ in range(4):                               # repeat so runs like "4.5OO" are fully fixed
        prev = t
        t = re.sub(r"(?<=[R.\d])[Oo](?=[\dOo])", "0", t)
        t = re.sub(r"(?<=[.\d])[Oo](?=[\d\s/±)]|$)", "0", t)
        t = re.sub(r"(?<=[\d.])[Ss](?=\d)", "5", t)
        t = re.sub(r"(?<=\d)[Ss](?=[.\d])", "5", t)
        t = re.sub(r"(?<=[\d.])[lI|](?=\d)", "1", t)
        t = re.sub(r"(?<=\d)[lI|](?=[.\d])", "1", t)
        t = re.sub(r"(?<=\.\d)B(?=\d)|(?<=\d)B(?=\d\b)", "8", t)
        if t == prev:
            break
    t = t.replace(",", ".") if re.fullmatch(r"[\d.,±+\-/ Ø]+", t) else t
    # diameter symbol read as 0 / O / "(/)" / "Q"
    t = re.sub(r"^[0OQ]\s+(?=\.\d)", "Ø", t)                     # "0 .375" -> "Ø.375"
    t = re.sub(r"(?<=\s)[0OQ]\s+(?=\.\d)", "Ø", t)
    t = re.sub(r"(?<![\w.])(?:\(/\)|\(\))\s*(?=\.?\d)", "Ø", t)
    t = re.sub(r"(?<=\dX)\s*[0OQ](?=\.\d)", " Ø", t)              # "2X0.500" -> "2X Ø.500"
    # ± read as +, t, £, ‡ or ±± between a value and its tolerance
    t = re.sub(r"(?<=[\d°])\s*[t£‡]\s*(?=\.?\d)", " ±", t)
    t = re.sub(rf"({NUM}°?)\s*\+\s*({NUM}°?)(?![\d.])(?!\s*/?\s*-)", r"\1 ±\2", t)
    t = re.sub(r"(?<=°)\s*\+\s*(?=\d)", " ±", t)
    t = re.sub(r"±\s*±", "±", t)
    # degree sign read as o / ° / º after an angle
    t = re.sub(r"(?<=\d)[oº](?=\s|$|±)", "°", t)
    # "X" count read as x / *
    t = re.sub(r"^(\d+)\s*[x*]\s", r"\1X ", t)
    return t


def norm_text(t: str, ocr: bool = False) -> str:
    t = t.replace("⌀", "Ø").replace("Φ", "Ø").replace("φ", "Ø").replace("∅", "Ø").replace("ø", "Ø")
    t = t.replace("+/-", "±").replace("+-", "±").replace("×", "X").replace("–", "-").replace("−", "-")
    t = t.replace("\x98", "(").replace("\x99", ")").replace("⌴", "⌴")     # SpaceX / NX CGM glyphs for ( )
    t = re.sub(r"\s+", " ", t).strip()
    if ocr:
        t = ocr_fix(t)
    t = re.sub(r"Ø\s+(?=[\d.])", "Ø", t)
    t = re.sub(r"\bR\s+(?=\.\d)", "R", t)
    t = re.sub(r"\s*±\s*", " ±", t)
    t = re.sub(r"\(\s+", "(", t)
    t = re.sub(r"\s+\)", ")", t)
    t = re.sub(r"(?<=\d)\s+(?=[+-]\.?\d)", " ", t)
    return t.strip()


@dataclass
class TL:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    vertical: bool = False
    size: float = 10.0
    src: str = "text"
    used: bool = False
    rot: int = 0                    # vertical text: 90 = reads bottom-to-top, 270 = top-to-bottom
    conf: float = 1.0               # OCR confidence (1.0 for PDF text)

    @property
    def h(self):
        return max(1.0, (self.x1 - self.x0) if self.vertical else (self.y1 - self.y0))

    @property
    def cx(self):
        return (self.x0 + self.x1) / 2

    @property
    def cy(self):
        return (self.y0 + self.y1) / 2

    def inside(self, r, pad=0.0):
        return r[0] - pad <= self.cx <= r[2] + pad and r[1] - pad <= self.cy <= r[3] + pad


def _union(rs):
    rs = [r for r in rs if r]
    if not rs:
        return None
    return (min(r[0] for r in rs), min(r[1] for r in rs), max(r[2] for r in rs), max(r[3] for r in rs))


def _grow(r, d):
    return (r[0] - d, r[1] - d, r[2] + d, r[3] + d)


def _overlap(a, b):
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def _rect_dist(a, b):
    dx = max(0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0, max(a[1], b[1]) - min(a[3], b[3]))
    return (dx * dx + dy * dy) ** 0.5


# ----------------------------------------------------------------------------------------------- data
@dataclass
class View:
    page: int
    title: str
    kind: str                       # main | section | detail | view | untitled
    rect: tuple
    find_no: str = ""
    dash: str = ""
    name: str = ""
    label: str = ""                 # A-A / B
    ref: Optional[tuple] = None     # (sheet_no or None, zone)
    reference_only: bool = False
    comps: list = field(default_factory=list)
    parts: set = field(default_factory=set)
    title_rect: Optional[tuple] = None


@dataclass
class PartEntry:
    part_number: str
    name: str = ""
    find_no: str = ""
    dash: str = ""
    sheet: str = ""
    zone: str = ""
    material: str = ""
    size: str = ""
    cage: str = ""
    qty_total: str = ""
    qty_by: dict = field(default_factory=dict)
    flags: list = field(default_factory=list)
    source: str = "parts list"


@dataclass
class PartOption:
    part_number: str
    name: str
    kind: str                       # Detail | Assembly | Tabulated | Drawing
    dash: str = ""
    find_no: str = ""
    views: list = field(default_factory=list)
    sheets: list = field(default_factory=list)
    entry: Optional[PartEntry] = None
    note: str = ""

    @property
    def label(self):
        return f"{self.part_number}  {self.name}".strip()


@dataclass
class Candidate:
    page: int
    requirement: str
    bbox: tuple                     # page points
    x: float = 0.0                  # balloon position, normalised 0..1
    y: float = 0.0
    char_type: str = "Dimension"
    designator: str = ""
    ref_location: str = ""
    view: str = ""
    comment: str = ""
    source: str = "text"
    selected: bool = True
    bsize: float = 0.0              # balloon radius / long side of the sheet (0 = standard size)
    char_no: Optional[int] = None   # customer's own balloon number (pre-ballooned drawings)
    nbbox: tuple = ()               # callout box normalised 0..1 (for review thumbnails)
    vertical: bool = False
    rot: int = 0


class Sheet:
    def __init__(self, index: int, W: float, H: float):
        self.index = index
        self.W, self.H = W, H
        self.lines: list[TL] = []
        self.hsegs: list[tuple] = []
        self.vsegs: list[tuple] = []
        self.src = "text"
        self.kind = "sheet"
        self.sheet_no: Optional[int] = None
        self.cols: list[tuple] = []     # (label, x0, x1)
        self.rows: list[tuple] = []     # (label, y0, y1)
        self.frame = (0, 0, W, H)
        self.excl: list[tuple] = []
        self.tb_rect = None
        self.notes_rect = None
        self.pl_rect = None
        self.tab_rect = None
        self.views: list[View] = []
        self.comps: list[tuple] = []
        self.med = 10.0
        self._segmented = False

    # zones --------------------------------------------------------------------------------
    def zone_at(self, x, y) -> str:
        c = next((lab for lab, a, b in self.cols if a <= x < b), None)
        r = next((lab for lab, a, b in self.rows if a <= y < b), None)
        return f"{r}{c}" if c and r else ""

    def zone_center(self, zone):
        m = re.match(r"^([A-Z])(\d{1,2})$", zone or "")
        if not m:
            return None
        c = next(((a + b) / 2 for lab, a, b in self.cols if lab == m.group(2)), None)
        r = next(((a + b) / 2 for lab, a, b in self.rows if lab == m.group(1)), None)
        return (c, r) if c is not None and r is not None else None

    def excluded(self, l: TL) -> bool:
        if not l.inside(self.frame, pad=-2):
            return True
        return any(l.inside(r) for r in self.excl)


# ----------------------------------------------------------------------------------------------- loading
def _pdf_lines(page) -> list[TL]:
    out = []
    mat = page.rotation_matrix if page.rotation else None
    d = page.get_text("dict")
    for b in d["blocks"]:
        for l in b.get("lines", []):
            spans = [s for s in l["spans"] if s["text"].strip()]
            if not spans:
                continue
            text = norm_text(" ".join(s["text"] for s in spans))
            if not text:
                continue
            r = pymupdf.Rect(l["bbox"])
            if mat is not None:
                r = r * mat
            dx, dy = l["dir"]
            vertical = abs(dx) < 0.5
            if mat is not None and page.rotation in (90, 270):
                vertical = not vertical
            rot = (270 if dy > 0 else 90) if vertical and not page.rotation else (90 if vertical else 0)
            out.append(TL(text, r.x0, r.y0, r.x1, r.y1, vertical, max(s["size"] for s in spans), rot=rot))
    # de-duplicate (CAD exports often draw text twice)
    seen, uniq = set(), []
    for t in out:
        k = (t.text, round(t.x0), round(t.y0))
        if k not in seen:
            seen.add(k)
            uniq.append(t)
    return uniq


def _pdf_segments(page, W, H, limit=120000):
    hs, vs = [], []
    try:
        drawings = page.get_drawings()
    except Exception:
        return hs, vs
    mat = page.rotation_matrix if page.rotation else None
    maxlen = 4.5 * 72
    n = 0
    for dr in drawings:
        for it in dr["items"]:
            n += 1
            if n > limit:
                return hs, vs
            if it[0] == "l":
                p1, p2 = it[1], it[2]
                if mat is not None:
                    p1, p2 = p1 * mat, p2 * mat
                if abs(p1.y - p2.y) < 0.6 and 0 < abs(p1.x - p2.x) < maxlen:
                    hs.append((min(p1.x, p2.x), p1.y, max(p1.x, p2.x)))
                elif abs(p1.x - p2.x) < 0.6 and 0 < abs(p1.y - p2.y) < maxlen:
                    vs.append((p1.x, min(p1.y, p2.y), max(p1.y, p2.y)))
            elif it[0] == "re":
                r = it[1] * mat if mat is not None else it[1]
                if r.width < maxlen and r.height < maxlen:
                    hs += [(r.x0, r.y0, r.x1), (r.x0, r.y1, r.x1)]
                    vs += [(r.x0, r.y0, r.y1), (r.x1, r.y0, r.y1)]
    return hs, vs


def _raster_segments(im, W, H, px_per_unit_x, px_per_unit_y, char_in: float = 0.06):
    """Horizontal / vertical ruling segments from a scanned image, in page units."""
    try:
        import cv2
        import numpy as np
        from . import ocr as ocrmod
    except Exception:
        return [], []
    g = np.array(im.convert("L"))
    min_len = max(6, int(char_in * 72 * px_per_unit_x))
    hl, vl = ocrmod.line_masks(g, min_len)
    hs, vs = [], []
    maxlen = 4.5 * 72
    for mask, horiz in ((hl, True), (vl, False)):
        n, lab, st, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        for i in range(1, n):
            x, y, w, h, a = st[i]
            if horiz:
                L = w / px_per_unit_x
                if 0 < L < maxlen and h < 12:
                    hs.append((x / px_per_unit_x, (y + h / 2) / px_per_unit_y, (x + w) / px_per_unit_x))
            else:
                L = h / px_per_unit_y
                if 0 < L < maxlen and w < 12:
                    vs.append(((x + w / 2) / px_per_unit_x, y / px_per_unit_y, (y + h) / px_per_unit_y))
    return hs, vs


def _meaningful(lines, W, H):
    return sum(1 for l in lines if re.search(r"\d", l.text) and 0.04 * W < l.cx < 0.96 * W
               and 0.04 * H < l.cy < 0.96 * H and not BANNER_RE.search(l.text))


DIM_TOKEN_RE = re.compile(r"(^|[\sØR(])\.?\d+\.\d+|^\.\d{2,4}\b")


def _dim_lines(lines) -> int:
    """Short text lines that are dimensions (not coordinates 'Z = 43.78', scales '1.5:1' or table cells in prose)."""
    return sum(1 for l in lines if len(l.text.split()) <= 4 and DIM_TOKEN_RE.search(l.text)
               and not re.search(r"=|SCALE|:\d|SHEET|REV", l.text, re.I))


def _stroked_mode(page, s: "Sheet", ptext: str, producer: str, doc_dims: int = 0) -> int:
    """0 = do not OCR the line work, else the OCR dpi. Rules decide first ('always' / 'never' for a scope); without
    a rule, a vector sheet whose text layer holds (almost) no dimensions but lots of line work is OCR'd."""
    from . import rules as _rules
    rs = _rules.active("stroked", ptext, producer)
    if rs:
        r = rs[0]
        return 0 if str(r.p("mode")).lower() == "never" else int(r.p("dpi") or 200)
    dims = _dim_lines(s.lines)
    if dims >= 4 or doc_dims >= 6 or max(s.W, s.H) < 1000:     # letter / A4 pages are lists, not drawings
        return 0
    try:
        n = sum(len(d["items"]) for d in page.get_drawings())
    except Exception:
        return 0
    return 200 if n >= 300 else 0


def _ocr_lines(boxes) -> list[TL]:
    out = []
    for b in boxes:
        x0, y0, x1, y1, t, conf = b[:6]
        rot = b[6] if len(b) > 6 else 0
        t = norm_text(t, ocr=True)
        if not t or conf < 0.35:
            continue
        vertical = bool(rot) or ((y1 - y0) > 2.2 * (x1 - x0) and len(t) > 2)
        out.append(TL(t, x0, y0, x1, y1, vertical, (x1 - x0) if vertical else (y1 - y0), "ocr",
                      rot=(rot or 90) if vertical else 0, conf=float(conf)))
    return out


def load_sheets(data: bytes, filename: str, use_ocr: bool = True,
                progress: Optional[Callable[[str, float], None]] = None) -> list[Sheet]:
    from . import ocr as ocrmod
    sheets = []
    h = hashlib.sha256(data).hexdigest()
    if is_pdf(filename):
        doc = pymupdf.open(stream=data, filetype="pdf")
        from . import rules as _rules
        md = doc.metadata or {}
        producer = f"{md.get('creator') or ''} {md.get('producer') or ''}"
        # dimensions in the whole document's text layer: the automatic line-drawn-text check only runs when
        # the drawing as a whole has (almost) none - notes / parts-list sheets of normal CAD drawings stay fast
        doc_dims = 0
        for pg in doc:
            try:
                doc_dims += _dim_lines(_pdf_lines(pg))
            except Exception:
                pass
            if doc_dims >= 6:
                break
        for pno, page in enumerate(doc):
            W, H = page.rect.width, page.rect.height
            s = Sheet(pno, W, H)
            s.lines = _pdf_lines(page)
            ptext = filename + "\n" + " ".join(l.text for l in s.lines)
            s._rules_text, s._producer = ptext, producer
            reps = _rules.active("replace", ptext, producer)
            if reps:
                for l in s.lines:
                    l.text = _rules.apply_replacements(l.text, reps, ocr=False)
            blank = bool(re.search(r"INTENTIONALLY\s+(LEFT\s+)?BLANK", " ".join(l.text for l in s.lines), re.I))
            try:
                img_area = sum(abs(r.width * r.height) for img in page.get_images(full=True)
                               for r in page.get_image_rects(img[0]))
            except Exception:
                img_area = 0
            scanned = img_area > 0.25 * W * H
            s._hint_drawing = bool(re.search(r"DWG|DRAW", filename, re.I))
            need = _meaningful(s.lines, W, H) < 3 or (scanned and _meaningful(s.lines, W, H) < 8)
            stroked = _stroked_mode(page, s, ptext, producer, doc_dims)
            if stroked and not need and not blank and use_ocr and ocrmod.available():
                # dimension text drawn as lines (CAD PMI / 3D annotation exports): OCR on top of the text layer
                if progress:
                    progress(f"Reading line-drawn text on page {pno + 1} of {doc.page_count}", pno / doc.page_count)
                boxes = ocrmod.ocr_pdf_page(h, page, stroked, None)
                extra = [o for o in _ocr_lines(boxes)
                         if not any(_overlap((o.x0, o.y0, o.x1, o.y1), (l.x0, l.y0, l.x1, l.y1)) for l in s.lines)]
                reps_o = _rules.active("replace", ptext, producer)
                for o in extra:
                    o.text = _rules.apply_replacements(o.text, reps_o, ocr=True)
                s.lines = s.lines + extra
                s.src = "text+ocr"
            if need and not blank and use_ocr and ocrmod.available():
                if progress:
                    progress(f"OCR page {pno + 1} of {doc.page_count} (scanned)", pno / doc.page_count)
                def pr(f, pno=pno):
                    if progress:
                        progress(f"OCR page {pno + 1} of {doc.page_count} (scanned) — reading text in 3 directions",
                                 (pno + f) / doc.page_count)
                boxes = ocrmod.ocr_pdf_page(h, page, 150, pr)
                extra = [o for o in _ocr_lines(boxes)
                         if not any(_overlap((o.x0, o.y0, o.x1, o.y1), (l.x0, l.y0, l.x1, l.y1)) for l in s.lines)]
                reps_o = _rules.active("replace", ptext, producer)
                for o in extra:
                    o.text = _rules.apply_replacements(o.text, reps_o, ocr=True)
                s.lines = s.lines + extra
                s.src = "ocr"
                try:
                    from PIL import Image
                    z = 200 / 72.0
                    pix = page.get_pixmap(matrix=pymupdf.Matrix(z, z), colorspace=pymupdf.csGRAY, alpha=False)
                    s.hsegs, s.vsegs = _raster_segments(Image.open(io.BytesIO(pix.tobytes("png"))), W, H, z, z)
                except Exception:
                    pass
            else:
                s.hsegs, s.vsegs = _pdf_segments(page, W, H)
                if _meaningful(s.lines, W, H) < 1:
                    s.src = "none"
            sheets.append(s)
            if progress:
                progress(f"Reading page {pno + 1} of {doc.page_count}", (pno + 1) / doc.page_count)
        doc.close()
    elif is_image(filename):
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im0:
            n = getattr(im0, "n_frames", 1) or 1
        for pno in range(n):
            im = open_image_page(data, pno)
            W, H = im.size
            s = Sheet(pno, W, H)
            if use_ocr and ocrmod.available():
                if progress:
                    progress(f"OCR image page {pno + 1} of {n}", pno / n)
                s.lines = _ocr_lines(ocrmod.ocr_pil_page(h, im, pno))
                from . import rules as _rules
                s._rules_text, s._producer = filename + "\n" + " ".join(l.text for l in s.lines), ""
                reps = _rules.active("replace", s._rules_text, "")
                for l in s.lines:
                    l.text = _rules.apply_replacements(l.text, reps, ocr=True)
                s.src = "ocr"
                try:
                    s.hsegs, s.vsegs = _raster_segments(im, W, H, 1.0, 1.0, char_in=0.06 * 150 / 72)
                except Exception:
                    pass
            else:
                s.src = "none"
            sheets.append(s)
    return sheets


def render_gray(data: bytes, filename: str, page: int, long_px: int = 1800):
    """Grayscale numpy array of the page + scale factor (pixels per page unit)."""
    import numpy as np
    if is_pdf(filename):
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            pg = doc[page]
            z = long_px / max(pg.rect.width, pg.rect.height)
            pix = pg.get_pixmap(matrix=pymupdf.Matrix(z, z), colorspace=pymupdf.csGRAY, alpha=False)
            arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
            return arr.copy(), z
    im = open_image_page(data, page).convert("L")
    z = long_px / max(im.size)
    im = im.resize((max(1, int(im.size[0] * z)), max(1, int(im.size[1] * z))))
    return np.array(im), z


# ----------------------------------------------------------------------------------------------- per-sheet analysis
def _zones(s: Sheet):
    W, H = s.W, s.H
    tops = [l for l in s.lines if re.fullmatch(r"\d{1,2}", l.text) and not l.vertical
            and (l.y1 < 0.045 * H or l.y0 > 0.955 * H)]
    sides = [l for l in s.lines if re.fullmatch(r"[A-HJ-NP-Z]", l.text) and (l.x1 < 0.035 * W or l.x0 > 0.965 * W)]

    def bands(items, key):
        by = defaultdict(list)
        for l in items:
            by[l.text].append(key(l))
        pts = sorted((statistics.mean(v), k) for k, v in by.items())
        if len(pts) < 3:
            return []
        out = []
        for i, (c, lab) in enumerate(pts):
            lo = (pts[i - 1][0] + c) / 2 if i > 0 else c - (pts[1][0] - pts[0][0]) / 2
            hi = (pts[i + 1][0] + c) / 2 if i < len(pts) - 1 else c + (pts[-1][0] - pts[-2][0]) / 2
            out.append((lab, lo, hi))
        return out
    s.cols = bands(tops, lambda l: l.cx)
    s.rows = bands(sides, lambda l: l.cy)
    fx0 = max([l.x1 for l in sides if l.x1 < 0.035 * W] or [0.025 * W]) + 2
    fx1 = min([l.x0 for l in sides if l.x0 > 0.965 * W] or [0.975 * W]) - 2
    fy0 = max([l.y1 for l in tops if l.y1 < 0.045 * H] or [0.025 * H]) + 2
    fy1 = min([l.y0 for l in tops if l.y0 > 0.955 * H] or [0.975 * H]) - 2
    s.frame = (fx0, fy0, fx1, fy1)


def _sheet_no(s: Sheet):
    for l in s.lines:
        if l.cx < 0.55 * s.W or l.cy < 0.75 * s.H:
            continue
        m = re.search(r"\bSHEET\s*:?\s*(\d{1,3})\b", l.text, re.I) or re.fullmatch(r"(\d{1,3})\s*OF\s*\d{1,3}", l.text, re.I) \
            or re.search(r"\bSH(?:EET)?\.?\s*(\d{1,3})\s*OF\s*\d", l.text, re.I)
        if m:
            s.sheet_no = int(m.group(1))
            return
    for l in s.lines:
        if re.fullmatch(r"SHEET|SH\.?", l.text, re.I) and l.cx > 0.55 * s.W and l.cy > 0.75 * s.H:
            nb = [o for o in s.lines if o is not l and re.fullmatch(r"\d{1,3}", o.text)
                  and abs(o.cy - l.cy) < 1.2 * l.h and 0 < o.x0 - l.x1 < 12 * l.h]
            if nb:
                s.sheet_no = int(min(nb, key=lambda o: o.x0 - l.x1).text)
                return


def _block(s: Sheet, seeds: list[TL], gap: float, xpad: float = 0, down_only=False, x_lock=None):
    r = _union([(l.x0, l.y0, l.x1, l.y1) for l in seeds])
    if not r:
        return None
    changed = True
    while changed:
        changed = False
        for l in s.lines:
            if l.inside(r):
                continue
            if down_only and l.y0 < r[1] - 1:
                continue
            if x_lock and not (x_lock[0] <= l.x0 <= x_lock[1]):
                continue
            if _rect_dist((l.x0, l.y0, l.x1, l.y1), r) < gap and (xpad == 0 or l.x0 >= r[0] - xpad):
                r = _union([r, (l.x0, l.y0, l.x1, l.y1)])
                changed = True
    return r


def _title_block(s: Sheet):
    fx0, fy0, fx1, fy1 = s.frame
    seeds = [l for l in s.lines if TB_WORDS.search(l.text) and l.cx > fx1 - 0.45 * (fx1 - fx0)
             and l.cy > fy1 - 0.22 * (fy1 - fy0) and not BANNER_RE.search(l.text)
             and not re.match(r"^SCALE\s*:?\s*\d", l.text, re.I) and l.size < 1.6 * s.med and not l.vertical
             and not re.search(r"PART\s+OR|IDENTIFYING|NOMENCLATURE|FIND|SHORT NOTES|SPECIFICATION|^ZONE$|^SHEET$|"
                               r"^CAGE$|^CODE$|PARTS\s+LIST|QTY", l.text, re.I)]
    if not seeds:
        return
    r = _union([(l.x0, l.y0, l.x1, l.y1) for l in seeds])
    r = (r[0] - 8, r[1] - 8, s.frame[2] + 4, s.frame[3] + 4)
    s.tb_rect = r
    s.excl.append(r)
    rev = [l for l in s.lines if re.match(r"^REVISIONS?\b|^REVISION HISTORY", l.text, re.I) and l.cy < 0.35 * s.H]
    if rev:
        hdr = min(rev, key=lambda l: l.y0)
        rb = _block(s, [hdr], gap=3 * hdr.h, down_only=True, x_lock=(hdr.x0 - 0.12 * s.W, s.W))
        if rb:
            s.excl.append((min(rb[0], hdr.x0 - 0.12 * s.W), rb[1] - 5, s.frame[2] + 4, rb[3] + 5))
    for l in s.lines:
        if BANNER_RE.search(l.text) or (l.src == "text" and l.size < 0.35 * s.med and not re.search(r"\d\.\d", l.text)):
            s.excl.append((l.x0 - 2, l.y0 - 2, l.x1 + 2, l.y1 + 2))


NOTES_HDR = (r"(GENERAL\s+)?NOTES?(\s*[-,:]?\s*UNLESS\s+OTHERWISE\s+(SPECIFIED|NOTED|STATED))?\s*:?(\s*\(.*\))?"
             r"|(GENERAL\s+)?NOTES?\s+CONT\w*\s+(FROM|ON)\s+(SHEET|SH\.?)\s*\d+\s*:?")
# "NOTES CONTINUED ON SHEET 2" - any spelling (drawings have "CONTINURED", "CONT'D", "CONTD" …)
NOTES_CONT_ON = re.compile(r"\bNOTES?\s+CONT[\w'’]*\.?\s+(ON|TO)\s+(SHEET|SH\.?|NEXT)", re.I)
NOTES_CONT_FROM = re.compile(r"\bNOTES?\s+CONT[\w'’]*\.?\s+FROM\b", re.I)


def _notes_region(s: Sheet):
    hdrs = [l for l in s.lines if re.fullmatch(NOTES_HDR, l.text, re.I)
            and (not s.excluded(l) or NOTES_CONT_FROM.search(l.text))]
    if not hdrs:
        return
    hdr = min(hdrs, key=lambda l: (l.y0, l.x0))
    lines = sorted([l for l in s.lines if l.y0 >= hdr.y0 - 2 and hdr.x0 - 25 <= l.x0 < hdr.x0 + 0.5 * s.W
                    and not l.vertical], key=lambda l: l.y0)
    r = (hdr.x0, hdr.y0, hdr.x1, hdr.y1)
    for l in lines:
        if l is hdr:
            continue
        if l.y0 - r[3] > 4.0 * max(hdr.h, l.h):
            break
        r = _union([r, (l.x0, l.y0, l.x1, l.y1)])
    s.notes_rect = (r[0] - 6, r[1] - 4, r[2] + 6, r[3] + 4)
    s.excl.append(s.notes_rect)


def _classify(s: Sheet):
    W, H = s.W, s.H
    big = max(W, H) >= 15 * 72 or bool(s.cols)
    txt = " ".join(l.text for l in s.lines[:400]).upper()
    head = " ".join(l.text for l in sorted(s.lines, key=lambda l: l.y0)[:25]).upper()
    if not big:
        txt = head
        if "ENGINEERING ORDER" in txt or re.search(r"\bE\.?O\.?\b", txt[:300]):
            s.kind = "eo"
        elif "APPLICATION LIST" in txt:
            s.kind = "application list"
        elif "PARTS LIST" in txt or "PART LIST" in txt:
            s.kind = "parts list"
        elif "NOTES LIST" in txt or "NOTE LIST" in txt:
            s.kind = "notes list"
        elif s.src == "ocr" or getattr(s, "_hint_drawing", False):
            s.kind = "sheet"                       # scanned drawing sheet printed small
        else:
            s.kind = "document"
    else:
        s.kind = "sheet"
        if "INTENTIONALLY LEFT BLANK" in txt and len(s.lines) < 80:
            s.kind = "blank"


def _parts_list(s: Sheet, info: "DrawingInfo"):
    anchors = [l for l in s.lines if re.search(r"IDENTIFYING|PART\s+OR\b|PART\s+NUMBER|PART\s+NO\b", l.text, re.I)]
    for a in anchors:
        mates = [l for l in s.lines if re.search(r"NOMENCLATURE|DESCRIPTION", l.text, re.I) and abs(l.cy - a.cy) < 60]
        if not mates:
            continue
        HDR = re.compile(r"PART|IDENTIFYING|NOMENCLATURE|DESCRIPTION|FIND|^NO\.?$|ZONE|SHEET|^SH$|MATERIAL|"
                         r"SPECIFICATION|CAGE|CODE|UOM|U/M|QTY|TOT|SHORT\s+NOTES|SIZE|ITEM|^REQD", re.I)
        hl = [l for l in s.lines if HDR.search(l.text) and abs(l.cy - a.cy) < 3 * a.h and not l.vertical]
        band_y0 = min(l.y0 for l in hl) - 0.3 * a.h
        band_y1 = max(l.y1 for l in hl) + 0.3 * a.h
        band = [l for l in s.lines if band_y0 <= l.cy <= band_y1 and not l.vertical
                and (HDR.search(l.text) or re.fullmatch(r"\d{3}|-\d{1,4}", l.text) or l in hl)]
        # column header cells: merge vertically-stacked header words
        cells: list[list[TL]] = []
        isnum = lambda t: re.fullmatch(r"\d{3}|-\d{1,4}", t)
        # narrow header words first; a line spanning several columns ("QTY PER ASSY") is a group label
        for l in sorted(band, key=lambda l: (0 if isnum(l.text) else 1, l.x1 - l.x0)):
            hits = [c for c in cells if l.x0 < max(x.x1 for x in c) - 2 and l.x1 > min(x.x0 for x in c) + 2]
            if len(hits) >= 2 or (hits and isnum(hits[0][0].text)):
                continue
            if hits and not isnum(l.text):
                hits[0].append(l)
            else:
                cells.append([l])
        cols = []
        for c in cells:
            t = " ".join(x.text for x in sorted(c, key=lambda x: x.y0)).upper()
            cx = statistics.mean([(x.x0 + x.x1) / 2 for x in c])
            key = None
            if re.fullmatch(r"\d{3}|-\d{1,4}", t):
                key = "q:" + t.lstrip("-")
            elif "FIND" in t or re.fullmatch(r"ITEM( NO)?|NO\.?", t):
                key = "find_no"
            elif "ZONE" in t:
                key = "zone"
            elif "SHEET" in t or t == "SH":
                key = "sheet"
            elif "NOMENCLATURE" in t or "DESCRIPTION" in t:
                key = "name"
            elif "IDENTIFYING" in t or "PART OR" in t or "PART NUMBER" in t or "PART NO" in t:
                key = "pn"
            elif "SHORT NOTES" in t or "MATERIAL SIZE" in t or "SIZE" in t:
                key = "size"
            elif "MATERIAL" in t or "SPECIFICATION" in t:
                key = "material"
            elif "CAGE" in t or "CODE IDENT" in t or "FSCM" in t:
                key = "cage"
            elif "UOM" in t or "U/M" in t:
                key = "uom"
            elif "TOT" in t or t.startswith("QTY") or "REQD" in t:
                key = "qty"
            if key and not (key == "q:" and False):
                cols.append((cx, key, min(x.x0 for x in c), max(x.x1 for x in c)))
        keys = {k for _, k, _, _ in cols}
        if "pn" not in keys:
            continue
        cols.sort()
        bounds = []
        for i, (cx, key, x0, x1) in enumerate(cols):
            lo = (cols[i - 1][0] + cx) / 2 if i else x0 - 40
            hi = (cols[i + 1][0] + cx) / 2 if i < len(cols) - 1 else x1 + 40
            bounds.append((key, lo, hi))
        tx0, tx1 = bounds[0][1], bounds[-1][2]
        pn_b = next(b for b in bounds if b[0] == "pn")
        header_y0 = band_y0
        header_y1 = band_y1
        body = [l for l in s.lines if tx0 <= l.cx <= tx1 and not l.vertical and l not in band]
        pn_lines = [l for l in body if pn_b[1] <= l.cx <= pn_b[2] and re.search(r"\d", l.text)
                    and re.fullmatch(r"[A-Z0-9#][A-Z0-9\-/\.#]{2,}(\s+[A-Z0-9\-/\.#]+){0,2}", l.text.upper())
                    and not re.search(r"PARTS\s+LIST|CONTINUED|\bOF\b", l.text, re.I)]
        above = sorted([l for l in pn_lines if l.cy < header_y0], key=lambda l: -l.cy)
        below = sorted([l for l in pn_lines if l.cy > header_y1], key=lambda l: l.cy)
        dn = info.drawing_no.upper()

        def score(seq_):
            return (sum(1 for l in seq_[:40] if dn and dn in l.text.upper()), len(seq_))
        seq = above if score(above) >= score(below) else below
        if not seq:
            continue
        rows_y = [seq[0].cy]
        ref_y = header_y0 if seq is above else header_y1
        if abs(seq[0].cy - ref_y) > 8 * seq[0].h:
            continue
        for l in seq[1:]:
            pitch = abs(rows_y[-1] - rows_y[-2]) if len(rows_y) > 1 else 3 * l.h
            if abs(l.cy - rows_y[-1]) > max(3.2 * pitch, 6 * l.h):
                break
            if abs(l.cy - rows_y[-1]) > 0.6 * l.h:
                rows_y.append(l.cy)
        pitch = statistics.median([abs(a_ - b_) for a_, b_ in zip(rows_y, rows_y[1:])]) if len(rows_y) > 1 else 30
        y_lo, y_hi = min(rows_y) - 0.6 * pitch, max(rows_y) + 0.6 * pitch
        rows = {i: defaultdict(list) for i in range(len(rows_y))}
        flags = {i: [] for i in range(len(rows_y))}
        for l in s.lines:
            if l.vertical or not (y_lo <= l.cy <= y_hi) or not l.inside(s.frame, pad=-1):
                continue
            i = min(range(len(rows_y)), key=lambda k: abs(rows_y[k] - l.cy))
            if abs(rows_y[i] - l.cy) > 0.55 * pitch:
                continue
            if tx0 <= l.cx <= tx1:
                key = next((b[0] for b in bounds if b[1] <= l.cx < b[2]), None)
                if key:
                    rows[i][key].append(l)
            elif tx0 - 0.08 * s.W < l.cx < tx0 and re.fullmatch(r"(\d{1,2}[A-Z]?\s*)+", l.text):
                flags[i] += l.text.split()
        s.pl_rect = (tx0 - 0.08 * s.W, min(y_lo, header_y0) - 4, tx1 + 4, max(y_hi, header_y1) + 4)
        s.excl.append(s.pl_rect)
        for i in rows:
            r = rows[i]

            def g(k):
                return " ".join(x.text for x in sorted(r.get(k, []), key=lambda x: (x.y0, x.x0))).strip()
            pn = g("pn")
            if not pn or not re.search(r"\d", pn):
                continue
            e = PartEntry(part_number=pn.replace(" ", ""), name=g("name"), find_no=g("find_no"), sheet=g("sheet"),
                          zone=g("zone"), material=g("material"), size=g("size"), cage=g("cage"),
                          qty_total=g("qty"), flags=sorted(set(flags[i])))
            e.qty_by = {k[2:]: g(k) for k in (b[0] for b in bounds) if k.startswith("q:") and g(k)}
            m = re.search(r"-(\d{1,4}[A-Z]?)$", e.part_number)
            e.dash = m.group(1) if m else ""
            info.parts.setdefault(e.part_number, e)
        break


def _refine_tab_ocr(s: Sheet, info: "DrawingInfo"):
    """Scanned tab blocks are small print: re-read that region at high resolution."""
    if s.src != "ocr":
        return
    heads = [l for l in s.lines if re.fullmatch(r"DASH\s*(NO\.?|NUMBER)?", l.text.strip(), re.I)]
    if not heads:
        return
    from . import ocr as ocrmod
    d = heads[0]
    rect = (max(0, d.x0 - 6 * d.h), max(0, d.y0 - 3 * d.h), min(s.W, d.x1 + 30 * d.h), min(s.H, d.y1 + 25 * d.h))
    try:
        if is_pdf(info.filename):
            with pymupdf.open(stream=info.data, filetype="pdf") as doc:
                boxes = ocrmod.ocr_clip(doc[s.index], rect, 400)
        else:
            boxes = ocrmod.ocr_clip(open_image_page(info.data, s.index), rect)
    except Exception:
        return
    new = _ocr_lines(boxes)
    if len(new) < 3:
        return
    s.lines = [l for l in s.lines if not l.inside(rect)] + [l for l in new if l.inside(rect)]
    for l in s.lines:
        if l.inside(rect) and re.fullmatch(r"[B8]|13|[CcG(]|[A4]", l.text) and l.size > 1.2 * d.size:
            l.text = {"13": "B", "8": "B", "c": "C", "(": "C", "G": "C", "4": "A"}.get(l.text, l.text)


def _tab_table(s: Sheet, info: "DrawingInfo"):
    """Tabulated dash-number block:  DASH NO | B | C  /  -501 | 5.400 | 5.05 …"""
    VAL = re.compile(r"[Ø]?\d*\.\d+|\d+(?:\.\d+)?")
    _refine_tab_ocr(s, info)
    for d in [l for l in s.lines if re.fullmatch(r"DASH\s*(NO\.?|NUMBER)?", l.text.strip(), re.I)]:
        rows = sorted([l for l in s.lines if re.fullmatch(r"-\d{1,4}[A-Z]?", l.text) and abs(l.cx - d.cx) < 4 * d.h
                       and 0 < l.cy - d.cy < 40 * d.h], key=lambda l: l.cy)
        if not rows:
            continue
        keep = [rows[0]]
        for r in rows[1:]:
            pitch = (keep[-1].cy - keep[0].cy) / max(1, len(keep) - 1) if len(keep) > 1 else 4 * r.h
            if r.cy - keep[-1].cy > 3 * max(pitch, 2 * r.h):
                break
            keep.append(r)
        hdr_band = [l for l in s.lines if d.y0 - d.h <= l.cy <= keep[0].y0 and l.x0 > d.x1 - 2]
        rects = [(d.x0, d.y0, d.x1, d.y1)]
        for r in keep:
            cells = sorted([l for l in s.lines if abs(l.cy - r.cy) < 0.8 * r.h and r.x1 < l.x0 < r.x1 + 25 * d.h
                            and VAL.fullmatch(l.text)], key=lambda l: l.x0)
            vals, cell_boxes = {}, []
            for i, c in enumerate(cells):
                hb = [h for h in hdr_band if abs(h.cx - c.cx) < max(3 * c.h, (c.x1 - c.x0))]
                lab = min(hb, key=lambda h: abs(h.cx - c.cx)).text if hb else f"COL {i + 1}"
                lab = lab if re.fullmatch(r"[A-Z]{1,3}", lab) else f"COL {i + 1}"
                vals[lab] = c.text
                cell_boxes.append((lab, c.text, s.index, (c.x0, c.y0, c.x1, c.y1)))
                rects.append((c.x0, c.y0, c.x1, c.y1))
            rects.append((r.x0, r.y0, r.x1, r.y1))
            if vals:
                dash = r.text.lstrip("-")
                info.tab[dash] = vals
                info.tab_cells[dash] = cell_boxes
        if info.tab:
            info.tab_letters |= {k for v in info.tab.values() for k in v}
            s.tab_rect = _grow(_union(rects + [(h.x0, h.y0, h.x1, h.y1) for h in hdr_band]), 4)
            s.excl.append(s.tab_rect)


# ----------------------------------------------------------------------------------------------- views
def _find_titles(s: Sheet) -> list[View]:
    cand = [l for l in s.lines if not l.vertical and not s.excluded(l)]
    views = []
    used = set()
    for l in cand:
        if id(l) in used:
            continue
        t = l.text.strip()
        big = l.size >= 1.25 * s.med
        v = None
        m = TITLE_SECT.match(t)
        if m and (big or len(t) < 22):
            kind = {"SECTION": "section", "DETAIL": "detail", "VIEW": "view"}[m.group(1).upper()]
            lab = m.group(2).upper() + (f"-{m.group(3).upper()}" if m.group(3) else "")
            v = View(s.index, t, kind, (l.x0, l.y0, l.x1, l.y1), label=lab)
        elif big and TITLE_FIND.match(t):
            mm = TITLE_FIND.match(t)
            v = View(s.index, t, "main", (l.x0, l.y0, l.x1, l.y1), find_no=mm.group(1), name=mm.group(2).strip())
        elif TITLE_DASH.match(t) and l.size >= (1.25 if " " in t else 1.6) * s.med:
            mm = TITLE_DASH.match(t)
            v = View(s.index, t, "main", (l.x0, l.y0, l.x1, l.y1), dash=mm.group(1), name=(mm.group(2) or "").strip())
        elif big and re.fullmatch(r"[A-Z][A-Z0-9 ,&/\-.']{2,}", t) and not re.search(r"\d\.\d", t):
            # name line with a 3-digit find-number circle just left of it: "301  WEIGHTED ARM"
            fn = [o for o in cand if re.fullmatch(r"\d{3}", o.text) and abs(o.cy - l.cy) < 0.8 * l.h
                  and 0 <= l.x0 - o.x1 < 4 * l.h]
            below_scale = [o for o in cand if SCALE_RE.match(o.text) and 0 < o.y0 - l.y1 < 2.5 * l.h
                           and abs(o.cx - l.cx) < (l.x1 - l.x0)]
            if fn:
                v = View(s.index, f"{fn[0].text} {t}", "main", _union([(l.x0, l.y0, l.x1, l.y1),
                                                                         (fn[0].x0, fn[0].y0, fn[0].x1, fn[0].y1)]),
                         find_no=fn[0].text, name=t)
                used.add(id(fn[0]))
            elif below_scale:
                v = View(s.index, t, "main", (l.x0, l.y0, l.x1, l.y1), name=t)
        elif SCALE_RE.match(t):
            # Douglas / Boeing: big single letter over "SCALE 2/1" is a section/detail title
            above = [o for o in cand if re.fullmatch(r"[A-Z](\s+\d{1,2})?", o.text) and 0 < l.y0 - o.y1 < 3 * l.h
                     and abs(o.cx - l.cx) < 4 * l.h and o.size >= 1.3 * l.size]
            if above:
                o = above[0]
                v = View(s.index, f"DETAIL {o.text.split()[0]}", "detail", _union([(o.x0, o.y0, o.x1, o.y1),
                                                                                    (l.x0, l.y0, l.x1, l.y1)]),
                         label=o.text.split()[0])
                used.add(id(o))
        if not v:
            continue
        used.add(id(l))
        tr = v.rect
        below = [o for o in cand if 0 <= o.y0 - tr[3] < 3.5 * l.h and abs(o.cx - (tr[0] + tr[2]) / 2) < (tr[2] - tr[0])]
        for o in below:
            if SCALE_RE.match(o.text) or re.search(r"REFERENCE ONLY|ISOMETRIC|TRIMETRIC|VIEW$|CONFIGURATION", o.text, re.I):
                tr = _union([tr, (o.x0, o.y0, o.x1, o.y1)])
                used.add(id(o))
                if re.search(r"REFERENCE ONLY", o.text, re.I):
                    v.reference_only = True
        # sheet/zone flag beside the title ("4 over B8")
        near = [o for o in cand if ZONE_RE.match(o.text) and abs(o.cy - (tr[1] + tr[3]) / 2) < 2.2 * l.h
                and min(abs(o.x0 - tr[2]), abs(tr[0] - o.x1)) < 8 * l.h]
        if near:
            z = min(near, key=lambda o: min(abs(o.x0 - tr[2]), abs(tr[0] - o.x1)))
            sh = [o for o in cand if re.fullmatch(r"\d{1,2}", o.text) and 0 < z.y0 - o.y1 < 1.6 * z.h + 4
                  and abs(o.cx - z.cx) < 2 * z.h]
            v.ref = (int(sh[0].text) if sh else None, z.text)
            tr = _union([tr, (z.x0, z.y0, z.x1, z.y1)] + ([(sh[0].x0, sh[0].y0, sh[0].x1, sh[0].y1)] if sh else []))
        v.title_rect = tr
        v.rect = tr
        views.append(v)
    return views


def _segment(s: Sheet, data: bytes, filename: str):
    if s._segmented:
        return
    s._segmented = True
    try:
        import cv2
        import numpy as np
    except Exception:
        return
    arr, z = render_gray(data, filename, s.index)
    Hp, Wp = arr.shape
    zx = Wp / s.W
    zy = Hp / s.H
    mask = (arr < 170).astype(np.uint8)
    fx0, fy0, fx1, fy1 = s.frame
    keep = np.zeros_like(mask)
    keep[int(fy0 * zy) + 5:int(fy1 * zy) - 5, int(fx0 * zx) + 5:int(fx1 * zx) - 5] = 1
    mask &= keep
    for r in s.excl:
        mask[max(0, int(r[1] * zy)):int(r[3] * zy) + 1, max(0, int(r[0] * zx)):int(r[2] * zx) + 1] = 0
    px_per_in = zx * 72.0 if is_pdf(filename) else zx * 150.0
    k = max(3, int(round(0.26 * px_per_in)))
    dil = cv2.dilate(mask, np.ones((k, k), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(dil, connectivity=8)
    comps = []
    min_side = 0.25 * px_per_in
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if w < min_side and h < min_side:
            continue
        if (w < 0.012 * Wp and h > 0.3 * Hp) or (h < 0.012 * Hp and w > 0.3 * Wp):
            continue                                  # border / frame strips
        comps.append((x / zx, y / zy, (x + w) / zx, (y + h) / zy))
    s.comps = comps


def _assign_views(s: Sheet, data, filename):
    _segment(s, data, filename)
    titles = [v for v in s.views if v.title_rect]
    if not s.comps:
        for v in titles:
            v.comps = [_grow(v.rect, 3 * 72)]
        return
    assigned = defaultdict(list)
    free = []
    for c in s.comps:
        best, bd = None, None
        for vi, v in enumerate(titles):
            tr = v.title_rect
            tcx = (tr[0] + tr[2]) / 2
            # geometry usually sits above its title and overlaps it horizontally
            if c[0] - 0.02 * s.W <= tcx <= c[2] + 0.02 * s.W and c[1] <= tr[1] + 5:
                d = max(0.0, tr[1] - c[3])
                if _overlap(_grow(c, 2), tr):
                    d = 0.0
                if d < 0.12 * s.H and (bd is None or d < bd):
                    best, bd = vi, d
        if best is None:
            free.append(c)
        else:
            assigned[best].append(c)
    for vi, v in enumerate(titles):
        v.comps = assigned.get(vi, [])
    # leftover geometry -> closest view group, agglomeratively (always attach the globally closest piece first,
    # so each view grows outward from its own geometry)
    lim = 0.25 * max(s.W, s.H)

    def factor(c, v):
        tr = v.title_rect
        f = 2.5 if c[1] > tr[3] else 1.0              # geometry sits above its title
        pad = 0.25 * (tr[2] - tr[0])
        if c[0] < tr[2] + pad and c[2] > tr[0] - pad:
            f *= 0.5                                   # same column as the title
        return f

    def dist(c, v):
        return min([_rect_dist(c, x) for x in v.comps + [v.title_rect]]) * factor(c, v)
    best = {i: min(((dist(c, v), vi) for vi, v in enumerate(titles)), default=(1e18, -1)) for i, c in enumerate(free)}
    remaining = set(best)
    while remaining:
        i = min(remaining, key=lambda k: best[k][0])
        d, vi = best[i]
        remaining.discard(i)
        if vi < 0 or d > lim:
            s.views.append(View(s.index, "(untitled view)", "untitled", free[i], comps=[free[i]]))
            continue
        v = titles[vi]
        v.comps.append(free[i])
        for k in remaining:                       # only the grown group can get closer
            nd = _rect_dist(free[k], free[i]) * factor(free[k], v)
            if nd < best[k][0]:
                best[k] = (nd, vi)
    for v in s.views:
        v.rect = _union(v.comps + ([v.title_rect] if v.title_rect else [])) or v.rect


# ----------------------------------------------------------------------------------------------- drawing
class DrawingInfo:
    def __init__(self, data: bytes, filename: str, sheets: list[Sheet]):
        self.data = data
        self.filename = filename
        self.sha = hashlib.sha256(data).hexdigest()
        self.sheets = sheets
        self.parts: dict[str, PartEntry] = {}
        self.tab: dict[str, dict] = {}
        self.tab_letters: set = set()
        self.tab_cells: dict[str, list] = {}
        self.notes: list[dict] = []
        self.flag_locations: dict[str, list] = defaultdict(list)   # flag no -> [(page, x, y)]
        self.boxed_flags: set = set()                              # note numbers drawn in a flag box
        self.drawing_no = ""
        self.rev = ""
        self.cage = ""
        self.title = ""
        self.tolerances: dict = {}
        self.dashes: set = set()
        self.ocr_used = any(s.src == "ocr" for s in sheets)
        alltxt = " ".join(l.text for s in sheets[:3] for l in s.lines[:600]).upper()
        self.inch = not re.search(r"MILLIMET|DIMENSIONS ARE IN MM|\bMM\b", alltxt) or "INCH" in alltxt
        self.no_text = [s.index for s in sheets if s.src == "none" and s.kind == "sheet"]

    # --------------------------------------------------------------------- summary
    @property
    def drawing_sheets(self):
        return [s for s in self.sheets if s.kind == "sheet"]

    def summary(self) -> str:
        kinds = Counter(s.kind for s in self.sheets)
        parts = [f"{n} {k}{'s' if n > 1 else ''}" for k, n in kinds.items()]
        src = " (OCR used on scanned pages — verify values)" if self.ocr_used else ""
        return f"{len(self.sheets)} page(s): " + ", ".join(parts) + src

    # --------------------------------------------------------------------- views mapping
    def ensure_views(self):
        if getattr(self, "_views_done", False):
            return
        self._views_done = True
        for s in self.drawing_sheets:
            _assign_views(s, self.data, self.filename)
        by_sheet = {s.sheet_no or (s.index + 1): s for s in self.drawing_sheets}
        dn = self.drawing_no

        def pn_for_find(fn):
            for e in self.parts.values():
                if e.find_no == fn:
                    return e.part_number
            return f"{dn}-{fn}" if dn else fn

        def pn_for_dash(d):
            for e in self.parts.values():
                if e.dash == d or e.find_no == d:
                    return e.part_number
            return f"{dn}-{d}" if dn else f"-{d}"
        allviews = [v for s in self.drawing_sheets for v in s.views]
        for v in allviews:
            if v.find_no:
                v.parts.add(pn_for_find(v.find_no))
            elif v.dash:
                v.parts.add(pn_for_dash(v.dash))
            elif v.name and v.kind == "main":
                for e in self.parts.values():
                    if e.name and re.sub(r"\W", "", e.name.upper()) == re.sub(r"\W", "", v.name.upper()):
                        v.parts.add(e.part_number)

        def view_at(sheet_no, x, y, page=None):
            s = by_sheet.get(sheet_no) if sheet_no else next((x_ for x_ in self.drawing_sheets if x_.index == page), None)
            if not s:
                return None
            best = None
            for v in s.views:
                for c in v.comps or [v.rect]:
                    if c[0] <= x <= c[2] and c[1] <= y <= c[3]:
                        if best is None or (c[2] - c[0]) * (c[3] - c[1]) < best[1]:
                            best = (v, (c[2] - c[0]) * (c[3] - c[1]))
            if best:
                return best[0]
            # nearest geometry within ~1 zone width
            near, nd = None, None
            for v in s.views:
                for c in v.comps or [v.rect]:
                    d = _rect_dist(c, (x, y, x, y))
                    if nd is None or d < nd:
                        near, nd = v, d
            return near if nd is not None and nd < 0.07 * max(s.W, s.H) else None
        # sections / details inherit the part of the view they are cut from (flag "4 / B8", "SEE DETAIL B")
        for _ in range(3):
            for s in self.drawing_sheets:
                for v in s.views:
                    if v.parts or v.kind not in ("section", "detail", "view"):
                        continue
                    src = None
                    if v.ref:
                        sn, zone = v.ref
                        tgt = by_sheet.get(sn) if sn else s
                        c = tgt.zone_center(zone) if tgt else None
                        if c:
                            src = view_at(tgt.sheet_no or (tgt.index + 1), c[0], c[1], tgt.index)
                    if src is None and v.label:
                        lab = v.label.split("-")[0]
                        for s2 in self.drawing_sheets:
                            for l in s2.lines:
                                if re.search(rf"SEE\s+(DETAIL|SECTION|VIEW)\s+{lab}\b", l.text, re.I):
                                    src = view_at(None, l.cx, l.cy, s2.index)
                                    if src:
                                        break
                    if src is None and v.label:
                        lab = v.label.split("-")[0]
                        hits = [l for l in s.lines if l.text == lab and l.size >= 1.2 * s.med
                                and not l.inside(v.rect, pad=5) and not s.excluded(l)]
                        for l in hits:
                            src = view_at(None, l.cx, l.cy, s.index)
                            if src is not None and src is not v and src.parts:
                                break
                            src = None
                    if src is not None and src is not v:
                        v.parts |= src.parts
        # a detail with no view of its own is drawn inside its parent assembly's view
        titled = {p for v in allviews for p in v.parts}
        for e in self.parts.values():
            if e.part_number in titled or not (dn and e.part_number.upper().startswith(dn.upper())):
                continue
            sn = int(re.sub(r"\D", "", e.sheet.split()[0]) or 0) if e.sheet else 0
            parents = [k for k, q in e.qty_by.items() if q and q != "-" and k not in (e.find_no, e.dash)]
            pv = [v for v in allviews if (v.find_no in parents or v.dash in parents) and v.kind == "main"
                  and (not sn or (self.sheets[v.page].sheet_no or v.page + 1) == sn)]
            if not pv and e.name:
                nm = re.sub(r"\W", "", e.name.upper())
                pv = [v for v in allviews if v.kind == "main" and v.name and
                      re.sub(r"\W", "", v.name.upper()).startswith(nm) and re.search(r"ASSY|ASSEMBLY", v.name.upper())]
            for v in pv:
                v.parts.add(e.part_number)
                for v2 in allviews:            # its sections / views follow the parent view
                    if v2.page == v.page and v2.kind in ("section", "view", "detail") and v.parts & v2.parts:
                        v2.parts.add(e.part_number)
        # parts-list sheet/zone callout -> fallback: the view at that zone
        titled = {p for v in allviews for p in v.parts}
        for e in self.parts.values():
            if e.part_number in titled or not e.sheet or not e.zone:
                continue
            try:
                sn = int(re.sub(r"\D", "", e.sheet) or 0)
            except ValueError:
                continue
            s = by_sheet.get(sn)
            c = s.zone_center(e.zone.split()[0]) if s else None
            if c:
                v = view_at(sn, c[0], c[1], s.index)
                if v:
                    v.parts.add(e.part_number)
        # a sheet with exactly one part-titled view: untitled geometry on it belongs to that part
        for s in self.drawing_sheets:
            owners = {p for v in s.views if v.kind == "main" for p in v.parts}
            if len(owners) == 1:
                for v in s.views:
                    if not v.parts:
                        v.parts |= owners

    # --------------------------------------------------------------------- part options
    def part_options(self) -> list[PartOption]:
        self.ensure_views()
        dn = self.drawing_no
        opts: dict[str, PartOption] = {}

        def add(pn, name="", kind="Detail", dash="", find="", entry=None, note=""):
            pn = pn.strip()
            if not pn:
                return
            o = opts.get(pn)
            if o is None:
                opts[pn] = PartOption(pn, name, kind, dash, find, entry=entry, note=note)
            else:
                o.name = o.name or name
                o.entry = o.entry or entry
                o.find_no = o.find_no or find
                o.dash = o.dash or dash
                if kind in ("Assembly", "Tabulated"):
                    o.kind = kind
        asm_cols = set()
        for e in self.parts.values():
            asm_cols |= {k for k, q in e.qty_by.items() if re.fullmatch(r"\d+", q or "") and k not in (e.find_no, e.dash)}
        for e in self.parts.values():
            own = dn and e.part_number.upper().startswith(dn.upper())
            if not own:
                continue
            isasm = e.find_no in asm_cols or e.dash in asm_cols or re.search(r"\bASSY\b|ASSEMBLY|\bKIT\b", e.name.upper())
            add(e.part_number, e.name, "Assembly" if isasm else "Detail", e.dash, e.find_no, e)
        for d, vals in self.tab.items():
            add(f"{dn}-{d}" if dn else f"-{d}", "", "Tabulated", d, note="tabulated: " + ", ".join(f"{k}={v}" for k, v in vals.items()))
        for d in sorted(self.dashes):
            add(f"{dn}-{d}", "", "Detail", d)
        for s in self.drawing_sheets:
            for v in s.views:
                for p in v.parts:
                    if p not in opts and p.upper().startswith(dn.upper() if dn else p.upper()):
                        add(p, v.name, "Detail", v.dash, v.find_no)
        for o in opts.values():
            vs = [v for s in self.drawing_sheets for v in s.views if o.part_number in v.parts]
            o.views = vs
            o.sheets = sorted({(self.sheets[v.page].sheet_no or v.page + 1) for v in vs})
            if not o.name:
                o.name = next((v.name for v in vs if v.name), "") or self.title
        whole = PartOption(dn or "(drawing)", self.title, "Drawing", note="entire drawing (all sheets)")
        whole.views = [v for s in self.drawing_sheets for v in s.views]
        whole.sheets = sorted({(s.sheet_no or s.index + 1) for s in self.drawing_sheets})
        out = sorted(opts.values(), key=lambda o: (0 if o.kind in ("Detail", "Tabulated") else 1, o.part_number))
        if not out or not any(o.views for o in out):
            # single-part drawing: every option covers the whole drawing
            for o in out:
                o.views = whole.views
                o.sheets = whole.sheets
        return out + [whole]

    # --------------------------------------------------------------------- notes
    def applicable_notes(self, opt: Optional[PartOption]) -> list[dict]:
        """Every note with an 'applies' flag for the chosen part."""
        if self.boxed_flags:
            flagged = set(self.boxed_flags)
        else:
            flagged = {n["label"] for n in self.notes if self.flag_locations.get(n["label"])}
        def own_flags(o):
            got = set()
            for lab, locs in self.flag_locations.items():
                for page, x, y in locs:
                    for v in o.views:
                        if v.page == page and any(c[0] <= x <= c[2] and c[1] <= y <= c[3] for c in (v.comps or [v.rect])):
                            got.add(lab)
            if o.entry:
                got |= set(o.entry.flags)
            return got

        part_flags, from_child = set(), {}
        if opt is not None and opt.kind != "Drawing":
            part_flags = own_flags(opt)
            if opt.kind == "Assembly":
                # an assembly carries the flag notes of every part that goes into it (details, sub-assemblies)
                by_pn = {o.part_number.upper(): o for o in self.part_options()}
                seen, todo = {opt.part_number.upper()}, [opt]
                while todo:
                    cur = todo.pop()
                    try:
                        kids = self.children(cur) if cur.kind == "Assembly" else []
                    except Exception:
                        kids = []
                    for k in kids:
                        pn = (k.get("part_number") or "").upper()
                        child = by_pn.get(pn)
                        if not child or pn in seen:
                            continue
                        seen.add(pn)
                        for lab in own_flags(child) - part_flags:
                            from_child.setdefault(lab, child.part_number)
                        todo.append(child)
                part_flags |= set(from_child)
        out = []
        for n in self.notes:
            base = re.match(r"\d+", n["label"])
            lab = base.group(0) if base else n["label"]
            if n.get("dashes") and opt is not None and opt.dash:
                ok = opt.dash in n["dashes"]
                out.append(dict(n, applies=ok, why=("coded note — marked for -" if ok else "coded note — not marked for -")
                                + opt.dash))
                continue
            if opt is None or opt.kind == "Drawing":
                applies, why = True, "drawing note"
            elif lab in flagged or n["label"] in flagged:
                applies = lab in part_flags or n["label"] in part_flags
                child = from_child.get(lab) or from_child.get(n["label"])
                why = (f"flag note — flagged on {child} (part of this assembly)" if child else
                       "flag note — flagged on this part") if applies else "flag note — not flagged on this part"
            else:
                applies, why = True, "general note"
            out.append(dict(n, applies=applies, why=why))
        return out

    # --------------------------------------------------------------------- children / materials
    def children(self, opt: PartOption) -> list[dict]:
        if not opt or opt.kind != "Assembly":
            return []
        key = opt.find_no or opt.dash
        rows = []
        for e in self.parts.values():
            q = e.qty_by.get(key)
            if q and q not in ("-",):
                rows.append(dict(item_no=e.find_no, part_number=e.part_number, part_name=e.name, qty=q,
                                 notes=" ".join(x for x in (e.size, e.material) if x)))
        if not rows and not any(e.qty_by for e in self.parts.values()):
            rows = [dict(item_no=e.find_no, part_number=e.part_number, part_name=e.name, qty=e.qty_total,
                         notes=" ".join(x for x in (e.size, e.material) if x))
                    for e in self.parts.values() if e.part_number != opt.part_number]
        return rows

    # --------------------------------------------------------------------- balloon
    def balloon(self, opt: Optional[PartOption] = None, page: Optional[int] = None,
                rect: Optional[tuple] = None) -> list[Candidate]:
        """rect is normalised (0..1) on `page`. Reader rules (ignore / extra callouts / tables) are applied."""
        cands = self._balloon_core(opt, page, rect)
        try:
            return _apply_rules(self, cands, opt, page, rect)
        except Exception:
            import traceback
            traceback.print_exc()
            return cands

    def _balloon_core(self, opt: Optional[PartOption] = None, page: Optional[int] = None,
                      rect: Optional[tuple] = None) -> list[Candidate]:
        tabvals = {}
        if opt is not None and opt.dash and opt.dash in self.tab:
            tabvals = self.tab[opt.dash]
        out: list[Candidate] = self._tab_candidates(opt)
        if rect is not None and page is not None:
            self.ensure_views()
            s = self.sheets[page]
            r = (rect[0] * s.W, rect[1] * s.H, rect[2] * s.W, rect[3] * s.H)
            return self.tag_sources(_extract(self, s, [r], {}, opt, ignore_excl=True))
        if opt is None or opt.kind == "Drawing":
            self.ensure_views()
            for s in self.drawing_sheets:
                out += _extract(self, s, None, tabvals, opt)
            return self.tag_sources(out)
        self.ensure_views()
        by_page = defaultdict(list)
        for v in opt.views:
            if v.reference_only:
                continue
            by_page[v.page] += (v.comps or [v.rect]) + ([v.title_rect] if v.title_rect else [])
        for pno, rects in sorted(by_page.items()):
            out += _extract(self, self.sheets[pno], rects, tabvals, opt)
        return out

    def _tab_candidates(self, opt) -> list[Candidate]:
        if opt is None or not opt.dash or opt.dash not in self.tab_cells:
            return []
        out = []
        for lab, val, page, bb in self.tab_cells[opt.dash]:
            s = self.sheets[page]
            l = TL(val, *bb, src=s.src)
            c = _mk(s, l, f"{lab} = {val}" if not lab.startswith("COL") else val, "Dimension", "",
                    s.sheet_no or page + 1, "")
            c.comment = f"Tabulated value ({lab}) for dash -{opt.dash}" + (" – OCR, verify" if s.src == "ocr" else "")
            out.append(c)
        return out

    # ------------------------------------------------------------------ customer-ballooned drawings (SpaceX ◇n)
    @property
    def customer_balloons(self) -> dict:
        cb = getattr(self, "_cust", None)
        if cb is None:
            cb = {}
            if is_pdf(self.filename):
                from .preballoon import find_all
                try:
                    cb = find_all(self.data)
                except Exception:
                    cb = {}
            self._cust = cb
            # numbers inside the customer's balloons are not flag-note markers
            for lab, locs in list(self.flag_locations.items()):
                self.flag_locations[lab] = [
                    (pg, x, y) for pg, x, y in locs
                    if not any(b.rect[0] - 1 <= x <= b.rect[2] + 1 and b.rect[1] - 1 <= y <= b.rect[3] + 1
                               for b in cb.get(pg, []))]
        return cb

    @property
    def preballooned(self) -> bool:
        from .preballoon import is_preballooned
        return is_preballooned(self.customer_balloons)

    def balloon_customer(self, pages=None, rect=None) -> tuple[list[Candidate], list[Candidate], list[int]]:
        """Characteristics numbered with the customer's own balloons.
        Returns (numbered candidates, callouts the customer did not balloon, numbers missing from the sequence)."""
        from .preballoon import match, missing_numbers
        out, extra = [], []
        for pg, bl in sorted(self.customer_balloons.items()):
            if pages is not None and pg not in pages:
                continue
            got, un = match(self, pg, bl, rect)
            out += got
            extra += un
        out.sort(key=lambda c: c.char_no or 0)
        allnums = [b.num for bl in self.customer_balloons.values() for b in bl]
        missing = missing_numbers(allnums) if pages is None and rect is None else []
        return out, extra, missing

    def balloon_sheet(self, page: int) -> list[Candidate]:
        s = self.sheets[page]
        if s.kind != "sheet":
            return _extract(self, s, None, {}, None, ignore_excl=True)
        self.ensure_views()
        return self.tag_sources(_extract(self, s, None, {}, None))

    # ------------------------------------------------------------------ source part of each callout
    def part_label(self, pn: str) -> str:
        """'985AB1234-004 (FN 004) MOTOR PLATE'  /  '99X1234-3 (-3)'."""
        e = self.parts.get(pn)
        dash = re.search(r"-(\d{1,4}[A-Z]?)$", pn)
        tag = f"FN {e.find_no}" if e and e.find_no else (f"-{dash.group(1)}" if dash else "")
        name = e.name if e and e.name else ""
        return " ".join(x for x in (pn, f"({tag})" if tag else "", name) if x)

    def _source_parts(self, page: int, x: float, y: float):
        """Views (smallest first) containing the point -> ordered part numbers, details before assemblies."""
        s = self.sheets[page]
        hits = []
        for v in s.views:
            for c in (v.comps or [v.rect]):
                if c[0] <= x <= c[2] and c[1] <= y <= c[3]:
                    hits.append(((c[2] - c[0]) * (c[3] - c[1]), v))
                    break
        hits.sort(key=lambda h: h[0])
        for _, v in hits:
            if v.parts:
                def asm(pn):
                    e = self.parts.get(pn)
                    return 1 if (e and re.search(r"ASSY|ASSEMBLY|KIT", e.name or "", re.I)) or \
                        (e and any(k == (e.find_no or e.dash) for k in e.qty_by)) else 0
                return sorted(v.parts, key=lambda p: (asm(p), p)), v
            if v.kind != "untitled":
                return [], v
        return [], None

    def view_part_number(self, v) -> str:
        """Part number the view is drawn for (its title), e.g. '985AB1234-308'."""
        dn = self.drawing_no
        key = v.find_no or v.dash
        if key:
            for e in self.parts.values():
                if e.find_no == key or e.dash == key:
                    return e.part_number
            return f"{dn}-{key}" if dn else f"-{key}"
        if not v.parts:
            return ""
        # section / detail views: use the part whose titled view they were cut from
        titled = {self.view_part_number(mv) for mv in self.sheets[v.page].views
                  if mv.kind == "main" and (mv.find_no or mv.dash)}
        pick = sorted(p for p in v.parts if p in titled)
        return (pick or sorted(v.parts))[0]

    def tag_sources(self, cands: list[Candidate]) -> list[Candidate]:
        """Whole-drawing / sheet / area ballooning: Comments = the part number the dimension is shown on."""
        for c in cands:
            if c.comment.startswith("Tabulated"):
                continue
            x, y = (c.bbox[0] + c.bbox[2]) / 2, (c.bbox[1] + c.bbox[3]) / 2
            parts, v = self._source_parts(c.page, x, y)
            pn = self.view_part_number(v) if v is not None else ""
            if not pn and parts:
                pn = parts[-1]
            if pn:
                c.comment = f"{pn} | {c.comment}" if c.comment else pn
        return cands

    def regions(self, opt: Optional[PartOption]) -> dict[int, list]:
        """Normalised view rectangles for the viewer overlay."""
        if not opt:
            return {}
        self.ensure_views()
        out = defaultdict(list)
        for v in opt.views:
            s = self.sheets[v.page]
            for c in (v.comps or [v.rect]):
                out[v.page].append((c[0] / s.W, c[1] / s.H, c[2] / s.W, c[3] / s.H))
        return dict(out)


# ----------------------------------------------------------------------------------------------- extraction
def _boxed(s: Sheet, l: TL, frac: tuple = (0.0, 1.0)):
    """-> None | 'basic' | 'frame' using vector line segments around (part of) the text."""
    if not s.hsegs:
        return None
    if l.vertical:
        # text runs bottom->top: the "along" axis is y
        a0, a1 = l.y1 - (l.y1 - l.y0) * frac[1], l.y1 - (l.y1 - l.y0) * frac[0]
        c0, c1 = l.x0, l.x1
        along_segs = [(g[1], g[0], g[2]) for g in s.vsegs]      # -> (start, position, end) like hsegs
        across_segs = [(g[1], g[0], g[2]) for g in s.hsegs]     # -> (position, start, end) like vsegs
    else:
        a0, a1 = l.x0 + (l.x1 - l.x0) * frac[0], l.x0 + (l.x1 - l.x0) * frac[1]
        c0, c1 = l.y0, l.y1
        along_segs, across_segs = s.hsegs, s.vsegs
    h = c1 - c0
    w = a1 - a0
    if h <= 0 or w <= 0:
        return None
    top = [g for g in along_segs if c0 - 1.3 * h <= g[1] <= c0 + 0.25 * h and g[0] <= a0 + 0.15 * w and g[2] >= a1 - 0.15 * w]
    bot = [g for g in along_segs if c1 - 0.25 * h <= g[1] <= c1 + 1.3 * h and g[0] <= a0 + 0.15 * w and g[2] >= a1 - 0.15 * w]
    if not top or not bot:
        return None
    t = min(top, key=lambda g: abs(g[1] - c0))
    b = min(bot, key=lambda g: abs(g[1] - c1))
    if (b[1] - t[1]) > 3.2 * h:
        return None
    left = [g for g in across_segs if a0 - 1.6 * h <= g[0] <= a0 + 0.2 * h and g[1] <= t[1] + 2 and g[2] >= b[1] - 2]
    right = [g for g in across_segs if a1 - 0.2 * h <= g[0] <= a1 + 1.6 * h and g[1] <= t[1] + 2 and g[2] >= b[1] - 2]
    if not left or not right:
        return None
    lx = max(left, key=lambda g: g[0])[0]
    rx = min(right, key=lambda g: g[0])[0]
    span = max(t[2], b[2]) - min(t[0], b[0])
    if span > (rx - lx) + 1.5 * h:
        return "frame"
    return "basic"


def _frame_datums(s: Sheet, l: TL) -> str:
    ds = [o for o in s.lines if abs(o.cy - l.cy) < 0.6 * l.h and 0 < o.x0 - l.x1 < 12 * l.h
          and re.fullmatch(r"([A-Z](\s*[ⓂⓁⓈ])?\s*){1,4}", o.text)]
    return " ".join(o.text for o in sorted(ds, key=lambda o: o.x0))


DIMLIKE = re.compile(r"\d?\.\d|\d\.|±|Ø|°|UN[CFJ]|\d-\d")


def _reading_order(res: list, rot: int, h: float) -> str:
    """Join OCR pieces in reading order: rows first, then along the row (works for rotated text too)."""
    def uv(r):
        cx, cy = (r[0] + r[2]) / 2, (r[1] + r[3]) / 2
        return {0: (cx, cy), 90: (-cy, cx), 270: (cy, -cx)}.get(rot, (cx, cy))
    items = sorted(((uv(r), r[4]) for r in res), key=lambda it: it[0][1])
    rows: list[list] = []
    for (u, v), t in items:
        if rows and abs(v - rows[-1][0]) < 0.5 * h:
            rows[-1][1].append((u, t))
        else:
            rows.append([v, [(u, t)]])
    return " ".join(t for _, row in rows for _, t in sorted(row))


def _digits(t: str) -> str:
    return re.sub(r"\D", "", t)


def _better_reread(old: str, new: str) -> bool:
    """Only let a close-up read replace the first read when it keeps everything numeric the first read had."""
    from difflib import SequenceMatcher
    if not re.search(r"\d", new):
        return False
    do, dn = _digits(old), _digits(new)
    if len(dn) < len(do) or len(dn) > len(do) + 3:
        return False
    if len(re.findall(r"\.\d", new)) < len(re.findall(r"\.\d", old)):
        return False                                       # never lose a decimal point
    if do and SequenceMatcher(None, do, dn).ratio() < 0.75:
        return False
    for sym in ("Ø", "±", "°", "R"):
        if sym in old and sym not in new:
            return False
    return True


def _reread(info: "DrawingInfo", s: Sheet, lines: list):
    """Second OCR pass: zoom into each doubtful callout and read it again at high resolution (cached).
    The close-up read is used only when it is more confident and loses nothing numeric."""
    from . import ocr as ocrmod
    todo = [l for l in lines if l.src == "ocr" and not getattr(l, "_reread", False) and re.search(r"\d", l.text)
            and DIMLIKE.search(l.text) and len(l.text) <= 45 and getattr(l, "conf", 1.0) < 0.93]
    for l in lines:
        if l.src == "ocr" and l not in todo:
            l._reread = True
    if not todo or not ocrmod.available():
        for l in todo:
            l._reread = True
        return
    doc = pymupdf.open(stream=info.data, filetype="pdf") if is_pdf(info.filename) else None
    img = None if doc else open_image_page(info.data, s.index)
    try:
        src = doc[s.index] if doc else img
        for l in todo:
            l._reread = True
            h = l.h
            rect = (max(0, l.x0 - 0.6 * h), max(0, l.y0 - 0.6 * h), min(s.W, l.x1 + 0.6 * h), min(s.H, l.y1 + 0.6 * h))
            key = (info.sha, s.index, tuple(round(v) for v in rect), ocrmod.prep_key(), "v2")
            got = _REREAD_CACHE.get(key)
            if got is None:
                got = ("", 0.0)
                try:
                    tries = (90, 270) if l.vertical else (0,)
                    best = None
                    m = 0.15 * h
                    for rot in tries:
                        res = ocrmod.ocr_clip(src, rect, dpi=400, rotate=rot, clean=ocrmod.local_filters_on())
                        # only pieces inside the original callout box - not neighbouring text in the margin
                        res = [r for r in res if r[4].strip()
                               and l.x0 - m <= (r[0] + r[2]) / 2 <= l.x1 + m and l.y0 - m <= (r[1] + r[3]) / 2 <= l.y1 + m]
                        if not res:
                            continue
                        txt = _reading_order(res, rot, h)
                        conf = sum(r[5] for r in res) / len(res)
                        if best is None or conf > best[1]:
                            best = (txt, conf)
                    if best:
                        got = best
                except Exception:
                    got = ("", 0.0)
                _REREAD_CACHE[key] = got
            txt, conf = got
            if txt and conf >= max(0.6, getattr(l, "conf", 0.0) + 0.03):
                new = norm_text(txt, ocr=True)
                if new != l.text and _better_reread(l.text, new):
                    l.text = new
                    l.conf = conf
    finally:
        if doc:
            doc.close()


_REREAD_CACHE: dict = {}


def _extract(info: DrawingInfo, s: Sheet, rects, tabvals, opt, ignore_excl=False) -> list[Candidate]:
    def inreg(l: TL):
        return rects is None or any(r[0] <= l.cx <= r[2] and r[1] <= l.cy <= r[3] for r in rects)
    import copy
    titles = {id(v) for v in s.views}
    trects = [v.title_rect for v in s.views if v.title_rect]
    orig = [l for l in s.lines if inreg(l) and (ignore_excl or not s.excluded(l))
            and not any(l.inside(tr) for tr in trects)]
    if s.src == "ocr":
        _reread(info, s, orig)
    # work on copies: merging stacked tolerances must not alter the analysed drawing
    cp = {id(l): copy.copy(l) for l in s.lines}
    lines = [cp[id(l)] for l in orig]
    all_lines = [cp[id(l)] for l in s.lines if not s.excluded(l)]
    for l in cp.values():
        l.used = False
    if s.src == "ocr" and info.inch:
        for l in lines:
            if l.src == "ocr":
                # inch drawings never write a leading zero, so "0.500" is really "Ø.500"
                l.text = re.sub(r"(?<![\d.±+\-/Ø])0\.(?=\d{2,4}\b)", "Ø.", l.text)
    s._placed = []                                   # balloons placed in this run (collision avoidance)
    metric = not getattr(info, "inch", True)
    N = NUM_M if metric else NUM                    # whole numbers count as dimensions on metric drawings
    dim_rx = DIM_IN_LINE_M if metric else DIM_IN_LINE
    sn = s.sheet_no or (s.index + 1)
    src_note = "OCR – verify" if s.src == "ocr" else ""
    cands: list[Candidate] = []

    def near(l, pred, dx, dy):
        return [o for o in lines if o is not l and not o.used and pred(o)
                and abs(o.cy - l.cy) < dy and (o.x0 - l.x1 < dx and l.x0 - o.x1 < dx)]

    # 0) composite / multi-row feature control frames: one symbol cell spans several rows, e.g.
    #      | ⌖ | Ø.010 Ⓜ | A | B | C |
    #      |   | Ø.000 Ⓜ | A |
    #    every row is its own characteristic and inherits the symbol ("⌖ Ø.000 Ⓜ A")
    row_rx = re.compile(rf"^(?:Ø\s*)?(?:{N}|{NUM_M})\s*[ⓂⓁⓈⓅⓊ]?(?:\s*[ⓂⓁⓈⓅⓊ]?\s*[\d.]+)?(?:\s*[A-Z](?:\s*[ⓂⓁⓈ])?){{0,3}}$")
    for l in lines:
        if l.vertical or l.used:
            continue
        t = l.text.strip()
        if not t or t[0] not in GDT_CHARS:
            continue
        sym = t[0]
        lone = len(t) == 1                               # symbol in its own cell, rows to its right
        rows = []
        for o in lines:
            if o is l or o.used or o.vertical or any(ch in o.text for ch in GDT_CHARS):
                continue
            if not row_rx.match(o.text.strip()):
                continue
            if lone:
                ok = (l.y0 - 1.3 * l.h <= o.cy <= l.y1 + 1.3 * l.h) and -0.6 * l.h <= o.x0 - l.x1 <= 3.0 * l.h
            else:
                # next row(s) below a frame that already carries the symbol, starting in its value column.
                # Measured in the row's own text height: a tall symbol centred on both rows can make the
                # first row's box twice as high.
                rh = min(o.h, l.h)
                ok = (0.35 * rh < o.cy - l.cy < 3.2 * rh and o.y1 > l.y1 - 0.2 * rh
                      and l.x0 - 0.3 * rh <= o.x0 <= l.x0 + 5.0 * rh)
            if ok:
                rows.append(o)
        if not rows:
            continue
        if not lone:
            # only rows stacked directly under each other (no gaps bigger than one row)
            rows.sort(key=lambda o: o.cy)
            keep, last = [], l
            for o in rows:
                if o.cy - last.cy < 1.6 * min(o.h, l.h) + (l.h - min(o.h, l.h)):
                    keep.append(o)
                    last = o
            rows = keep
        for o in rows:
            o.text = f"{sym} {o.text.strip()}"
        if lone:
            l.used = True
    # 0b) frames whose cells are separate text objects: pull the datum cells of the same row onto the frame
    datum_rx = re.compile(r"^(?:[A-Z](?:\s*[ⓂⓁⓈ])?)(?:\s*[|\-]?\s*[A-Z](?:\s*[ⓂⓁⓈ])?){0,3}$")
    for l in lines:
        if l.used or l.vertical or not l.text or l.text.strip()[0] not in GDT_CHARS:
            continue
        right = l.x1
        while True:
            nxt = [o for o in lines if o is not l and not o.used and not o.vertical and datum_rx.match(o.text.strip())
                   and abs(o.cy - l.cy) < 0.45 * l.h and -0.3 * l.h <= o.x0 - right < 3.2 * l.h]
            if not nxt:
                break
            o = min(nxt, key=lambda o: o.x0)
            if any(q is not l and q is not o and not q.vertical and abs(q.cy - l.cy) < 0.45 * l.h
                   and right - 0.3 * l.h <= q.x0 < o.x0 for q in lines):
                break                                     # something else sits in between: not this frame's datums
            l.text = f"{l.text} {o.text.strip()}"
            right = l.x1 = max(l.x1, o.x1)
            o.used = True
    # 1) stacked unequal tolerances: ".100 +.000" over "-.030"
    for l in lines:
        mt = re.fullmatch(rf"({SNUM}|0)((?:\s+[A-Z][A-Z .]*)?)", l.text.strip())
        if l.used or not mt:
            continue
        hosts = [o for o in lines if o is not l and not o.used and not o.vertical == (not l.vertical)
                 and re.search(rf"(?:{N}|\d)\s*[+-]?\s*{N}?\s*$|{SNUM}$", o.text)
                 and 0.1 * l.h < (l.cy - o.cy) < 1.9 * l.h and o.x1 > l.x0 - 1.5 * l.h and o.x0 < l.x1]
        if hosts:
            o = min(hosts, key=lambda o: abs(o.x1 - l.x1))
            tol = mt.group(1).replace(" ", "")
            tail = mt.group(2).strip()
            if tol == "0" and not re.search(rf"{SNUM}\s*$", o.text):
                continue                                   # a bare 0 is only a lower tolerance under "+x"
            if tol == "0":
                tol = "-0"
            # "5 0" over "-0.3": the upper tolerance is written without its sign
            o.text = re.sub(rf"^(.*?{N})\s+(0|{N})$", r"\1 +\2", o.text) if not re.search(SNUM + r"\s*$", o.text) \
                else o.text
            if re.search(rf"{SNUM}\s*$", o.text):
                o.text = f"{o.text}/{tol}"
            else:
                o.text = f"{o.text} {tol}"
            if tail:
                o.text += " " + tail
            o.y1 = max(o.y1, l.y1)
            o.x0, o.x1 = min(o.x0, l.x0), max(o.x1, l.x1)         # the box covers the whole stacked tolerance
            l.used = True
    # 2) stacked limits: ".8130" over ".8120" (tight, same column, close values)
    for l in lines:
        if l.used or l.vertical:
            continue
        m = re.fullmatch(rf"((?:\d+X\s*)?(?:Ø|R)?)\s*({N})", l.text)
        if not m:
            continue
        below = [o for o in lines if o is not l and not o.used and re.fullmatch(N, o.text)
                 and 0.5 * l.h < o.cy - l.cy < 1.6 * l.h and abs(o.x1 - l.x1) < 1.2 * l.h]
        if below:
            o = below[0]
            a, b = float(m.group(2)), float(o.text)
            if max(a, b) > 0 and abs(a - b) / max(a, b) < 0.12 and a != b:
                lo, hi = sorted([m.group(2), o.text], key=float)
                l.text = f"{m.group(1)}{lo}-{hi}"
                l.y1 = o.y1
                l.x0, l.x1 = min(l.x0, o.x0), max(l.x1, o.x1)
                o.used = True
    # 3a) a lone Ø / R / depth symbol (or "(" / ")") on its own line belongs to the number just right of it
    for l in lines:
        sym = l.text.replace("(", "").replace(")", "").strip()
        if l.used or l.vertical or sym not in ("Ø", "R", "SR", "SØ", "↧"):
            continue
        tgt = [o for o in lines if o is not l and not o.used and re.search(N, o.text)
               and abs(o.cy - l.cy) < 0.9 * l.h and -0.3 * l.h <= o.x0 - l.x1 < 2.5 * l.h]
        if tgt:
            o = min(tgt, key=lambda o: o.x0)
            opened = "(" in l.text or o.text.lstrip().startswith("(")
            core = o.text.lstrip("( ")
            m_num = re.match(rf"^({N})\s*\)?\s*(.*)$", core)
            closes = [q for q in lines if q is not o and not q.used and q.text.lstrip().startswith(")")
                      and abs(q.cy - o.cy) < 0.9 * l.h and 0 <= q.x0 - o.x1 < 1.5 * l.h]
            if opened and m_num and (closes or core.rstrip().endswith(")") or ")" in core):
                # "(Ø4.2)" - a size in parentheses is a reference value (e.g. the tap drill), keep it that way
                o.text = f"({sym}{m_num.group(1)})"
                if m_num.group(2).strip():
                    o.text += " " + m_num.group(2).strip()
                for q in closes[:1]:
                    q.text = q.text.lstrip()[1:].strip()
            else:
                o.text = f"{sym}{core}"
            o.x0 = l.x0
            l.used = True
    for l in lines:                                        # a line that only closes a "( … )" group
        if not l.used and l.text.lstrip().startswith(")") and re.search(r"\d", l.text):
            l.text = l.text.lstrip()[1:].strip()
    # 3) count prefix on its own line: "2X" just left of a number
    for l in lines:
        if not l.used and re.fullmatch(r"\d+\s*X\s*\(", l.text):
            l.text = l.text.rstrip("( ")
        if l.used or not re.fullmatch(r"\d+\s*X", l.text):
            continue
        tgt = [o for o in lines if o is not l and not o.used and re.search(N, o.text)
               and abs(o.cy - l.cy) < 1.2 * l.h and 0 <= o.x0 - l.x1 < 4 * l.h]
        if tgt:
            o = min(tgt, key=lambda o: o.x0)
            o.text = f"{l.text.replace(' ', '')} {o.text}"
            o.x0 = l.x0
            l.used = True
    # 4) modifier lines appended to the callout above ("MIN 4 FULL THREADS", "2 SURFACES", "MINOR Ø OF 201")
    def is_modifier(o):
        return (not o.vertical and not re.search(NUM, o.text) and len(o.text) <= 45 and re.search(r"[A-Z]{2,}", o.text)
                and not THREAD_RE.search(o.text) and not any(ch in o.text for ch in GDT_CHARS)
                and not re.search(r"#\d|\d-\d", o.text)
                and not TITLE_SECT.match(o.text) and not SCALE_RE.match(o.text) and o.text not in ("CC", "KC", "SC"))

    # 5) candidates
    for l in lines:
        if l.used:
            continue
        t = l.text
        up = t.upper()
        found: list[tuple[str, str]] = []
        if any(ch in t for ch in GDT_CHARS):
            if re.search(N, t):
                found.append((t, "GD&T"))
        elif THREAD_RE.search(t):
            found.append((t, "Thread"))
        else:
            alpha = [w for w in re.findall(r"[A-Za-z]{3,}", t) if w.upper() not in
                     ("MAX", "MIN", "BSC", "REF", "THRU", "DEEP", "TYP", "HEX", "PLACES", "EQ", "CSK", "CBORE",
                      "DIA", "SPACED", "DRILL", "ALL", "AROUND", "OVER", "SURFACES", "HOLES", "HOLE", "SPOTFACE",
                      "CHAMFER", "BREAK", "EDGES", "WIDE", "FULL", "BOTH", "SIDES", "FAR", "SIDE", "NEAR")]
            if len(alpha) >= 3 or re.search(r"\bNOTE\b|\bSEE\b|\bFN\b|\bSH(EET)?\b", up):
                continue
            if re.fullmatch(rf"\d+\s*X\s*\((?:Ø|R)?\s*{N}\)(?:\s+.*)?", t):
                found.append((t, "Reference"))                 # "7X (Ø4.2)": reference size
                whole = None
            whole = None if found else re.fullmatch(
                rf"(?:\(?\d+\s*X\)?\s*)?(?:Ø|SØ|R|SR|CR|↧)?\s*{N}°?(?:\s*±\s*{N}°?|\s*[+-]\s*{N}\s*/?\s*[+-]\s*{N}"
                rf"|\s*[-]\s*{N})?(?:\s+[A-Z][A-Z .]*)?|\(.*{N}.*\)|\[.*{N}.*\]", t)
            if whole and metric and re.fullmatch(r"\d+", t.strip()) and (l.size < 0.9 * s.med or len(t.strip()) > 5):
                whole = None                             # small plain integers: balloon / find / sheet numbers
            if found:
                pass
            elif whole:
                found.append((t, "Dimension"))
            else:
                for rx in (dim_rx, ANGLE_IN_LINE):
                    for m in rx.finditer(t):
                        d = m.group("d").strip()
                        if re.search(r"\d", d):
                            found.append((d, "Angle" if "°" in d else "Dimension"))
                    if found:
                        break
        for req, typ in found:
            if re.fullmatch(r"[Ø]?0?\.0+|0", req.replace(" ", "")):
                continue                                     # ordinate origin .000
            mcount = re.match(r"^\(?\d+\s*X\)?\s+", req)
            frac = (len(mcount.group(0)) / max(len(t), 1), 1.0) if (mcount and req == t) else (0.0, 1.0)
            box = _boxed(s, l, frac)
            if box == "basic" and typ == "Dimension":
                m = re.match(r"^(\d+\s*X\s+)?(.*)$", req)
                req = f"{m.group(1) or ''}[{m.group(2).strip()}]"
                typ = "Basic"
            elif box == "frame" and typ != "GD&T":
                req = f"FCF {req} {_frame_datums(s, l)}".strip()
                typ = "GD&T"
            if re.fullmatch(r"(?:\d+\s*X\s*)?\(.*\)(?:\s+.*)?", req):
                typ = "Reference"
            # modifiers under the callout
            mods = [] if l.vertical else [o for o in all_lines if o is not l and not o.used and is_modifier(o)
                                          and -0.2 * l.h <= o.y0 - l.y1 < 0.9 * l.h and o.x0 < l.x1 and o.x1 > l.x0 - 3 * l.h]
            for o in sorted(mods, key=lambda o: o.y0)[:2]:
                req = f"{req} {o.text}"
                o.used = True
                l.x0, l.x1, l.y1 = min(l.x0, o.x0), max(l.x1, o.x1), max(l.y1, o.y1)   # balloon clears it too
            desig = ""
            mcc = re.search(r"\s+\b(CC|KC|SC)\b\s*$", req)
            if mcc:
                req = req[:mcc.start()]
                desig = "Critical" if mcc.group(1) == "CC" else "Key" if mcc.group(1) == "KC" else "Major"
            cc = near(l, lambda o: o.text in ("CC", "KC", "SC", "CRIT", "KEY", "MAJ"), 4 * l.h, 0.9 * l.h)
            if cc and not desig:
                desig = "Critical" if cc[0].text in ("CC", "CRIT") else "Key" if cc[0].text in ("KC", "KEY") else "Major"
            cands.append(_mk(s, l, req, typ, desig, sn, src_note))
        if found:
            l.used = True
    # reading order within the sheet
    cands.sort(key=lambda c: (round(c.bbox[1] / (0.06 * s.H)), c.bbox[0]))
    return cands


# ----------------------------------------------------------------------------------------------- balloon placement
class _Grid:
    """Bucket boxes by area so collision checks stay fast on busy sheets."""
    def __init__(self, cell: float):
        self.cell = cell
        self.b: dict[tuple, list] = {}

    def add(self, box, item=None):
        c = self.cell
        for gx in range(int(box[0] // c), int(box[2] // c) + 1):
            for gy in range(int(box[1] // c), int(box[3] // c) + 1):
                self.b.setdefault((gx, gy), []).append((box, item))

    def query(self, box):
        c, seen, out = self.cell, set(), []
        for gx in range(int(box[0] // c), int(box[2] // c) + 1):
            for gy in range(int(box[1] // c), int(box[3] // c) + 1):
                for e in self.b.get((gx, gy), ()):
                    if id(e) not in seen:
                        seen.add(id(e))
                        out.append(e)
        return out


def _placement_index(s: Sheet):
    idx = getattr(s, "_pidx", None)
    if idx is None:
        cell = max(s.W, s.H) / 60.0
        text, geo = _Grid(cell), _Grid(cell)
        for l in s.lines:
            text.add((l.x0, l.y0, l.x1, l.y1), l)
        n = 0
        for x0, y, x1 in s.hsegs:
            if x1 - x0 > 0.5:
                geo.add((x0, y - 0.3, x1, y + 0.3))
                n += 1
            if n > 60000:
                break
        n = 0
        for x, y0, y1 in s.vsegs:
            if y1 - y0 > 0.5:
                geo.add((x - 0.3, y0, x + 0.3, y1))
                n += 1
            if n > 60000:
                break
        idx = s._pidx = (text, geo)
    return idx


def _view_box_for(s: Sheet, l: TL):
    """The part view a callout belongs to (the one containing it, else the nearest)."""
    best, bd = None, None
    for v in s.views:
        for c in (v.comps or [v.rect]):
            if not c:
                continue
            if c[0] <= l.cx <= c[2] and c[1] <= l.cy <= c[3]:
                return c
            d = _rect_dist(c, (l.x0, l.y0, l.x1, l.y1))
            if bd is None or d < bd:
                best, bd = c, d
    return best if bd is not None and bd < 0.25 * max(s.W, s.H) else None


def _place_balloon(s: Sheet, l: TL, r: float) -> tuple[float, float]:
    """Balloon centre next to the whole callout (value + stacked tolerance + frame cells), on the side facing
    away from the part view, clear of other text, geometry and balloons already placed."""
    text, geo = _placement_index(s)
    placed = getattr(s, "_placed", None)
    if placed is None:
        placed = s._placed = []
    x0, y0, x1, y1 = l.x0, l.y0, l.x1, l.y1
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    g = 0.35 * r
    opts = [(x1 + g + r, cy), (x0 - g - r, cy),
            (cx, y0 - g - r), (cx, y1 + g + r),
            (x1 + g * 0.5 + r * 0.8, y0 - g * 0.5 - r * 0.8), (x1 + g * 0.5 + r * 0.8, y1 + g * 0.5 + r * 0.8),
            (x0 - g * 0.5 - r * 0.8, y0 - g * 0.5 - r * 0.8), (x0 - g * 0.5 - r * 0.8, y1 + g * 0.5 + r * 0.8)]
    own = (x0 - 0.5, y0 - 0.5, x1 + 0.5, y1 + 0.5)
    reach = 5.0 * r                                      # how far past the balloon we look for part geometry
    best, bcost = opts[0], None
    for k, (bx, by) in enumerate(opts):
        box = (bx - r, by - r, bx + r, by + r)
        cost = 0.4 * k                                   # mild preference for the usual right-hand spot
        if box[0] < 0.005 * s.W or box[2] > 0.995 * s.W or box[1] < 0.005 * s.H or box[3] > 0.995 * s.H:
            cost += 1000
        for tb, t in text.query(box):
            if not _overlap(tb, box):
                continue
            ix = max(0.0, min(tb[2], own[2]) - max(tb[0], own[0])) * max(0.0, min(tb[3], own[3]) - max(tb[1], own[1]))
            area = max((tb[2] - tb[0]) * (tb[3] - tb[1]), 1e-6)
            if ix / area > 0.7:
                continue                                 # part of the callout itself
            cost += 60                                   # never on top of other text / tolerances
        for pb in placed:
            if math.hypot(pb[0] - bx, pb[1] - by) < pb[2] + r + 0.15 * r:
                cost += 80
        crossings = sum(1 for gb, _ in geo.query(box) if _overlap(gb, box))
        cost += min(crossings, 12) * 2.0                 # part lines / dimension lines under the balloon
        # outside the part view: the side of the callout facing away from the part has little geometry
        dx, dy = bx - cx, by - cy
        dn = math.hypot(dx, dy) or 1.0
        ux, uy = dx / dn, dy / dn
        px, py = bx + ux * (r + reach / 2), by + uy * (r + reach / 2)
        probe = (px - reach / 2, py - reach / 2, px + reach / 2, py + reach / 2)
        ahead = sum(1 for gb, _ in geo.query(probe) if _overlap(gb, probe))
        cost += min(ahead, 25) * 0.5
        if bcost is None or cost < bcost:
            best, bcost = (bx, by), cost
    placed.append((best[0], best[1], r))
    return best


def _mk(s: Sheet, l: TL, req: str, typ: str, desig: str, sn, note) -> Candidate:
    # balloon diameter ≈ 1.5 × the callout's text height (font size for PDF text, box height for OCR)
    base = l.size if (l.size and l.size > 0) else l.h
    if l.src == "ocr":
        base = min(base, l.h)
    r = min(max(0.75 * base, 0.0032 * max(s.W, s.H)), 0.016 * max(s.W, s.H))
    bx, by = _place_balloon(s, l, r)
    bx = min(max(bx, 0.01 * s.W), 0.99 * s.W)
    by = min(max(by, 0.01 * s.H), 0.99 * s.H)
    zone = s.zone_at(l.cx, l.cy)
    view = ""
    for v in s.views:
        if any(c[0] <= l.cx <= c[2] and c[1] <= l.cy <= c[3] for c in (v.comps or [v.rect])):
            view = v.title
            break
    c = Candidate(s.index, re.sub(r"\s+", " ", req).strip(), (l.x0, l.y0, l.x1, l.y1), bx / s.W, by / s.H, typ, desig,
                  f"SH {sn}" + (f" {zone}" if zone else ""), view, note, l.src)
    c.selected = typ != "Reference"
    c.nbbox = (l.x0 / s.W, l.y0 / s.H, l.x1 / s.W, l.y1 / s.H)
    c.vertical = bool(l.vertical)
    c.rot = getattr(l, "rot", 0) or (90 if l.vertical else 0)
    c.bsize = r / max(s.W, s.H)
    return c


# ----------------------------------------------------------------------------------------------- reader rules
def _rule_title_fields(info: DrawingInfo):
    from . import rules as _rules
    for s in info.drawing_sheets[:1] or info.sheets[:1]:
        txt = getattr(s, "_rules_text", "")
        prod = getattr(s, "_producer", "")
        for r in _rules.active("title", txt, prod):
            f = str(r.p("field"))
            try:
                m = re.search(str(r.p("pattern")), "\n".join(l.text for l in s.lines), re.I)
            except re.error:
                continue
            if not m or not m.groups():
                continue
            v = m.group(1).strip()
            if f == "rev" and not info.rev:
                info.rev = v
            elif f == "drawing_no" and not info.drawing_no:
                info.drawing_no = v
            elif f == "title" and not info.title:
                info.title = v
            elif f == "cage" and not info.cage:
                info.cage = v


def _apply_rules(info: DrawingInfo, cands: list, opt, page, rect) -> list:
    from . import rules as _rules
    sheets = [info.sheets[page]] if (rect is not None and page is not None) else info.drawing_sheets
    out = list(cands)
    for s in sheets:
        txt, prod = getattr(s, "_rules_text", ""), getattr(s, "_producer", "")
        area = None
        if rect is not None:
            area = (rect[0] * s.W, rect[1] * s.H, rect[2] * s.W, rect[3] * s.H)

        def in_area(l):
            return area is None or l.inside(area)
        sn = s.index + 1
        # ---- ignore
        ign = _rules.active("ignore", txt, prod)
        if ign:
            out = [c for c in out if c.page != s.index or not _rules.is_ignored(c.requirement, ign)]
        words = _rules.gdt_words(txt, prod)
        # ---- characteristic tables (PMI schedule ...): one characteristic per row
        whole = opt is None or opt.kind == "Drawing" or rect is not None
        for r in _rules.active("table", txt, prod) if whole else []:
            rows = _rules.table_rows([l for l in s.lines if l.src == "text" or True], str(r.p("title")))
            if not rows:
                continue
            tbl_lines = [row["_line"] for row in rows]
            y0 = min(l.y0 for l in tbl_lines) - 2
            y1 = max(l.y1 for l in tbl_lines) + 40
            x0 = min(l.x0 for l in tbl_lines) - 5
            # values already picked up from inside the table are replaced by the row characteristics
            out = [c for c in out if c.source == "rule-table" or not (
                c.page == s.index and x0 <= (c.bbox[0] + c.bbox[2]) / 2 and y0 <= (c.bbox[1] + c.bbox[3]) / 2 <= y1)]
            tcol = str(r.p("type_column") or "").upper().strip()
            skip = str(r.p("skip") or "")
            for row in rows:
                l = row["_line"]
                if not in_area(l):
                    continue
                flat = " ".join(v for k, v in row.items() if k != "_line")
                try:
                    if skip and re.search(skip, flat, re.I):
                        continue
                except re.error:
                    pass
                req = _rules.fill_template(str(r.p("template") or ""), row)
                if not req:
                    continue
                req = _rules.spell_to_symbols(req, words)
                if any(c.page == s.index and c.requirement.upper() == req.upper() and c.source == "rule-table"
                       and abs(c.bbox[1] - l.y0) < 1 for c in out):
                    continue
                typ = "GD&T" if any(sym in req for _w, sym in words) else \
                    ("Note" if (tcol and "NOTE" in (row.get(tcol, "") or "").upper()) or re.search(r"[A-Z]{4,}", req)
                     else "Dimension")
                c = _mk(s, l, req, typ, "", sn, f"table: {r.name}")
                c.source = "rule-table"
                out.append(c)
        # ---- extra callout patterns
        for r in _rules.active("callout", txt, prod):
            try:
                rx = re.compile(str(r.p("pattern")), re.I)
            except re.error:
                continue
            for l in s.lines:
                if not in_area(l) or s.excluded(l) and rect is None:
                    continue
                m = rx.search(l.text)
                if not m:
                    continue
                if any(c.page == s.index and (m.group(0).upper() in c.requirement.upper()) for c in out):
                    continue
                if opt is not None and opt.kind != "Drawing" and rect is None:
                    vs = [v for v in opt.views if v.page == s.index]
                    if vs and not any(l.inside(cr) for v in vs for cr in (v.comps or [v.rect])):
                        continue
                req = l.text.strip()
                c = _mk(s, l, req, str(r.p("char_type") or "Note"), str(r.p("designator") or ""), sn,
                        f"rule: {r.name}")
                c.source = "rule"
                out.append(c)
    # a schedule table is the authority: callouts read off the views that repeat a schedule row are
    # left in the list for cross-checking, unticked
    table_reqs = [c.requirement.upper() for c in out if c.source == "rule-table"]
    if table_reqs:
        def key(t):
            return re.sub(r"[^0-9.]", "", t.replace("0.", "."))
        for c in out:
            if c.source == "rule-table" or not c.selected:
                continue
            k = key(c.requirement)
            if k and any(k in key(t) for t in table_reqs) and (c.source == "ocr" or c.source == "rule"
                                                               or c.char_type == "Reference"):
                c.selected = False
                c.comment = (c.comment + " | " if c.comment else "") + "also in the PMI schedule"
    return out


# ----------------------------------------------------------------------------------------------- top level
def _drawing_number(info: DrawingInfo):
    tok = re.compile(r"\b(?=[A-Z0-9-]*\d)(?=[A-Z0-9-]*[A-Z])[A-Z0-9][A-Z0-9-]{5,}[A-Z0-9]\b"
                     r"|\b\d{5,}-\d{2,4}\b")                            # all-numeric numbers: 01234567-030
    cnt = Counter()
    for s in info.drawing_sheets:
        for l in s.lines:
            if l.cx > 0.5 * s.W and l.cy > 0.75 * s.H:
                for m in tok.finditer(l.text.upper()):
                    v = m.group(0)
                    if not re.match(r"^(ARIAL|LM_|MIL-|AMS|ASME|ASTM|F\d{5}-\d{2}-C)", v):
                        cnt[v] += 3 if l.size >= s.med else 1
        for l in s.lines:
            mp = re.match(r"^PART\s+(?:NO|NUMBER)\.?\s*:?\s*(\S+)$", l.text, re.I)
            if mp and tok.fullmatch(mp.group(1).upper()):
                cnt[mp.group(1).upper()] += 2
            if re.match(r"^(DWG\.?\s*NO\.?|DRAWING\s+NO\.?|DRAWING\s+NUMBER)", l.text, re.I):
                nb = [o for o in s.lines if o is not l and (0 <= o.y0 - l.y1 < 5 * l.h or abs(o.cy - l.cy) < l.h)
                      and -2 * l.h < o.x0 - l.x0 < 25 * l.h and tok.search(o.text.upper())]
                for o in nb:
                    cnt[tok.search(o.text.upper()).group(0)] += 6
    fn = re.sub(r"[_ ]+", " ", info.filename.upper())
    for m in tok.finditer(fn):
        v = m.group(0)
        if not re.match(r"^(MIL|REV|DWG|PROPER)", v):
            cnt[v] += 2
    if cnt:
        best = cnt.most_common(1)[0][0]
        # prefer the base number when both "XXXX" and "XXXX-001" are present and the base is frequent
        info.drawing_no = best


def _title_block_fields(info: DrawingInfo):
    s = next((x for x in info.drawing_sheets if x.tb_rect), None)
    if not s:
        return
    x0, y0, x1, y1 = s.tb_rect
    pad = 0.015 * s.W                                   # fields at the very edge (SpaceX REV box) sit just outside
    tb = [l for l in s.lines if l.inside((x0 - pad, y0 - pad, min(s.W, x1 + pad), min(s.H, y1 + pad)))]
    for l in tb:
        if re.fullmatch(r"REV\.?", l.text, re.I) and not info.rev:
            nb = [o for o in tb if o is not l and 0 <= o.y0 - l.y1 < 4 * l.h and abs(o.cx - l.cx) < 3 * l.h
                  and re.fullmatch(r"[A-Z]{1,2}|-|\d{1,2}", o.text)]
            if nb:
                info.rev = nb[0].text
        if re.search(r"CAGE|CODE\s+IDENT|FSCM", l.text, re.I) and not info.cage:
            m = re.search(r"\b([0-9A-Z]{5})\b\s*$", l.text)
            if m and re.search(r"\d", m.group(1)):
                info.cage = m.group(1)
            else:
                nb = [o for o in tb if o is not l and re.fullmatch(r"[0-9A-Z]{5}", o.text) and re.search(r"\d", o.text)
                      and (0 <= o.y0 - l.y1 < 4 * l.h or (abs(o.cy - l.cy) < l.h and 0 < o.x0 - l.x1 < 10 * l.h))]
                if nb:
                    info.cage = nb[0].text
    if not info.rev:                                    # latest letter in the revisions table
        for l in s.lines:
            if re.fullmatch(r"REV\.?", l.text, re.I) and not l.inside(s.tb_rect) and l.cy < 0.35 * s.H:
                col = [o for o in s.lines if o is not l and o.y0 > l.y1 - 1 and abs(o.cx - l.cx) < 1.5 * l.h
                       and re.fullmatch(r"[A-HJ-NP-Z]{1,2}|\d{1,2}", o.text) and o.y0 - l.y1 < 30 * l.h
                       and o.size <= 1.4 * l.size]
                if col:
                    info.rev = max(col, key=lambda o: o.y0).text
                    break
    cands = [l for l in tb if re.search(r"[A-Z]{3,}", l.text) and not TB_WORDS.search(l.text)
             and not COMPANY_RE.search(l.text) and info.drawing_no not in l.text.upper()
             and not re.search(r"\d{4}-\d{2}|\d{2}/\d{2}", l.text) and len(l.text) > 3]
    if cands:
        big = max(cands, key=lambda l: (l.size, len(l.text)))
        same = sorted([l for l in cands if abs(l.size - big.size) < 0.5 and abs(l.cx - big.cx) < 30 * big.h
                       and abs(l.cy - big.cy) < 4 * big.h], key=lambda l: l.y0)
        last = same[-1]
        cont = [l for l in tb if l not in same and abs(l.size - big.size) < 0.5 and 0 <= l.y0 - last.y1 < 0.8 * big.h
                and abs(l.cx - last.cx) < 12 * big.h and re.fullmatch(r"[A-Z0-9_\-]{1,6}", l.text)]
        title = " ".join(l.text for l in same + sorted(cont, key=lambda l: l.y0)[:1])
        title = re.sub(r"\b\d{1,2}-[A-Za-z]{3}-\d{2,4}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b", " ", title)   # dates in the row
        for tokn in (info.cage, info.drawing_no):
            if tokn:
                title = re.sub(rf"\b{re.escape(tokn)}\b", " ", title)
        info.title = re.sub(r"\s+", " ", title).strip()[:120]


def _tolerances(info: DrawingInfo):
    tol = {}
    for s in info.drawing_sheets:
        pool = [l for l in s.lines if (s.tb_rect and l.inside(s.tb_rect)) or (s.notes_rect and l.inside(s.notes_rect))]
        for l in pool:
            t = l.text.upper()

            def val_of(text):
                m = re.search(r"±\s*(\d*\.\d+|\d+)|(?:^|\s)(\.\d+)\s*$", text)
                return (m.group(1) or m.group(2)) if m else None

            def right_val():
                nb = [o for o in pool if o is not l and abs(o.cy - l.cy) < 0.7 * l.h and 0 < o.x0 - l.x1 < 12 * l.h
                      and re.search(r"\d", o.text)]
                return val_of(min(nb, key=lambda o: o.x0).text) if nb else None
            m = re.match(r"^(\d)\s*(?:PL\b|PLACES?\b|PLACE\s+DEC|DEC\b)", t) or re.match(r"^\.(X{1,4})\b", t) \
                or re.match(r"^X+\.(X{1,4})\b", t)
            if m:
                n = int(m.group(1)) if m.group(1).isdigit() else len(m.group(1))
                v = val_of(t[m.end():]) or right_val()
                if v and 1 <= n <= 4:
                    tol.setdefault(f"tol_{n}dec", v)
            m0 = re.match(r"^X\s*±\s*(\d*\.?\d+)\s*$", t)                      # metric "X ± 2"
            if m0:
                tol.setdefault("tol_0dec", m0.group(1))
            ma = re.match(r"^X(?:\.X+)?\s*°\s*±\s*(\d*\.?\d+)\s*°?$", t)            # "X° ± 1°"
            if ma and "." not in t.split("°")[0]:
                tol.setdefault("tol_angle", ma.group(1))
            if re.match(r"^ANGLES?\b", t):
                rest = t[5:]
                m2 = re.search(r"(\d+)\s*DEG\s*(\d+)\s*MIN", rest)
                if m2:
                    tol.setdefault("tol_angle", f"{int(m2.group(1)) + int(m2.group(2)) / 60:g}")
                else:
                    v = val_of(rest.replace("°", "")) or right_val()
                    if v:
                        tol.setdefault("tol_angle", v.replace("°", ""))
            for mm in re.finditer(r"\.(X{1,4})\s*±\s*(\d*\.\d+)", t):
                tol.setdefault(f"tol_{len(mm.group(1))}dec", mm.group(2))
        if tol:
            break
    info.tolerances = {k: (("0" + v) if v.startswith(".") else v) for k, v in tol.items()}


def _notes(info: DrawingInfo):
    from .importers import NOTE_RE
    rows = []
    for s in info.drawing_sheets:
        if not s.notes_rect:
            continue
        rows.append(["__SHEET__"])                         # sheet boundary marker
        ls = sorted([l for l in s.lines if l.inside(s.notes_rect) and not l.vertical], key=lambda l: (l.y0, l.x0))
        # join lines on the same row (flag number box + note text)
        cur = []
        for l in ls:
            if cur and abs(l.cy - cur[-1].cy) < 0.5 * l.h:
                cur.append(l)
            else:
                if cur:
                    rows.append(cur)
                cur = [l]
        if cur:
            rows.append(cur)
    notes: list[dict] = []
    current = None
    expected = 1
    boxed = set()
    sheet_of = {}
    for s in info.drawing_sheets:
        for l in s.lines:
            sheet_of[id(l)] = s
    skip_sheet = False
    sheet_no = -1
    for row in rows:
        if row == ["__SHEET__"]:
            skip_sheet = False
            sheet_no += 1
            continue
        if skip_sheet:
            continue
        row = sorted(row, key=lambda l: l.x0)
        first = row[0]
        if re.fullmatch(r"\d{1,3}", first.text) and sheet_of.get(id(first)) is not None \
                and _boxed(sheet_of[id(first)], first) in ("basic", "frame"):
            boxed.add(first.text)
        text = " ".join(l.text for l in row)
        if NOTES_CONT_ON.search(text):
            # "NOTES CONTINUED ON SHEET 2": everything after this on the sheet is not a note
            before = text[:NOTES_CONT_ON.search(text).start()].strip()
            if before and current:
                current["text"] += " " + before
            skip_sheet = True
            continue
        if re.fullmatch(NOTES_HDR, text, re.I) or NOTES_CONT_FROM.search(text):
            continue
        if BANNER_RE.search(text) and not re.match(r"^\d{1,3}\s*[.)]?\s", text):
            if current and current.get("sheet") == sheet_no:
                skip_sheet = True                          # distribution statement / export warning ends the notes
            continue
        m7 = re.match(r"^(\d{1,3})([A-Z])\s+(.+)$", text)          # "7A MASKING REQUIRED …" (SpaceX sub-notes)
        if m7 and current and re.match(r"\d+", current["label"]).group(0) == m7.group(1):
            current = {"label": f"{m7.group(1)}{m7.group(2)}", "text": m7.group(3), "sheet": sheet_no}
            notes.append(current)
            continue
        m = re.match(r"^(\d{1,3})\s*[.)]?\s+(.+)$", text)
        if m and (not notes or int(m.group(1)) in (expected, expected + 1, expected + 2)):
            current = {"label": m.group(1), "text": m.group(2), "sheet": sheet_no}
            notes.append(current)
            expected = int(m.group(1)) + 1
            continue
        sm = re.match(r"^([A-Z])\s*[.)]\s+(.+)$", text)
        if sm and current:
            # "A. PRIOR TO FIRST USE …" is a sub-paragraph of the note above: keep it in that note
            current["text"] += f"\n{sm.group(1)}. {sm.group(2)}"
            continue
        if current:
            current["text"] += " " + text
    if not notes:
        notes = _coded_notes(info)
    if not notes:
        # separate NOTES LIST pages in the package (Boeing / Douglas style)
        from .importers import parse_notes
        pages = [s for s in info.sheets if s.kind == "notes list"]
        if pages:
            text = []
            for s in pages:
                ls = sorted(s.lines, key=lambda l: (round(l.y0 / max(3.0, l.h * 0.6)), l.x0))
                text += [l.text for l in ls]
            notes = parse_notes("\n".join(text))
    for n in notes:
        n.pop("sheet", None)
        from .importers import split_inline_subparagraphs
        n["text"] = split_inline_subparagraphs(
            "\n".join(re.sub(r"\s+", " ", part).strip() for part in n["text"].split("\n")).strip())
    info.notes = notes
    info.boxed_flags = boxed
    labels = {n["label"] for n in notes}
    # flag note locations: standalone note numbers outside the notes / title block
    for s in info.drawing_sheets:
        for l in s.lines:
            if not re.fullmatch(r"\d{1,2}[A-Z]?", l.text) or l.text not in labels:
                continue
            if s.notes_rect and l.inside(s.notes_rect):
                continue
            if s.tb_rect and l.inside(s.tb_rect):
                continue
            if not l.inside(s.frame, pad=-2):
                continue
            below_zone = [o for o in s.lines if ZONE_RE.match(o.text) and 0 < o.y0 - l.y1 < 1.6 * l.h + 4
                          and abs(o.cx - l.cx) < 2 * l.h]
            if below_zone:
                continue                                   # that's a sheet/zone flag, not a note flag
            info.flag_locations[l.text].append((s.index, l.cx, l.cy))


def _coded_notes(info: "DrawingInfo") -> list[dict]:
    """Douglas/Boeing coded notes list: NOTE CODE | text | applicability (*, P, A) per dash-number column."""
    out = []
    for s in [x for x in info.sheets if x.kind == "notes list"]:
        codes = sorted([l for l in s.lines if re.fullmatch(r"[A-Z]{2,3}\d{1,2}", l.text)], key=lambda l: l.y0)
        if len(codes) < 2:
            continue
        cx = statistics.median([c.cx for c in codes])
        codes = [c for c in codes if abs(c.cx - cx) < 3 * c.h]
        dash_hdr = [l for l in s.lines if re.fullmatch(r"-\d{1,4}[A-Z]?", l.text) and l.y1 < codes[0].y0]
        marks = [l for l in s.lines if l.text in ("*", "P", "A", "X")]
        for i, c in enumerate(codes):
            y_end = codes[i + 1].y0 if i + 1 < len(codes) else s.H
            txt = [l for l in s.lines if l.x0 > c.x1 and c.y0 - 2 <= l.y0 < y_end - 1 and l not in marks
                   and not re.search(r"DRAWING NO|APPLICATION|CAGE CODE|LEGENDS ON SHEET", l.text)]
            text = " ".join(l.text for l in sorted(txt, key=lambda l: (l.y0, l.x0)))
            dashes = set()
            for mk in marks:
                if abs(mk.cy - c.cy) < 0.9 * c.h and dash_hdr:
                    col = min(dash_hdr, key=lambda d: abs(d.cx - mk.cx))
                    if abs(col.cx - mk.cx) < 3 * c.h:
                        dashes.add(col.text.lstrip("-"))
            if text:
                out.append({"label": c.text, "text": text, "dashes": sorted(dashes)})
    return out


def analyze_drawing(data: bytes, filename: str, use_ocr: bool = True,
                    progress: Optional[Callable[[str, float], None]] = None) -> DrawingInfo:
    sheets = load_sheets(data, filename, use_ocr, progress)
    info = DrawingInfo(data, filename, sheets)
    for s in sheets:
        sizes = [l.size for l in s.lines if not BANNER_RE.search(l.text)]
        s.med = statistics.median(sizes) if sizes else 10.0
        _zones(s)
        _classify(s)
    for s in info.drawing_sheets:
        _sheet_no(s)
        _title_block(s)
        body = [l.size for l in s.lines if not s.excluded(l) and not BANNER_RE.search(l.text)]
        if len(body) >= 3:
            s.med = statistics.median(body)
    _drawing_number(info)
    for s in info.drawing_sheets:
        _parts_list(s, info)
        _tab_table(s, info)
        _notes_region(s)
    _title_block_fields(info)
    _rule_title_fields(info)
    _tolerances(info)
    _notes(info)
    # dash numbers mentioned anywhere (application lists, parts lists, EOs, view titles)
    if info.drawing_no:
        rx = re.compile(rf"\b{re.escape(info.drawing_no)}\s*-\s*(\d{{1,4}}[A-Z]?)\b", re.I)
        for s in sheets:
            for l in s.lines:
                for m in rx.finditer(l.text.upper()):
                    d = m.group(1)
                    if not (d.startswith("0") and len(d) < 3):
                        info.dashes.add(d)
    for s in info.drawing_sheets:
        s.views = _find_titles(s)
    if progress:
        progress("Analysis complete", 1.0)
    return info


_CACHE: dict[str, DrawingInfo] = {}


def analyze_cached(data: bytes, filename: str, use_ocr: bool = True, progress=None) -> DrawingInfo:
    from . import ocr as _ocr
    from . import rules as _rules
    key = hashlib.sha256(data).hexdigest() + str(use_ocr) + (_ocr.prep_key() if use_ocr else "") + _rules.version_key()
    if key not in _CACHE:
        if len(_CACHE) > 6:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = analyze_drawing(data, filename, use_ocr, progress)
    return _CACHE[key]
