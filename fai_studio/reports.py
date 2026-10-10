"""PDF report builder: AS9102 Forms 1/2/3 or Dimensional Inspection Report,
ballooned drawings (vector-preserving), and photo appendix."""
from __future__ import annotations

import io
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape as _xml_escape

import pymupdf
from PIL import Image
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (Image as RLImage, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

from . import multipart as mp
from .config import asset_path
from .report_types import STANDARD, is_clean, is_fai
from .docrender import is_image, is_pdf, open_image_page, page_size_points

CHARCOAL = colors.HexColor("#080B0B")
ORANGE = colors.HexColor("#FF6A00")
GRID = colors.HexColor("#6b6f72")
LABEL = colors.HexColor("#4a4f52")
SHADE = colors.HexColor("#F2F3F4")
REJ = colors.HexColor("#FDE2E1")
ACC_TXT = colors.HexColor("#146c2e")
REJ_TXT = colors.HexColor("#B42318")
CLEAN_HDR = colors.HexColor("#E4E4E4")          # plain AS9102 layout: light grey heading cells, black text

_fonts_ok = False


def _fonts():
    global _fonts_ok
    if _fonts_ok:
        return
    pdfmetrics.registerFont(TTFont("DejaVu", str(asset_path("fonts", "DejaVuSans.ttf"))))
    pdfmetrics.registerFont(TTFont("DejaVu-Bold", str(asset_path("fonts", "DejaVuSans-Bold.ttf"))))
    pdfmetrics.registerFont(TTFont("DejaVu-Oblique", str(asset_path("fonts", "DejaVuSans-Oblique.ttf"))))
    pdfmetrics.registerFont(TTFont("DejaVuC", str(asset_path("fonts", "DejaVuSansCondensed.ttf"))))
    pdfmetrics.registerFont(TTFont("DejaVuC-Bold", str(asset_path("fonts", "DejaVuSansCondensed-Bold.ttf"))))
    from reportlab.pdfbase.pdfmetrics import registerFontFamily
    registerFontFamily("DejaVu", normal="DejaVu", bold="DejaVu-Bold", italic="DejaVu-Oblique",
                       boldItalic="DejaVu-Bold")
    registerFontFamily("DejaVuC", normal="DejaVuC", bold="DejaVuC-Bold", italic="DejaVuC", boldItalic="DejaVuC-Bold")
    # GD&T fallback: DejaVu lacks ⌖ ⌓ ⌒ ⌭ ⌯ ⌰ ⏤ ⟂ ⏊ ⫽ Ⓜ Ⓛ Ⓢ Ⓟ Ⓤ Ⓕ Ⓣ ⌴ ⌵ …; FreeSerif has all of them
    pdfmetrics.registerFont(TTFont("GDT", str(asset_path("fonts", "FreeSerif.ttf"))))
    registerFontFamily("GDT", normal="GDT", bold="GDT", italic="GDT", boldItalic="GDT")
    _fonts_ok = True


_MAIN_GLYPHS = None
_GDT_GLYPHS = None


def escape(text) -> str:
    """XML-escape for reportlab Paragraphs and route characters the main font can't draw
    (GD&T symbols, modifiers) to the GD&T fallback font, so every symbol prints."""
    global _MAIN_GLYPHS, _GDT_GLYPHS
    _fonts()
    s = _xml_escape(str(text))
    if all(ord(ch) < 128 for ch in s):
        return s
    if _MAIN_GLYPHS is None:
        _MAIN_GLYPHS = set(pdfmetrics.getFont("DejaVuC").face.charToGlyph.keys())
        _GDT_GLYPHS = set(pdfmetrics.getFont("GDT").face.charToGlyph.keys())
    out = []
    for ch in s:
        o = ord(ch)
        if o >= 128 and o not in _MAIN_GLYPHS and o in _GDT_GLYPHS:
            out.append(f'<font name="GDT">{ch}</font>')
        else:
            out.append(ch)
    return "".join(out)


@dataclass
class ReportOptions:
    form1: bool = True
    form2: bool = True
    form3: bool = True
    drawings: bool = True
    photos: bool = True
    append_pdfs: bool = False        # append other PDF attachments (certs etc.) marked include_in_report
    watermark_draft: bool = True
    balloon_scale: float = 1.0
    report_type: Optional[str] = None  # print as this document type (default: the type set on the report)


def _styles(clean: bool = False):
    _fonts()
    base = ParagraphStyle("b", fontName="DejaVuC", fontSize=8, leading=9.6)
    return {
        "cell": base,
        "cellb": ParagraphStyle("cb", parent=base, fontName="DejaVuC-Bold"),
        "hdr": ParagraphStyle("h", parent=base, fontName="DejaVuC-Bold", fontSize=7, leading=8.4,
                              textColor=colors.white, alignment=TA_CENTER),
        "title": ParagraphStyle("t", parent=base, fontName="DejaVu-Bold", fontSize=12, leading=14),
        "sect": ParagraphStyle("s", parent=base, fontName="DejaVu-Bold", fontSize=8.5, leading=10,
                               textColor=CHARCOAL, spaceBefore=4, spaceAfter=2),
        "small": ParagraphStyle("sm", parent=base, fontSize=6.5, leading=7.8, textColor=LABEL),
        "cap": ParagraphStyle("cap", parent=base, fontSize=8, leading=10, alignment=TA_CENTER),
    } if not clean else {
        "cell": base,
        "cellb": ParagraphStyle("cb", parent=base, fontName="DejaVuC-Bold"),
        "hdr": ParagraphStyle("h", parent=base, fontName="DejaVuC-Bold", fontSize=7, leading=8.4,
                              textColor=colors.black, alignment=TA_CENTER),
        "title": ParagraphStyle("t", parent=base, fontName="DejaVu-Bold", fontSize=11, leading=13),
        "sect": ParagraphStyle("s", parent=base, fontName="DejaVu-Bold", fontSize=8.5, leading=10,
                               textColor=colors.black, spaceBefore=4, spaceAfter=2),
        "small": ParagraphStyle("sm", parent=base, fontSize=6.5, leading=7.8, textColor=colors.black),
        "cap": ParagraphStyle("cap", parent=base, fontSize=8, leading=10, alignment=TA_CENTER),
    }


def P(text, style):
    return Paragraph(escape(str(text or "")).replace("\n", "<br/>"), style)


def box(num, label, value, st, bold=False):
    lbl = f"<font size=6.2 color='#4a4f52'>{escape(str(num)) + '. ' if num else ''}{escape(label)}</font><br/>"
    val = escape(str(value or "")).replace("\n", "<br/>") or "&nbsp;"
    if bold:
        val = f"<b>{val}</b>"
    return Paragraph(lbl + val, st["cell"])


def chk(on: bool) -> str:
    return "☒" if on else "☐"


def grid_table(data, widths, header_rows=0, zebra=False, extra=None, repeat=0, clean=False):
    # splitInRow: a very long note / comment may span pages instead of crashing the report
    t = Table(data, colWidths=widths, repeatRows=repeat, splitInRow=1)
    style = [
        ("GRID", (0, 0), (-1, -1), 0.6 if clean else 0.5, colors.black if clean else GRID),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
    ]
    if header_rows:
        style += [("BACKGROUND", (0, 0), (-1, header_rows - 1), CLEAN_HDR if clean else CHARCOAL),
                  ("VALIGN", (0, 0), (-1, header_rows - 1), "MIDDLE")]
    if zebra and not clean:
        for r in range(header_rows, len(data)):
            if (r - header_rows) % 2 == 1:
                style.append(("BACKGROUND", (0, r), (-1, r), SHADE))
    if extra:
        style += extra
    t.setStyle(TableStyle(style))
    return t


class Builder:
    def __init__(self, db, report_id: int, opts: ReportOptions):
        self.db = db
        self.rid = report_id
        self.o = opts
        self.rep = db.get_report(report_id)
        if opts is not None and opts.report_type:
            self.rep = dict(self.rep, report_type=opts.report_type)
        self.meta = db.get_meta()
        self.chars = db.rows("characteristics", report_id)
        self.comps = db.rows("components", report_id)
        self.mats = db.rows("materials", report_id)
        self.atts = db.rows("attachments", report_id)
        self.is_fai = is_fai(self.rep["report_type"])
        self.clean = is_clean(self.rep["report_type"])        # plain black & white AS9102 layout
        self.standard = self.rep["report_type"] == STANDARD
        self.st = _styles(self.clean)
        # company logo: on every report except the plain AS9102 FAI - Clean forms
        from .branding import logo_bytes
        self.logo = None if (self.is_fai and self.clean) else logo_bytes(self.meta)
        self.W = landscape(letter)[0] - 0.8 * inch

    def _grid(self, *a, **k):
        return grid_table(*a, clean=self.clean, **k)

    # ------------------------------------------------------------------ page decoration
    def _on_page(self, title):
        rep, meta = self.rep, self.meta
        if self.clean:
            return self._on_page_clean(title)

        def draw(c, doc):
            w, h = landscape(letter)
            c.saveState()
            c.setFillColor(CHARCOAL)
            c.rect(0, h - 0.62 * inch, w, 0.62 * inch, stroke=0, fill=1)
            c.setFillColor(ORANGE)
            c.rect(0, h - 0.66 * inch, w, 0.04 * inch, stroke=0, fill=1)
            x_text = 0.4 * inch
            if self.logo:
                lw, lh = self._logo_size(2.2 * inch, 0.48 * inch)
                c.setFillColor(colors.white)
                c.roundRect(0.3 * inch, h - 0.57 * inch, lw + 0.2 * inch, 0.52 * inch, 3, stroke=0, fill=1)
                c.drawImage(ImageReader(io.BytesIO(self.logo)), 0.4 * inch, h - 0.31 * inch - lh / 2, lw, lh,
                            mask="auto")
                x_text = 0.4 * inch + lw + 0.25 * inch
            c.setFillColor(colors.white)
            c.setFont("DejaVu-Bold", 15)
            if not self.logo:
                c.drawString(x_text, h - 0.36 * inch, meta.get("company_short", "") or "")
            c.setFont("DejaVu", 7.5)
            c.drawString(x_text, h - (0.40 if self.logo else 0.52) * inch,
                         f"{meta.get('company_name', '')}  •  CAGE {meta.get('cage_code', '')}  •  "
                         f"{meta.get('company_address', '')}")
            c.setFont("DejaVu-Bold", 11)
            c.drawRightString(w - 0.4 * inch, h - 0.33 * inch, title)
            c.setFont("DejaVu", 7.5)
            sub = "AS9102 Rev C format" if self.is_fai else meta.get("quality_statement", "")
            c.drawRightString(w - 0.4 * inch, h - 0.50 * inch, sub)
            if self.o.watermark_draft and rep["status"] not in ("Approved",):
                c.setFillColor(colors.Color(1, 0.42, 0, alpha=0.08))
                c.setFont("DejaVu-Bold", 110)
                c.translate(w / 2, h / 2 - 30)
                c.rotate(20)
                c.drawCentredString(0, 0, rep["status"].upper())
            c.restoreState()
        return draw

    def _logo_size(self, max_w, max_h):
        from PIL import Image as _PIL
        with _PIL.open(io.BytesIO(self.logo)) as im:
            w, h = im.size
        s = min(max_w / w, max_h / h)
        return w * s, h * s

    def _on_page_clean(self, title):
        """Plain AS9102C page: the form carries its own title rows; nothing else except an optional draft mark."""
        rep = self.rep

        def draw(c, doc):
            w, h = landscape(letter)
            if self.o.watermark_draft and rep["status"] not in ("Approved",):
                c.saveState()
                c.setFillColor(colors.Color(0, 0, 0, alpha=0.05))
                c.setFont("DejaVu-Bold", 110)
                c.translate(w / 2, h / 2 - 30)
                c.rotate(20)
                c.drawCentredString(0, 0, rep["status"].upper())
                c.restoreState()
        return draw

    # ------------------------------------------------------------------ common header 1-4
    def _ident_block(self, nums=(1, 2, 3, 4)):
        r, st = self.rep, self.st
        cw = self.W / 4
        return self._grid([[box(nums[0], "Part Number", r["part_number"], st, True),
                            box(nums[1], "Part Name", r["part_name"], st, True),
                            box(nums[2], "Serial Number", r["serial_number"], st),
                            box(nums[3], "FAI Report Number" if self.is_fai else "Report Number",
                                r["report_number"], st)]], [cw] * 4)

    # ------------------------------------------------------------------ Form 1
    def form1(self):
        r, st, W = self.rep, self.st, self.W
        out = [Paragraph("Form 1: Part Number Accountability", st["title"]), Spacer(1, 4)]
        out.append(self._ident_block())
        cw = W / 4
        out.append(self._grid([
            [box(5, "Part Revision Level", r["part_rev"], st), box(6, "Drawing Number", r["drawing_number"], st),
             box(7, "Drawing Revision Level", r["drawing_rev"], st),
             box(8, "Additional Changes", r["additional_changes"], st)],
            [box(9, "Manufacturing Process Reference", r["mfg_process_ref"], st),
             box(10, "Organization Name", r["organization_name"] or self.meta.get("company_name"), st),
             box(11, "Supplier Code", r["supplier_code"] or self.meta.get("cage_code"), st),
             box(12, "Purchase Order Number", r["po_number"], st)],
        ], [cw] * 4))
        det = r["detail_or_assembly"] == "Detail"
        full = r["fai_type"] == "Full"
        p13 = Paragraph(f"<font size=6.2 color='#4a4f52'>13.</font> {chk(det)} Detail FAI &nbsp;&nbsp; "
                        f"{chk(not det)} Assembly FAI", st["cell"])
        p14 = Paragraph(
            f"<font size=6.2 color='#4a4f52'>14.</font> {chk(full)} Full FAI &nbsp;&nbsp; {chk(not full)} Partial FAI"
            f" &nbsp;&nbsp; <font size=6.2 color='#4a4f52'>Baseline Part Number including revision level:</font> "
            f"{escape(r['partial_baseline'] or '')}<br/><font size=6.2 color='#4a4f52'>Reason for Partial FAI:</font> "
            f"{escape(r['partial_reason'] or '')}", st["cell"])
        out.append(self._grid([[p13, p14]], [cw, W - cw]))
        out.append(Spacer(1, 6))
        out.append(Paragraph("Index of part numbers or sub-assembly numbers required to make the assembly noted above",
                             st["sect"]))
        hdr = [P(h, st["hdr"]) for h in ("Item", "15. Part Number", "16. Part Name", "Qty",
                                            "17. Part Serial Number", "18. FAI Report Number", "Notes")]
        widths = [0.45 * inch, 2.1 * inch, 2.6 * inch, 0.5 * inch, 1.5 * inch, 1.45 * inch]
        widths.append(W - sum(widths))
        rows = [hdr]
        for c in self.comps:
            rows.append([P(c["item_no"], st["cell"]), P(c["part_number"], st["cell"]), P(c["part_name"], st["cell"]),
                         P(c["qty"], st["cell"]), P(c["serial_number"], st["cell"]),
                         P(c["fai_report_number"], st["cell"]), P(c["notes"], st["cell"])])
        if len(rows) == 1:
            rows.append([P("", st["cell"])] * 6 + [P("N/A — detail part" if det else "", st["cell"])])
        out.append(self._grid(rows, widths, header_rows=1, zebra=True, repeat=1))
        out.append(Spacer(1, 6))
        has_nc = any(c["disposition"] == "Reject" or c["nonconformance"].strip() for c in self.chars)
        sig = [
            [Paragraph("<font size=6.2 color='#4a4f52'>19.</font> Signature indicates that all characteristics are "
                       "accounted for; meet drawing requirements or are properly documented for disposition.<br/>"
                       f"<b>Does FAI contain documented nonconformance(s)?</b> &nbsp;{chk(has_nc)} Yes "
                       f"&nbsp; {chk(not has_nc)} No", st["cell"]), "", "", ""],
            [box(20, "FAI Complete Signature", r["prepared_by"], st), box(21, "Date", r["prepared_date"], st),
             box(22, "Reviewed By", r["reviewed_by"], st), box(23, "Date", r["reviewed_date"], st)],
            [box(24, "Customer Approval", r["customer_approval"], st),
             box(25, "Date", r["customer_approval_date"], st),
             box("", "Report Status", r["status"], st, True), box("", "Customer", r["customer"], st)],
            [box(26, "Comments", r["comments"], st), "", "", ""],
        ]
        out.append(KeepTogether(self._grid(sig, [W * 0.34, W * 0.16, W * 0.34, W * 0.16],
                                           extra=[("SPAN", (0, 0), (-1, 0)), ("SPAN", (0, 3), (-1, 3))])))
        return out

    # ------------------------------------------------------------------ Form 2
    def form2(self):
        r, st, W = self.rep, self.st, self.W
        out = [Paragraph("Form 2: Product Accountability — Raw Material, Specifications and Special Process(es), "
                         "Functional Testing", st["title"]), Spacer(1, 4), self._ident_block(), Spacer(1, 6)]
        mats = [m for m in self.mats if m["category"] != "Functional Test"]
        tests = [m for m in self.mats if m["category"] == "Functional Test"]
        hdr = [P(h, st["hdr"]) for h in ("Type", "5. Material or Process Name", "6. Specification Number", "7. Code",
                                            "8. Special Process Supplier Code", "9. Customer Approval Verification",
                                            "10. Certificate of Conformance Number", "Comments")]
        widths = [0.95 * inch, 1.9 * inch, 1.55 * inch, 0.8 * inch, 1.35 * inch, 0.95 * inch, 1.35 * inch]
        widths.append(W - sum(widths))
        rows = [hdr]
        for m in mats:
            sup = " / ".join(x for x in (m["supplier"], m["supplier_code"]) if x)
            rows.append([P(m["category"], st["cell"]), P(m["name"], st["cell"]), P(m["spec_number"], st["cell"]),
                         P(m["code"], st["cell"]), P(sup, st["cell"]), P(m["customer_approval"], st["cell"]),
                         P(m["coc_number"], st["cell"]), P(m["comments"], st["cell"])])
        if len(rows) == 1:
            rows.append([P("", st["cell"]) for _ in hdr])
        out.append(self._grid(rows, widths, header_rows=1, zebra=True, repeat=1))
        out.append(Spacer(1, 8))
        out.append(Paragraph("Functional Testing", st["sect"]))
        hdr2 = [P(h, st["hdr"]) for h in ("Test Name", "11. Functional Test Procedure Number",
                                             "12. Acceptance Report Number, if applicable", "Comments")]
        w2 = [2.4 * inch, 2.4 * inch, 2.4 * inch]
        w2.append(W - sum(w2))
        rows2 = [hdr2] + [[P(t["name"], st["cell"]), P(t["spec_number"], st["cell"]), P(t["coc_number"], st["cell"]),
                           P(t["comments"], st["cell"])] for t in tests]
        if len(rows2) == 1:
            rows2.append([P("N/A", st["cell"]), P("", st["cell"]), P("", st["cell"]), P("", st["cell"])])
        out.append(self._grid(rows2, w2, header_rows=1, zebra=True, repeat=1))
        out.append(Spacer(1, 6))
        out.append(self._grid([[box(13, "Comments", "", st), box(14, "Prepared By", r["prepared_by"], st),
                                box(15, "Date", r["prepared_date"], st)]], [W * 0.6, W * 0.25, W * 0.15]))
        return out

    # ------------------------------------------------------------------ Form 3
    def _char_rows(self, cols):
        st = self.st
        rows, extra = [], []
        for i, c in enumerate(self.chars, start=1):
            disp = c["disposition"]
            color = "#000000" if self.clean else \
                "#146c2e" if disp == "Accept" else "#B42318" if disp == "Reject" else "#4a4f52"
            vals = []
            for key in cols:
                if key == "disposition":
                    vals.append(Paragraph(f"<font color='{color}'><b>{escape(disp or '')}</b></font>", st["cell"]))
                elif key == "char_no":
                    vals.append(Paragraph(f"<b>{c['char_no']}</b>", st["cell"]))
                elif key == "requirement":
                    vals.append(P(c["requirement"], st["cell"]))
                elif key == "tol":
                    t = ""
                    if c["tol_plus"] and c["tol_plus"] == c["tol_minus"]:
                        t = f"±{c['tol_plus']}"
                    elif c["tol_plus"] or c["tol_minus"]:
                        t = f"+{c['tol_plus'] or '0'}/-{c['tol_minus'] or '0'}"
                    vals.append(P(t, st["cell"]))
                elif key == "loc":
                    vals.append(P(c["ref_location"], st["cell"]))
                else:
                    vals.append(P(c.get(key, ""), st["cell"]))
            rows.append(vals)
            if disp == "Reject" and not self.clean:
                extra.append(("BACKGROUND", (0, i), (-1, i), REJ))
        return rows, extra

    def form3(self):
        r, st, W = self.rep, self.st, self.W
        out = [Paragraph("Form 3: Characteristic Accountability, Verification and Compatibility Evaluation",
                         st["title"]), Spacer(1, 4), self._ident_block(), Spacer(1, 6)]
        if self.clean:
            # exactly the AS9102 Rev C Form 3 fields
            heads = ("5. Char No.", "6. Reference Location", "7. Characteristic Designator", "8. Requirement",
                     "9. Results", "10. Designed / Qualified Tooling", "11. Nonconformance Number",
                     "12. Additional Data / Comments")
            cols = ("char_no", "loc", "designator", "requirement", "results", "tooling", "nonconformance", "comments")
            widths = [0.5 * inch, 0.9 * inch, 0.85 * inch, 2.9 * inch, 1.5 * inch, 1.2 * inch, 1.0 * inch]
        else:
            heads = ("5. Char No.", "6. Reference Location", "7. Characteristic Designator", "8. Requirement",
                     "9. Results", "Accept / Reject", "10. Designed / Qualified Tooling", "11. Nonconformance Number",
                     "12. Comments")
            cols = ("char_no", "loc", "designator", "requirement", "results", "disposition", "tooling",
                    "nonconformance", "comments")
            widths = [0.5 * inch, 0.85 * inch, 0.8 * inch, 2.6 * inch, 1.35 * inch, 0.7 * inch, 1.1 * inch, 0.95 * inch]
        widths.append(W - sum(widths))
        body, extra = self._char_rows(cols)
        rows = [[P(h, st["hdr"]) for h in heads]] + (body or [[P("", st["cell"]) for _ in heads]])
        out.append(self._grid(rows, widths, header_rows=1, zebra=True, extra=extra, repeat=1))
        out.append(Spacer(1, 6))
        if self.clean:
            out.append(KeepTogether(self._grid(
                [[box(13, "Prepared By", r["prepared_by"], st), box(14, "Date", r["prepared_date"], st)]],
                [W * 0.7, W * 0.3])))
            return out
        out.append(KeepTogether(self._grid(
            [[box("", "Units", r["units"], st),
              box("", "Title Block Tolerances", (f"X ±{r['tol_0dec']}   " if r.get('tol_0dec') else "") + f".X ±{r['tol_1dec']}   .XX ±{r['tol_2dec']}   .XXX ±{r['tol_3dec']}"
                  f"   .XXXX ±{r['tol_4dec']}   Angles ±{r['tol_angle']}°", st),
              box(13, "Prepared By", r["prepared_by"], st), box(14, "Date", r["prepared_date"], st)]],
            [W * 0.1, W * 0.5, W * 0.25, W * 0.15])))
        return out

    # ================================================================== plain AS9102C forms (customer template)
    # Layout copied from the AS9102C form set: every field is a grey label cell with its value in a white cell
    # below, black grid, "AS9102C — FORM n" title rows, no branding.
    def _lv(self, rows, ncols=32, min_h=None):
        """rows: list of rows; each row a list of (label, value, span) over `ncols` grid units, or
        ('text', str) for a full-width plain line, or ('title', str, size)."""
        st, W = self.st, self.W
        unit = W / ncols
        data, style, r = [], [], 0
        lab_st = ParagraphStyle("lab", parent=st["cell"], fontName="DejaVuC-Bold", fontSize=7, leading=8.4)
        val_st = ParagraphStyle("val", parent=st["cell"], fontSize=8.5, leading=10.2)
        for row in rows:
            if row and row[0] in ("title", "text", "note"):
                # title = "AS9102C — FORM 1"; text = the bold capitals sub-title; note = plain instruction line
                size = 11 if row[0] == "title" else 7.5 if row[0] == "text" else 7
                ps = ParagraphStyle("t", parent=st["cell"], fontName="DejaVu" if row[0] == "note" else "DejaVu-Bold",
                                    fontSize=size, leading=size * 1.25)
                content = Paragraph(escape(row[1]), ps)
                if row[0] == "title" and len(row) > 2 and row[2]:          # logo on the right of the title row
                    lw, lh = self._logo_size(2.0 * inch, 0.42 * inch)
                    img = RLImage(io.BytesIO(row[2]), width=lw, height=lh)
                    inner = Table([[content, img]], colWidths=[W - lw - 8, lw + 3])
                    inner.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                                               ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                               ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
                    content = inner
                data.append([content] + [""] * (ncols - 1))
                style.append(("SPAN", (0, r), (-1, r)))
                r += 1
                continue
            labels, values, c = [], [], 0
            for cell in row:
                lab, val, span = cell
                labels.append((c, span, lab))
                values.append((c, span, val))
                c += span
            lrow, vrow = [""] * ncols, [""] * ncols
            for c0, span, lab in labels:
                if span > 1:
                    style.append(("SPAN", (c0, r), (c0 + span - 1, r)))
                if hasattr(lab, "wrap"):                 # an answer placed on the label row (field 19)
                    lrow[c0] = lab
                    continue
                lrow[c0] = Paragraph(escape(lab), lab_st) if lab else ""
                style.append(("BACKGROUND", (c0, r), (c0 + span - 1, r), CLEAN_HDR))
            has_values = any(v is not None for _, _, v in values)
            data.append(lrow)
            r += 1
            if has_values:
                for c0, span, val in values:
                    vrow[c0] = val if hasattr(val, "wrap") else Paragraph(
                        escape(str(val or "")).replace("\n", "<br/>") or "&nbsp;", val_st)
                    if span > 1:
                        style.append(("SPAN", (c0, r), (c0 + span - 1, r)))
                data.append(vrow)
                r += 1
        t = Table(data, colWidths=[unit] * ncols, splitInRow=1)
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 2.5), ("RIGHTPADDING", (0, 0), (-1, -1), 2.5),
                               ("TOPPADDING", (0, 0), (-1, -1), 1.6), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.6)]
                              + style))
        return t

    def _list_table(self, heads, spans, rows, min_rows=0, group=None, ncols=32):
        """Column table on the 32-unit grid. heads: labels; spans: grid units per column; rows: list of lists."""
        st, W = self.st, self.W
        unit = W / ncols
        lab_st = ParagraphStyle("lab", parent=st["cell"], fontName="DejaVuC-Bold", fontSize=7, leading=8.4)
        cell_st = ParagraphStyle("c", parent=st["cell"], fontSize=8, leading=9.6)
        data, style = [], []
        starts = [sum(spans[:i]) for i in range(len(spans))]

        def put(values, bold=False):
            row = [""] * ncols
            for i, v in enumerate(values):
                row[starts[i]] = v if hasattr(v, "wrap") else Paragraph(
                    (f"<b>{escape(str(v or ''))}</b>" if bold else escape(str(v or ""))).replace("\n", "<br/>"),
                    lab_st if bold else cell_st)
            data.append(row)
            r = len(data) - 1
            for i, sp in enumerate(spans):
                if sp > 1:
                    style.append(("SPAN", (starts[i], r), (starts[i] + sp - 1, r)))
            return r
        hdr_rows = 0
        if group:                                   # e.g. [("Characteristic Accountability", 4), ...] in columns
            row = [""] * ncols
            r = len(data)
            c = 0
            for label, ncols_g in group:
                g0 = starts[c]
                g1 = starts[c + ncols_g - 1] + spans[c + ncols_g - 1] - 1
                row[g0] = Paragraph(escape(label), lab_st) if label else ""
                style.append(("SPAN", (g0, r), (g1, r)))
                if label:
                    style.append(("BACKGROUND", (g0, r), (g1, r), CLEAN_HDR))
                c += ncols_g
            data.append(row)
            hdr_rows += 1
        r = put(heads, bold=True)
        style.append(("BACKGROUND", (0, r), (-1, r), CLEAN_HDR))
        hdr_rows += 1
        for vals in rows:
            put(vals)
        for _ in range(max(0, min_rows - len(rows))):
            put([""] * len(heads))
        t = Table(data, colWidths=[unit] * ncols, repeatRows=hdr_rows, splitInRow=1)
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 2.5), ("RIGHTPADDING", (0, 0), (-1, -1), 2.5),
                               ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
                               ("ROWBACKGROUNDS", (0, hdr_rows), (-1, -1), [colors.white])] + style))
        return t

    def _ident_clean(self):
        r = self.rep
        return [("1. Part Number", r["part_number"], 8), ("2. Part Name", r["part_name"], 8),
                ("3. Serial Number", r["serial_number"], 8), ("4. FAIR Identifier", r["report_number"], 8)]

    def form1_clean(self):
        r = self.rep
        det = r["detail_or_assembly"] == "Detail"
        full = r["fai_type"] == "Full"
        has_nc = any(c["disposition"] == "Reject" or (c["nonconformance"] or "").strip() for c in self.chars)
        head = self._lv([
            ("title", "AS9102C — FORM 1"), ("text", "PART NUMBER ACCOUNTABILITY"),
            self._ident_clean(),
            [("5. Part Revision Level", r["part_rev"], 8), ("6. Drawing Number", r["drawing_number"], 8),
             ("7. Drawing Revision Level", r["drawing_rev"], 8), ("8. Additional Changes", r["additional_changes"], 8)],
            [("9. Manufacturing Process Reference", r["mfg_process_ref"], 8),
             ("10. Organization Name", r["organization_name"] or self.meta.get("company_name", ""), 8),
             ("11. Supplier Code", r["supplier_code"] or self.meta.get("cage_code", ""), 8),
             ("12. Purchase Order Number", r["po_number"], 8)],
            [("13. Detail / Assembly", "Detail FAI" if det else "Assembly FAI", 8),
             ("14. Full FAI / Partial FAI", "Full FAI" if full else "Partial FAI", 24)],
            [("Baseline Part Number (including revision level)", r["partial_baseline"], 32)],
            [("Reason for Full / Partial FAI", r["partial_reason"], 32)],
            ("note", "Detail part: go to field 19. Assembly: complete the INDEX below."),
            ("note", "INDEX of part numbers or sub-assembly numbers required to make the assembly noted above."),
        ])
        idx = self._list_table(["15. Part Number", "16. Part Name", "17. Part Type", "18. FAIR Identifier"],
                               [8, 8, 8, 8],
                               [[c["part_number"], c["part_name"], c["notes"], c["fai_report_number"]]
                                for c in self.comps], min_rows=4)
        sig = self._lv([
            [("19. Does FAIR Contain a Documented Nonconformance(s)?", None, 24),
             (Paragraph(f"<b>{'Yes' if has_nc else 'No'}</b>", self.st["cell"]), None, 8)],
            [("20. FAIR Verified By", r["prepared_by"], 24), ("21. Date", r["prepared_date"], 8)],
            [("22. FAIR Reviewed/Approved By", r["reviewed_by"], 24), ("23. Date", r["reviewed_date"], 8)],
            [("24. Customer Approval", r["customer_approval"], 24), ("25. Date", r["customer_approval_date"], 8)],
            [("26. Comments", r["comments"], 32)],
        ])
        return [head, idx, sig]

    def form2_clean(self):
        r = self.rep
        mats = [m for m in self.mats if m["category"] != "Functional Test"]
        tests = [m for m in self.mats if m["category"] == "Functional Test"]
        head = self._lv([("title", "AS9102C — FORM 2"),
                         ("text", "PRODUCT ACCOUNTABILITY — MATERIALS, SPECIAL PROCESSES, AND FUNCTIONAL TESTING"),
                         self._ident_clean()])
        mt = self._list_table(
            ["5. Material or Process Name", "6. Specification Number", "7. Code", "8. Supplier",
             "9. Customer Approval Verification", "10. Certificate of Conformance Number"],
            [6, 6, 2, 6, 6, 6],
            [[m["name"], m["spec_number"], m["code"],
              " / ".join(x for x in (m["supplier"], m["supplier_code"]) if x), m["customer_approval"],
              m["coc_number"]] for m in mats], min_rows=8)
        ft = self._list_table(["11. Functional Test Procedure Number", "12. Acceptance Report Number"], [16, 16],
                              [[f"{t['name']} {t['spec_number']}".strip(), t["coc_number"]] for t in tests],
                              min_rows=3)
        notes = [m["comments"] for m in self.mats
                 if m["comments"] and not re.match(r"^From (note|char)\b", m["comments"], re.I)]
        com = self._lv([[("13. Comments", "\n".join(notes), 32)]])
        return [head, mt, ft, com]

    def form3_clean(self):
        head = self._lv([("title", "AS9102C — FORM 3"),
                         ("text", "CHARACTERISTIC ACCOUNTABILITY, VERIFICATION, AND COMPATIBILITY EVALUATION"),
                         self._ident_clean()])
        rows = [[c["char_no"], c["ref_location"], c["designator"], c["requirement"], c["results"], c["tooling"],
                 c["nonconformance"], c["comments"]] for c in self.chars]
        tbl = self._list_table(
            ["5. Char. No.", "6. Reference Location", "7. Characteristic Designator", "8. Requirement", "9. Results",
             "10. Designed / Qualified Tooling", "11. Nonconformance Number", "12. Additional Data / Comments"],
            [2, 3, 4, 6, 5, 4, 4, 4], rows, min_rows=12,
            group=[("Characteristic Accountability", 4), ("Inspection / Test Results", 3), ("", 1)])
        return [head, tbl]

    # ------------------------------------------------------------------ Standard report (Form 3 layout)
    def standard_report(self):
        """One inspection report laid out like AS9102 Form 3 (same cells, same columns + Accept / Reject)."""
        r = self.rep
        n = len(self.chars)
        acc = sum(1 for c in self.chars if c["disposition"] == "Accept")
        rej = sum(1 for c in self.chars if c["disposition"] == "Reject")
        na = sum(1 for c in self.chars if c["disposition"] == "N/A")
        opn = n - acc - rej - na
        org = r["organization_name"] or self.meta.get("company_name", "")
        cage = r["supplier_code"] or self.meta.get("cage_code", "")
        head = self._lv([
            ("title", "INSPECTION REPORT", self.logo), ("text", "CHARACTERISTIC ACCOUNTABILITY AND VERIFICATION"),
            [("Part Number", r["part_number"], 8), ("Part Name", r["part_name"], 8),
             ("Serial / Lot Number", self._serial_text(), 8), ("Report Number", r["report_number"], 8)],
            [("Part Revision Level", r["part_rev"], 8), ("Drawing Number", r["drawing_number"], 8),
             ("Drawing Revision Level", r["drawing_rev"], 8), ("Customer", r["customer"], 8)],
            [("Purchase Order Number", r["po_number"], 8), ("Job / Work Order", r["job_number"], 8),
             ("Quantity / Sample Size", " / ".join(x for x in (r["quantity"], r["sample_size"]) if x), 8),
             ("Organization / Supplier Code", " / ".join(x for x in (org, cage) if x), 8)],
        ])
        rows = [[c["char_no"], c["ref_location"], c["designator"], c["requirement"], c["results"],
                 Paragraph(f"<b>{escape(c['disposition'] or '')}</b>", self.st["cell"]), c["tooling"],
                 c["nonconformance"], c["comments"]] for c in self.chars]
        tbl = self._list_table(
            ["Char. No.", "Reference Location", "Characteristic Designator", "Requirement", "Results",
             "Accept / Reject", "Designed / Qualified Tooling", "Nonconformance Number", "Additional Data / Comments"],
            [2, 3, 3, 6, 4, 3, 4, 3, 4], rows, min_rows=10,
            group=[("Characteristic Accountability", 4), ("Inspection / Test Results", 4), ("", 1)])
        tol = (f"X ±{r['tol_0dec']}   " if r.get("tol_0dec") else "") + \
            f".X ±{r['tol_1dec']}   .XX ±{r['tol_2dec']}   .XXX ±{r['tol_3dec']}   .XXXX ±{r['tol_4dec']}   " \
            f"Angles ±{r['tol_angle']}°   ({r['units']})"
        foot = self._lv([
            [("Result Summary", f"{n} characteristics:  {acc} accepted,  {rej} rejected,  {na} N/A"
              + (f",  {opn} open" if opn else ""), 16),
             ("Title Block Tolerances (unless otherwise specified)", tol, 16)],
            [("Inspected By", r["prepared_by"], 12), ("Date", r["prepared_date"], 4),
             ("Reviewed / Approved By", r["reviewed_by"], 12), ("Date", r["reviewed_date"], 4)],
            [("Comments", r["comments"], 32)],
        ])
        return [head, tbl, Spacer(1, 0), KeepTogether(foot)] + self.per_part()

    # ------------------------------------------------------------------ Inspection report
    def inspection(self):
        r, st, W = self.rep, self.st, self.W
        n = len(self.chars)
        acc = sum(1 for c in self.chars if c["disposition"] == "Accept")
        rej = sum(1 for c in self.chars if c["disposition"] == "Reject")
        na = sum(1 for c in self.chars if c["disposition"] == "N/A")
        cw = W / 6
        out = [Paragraph("Dimensional Inspection Report", st["title"]), Spacer(1, 4),
               self._grid([
                   [box("", "Report Number", r["report_number"], st, True), box("", "Part Number", r["part_number"], st, True),
                    box("", "Part Name", r["part_name"], st), box("", "Revision", r["part_rev"], st),
                    box("", "Drawing / Rev", f"{r['drawing_number']} {r['drawing_rev']}".strip(), st),
                    box("", "Customer", r["customer"], st)],
                   [box("", "PO Number", r["po_number"], st), box("", "Job / WO", r["job_number"], st),
                    box("", "Serial / Lot", self._serial_text(), st), box("", "Qty / Sample", f"{r['quantity']} / {r['sample_size']}", st),
                    box("", "Inspector / Date", f"{r['prepared_by']}  {r['prepared_date']}", st),
                    box("", "Approved / Date", f"{r['reviewed_by']}  {r['reviewed_date']}", st)],
               ], [cw] * 6),
               Spacer(1, 4),
               self._grid([[Paragraph(f"<b>Characteristics:</b> {n} &nbsp;&nbsp; <font color='#146c2e'><b>Accepted:</b> {acc}</font>"
                                      f" &nbsp;&nbsp; <font color='#B42318'><b>Rejected:</b> {rej}</font> &nbsp;&nbsp; <b>N/A:</b> {na}"
                                      f" &nbsp;&nbsp; <b>Open:</b> {n - acc - rej - na} &nbsp;&nbsp; <b>Units:</b> {escape(r['units'])}"
                                      f" &nbsp;&nbsp; <b>Status:</b> {escape(r['status'])}", st["cell"])]], [W]),
               Spacer(1, 6)]
        heads = ("Char", "Loc", "Type", "Requirement", "Nominal", "Tol", "LSL", "USL", "Actual", "Status", "Gage",
                 "NCR", "Comments")
        cols = ("char_no", "loc", "char_type", "requirement", "nominal", "tol", "lower_limit", "upper_limit",
                "results", "disposition", "tooling", "nonconformance", "comments")
        widths = [0.42, 0.6, 0.72, 1.85, 0.62, 0.95, 0.62, 0.62, 0.95, 0.6, 0.75, 0.6]
        widths = [w * inch for w in widths]
        widths.append(W - sum(widths))
        body, extra = self._char_rows(cols)
        rows = [[P(h, st["hdr"]) for h in heads]] + (body or [[P("", st["cell"]) for _ in heads]])
        out.append(self._grid(rows, widths, header_rows=1, zebra=True, extra=extra, repeat=1))
        if r["comments"]:
            out += [Spacer(1, 6), self._grid([[box("", "Comments", r["comments"], st)]], [W])]
        return out + self.per_part()

    # ------------------------------------------------------------------ multi-part readings
    def _serial_text(self) -> str:
        r = self.rep
        if not mp.applies(r) or mp.part_count(r) <= 1:
            return r["serial_number"]
        sns = [s for s in mp.serials(r) if s]
        n = mp.part_count(r)
        if not sns:
            return f"{n} parts"
        return f"{n} parts: " + (", ".join(sns) if len(sns) <= 4 else f"{sns[0]} … {sns[-1]}")

    PARTS_PER_GROUP = 5

    def per_part(self):
        """Per-part dimensional results (Inspection / Standard reports covering several parts or low & high).
        Printed in groups of 5 parts: characteristics 1…N for P1–P5, then a new page with 1…N for P6–P10, and so on.
        The characteristic columns repeat on every group; the page number runs across the whole document."""
        r, st, W = self.rep, self.st, self.W
        if not mp.applies(r):
            return []
        n, mm = mp.part_count(r), mp.minmax(r)
        sns = mp.serials(r)
        plain = self.clean or self.standard
        hdr = st["hdr"] if not plain else ParagraphStyle("hp", parent=st["hdr"], textColor=colors.black)
        cell = ParagraphStyle("pp", parent=st["cell"], fontSize=7.5, leading=9)
        g = self.PARTS_PER_GROUP
        groups = [list(range(a, min(n, a + g - 1) + 1)) for a in range(1, n + 1, g)]
        # judged again here so the red marks always match the current limits and reading mode
        readings = [(c, mp.recompute(mp.parse(c.get("part_readings")), c, mm)) for c in self.chars]
        fixed = [0.42 * inch, 2.1 * inch, 0.66 * inch, 0.66 * inch]
        mode = "low & high reading per part" if mm else "one reading per part"
        out = []
        for gi, parts in enumerate(groups, start=1):
            out += [PageBreak(),
                    Paragraph(f"Per-Part Dimensional Results — Parts {parts[0]}–{parts[-1]} of {n}"
                              + (f" &nbsp;<font size=9>(group {gi} of {len(groups)})</font>" if len(groups) > 1 else ""),
                              st["title"]),
                    Paragraph(escape(f"{r['part_number']}  Rev {r['part_rev']}  ·  {mode}"), st["small"]),
                    Spacer(1, 5)]
            h1 = [P(h, hdr) for h in ("Char", "Requirement", "LSL", "USL")]
            h2 = [P("", hdr) for _ in range(4)]
            style = []
            col = 4
            for p in parts:
                lab = f"P{p}" + (f"\n{sns[p - 1]}" if sns[p - 1] else "")
                if mm:
                    h1 += [P(lab, hdr), P("", hdr)]
                    h2 += [P("Low", hdr), P("High", hdr)]
                    style.append(("SPAN", (col, 0), (col + 1, 0)))
                    col += 2
                else:
                    h1.append(P(lab, hdr))
                    col += 1
            head_rows = 2 if mm else 1
            data = [h1, h2] if mm else [h1]
            if mm:
                for c0 in range(4):
                    style.append(("SPAN", (c0, 0), (c0, 1)))
            for ri, (c, rd) in enumerate(readings, start=head_rows):
                row = [Paragraph(f"<b>{escape(c['char_no'])}</b>", cell), P(c["requirement"], cell),
                       P(c["lower_limit"], cell), P(c["upper_limit"], cell)]
                cc = 4
                for p in parts:
                    x = rd.get(p) or {}
                    vals = (x.get("lo", ""), x.get("hi", "")) if mm else (x.get("lo", ""),)
                    for v in vals:
                        if mp.value_rejected(v, c, x.get("d", "")):
                            if self.clean:
                                row.append(Paragraph(f"<b>{escape(v)}</b> *", cell))
                            else:
                                row.append(Paragraph(f"<font color='#B42318'><b>{escape(v)}</b></font>", cell))
                                style.append(("BACKGROUND", (cc, ri), (cc, ri), REJ))
                        else:
                            row.append(P(v, cell))
                        cc += 1
                data.append(row)
            ncols = g * (2 if mm else 1)                  # same column widths on every group, even a short last one
            pw = min(1.3 * inch, (W - sum(fixed)) / ncols)
            widths = fixed + [pw] * (len(parts) * (2 if mm else 1))
            t = grid_table(data, widths, header_rows=head_rows, zebra=True, extra=style, repeat=head_rows, clean=plain)
            t.hAlign = "LEFT"
            out.append(t)
            if self.clean:
                out += [Spacer(1, 3), Paragraph("* reading out of tolerance", st["small"])]
        return out

    # ------------------------------------------------------------------ photos
    def photos(self):
        st = self.st
        photos = [a for a in self.atts if a["kind"] == "Photo" and a["include_in_report"] and is_image(a["filename"])]
        if not photos:
            return []
        out = [Paragraph("Photographs", st["title"]), Spacer(1, 6)]
        cell_w, cell_h = (self.W - 12) / 2, 3.2 * inch
        cells = []
        for a in photos:
            im = open_image_page(self.db.attachment_bytes(a["id"]), 0).convert("RGB")
            im.thumbnail((2000, 2000))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85)
            buf.seek(0)
            iw, ih = im.size
            s = min(cell_w / iw, cell_h / ih)
            img = RLImage(buf, width=iw * s, height=ih * s)
            cap = P(a["description"] or a["filename"], st["cap"])
            cells.append([img, cap])
        rows = []
        for i in range(0, len(cells), 2):
            pair = cells[i:i + 2]
            rows.append([p[0] for p in pair] + ([""] if len(pair) == 1 else []))
            rows.append([p[1] for p in pair] + ([""] if len(pair) == 1 else []))
        t = Table(rows, colWidths=[cell_w + 6, cell_w + 6])
        t.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
        out.append(t)
        return out

    # ------------------------------------------------------------------ build
    def _section_pdf(self, flow, title) -> bytes:
        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=landscape(letter), leftMargin=0.4 * inch, rightMargin=0.4 * inch,
                                topMargin=(0.4 if self.clean else 0.85) * inch, bottomMargin=0.45 * inch,
                                title=f"{self.rep['report_number']} {title}", author=self.db.user)
        doc.build(flow, onFirstPage=self._on_page(title), onLaterPages=self._on_page(title))
        return buf.getvalue()

    def build(self, out_path: str | Path) -> Path:
        out = pymupdf.open()
        if self.is_fai:
            f1, f2, f3 = (self.form1_clean, self.form2_clean, self.form3_clean) if self.clean else \
                (self.form1, self.form2, self.form3)
            if self.o.form1:
                out.insert_pdf(pymupdf.open(stream=self._section_pdf(f1(), "FAI — Form 1"), filetype="pdf"))
            if self.o.form2:
                out.insert_pdf(pymupdf.open(stream=self._section_pdf(f2(), "FAI — Form 2"), filetype="pdf"))
            if self.o.form3:
                out.insert_pdf(pymupdf.open(stream=self._section_pdf(f3(), "FAI — Form 3"), filetype="pdf"))
        elif self.standard:
            out.insert_pdf(pymupdf.open(stream=self._section_pdf(self.standard_report(), "Standard Report"),
                                        filetype="pdf"))
        else:
            out.insert_pdf(pymupdf.open(stream=self._section_pdf(self.inspection(), "Inspection Report"),
                                        filetype="pdf"))
        if self.o.drawings:
            self._append_drawings(out)
        if self.o.photos:
            ph = self.photos()
            if ph:
                out.insert_pdf(pymupdf.open(stream=self._section_pdf(ph, "Photographs"), filetype="pdf"))
        if self.o.append_pdfs:
            for a in self.atts:
                if a["kind"] not in ("Drawing", "Photo") and a["include_in_report"] and is_pdf(a["filename"]):
                    try:
                        out.insert_pdf(pymupdf.open(stream=self.db.attachment_bytes(a["id"]), filetype="pdf"))
                    except Exception:
                        pass
        self._stamp(out)
        out_path = Path(out_path)
        out.set_metadata({"title": f"{self.rep['report_number']} — {self.rep['part_number']} Rev {self.rep['part_rev']}",
                          "author": self.db.user, "creator": "ADMI FAI Studio",
                          "subject": self.rep["report_type"]})
        out.save(out_path, garbage=3, deflate=True)
        out.close()
        return out_path

    def _balloon_color(self, disp):
        if self.clean:
            return (0, 0, 0)                               # plain layout: black balloons
        return {"Reject": (0.71, 0.14, 0.09), "Accept": (0.08, 0.42, 0.18)}.get(disp, (1.0, 0.416, 0.0))

    def _draw_balloons(self, page, att_id, pno, W, H):
        size = max(W, H)
        font = pymupdf.Font(fontfile=str(asset_path("fonts", "DejaVuSans-Bold.ttf")))
        page.insert_font(fontname="dvb", fontbuffer=font.buffer)
        for c in self.chars:
            if c["balloon_attachment_id"] != att_id or (c["balloon_page"] or 0) != pno or c["balloon_x"] is None:
                continue
            x, y = c["balloon_x"] * W, c["balloon_y"] * H
            bs = min(max(c.get("balloon_size") or 0.0085, 0.0035), 0.02)
            r = size * bs * self.o.balloon_scale
            col = self._balloon_color(c["disposition"])
            page.draw_circle((x, y), r, color=col, fill=(1, 1, 1), width=max(0.8, r * 0.14), overlay=True)
            txt = str(c["char_no"])
            fs = r * (1.05 if len(txt) <= 2 else 0.8)
            tw = font.text_length(txt, fontsize=fs)
            page.insert_text((x - tw / 2, y + fs * 0.36), txt, fontsize=fs, fontname="dvb", color=col, overlay=True)

    def _append_drawings(self, out):
        for a in self.atts:
            if a["kind"] != "Drawing" or not a["include_in_report"]:
                continue
            data = self.db.attachment_bytes(a["id"])
            if is_pdf(a["filename"]):
                src = pymupdf.open(stream=data, filetype="pdf")
                start = out.page_count
                out.insert_pdf(src)
                for pno in range(src.page_count):
                    page = out[start + pno]
                    if page.rotation:
                        page.remove_rotation()
                    W, H = page.rect.width, page.rect.height
                    self._draw_balloons(page, a["id"], pno, W, H)
            elif is_image(a["filename"]):
                from .docrender import page_count_for
                n = page_count_for(data, a["filename"])
                for pno in range(n):
                    im = open_image_page(data, pno).convert("RGB")
                    w_pt, h_pt = page_size_points(data, a["filename"], pno)
                    # normalize to at least letter size for printing
                    s = max(792 / max(w_pt, h_pt), 1.0) if max(w_pt, h_pt) < 792 else 1.0
                    W, H = w_pt * s, h_pt * s
                    page = out.new_page(width=W, height=H)
                    buf = io.BytesIO()
                    im.save(buf, "JPEG", quality=88)
                    page.insert_image(page.rect, stream=buf.getvalue())
                    self._draw_balloons(page, a["id"], pno, W, H)

    def _stamp(self, out):
        n = out.page_count
        fontfile = str(asset_path("fonts", "DejaVuSans.ttf"))
        font = pymupdf.Font(fontfile=fontfile)
        r = self.rep
        gen = datetime.now().strftime("%Y-%m-%d %H:%M")
        for i, page in enumerate(out):
            page.insert_font(fontname="dv", fontbuffer=font.buffer)
            W, H = page.rect.width, page.rect.height
            fs = max(6.5, min(W, H) / 612 * 6.5)
            fs_pg = fs * 1.35                           # the page number is printed larger than the footer text
            left = f"{r['report_number']}  |  P/N {r['part_number']} Rev {r['part_rev']}  |  Generated {gen}"
            right = f"Page {i + 1} of {n}"
            if not self.clean:                          # the plain AS9102C forms only carry the page number
                page.insert_text((0.4 * 72, H - 0.2 * 72), left, fontsize=fs, fontname="dv", color=(0.3, 0.3, 0.3))
            tw = font.text_length(right, fontsize=fs_pg)
            page.insert_text((W - 0.4 * 72 - tw, H - 0.2 * 72), right, fontsize=fs_pg, fontname="dv",
                             color=(0, 0, 0))


def build_pdf(db, report_id: int, out_path: str | Path, opts: ReportOptions | None = None) -> Path:
    return Builder(db, report_id, opts or ReportOptions()).build(out_path)


def build_preview(db, report_id: int, opts: ReportOptions | None = None) -> Path:
    tmp = Path(tempfile.gettempdir()) / f"fai_preview_{report_id}_{datetime.now():%H%M%S%f}.pdf"
    return build_pdf(db, report_id, tmp, opts)
