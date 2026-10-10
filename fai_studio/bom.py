"""Parts lists, BOMs and notes lists -> one parts tree (who is made from / built from what, and which
materials, processes and notes apply to which part number).

The Certificate of Conformance uses the tree so a CoC for a single detail lists only that detail's material
(including the stock or forging it is made from) and its outside processing, and a CoC for an assembly walks
down through every detail and sub-detail.

Readers (text PDFs are read from the text layer; scanned / image-only pages are OCR'd):
  * Lockheed Martin "Engineering Parts List" (EPL / CEBOM) and "Engineering BOM with Associated Data"
    - table sections: Structure Assembly, BOM Documents, Material Stock Size, PMP specifications, Vendor Data,
      Notes / Material Drawing Notes.
  * Boeing PDM "PART LIST" (heading / application / part list / notes / reference documents): QTY column per
    dash number ('*' = the dash's own row, a number = used in that dash), make-from, stock size, material, spec,
    supplier.
  * Boeing / Douglas coded NOTES LISTs (NL) and PDM notes pages: note code, text and applicability marks per
    dash column (* P A B).  On scans the marks are found from the ink in each dash column.
  * Boeing APPLICATION LISTs (AL): configuration part -> next assembly.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import statistics
from dataclasses import dataclass, field
from typing import Callable, Optional

PN_RE = re.compile(r"^(?=.*\d)[A-Z0-9][A-Z0-9./]*(?:-[A-Z0-9./]+)*$")
DASH_RE = re.compile(r"^-\d{1,4}[A-Z]?$")
CODE_RE = re.compile(r"^(?=.*[A-Z])[0-9A-Z]{3}[0-9A-Z]$")
RAW_RE = re.compile(r"\b(SHEET|STRIP|PLATE|BAR|ROD|FORGING|EXTRUSION|EXTRUDED|TUBE|TUBING|CASTING|BILLET|"
                    r"WIRE|ALLOY|ALUMINUM|TITANIUM|STEEL|CRES|INCONEL|BRONZE|BRASS|COPPER|MAGNESIUM|PLASTIC|"
                    r"PHENOLIC|DELRIN|NYLON|PEEK|ULTEM|HONEYCOMB|LAMINATE|FABRIC|PREPREG|STOCK)\b", re.I)
CONSUMABLE_RE = re.compile(r"\b(COATING|PRIMER|PAINT|INK|THINNER|ADHESIVE|SEALANT|LUBRICANT|SOLVENT|COMPOUND|"
                           r"TOPCOAT|ENAMEL|EPOXY|POLYURETHANE|FILM|TAPE|SILICA|CLEANER|RESIN|CATALYST)\b", re.I)


# ============================================================================== data model
@dataclass
class Item:
    pn: str
    name: str = ""
    cage: str = ""
    uom: str = ""
    buy: Optional[bool] = None          # True = purchased, False = made here, None = unknown
    stock: str = ""                     # stock size (".050 X 3 X 7.5")
    matl: str = ""                      # material description ("SHEET, AL 6061-T6")
    spec: str = ""                      # material spec ("AMS4027 TEMP T6")
    supplier: str = ""                  # supplier name (and address lines)
    make_from: str = ""                 # part number of the forging / stock it is made from
    comments: str = ""
    category: str = ""                  # PMP category for Lockheed material ids
    vendors: list = field(default_factory=list)
    source: str = ""


@dataclass
class Proc:
    kind: str                           # Process | Test
    ident: str                          # spec / document number
    text: str                           # what it is ("PRIMER APPLICATION")
    applies: set                        # part numbers it applies to
    source: str = ""


@dataclass
class Note:
    label: str
    text: str
    applies: set                        # part numbers; empty set = the whole list (general note)
    source: str = ""
    partial: bool = False


class Bom:
    def __init__(self):
        self.items: dict[str, Item] = {}
        self.children: dict[str, dict[str, str]] = {}     # parent -> {child: qty}
        self.notes: list[Note] = []
        self.procs: list[Proc] = []
        self.lists: set[str] = set()                       # list / drawing numbers ("99X5678")
        self.tops: set[str] = set()                        # part numbers a list is *for* (LM EPL header)
        self.sources: list[str] = []
        self.kinds: set[str] = set()

    # ---------------------------------------------------------------- building
    def item(self, pn: str, **kw) -> Item:
        pn = norm_pn(pn)
        it = self.items.get(pn)
        if it is None:
            it = self.items[pn] = Item(pn)
        for k, v in kw.items():
            if v in (None, "", []):
                continue
            if k == "vendors":
                it.vendors += [x for x in v if x not in it.vendors]
            elif not getattr(it, k):
                setattr(it, k, v)
        return it

    def link(self, parent: str, child: str, qty: str = ""):
        parent, child = norm_pn(parent), norm_pn(child)
        if parent and child and parent != child:
            self.children.setdefault(parent, {})
            if qty or child not in self.children[parent]:
                self.children[parent][child] = qty

    def merge(self, other: "Bom") -> "Bom":
        for pn, it in other.items.items():
            self.item(pn, **{k: getattr(it, k) for k in it.__dataclass_fields__ if k != "pn"})
        for p, ch in other.children.items():
            for c, q in ch.items():
                self.link(p, c, q)
        seen = {(n.label, n.text[:60]) for n in self.notes}
        for n in other.notes:
            key = (n.label, n.text[:60])
            if key in seen:
                for m in self.notes:
                    if (m.label, m.text[:60]) == key:
                        m.applies |= n.applies
                continue
            self.notes.append(n)
        self.procs += other.procs
        self.lists |= other.lists
        self.tops |= other.tops
        self.sources += other.sources
        self.kinds |= other.kinds
        return self

    # ---------------------------------------------------------------- queries
    def resolve(self, pn: str) -> Optional[str]:
        """Find the part number in the tree (tolerant of spaces / 'P/N' prefixes / missing leading zeros)."""
        p = norm_pn(pn)
        if not p:
            return None
        known = set(self.items) | set(self.children) | self.tops | {c for ch in self.children.values() for c in ch}
        if p in known:
            return p
        for k in known:
            if k.replace("-", "") == p.replace("-", ""):
                return k
        return None

    def scope(self, pn: str) -> list[str]:
        """The part and everything below it: children (recursively) and what each is made from."""
        root = self.resolve(pn) or norm_pn(pn)
        out, todo, seen = [], [root], set()
        while todo:
            p = todo.pop(0)
            if p in seen:
                continue
            seen.add(p)
            out.append(p)
            it = self.items.get(p)
            if it and it.make_from:
                todo.append(norm_pn(it.make_from))
            todo += sorted(self.children.get(p, {}), key=_pn_sort)
        return out

    def index_rows(self, pn: str, include_stock: bool = False) -> list[dict]:
        """AS9102 Form 1 index of part numbers for assembly `pn`: every sub-assembly, detail, make-from stock and
        purchased part below it, depth first, with level, quantity per its parent and a short description."""
        root = self.resolve(pn) or norm_pn(pn)
        out: list[dict] = []
        seen: set = set()

        def is_assy(c):
            ch = set(self.children.get(c, {}))
            ci = self.items.get(c)
            if ci is not None and ci.make_from:
                ch.discard(ci.make_from)
            return bool(ch)

        def kind_of(p, c):
            it = self.items.get(p)
            ci = self.items.get(c) or Item(c)
            txt = f"{ci.name} {ci.category}"
            if is_assy(c):
                return "Assembly"
            if it is not None and it.make_from == c:
                return "Make-from stock"
            if ci.uom.lower().startswith("a") and CONSUMABLE_RE.search(txt):
                return "Consumable"
            if ci.uom.lower().startswith("a") and RAW_RE.search(txt):
                return "Raw material"
            if (ci.stock or ci.matl or ci.make_from or any(c.startswith(l) for l in self.lists)) and ci.buy is not True:
                return "Detail"
            return "Purchased"
        order = {"Assembly": 0, "Detail": 1, "Purchased": 2, "Make-from stock": 3, "Raw material": 4, "Consumable": 5}

        def walk(p, level, path):
            kids = [(c, q, kind_of(p, c)) for c, q in self.children.get(p, {}).items()]
            kids = [k for k in kids if include_stock or k[2] not in ("Make-from stock", "Raw material", "Consumable")]
            kids.sort(key=lambda k: (order.get(k[2], 9), _pn_sort(k[0])))
            for i, (c, q, kind) in enumerate(kids, start=1):
                if (p, c) in seen:
                    continue
                seen.add((p, c))
                ci = self.items.get(c) or Item(c)
                mat = " ".join(x for x in (ci.stock if ci.stock.upper() != "NOTED" else "", ci.matl) if x)
                if ci.spec:
                    mat += f" PER {ci.spec}"
                if ci.make_from:
                    mat = (mat + " " if mat else "") + f"(make from {ci.make_from})"
                item_no = f"{path}.{i}" if path else str(i)
                out.append({"level": level, "item_no": item_no, "part_number": c, "part_name": ci.name,
                            "qty": q or "", "parent": p, "kind": kind, "material": mat.strip(),
                            "cage": ci.cage})
                if kind == "Assembly" and level < 12:
                    walk(c, level + 1, item_no)
        walk(root, 1, "")
        return out

    def parent_of(self, pn: str) -> list[str]:
        return [p for p, ch in self.children.items() if pn in ch]

    def summary(self) -> str:
        return (f"{', '.join(sorted(self.kinds)) or 'BOM'}: {len(self.items)} part(s), "
                f"{sum(len(c) for c in self.children.values())} link(s), {len(self.notes)} note(s), "
                f"{len(self.procs)} process / spec document(s)")


def norm_pn(pn: str) -> str:
    p = re.sub(r"\s+", "", str(pn or "")).upper().strip(".,;:")
    p = re.sub(r"^(P/N|PN)[:#]?", "", p)
    return p


def _pn_sort(p):
    m = re.search(r"-(\d+)", p)
    return (int(m.group(1)) if m else 0, p)


# ============================================================================== OCR text clean-up
def fix_caps(t: str) -> str:
    """Engineering lists are all capitals; OCR mixes case ('MAY Be USEd') - normalise."""
    letters = [ch for ch in t if ch.isalpha()]
    if letters and sum(ch.isupper() for ch in letters) >= 0.4 * len(letters):
        return t.upper()
    return t.upper() if len(t) <= 3 else t


_VOCAB = None


def _vocab() -> set:
    global _VOCAB
    if _VOCAB is None:
        base = """A AN AND ANY AS AT BE BY FOR FROM IN IS IT NO NOT OF ON OR PER SEE SO THE TO WITH WITHOUT
        MAY SHALL USED USE SUBJECT RESTRICTIONS RESTRICTION NOTE NOTES WHERE WHEN GOVERNMENT INDUSTRY
        SPECIFICATION SPECIFICATIONS STANDARD STANDARDS SHOWN ADDITION DOUGLAS MATERIAL MATERIALS PROCESS
        PROCESSES BINDING DAC SUBCONTRACTORS SUBCONTRACTOR FABRICATING ASSEMBLING PARTS PART DESIGN
        REQUIREMENTS REQUIREMENT TAG BAG NUMBER NUMBERS IDENTIFY IDENTIFICATION INK STAMP STAMPING LOCATION
        OPTIONAL DIMENSIONAL TOLERANCES TOLERANCE SYMBOLS ASSEMBLY SHOP PRACTICE PRACTICES INSTALL RIVETS
        RIVET FLUSH SCREWS SCREW HOLES SLOTS PUNCHED FULL SIZE MACHINE NYLON CONCAVITY EDGES EDGE PERMISSIBLE
        SULFURIC CHROMIC ACID ANODIZE ANODIZING SEAL SEALED DICHROMATE TYPE CLASS PRIME PRIMER PRIMED GLOSS
        WHITE BLACK GRAY GREY EPOXY COAT COATING COATED TOPCOAT ENAMEL PAINT FINISH FINISHED PENETRANT
        MAGNETIC PARTICLE ULTRASONIC INSPECT INSPECTION INSPECTED NONDESTRUCTIVE PROCEDURE APPROVAL SURFACE
        TREAT TREATMENT TREATED CRES PASSIVATE PASSIVATION HEAT CADMIUM PLATE PLATED PLATING NICKEL CHROME
        ZINC CONVERSION CHEMICAL FILM ALODINE BOND BONDING BONDED ADHESIVE SEALANT SEALING FAY FAYING WET
        INSTALLED INSTALLATION BEARING BEARINGS BUSHING BUSHINGS STAKE STAKED NUTPLATE NUTPLATES FASTENER
        FASTENERS BOLT BOLTS NUT NUTS WASHER WASHERS PIN PINS SPACER SPACERS AFTER BEFORE PRIOR ALL EXCEPT
        UNLESS OTHERWISE SPECIFIED NOTED INDICATED FEATURE FEATURES INTERFACE KEY CHARACTERISTIC MARKED
        MARK MARKING SYMBOL ADJACENT DRAWING DRAWINGS SHEET SHEETS FIELD APPLICATION APPLICABLE ITEM ITEMS
        SHALL THIS THAT THESE THOSE ARE WILL ONLY ALSO BOTH EACH SIDE SIDES NEAR FAR AREA AREAS BREAK SHARP
        REMOVE BURRS RADIUS RADII MAX MIN MAXIMUM MINIMUM INCH INCHES DEPTH LENGTH WIDTH THICKNESS GRAIN
        DIRECTION FORM FORMED FORMING BEND BENDS RELIEF RELIEFS JOGGLE ROUGHNESS MICROINCHES DATA BASE
        DATABASE CAD CAM MASTER DEFINITION DIMENSIONS DIMENSION MODELED MODEL PRECISION NUMERIC IMPLY
        TOUCH UP REQUIRED ORGANIC INORGANIC CORROSION RESISTANT PREVENTIVE COMPOUND LUBRICANT DRY SOLID
        APPLY APPLIED CURE CURED TEMPERATURE ALUMINUM ALLOY STEEL TITANIUM ALCLAD SEAM WELD WELDING BRAZE
        PILOT DRILL DRILLED REAM REAMED COUNTERSINK COUNTERBORE THREAD THREADS THREADED INSERT INSERTS
        HELICAL COIL LOCKING SELF ATTACH SUPPLEMENT FOLLOWING DOCUMENTS DOCUMENT REFERENCE ONLY CONTINUED
        PROTECT PROTECTION PROTECTIVE PACKAGE PACKAGING SHIPPING HANDLING CLEAN CLEANING SOLVENT WIPE
        BETWEEN WITHIN OVER UNDER ABOVE BELOW INTO ONTO UPON THRU THROUGH ITS THEIR THEM THEY OTHER SAME
        TRUE POSITION FLATNESS PARALLEL PERPENDICULAR CONCENTRIC SYMMETRIC DATUM DATUMS TARGET TARGETS
        DN LUSTERLESS POLYURETHANE FED STD PRIOR EXCEED FINISHED REFERENCED RESTRAINED CONDITION HOLES
        NICKEL BASE ALLOYS FABRICATE ROLL THREAD TEXTURE CENTER ACCEPTANCE TESTS SAMPLES TESTED STRAIN AGE
        HARDENED SHOULDER GRAIN FOLLOW OUTLINE MEASURED SHANK DIAMETER MAJOR MINOR LESS THAN EDGES AFTER
        CUTTING SEALING CONCAVITY BELTING HIGH FLEX PLY PRODUCT ALTERNATE SUPERSEDED STOCK LONG IN
        HOLLOW MEMBER MEMBERS METHOD SOLUTION FABRICATION BAR BARS SHEET PLATE ROD TUBE FORGING CASTING
        EXTRUSION ANNEALED TEMPER AGED DRAWN ROLLED COLD HOT CONDITION HARDNESS TENSILE PROOF LOAD TEST
        TESTING CERTIFIED CERTIFICATION CONFORMANCE DPS DMS PRIMERS PROCEDURES INSPECTIONS
        BRONZE FATIGUE QUALIFIED RELIEVED ALUMINUM HINGE SUPPORT CRANK BRACKET FITTING ANGLE CLIP LUG
        SHAPE EXTRUDED SUBSTITUTE THICKNESS"""
        _VOCAB = set(base.split()) | _SPEC_PREFIX
    return _VOCAB


_SPEC_PREFIX = {"DPS", "DMS", "AMS", "MIL", "STD", "QQ", "TYPE", "CLASS", "GRADE", "NAS", "ANSI", "ASTM", "SAE", "BMS",
                "MAP", "TT", "FED", "DN", "FR", "UNS", "SPEC"}


def split_words(text: str) -> str:
    """Re-insert spaces OCR dropped between capital words ('TAGORBAGANDTAGWITH' -> 'TAG OR BAG AND TAG WITH')."""
    V = _vocab()

    def seg(tok):
        n = len(tok)
        best = [None] * (n + 1)
        best[0] = []
        for i in range(n):
            if best[i] is None:
                continue
            for j in range(i + 1, min(n, i + 16) + 1):
                w = tok[i:j]
                if w in V and (best[j] is None or len(best[i]) + 1 < len(best[j])):
                    best[j] = best[i] + [w]
        return best[n]

    def fix_tok(tok):
        core = tok.strip(".,;:()")
        if len(core) < 7 or not core.isupper() or core in V:
            return tok
        chunks = re.findall(r"[A-Z]+|[^A-Z]+", core)
        outc = []
        for ch in chunks:
            if ch.isalpha() and len(ch) >= 7 and ch not in V:
                parts = seg(ch)
                outc.append(" ".join(parts) if parts and len(parts) > 1 else ch)
            else:
                outc.append(ch)
        # 'DN7507GLOSS' -> 'DN7507 GLOSS', 'PER4.707' stays (spec numbers keep their prefix)
        res = ""
        for i, ch in enumerate(outc):
            if i and res and res[-1].isdigit() and ch[:1].isalpha() and (ch.split(" ")[0] in V and len(ch.split(" ")[0]) >= 3):
                res += " "
            elif i and res and res[-1].isalpha() and ch[:1].isdigit() and res.split(" ")[-1] in V and \
                    len(res.split(" ")[-1]) >= 4 and res.split(" ")[-1] not in _SPEC_PREFIX:
                res += " "
            res += ch
        return tok.replace(core, res)
    return " ".join(fix_tok(t) for t in text.split(" "))


_DIGITISH = str.maketrans({"O": "0", "o": "0", "Q": "0", "D": "0", "B": "8", "I": "1", "l": "1", "|": "1",
                          "S": "5", "Z": "2"})


def _fix_number(tok: str) -> str:
    """'4.3O1' -> '4.301', '4.BO6' -> '4.806': a token that is a number apart from look-alike letters."""
    if re.fullmatch(r"[\d.][\d.\-OoQDBIl|SZ]*", tok) and re.search(r"\d", tok) and re.search(r"[\d]", tok[:3]) \
            and sum(ch.isalpha() for ch in tok) <= max(1, len(tok) // 2) \
            and (tok[-1].isdigit() or not re.search(r"[A-Za-z]{2}", tok)):
        return tok.translate(_DIGITISH)
    return tok


def _fix_word(tok: str) -> str:
    """Scanned text: 'METH0D' -> 'METHOD', 'TREATEO' -> 'TREATED', 'HOLLUW' -> 'HOLLOW' (closest known word)."""
    import difflib
    V = _vocab()
    if re.fullmatch(r"[A-Z]+0[A-Z0]*", tok) and len(tok) >= 4:
        tok = tok.replace("0", "O")
    if tok in ("IHE", "TNE", "THF", "TIIE"):
        return "THE"
    m = re.fullmatch(r"([A-Z]+)I\.([A-Z]{2,})", tok)                 # 'SOI.UTION' -> 'SOLUTION'
    if m and f"{m.group(1)}L{m.group(2)}" in V:
        return f"{m.group(1)}L{m.group(2)}"
    if not tok.isalpha() or not tok.isupper() or len(tok) < 4 or tok in V:
        return tok
    cands = difflib.get_close_matches(tok, [v for v in V if abs(len(v) - len(tok)) <= 1], n=2,
                                      cutoff=0.75 if len(tok) <= 5 else 0.8)
    if cands and (len(cands) == 1 or difflib.SequenceMatcher(None, tok, cands[0]).ratio() >
                  difflib.SequenceMatcher(None, tok, cands[1]).ratio() + 0.05):
        return cands[0]
    return tok


def repair_specs(text: str) -> str:
    """The safe part of repair_ocr, for text of unknown origin: only spec numbers and glued 'PERDPS'."""
    if not text:
        return text
    t = text.replace("，", ", ").replace("；", "; ")
    t = re.sub(r"\bPER(?=(DPS|DMS|AMS|BMS|MIL-|QQ-)\s?\d?)", "PER ", t)
    t = re.sub(r"\b(DN|BMS)(\d[\dOBIl]{2,})", lambda m: m.group(1) + _fix_number(m.group(2)), t)
    return re.sub(r"\b(DPS|DMS|AMS|BMS|BAC(?=\s?\d)|MIL-[A-Z]+|QQ-[A-Z])-?\s?(\d[\dOBIl.\-]*|[OBIl][\dOBIl.\-]*\d)",
                  lambda m: f"{m.group(1)}{'-' if m.group(1).startswith(('MIL', 'QQ')) else ' '}"
                            f"{_fix_number(m.group(2))}", t)


def repair_ocr(text: str) -> str:
    """Clean OCR text from old scanned lists so it prints properly on a certificate: look-alike digits in
    numbers and spec numbers, misread letters in known words, full-width punctuation, glued spec prefixes."""
    if not text:
        return text
    t = text.replace("，", ", ").replace("；", "; ").replace("：", ": ")
    t = re.sub(r"\bPER(?=(DPS|DMS|AMS|BMS|MIL-|QQ-)\s?\d?)", "PER ", t)
    t = re.sub(r"\b(DPS|DMS|AMS|BMS|BAC(?=\s?\d)|MIL-[A-Z]+|QQ-[A-Z])-?\s?(\d[\dOBIl.\-]*|[OBIl][\dOBIl.\-]*\d)",
               lambda m: f"{m.group(1)}{'-' if m.group(1).startswith(('MIL', 'QQ')) else ' '}{_fix_number(m.group(2))}",
               t)
    t = re.sub(r"(\d\.\d+)\.(?=[A-Z]{2})", r"\1. ", t)                     # 'DPS9.07.QQ-P-35' -> '... 9.07. QQ'
    t = re.sub(r"(\d)(MAY\s?BE|SHALL|AND|WITH|PER)\b", r"\1 \2", t)
    t = re.sub(r"\bMAYBE\b", "MAY BE", t)
    t = re.sub(r"^((?:\S+\s+){2,4})\S+\s+\1", r"\1", t)             # OCR read the start twice
    t = re.sub(r"\b(BAR|SHEET|PLATE|ROD|TUBE|ALUMINUM|STEEL|CRES)\.\s?(?=[A-Z])", r"\1, ", t)
    t = re.sub(r"\bTEMPER\s?T(\d)", r"TEMPER T\1", re.sub(r"\bTEMP\s?T(\d)", r"TEMP T\1", t))
    t = t.replace("×", " X ")
    out = []
    for tok in t.split(" "):
        m = re.match(r"^([(\[]*)(.*?)([.,;:)\]]*)$", tok)
        pre, core, post = m.group(1), m.group(2), m.group(3)
        if re.fullmatch(r"[A-Z]+I\.[A-Z]{2,}", core):
            core = _fix_word(core)
        elif "." in core and not re.search(r"[A-Z]{2}", core):
            core = _fix_number(core)
        else:
            parts = core.split("-")
            core = "-".join(_fix_number(_fix_word(pt)) if pt else pt for pt in parts)
        out.append(pre + core + post)
    return re.sub(r"\s{2,}", " ", " ".join(out)).strip()


# ============================================================================== page access (text or OCR)
@dataclass
class W:
    x0: float
    y0: float
    x1: float
    y1: float
    t: str

    @property
    def cx(self):
        return (self.x0 + self.x1) / 2

    @property
    def cy(self):
        return (self.y0 + self.y1) / 2

    @property
    def h(self):
        return self.y1 - self.y0


class PageData:
    """Words + tables for one PDF page, from the text layer or (scans) from OCR at `dpi`."""

    def __init__(self, page, data_hash: str, ocr_ok: bool = True, dpi: int = 300):
        self.page = page
        self.hash = data_hash
        self.no = page.number
        self.W, self.H = page.rect.width, page.rect.height
        self.text = page.get_text()
        self.scanned = _is_scanned(page, self.text)
        self._ocr = None
        self._img = None
        # never render more than ~5200 px on the long side (big drawing sheets would exhaust memory)
        self.dpi = max(72, min(dpi, int(5200 * 72 / max(self.W, self.H, 1))))
        self.ocr_ok = ocr_ok
        self.rot = 0                         # sideways scans: page image turned upright by this (PIL, CCW)

    # ---- words
    def words(self) -> list[W]:
        if not self.scanned:
            return [W(*w[:5]) for w in self.page.get_text("words")]
        return self.ocr_words()

    def ocr_words(self) -> list[W]:
        if self._ocr is None:
            self._ocr = _ocr_page(self) if self.ocr_ok else []
        s = 72.0 / self.dpi
        ws = [W(a * s, b * s, c * s, d * s, t.upper().strip()) for a, b, c, d, t in self._ocr if t.strip()]
        return _dedupe_words(ws)

    def image(self):
        """Grayscale page image (numpy), turned upright for sideways scans."""
        if self._img is None:
            import numpy as np
            from PIL import Image
            pix = self.page.get_pixmap(dpi=self.dpi, alpha=False)
            im = Image.open(io.BytesIO(pix.tobytes("png"))).convert("L")
            if self.rot:
                im = im.rotate(self.rot, expand=True)
            self._img = np.array(im)
            if self.rot in (90, 270):
                self.W, self.H = self.page.rect.height, self.page.rect.width
        return self._img

    def full_text(self) -> str:
        if not self.scanned:
            return self.text
        ws = self.ocr_words()
        return "\n".join(w.t for w in sorted(ws, key=lambda w: (round(w.y0 / 4), w.x0))) + "\n" + self.text

    # ---- tables
    def tables(self) -> list[list[list[str]]]:
        if not self.scanned:
            out = []
            try:
                for t in self.page.find_tables().tables:
                    out.append([[_clean(c) for c in r] for r in t.extract()])
            except Exception:
                pass
            return out
        return _image_tables(self)


def _dedupe_words(ws: list[W]) -> list[W]:
    """Overlapping OCR tiles read the same line twice or in two overlapping pieces: keep one read / stitch
    the pieces ('DASH NUM' + 'DASHNUMBER PER' -> 'DASH NUMBER PER')."""
    ws = sorted(ws, key=lambda w: -len(w.t))
    keep: list[W] = []
    for w in ws:
        area = max(1e-6, (w.x1 - w.x0) * (w.y1 - w.y0))
        dup = False
        for k in keep:
            ix = max(0.0, min(w.x1, k.x1) - max(w.x0, k.x0))
            iy = max(0.0, min(w.y1, k.y1) - max(w.y0, k.y0))
            if ix * iy > 0.6 * area and (w.t in k.t or len(w.t) <= 2 and w.t.isalnum()):
                dup = True
                break
        if not dup:
            keep.append(w)
    # stitch horizontally overlapping pieces of one line
    keep.sort(key=lambda w: w.x0)
    out: list[W] = []
    for w in keep:
        host = None
        for i, prev in enumerate(out):
            if abs(prev.cy - w.cy) < 0.45 * max(prev.h, w.h) and prev.x0 < w.x0 < prev.x1 - 2 and w.x1 > prev.x1 \
                    and len(w.t) > 2 and len(prev.t) > 2:
                host = i
                break
        if host is None:
            out.append(w)
            continue
        prev = out[host]
        a, b = prev.t, w.t
        a_ns, b_ns = a.replace(" ", ""), b.replace(" ", "")
        k = 0
        for n in range(min(len(a_ns), len(b_ns)), 2, -1):
            if a_ns.endswith(b_ns[:n]):
                k = n
                break
        if not k:
            out.append(w)                      # no common text: two separate words, leave both
            continue
        cnt, i = 0, 0
        while i < len(b) and cnt < k:
            if b[i] != " ":
                cnt += 1
            i += 1
        rest = b[i:]
        out[host] = W(prev.x0, min(prev.y0, w.y0), w.x1, max(prev.y1, w.y1), (a + rest).strip())
    return out


def _clean(c) -> str:
    return re.sub(r"[ \t]+", " ", str(c or "").replace("\n", " ")).strip()


def _clean_scan(c) -> str:
    return split_words(_clean(c).upper())


def _is_scanned(page, text: str) -> bool:
    body = re.sub(r"(Boeing Proprietary|Document Category|Export License|Export Jurisdiction|Document Number|"
                  r"Dash Number|Type:\w+|REISSUE|Name:.*?revision\.|Not Valid After.*|ID:|REV:\S*)", "", text)
    if len(re.sub(r"\s", "", body)) > 120:
        return False
    return bool(page.get_images())


def _ocr_page(pd: PageData) -> list[tuple]:
    """OCR a scanned page (cached on disk). Sideways scans are detected from the OCR itself and read again
    upright; pd.rot / pd.W / pd.H then describe the upright page and all boxes are in upright pixels."""
    from . import ocr
    from .config import cache_dir
    if not ocr.available():
        return []
    key = hashlib.sha1(f"{pd.hash}:{pd.no}:{pd.dpi}:bom2".encode()).hexdigest()
    cf = cache_dir() / f"bom_{key}.json"
    if cf.exists():
        try:
            d = json.loads(cf.read_text())
            if d.get("rot"):
                pd.rot = d["rot"]
                pd._img = None
                pd.image()
            return [tuple(x) for x in d["words"]]
        except Exception:
            pass
    from PIL import Image
    im = Image.fromarray(pd.image())
    res = [(a, b, c, d, t) for a, b, c, d, t, _k in ocr.ocr_image(im.convert("RGB"))]
    chars = sum(len(t) for *_x, t in res) or 1
    vert = sum(len(t) for a, b, c, d, t in res if len(t) > 2 and (d - b) > 1.5 * (c - a))
    if vert > 0.45 * chars:
        best = None
        for rot in (90, 270):
            r = im.rotate(rot, expand=True)
            w, h = r.size
            probe = ocr.ocr_image(r.crop((0, 0, w, int(h * 0.35))).convert("RGB"), tile=2400)
            sc = sum(k * len(t) for *_b, t, k in probe if (_b[2] - _b[0]) >= (_b[3] - _b[1]))
            if best is None or sc > best[0]:
                best = (sc, rot)
        pd.rot = best[1]
        pd._img = None
        im = Image.fromarray(pd.image())
        res = [(a, b, c, d, t) for a, b, c, d, t, _k in ocr.ocr_image(im.convert("RGB"))]
    try:
        cf.write_text(json.dumps({"rot": pd.rot, "words": res}))
    except Exception:
        pass
    return res


def _vrules(pd: PageData, y0: float, y1: float, x0: float = 0.0, x1: Optional[float] = None) -> list[float]:
    """x (points) of vertical ruling lines crossing the band y0..y1 on a scanned page (optionally only between
    x0 and x1 - one sheet of a two-up scan, so a faint sheet is judged on its own)."""
    img = pd.image()
    s = pd.dpi / 72.0
    xa = max(0, int(x0 * s))
    band = img[max(0, int(y0 * s)):int(y1 * s), xa:(int(x1 * s) if x1 else None)]
    if band.size == 0:
        return []
    dark = band < 150
    # old scans: rules are faint / broken - allow one pixel of wobble by OR-ing neighbouring columns
    import numpy as np
    wob = dark.copy()
    wob[:, 1:] |= dark[:, :-1]
    wob[:, :-1] |= dark[:, 1:]
    frac = wob.mean(axis=0)
    thr = 0.8 if (frac > 0.8).sum() >= 4 else 0.55
    xs = [i for i, f in enumerate(frac) if f > thr]
    out, run = [], []
    for x in xs:
        if run and x - run[-1] > 2:
            out.append(sum(run) / len(run) / s)
            run = []
        run.append(x)
    if run:
        out.append(sum(run) / len(run) / s)
    return [x + xa / s for x in out]


# ============================================================================== image tables (scanned EBOMs)
def _image_tables(pd: PageData) -> list[list[list[str]]]:
    import numpy as np
    try:
        import cv2
    except Exception:
        return []
    g = pd.image()
    H, Wd = g.shape
    bw = (g < 170).astype("uint8") * 255
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, Wd // 40), 1))
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(12, H // 70)))
    hl = cv2.morphologyEx(bw, cv2.MORPH_OPEN, hk)
    vl = cv2.morphologyEx(bw, cv2.MORPH_OPEN, vk)
    grid = cv2.dilate(hl | vl, np.ones((3, 3), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(grid)
    s = pd.dpi / 72.0
    sc = pd.dpi / 72.0
    words = [(w.x0 * sc, w.y0 * sc, w.x1 * sc, w.y1 * sc, w.t) for w in pd.ocr_words()]
    out = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if w < Wd * 0.25 or h < 20:
            continue
        sub_h = hl[y:y + h, x:x + w] > 0
        sub_v = vl[y:y + h, x:x + w] > 0
        rows_y = _line_pos(sub_h.sum(axis=1), w * 0.5)
        cols_x = _line_pos(sub_v.sum(axis=0), h * 0.35)
        if len(rows_y) < 2 or len(cols_x) < 2:
            continue
        rows_y = [y + r for r in rows_y]
        cols_x = [x + c for c in cols_x]
        table = []
        for r0, r1 in zip(rows_y, rows_y[1:]):
            if r1 - r0 < 8:
                continue
            row = []
            for c0, c1 in zip(cols_x, cols_x[1:]):
                if c1 - c0 < 6:
                    continue
                cell = [wd for wd in words if c0 - 2 <= (wd[0] + wd[2]) / 2 <= c1 + 2 and r0 - 2 <= (wd[1] + wd[3]) / 2 <= r1 + 2]
                row.append(" ".join(wd[4] for wd in sorted(cell, key=lambda q: (round(q[1] / 6), q[0]))).strip())
            row = [_clean_scan(c) for c in row]
            if any(row):
                table.append(row)
            elif table:
                table.append(row)
        if table:
            out.append((y, table))
    return [t for _y, t in sorted(out, key=lambda q: q[0])]


def _line_pos(profile, thresh) -> list[int]:
    pos, run = [], []
    for i, v in enumerate(profile):
        if v >= thresh:
            run.append(i)
        elif run:
            pos.append(int(sum(run) / len(run)))
            run = []
    if run:
        pos.append(int(sum(run) / len(run)))
    return pos


# ============================================================================== detection
def detect(pages: list[PageData]) -> str:
    t = "\n".join(p.text for p in pages[:6])
    tu = t.upper()
    if "ENGINEERING PARTS LIST" in tu and "STRUCTURE ASSEMBLY" in tu or "COMPONENT PART" in tu and "UOM" in tu:
        return "lm_epl"
    if "ENGINEERING BOM WITH ASSOCIATED DATA" in tu:
        return "lm_ebom"
    if "LIST TYPE" in tu and "THE BOEING COMPANY" in tu:
        return "boeing_pdm"
    if "APPLICATION LIST" in tu or "NOTES LIST" in tu and "NOTE" in tu and "CODE" in tu:
        return "boeing_nl"
    if re.search(r"Type:(PL|NL|AL)\b", t):
        return "boeing_scan"
    # scanned: look at OCR of the first page - only for list-sized pages (up to 11 x 17), never drawing sheets
    if pages and pages[0].scanned and max(pages[0].W, pages[0].H) <= 1300:
        ot = pages[0].full_text().upper()
        if "ENGINEERING BOM" in ot or "ENGINEERING PARTS LIST" in ot:
            return "lm_ebom"
        if "NOTES LIST" in ot or "PARTS LIST" in ot or "APPLICATION LIST" in ot:
            return "boeing_scan"
    return ""


# ============================================================================== Lockheed EPL / EBOM
LM_SECTIONS = [
    ("structure", [("QTY", "QPA"), ("COMPONENT PART", "PART NUMBER", "PART"), ("NAME", "NOMENCLATURE")]),
    ("documents", [("DOCUMENT", "DOCUMENT NUMBER"), ("NAME", "NOMENCLATURE"), ("CAGE",)]),
    ("stock", [("MATERIAL ID",), ("HEIGHT",), ("WIDTH",)]),
    ("pmp", [("MAT ID/PART ID/PROC ID", "MATL ID/PART ID/PROC ID"), ("CATEGORY",), ("SPECIFICATION",)]),
    ("vendors", [("VENDOR NAME",), ("LOCATION",)]),
    ("matnotes", [("MATERIAL ID",), ("DRAWING NOTE",)]),
    ("notes", [("NOTE NO", "NOTE NUMBER"), ("NOTE TEXT",)]),
    ("nextassy", [("PARENT PART NUMBER",), ("NAME",)]),
    ("general", [("ASSEMBLY MODE",), ("TRACEABILITY",)]),
    ("refdocs", [("NUMBER",), ("CAGE",), ("NAME",)]),
]


def _is_raw(it: Item) -> bool:
    """Raw stock the part is made from (bought 'as required'), not hardware or a consumable."""
    u = re.sub(r"[\s\-]", "", (it.uom or "").lower())
    asreq = u.startswith("ar") or u.startswith("asr") or u in ("in", "ft", "lb", "sqft", "sq ft")
    txt = f"{it.name} {it.category}"
    return asreq and bool(RAW_RE.search(txt)) and not CONSUMABLE_RE.search(txt)


def _hdr_kind(row: list[str]) -> Optional[str]:
    cells = [re.sub(r"\s+", "", c.upper()) for c in row]
    for kind, need in LM_SECTIONS:
        if all(any(any(c == a.replace(" ", "") or c.startswith(a.replace(" ", "")) for a in alts) for c in cells)
               for alts in need):
            if kind == "structure" and not any("UOM" in c or "QTY" == c or "QPA" == c for c in cells):
                continue
            if kind == "documents" and any(c in ("QPA", "UOM", "QTY") for c in cells):
                continue
            if kind == "refdocs" and (len(cells) > 3 or any("DOCUMENT" in c for c in cells)):
                continue
            return kind
    return None


def _lm_sections(pages: list[PageData]):
    """Yield (kind, header, rows) for every recognised table, stitching tables continued on the next page."""
    last = None
    for pd in pages:
        for t in pd.tables():
            rows = [r for r in t if any(r)]
            # drop the page banner row(s) that find_tables glues onto continued tables
            while rows and re.search(r"EXPORT CONTROLLED|ENGINEERING PARTS LIST|ENGINEERING BOM", " ".join(rows[0]).upper()):
                rows = rows[1:]
            rows = [r for r in rows if not re.match(r"^PAGE \d+ OF \d+", (r[0] or "").upper())]
            if not rows:
                continue
            k = _hdr_kind(rows[0])
            if k:
                last = (k, [c.upper() for c in rows[0]])
                yield k, last[1], rows[1:]
            elif last and len(rows[0]) == len(last[1]):
                yield last[0], last[1], rows


def _col(hdr: list[str], *names) -> Optional[int]:
    H = [re.sub(r"\s+", "", c.upper()) for c in hdr]
    for n in names:
        n = n.replace(" ", "")
        for i, c in enumerate(H):
            if c == n:
                return i
    for n in names:
        n = n.replace(" ", "")
        for i, c in enumerate(H):
            if c.startswith(n):
                return i
    for n in names:                               # OCR-damaged header ('PART MEER'): first word match
        n0 = n.split()[0]
        if len(n0) >= 4:
            for i, c in enumerate(hdr):
                if c.upper().split()[:1] == [n0]:
                    return i
    return None


def _get(r, i):
    return (r[i] if i is not None and i < len(r) else "") or ""


def parse_lm(pages: list[PageData], name: str) -> Bom:
    b = Bom()
    b.kinds.add("Lockheed parts list")
    b.sources.append(name)
    text = "\n".join(p.full_text() if p.scanned and p.no == 0 else p.text for p in pages[:1])
    m = re.search(r"Part\s*Number:\s*([A-Z0-9\-./]+)", text, re.I) or \
        re.search(r"\bPL\s*([0-9]{3}[A-Z]{2}[0-9]{4,}-[0-9A-Z]+)", text) or re.search(r"\bPL\s+([0-9][A-Z0-9\-]+)", text)
    top = norm_pn(m.group(1)) if m else ""
    nm = re.search(r"NAME:\s*(.+?)(?:\s{2,}|CONTRACT|\n)", text, re.I)
    if top:
        b.tops.add(top)
        b.lists.add(re.sub(r"-\d+[A-Z]?$", "", top))
        b.item(top, name=(nm.group(1).strip() if nm else ""), buy=False, source=name)
    stock, pmp, vend = {}, {}, {}
    for kind, hdr, rows in _lm_sections(pages):
        if kind == "structure":
            ci = {k: _col(hdr, *v) for k, v in dict(q=("QTY", "QPA"), uom=("UOM",), pn=("COMPONENT PART", "PART NUMBER"),
                                                        find=("FIND NUM", "FIND NO", "FIND"),
                                                        name=("NAME", "NOMENCLATURE OR DESCRIPTION", "NOMENCLATURE"),
                                                        cage=("CAGE", "CAGE CODE"), src=("SOURCE",),
                                                        com=("COMMENTS", "MATERIAL NOTES", "SHORT NOTES")).items()}
            for r in rows:
                pn = norm_pn(_get(r, ci["pn"]))
                if not pn or not re.search(r"\d", pn):
                    continue
                uom = _get(r, ci["uom"]).lower()
                src = _get(r, ci["src"]).lower()
                b.item(pn, name=_get(r, ci["name"]), cage=_get(r, ci["cage"]), uom=uom,
                       buy=(src == "buy") if src else None, comments=_get(r, ci["com"]), source=name)
                if top:
                    b.link(top, pn, _get(r, ci["q"]))
        elif kind == "documents":
            ci = {k: _col(hdr, *v) for k, v in dict(doc=("DOCUMENT NUMBER", "DOCUMENT"),
                                                        name=("NOMENCLATURE OR DESCRIPTION", "NAME", "NOMENCLATURE"),
                                                        short=("SHORT NOTES",)).items()}
            for r in rows:
                d = _get(r, ci["doc"]).strip()
                if d:
                    b.procs.append(Proc("", d, _get(r, ci["name"]), {top} if top else set(), f"{name}: BOM document"))
        elif kind == "stock":
            for r in rows:
                if r and r[0]:
                    dims = [x for x in r[1:4] if x]
                    stock[norm_pn(r[0])] = (" X ".join(dims), r[4] if len(r) > 4 else "")
        elif kind == "pmp":
            ci = {k: _col(hdr, *v) for k, v in dict(id=("MAT ID/PART ID/PROC ID", "MATL ID/PART ID/PROC ID"),
                                                        cat=("CATEGORY",), spec=("SPECIFICATION",), frm=("FROM",),
                                                        temper=("TEMPER",), mods=("SELECTED SPEC MODS",)).items()}
            for r in rows:
                i = norm_pn(_get(r, ci["id"]))
                if i:
                    pmp.setdefault(i, dict(cat=_get(r, ci["cat"]), spec=_get(r, ci["spec"]), frm=_get(r, ci["frm"]),
                                           temper=_get(r, ci["temper"]), mods=[]))
                    md = _get(r, ci["mods"])
                    if md and md not in pmp[i]["mods"]:
                        pmp[i]["mods"].append(md)
        elif kind == "vendors":
            ci = {k: _col(hdr, *v) for k, v in dict(id=("MATL ID/PART ID/PROC ID", "MAT ID/PART ID/PROC ID"),
                                                        code=("VENDOR MATL CODE/PART NO",), name=("VENDOR NAME",),
                                                        loc=("LOCATION",), cage=("CAGE",), phone=("PHONE",)).items()}
            for r in rows:
                i = norm_pn(_get(r, ci["id"]))
                if i and _get(r, ci["name"]):
                    vend.setdefault(i, []).append(dict(name=_get(r, ci["name"]), location=_get(r, ci["loc"]),
                                                       cage=_get(r, ci["cage"]), code=_get(r, ci["code"]),
                                                       phone=_get(r, ci["phone"])))
        elif kind in ("notes", "matnotes"):
            for r in rows:
                if len(r) >= 2 and r[-1].strip():
                    lab = r[0].strip() if kind == "notes" else (r[1].strip() or r[0].strip())
                    b.notes.append(Note(lab, r[-1].strip(), {top} if top else set(), f"{name}: note"))
        elif kind == "nextassy":
            for r in rows:
                p = norm_pn(_get(r, 0))
                if p and top and re.search(r"\d", p):
                    b.link(p, top, _get(r, _col(hdr, "QTY")))
    # attach stock size, PMP spec and vendors to the material / part ids
    for pn, it in b.items.items():
        if pn in stock:
            it.stock = stock[pn][0]
            if stock[pn][1] and not it.comments:
                it.comments = stock[pn][1]
        if pn in pmp:
            p = pmp[pn]
            it.category = p["cat"]
            if p["spec"] and p["spec"].upper() != "COMMERCIAL":
                it.spec = p["spec"] + (f" ({', '.join(p['mods'])})" if p["mods"] else "")
            mat = " ".join(x for x in (p["frm"], p["cat"], p["temper"]) if x and x.upper() not in ("LIQUID",))
            if _is_raw(it):
                it.matl = mat
        if pn in vend:
            it.vendors = vend[pn]
    for pr in b.procs:
        p = pmp.get(norm_pn(pr.ident))
        if p and p["cat"]:
            pr.text = p["cat"] + (f" — {pr.text}" if pr.text and pr.text.upper() != p["cat"].upper() else "")
    return b


# ============================================================================== Boeing PDM part list
def parse_boeing_pdm(pages: list[PageData], name: str) -> Bom:
    b = Bom()
    b.kinds.add("Boeing parts list")
    b.sources.append(name)
    seen_sheets = set()
    for pd in pages:
        t = pd.text
        m = re.search(r"LIST NO\s*\n\s*(\S+)", t) or re.search(r"Document Number:\s*(\S+)", t)
        lst = norm_pn(m.group(1)) if m else ""
        if lst:
            b.lists.add(lst)
        sh = re.search(r"SHEET\s*\n\s*(\d+)\s+OF\s+(\d+)", t)
        key = (lst, sh.group(1) if sh else pd.no, sh.group(2) if sh else "")
        if key in seen_sheets:                      # the same list printed twice in one package
            continue
        seen_sheets.add(key)
        if "Type:DWG" in t and "PART LIST" not in t:
            continue
        words = pd.words()
        up = t.upper()
        if "STOCK SIZE" in up and "PART NO" in up:
            _pdm_partlist(b, words, lst, name)
        elif "NOTE CODE" in up or "APPLICABILITY INDICATORS" in up:
            _coded_notes(b, pd, words, lst, name)
        if "APPLICATION" in up and "NEXT ASSY" in up:
            _pdm_application(b, pd, lst)
        if "IDENTIFYING NO" in up:
            _pdm_refdocs(b, words, name)
    return b


def _hdr_x(words, *seq) -> Optional[W]:
    """First word starting the header phrase `seq` (e.g. 'MATL','DESC')."""
    for i, w in enumerate(words):
        if w.t.upper() == seq[0]:
            ok = True
            for j, s in enumerate(seq[1:], start=1):
                nxt = [v for v in words if v.t.upper() == s and abs(v.cy - w.cy) < 3 and 0 < v.x0 - w.x1 < 40]
                if not nxt:
                    ok = False
                    break
            if ok:
                return w
    return None


def _phrases(ln: list[W], gap: float = 7.0) -> list[W]:
    """Join words separated by normal spaces into phrases (cells)."""
    out: list[W] = []
    for w in sorted(ln, key=lambda w: w.x0):
        if out and w.x0 - out[-1].x1 <= gap and abs(w.cy - out[-1].cy) < 4:
            p = out[-1]
            out[-1] = W(p.x0, min(p.y0, w.y0), w.x1, max(p.y1, w.y1), p.t + " " + w.t)
        else:
            out.append(W(w.x0, w.y0, w.x1, w.y1, w.t))
    return out


def _hdr_phrase(words, first, *rest) -> Optional[W]:
    w = _hdr_x(words, first, *rest)
    if w is None:
        return None
    ph = [p for p in _phrases([v for v in words if abs(v.cy - w.cy) < 3], 5) if p.x0 <= w.x0 + 0.1 <= p.x1 + 0.1]
    return ph[0] if ph else w


def _pdm_partlist(b: Bom, words: list[W], lst: str, name: str):
    stock_h = _hdr_phrase(words, "STOCK")
    mdesc = _hdr_phrase(words, "MATL", "DESC")
    mspec = _hdr_phrase(words, "MATL", "SPEC")
    note_h = _hdr_x(words, "NOTE", "CODE")
    used = _hdr_x(words, "USED", "ON")
    part_h = _hdr_x(words, "PART", "NO.")
    if not (stock_h and mdesc and mspec and part_h):
        return
    hdr_bottom = (used.y1 if used else part_h.y1) + 2
    dashes = sorted([w for w in words if DASH_RE.match(w.t) and w.y1 < hdr_bottom and w.x1 < part_h.x0], key=lambda w: w.x0)
    if not dashes:
        return
    step = statistics.median([b2.cx - a.cx for a, b2 in zip(dashes, dashes[1:])]) if len(dashes) > 1 else 28
    centers = {"stock": stock_h.x0 + 25, "matl": mdesc.cx, "spec": mspec.cx}
    note_x = note_h.x0 - 6 if note_h else 10 ** 6
    stop = [w.y0 for w in words if w.y0 > hdr_bottom and (w.t in ("PRINTED", "***") or w.t.startswith("Document"))]
    y_stop = min(stop + [10 ** 6])
    body = [w for w in words if hdr_bottom < w.y0 < y_stop - 1]
    lines = _lines(body)
    blocks: list[tuple[Item, list, list]] = []           # (item, pn line, detail lines)
    for ln in lines:
        pnw = [w for w in ln if part_h.x0 - 40 <= w.x0 <= part_h.x0 + 10 and len(w.t) >= 4 and PN_RE.match(w.t.upper())]
        if pnw:
            pn = norm_pn(pnw[0].t)
            nm = " ".join(w.t for w in ln if pnw[0].x1 + 3 < w.x0 < note_x)
            cage = " ".join(w.t for w in ln if pnw[0].x0 - 90 < w.x0 < pnw[0].x0 - 8 and re.fullmatch(r"[0-9A-Z]{5}", w.t))
            it = b.item(pn, name=nm, cage=cage, source=name)
            blocks.append((it, ln, []))
            qline = [w for w in lines_near(lines, ln, 5) if w.x1 < part_h.x0 - 20]
            for d in dashes:
                hit = [w for w in qline if abs(w.cx - d.cx) < step * 0.45]
                if not hit:
                    continue
                v = hit[0].t
                parent = norm_pn(f"{lst}{d.t}") if lst else ""
                if not parent or parent == pn:
                    continue
                if v == "*":
                    b.link(parent, pn, "1")                # tabulated: the row is the dash itself
                elif re.fullmatch(r"\d+(\.\d+)?|AR|REF", v):
                    b.link(parent, pn, v)
        elif blocks:
            blocks[-1][2].append(ln)
    for it, pnl, det in blocks:
        _pdm_details(it, pnl, det, centers, stock_h.x0 - 25, used, b)


def _pdm_details(it: Item, pnl, det, centers, left_x, used, b: Bom):
    rows = []
    for ln in det:
        right = [w for w in ln if w.x0 >= left_x]
        if not right:
            continue
        rt = " ".join(w.t for w in right)
        mf = re.match(r"MAKE\s+FROM:?\s*(.+)", rt, re.I)
        if mf:
            it.make_from = norm_pn(mf.group(1).split()[0])
            b.item(it.make_from)
            continue
        if used and len(right) <= 5 and all(re.fullmatch(r"[A-Z0-9]{1,6}", w.t) for w in right) \
                and re.fullmatch(r"[A-Z]\d{4}|[A-Z0-9]{3}", right[0].t):
            continue                                        # USED ON / START / END effectivity line
        rows.append((statistics.mean(w.cy for w in right), right, rt))
    # the material cells sit around the stock-size line (a wrapped MATL DESC is centred on it)
    dim = [r for r in rows if re.match(r"^(\.?\d[\d.]*\s*X\b|NOTED\b|\.?\d[\d.]*\s*(DIA|OD|X))", r[2], re.I)
           or (re.match(r"^\.?\d", r[2]) and " X " in r[2])]
    if dim:
        ys = [r[0] for r in dim]
        mat_rows = [r for r in rows if min(abs(r[0] - y) for y in ys) <= 15]
    else:
        mat_rows = []
    cells = {"stock": [], "matl": [], "spec": []}
    for y, right, _rt in sorted(mat_rows, key=lambda r: r[0]):
        for ph in _phrases(right):
            k = min(centers, key=lambda c: abs(centers[c] - (ph.cx if c != "stock" else ph.x0 + 25)))
            if k == "stock" and ph.x0 > centers["stock"] + 60:
                k = "matl"
            cells[k].append(ph.t)
    if dim:
        it.stock = it.stock or " ".join(cells["stock"])
        it.matl = it.matl or " ".join(cells["matl"])
        it.spec = it.spec or " ".join(cells["spec"])
        it.buy = False
    sup = [r[2] for r in rows if r not in mat_rows]
    if sup:
        txt = "\n".join(sup)
        if re.search(r"MILITARY STANDARDS|PROMULGATED|STANDARDIZATION", txt):
            it.comments = it.comments or txt
        else:
            it.supplier = it.supplier or txt
        if it.buy is None:
            it.buy = True


def _lines(ws: list[W], tol: float = 3.0) -> list[list[W]]:
    out: list[list[W]] = []
    for w in sorted(ws, key=lambda w: (w.cy, w.x0)):
        if out and abs(statistics.mean(v.cy for v in out[-1]) - w.cy) <= tol:
            out[-1].append(w)
        else:
            out.append([w])
    return [sorted(l, key=lambda w: w.x0) for l in out]


def lines_near(lines, ln, tol):
    cy = statistics.mean(w.cy for w in ln)
    return [w for l in lines for w in l if abs(w.cy - cy) <= tol]


def _pdm_application(b: Bom, pd: PageData, lst: str):
    """APPLICATION table: part -> next assembly (only links inside this list are used)."""
    try:
        tabs = pd.page.find_tables().tables if not pd.scanned else []
    except Exception:
        tabs = []
    for t in tabs:
        rows = t.extract()
        hdr = [_clean(c).upper() for c in rows[0]] if rows else []
        if "PART NO." in hdr and "NEXT ASSY" in hdr:
            ip, ia = hdr.index("PART NO."), hdr.index("NEXT ASSY")
            cur = ""
            for r in rows[1:]:
                p = _clean(r[ip]) if ip < len(r) else ""
                a = _clean(r[ia]) if ia < len(r) else ""
                if p:
                    cur = norm_pn(p.split()[0])
                if cur and a:
                    for nx in a.split():
                        if PN_RE.match(nx.upper()) and lst and nx.upper().startswith(lst):
                            b.link(nx, cur)


def _pdm_refdocs(b: Bom, words: list[W], name: str):
    hdr = _hdr_x(words, "IDENTIFYING", "NO.")
    if not hdr:
        return
    for ln in _lines([w for w in words if w.y0 > hdr.y1 + 4]):
        if len(ln) >= 2 and ln[-1].t.upper() in ("PROCESS", "SPEC", "STD", "STANDARD"):
            b.procs.append(Proc("", ln[0].t, ln[-1].t.upper(), set(), f"{name}: reference document"))


# ============================================================================== coded notes (NL / PDM notes pages)
SHEET_ANCHOR_RE = re.compile(r"^CL\W{0,3}\w?\W{0,2}SSIFICATION|^CONTRACT\s*N[O0]", re.I)


def _sheet_windows(pd: PageData, words: list[W]) -> list[tuple[float, float]]:
    """Old Douglas lists are often scanned two sheets side by side on one page. Returns the x-range of each sheet
    (from the CLASSIFICATION / CONTRACT NO. blocks at each sheet's left edge), so text is never mixed across
    sheets. One sheet -> [(0, page width)]."""
    xs = sorted(w.x0 for w in words if SHEET_ANCHOR_RE.search(w.t.strip()))
    lefts: list[float] = []
    for x in xs:
        if not lefts or x - lefts[-1] > pd.W * 0.2:
            lefts.append(x)
    if len(lefts) < 2:
        return [(0.0, pd.W, 0.0)]
    edges = [0.0] + [x - 12 for x in lefts[1:]] + [pd.W]
    return [(a, b, l) for (a, b), l in zip(zip(edges, edges[1:]), lefts)]   # (from x, to x, sheet's left edge)


def _in_window(words: list[W], win: tuple[float, float]) -> list[W]:
    return [w for w in words if win[0] <= w.cx < win[1]]


def _shift(w: W, dx: float, t: Optional[str] = None) -> W:
    return W(w.x0 + dx, w.y0, w.x1 + dx, w.y1, w.t if t is None else t)


def _coded_notes(b: Bom, pd: PageData, words: list[W], lst: str, name: str,
                 win: Optional[tuple[float, float]] = None, template: Optional[tuple] = None):
    """Notes of one sheet. template: (code header, dash headers, sheet left) read on another sheet of the same
    layout - used when OCR missed this sheet's header. Returns this sheet's (code header, dash headers, left)."""
    code_h = None
    for w in words:
        if w.t.upper() in ("CODE", "NOTE CODE", "NOTECODE") and any(
                v.t.upper() == "NOTE" and abs(v.cx - w.cx) < 30 and -16 < v.cy - w.cy <= 1 for v in words):
            code_h = w
            break
        if w.t.upper() in ("NOTE CODE",):
            code_h = w
            break
    left = win[2] if win else 0.0
    if code_h is None:
        if not template:
            return None
        dx = left - template[2]
        code_h = _shift(template[0], dx)
        dashes = [_shift(d, dx) for d in template[1]]
    else:
        dashes = [w for w in words if DASH_RE.match(w.t) and abs(w.cy - code_h.cy) < 16 and w.x1 < code_h.x0]
        if not dashes and template:
            dashes = [_shift(d, left - template[2]) for d in template[1]]
    top = code_h.cy + 2
    # split OCR lines like "AAZ1 WHERE A GOVERNMENT ..." at the code column; drop vertical margin text
    ws = []
    for w in words:
        if len(w.t) > 2 and (w.y1 - w.y0) > 1.8 * (w.x1 - w.x0):
            continue
        m = re.match(r"^([0-9A-Z]{4})\s*(.+)$", w.t)
        if m and code_h.x0 - 12 <= w.x0 <= code_h.x1 - 4 and CODE_RE.match(m.group(1)) and w.x1 > code_h.x1 + 8 \
                and (" " in w.t[:6] or len(w.t) > 8):
            frac = len(m.group(1)) / max(1, len(w.t))
            xm = w.x0 + (w.x1 - w.x0) * frac
            ws += [W(w.x0, w.y0, xm, w.y1, m.group(1)), W(xm + 3, w.y0, w.x1, w.y1, m.group(2))]
        elif len(w.t.replace(" ", "")) == 4 and " " in w.t and CODE_RE.match(w.t.replace(" ", "")) \
                and abs(w.cx - code_h.cx) < 30:
            ws.append(W(w.x0, w.y0, w.x1, w.y1, w.t.replace(" ", "")))
        else:
            ws.append(w)
    y_stop0 = min([w.y0 for w in ws if w.y0 > top and re.search(r"^CONTINUED$|^PRINTED$|^PREPARED|^\*\*\*|^Document",
                                                                w.t, re.I)] + [pd.H])
    codes = [w for w in ws if top < w.y0 < y_stop0 and CODE_RE.match(w.t.upper()) and abs(w.cx - code_h.cx) < 30
             and not DASH_RE.match(w.t)]
    if not codes:
        return (code_h, dashes, left)
    mcx = statistics.median(c.cx for c in codes)
    codes = sorted([c for c in codes if abs(c.cx - mcx) < 12], key=lambda w: w.y0)
    text_x = statistics.median(c.x1 for c in codes) + 2
    stop = [w for w in ws if re.search(r"^CONTINUED$|END OF NOTES|^PRINTED$|PRINTED ON|PREPARED UNDER|^PREPARED|"
                                       r"^\*\*\*|^Document|Number:", w.t, re.I) and w.y0 > top]
    y_stop = min([w.y0 for w in stop] + [pd.H])
    dsort = sorted(dashes, key=lambda d: d.x0)
    step = statistics.median([bb.cx - a.cx for a, bb in zip(dsort, dsort[1:])]) if len(dsort) > 1 else 30
    rules = _vrules(pd, top + 2, min(y_stop, top + 200), *(win[:2] if win else (0.0, None))) if pd.scanned else []
    hdr_y1 = max([d.y1 for d in dashes] + [code_h.y1]) + 1
    for i, c in enumerate(codes):
        y_end = min(codes[i + 1].y0 - 1 if i + 1 < len(codes) else y_stop, y_stop)
        txt = [w for w in ws if w.x0 >= text_x and c.y0 - 3 <= w.cy < y_end and w.y0 < y_end
               and not re.search(r"THE INFORMATION ON THIS SHEET|LEGENDS ON SHEET", w.t, re.I)]
        text = " ".join(w.t for l in _lines(txt) for w in l).strip()
        text = re.sub(r"\s+", " ", text)
        text = re.sub(r"(\b[A-Z]+-)\s+(\d)", r"\1\2", text)          # "MIL-C- 83286" -> "MIL-C-83286"
        if pd.scanned:
            text = repair_ocr(split_words(text))
        applies, partial = set(), False
        for d in dashes:
            mark = _mark_at(pd, ws, d, c, step, rules, hdr_y1)
            if mark:
                applies.add(norm_pn(f"{lst}{d.t}") if lst else d.t)
                partial = partial or mark in ("P", "B")
        if text:
            b.notes.append(Note(c.t.upper(), text, applies, f"{name}: note {c.t.upper()}", partial))
    return (code_h, dashes, left)


def _mark_at(pd: PageData, ws: list[W], d: W, c: W, step: float, rules: Optional[list] = None,
             y_min: float = 0.0) -> str:
    """Applicability mark (* P A B) in dash column `d` on the row of note code `c`."""
    if rules:
        left = max([r for r in rules if r < d.cx - 1] or [d.cx - step / 2])
        right = min([r for r in rules if r > d.cx + 1] or [d.cx + step / 2])
    else:
        left, right = d.cx - step * 0.5, d.cx + step * 0.5
    hits = [w for w in ws if left - 1 <= w.cx <= right + 1 and abs(w.cy - c.cy) < max(6, c.h * 0.9)
            and (w.t.strip() in ("*", "P", "A", "B", "X", "+", "x") or
                 (len(w.t.strip()) <= 2 and not w.t.strip().isascii()) or w.t.strip() in ("•", "·", "#", "¥"))]
    if hits:
        return hits[0].t.strip() if hits[0].t.strip() in ("P", "A", "B") else "*"
    if not pd.scanned:
        return ""
    # scans: OCR often drops the little '*' - look for ink inside the cell next to the note code
    img = pd.image()
    s = pd.dpi / 72.0
    x0, x1 = int(left * s) + 4, int(right * s) - 4
    y0, y1 = int(max(c.y0 - c.h * 0.5, y_min) * s), int((c.y1 + c.h * 0.3) * s)
    crop = img[max(0, y0):y1, max(0, x0):x1]
    if crop.size == 0 or x1 <= x0:
        return ""
    ink = crop < 120
    ink[:, ink.mean(axis=0) > 0.6] = False               # stray vertical rules
    ink[ink.mean(axis=1) > 0.6, :] = False               # stray horizontal rules
    return "*" if ink.sum() >= 12 else ""


# ============================================================================== Boeing NL / AL documents
def parse_boeing_lists(pages: list[PageData], name: str) -> Bom:
    b = Bom()
    b.kinds.add("Boeing notes / application list")
    b.sources.append(name)
    for pd in pages:
        t = pd.full_text() if pd.scanned else pd.text
        m = re.search(r"Document Number:\s*(\S+)", pd.text) or re.search(r"\b(?:NL|PL|AL)\s+(\d{1,3}[A-Z]\d[A-Z]\d{4})", t)
        lst = norm_pn(m.group(1)) if m else ""
        if lst:
            b.lists.add(lst)
        words = pd.words()
        up = t.upper()
        wins = _sheet_windows(pd, words) if pd.scanned else [(0.0, pd.W, 0.0)]
        template, missed = None, []
        for win in wins:                               # each sheet on its own (two-up scans)
            ww = _in_window(words, win) if len(wins) > 1 else words
            if "APPLICATION LIST" in up and "NEXT" in up:
                _al(b, ww, lst)
            got = _coded_notes(b, pd, ww, lst, name, win if len(wins) > 1 else None, template)
            if got and got[1]:
                template = template or got
            elif got is None:
                missed.append((win, ww))
        if len(wins) > 1 and template:                 # sheets read before a header was found: read them again
            for win, ww in missed:
                _coded_notes(b, pd, ww, lst, name, win, template)
    return b


def _al(b: Bom, words: list[W], lst: str):
    nh = _hdr_x(words, "NEXT", "ASSEMBLY")
    ch = _hdr_x(words, "CONFIGURATION/")
    if not nh:
        return
    for ln in _lines([w for w in words if w.y0 > nh.y1 + 10]):
        part = [w for w in ln if (ch is None or w.x0 < ch.x1 + 60) and PN_RE.match(w.t.upper()) and "-" in w.t]
        nxt = [w for w in ln if abs(w.x0 - nh.x0) < 40 and PN_RE.match(w.t.upper())]
        if part and nxt:
            b.link(nxt[0].t, part[0].t)


# ============================================================================== entry point
_CACHE: dict[str, Optional[Bom]] = {}


def parse_pdf(data: bytes, filename: str, ocr_ok: bool = True,
              progress: Optional[Callable[[str, float], None]] = None) -> Optional[Bom]:
    """Read a parts list / BOM / notes list PDF. Returns None when the file is not one of those
    (e.g. a drawing - those are read by drawing_intel)."""
    h = hashlib.sha256(data).hexdigest()
    key = f"{h}:{ocr_ok}"
    if key in _CACHE:
        return _CACHE[key]
    import pymupdf
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception:
        return None
    pages = [PageData(p, h, ocr_ok) for p in doc]
    kind = detect(pages)
    b: Optional[Bom] = None
    try:
        if kind in ("lm_epl", "lm_ebom"):
            if progress:
                progress("Reading parts list…", 0.1)
            b = parse_lm(pages, filename)
        elif kind == "boeing_pdm":
            b = parse_boeing_pdm(pages, filename)
        elif kind in ("boeing_nl", "boeing_scan"):
            for i, pd in enumerate(pages):
                if progress:
                    progress(f"Reading {filename} page {i + 1} of {len(pages)}…", i / max(1, len(pages)))
            b = parse_boeing_lists(pages, filename)
            if kind == "boeing_scan":
                _scanned_pl(b, pages, filename)
    finally:
        pass
    if b is not None:
        _merge_labels(b)
    if b is not None and not (b.items or b.notes or b.procs or b.children):
        b = None
    _CACHE[key] = b
    return b


def _merge_labels(b: Bom):
    """A note code printed on two sheets (or read twice) becomes one note with the combined applicability."""
    by: dict[str, Note] = {}
    out = []
    for n in b.notes:
        k = n.label
        if k in by and len(k) >= 3:
            m = by[k]
            m.applies |= n.applies
            m.partial = m.partial or n.partial
            if len(n.text) > len(m.text):
                m.text = n.text
            continue
        by[k] = n
        out.append(n)
    b.notes = out


def _scanned_pl(b: Bom, pages: list[PageData], name: str):
    """Douglas / Boeing parts lists that only exist as scans (QTY REQD PER NOTED DASH NO. layout)."""
    lst = next(iter(sorted(b.lists)), "")
    for pd in pages:
        if "Type:PL" not in pd.text or not pd.scanned:
            continue
        try:
            _scan_pl_page(b, pd, lst, name)
        except Exception:
            import traceback
            traceback.print_exc()


def _ink(pd: PageData, x0, y0, x1, y1) -> int:
    img = pd.image()
    s = pd.dpi / 72.0
    crop = img[max(0, int(y0 * s)):int(y1 * s), max(0, int(x0 * s) + 3):int(x1 * s) - 3]
    if crop.size == 0:
        return 0
    ink = crop < 120
    ink[:, ink.mean(axis=0) > 0.6] = False
    ink[ink.mean(axis=1) > 0.6, :] = False
    return int(ink.sum())


def _scan_pl_page(b: Bom, pd: PageData, lst: str, name: str):
    words = pd.words()
    wins = _sheet_windows(pd, words)
    for win in wins:                                  # two sheets scanned side by side: one at a time
        _scan_pl_sheet(b, pd, _in_window(words, win) if len(wins) > 1 else words, lst, name,
                       win if len(wins) > 1 else None)


def _scan_pl_sheet(b: Bom, pd: PageData, ws: list[W], lst: str, name: str, win: Optional[tuple] = None):
    idn = next((w for w in ws if "IDENTIFYING" in w.t.replace(" ", "")), None)
    if idn is None:
        return
    band = [w for w in ws if idn.y0 - 16 <= w.cy <= idn.y1 + 12]
    body_top = max(w.y1 for w in band) + 2
    stop_w = [w for w in ws if w.y0 > body_top and (re.search(r"PREPARED|CONTINUED|^NOTE\s+[A-Z]\b", w.t)
                                                    or re.search(r"ENDOFPARTS|TOTALNUMBER", w.t.replace(" ", "")))]
    for ln in _lines([w for w in ws if w.y0 > body_top]):
        if re.search(r"ENDOFPA|TOTALNUMBER|NUMBERP/?L", "".join(w.t for w in ln).replace(" ", "").upper()):
            stop_w.append(ln[0])
    y_stop = min([w.y0 for w in stop_w] + [pd.H])
    rules = sorted(_vrules(pd, body_top + 4, min(y_stop, body_top + 250), *(win[:2] if win else (0.0, None))))
    # faint scans: where one detected column holds two header labels, split it between them
    labels = sorted([w for w in band if re.search(r"IDENTIFYING|NOMENCLATURE|FSCM|STOCK|SPECIFICATION|^DESCRIPTION|"
                                                   r"MATERIAL", w.t.replace(" ", ""))
                     and not re.search(r"ORDESCRIPTION|FSCMN[O0]\.?\d", w.t.replace(" ", ""))], key=lambda w: w.cx)
    merged: list[W] = []
    for w in labels:                                 # 'MATERIAL' over 'DESCRIPTION' is one label
        if merged and abs(merged[-1].cx - w.cx) < 25:
            continue
        merged.append(w)
    for a, c in zip(merged, merged[1:]):
        if not any(a.cx < r < c.cx for r in rules):
            rules.append((a.x1 + c.x0) / 2 if c.x0 > a.x1 else (a.cx + c.cx) / 2)
    # faint left part of the sheet: make sure the part-number column and the dash columns have their edges
    if not any(r < idn.x0 + 2 for r in rules):
        rules.append(idn.x0 - 12)
        dh = sorted([w for w in band if w.x1 < idn.x0 and re.fullmatch(r"-?\d{1,4}[A-Z]?", w.t)], key=lambda w: w.cx)
        if dh:
            step = statistics.median([b.cx - a.cx for a, b in zip(dh, dh[1:])]) if len(dh) > 1 else 26
            for d in dh:
                rules += [d.cx - step / 2, d.cx + step / 2]
    rules = sorted(r for i, r in enumerate(sorted(rules)) if i == 0 or r - sorted(rules)[i - 1] > 4)
    if len(rules) < 4:
        return
    cols = list(zip(rules, rules[1:]))

    def col_of(x):
        for i, (l, r) in enumerate(cols):
            if l <= x <= r:
                return i
        return None

    def col_for(pat):
        for w in band:
            if re.search(pat, w.t.replace(" ", "")):
                return col_of(w.cx)
        return None
    pn_c = col_of(idn.cx)
    name_c = col_for(r"NOMENCLATURE|DESCRIPTION$") if col_for(r"NOMENCLATURE") is None else col_for(r"NOMENCLATURE")
    stock_c = col_for(r"STOCK")
    spec_c = col_for(r"SPECIFICATION|SUPPLIER")
    mdesc_c = None
    for w in band:
        if "DESCRIPTION" in w.t.replace(" ", "") and col_of(w.cx) not in (name_c, pn_c):
            mdesc_c = col_of(w.cx)
    if pn_c is None:
        return
    dash_cols = {}
    for w in band:
        c = col_of(w.cx)
        if c is not None and c < pn_c and re.fullmatch(r"-?\d{1,4}[A-Z]?", w.t):
            dash_cols[c] = "-" + w.t.lstrip("-")
    body = [w for w in ws if body_top <= w.y0 < y_stop - 3]
    ref_y = min([w.y0 for w in body if re.search(r"FOLLOWING\s*DOCUMENTS|SUPPLEMENT", w.t)] + [10 ** 6])
    for w in body:                                   # OCR reads the leading dash of '-505' as '~' / '—'
        if re.fullmatch(r"[~—–_]\d{1,4}[A-Z]?", w.t):
            w.t = "-" + w.t[1:]
    cand = sorted([w for w in body if (col_of(w.x0 + 2) == pn_c or (col_of(w.cx) == pn_c and
                                                                    w.x0 > cols[pn_c][0] - 14))
                   and len(w.t.replace(" ", "")) <= 24 and
                   (PN_RE.match(w.t.replace(" ", "")) or re.fullmatch(r"-?\d{1,4}[A-Z]?", w.t))
                   and not (ref_y - 2 <= w.y0 <= ref_y + 2)], key=lambda w: (w.y0, -len(w.t)))
    starts: list[W] = []
    for w in cand:                                  # one start per row (overlapping OCR reads of one cell)
        if starts and abs(starts[-1].y0 - w.y0) < 3:
            continue
        starts.append(w)
    # rows whose part number OCR missed ('-1' on a faint scan): the row still has a name and stock / material,
    # and its own dash column carries the '*' - take the part number from that column
    if dash_cols and name_c is not None:
        for w in sorted((w for w in body if col_of(w.cx) == name_c and w.y0 < ref_y - 2), key=lambda w: w.y0):
            if any(abs(w.y0 - st.y0) < 5 for st in starts) or not any(
                    abs(v.y0 - w.y0) < 4 for v in body if col_of(v.cx) in (stock_c, mdesc_c, spec_c)):
                continue
            if any(0 < w.y0 - v.y0 < 30 and col_of(v.cx) == name_c for v in body):
                continue                                 # a second line of the name above, not a new row
            marks = [dash for c, dash in dash_cols.items()
                     if _ink(pd, cols[c][0], w.y0 - 1, cols[c][1], w.y1 + w.h * 0.8) >= 12]
            if len(marks) == 1:
                l, _r = cols[pn_c]
                starts.append(W(l + 3, w.y0, l + 20, w.y1, marks[0]))
        starts.sort(key=lambda w: w.y0)
    for i, st in enumerate(starts):
        y0 = st.y0 - 2
        y1 = starts[i + 1].y0 - 2 if i + 1 < len(starts) else y_stop
        if st.y0 < ref_y:
            y1 = min(y1, ref_y - 1)

        def cell(c):
            if c is None:
                return ""
            l, r = cols[c]
            return " ".join(w.t for ln in _lines([w for w in body if l <= w.cx <= r and y0 <= w.cy < y1])
                            for w in ln).strip()
        raw_pn = st.t.replace(" ", "")
        nm = repair_ocr(cell(name_c))
        doc_kind = re.match(r"^(PROCESS|SPEC|STD|ST0|STANDARD|METHOD|DWG)\b", nm.upper())
        if not doc_kind and re.match(r"^(MIL-(STD|ST0|[A-Z]{1,3})-|QQ-|DPS|DMS|AMS|ANSI|ASTM|SAE|FED-|TT-|BMS|BAC\d)",
                                     raw_pn.upper()) and not cell(stock_c) and not cell(mdesc_c):
            doc_kind = re.match(r"(.*)", "SPEC")
        if st.y0 > ref_y or doc_kind:
            if doc_kind:
                nm = doc_kind.group(1)
                b.procs.append(Proc("", raw_pn, nm.upper(), set(), f"{name}: reference document"))
            continue
        pn = norm_pn(f"{lst}-{raw_pn.lstrip('-')}" if re.fullmatch(r"-?\d{1,4}[A-Z]?", raw_pn) and lst else raw_pn)
        stock, matl, spec = (repair_ocr(cell(stock_c)).replace(">", "X").replace(")", " X"),
                             repair_ocr(cell(mdesc_c)), repair_ocr(cell(spec_c)))
        stock = re.sub(r"\s+X\s+X\s+|\s*X\s*X\s*", " X ", stock)
        stock = re.sub(r"(\d)X(\d)", r"\1 X \2", re.sub(r"\bX(\d)", r"X \1", stock)).strip()
        qty_text = " ".join(cell(c) for c in dash_cols)
        it = b.item(pn, name=nm, source=name)
        mf = re.search(r"MAKE\s*-?\s*FROM:?\s*([A-Z0-9][A-Z0-9\-]{3,})", f"{matl} {stock} {spec}")
        if mf:
            it.make_from = it.make_from or norm_pn(mf.group(1) if re.search(r"\d", mf.group(1)) or not lst
                                                   else mf.group(1))
            if re.fullmatch(r"-\d{1,4}[A-Z]?", it.make_from) and lst:
                it.make_from = norm_pn(lst + it.make_from)
            b.item(it.make_from, source=name)
            matl = re.sub(r"MAKE\s*-?\s*FROM:?\s*\S+", "", matl).strip()
        if re.search(r"SU.?ERSED|ERSEDED|SUPERS", qty_text + " " + nm):
            it.comments = it.comments or "superseded"
            continue
        if stock or matl or mf:
            it.stock = it.stock or stock
            it.matl = it.matl or matl
            if spec and re.search(r"\d", spec) and re.search(r"AMS|MIL|QQ|DMS|ASTM|SAE|BMS|SPEC|TYPE|CLASS", spec):
                it.spec = it.spec or spec
            elif spec:
                it.supplier = it.supplier or spec
            it.buy = False
        # quantity per dash column: '*' = this row is that dash, a number = used in it
        for c, dash in dash_cols.items():
            parent = norm_pn(f"{lst}{dash}") if lst else ""
            if not parent or parent == pn:
                continue
            q = cell(c)
            if not q:
                l, r = cols[c]
                if _ink(pd, l, st.y0 - 1, r, min(y1, st.y1 + st.h * 1.8)) >= 12:
                    b.link(parent, pn, "")
                continue
            if "*" in q or "★" in q:
                continue
            m = re.findall(r"\d+|\bAR\b|\bREF\b", q)
            b.link(parent, pn, m[-1] if m else "")


