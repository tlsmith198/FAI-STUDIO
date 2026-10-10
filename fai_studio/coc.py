"""Certificate of Conformance (CoC).

build_items(): propose the Material / Process / Test certification rows for a report from everything the app
               already read - the parts list on the drawing (or a separate parts list), the notes (and which
               detail each flag note is marked on), and Form 2. Vendors are matched from the shared vendor list
               by CAGE code, McMaster part-number pattern or Form 2 supplier name.
build_coc_pdf(): the printed certificate, laid out like ADMI's existing CoC (company header and footer
               statement on every page, part information, certification statement, material / process
               certifications table, certified-by block at the end).
"""
from __future__ import annotations

import io
import re
from pathlib import Path
from typing import Optional

from .bom import repair_specs
from .tolerance import classify_spec, find_specs

ITEM_TYPES = ["Material", "Process", "Test", "Purchased"]
TEST_RE = re.compile(r"PROOF\s+LOAD\s+TEST|PROOF\s+TEST|ULTRASONIC|\bNDI\b|NON-?DESTRUCTIVE|PENETRANT|"
                     r"MAGNETIC\s+PARTICLE|RADIOGRAPH|X-RAY|HARDNESS\s+TEST|PRESSURE\s+TEST|LEAK\s+TEST|"
                     r"TENSILE\s+TEST|LOAD\s+TEST", re.I)
PROCESS_RE = re.compile(r"ANODIZ|CHEM\s*-?\s*FILM|CONVERSION\s+COAT|PASSIVAT|\bPLAT(E|ING)\b(?!\s+(STK|STOCK))|"
                        r"\bPRIME(R)?\b|\bPAINT|TOPCOAT|FINISH\s+ALL|FINISH\s+PER|HEAT\s+TREAT|\bWELD|BRAZ|"
                        r"SHOT\s+PEEN|BLACK\s+OXIDE|ALODINE|POWDER\s+COAT|NITRID|CADMIUM\s+PLAT|ZINC\s+PLAT|"
                        r"NICKEL\s+PLAT|CHROM(E|ATE)|IRIDITE|\bFORMING\b|STRESS\s+RELIEV|SOLDER|BOND|POT(TING)?\b|"
                        r"COAT(ING)?\s+PER|APPLY\s+.{0,30}COAT|SURFACE\s+TREAT|^COAT\b|TOUCH[\s-]*UP|"
                        r"^FORM(ED)?\s+(PER|SHOULDER)|\bROLL(ED)?\s+(THREADS?|RADIUS)|DRY\s+FILM\s+LUBRICANT|"
                        r"SOLID\s+FILM|SEAL(ED)?\s+PER", re.I)
NOT_A_CERT_RE = re.compile(r"ABBREVIATION|SUBSTITUT|INTERPRET|DATA\s+SET|ZONE\s+DESIGNATION|PERMANENT\s*(LY)?\s+MARK|"
                           r"\bMARK\s+(THE|P/N|PART)|SHIPPING|STORAGE\s+CONTAINER|BAG\s+AND\s+TAG|BREAK\s+ALL|"
                           r"REMOVE\s+BURRS|APPLIES\s+TO\s+THE\s+PROGRAM|TORQUE|INSTALL|OPERATING\s+ENVIRONMENT|"
                           r"MODELED|RECORD\s+OF\s+AUTHORITY|ALIGN\b|\bNOT\s+REQUIRED\b|^NO\s+\w+\s+(COATING|FINISH)|"
                           r"PROCEDURE\s+APPROVAL|NOT\s+PERMITTED|WHERE\s+A\s+GOVERNMENT|FABRICATION\s+PRACTICES|"
                           r"SHOP\s+PRACTICE|NOTED\s+SYMBOL|SYMBOL\s+INDICATES|COUPON\s+LOCATION|FREE\s*FROM|VISIBLE\s*CONTAM|"
                           r"HANDLED\s*WITH|WORKMANSHIP|IT\s*IS\s*PERMISSIBLE|DIMENSIONAL\s*LIMITS\s*APPLY|"
                           r"COMMERCIALLY\s*AVAILABLE|SHALL\s*NOT\s*EXCEED|\bWEIGHT\b|\bLBS\b|^DO\s+NOT\b|^LOCATE\b|\bDRILL\b|"
                           r"^DURING\s+MACHINING|SHALL\s+BE\s+REMOVED|^CONSULT\b|MASTER\s*MODEL|^MEET\s*DIMENSIONS|PLANAR|"
                           r"FEATURE\s*LOCATION|\bMBD\b|MODEL\s*BASED|^ALL\s*GEOMETRIC|DIRECTION\s*OF\s*GRAIN", re.I)
MCMASTER_RE = re.compile(r"^\d{4,5}[A-Z]\d{1,4}$")
RAW_NAME_RE = re.compile(r"^\s*(SHAPE|EXTRU|BAR\b|PLATE\b|SHEET\b|FORGING|CASTING|BILLET|INGOT|ROD\b|TUBE\b|TUBING|WIRE\b|"
                         r"RAW\s+STOCK|STOCK\b)", re.I)


def _first(text: str, n: int = 150) -> str:
    t = re.sub(r"\s+", " ", text.split("\n")[0]).strip()
    m = re.search(r"^(.{20,}?[.;:])\s", t + " ")
    t = m.group(1) if m and len(m.group(1)) <= n else t
    return t if len(t) <= n else t[:n - 1].rsplit(" ", 1)[0] + "…"


def match_vendor(vendors: list[dict], cage: str = "", part_number: str = "", name: str = "") -> Optional[dict]:
    cage = (cage or "").strip().upper()
    if cage:
        for v in vendors:
            if v.get("cage") and v["cage"].strip().upper() == cage:
                return v
    if part_number and MCMASTER_RE.match(part_number.strip().upper()):
        for v in vendors:
            if "MCMASTER" in v["name"].upper().replace("-", "").replace(" ", ""):
                return v
    if name:
        key = re.sub(r"[^A-Z0-9]", "", name.upper())[:10]
        if len(key) >= 4:
            for v in vendors:
                if re.sub(r"[^A-Z0-9]", "", v["name"].upper()).startswith(key):
                    return v
    return None


def _vendor_fields(v: Optional[dict]) -> dict:
    return {"vendor_name": v["name"], "vendor_info": v["address"]} if v else {}


MATERIAL_NOTE_RE = re.compile(r"^(RAW\s+)?MAT(ERIA)?'?L\b\.?\s*[:\-–]|^MATERIAL\s+(PER|SHALL\s+BE|IS)\b|"
                              r"^(FABRICATE|MAKE|MACHINE)\s+FROM\b|^STOCK\s*[:\-–]", re.I)


def kind_of(text: str) -> Optional[str]:
    """Judge a note by its first sentence: that is what the note asks for (the rest is detail).
    -> 'Material' / 'Process' / 'Test' or None (not something a certificate covers)."""
    line = re.sub(r"\s+", " ", (text or "").split("\n")[0]).strip()
    line = re.sub(r"^\(?[0-9A-Z]{1,4}[.)]\s+(?=[A-Z])", "", line) if re.match(r"^\(?\d{1,3}[.)]\s", line) else line
    if MATERIAL_NOTE_RE.search(line) and not re.search(r"^MATERIAL\s+SUBSTITUT", line, re.I):
        return "Material"
    first = re.split(r"(?<=[.:])\s|(?<=[A-Z]{3}\.)(?=[A-Z]{2})", line, maxsplit=1)[0]
    if NOT_A_CERT_RE.search(first):
        return None
    lead = " ".join(first.split()[:8])
    if PROCESS_RE.search(lead):
        return "Process"
    if TEST_RE.search(first) or re.match(r"^(THE\s+)?(RAW\s+STOCK|NOTED\s+ITEMS?)?.{0,40}\bINSPECT", first, re.I) \
            and re.search(r"ULTRASONIC|\bNDI\b|PENETRANT|MAGNETIC|RADIOGRAPH|X-RAY|PROOF", first, re.I):
        return "Test"
    if PROCESS_RE.search(first):
        return "Process"
    if classify_spec(first) == "Special Process" and not re.search(r"\bMACHIN|PLASTIC|\bPLATE\b", first, re.I):
        return "Process"
    if re.match(r"^(SPECIAL\s+)?PROCESS(ED)?\s+PER\b", first, re.I) and find_specs(first):
        return "Process"
    return None


_ALLOY_RE = re.compile(r"\b(\d{4}-T\d+|\d{2}-\d{1,2}\s?PH|PH\s?\d{2}-\d{1,2}\s?MO|TI-?\d+AL-?\d+V|INCONEL\s*\d+|"
                       r"\d{3}\s?(?:SST|CRES|STAINLESS)|A286|4130|4340|C\d{5})", re.I)


def _key40(t: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", re.sub(r"^[^\n]*\n", "", t).upper())[:40]


def covered(items: list[dict], kind: str, desc: str) -> bool:
    """Is this material / process already on the certificate (same text, or all its specs / alloy already on a
    row of the same kind)? Stops a note, the parts list and Form 2 all adding the same thing."""
    k = _key40(desc)
    same = [it for it in items if it["item_type"] == kind or (kind == "Material" and it["item_type"] == "Purchased")]
    if any(k and (k in _key40(it["description"]) or _key40(it["description"]) in k and len(_key40(it["description"])) > 15)
           for it in same):
        return True
    specs = [sp.upper().replace(" ", "") for sp in find_specs(desc)]
    have = " ".join(it["description"].upper().replace(" ", "") for it in same)
    if specs and all(sp in have for sp in specs):
        return True
    if kind == "Material":
        alloys = [a.upper().replace(" ", "").replace("-", "") for a in _ALLOY_RE.findall(desc)]
        flat = have.replace("-", "")
        if alloys and all(a in flat for a in alloys):
            return True
    return False


def note_items(texts: list[tuple[str, str]], pn: str, items: list[dict], need_spec: bool = False) -> list[dict]:
    """Certificate rows from free text: (text, source) pairs - notes lists, Form 3 note rows, engineering orders,
    specifications and other technical data. Only material / special process / test call-outs are kept, and only
    when not already on the certificate. need_spec: keep only call-outs that name a specification (used for
    long documents such as engineering orders, where plain sentences are mostly not requirements)."""
    out: list[dict] = []
    for text, source in texts:
        k = kind_of(text or "")
        if not k:
            continue
        specs = find_specs(text)
        if need_spec and not specs and k != "Material":
            continue
        text = re.sub(r"^(?:[*•·+#]\s*|[PAB*]\s+(?=\S)|(?=[A-Z0-9]*\d)[A-Z0-9]{4}\s+(?=[A-Z]{3,}))+", "", text.strip())
        if not kind_of(text):
            continue
        d = _first(text)
        d = re.sub(r"^\(?\d{1,3}[.)]\s+", "", d)
        if specs and not any(sp in d.upper() for sp in specs):
            d += f" ({', '.join(specs[:3])})"
        desc = f"{pn}\n{d}" if pn else d
        if covered(items + out, k, desc):
            continue
        out.append({"item_type": k, "description": desc, "source": source, "cert_lot": "", "vendor_name": "",
                    "vendor_info": ""})
    return out


def split_text_callouts(text: str) -> list[str]:
    """A notes list / engineering order / spec page -> one entry per note or sentence-like line."""
    from .importers import parse_notes
    notes = parse_notes(text or "")
    if len(notes) >= 2:
        return [n["text"] for n in notes]
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in (text or "").splitlines()]
    return [ln for ln in lines if len(ln) >= 12]


LM_SKIP_RE = re.compile(r"PACKAG|SHIPPING|IDENTIFICATION|MARKING|CONTAMINATION|CLEANING|\bCLEAN\b|INSTALLATION|"
                        r"\bINSTALL|FASTEN|COUNTERSINK|MACHIN(ING|E)\s+PRACTICE|WORKMANSHIP|DEFINITIONS|"
                        r"CROSS-REFERENCE|ACCEPTANCE\s+CRITERIA|\bTORQUE|SURFACE\s+DEFECTS|HANDLING|STORAGE", re.I)
TEST_WORDS_RE = re.compile(r"ULTRASONIC|PENETRANT|MAGNETIC\s+PARTICLE|RADIOGRAPH|X-RAY|\bNDI\b|\bNDT\b|EDDY\s+CURRENT|"
                           r"PROOF\s+(LOAD\s+)?TEST|PRESSURE\s+TEST|LEAK\s+TEST|HARDNESS\s+TEST|TENSILE\s+TEST", re.I)
PROC_WORDS_RE = re.compile(r"APPLICATION|COATING|ANODI|PLATING|PLATE\b|HEAT\s+TREAT|PASSIVAT|PRIM(ER|ING)|TOPCOAT|"
                           r"CONVERSION|LUBRICANT|SHOT\s+PEEN|WELD|BRAZ|SOLDER|BOND|PAINT|FINISH|CHEM\s*FILM|"
                           r"STRESS\s+RELIE|NITRID|BLACK\s+OXIDE|ALODINE", re.I)


def _short_list(pns: list[str]) -> str:
    """'99X1234-3, 99X1234-7' -> '99X1234-3, -7'."""
    out, base = [], None
    for p in pns:
        m = re.match(r"^(.*?)(-\d+[A-Z]?)$", p)
        if m and base == m.group(1):
            out.append(m.group(2))
        else:
            out.append(p)
            base = m.group(1) if m else None
    return ", ".join(out)


def _lm_doc_kind(ident: str, text: str) -> Optional[str]:
    t = f"{ident} {text}"
    if TEST_WORDS_RE.search(t):
        return "Test"
    if LM_SKIP_RE.search(text.split("—")[0]):
        return None
    if PROC_WORDS_RE.search(text) or PROCESS_RE.search(text):
        return "Process"
    return None


def _test_group(text: str) -> str:
    for w in ("ULTRASONIC", "PENETRANT", "MAGNETIC PARTICLE", "RADIOGRAPH", "EDDY CURRENT"):
        if w in text.upper():
            return f"{w} INSPECTION"
    m = TEST_WORDS_RE.search(text)
    return m.group(0).upper() + (" INSPECTION" if "TEST" not in m.group(0).upper() else "") if m else text.split("—")[0].strip()


def _vendor_from_item(it, vendors: list[dict]) -> dict:
    """Vendor name / info for a material or purchased part: vendor library first, then the list's own data."""
    v = match_vendor(vendors, it.cage if not (it.cage or "").startswith(("0", "8")) or it.vendors else "",
                     it.pn, it.supplier.split("\n")[0] if it.supplier else "")
    if v:
        return _vendor_fields(v)
    if it.vendors:
        first = it.vendors[0]
        lib = match_vendor(vendors, first.get("cage", ""), "", first["name"])
        if lib:
            return _vendor_fields(lib)
        info = "\n".join(x for x in (first.get("location"), f"CAGE {first['cage']}" if first.get("cage") else "",
                                      f"({first['code']})" if first.get("code") else "") if x)
        alts = sorted({x["name"] for x in it.vendors[1:] if x["name"] != first["name"]})
        if alts:
            info += "\nALT: " + " / ".join(alts)
        return {"vendor_name": first["name"], "vendor_info": info}
    if it.supplier:
        lines = it.supplier.split("\n")
        return {"vendor_name": lines[0], "vendor_info": "\n".join(lines[1:])}
    return {}


def build_from_bom(pn: str, bom, vendors: list[dict]) -> list[dict]:
    """CoC rows for part `pn` from the parts tree: a detail gets only its own material (incl. the stock /
    forging it is made from) and outside processing; an assembly walks down every detail and sub-detail."""
    from .bom import Item, _is_raw, CONSUMABLE_RE as _CONS
    root = bom.resolve(pn) or pn
    scope = bom.scope(root)
    items: list[dict] = []
    seen: set = set()

    def add(kind, desc, source, vend=None, key=None):
        k = key or (kind, re.sub(r"\s+", " ", desc.upper()).strip())
        if k in seen or not desc.strip():
            return
        seen.add(k)
        items.append({"item_type": kind, "description": desc.strip(), "source": source, "cert_lot": "",
                      "vendor_name": "", "vendor_info": "", **(vend or {})})

    make_from = {bom.items[p].make_from: p for p in scope if p in bom.items and bom.items[p].make_from}
    lists = bom.lists

    def is_made(p, it):
        if p == root or p in bom.tops or bom.children.get(p):
            return True
        if it.buy is False or it.stock or it.matl:
            return True
        return it.buy is None and any(p.startswith(l) for l in lists) and not _is_raw(it)

    n_made = sum(1 for p in scope if p not in make_from and ((bom.items.get(p) and (bom.items[p].stock or bom.items[p].matl)
                                      and not _is_raw(bom.items[p])) or p in make_from.values())) + \
        len({q for p in scope for q in bom.parent_of(p) if q in scope and bom.items.get(p) and _is_raw(bom.items[p])})
    parent = {}
    for p in scope:
        for c in bom.children.get(p, {}):
            parent.setdefault(c, p)
    # ---- materials and purchased parts, in tree order
    for p in scope:
        it = bom.items.get(p) or Item(p)
        if p in make_from:                                      # forging / bar the detail is machined from
            det = bom.items.get(make_from[p])
            cut = det.stock if det is not None and det.stock and not it.stock else ""
            what = " ".join(x for x in (it.stock if it.stock.upper() != "NOTED" else cut, it.matl) if x)
            desc = (f"{p} {it.name}".strip() if it.name else f"MAKE-FROM {p}") + (f" — {what}" if what else "") + \
                (f" PER {it.spec}" if it.spec else "")
            if n_made > 1:
                desc = f"{make_from[p]}\n{desc}"
            add("Material", desc, f"make-from for {make_from[p]}", _vendor_from_item(it, vendors))
            continue
        if _is_raw(it):                                         # Lockheed 'as required' raw stock
            mat = " ".join(x for x in (it.stock, it.matl or it.name) if x)
            mat += f" PER {it.spec}" if it.spec else ""
            if n_made > 1:
                mat = f"{parent.get(p, root)}\n{mat}"
            add("Material", mat, f"{p} raw stock for {parent.get(p, root)}", _vendor_from_item(it, vendors))
            continue
        if (it.uom or "").lower().startswith("a") and _CONS.search(f"{it.name} {it.category}"):
            add("Material", f"{p} {it.name}".strip() + (f" PER {it.spec}" if it.spec else ""),
                f"consumable for {parent.get(p, root)}", _vendor_from_item(it, vendors))
            continue
        if is_made(p, it):
            if (it.stock or it.matl) and not it.make_from:
                stock = it.stock if it.stock.upper() != "NOTED" else ""
                mat = " ".join(x for x in (stock, it.matl) if x) + (f" PER {it.spec}" if it.spec else "")
                if n_made > 1:
                    mat = f"{p}\n{mat}"
                add("Material", mat, f"{p} {it.name}".strip(), _vendor_from_item(it, vendors) if it.supplier else None,
                    key=("Material", p))
            continue
        par = parent.get(p, root)
        pit = bom.items.get(par)
        if RAW_NAME_RE.search(it.name or "") and pit is not None and not (pit.stock or pit.matl):
            # an extrusion / bar / forging listed under a detail that has no stock of its own: its raw material
            add("Material", f"{par}\n{p} {it.name}".strip() + (f" PER {it.spec}" if it.spec else ""),
                f"make-from for {par}", _vendor_from_item(it, vendors))
            continue
        add("Purchased", f"{p} {it.name}".strip(), f"purchased, used on {_short_list([par])}"
            + (f", CAGE {it.cage}" if it.cage else ""), _vendor_from_item(it, vendors))
    # ---- processes and tests: notes applicable to each part in scope, grouped when shared
    groups: dict[tuple, list[str]] = {}
    texts: dict[tuple, tuple] = {}
    for n in bom.notes:
        hit = [p for p in scope if p in n.applies] if n.applies else ([root] if root in bom.tops else [])
        if not hit:
            continue
        k = kind_of(n.text)
        if not k:
            continue
        specs = find_specs(n.text)
        d = _first(n.text)
        if specs and not any(sp in d.upper() for sp in specs):
            d += f" ({', '.join(specs[:3])})"
        key = (k, d.upper())
        groups.setdefault(key, [])
        groups[key] += [p for p in hit if p not in groups[key]]
        texts[key] = (k, d, f"note {n.label}" + (" (partial)" if n.partial else ""))
    # BOM documents (Lockheed): group the specs of one process / inspection method
    note_txt = " ".join(d.upper().replace(" ", "") for (_k, d, _s) in texts.values())
    idents: dict[tuple, list] = {}
    for pr in bom.procs:
        hit = [p for p in scope if p in pr.applies]
        if not hit:
            continue
        if pr.ident.upper().replace(" ", "") in note_txt:
            continue                                       # already on the certificate through the note
        k = _lm_doc_kind(pr.ident, pr.text)
        if not k:
            continue
        head = _test_group(pr.text) if k == "Test" else pr.text.split("—")[0].strip()
        key = (k, head.upper(), tuple(hit))
        if key in texts:
            k0, d0, src = texts[key]
            texts[key] = (k0, d0.rstrip(")") + f", {pr.ident})", src)
        else:
            texts[key] = (k, f"{head} (PER {pr.ident})", "BOM document")
            groups[key] = hit
        idents.setdefault(key, []).append(pr.ident)
    order = {"Process": 0, "Test": 1}
    for key in sorted(groups, key=lambda k: (order.get(k[0], 2),)):
        k, d, src = texts[key]
        if k == "Material" and covered(items, k, d):
            continue                          # the parts list already gives this material
        pns = [p for p in scope if p in groups[key]]
        desc = f"{_short_list(pns)}\n{d.replace('(PER ', 'PER ').rstrip(')') if d.startswith(tuple('ABCDEFGHIJKLMNOPQRSTUVWXYZ')) and '(PER ' in d else d}"
        add(k, desc, src)
        if key in idents and items and items[-1]["description"] == desc.strip():
            items[-1]["_idents"] = idents[key]
    return items


def build_items(rep: dict, infos: list, form2: list[dict], vendors: list[dict],
                components: Optional[list[dict]] = None, extra_notes: Optional[list[str]] = None,
                boms: Optional[list] = None, tech_docs: Optional[list[tuple[str, str]]] = None) -> list[dict]:
    """infos: analysed DrawingInfo objects (drawings, parts lists, notes lists) attached to the report.
    components: Form 1 parts-list rows - used when no parts list could be read from the attachments
    (e.g. the BOM came in as a spreadsheet). extra_notes: note texts from separate notes lists and the Form 3
    note rows. tech_docs: (document name, text) of engineering orders, specifications and other technical data.
    Every material, purchased (COTS) part, special / outside process and test these call out for the part - or
    for any detail / sub-assembly below it - becomes a row, once."""
    pn = (rep.get("part_number") or "").strip().upper()
    items: list[dict] = []
    seen: set = set()
    if boms:
        from .bom import Bom
        tree = Bom()
        for b in boms:
            tree.merge(b)
        if tree.resolve(pn):
            items = build_from_bom(pn, tree, vendors)
            seen = {(it["item_type"], re.sub(r"\s+", " ", it["description"].upper()).strip()) for it in items}
            # drawing flag notes for the parts in scope (e.g. Lockheed drawings + a separate EPL)
            scope = tree.scope(pn)
            for info in infos:
                try:
                    opts = {o.part_number.upper(): o for o in info.part_options()}
                except Exception:
                    opts = {}
                for tp in scope:
                    o = opts.get(tp)
                    if o is None:
                        continue
                    for n in info.applicable_notes(o):
                        flagged = "flagged on this part" in n.get("why", "")
                        general = n.get("why") in ("general note", "drawing note")
                        if not n.get("applies") or not (flagged or (general and tp == pn)):
                            continue
                        k = kind_of(n["text"])
                        if not k:
                            continue
                        specs = find_specs(n["text"])
                        desc = f"{tp}\n{_first(n['text'])}"
                        if specs and not any(sp in desc.upper() for sp in specs):
                            desc += f" ({', '.join(specs[:3])})"
                        key = (k, re.sub(r"\s+", " ", desc.upper()).strip())
                        if key not in seen and not covered(items, k, desc) and \
                                not any(_first(n["text"]).upper()[:40] in it["description"].upper() for it in items):
                            seen.add(key)
                            items.append({"item_type": k, "description": desc, "source": f"drawing note {n['label']}",
                                          "cert_lot": "", "vendor_name": "", "vendor_info": ""})
            dn = " ".join(it["description"].upper().replace(" ", "") for it in items
                          if it["source"].startswith("drawing note"))
            items = [it for it in items if not (it.get("_idents") and
                                                all(i.upper().replace(" ", "") in dn for i in it["_idents"]))]
            for it in items:
                it.pop("_idents", None)
            items += _free_text_items(pn, items, extra_notes, tech_docs)
            _enrich_form2(items, form2, vendors)
            order = {"Material": 0, "Purchased": 1, "Process": 2, "Test": 3}
            items.sort(key=lambda it: order.get(it["item_type"], 9))
            for i, it in enumerate(items, start=1):
                it["seq"] = i
                it["description"] = repair_specs(it["description"])
            return items

    def add(item_type, description, source, vendor=None, cert=""):
        key = (item_type, re.sub(r"\s+", " ", description.upper()).strip())
        if key in seen or not description.strip():
            return
        seen.add(key)
        items.append({"item_type": item_type, "description": description.strip(), "source": source,
                      "cert_lot": cert, "vendor_name": "", "vendor_info": "", **_vendor_fields(vendor)})

    parts, notes, options, flagged_by_part = {}, [], [], {}
    drawing_nos, drawing_cages = set(), set()
    for info in infos:
        for k, e in getattr(info, "parts", {}).items():
            parts.setdefault(e.part_number.upper(), e)
        notes += [n for n in getattr(info, "notes", []) if n not in notes]
        if getattr(info, "drawing_no", None):
            drawing_nos.add(info.drawing_no.upper())
        if getattr(info, "cage", None):
            drawing_cages.add(info.cage.upper())
        try:
            options += [(info, o) for o in info.part_options()]
        except Exception:
            pass

    # ---- which parts go into the part being shipped
    top = next(((i, o) for i, o in options if o.part_number.upper() == pn), None)
    included: list = []
    if top and top[1].kind == "Assembly":
        info, opt = top
        by_pn = {o.part_number.upper(): (i, o) for i, o in options}
        todo, done = [top], {pn}
        while todo:
            ci, co = todo.pop()
            for k in ci.children(co) if co.kind == "Assembly" else []:
                kp = (k.get("part_number") or "").upper()
                if kp in done:
                    continue
                done.add(kp)
                if kp in parts:
                    included.append(parts[kp])
                if kp in by_pn:
                    todo.append(by_pn[kp])
    elif top:
        e = parts.get(pn) or top[1].entry
        if e:
            included.append(e)
    else:
        included = [e for k, e in parts.items() if k != pn]
    if top and (parts.get(pn) or top[1].entry) and parts.get(pn) not in included:
        included.insert(0, parts.get(pn) or top[1].entry)
    # items listed under a detail's own quantity column (stock it is made from, inserts …)
    changed = True
    while changed:
        changed = False
        keys = {k for e in included for k in (e.find_no, e.dash) if k}
        for e in parts.values():
            if e in included:
                continue
            if any(q and q not in ("-",) for k, q in e.qty_by.items() if k in keys and k not in
                   {x.find_no for x in included if re.search(r"ASSY|ASSEMBLY|KIT", x.name or "", re.I)} - keys):
                included.append(e)
                changed = True

    def is_made(e) -> bool:
        p = e.part_number.upper()
        return any(p.startswith(d) for d in drawing_nos) or (e.cage and e.cage.upper() in drawing_cages
                                                             and not MCMASTER_RE.match(p))

    made_from = {}                                   # "M/F FN 203": the detail is made from purchased FN 203
    for e in included:
        m = re.search(r"\bM/F\s+(?:FN\s*)?(\w+)", e.material or "", re.I)
        if m:
            made_from[m.group(1)] = e
    # ---- raw material for made parts, then purchased items
    for e in included:
        if not is_made(e) or re.search(r"\bM/F\b", e.material or "", re.I):
            continue
        if not (e.material or e.size) or re.search(r"ASSY|ASSEMBLY|KIT", e.name or "", re.I) and not e.size:
            continue
        desc = " ".join(x for x in (e.size, e.material) if x)
        add("Material", desc, f"FN {e.find_no} {e.part_number} — raw material".strip())
    for e in included:
        if is_made(e):
            continue
        raw = e.find_no in made_from
        desc = f"{e.part_number} {e.name}".strip()
        if raw and e.material:
            desc += f" ({e.material})"
        v = match_vendor(vendors, e.cage, e.part_number)
        src = f"FN {e.find_no} " + (f"raw stock for {made_from[e.find_no].part_number}" if raw else "purchased") + \
            (f", CAGE {e.cage}" if e.cage else "")
        add("Material" if raw else "Purchased", desc, src, v)

    # ---- no parts list on the drawings: fall back to the Form 1 index of parts (imported BOM spreadsheet)
    if not parts and components:
        dno = (rep.get("drawing_number") or pn.split("-")[0]).strip().upper()
        for cmp in components:
            cp = (cmp.get("part_number") or "").strip()
            if not cp or cp.upper() == pn:
                continue
            made = bool(dno) and cp.upper().startswith(dno)
            extra = (cmp.get("notes") or "").strip()
            if made:
                if extra:
                    add("Material", extra, f"Form 1 item {cmp.get('item_no') or ''} {cp} — material".replace("  ", " "))
                continue
            add("Purchased", f"{cp} {cmp.get('part_name') or ''}".strip(), "Form 1 parts list",
                match_vendor(vendors, "", cp))
    # ---- processes / tests from notes (flag notes: on the details they are marked on)
    for info in infos:
        try:
            opts = {o.part_number.upper(): o for o in info.part_options()}
        except Exception:
            opts = {}
        targets = [e.part_number.upper() for e in included if is_made(e)] + ([pn] if pn else [])
        for tp in dict.fromkeys(targets):
            o = opts.get(tp)
            if o is None:
                continue
            for n in info.applicable_notes(o):
                if not n.get("applies"):
                    continue
                flagged = "flagged on this part" in n.get("why", "")
                general = n.get("why") in ("general note", "drawing note")
                if not (flagged or (general and tp == pn)):
                    continue
                k = kind_of(n["text"])
                if not k:
                    continue
                specs = find_specs(n["text"])
                desc = f"{tp}\n{_first(n['text'])}"
                if specs and not any(sp in desc.upper() for sp in specs):
                    desc += f" ({', '.join(specs[:3])})"
                add(k, desc, f"note {n['label']}" + (" (flagged)" if flagged else ""))
    # ---- material called out in the drawing notes (MATERIAL: …) when no parts list gave one
    for info in infos:
        for n in getattr(info, "notes", []) or []:
            t = n.get("text", "") if isinstance(n, dict) else getattr(n, "text", "")
            if kind_of(t) == "Material":
                for it in note_items([(t, f"drawing note {n.get('label', '') if isinstance(n, dict) else ''}".strip())],
                                     pn, items):
                    add(it["item_type"], it["description"], it["source"])
    for it in _free_text_items(pn, items, extra_notes, tech_docs):
        add(it["item_type"], it["description"], it["source"])
    _enrich_form2(items, form2, vendors, add)
    items[:] = _group_same(items)
    order = {"Material": 0, "Purchased": 1, "Process": 2, "Test": 3}
    items.sort(key=lambda it: order.get(it["item_type"], 9))
    for i, it in enumerate(items, start=1):
        it["seq"] = i
        it["description"] = repair_specs(it["description"])
    return items


def _group_same(items: list[dict]) -> list[dict]:
    """The same process / test on several parts becomes one row: '99X1234-1, -2, -7 / PASSIVATE …'."""
    out, where = [], {}
    for it in items:
        if it["item_type"] in ("Process", "Test") and "\n" in it["description"]:
            pns, text = it["description"].split("\n", 1)
            k = (it["item_type"], re.sub(r"\s+", " ", text).strip().upper())
            if k in where:
                g = where[k]
                have = g["_pns"]
                for p in re.split(r",\s*", pns):
                    p = p.strip()
                    if p.startswith("-") and have:
                        p = re.sub(r"-[^-]*$", "", have[-1]) + p
                    if p and p not in have:
                        have.append(p)
                if it["source"] not in g["source"]:
                    g["source"] = f"{g['source']}; {it['source']}"[:200]
                continue
            g = dict(it, _pns=[p.strip() for p in re.split(r",\s*", pns) if p.strip()], _text=text)
            where[k] = g
            out.append(g)
        else:
            out.append(it)
    for g in out:
        if "_pns" in g:
            g["description"] = f"{_short_list(g.pop('_pns'))}\n{g.pop('_text')}"
    return out


def _free_text_items(pn: str, items: list[dict], extra_notes, tech_docs) -> list[dict]:
    """Notes lists, Form 3 note rows, engineering orders, specifications, other technical data."""
    texts = [(t, "notes list / Form 3 note") for t in (extra_notes or []) if t]
    out = note_items(texts, pn, items)
    for name, text in tech_docs or []:
        out += note_items([(t, name) for t in split_text_callouts(text)], pn, items + out, need_spec=True)
    return out


def _enrich_form2(items: list[dict], form2: list[dict], vendors: list[dict], add=None):
    """Form 2 rows: fill supplier / C of C numbers into matching rows, add the ones not covered yet."""
    if add is None:
        def add(item_type, description, source, vendor=None, cert=""):
            if any(it["description"].upper() == description.upper() for it in items):
                return
            items.append({"item_type": item_type, "description": description, "source": source, "cert_lot": cert,
                          "vendor_name": "", "vendor_info": "", **_vendor_fields(vendor)})
    tmap = {"Material": "Material", "Special Process": "Process", "Functional Test": "Test", "Other": "Material"}
    for m in form2:
        text = " ".join(x for x in (m.get("name"), m.get("spec_number")) if x)
        if not text.strip():
            continue
        if any((m.get("spec_number") or "#").upper() in it["description"].upper() for it in items):
            # enrich the matching row with the supplier / cert already entered on Form 2
            for it in items:
                if (m.get("spec_number") or "#").upper() in it["description"].upper():
                    if not it["vendor_name"] and m.get("supplier"):
                        it.update(_vendor_fields(match_vendor(vendors, m.get("supplier_code", ""), "",
                                                              m["supplier"])) or {"vendor_name": m["supplier"]})
                    if not it["cert_lot"] and m.get("coc_number"):
                        it["cert_lot"] = m["coc_number"]
            continue
        desc = m["name"] if (m.get("spec_number") or "").upper() in (m.get("name") or "").upper() else text
        v = match_vendor(vendors, m.get("supplier_code", ""), "", m.get("supplier", ""))
        add(tmap.get(m.get("category"), "Material"), desc, "Form 2", v, m.get("coc_number") or "")
        if items and not items[-1]["vendor_name"] and m.get("supplier"):
            items[-1]["vendor_name"] = m["supplier"]


# ============================================================================== PDF
def build_coc_pdf(db, report_id: int, out_path) -> Path:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (Image as RLImage, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table,
                                    TableStyle)

    from .branding import logo_bytes
    from .reports import _fonts, escape

    _fonts()
    rep = db.get_report(report_id)
    meta = db.get_meta()
    items = db.rows("coc_items", report_id)
    W = letter[0] - 1.3 * inch
    base = ParagraphStyle("b", fontName="DejaVu", fontSize=9, leading=11)
    bold = ParagraphStyle("bb", parent=base, fontName="DejaVu-Bold")
    sect = ParagraphStyle("s", parent=base, fontName="DejaVu-Bold", fontSize=8.5, textColor=colors.HexColor("#6b6f72"),
                          spaceBefore=8, spaceAfter=3)
    title = ParagraphStyle("t", parent=base, fontName="DejaVu", fontSize=12, leading=15, alignment=TA_CENTER)
    company = meta.get("company_name", "") or ""
    short = meta.get("company_short", "") or ""
    head1 = f"{company.upper()}" + (f" ({short})" if short and short.upper() not in company.upper() else "")
    footer = meta.get("coc_footer", "")
    logo = logo_bytes(meta)

    def P(text, st=base):
        return Paragraph(escape(str(text or "")).replace("\n", "<br/>") or "&nbsp;", st)

    def grid(data, widths, header=False, extra=()):
        t = Table(data, colWidths=widths, repeatRows=1 if header else 0, splitInRow=1)
        st = [("GRID", (0, 0), (-1, -1), 0.6, colors.black), ("VALIGN", (0, 0), (-1, -1), "TOP"),
              ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
              ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5)] + list(extra)
        t.setStyle(TableStyle(st))
        return t

    flow = [Paragraph("CERTIFICATE OF CONFORMANCE (CoC)", title), Spacer(1, 10),
            Paragraph("PART INFORMATION", sect)]
    info_rows = [("Part Number", rep["part_number"]),
                 ("Part Description", rep.get("coc_part_description") or rep["part_name"]),
                 ("Pack List Number(s)", rep.get("coc_pack_list")), ("Customer", rep["customer"]),
                 ("Purchase Order", rep["po_number"]), ("Quantity Shipped", rep.get("coc_qty_shipped") or rep["quantity"]),
                 ("Date Shipped", rep.get("coc_date_shipped"))]
    flow.append(grid([[P(a), P(b)] for a, b in info_rows], [1.75 * inch, 3.3 * inch]))
    flow += [Paragraph("CERTIFICATION STATEMENT", sect), P(meta.get("coc_statement", "")), Spacer(1, 4),
             Paragraph("MATERIAL / PROCESS CERTIFICATIONS", sect)]
    widths = [0.45 * inch, 2.75 * inch, 2.1 * inch]
    widths.append(W - sum(widths))
    rows = [[P("Item"), P("Type (Material / Process / Test)"), P("Vendor Name / Info"), P("Cert / Lot #")]]
    for i, it in enumerate(items, start=1):
        vend = "\n".join(x for x in (it["vendor_name"], it["vendor_info"]) if x)
        rows.append([P(i), P(it["description"]), P(vend), P(it["cert_lot"])])
    if len(rows) == 1:
        rows.append([P(""), P(""), P(""), P("")])
    flow.append(grid(rows, widths, header=True))
    sig = [[P("Certified By (Name)"), P(rep.get("coc_certified_by") or rep.get("prepared_by"))],
           [P("Title"), P(rep.get("coc_title") or meta.get("coc_default_title", ""))],
           [P("Signature"), P("")],
           [P("Date"), P(rep.get("coc_date") or rep.get("coc_date_shipped"))]]
    flow.append(KeepTogether(grid(sig, [widths[0] + widths[1], W - widths[0] - widths[1]],
                                  extra=[("TOPPADDING", (1, 2), (1, 2), 12)])))

    def page(c, doc):
        w, h = letter
        c.saveState()
        y = h - 0.55 * inch
        if logo:
            from PIL import Image as PILImage
            from reportlab.lib.utils import ImageReader
            with PILImage.open(io.BytesIO(logo)) as im:
                lw, lh = im.size
            s = min(1.4 * inch / lw, 0.5 * inch / lh)
            c.drawImage(ImageReader(io.BytesIO(logo)), 0.65 * inch, y - lh * s + 12, lw * s, lh * s, mask="auto")
        c.setFont("DejaVu", 11)
        c.drawCentredString(w / 2, y, head1)
        c.drawCentredString(w / 2, y - 13, "CERTIFICATE OF CONFORMANCE")
        # footer statement on every page, like the paper form
        from reportlab.platypus import Paragraph as _P
        fs = ParagraphStyle("f", parent=base, fontSize=8.5, leading=10.5, alignment=TA_CENTER)
        para = _P(escape(footer), fs)
        pw, ph = para.wrap(W, 1.2 * inch)
        para.drawOn(c, 0.65 * inch, 0.62 * inch)
        c.setFont("DejaVu", 7)
        c.setFillColor(colors.HexColor("#6b6f72"))
        c.drawString(0.65 * inch, 0.38 * inch, f"{rep['report_number']}  ·  P/N {rep['part_number']}")
        c.restoreState()

    from reportlab.pdfgen import canvas as _canvas

    class NumberedCanvas(_canvas.Canvas):
        """Two-pass canvas so every page can say 'Page x of y'."""
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self._pages = []

        def showPage(self):
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            n = len(self._pages)
            for st in self._pages:
                self.__dict__.update(st)
                self.setFont("DejaVu", 7)
                self.setFillColor(colors.HexColor("#6b6f72"))
                self.drawRightString(letter[0] - 0.65 * inch, 0.38 * inch, f"Page {self._pageNumber} of {n}")
                super().showPage()
            super().save()

    out = Path(out_path)
    doc = SimpleDocTemplate(str(out), pagesize=letter, leftMargin=0.65 * inch, rightMargin=0.65 * inch,
                            topMargin=1.0 * inch, bottomMargin=1.25 * inch,
                            title=f"CoC {rep['part_number']}", author=getattr(db, "user", ""))
    doc.build(flow, onFirstPage=page, onLaterPages=page, canvasmaker=NumberedCanvas)
    return out
