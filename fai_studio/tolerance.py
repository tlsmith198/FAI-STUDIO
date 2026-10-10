"""Requirement parsing and automatic accept/reject evaluation.

parse_requirement("Ø.250 +.002/-.000") -> Parsed(nominal=.25, plus=.002, minus=0, lower=.25, upper=.252)
Handles: ±, +/-, unequal bilateral, limit dimensions, MAX/MIN, GD&T feature
control frames, BASIC / REF dims, counts (4X), diameter / radius prefixes,
angles, and title-block default tolerances by decimal places.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional

GDT_SYMBOLS = {
    "⏊": "Perpendicularity", "⫽": "Parallelism",
    "⌖": "Position", "⏥": "Flatness", "⏤": "Straightness", "○": "Circularity",
    "⌭": "Cylindricity", "⌒": "Profile of a Line", "⌓": "Profile of a Surface",
    "⟂": "Perpendicularity", "⊥": "Perpendicularity", "∠": "Angularity",
    "∥": "Parallelism", "//": "Parallelism", "◎": "Concentricity", "⌯": "Symmetry",
    "↗": "Circular Runout", "⌰": "Total Runout",
}
GDT_WORDS = {
    "POSITION": "Position", "TRUE POSITION": "Position", "TP": "Position",
    "FLATNESS": "Flatness", "FLAT": "Flatness", "STRAIGHTNESS": "Straightness",
    "CIRCULARITY": "Circularity", "ROUNDNESS": "Circularity",
    "CYLINDRICITY": "Cylindricity", "PROFILE": "Profile of a Surface",
    "PERPENDICULARITY": "Perpendicularity", "PERP": "Perpendicularity",
    "ANGULARITY": "Angularity", "PARALLELISM": "Parallelism", "PARALLEL": "Parallelism",
    "CONCENTRICITY": "Concentricity", "SYMMETRY": "Symmetry",
    "RUNOUT": "Circular Runout", "TOTAL RUNOUT": "Total Runout", "FCF": "GD&T",
}
ACCEPT_WORDS = {"OK", "ACC", "ACCEPT", "ACCEPTED", "CONFORMS", "CONFORM", "C", "YES", "Y",
                "PASS", "PASSED", "GOOD", "COMPLIES", "VERIFIED", "SAT", "SATISFACTORY", "✓"}
REJECT_WORDS = {"NC", "NCR", "REJ", "REJECT", "REJECTED", "FAIL", "FAILED", "NO", "N",
                "NONCONFORMING", "NOT OK", "NG", "BAD", "UNSAT", "X", "✗"}

NUM = r"[-+]?(?:\d+\.\d*|\.\d+|\d+)"
_num_re = re.compile(NUM)


@dataclass
class Parsed:
    nominal: Optional[Decimal] = None
    plus: Optional[Decimal] = None
    minus: Optional[Decimal] = None      # stored as positive magnitude
    lower: Optional[Decimal] = None
    upper: Optional[Decimal] = None
    char_type: str = "Dimension"
    reference_only: bool = False
    decimals: int = 3
    bonus: bool = False                  # Ⓜ / Ⓛ on the tolerance: allowed value depends on the actual size

    def as_fields(self) -> dict:
        def f(v):
            return "" if v is None else fmt(v, self.decimals)
        return dict(nominal=f(self.nominal), tol_plus=f(self.plus), tol_minus=f(self.minus),
                    lower_limit=f(self.lower), upper_limit=f(self.upper))


def D(s) -> Optional[Decimal]:
    if s is None:
        return None
    if isinstance(s, Decimal):
        return s
    s = str(s).strip().replace(",", "")
    if not s:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        m = _num_re.search(s)
        if m:
            try:
                return Decimal(m.group(0))
            except InvalidOperation:
                return None
        return None


def decimals_of(s: str) -> int:
    m = re.search(r"\.(\d+)", s or "")
    return len(m.group(1)) if m else 0


def fmt(v: Decimal, decimals: int = 3) -> str:
    if v is None:
        return ""
    q = Decimal(1).scaleb(-max(decimals, 0)) if decimals > 0 else Decimal(1)
    try:
        out = str(v.quantize(q))
    except InvalidOperation:
        out = str(v)
    if out.startswith("0.") and decimals > 0:
        pass
    return out


def _strip_prefix(s: str) -> str:
    s = s.strip()
    s = re.sub(r"^\(?\s*\d+\s*[Xx×]\s*\)?\s*", "", s)             # 4X, (2X)
    s = re.sub(r"^(Ø|⌀|DIA\.?|R|SR|SØ|CR|M)(?=\s*[\d.])", "", s, flags=re.I)
    s = re.sub(r"^↧\s*", "", s)                                    # depth: "↧16.5 MAX"
    s = re.sub(r"^(Ø|⌀|R)(?=\s*[\d.])", "", s.strip())
    return s.strip()


def parse_requirement(text: str, defaults: Optional[dict] = None, units: str = "in") -> Parsed:
    """defaults: {'tol_1dec','tol_2dec','tol_3dec','tol_4dec','tol_angle'} as strings."""
    p = Parsed()
    if not text or not text.strip():
        return p
    raw = text.strip()
    raw = re.sub(r"^[A-Z]{1,2}\s*=\s*", "", raw)                 # tabulated "B = 5.400"
    raw = re.sub(r"\s*\((?:TAB|DASH)[^)]*\)\s*$", "", raw)
    up = raw.upper()
    core = re.sub(r"^\(?\s*\d+\s*[Xx×]\s*\)?\s*", "", raw)          # "2X [4.400]" -> "[4.400]"
    p.decimals = max(decimals_of(raw), 0)

    # Reference / basic: no acceptance limits
    if re.search(r"\bREF\b", up) or (core.startswith("(") and core.endswith(")")):
        p.reference_only = True
        p.char_type = "Reference"
        n = _num_re.search(core)
        p.nominal = D(n.group(0)) if n else None
        return p
    if re.search(r"\bBSC\b|\bBASIC\b", up) or (core.startswith("[") and core.endswith("]")):
        p.reference_only = True
        p.char_type = "Basic"
        n = _num_re.search(core)
        p.nominal = D(n.group(0)) if n else None
        return p

    # GD&T feature control frame -> tolerance zone, lower = 0
    gtype = None
    for sym, name in GDT_SYMBOLS.items():
        if sym in raw:
            gtype = name
            break
    if not gtype:
        for w, name in sorted(GDT_WORDS.items(), key=lambda kv: -len(kv[0])):
            if re.match(rf"^\s*\|?\s*{re.escape(w)}\b", up):
                gtype = name
                break
    if gtype:
        p.char_type = "GD&T"
        body = re.sub(r"^\(?\s*\d+\s*[Xx×]\s*\)?\s*", "", raw)          # strip 4X
        m = re.search(r"[Ø⌀]?\s*(" + NUM + ")", body)
        if m:
            tol = D(m.group(1))
            p.nominal = Decimal(0)
            p.plus = tol
            p.minus = Decimal(0)
            p.lower = Decimal(0)
            p.upper = tol
            p.decimals = max(decimals_of(m.group(1)), 3)
            # Ⓜ / Ⓛ right after the tolerance value (not on a datum): bonus tolerance. The stated value is
            # the limit at MMC/LMC - results inside it are good; results over it need the bonus worked out.
            if has_bonus(body):
                p.bonus = True
        return p

    is_angle = "°" in raw or re.search(r"\bDEG\b", up) is not None
    if is_angle:
        p.char_type = "Angle"
    body = _strip_prefix(raw.replace("°", "").replace("DEG", "").replace("deg", ""))
    body = body.replace("+/-", "±").replace("+-", "±")
    body = re.sub(r"\s+", " ", body)
    if re.search(r"\bTHD\b|UNC|UNF|UNEF|UNJ|UNS\b|NPT|#\d+-\d+|\d-\d+\s*UN|\bM\d+(\.\d+)?\s*X", up):
        # thread callouts: treat as attribute characteristic
        p.char_type = "Thread"
        return p

    # MAX / MIN
    m = re.match(rf"^({NUM})\s*(MAX|MIN)\b", body, re.I)
    if m:
        v = D(m.group(1))
        p.decimals = decimals_of(m.group(1))
        if m.group(2).upper() == "MAX":
            p.upper = v
            p.nominal = v
        else:
            p.lower = v
            p.nominal = v
        return p

    # ±
    m = re.match(rf"^({NUM})\s*±\s*({NUM})", body)
    if m:
        n, t = D(m.group(1)), abs(D(m.group(2)))
        p.decimals = max(decimals_of(m.group(1)), decimals_of(m.group(2)))
        p.nominal, p.plus, p.minus = n, t, t
        p.lower, p.upper = n - t, n + t
        return p

    # unequal bilateral: 1.250 +.005/-.002  |  1.250 +.005 -.002  | 1.250 -.002 +.005
    m = re.match(rf"^({NUM})\s*([+-]\s*{NUM})\s*/?\s*([+-]\s*{NUM})", body)
    if m:
        n = D(m.group(1))
        a = D(m.group(2).replace(" ", ""))
        b = D(m.group(3).replace(" ", ""))
        plus = max(a, b)
        minus = min(a, b)
        p.decimals = max(decimals_of(m.group(1)), decimals_of(m.group(2)), decimals_of(m.group(3)))
        p.nominal = n
        p.plus, p.minus = plus, abs(minus)
        p.lower, p.upper = n + minus, n + plus
        return p

    # limit dimension: 1.245-1.255  / 1.245/1.255 / 1.255 1.245 stacked
    m = re.match(rf"^({NUM})\s*(?:-|/|–|TO)\s*({NUM})$", body, re.I)
    if m and not m.group(2).startswith(("-", "+")):
        a, b = D(m.group(1)), D(m.group(2))
        lo, hi = min(a, b), max(a, b)
        p.decimals = max(decimals_of(m.group(1)), decimals_of(m.group(2)))
        p.lower, p.upper = lo, hi
        p.nominal = (lo + hi) / 2
        p.plus = hi - p.nominal
        p.minus = p.nominal - lo
        return p

    # Plain number (optionally "THRU", "DEEP", "HEX" …) -> title block default tolerance
    m = re.match(rf"^({NUM})(?:\s+[A-Z][A-Z .]*)?$", body)
    if m:
        n = D(m.group(1))
        p.nominal = n
        dec = decimals_of(m.group(1))
        p.decimals = dec
        tol = None
        if defaults:
            key = "tol_angle" if is_angle else {0: "tol_0dec", 1: "tol_1dec", 2: "tol_2dec", 3: "tol_3dec"}.get(
                dec, "tol_4dec" if dec >= 4 else None)
            if key and defaults.get(key):
                tol = D(defaults.get(key))
        if tol is not None:
            p.plus = p.minus = tol
            p.lower, p.upper = n - tol, n + tol
            p.decimals = max(dec, decimals_of(str(tol)))
        return p

    # Anything else is an attribute / note requirement
    if len(body) > 18 or not _num_re.search(body):
        p.char_type = "Note"
    return p


def split_results(results: str) -> list[str]:
    if not results:
        return []
    return [t for t in re.split(r"[;,\s/]+", results.strip()) if t]


def has_bonus(requirement: str) -> bool:
    """Feature control frame with Ⓜ / Ⓛ on the tolerance value (bonus tolerance applies)."""
    return bool(requirement) and bool(re.search(r"[Ø⌀]?\s*" + NUM + r"\s*[ⓂⓁ]", requirement)) and \
        any(ch in requirement for ch in GDT_SYMBOLS)


def evaluate(results: str, lower: str, upper: str, bonus: bool = False) -> str:
    """Return 'Accept', 'Reject' or '' (undetermined).
    bonus=True (MMC/LMC frames): over the stated tolerance is left for the inspector, never auto-rejected."""
    if not results or not results.strip():
        return ""
    txt = results.strip().upper()
    if txt in ACCEPT_WORDS:
        return "Accept"
    if txt in REJECT_WORDS:
        return "Reject"
    lo, hi = D(lower), D(upper)
    tokens = split_results(results)
    values = []
    for t in tokens:
        if _num_re.fullmatch(t.replace("Ø", "")):
            values.append(D(t.replace("Ø", "")))
        else:
            tu = t.upper()
            if tu in REJECT_WORDS:
                return "Reject"
            if tu in ACCEPT_WORDS:
                continue
            return ""
    if not values:
        return "Accept" if all(t.upper() in ACCEPT_WORDS for t in tokens) else ""
    if lo is None and hi is None:
        return ""
    for v in values:
        if lo is not None and v < lo:
            return "Reject"
        if hi is not None and v > hi:
            return "" if bonus else "Reject"
    return "Accept"


def out_of_tolerance_values(results: str, lower: str, upper: str, bonus: bool = False) -> list[str]:
    lo, hi = D(lower), D(upper)
    if bonus:
        hi = None                                      # over the MMC value may still be good with bonus
    bad = []
    for t in split_results(results):
        v = D(t) if _num_re.fullmatch(t) else None
        if v is None:
            continue
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            bad.append(t)
    return bad


def recompute_limits(nominal: str, plus: str, minus: str, decimals_hint: int = 3) -> tuple[str, str]:
    n, a, b = D(nominal), D(plus), D(minus)
    if n is None:
        return "", ""
    dec = max(decimals_hint, decimals_of(nominal), decimals_of(plus or ""), decimals_of(minus or ""))
    lo = n - abs(b) if b is not None else None
    hi = n + a if a is not None else None
    return (fmt(lo, dec) if lo is not None else "", fmt(hi, dec) if hi is not None else "")


SPEC_RE = re.compile(
    r"\b(AMS-QQ-[A-Z]-\d{2,4}[A-Z]?(?:/\d+)?|AMS[- ]?(?:STD|H)?[- ]?\d{3,5}[A-Z]?|MIL[- ][A-Z]{1,4}[- ]\d{2,6}[A-Z]?(?:/\d+)?|"
    r"ASTM[- ]?[A-Z]\d{1,4}(?:/[A-Z]\d+)?|QQ-[A-Z]-\d{2,4}(?:/\d+)?|NAS\s?\d{2,5}|MS\d{4,6}|AN\d{2,4}|"
    r"SAE[- ]?(?:AMS|AS|J)\s?\d{2,5}|AS\d{4,6}|BAC\s?\d{3,5}|DPS\s?\d[\d.]+|STM\d+|"
    r"LMA-[A-Z]{2}\d{3,5}|PS\s?\d{3,6}|ISO\s?\d{3,5}|A-A-\d{4,6}|FED-STD-\d{2,4}|SPX-\d{5,9}|TT-[A-Z]-\d{2,5}[A-Z]?|"
    r"ASME\s+Y14\.\d+M?|ASME\s+B\d+\.\d+M?|AS\d{3}-\d+[A-Z]\d*)\b", re.I)


def find_specs(text: str) -> list[str]:
    seen, out = set(), []
    for m in SPEC_RE.finditer(text or ""):
        s = m.group(1).upper()
        if s not in seen:
            seen.add(s)
            out.append(s)
    try:                                   # customer spec patterns from the Reader Rules library
        from .rules import extra_spec_patterns
        for rx, _cat in extra_spec_patterns():
            for m in rx.finditer(text or ""):
                s = (m.group(1) if m.groups() else m.group(0)).upper().strip()
                if s and s not in seen:
                    seen.add(s)
                    out.append(s)
    except Exception:
        pass
    return out


def classify_spec(note_text: str) -> str:
    u = (note_text or "").upper()
    if any(w in u for w in ("ANODIZ", "PASSIVAT", "PLAT", "HEAT TREAT", "CHEM FILM", "ALODINE",
                              "CONVERSION COAT", "PAINT", "PRIME", "SHOT PEEN", "NDT", "PENETRANT",
                              "MAG PARTICLE", "WELD", "BRAZ", "COAT", "HARDEN", "NITRID")):
        return "Special Process"
    if any(w in u for w in ("TEST", "PRESSURE", "LEAK", "FUNCTIONAL")):
        return "Functional Test"
    if any(w in u for w in ("MATERIAL", "ALLOY", "BAR", "PLATE", "SHEET", "FORGING", "CASTING",
                              "TI-6AL", "6061", "7075", "17-4", "304", "316", "INCONEL")):
        return "Material"
    return "Other"
