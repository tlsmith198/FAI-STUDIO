"""Customer-ballooned drawings (SpaceX and others that put their own characteristic numbers on the print).

SpaceX drawings carry a small diamond with a number next to every inspectable element: ◇1, ◇2 … ◇98.
Those numbers ARE the characteristic numbers the customer expects on the inspection report, so instead of
auto-numbering we:
  1. find every numbered diamond on each sheet (vector PDF: four short diagonal strokes around a number),
  2. read the callouts on the sheet with the normal extractor (with the balloon numbers taken out first),
  3. give each diamond the callout element it sits against (left/right on the same row, or just above/below),
     splitting "M3X0.5-6H ↧10 MIN" style callouts when the thread and the depth carry separate balloons,
  4. report any numbers in the sequence that were not found, so nothing is missed.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Optional

import pymupdf


@dataclass
class CustBalloon:
    num: int
    page: int
    rect: tuple            # diamond / hexagon outline (page points)
    text_rect: tuple       # the number itself
    double: bool = False   # drawn with a double outline

    @property
    def cx(self):
        return (self.rect[0] + self.rect[2]) / 2

    @property
    def cy(self):
        return (self.rect[1] + self.rect[3]) / 2


# ----------------------------------------------------------------------------------------- detection
def _segments(page) -> list[tuple]:
    """Short straight strokes (x0, y0, x1, y1) from the page's vector graphics."""
    out = []
    try:
        drawings = page.get_drawings()
    except Exception:
        return out
    for g in drawings:
        if g.get("type") not in ("s", "fs"):
            continue
        for it in g["items"]:
            if it[0] == "l":
                p, q = it[1], it[2]
                L = math.hypot(q.x - p.x, q.y - p.y)
                if 2.0 <= L <= 30.0:
                    out.append((p.x, p.y, q.x, q.y))
            elif it[0] == "re":
                r = it[1]
                for a, b in ((r.tl, r.tr), (r.tr, r.br), (r.br, r.bl), (r.bl, r.tl)):
                    L = math.hypot(b.x - a.x, b.y - a.y)
                    if 2.0 <= L <= 30.0:
                        out.append((a.x, a.y, b.x, b.y))
    return out


def find_balloons(page, page_index: int = 0) -> list[CustBalloon]:
    """Numbered diamonds ◇n on a vector PDF page."""
    words = [w for w in page.get_text("words") if re.fullmatch(r"\d{1,3}", w[4])]
    if not words:
        return []
    segs = _segments(page)
    if not segs:
        return []
    # bucket segments by midpoint for quick lookup
    cell = 20.0
    grid: dict[tuple, list] = {}
    for s in segs:
        mx, my = (s[0] + s[2]) / 2, (s[1] + s[3]) / 2
        grid.setdefault((int(mx // cell), int(my // cell)), []).append(s)

    def near(cx, cy, r):
        out = []
        for gx in range(int((cx - r) // cell), int((cx + r) // cell) + 1):
            for gy in range(int((cy - r) // cell), int((cy + r) // cell) + 1):
                out += grid.get((gx, gy), [])
        return out

    found = []
    for w in words:
        x0, y0, x1, y1 = w[:4]
        h = max(y1 - y0, 1.0)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        R = 1.9 * h + (x1 - x0) / 2
        cand = []
        for s in near(cx, cy, R):
            if not all(abs(px - cx) <= R and abs(py - cy) <= 0.92 * h for px, py in ((s[0], s[1]), (s[2], s[3]))):
                continue
            dx, dy = s[2] - s[0], s[3] - s[1]
            L = math.hypot(dx, dy)
            cand.append((s, abs(dy) / L if L else 0))
        diag = [s for s, sl in cand if 0.3 <= sl <= 0.95]
        if len(diag) < 3:
            continue
        quads = set()
        for s in diag:
            mx, my = (s[0] + s[2]) / 2, (s[1] + s[3]) / 2
            quads.add((mx > cx, my > cy))
        if len(quads) < 4 and not (len(quads) == 3 and len(diag) >= 3):
            continue
        xs = [v for s in diag for v in (s[0], s[2])]
        ys = [v for s in diag for v in (s[1], s[3])]
        rect = (min(xs), min(ys), max(xs), max(ys))
        rw, rh = rect[2] - rect[0], rect[3] - rect[1]
        # the number must sit in the middle of the shape, and the shape must be number-sized
        if not (rect[0] < x0 and rect[2] > x1 and rect[1] < cy < rect[3]):
            continue
        if rw > 4.5 * h + (x1 - x0) or rh > 2.6 * h or rh < 0.9 * h:
            continue
        # diamonds and flat-topped hexagons are both balloons; a two-row view-reference tag ("3" over "C6")
        # has a horizontal divider across its middle -> not a balloon
        divider = [s for s, sl in cand if sl < 0.1 and abs(s[2] - s[0]) > 0.35 * rw
                   and rect[0] - 1 <= min(s[0], s[2]) and max(s[0], s[2]) <= rect[2] + 1
                   and rect[1] + 0.25 * rh < s[1] < rect[3] - 0.25 * rh]
        if divider:
            continue
        # double outline (a second, larger shape around the first)
        double = len(diag) >= 8
        b = CustBalloon(int(w[4]), page_index, rect, tuple(w[:4]))
        b.double = double
        found.append(b)
    # the same number can be drawn twice (text + outline); keep one per position
    uniq: list[CustBalloon] = []
    for b in found:
        if not any(u.num == b.num and abs(u.cx - b.cx) < 3 and abs(u.cy - b.cy) < 3 for u in uniq):
            uniq.append(b)
    return uniq


def find_all(data: bytes) -> dict[int, list[CustBalloon]]:
    out: dict[int, list[CustBalloon]] = {}
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception:
        return out
    with doc:
        for i, page in enumerate(doc):
            bl = find_balloons(page, i)
            if bl:
                out[i] = bl
    return out


def is_preballooned(balloons: dict[int, list[CustBalloon]]) -> bool:
    nums = [b.num for bl in balloons.values() for b in bl]
    if len(nums) < 5:
        return False
    # a real characteristic sequence: mostly unique numbers covering a good part of 1..max
    uniq = set(nums)
    return len(uniq) >= 0.8 * len(nums) and len(uniq) >= 0.5 * max(uniq)


# ----------------------------------------------------------------------------------------- matching
def _dist(b: CustBalloon, box: tuple, h: float) -> float:
    """How far a balloon is from a callout, in text heights. Balloons sit on the same row (left/right)
    or in the same column (above/below) as the element they number."""
    x0, y0, x1, y1 = box
    gx = max(x0 - b.rect[2], b.rect[0] - x1, 0.0)
    gy = max(y0 - b.rect[3], b.rect[1] - y1, 0.0)
    if y0 - 0.25 * h <= b.cy <= y1 + 0.25 * h:          # same row
        return gx / h
    if x0 <= b.cx <= x1:                                 # directly above / below
        return gy / h + 0.6
    return (math.hypot(gx, gy) / h) + 1.5                # diagonal: allowed, but only as a last resort


def _parts(c) -> list[tuple[str, tuple]]:
    """Split a callout that carries several balloonable elements: 'M3X0.5-6H ↧ 9 MIN' -> thread, depth."""
    req = c.requirement
    if "↧" not in req or req.strip().startswith("↧"):
        return []
    i = req.index("↧")
    head, tail = req[:i].strip(), req[i:].strip()
    tail = re.sub(r"\s+(HOLES?|LOCATIONS?|SLOTS?)\s+LABELED\s+\w+.*$", "", tail)
    head_mods = re.search(r"\s+((?:HOLES?|LOCATIONS?|SLOTS?)\s+LABELED\s+\w+.*)$", req)
    if head_mods:
        head = f"{head} {head_mods.group(1)}"
    x0, y0, x1, y1 = c.bbox
    frac = i / max(len(req), 1)
    xm = x0 + frac * (x1 - x0)
    return [(head, (x0, y0, xm, y1)), (tail, (xm, y0, x1, y1))]


def match(info, page: int, balloons: list[CustBalloon], rect: Optional[tuple] = None):
    """Customer-numbered candidates for one sheet.
    Returns (candidates with .char_no set, unmatched callout candidates)."""
    import copy

    from .drawing_intel import TL, _extract, _mk
    s = info.sheets[page]
    if rect is not None:
        balloons = [b for b in balloons if rect[0] <= b.cx <= rect[2] and rect[1] <= b.cy <= rect[3]]
    if not balloons:
        return [], []
    # take the balloon numbers (and anything inside the diamonds) out of the text before reading callouts
    def in_balloon(l):
        return any(b.rect[0] - 0.5 <= l.cx <= b.rect[2] + 0.5 and b.rect[1] - 0.5 <= l.cy <= b.rect[3] + 0.5
                   for b in balloons)
    saved = s.lines
    try:
        s.lines = [l for l in saved if not in_balloon(l)]
        cands = _extract(info, s, None, {}, None, ignore_excl=True)
    finally:
        s.lines = saved
    dn = (getattr(info, "drawing_no", "") or "").upper()
    cands = [c for c in cands if not (dn and dn in c.requirement.upper())
             and not re.fullmatch(r"\d+\s+(PLACES?|LOCATIONS?|SURFACES?|SIDES?)\b.*", c.requirement)]
    h = sorted(b.rect[3] - b.rect[1] for b in balloons)[len(balloons) // 2]
    # element list: whole callouts + split parts
    elems = []                     # (cand index, part index or -1, box)
    for ci, c in enumerate(cands):
        parts = _parts(c)
        if parts:                  # thread + depth: each element can carry its own balloon
            for pi, (_, box) in enumerate(parts):
                elems.append((ci, pi, box))
        else:
            elems.append((ci, -1, c.bbox))
    pairs = []
    for bi, b in enumerate(balloons):
        for ei, (ci, pi, box) in enumerate(elems):
            d = _dist(b, box, h)
            if cands[ci].char_type == "Reference":
                d += 1.5           # reference values ((Ø4.2), (1210)) are normally not ballooned
            if d <= 4.0:
                pairs.append((d, bi, ei))
    pairs.sort()
    taken_b: dict[int, int] = {}
    use_count: dict[int, list] = {}
    # pass 1: one balloon per element, closest first; pass 2: extra balloons may share an element
    used_e: set = set()
    for pass_no in (1, 2):
        for d, bi, ei in pairs:
            if bi in taken_b:
                continue
            ci, pi, _ = elems[ei]
            if pass_no == 1 and ei in used_e:
                continue
            taken_b[bi] = ei
            used_e.add(ei)
            use_count.setdefault(ci, []).append((bi, pi))
    out = []
    sn = s.sheet_no or (page + 1)
    for ci, users in use_count.items():
        c = cands[ci]
        parts = _parts(c)
        if parts and len(users) == 1:
            assign = [(users[0][0], -1)]            # one balloon for the whole thread + depth callout
        elif parts:
            assign = list(users)                    # each balloon has its own element (thread / depth)
        else:
            assign = [(bi, -1) for bi, _ in users]
        for bi, pi in assign:
            b = balloons[bi]
            nc = copy.copy(c)
            if pi >= 0:
                nc.requirement, box = parts[pi]
                nc.bbox = box
                nc.char_type = "Dimension" if nc.requirement.lstrip().startswith("↧") else "Thread"
            _customer(nc, b, s, sum(1 for u in users if u[1] == pi) > 1)
            out.append(nc)
    # balloons that found no readable callout: keep them (accountability), with the nearest text
    for bi, b in enumerate(balloons):
        if bi in taken_b:
            continue
        near = [l for l in s.lines if not in_balloon(l) and re.search(r"[\dA-Z]", l.text)
                and _dist(b, (l.x0, l.y0, l.x1, l.y1), h) <= 3.0]
        near.sort(key=lambda l: _dist(b, (l.x0, l.y0, l.x1, l.y1), h))
        if near:
            l = near[0]
            req, box = l.text, (l.x0, l.y0, l.x1, l.y1)
        else:
            req, box = "", b.rect
        l = TL(req or "?", *box, src=s.src)
        nc = _mk(s, l, req, "Note" if not re.search(r"\d", req) else "Dimension", "", sn, "")
        _customer(nc, b, s, False)
        nc.comment = "Callout not read automatically – type the requirement from the drawing"
        out.append(nc)
    unmatched = [c for ci, c in enumerate(cands) if ci not in use_count]
    for c in unmatched:
        c.selected = False
        c.comment = "Not ballooned on the customer drawing"
    return out, unmatched


def _customer(c, b: CustBalloon, s, shared: bool):
    c.char_no = b.num
    c.customer_balloon = True
    # our balloon goes exactly on top of the customer's diamond
    c.x, c.y = b.cx / s.W, b.cy / s.H
    c.bsize = 0.55 * max(b.rect[2] - b.rect[0], b.rect[3] - b.rect[1]) / max(s.W, s.H)   # covers their diamond
    bx = (min(c.bbox[0], b.rect[0]), min(c.bbox[1], b.rect[1]), max(c.bbox[2], b.rect[2]), max(c.bbox[3], b.rect[3]))
    c.nbbox = (bx[0] / s.W, bx[1] / s.H, bx[2] / s.W, bx[3] / s.H)
    c.rot = 0
    c.vertical = False
    c.selected = True
    if c.char_type == "Reference":
        c.char_type = "Dimension"
    if shared:
        c.comment = "Shares a callout with another balloon – check which element this number is"
    if getattr(b, "double", False):
        c.comment = ("Double-outlined balloon on the drawing – check the customer's requirements for its meaning. "
                     + (c.comment or "")).strip()


def missing_numbers(nums: list[int]) -> list[int]:
    if not nums:
        return []
    have = set(nums)
    return [n for n in range(1, max(have) + 1) if n not in have]
