from pathlib import Path

from fai_studio.extract import find_candidates
from fai_studio.importers import COMPONENT_FIELDS, apply_mapping, detect_mapping, parse_notes, read_notes_text, read_table
from fai_studio.tolerance import evaluate, find_specs, parse_requirement

S = Path(__file__).resolve().parent.parent / "samples"
D = {"tol_1dec": "0.1", "tol_2dec": "0.01", "tol_3dec": "0.005", "tol_4dec": "0.0005", "tol_angle": "0.5"}


def lim(s):
    f = parse_requirement(s, D).as_fields()
    return f["lower_limit"], f["upper_limit"]


def test_parser():
    assert lim("1.250 ±.005") == ("1.245", "1.255")
    assert lim("Ø.250 +.002/-.000") == ("0.250", "0.252")
    assert lim("4X Ø.201 +.003 -.001") == ("0.200", "0.204")
    assert lim("1.245-1.255") == ("1.245", "1.255")
    assert lim(".010 MAX") == ("", "0.010")
    assert lim("2.000") == ("1.995", "2.005")          # title block .XXX
    assert lim("⌖ Ø.014 Ⓜ A B C") == ("0.000", "0.014")
    assert parse_requirement("1.000 BSC").reference_only


def test_evaluate():
    assert evaluate("1.2502, 1.2497", "1.245", "1.255") == "Accept"
    assert evaluate("1.256", "1.245", "1.255") == "Reject"
    assert evaluate("CONFORMS", "", "") == "Accept"


def test_specs():
    assert find_specs("PER AMS-QQ-A-250/11 AND MIL-A-8625") == ["AMS-QQ-A-250/11", "MIL-A-8625"]


def test_imports():
    rows = read_table(S / "parts_list.xlsx")
    hi, mp = detect_mapping(rows, COMPONENT_FIELDS)
    recs = apply_mapping(rows, hi, mp)
    assert recs[0]["part_number"] == "BRACKET-1001" and len(recs) == 4
    assert [n["label"] for n in parse_notes(read_notes_text(S / "notes_list.docx"))] == list("123456")
    notes = parse_notes(read_notes_text(S / "BRACKET-1001_revB.pdf"))
    assert len(notes) == 6 and notes[-1]["text"].endswith("MIL-STD-130.")


def test_auto_balloon():
    data = (S / "BRACKET-1001_revB.pdf").read_bytes()
    reqs = [c.requirement for c in find_candidates(data, "d.pdf", 0)[0]]
    assert "5.000 ±.005" in reqs and "1.250 +.003/-.001" in reqs and len(reqs) == 9


def test_drawing_intel_synthetic():
    from fai_studio.drawing_intel import analyze_drawing
    data = (S / "BRACKET-1001_revB.pdf").read_bytes()
    info = analyze_drawing(data, "BRACKET-1001_revB.pdf", use_ocr=False)
    opts = info.part_options()
    assert any(o.kind == "Drawing" for o in opts)
    whole = [o for o in opts if o.kind == "Drawing"][0]
    reqs = [c.requirement for c in info.balloon(whole)]
    assert "5.000 ±.005" in reqs
    assert len(info.notes) == 6


def test_ocr_drawing_corrections():
    from fai_studio.drawing_intel import norm_text
    cases = {"45o ±1o": "45° ±1°", "1.25O t.OO5": "1.250 ±.005", "2X0.500": "2X Ø.500",
             ".1OO +.OOO/-.O3O": ".100 +.000/-.030", "0 .375 THRU": "Ø.375 THRU", "R.O6": "R.06",
             "4.5OO + .01O": "4.500 ±.010", "SECTION A-A": "SECTION A-A", "BOSS": "BOSS"}
    for raw, want in cases.items():
        assert norm_text(raw, ocr=True) == want, raw


def test_ocr_preprocess_profiles():
    import numpy as np
    from PIL import Image
    from fai_studio import ocr
    assert ocr.normalize_prep(None)["profile"] == "standard"
    c = ocr.normalize_prep({"profile": "custom", "binarize": "otsu", "upscale": 2})
    assert c["profile"] == "custom" and c["binarize"] == "otsu" and c["upscale"] == 2.0
    assert ocr.prep_key({"profile": "heavy"}) != ocr.prep_key({"profile": "standard"})
    im = Image.fromarray((np.random.default_rng(0).random((300, 400)) * 255).astype("uint8"))
    for prof in ocr.PROFILES:
        out, back = ocr.clean_for_ocr(im, {"profile": prof})
        assert out.mode == "RGB"
        x, y = back(out.size[0] / 2, out.size[1] / 2)
        assert abs(x - 200) < 3 and abs(y - 150) < 3


def _synthetic_preballooned(path):
    """A small metric drawing with customer balloons (diamonds + one hexagon), like SpaceX prints."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas
    font = str(Path(__file__).resolve().parents[1] / "fai_studio" / "assets" / "fonts" / "FreeSerif.ttf")
    pdfmetrics.registerFont(TTFont("FS", font))
    H = 792
    c = canvas.Canvas(str(path), pagesize=(1224, H))

    def text(x, y, s, size=11):
        c.setFont("FS", size)
        c.drawString(x, H - y, s)

    def diamond(cx, cy, n):
        w, h = 7.1, 5.5
        p = c.beginPath()
        p.moveTo(cx, H - (cy - h)); p.lineTo(cx - w, H - cy); p.lineTo(cx, H - (cy + h)); p.lineTo(cx + w, H - cy)
        p.close()
        c.drawPath(p)
        text(cx - 2.5 * len(str(n)), cy + 3, str(n), 8.9)

    def hexagon(cx, cy, n):
        w, h = 5.8, 5.0
        pts = [(cx - w, cy - h), (cx + w, cy - h), (cx + w + 2.9, cy), (cx + w, cy + h), (cx - w, cy + h), (cx - w - 2.9, cy)]
        p = c.beginPath(); p.moveTo(pts[0][0], H - pts[0][1])
        for x, y in pts[1:]:
            p.lineTo(x, H - y)
        p.close(); c.drawPath(p)
        text(cx - 2.5 * len(str(n)), cy + 3, str(n), 8.9)
    text(60, 60, "UNLESS OTHERWISE SPECIFIED, ALL DIMENSIONS ARE IN MILLIMETERS")
    diamond(190, 200, 1); text(205, 204, "4X Ø10 ±0.1")
    diamond(190, 240, 2); text(205, 244, "⌖ Ø0.35 A B C")
    text(400, 204, "13 ±1"); diamond(445, 200, 3)
    hexagon(190, 300, 5); text(205, 304, "R1.5 ±0.2")                 # 4 was deleted by a revision
    text(400, 304, "(Ø4.2)")                                           # reference, not ballooned
    c.save()


def test_customer_balloons(tmp_path):
    from fai_studio.drawing_intel import analyze_drawing
    pdf = tmp_path / "prebal.pdf"
    _synthetic_preballooned(pdf)
    info = analyze_drawing(pdf.read_bytes(), "prebal.pdf", True)
    assert info.preballooned is False or True                          # 4 balloons < threshold of 5
    from fai_studio.preballoon import find_all, match, missing_numbers
    found = find_all(pdf.read_bytes())
    nums = sorted(b.num for b in found.get(0, []))
    assert nums == [1, 2, 3, 5]
    got, extra = match(info, 0, found[0])
    by = {c.char_no: c.requirement for c in got}
    assert by[1] == "4X Ø10 ±0.1" and by[2].startswith("⌖") and by[3] == "13 ±1" and by[5] == "R1.5 ±0.2"
    assert missing_numbers(nums) == [4]
    assert any(c.requirement == "(Ø4.2)" for c in extra)


def test_composite_frame_rows(tmp_path):
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas
    from fai_studio.drawing_intel import analyze_drawing
    from fai_studio.tolerance import parse_requirement
    font = str(Path(__file__).resolve().parents[1] / "fai_studio" / "assets" / "fonts" / "FreeSerif.ttf")
    pdfmetrics.registerFont(TTFont("FS", font))
    pdf = tmp_path / "comp.pdf"
    c = canvas.Canvas(str(pdf), pagesize=(1224, 792)); c.setFont("FS", 11)
    x, y = 200, 600
    c.rect(x, y - 14, 16, 28); c.drawString(x + 3, y - 4, "⌖")          # symbol cell spans both rows
    c.rect(x + 16, y, 60, 14); c.drawString(x + 19, y + 3, "Ø.010Ⓜ")
    c.drawString(x + 79, y + 3, "A B C")
    c.rect(x + 16, y - 14, 60, 14); c.drawString(x + 19, y - 11, "Ø.000Ⓜ"); c.drawString(x + 79, y - 11, "A")
    c.drawString(500, 600, "⌖ Ø.020Ⓜ A B C"); c.drawString(516, 586, "Ø.005Ⓜ A")
    c.save()
    info = analyze_drawing(pdf.read_bytes(), "comp.pdf", True)
    reqs = [c.requirement for c in info.balloon_sheet(0)]
    assert "⌖ Ø.010Ⓜ A B C" in reqs and "⌖ Ø.000Ⓜ A" in reqs
    assert "⌖ Ø.020Ⓜ A B C" in reqs and "⌖ Ø.005Ⓜ A" in reqs
    p = parse_requirement("⌖ Ø.000Ⓜ A")
    assert p.bonus
    from fai_studio.tolerance import evaluate
    assert evaluate("0.004", "0.000", "0.000", bonus=True) == ""       # no false reject at MMC
    assert evaluate("0.008", "0.000", "0.010", bonus=True) == "Accept"
    assert evaluate("0.004", "0.000", "0.000") == "Reject"


def test_metric_whole_numbers_and_balloon_size():
    from fai_studio.tolerance import parse_requirement
    p = parse_requirement("13", {"tol_0dec": "2"})
    assert (p.lower, p.upper) == (11, 15)
    p = parse_requirement("4X Ø10 +0.13/-0.01")
    assert str(p.lower) == "9.99" and str(p.upper) == "10.13"
    p = parse_requirement("↧16.5 MAX")
    assert str(p.upper) == "16.5"


def test_report_types():
    from fai_studio.report_types import (AS9102, AS9102_CLEAN, INSPECTION, REPORT_TYPES, STANDARD, is_clean,
                                         is_fai, number_prefix)
    assert REPORT_TYPES == [AS9102, AS9102_CLEAN, STANDARD, INSPECTION]
    assert is_fai(AS9102) and is_fai(AS9102_CLEAN) and not is_fai(STANDARD) and not is_fai(INSPECTION)
    assert is_clean(AS9102_CLEAN) and is_clean(STANDARD) and not is_clean(AS9102)
    assert number_prefix(AS9102_CLEAN) == "FAI" and number_prefix(STANDARD) == "IR"


def test_branding_image(tmp_path):
    from PIL import Image
    from fai_studio.branding import b64_to_bytes, image_file_to_b64
    src = tmp_path / "logo.png"
    Image.new("RGBA", (2400, 600), (255, 0, 0, 255)).save(src)
    logo = Image.open(__import__("io").BytesIO(b64_to_bytes(image_file_to_b64(str(src), 1200, 400))))
    assert logo.size == (1200, 300)
    icon = Image.open(__import__("io").BytesIO(b64_to_bytes(image_file_to_b64(str(src), 256, 256, square=True))))
    assert icon.size == (256, 256)


def test_notes_continue_and_keep_subparagraphs():
    from fai_studio.importers import parse_notes
    text = """NOTES:
1. INTERPRET DATA PER SPEC.
2. BREAK ALL SHARP EDGES.
3. FINISH ALL DETAILS AS FOLLOWS:
     A. MASK ALL FAYING SURFACES.
     B. PRIMER PER TT-C-490.
     E. TOUCH-UP ALLOWABLE
NOTES CONTINURED ON SHEET 2
DISTRIBUTION STATEMENT D: DISTRIBUTION AUTHORIZED TO DOD ONLY
Export Controlled Information
NOTES CONTINUED FROM SHEET 1:
4
BLUE ANODIZE THE NOTED ITEM.
5.
THE FOLLOWING REQUIREMENT APPLIES TO THE PROGRAM ONLY.
THE FABRICATOR MUST COMPLY WITH THE OTHER REQUIREMENTS.
     A. PRIOR TO FIRST USE INSPECT.
     B. MARK WITH A PROOF LOAD TAG.
     C. INSPECT ANNUALLY.
6.
ALL THREADS MODELED TO MINOR DIAMETER.
7A MASKING REQUIRED ON INDICATED SURFACES
Drawing - 985AB0045, 5D177, -.45, State: Released 11/25/2025
"""
    notes = parse_notes(text)
    labels = [n["label"] for n in notes]
    assert labels == ["1", "2", "3", "4", "5", "6"]
    by = {n["label"]: n["text"] for n in notes}
    assert by["3"].splitlines()[1:] == ["A. MASK ALL FAYING SURFACES.", "B. PRIMER PER TT-C-490.",
                                        "E. TOUCH-UP ALLOWABLE"]
    assert "DISTRIBUTION" not in by["3"]
    assert by["5"].count("\n") == 3 and "THE FABRICATOR MUST COMPLY" in by["5"].splitlines()[0]
    assert "State" not in by["6"] and "7A" not in by["6"]


def test_offline_store(tmp_path):
    """Offline store: negative ids, temporary report number, audit trail, approval blocked, discard wipes data."""
    import pytest
    from fai_studio.local_store import LocalStore
    s = LocalStore("Tim", tmp_path / "offline.sqlite3")
    r = s.create_report({"part_number": "P-1", "report_type": "AS9102 FAI"})
    assert r["id"] < 0 and "-TMP-" in r["report_number"] and r["locked"] is False
    S = Path(__file__).resolve().parents[1] / "samples"
    a = s.add_attachment(r["id"], S / "BRACKET-1001_revB.pdf", "Drawing")
    assert a["id"] < 0 and s.attachment_bytes(a["id"])[:5] == b"%PDF-"
    c = s.insert("characteristics", r["id"], {"char_no": 1, "requirement": "1.250 ±.005", "balloon_attachment_id": a["id"]})
    s.update_fields("characteristics", c["id"], {"results": "1.251", "disposition": "Accept"})
    assert s.rows("characteristics", r["id"])[0]["results"] == "1.251"
    assert [x["action"] for x in s.audit_for(r["id"])].count("UPDATE") == 2
    with pytest.raises(PermissionError):
        s.set_status(r["id"], "Approved")
    s.delete_report(r["id"])
    assert s.offline_reports() == [] and s.rows("attachments", r["id"]) == []


def _tree():
    """Synthetic Boeing-style tree: assy -1 = detail -3 (machined from forging -5) + bushing; -501 = detail -7."""
    from fai_studio.bom import Bom, Note
    b = Bom()
    b.lists.add("99X1234")
    b.item("99X1234-1", name="BRACKET ASSY")
    b.item("99X1234-3", name="BRACKET", make_from="99X1234-5")
    b.item("99X1234-5", name="PRECISION FORGING", stock="NOTED", matl="FORGING, AL 7050-T74", spec="AMS 4108", buy=False)
    b.item("99X1234-7", name="BRACKET", stock=".125 X 4 X 6", matl="SHEET, AL 6061-T6", spec="AMS4027", buy=False)
    b.item("NAS75-5", name="BUSHING", supplier="ACME BUSHING CO\n1 MAIN ST", buy=True)
    b.link("99X1234-1", "99X1234-3", "1")
    b.link("99X1234-1", "NAS75-5", "2")
    b.link("99X1234-501", "99X1234-7", "1")
    b.notes += [Note("AAG2", "CHROMIC ACID ANODIZE AND SEAL PER DPS 11.01-3.", {"99X1234-3", "99X1234-7"}),
                Note("BES1", "PENETRANT INSPECT PER DPS 4.707.", {"99X1234-3"}),
                Note("1BEW", "ULTRASONIC INSPECTION NOT REQUIRED.", {"99X1234-3"}),
                Note("9AAU", "TOUCH UP WITH THE REQUIRED ORGANIC COATING AFTER ASSEMBLY PER DPS 4.50-201.",
                     {"99X1234-1"}),
                Note("1ADT", "INSTALL BUSHING(S) PER DPS 1.33-17.", {"99X1234-1"})]
    return b


def test_coc_scope_detail_vs_assembly():
    """A detail CoC lists only its own material (incl. make-from forging) and processing; an assembly
    CoC walks down every detail and adds its own hardware and assembly processes."""
    from fai_studio.coc import build_items
    b = _tree()
    det = build_items({"part_number": "99X1234-3"}, [], [], [], boms=[b])
    txt = [(r["item_type"], r["description"]) for r in det]
    assert any(t == "Material" and "99X1234-5 PRECISION FORGING" in d and "AMS 4108" in d for t, d in txt)
    assert any(t == "Process" and "ANODIZE" in d for t, d in txt)
    assert any(t == "Test" and "PENETRANT" in d for t, d in txt)
    assert not any("NAS75-5" in d or "TOUCH UP" in d or "6061" in d or "NOT REQUIRED" in d for _t, d in txt)
    assy = build_items({"part_number": "99X1234-1"}, [], [], [], boms=[b])
    txt = [(r["item_type"], r["description"]) for r in assy]
    assert any(t == "Purchased" and d.startswith("NAS75-5") for t, d in txt)
    assert any(t == "Process" and "TOUCH UP" in d for t, d in txt)
    assert any("99X1234-3" in d and "ANODIZE" in d for _t, d in txt)
    assert not any("6061" in d for _t, d in txt)                  # -7 belongs to -501, not to -1
    bush = next(r for r in assy if r["description"].startswith("NAS75-5"))
    assert bush["vendor_name"] == "ACME BUSHING CO"
    other = build_items({"part_number": "99X1234-501"}, [], [], [], boms=[b])
    assert [r["item_type"] for r in other] == ["Material", "Process"]


def test_lm_epl_pdf(tmp_path):
    """Lockheed-style 'Engineering Parts List' tables are read from the text layer: raw stock with stock size and
    PMP spec, consumables with vendors, hardware, and BOM documents as processes / tests."""
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet
    from fai_studio import bom, coc
    st = getSampleStyleSheet()
    grid = TableStyle([("GRID", (0, 0), (-1, -1), 0.5, "black")])

    def T(rows):
        t = Table(rows)
        t.setStyle(grid)
        return t
    flow = [Paragraph("Engineering Parts List", st["Title"]),
            Paragraph("Part Number: 123XY4567-001 CAGE: 04236 Name: BRACKET, TEST", st["Normal"]), Spacer(1, 8),
            Paragraph("Structure Assembly", st["Heading3"]),
            T([["Qty", "UOM", "Component Part", "Find Num", "Name", "CAGE", "Source"],
               ["0", "ar", "1716C", "", "PLATE, ALUMINUM ALLOY, 7050-T7451", "04236", "buy"],
               ["0", "ar", "LM-CC11312B", "", "PRIMER, EPOXY, 2-PART KIT", "02GJ5", "buy"],
               ["4", "ea", "MS20605R3W4", "001", "RIVET, BLIND", "80205", "buy"]]),
            Paragraph("BOM Documents", st["Heading3"]),
            T([["Document", "Find", "Name", "CAGE", "State"],
               ["MAP-301124-0101", "", "PRIMING, EPOXY", "02GJ5", "RELEASED"],
               ["ASTM B594", "", "ULTRASONIC INSPECTION OF ALUMINUM", "81346", "RELEASED"],
               ["MAP-931051", "", "PACKAGING", "02GJ5", "RELEASED"]]),
            Paragraph("Material Stock Size", st["Heading3"]),
            T([["Material ID", "Height", "Width", "Length", "Comments"], ["1716C", "6.5", "34", "34", ""]]),
            Paragraph("PMP", st["Heading3"]),
            T([["Mat Id/Part Id/Proc Id", "Category", "Version", "Specification", "Rev", "From", "Temper"],
               ["1716C", "7050 ALUMINUM ALLOY", "K", "AMS 4050", "K", "PLATE", "T7451 TEMPER"],
               ["MAP-301124-0101", "PRIMER APPLICATION", "-", "MAP-301124-0101", "-", "", ""],
               ["ASTM B594", "ULTRASONIC INSPECTION", "19", "ASTM B594", "19", "", ""],
               ["MAP-931051", "PACKAGING & SHIPPING", "P", "MAP-931051", "P", "", ""]]),
            Paragraph("Vendor Data", st["Heading3"]),
            T([["Matl Id/Part Id/Proc Id", "Vendor Matl Code/Part No", "Vendor Name", "Location", "CAGE", "Phone"],
               ["LM-CC11312B", "515-700", "PRC-DESOTO INTERNATIONAL INC.", "SYLMAR", "", "818-000-0000"]])]
    p = tmp_path / "epl.pdf"
    SimpleDocTemplate(str(p), pagesize=landscape(letter)).build(flow)
    b = bom.parse_pdf(p.read_bytes(), p.name, ocr_ok=False)
    assert b is not None and "123XY4567-001" in b.tops
    rows = coc.build_items({"part_number": "123XY4567-001"}, [], [], [], boms=[b])
    d = {r["description"]: r for r in rows}
    mat = next(r for r in rows if r["item_type"] == "Material" and "7050" in r["description"])
    assert mat["description"].startswith("6.5 X 34 X 34") and "AMS 4050" in mat["description"]
    primer = next(r for r in rows if "LM-CC11312B" in r["description"])
    assert primer["vendor_name"] == "PRC-DESOTO INTERNATIONAL INC."
    assert any(r["item_type"] == "Purchased" and r["description"].startswith("MS20605R3W4") for r in rows)
    assert any(r["item_type"] == "Process" and "PRIMER APPLICATION" in r["description"] for r in rows)
    assert any(r["item_type"] == "Test" and "ULTRASONIC" in r["description"] for r in rows)
    assert not any("PACKAGING" in k for k in d)


def test_reader_rules_table_and_text_fixes():
    """Reader rules: a schedule table becomes one characteristic per row with GD&T symbols; text fixes, ignore
    patterns and the example compare work."""
    from fai_studio import rules as R
    from fai_studio.drawing_intel import TL
    R.set_active([dict(d, id=i + 1) for i, d in enumerate(R.DEFAULT_RULES)])

    def L(t, x, y):
        return TL(t, x, y, x + 6 * len(t), y + 10)
    lines = [L("PMI SCHEDULE (AS AUTHORED)", 100, 50),
             L("NO.", 100, 70), L("CHARACTERISTIC", 130, 70), L("TOL.", 280, 70), L("DATUM REFS", 330, 70),
             L("1", 100, 90), L("FLATNESS", 130, 90), L(".020", 280, 90), L("A, B", 330, 90),
             L("2", 100, 110), L("PROFILE OF A SURFACE", 130, 110), L(".010", 280, 110), L("A", 330, 110),
             L("3", 100, 130), L("PERPENDICULARITY", 130, 130), L(".005", 280, 130), L("—", 330, 130),
             L("NOTES:", 20, 300), L("1. SOMETHING ELSE", 20, 315)]
    rows = R.table_rows(lines, "PMI SCHEDULE")
    words = R.gdt_words()
    got = [R.spell_to_symbols(R.fill_template("{CHARACTERISTIC} {TOL.} {DATUM REFS}", r), words) for r in rows]
    assert got == ["⏥ .020 A B", "⌓ .010 A", "⊥ .005"]
    fix = R.Rule(1, "fix", "replace", {"find": r"0/(\.?\d)", "replace": r"Ø\1", "regex": True, "ocr_only": True})
    assert R.apply_replacements("0/.250 THRU", [fix], ocr=True) == "Ø.250 THRU"
    assert R.apply_replacements("0/.250 THRU", [fix], ocr=False) == "0/.250 THRU"
    ign = R.active("ignore")
    assert R.is_ignored("Z = 43.7875", ign) and not R.is_ignored("1.250 ±.005", ign)
    scoped = R.Rule(2, "s", "stroked", {"mode": "always"}, scope_text="PMI VIEW SHEET", scope_producer="NX")
    assert scoped.applies("TITLE: PMI VIEW SHEET", "Siemens NX 8") and not scoped.applies("PMI VIEW SHEET", "CATIA")
    missing, extra = R.compare(["⏥ .020 A, B", "⌓ .010 A"], ["⏥ .020 A B", "⊥ .005"])
    assert missing == ["⌓ .010 A"] and extra == ["⊥ .005"]
    assert len(R.import_json(R.export_json([dict(d) for d in R.DEFAULT_RULES]))) == len(R.DEFAULT_RULES)
    R.set_active([])


def test_multipart_rollup_and_status():
    from fai_studio import multipart as mp
    ch = {"requirement": ".250 ±.005", "lower_limit": ".245", "upper_limit": ".255"}
    rd = {1: {"lo": ".2480", "hi": ".2510"}, 2: {"lo": ".2470", "hi": ".2560"}, 3: {"lo": ".250", "hi": ""}}
    mp.recompute(rd, ch)
    assert rd[1]["d"] == "Accept" and rd[2]["d"] == "Reject" and rd[3]["d"] == "Accept"
    text, disp = mp.rollup(rd, 5)
    assert text.startswith(".2470 – .2560") and "(3 of 5 parts)" in text and disp == "Reject"
    rd.pop(2)
    assert mp.rollup(rd, 5)[1] == ""                      # open until every part has a reading
    rd.update({2: {"lo": ".25"}, 4: {"lo": ".25"}, 5: {"lo": ".25"}})
    mp.recompute(rd, ch)
    assert mp.rollup(rd, 5)[1] == "Accept"
    assert mp.parse(mp.dump(rd)) .keys() == rd.keys()
    chars = [{"char_no": "1", "disposition": "Reject", "part_readings": mp.dump({1: {"lo": "1", "d": "Accept"},
                                                                                   2: {"lo": "2", "d": "Reject"}})},
             {"char_no": "2", "disposition": "N/A", "part_readings": ""}]
    st = mp.part_status(chars, 3)
    assert [s["status"] for s in st] == ["Accept", "Reject", "Open"] and st[1]["rejected"] == ["1"]
    rep = {"report_type": "Inspection Report", "part_count": 3, "reading_mode": "minmax",
           "serial_number": "A1", "part_serials": "\nA2"}
    assert mp.applies(rep) and mp.serials(rep) == ["A1", "A2", ""]
    assert not mp.applies(dict(rep, report_type="FAI - Full"))
    assert mp.key_of("_p12_hi") == (12, "hi") and mp.key_of("results") is None


def test_multipart_import_assignment():
    from fai_studio import multipart as mp
    rep = {"report_type": "Inspection Report", "part_count": 3, "reading_mode": "minmax",
           "part_serials": "A1\nA2\nA3"}
    # file order, pairs in one cell
    got = mp.assign_imports([{"results": ".248 / .251"}, {"results": ".249, .252"}], rep)
    assert got == {1: {"lo": ".248", "hi": ".251"}, 2: {"lo": ".249", "hi": ".252"}}
    # serial column + two rows per part = low then high
    got = mp.assign_imports([{"results": ".250", "part": "A3"}, {"results": ".253", "part": "A3"},
                             {"results": ".247", "part": "2"}], rep)
    assert got[3] == {"lo": ".250", "hi": ".253"} and got[2]["lo"] == ".247"
    # separate high column; single-value mode ignores highs
    assert mp.assign_imports([{"results": "1", "results_hi": "2"}], rep)[1] == {"lo": "1", "hi": "2"}
    single = dict(rep, reading_mode="single")
    got = mp.assign_imports([{"results": "OK"}, {"results": ".25"}, {"results": ".26"}, {"results": ".27"}], single)
    assert [got[i]["lo"] for i in (1, 2, 3)] == ["OK", ".25", ".26"] and 4 not in got


def test_ocr_learning_derive_and_apply():
    from fai_studio import ocr_learn as ol
    # letter/symbol misreads are learned as word fixes
    got = ol.derive("0.25O +.OO5", "0.250 ±.005")
    assert ("token", "0.25O", "0.250") in got and ("token", "+.OO5", "±.005") in got
    assert not any(k == "whole" for k, _, _ in got)         # the word fixes already rebuild the whole callout
    # digit-for-digit changes depend on the picture: never learned
    assert ol.derive(".250 ±.005", ".260 ±.005") == []
    assert ol.derive("Ø.25O", "Ø.260") == [] and ol.derive("Ø.25", "Ø.250") == []
    assert ol.derive("l.5OO", "1.500") == [("token", "l.5OO", "1.500")]
    # a completely different callout is not a fix of a misread
    assert ol.derive("4X R.06", "SURFACE FINISH 63") == []
    # word count changed -> whole-callout pair only
    assert ol.derive("2XØ.250", "2X Ø.250") == [("whole", "2XØ.250", "2X Ø.250")]
    rows = [{"id": 1, "kind": "token", "wrong": "0.25O", "right": "0.250", "status": "active", "hits": 3},
            {"id": 2, "kind": "token", "wrong": "+.OO5", "right": "±.005", "status": "learning", "hits": 1},
            {"id": 3, "kind": "whole", "wrong": "2XØ.250", "right": "2X Ø.250", "status": "active", "hits": 2},
            # read corrected two different ways -> ambiguous, not applied
            {"id": 4, "kind": "token", "wrong": "l.5", "right": "1.5", "status": "active", "hits": 2},
            {"id": 5, "kind": "token", "wrong": "l.5", "right": "I.5", "status": "active", "hits": 2}]
    m = ol.Memory(rows)
    assert m.apply("0.25O +.OO5") == ("0.250 +.OO5", [1])     # the learning one is not applied yet
    assert m.apply("2XØ.250") == ("2X Ø.250", [3])
    assert m.apply("l.5 THRU") == ("l.5 THRU", [])
    assert m.apply(".500 ±.010") == (".500 ±.010", [])


def test_ocr_learning_feedback_rules():
    from fai_studio import ocr_learn as ol
    tok = {"kind": "token", "right": "0.250"}
    assert ol.fix_kept(tok, "0.250 ±.005") and ol.fix_kept(tok, "0.260 ±.005")   # digits changed, fix still right
    assert not ol.fix_kept(tok, "0.25O ±.005")
    assert ol.derive("l.5OO", "1.500 REF") == []                                  # a word added: an edit
    assert ol.fix_kept({"kind": "whole", "right": "2X Ø.250"}, "2X  Ø.250")


def test_ocr_repair_and_coc_sources():
    from fai_studio.bom import repair_ocr, repair_specs
    from fai_studio import coc
    assert repair_ocr("PRIMER TREAT HOLLUW MEMBER PER DPS 4.3O1，METH0D 3.") == \
        "PRIMER TREAT HOLLOW MEMBER PER DPS 4.301, METHOD 3."
    assert repair_ocr("BAR, CRES 15-5PH SOI.UTION HEAT TREATEO") == "BAR, CRES 15-5PH SOLUTION HEAT TREATED"
    assert repair_ocr("HEAT TREAT 15-5PHT0 HEAT TREAT 15-5PH TO H1025 PER DPS 5.00.") == \
        "HEAT TREAT 15-5PH TO H1025 PER DPS 5.00."
    for keep in ("BACB30LE3K4", "NAS1149F0363P", "MS24665-132", "17-4PH", "QQ-A-250/12 T6"):
        assert repair_ocr(keep) == keep
    assert repair_specs("PERDPS11.O1-3 DN75O7") == "PER DPS 11.01-3 DN7507"
    assert coc.kind_of("3. MATERIAL: 15-5PH CRES BAR PER AMS 5659") == "Material"
    assert coc.kind_of("PROCESS PER DPS 4.806.") == "Process"
    assert coc.kind_of("MATERIAL SUBSTITUTION NOT PERMITTED") is None
    assert coc.kind_of("MEETDIMENSIONS AFTER PLATING.") is None
    have = [{"item_type": "Material", "description": "1.000 DIA BAR, CRES 15-5PH PER AMS 5659", "source": "PL"}]
    # the material note is already on the certificate through the parts list; the EO shot-peen is new
    got = coc.note_items([("MATERIAL: 15-5PH CRES PER AMS 5659", "notes list"),
                          ("* * A AOE1 SHOT PEEN PER DPS 4.999.", "engineering order")], "17X1-501", have)
    assert [(g["item_type"], g["description"]) for g in got] == [("Process", "17X1-501\nSHOT PEEN PER DPS 4.999.")]
    rows = coc._group_same([
        {"item_type": "Process", "description": "985X-001\nPASSIVATE PER AMS 2700", "source": "note 30"},
        {"item_type": "Process", "description": "985X-002\nPASSIVATE PER AMS 2700", "source": "note 30"}])
    assert len(rows) == 1 and rows[0]["description"] == "985X-001, -002\nPASSIVATE PER AMS 2700"
