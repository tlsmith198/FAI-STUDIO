"""Drawing Reader Rules - a shared, growing library of reading rules kept in the database.

Each rule has a type, parameters, a scope (which drawings it applies to) and may carry example drawings with the
characteristics they should produce; "Test" re-reads every example with the current rules so a change that fixes
one drawing but breaks another shows up before it is used.  Every change is written to a change log with the old
and new version, so any rule can be rolled back.

Rule types
  replace   text fix before reading          find, replace, regex, ocr_only
  ignore    text never ballooned             pattern (regex)
  callout   extra callout pattern            pattern (regex), char_type, designator
  table     characteristic table             title, template, skip, balloon ('row')
  gdt_word  GD&T word -> symbol              word, symbol
  stroked   read line-drawn (vector) text    mode ('always' / 'never'), dpi
  spec      spec / process pattern           pattern (regex), category
  title     title block field                field ('rev' / 'drawing_no' / 'title'), pattern (regex, group 1)

Scope (all optional): scope_text - regex that must match the drawing's text or file name;
                      scope_producer - text the PDF Creator / Producer must contain.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

RULE_TYPES: dict[str, dict] = {
    "replace": {"label": "Text fix (find → replace)",
                "params": [("find", "Find", "text"), ("replace", "Replace with", "text"),
                           ("regex", "Regular expression", "bool"), ("ocr_only", "Only on OCR / scanned text", "bool")],
                "help": "Corrects text before it is read, e.g. '0/' → 'Ø' on one customer's scans, or 'THRU ALL' → 'THRU'."},
    "ignore": {"label": "Ignore text (never balloon)",
               "params": [("pattern", "Pattern (regex)", "text")],
               "help": "Text matching this is never offered as a characteristic, e.g. model coordinates 'Z = 43.7875'."},
    "callout": {"label": "Extra callout pattern",
                "params": [("pattern", "Pattern (regex)", "text"),
                           ("char_type", "Characteristic type", "choice:Dimension,GD&T,Note,Thread,Surface Finish,"
                                                                 "Material,Visual,Functional,Reference,Basic,Other"),
                           ("designator", "Designator", "choice:,Key,Critical,Major,Minor")],
                "help": "Balloons any text matching the pattern that the reader would otherwise skip, "
                        "e.g. 'DRILL PER MS\\d+' or 'SEE NOTE \\d+'."},
    "table": {"label": "Characteristic table (schedule)",
              "params": [("title", "Table title contains", "text"),
                         ("template", "Requirement from columns", "text"),
                         ("type_column", "Type column (optional)", "text"),
                         ("skip", "Skip rows matching (regex)", "text")],
              "help": "Reads a table printed on the drawing where each row is one characteristic (PMI schedule, "
                      "dimension table, ...). Use {COLUMN HEADING} in the template, e.g. "
                      "'{CHARACTERISTIC} {TOL.} {DATUM REFS}'. GD&T words become symbols."},
    "gdt_word": {"label": "GD&T word → symbol",
                 "params": [("word", "Word(s) on the drawing / table", "text"), ("symbol", "Symbol", "gdt")],
                 "help": "Used for tables and notes that spell the characteristic out (FLATNESS → ⏥)."},
    "stroked": {"label": "Read line-drawn (vector) text",
                "params": [("mode", "Mode", "choice:always,never"), ("dpi", "OCR resolution (dpi)", "int")],
                "help": "CAD PMI / 3D annotation exports often draw dimension text as lines, not text. 'always' OCRs "
                        "those sheets as well as reading the text layer. Leave unscoped to apply everywhere."},
    "spec": {"label": "Spec / process pattern",
             "params": [("pattern", "Pattern (regex)", "text"),
                        ("category", "Form 2 category", "choice:Special Process,Material,Functional Test,Other")],
             "help": "Recognises a customer's specification numbers in notes (Form 2, CoC), e.g. 'BAC\\d{4}'."},
    "title": {"label": "Title block field",
              "params": [("field", "Field", "choice:rev,drawing_no,title,cage"), ("pattern", "Pattern (regex, group 1)", "text")],
              "help": "Reads a title-block field the standard reader misses, e.g. rev: 'REV:\\s*(\\w+)'."},
}

GDT_SYMBOLS = [("⌖", "Position"), ("⏥", "Flatness"), ("⏤", "Straightness"), ("○", "Circularity"),
               ("⌭", "Cylindricity"), ("⌒", "Profile of a line"), ("⌓", "Profile of a surface"),
               ("⊥", "Perpendicularity"), ("∥", "Parallelism"), ("∠", "Angularity"), ("◎", "Concentricity"),
               ("⌯", "Symmetry"), ("↗", "Circular runout"), ("⌰", "Total runout")]
MODIFIERS = [("Ⓜ", "MMC"), ("Ⓛ", "LMC"), ("Ⓟ", "Projected tol. zone"), ("Ⓕ", "Free state"), ("Ⓢ", "RFS"),
             ("Ⓣ", "Tangent plane"), ("Ⓤ", "Unequal profile"), ("Ⓔ", "Envelope"), ("Ⓒ", "Continuous feature"),
             ("Ⓘ", "Independency")]
OTHER_SYMBOLS = [("Ø", "Diameter"), ("±", "Plus/minus"), ("°", "Degree"), ("⌴", "Counterbore"), ("⌵", "Countersink"),
                 ("↧", "Depth"), ("□", "Square"), ("R", "Radius"), ("SR", "Spherical radius"), ("SØ", "Spherical dia."),
                 ("CR", "Controlled radius"), ("⌂", "Between"), ("▭", "Basic box")]

DEFAULT_RULES: list[dict] = [
    *[{"name": f"GD&T word: {w}", "rule_type": "gdt_word", "params": {"word": w, "symbol": s},
       "notes": "Built-in"} for w, s in [
        ("TRUE POSITION", "⌖"), ("POSITION", "⌖"), ("FLATNESS", "⏥"), ("STRAIGHTNESS", "⏤"),
        ("CIRCULARITY", "○"), ("ROUNDNESS", "○"), ("CYLINDRICITY", "⌭"), ("PROFILE OF A LINE", "⌒"),
        ("LINE PROFILE", "⌒"), ("PROFILE OF A SURFACE", "⌓"), ("SURFACE PROFILE", "⌓"),
        ("PERPENDICULARITY", "⊥"), ("PARALLELISM", "∥"), ("ANGULARITY", "∠"), ("CONCENTRICITY", "◎"),
        ("SYMMETRY", "⌯"), ("CIRCULAR RUNOUT", "↗"), ("TOTAL RUNOUT", "⌰")]],
    {"name": "PMI schedule (characteristic / tol. / datum refs)", "rule_type": "table",
     "params": {"title": "PMI SCHEDULE", "template": "{CHARACTERISTIC} {TOL.} {DATUM REFS}", "type_column": "",
                "skip": ""},
     "notes": "NX PMI view sheets: one GD&T callout per row."},
    {"name": "PMI schedule (type / text as authored)", "rule_type": "table",
     "params": {"title": "PMI SCHEDULE", "template": "{TEXT AS AUTHORED}", "type_column": "TYPE",
                "skip": "FLAGNOTE|PART CALLOUT|IN TRIANGLE"},
     "notes": "NX PMI view sheets: notes / callouts as authored; flag notes and part callouts skipped."},
    {"name": "Line-drawn PMI text (NX 3D annotation sheets)", "rule_type": "stroked",
     "params": {"mode": "always", "dpi": 200}, "scope_text": r"PMI VIEW SHEET|3D ANNOTATIONS|AUTHORED 3D PLACEMENT",
     "notes": "Model PMI is exported as line strokes; OCR it in addition to the text layer."},
    {"name": "Model coordinates are not dimensions", "rule_type": "ignore",
     "params": {"pattern": r"^\(?[XYZ]\s*=\s*-?[\d.]+\)?$|IN MODEL\)?$"}, "notes": "Coordinates printed on PMI sheets."},
    {"name": "Rev written as 'REV: 002'", "rule_type": "title",
     "params": {"field": "rev", "pattern": r"\bREV[:.]?\s+([A-Z0-9]{1,4})\b(?!\s*REISSUE)"}},
    {"name": "Drill per MS spec", "rule_type": "callout",
     "params": {"pattern": r"DRILL\s+PER\s+MS\s?\d+", "char_type": "Note", "designator": ""}},
]


@dataclass
class Rule:
    id: int
    name: str
    rule_type: str
    params: dict = field(default_factory=dict)
    enabled: bool = True
    priority: int = 100
    scope_text: str = ""
    scope_producer: str = ""
    notes: str = ""

    def applies(self, text: str, producer: str) -> bool:
        if not self.enabled:
            return False
        if self.scope_text:
            try:
                if not re.search(self.scope_text, text, re.I):
                    return False
            except re.error:
                return False
        if self.scope_producer and self.scope_producer.lower() not in (producer or "").lower():
            return False
        return True

    def p(self, k, default=""):
        v = self.params.get(k, default)
        return default if v is None else v


_ACTIVE: list[Rule] = []
_KEY = ""


def rule_from_row(r: dict) -> Rule:
    params = r.get("params") or {}
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except Exception:
            params = {}
    return Rule(int(r.get("id") or 0), r.get("name") or "", r.get("rule_type") or "", params,
                bool(r.get("enabled", True)), int(r.get("priority") or 100), r.get("scope_text") or "",
                r.get("scope_producer") or "", r.get("notes") or "")


def set_active(rows: list[dict]) -> None:
    global _ACTIVE, _KEY
    rules = sorted((rule_from_row(r) for r in rows), key=lambda r: (r.priority, r.id))
    _ACTIVE = rules
    blob = json.dumps([(r.id, r.name, r.rule_type, r.params, r.enabled, r.priority, r.scope_text, r.scope_producer)
                       for r in rules], sort_keys=True, default=str)
    _KEY = hashlib.sha1(blob.encode()).hexdigest()[:12]


def version_key() -> str:
    return _KEY


def load_shared(db) -> None:
    """Pick up the rule library (server, or the offline snapshot on this PC)."""
    try:
        if hasattr(db, "reader_rules"):
            set_active(db.reader_rules())
    except Exception:
        pass


def active(kind: str, text: str = "", producer: str = "") -> list[Rule]:
    return [r for r in _ACTIVE if r.rule_type == kind and r.applies(text, producer)]


# ------------------------------------------------------------------------------ helpers used by the reader
def apply_replacements(s: str, rules: list[Rule], ocr: bool) -> str:
    for r in rules:
        if r.p("ocr_only") and not ocr:
            continue
        f, rep = str(r.p("find")), str(r.p("replace"))
        if not f:
            continue
        try:
            s = re.sub(f, rep, s) if r.p("regex") else s.replace(f, rep)
        except re.error:
            continue
    return s


def gdt_words(text: str = "", producer: str = "") -> list[tuple[str, str]]:
    words = [(str(r.p("word")).upper().strip(), str(r.p("symbol")).strip()) for r in active("gdt_word", text, producer)]
    return sorted([w for w in words if w[0] and w[1]], key=lambda w: -len(w[0]))


def spell_to_symbols(req: str, words: list[tuple[str, str]]) -> str:
    """'FLATNESS .020 A, B' -> '⏥ .020 A B'."""
    u = req.upper()
    for w, sym in words:
        m = re.search(rf"\b{re.escape(w)}\b", u)
        if m:
            req = req[:m.start()] + sym + req[m.end():]
            u = req.upper()
            break
    if any(sym in req for _w, sym in words):
        req = re.sub(r"\s*,\s*", " ", req)                      # datum list 'A, B' -> 'A B'
        req = re.sub(r"\s+[—–-]\s*$", "", req)                  # empty datum cell '—'
    return re.sub(r"\s+", " ", req).strip()


def extra_spec_patterns() -> list[tuple[re.Pattern, str]]:
    out = []
    for r in active("spec"):
        try:
            out.append((re.compile(str(r.p("pattern")), re.I), str(r.p("category") or "Special Process")))
        except re.error:
            pass
    return out


def is_ignored(text: str, rules: list[Rule]) -> bool:
    for r in rules:
        try:
            if r.p("pattern") and re.search(str(r.p("pattern")), text, re.I):
                return True
        except re.error:
            continue
    return False


# ------------------------------------------------------------------------------ table rows -> characteristics
def table_rows(lines: list, title: str) -> list[dict]:
    """Rows of a ruled/aligned text table whose title contains `title`. lines: TL-like objects (text, x0..y1).
    Returns [{COLUMN: text, '_line': first TL of the row}]."""
    t = next((l for l in lines if title.upper() in l.text.upper()), None)
    if t is None:
        return []
    below = sorted([l for l in lines if l.y0 > t.y1 - 1 and l.y0 < t.y1 + 400], key=lambda l: (round(l.y0), l.x0))
    if not below:
        return []
    # header row: the first row below the title with 3+ cells
    rows: list[list] = []
    for l in below:
        if rows and abs(rows[-1][0].cy - l.cy) < max(2.5, 0.45 * l.h):
            rows[-1].append(l)
        else:
            rows.append([l])
    hdr_row = next((r for r in rows if len(r) >= 3), None)
    if hdr_row is None:
        return []
    hdr = sorted(hdr_row, key=lambda l: l.x0)
    left, right = hdr[0].x0 - 6, max(l.x1 for l in hdr) + 60
    rows = [[l for l in r if left <= l.x0 <= right] for r in rows]
    rows = [r for r in rows if r]
    hdr_row = next(r for r in rows if hdr[0] in r)
    cols = [(h.text.upper().strip(), h.x0) for h in hdr]
    bounds = [c[1] - 3 for c in cols] + [10 ** 6]
    out = []
    started = False
    gap_limit = None
    last_y = hdr[0].y1
    for r in rows:
        if r is hdr_row:
            started = True
            continue
        if not started:
            continue
        y = min(l.y0 for l in r)
        step = y - last_y
        if gap_limit is not None and step > gap_limit:
            break
        if out and gap_limit is None:
            gap_limit = max(3.5 * step, 25)
        last_y = max(l.y1 for l in r)
        cells: dict[str, str] = {}
        for l in sorted(r, key=lambda l: l.x0):
            i = max(k for k in range(len(cols)) if l.x0 >= bounds[k] or k == 0)
            cells[cols[i][0]] = (cells.get(cols[i][0], "") + " " + l.text).strip()
        if len(cells) < 2:
            if out and step < 1.6 * max(l.h for l in r):  # wrapped continuation of the previous row
                for k, v in cells.items():
                    out[-1][k] = (out[-1].get(k, "") + " " + v).strip()
                continue
            break
        cells["_line"] = sorted(r, key=lambda l: l.x0)[0]
        out.append(cells)
    return out


def fill_template(tpl: str, row: dict) -> str:
    def sub(m):
        k = m.group(1).upper().strip()
        v = row.get(k)
        if v is None:                                     # tolerant: 'TOL' matches 'TOL.'
            v = next((vv for kk, vv in row.items() if isinstance(vv, str) and kk.rstrip(".") == k.rstrip(".")), "")
        return v if v not in ("—", "-", "–") else ""
    return re.sub(r"\s+", " ", re.sub(r"\{([^}]+)\}", sub, tpl)).strip()


# ------------------------------------------------------------------------------ example testing
def compare(expected: list[str], got: list[str]) -> tuple[list[str], list[str]]:
    def norm(s):
        return re.sub(r"[\s,]+", " ", (s or "").upper()).strip()
    e = [norm(x) for x in expected if norm(x)]
    g = [norm(x) for x in got if norm(x)]
    missing = [x for x in e if x not in g]
    extra = [x for x in g if x not in e]
    return missing, extra


def export_json(rules: list[dict], examples: Optional[dict] = None) -> str:
    keep = ("name", "rule_type", "params", "enabled", "priority", "scope_text", "scope_producer", "notes")
    out = []
    for r in rules:
        d = {k: r.get(k) for k in keep}
        if isinstance(d["params"], str):
            try:
                d["params"] = json.loads(d["params"])
            except Exception:
                d["params"] = {}
        out.append(d)
    return json.dumps({"format": "ADMI FAI Studio reader rules", "version": 1, "rules": out}, indent=2,
                      ensure_ascii=False)


def import_json(text: str) -> list[dict]:
    d = json.loads(text)
    rules = d.get("rules", d if isinstance(d, list) else [])
    out = []
    for r in rules:
        if r.get("rule_type") in RULE_TYPES and r.get("name"):
            out.append(r)
    return out
