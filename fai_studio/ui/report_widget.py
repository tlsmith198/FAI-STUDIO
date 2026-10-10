"""One open report: Form 1 / Form 2 / Form 3 + drawing / attachments / preview / history."""
from __future__ import annotations

import io
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Optional

import pymupdf
from PySide6.QtCore import QItemSelectionModel, QModelIndex, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QAbstractItemDelegate, QAbstractItemView, QComboBox, QFileDialog, QFormLayout, QFrame,
                               QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
                               QMenu, QMessageBox, QPlainTextEdit, QProgressDialog, QPushButton, QScrollArea,
                               QSplitter, QTableView, QTableWidget, QTableWidgetItem, QTabWidget, QToolButton,
                               QVBoxLayout, QWidget)

from ..db import ConflictError
from ..docrender import is_image, is_pdf, is_viewable, render_page
from ..importers import (COMPONENT_FIELDS, MATERIAL_FIELDS, RESULT_FIELDS, parse_notes, read_notes_text,
                         read_table)
from ..report_types import REPORT_TYPES, is_fai
from ..tolerance import (classify_spec, evaluate, find_specs, has_bonus, out_of_tolerance_values, parse_requirement,
                         recompute_limits)
from .common import (ACCEPT_BG, ACCEPT_FG, NA_FG, REJECT_BG, REJECT_FG, STATUS_COLORS, FlowRow, ResponsiveForm,
                     ResponsiveGrid, guard)
from .dialogs import (DRAWING_FILTER, NOTES_FILTER, PHOTO_FILTER, TABLE_FILTER, CandidateDialog, IssuesDialog,
                      MappingDialog, NotesDialog, ReportOptionsWidget)
from .drawing_view import DrawingViewer
from .table_model import ClipTable, Col, RecordDelegate, RecordModel
from .. import multipart as mp
from .. import ocr_learn as ol

DESIGNATORS = ["", "Key", "Critical", "Major", "Minor"]
CHAR_TYPES = ["Dimension", "GD&T", "Angle", "Note", "Thread", "Surface Finish", "Material", "Visual",
              "Functional", "Reference", "Basic", "Other"]
DISPOSITIONS = ["", "Accept", "Reject", "N/A"]
GENERAL_NOTE_RE = re.compile(r"INTERPRET|DIMENSIONING AND TOLERANCING|ASME Y14|DIMENSIONS ARE IN|UNLESS OTHERWISE",
                             re.I)

CHAR_COLS = [
    Col("char_no", "Char\nNo.", 48, "int", tip="5. Characteristic number (balloon)"),
    Col("ref_location", "Ref\nLoc", 70, tip="6. Reference location — sheet / zone"),
    Col("designator", "Desig.", 62, "combo", DESIGNATORS, "7. Characteristic designator (Key / Critical …)"),
    Col("char_type", "Type", 86, "combo", CHAR_TYPES),
    Col("requirement", "Requirement", 230, "gdt", tip="8. Requirement — type the callout, e.g. 1.250 ±.005, Ø.250 +.002/-.000, "
                                              ".010 MAX, ⌖ Ø.014 Ⓜ A B C. Nominal / limits are calculated."),
    Col("nominal", "Nominal", 66),
    Col("tol_plus", "+Tol", 54),
    Col("tol_minus", "−Tol", 54),
    Col("lower_limit", "LSL", 64),
    Col("upper_limit", "USL", 64),
    Col("results", "Results", 120, tip="9. Actual result(s). Multiple samples: separate with commas or spaces. "
                                       "Text such as OK / CONFORMS is accepted."),
    Col("disposition", "Acc/\nRej", 62, "combo", DISPOSITIONS, "Auto-set from results and limits; can be overridden"),
    Col("tooling", "Tooling /\nGage", 110, "gage", tip="10. Designed / qualified tooling or gage: pick from the Gage "
        "Library (drop-down) or type any part of the gage ID / description to find it"),
    Col("nonconformance", "NCR\nNo.", 70, tip="11. Nonconformance number"),
    Col("comments", "Comments", 160),
    Col("_balloon", "Balloon", 62, "readonly"),
]
COMP_COLS = [
    Col("seq", "Seq", 44, "int"), Col("item_no", "Item", 50), Col("part_number", "15. Part Number", 170),
    Col("part_name", "16. Part Name", 220), Col("qty", "Qty", 50), Col("serial_number", "17. Serial Number", 130),
    Col("fai_report_number", "18. FAI Report Number", 150), Col("notes", "Notes / Material", 220),
]
MAT_COLS = [
    Col("seq", "Seq", 44, "int"),
    Col("category", "Type", 120, "combo", ["Material", "Special Process", "Functional Test", "Other"]),
    Col("name", "5. Material / Process Name", 220), Col("spec_number", "6. Specification No.", 150),
    Col("code", "7. Code", 80), Col("supplier", "Supplier", 140), Col("supplier_code", "8. Supplier Code", 110),
    Col("customer_approval", "9. Cust. Approval", 100, "combo", ["", "Yes", "No", "N/A"]),
    Col("coc_number", "10. C of C / Report No.", 150), Col("comments", "Comments", 200),
]
ATT_COLS = [
    Col("kind", "Kind", 110, "combo", ["Drawing", "Photo", "Parts List", "Notes List", "Engineering Order",
                                       "Application List", "Specification", "Certificate", "CMM Report", "Other"]),
    Col("filename", "File", 260, "readonly"), Col("page_count", "Pages", 50, "readonly"),
    Col("_size", "Size", 70, "readonly"), Col("description", "Description / Caption", 260),
    Col("include_in_report", "In\nReport", 56, "bool"), Col("uploaded_by", "Uploaded By", 110, "readonly"),
    Col("uploaded_at", "Uploaded", 130, "readonly"),
]

COC_COLS = [
    Col("seq", "Item", 44, "int"),
    Col("item_type", "Type", 92, "combo", ["Material", "Process", "Test", "Purchased"]),
    Col("description", "Material / Process / Test  (printed on the CoC)", 330, "multiline",
        "What is certified - material (size + alloy / temper), purchased part, process or test incl. spec. "
        "Enter = new line."),
    Col("vendor_name", "Vendor", 170, "complete", "Pick from the Vendor Library - the address fills in automatically"),
    Col("vendor_info", "Vendor Address / Info", 210, "multiline"),
    Col("cert_lot", "Cert / Lot #", 150, "multiline", "e.g. PO 1214 / LOT# 9208145 or CERT: 2164487-01"),
    Col("source", "Found in", 170, "readonly", "Where the row came from (parts list FN, note number, Form 2)"),
]
COC_FIELDS = [("coc_part_description", "Part description"), ("coc_pack_list", "Pack list number(s)"),
              ("coc_qty_shipped", "Quantity shipped"), ("coc_date_shipped", "Date shipped"),
              ("coc_certified_by", "Certified by (name)"), ("coc_title", "Title"), ("coc_date", "Date signed")]

HEADER_GROUPS = [
    ("Identification (Form 1: 1–9)", [
        ("report_number", "4. Report number"), ("report_type", "Report type"),
        ("part_number", "1. Part number"), ("part_name", "2. Part name"),
        ("part_rev", "5. Part revision"), ("serial_number", "3. Serial number"),
        ("drawing_number", "6. Drawing number"), ("drawing_rev", "7. Drawing revision"),
        ("additional_changes", "8. Additional changes"), ("mfg_process_ref", "9. Mfg process reference"),
    ]),
    ("Order / Organization (10–12)", [
        ("organization_name", "10. Organization name"), ("supplier_code", "11. Supplier code"),
        ("po_number", "12. PO number"), ("customer", "Customer"),
        ("job_number", "Job / work order"), ("quantity", "Quantity"), ("sample_size", "Sample size"),
    ]),
    ("FAI Type (13–14)", [
        ("detail_or_assembly", "13. Detail / Assembly"), ("fai_type", "14. Full / Partial"),
        ("partial_baseline", "Baseline P/N incl. rev"), ("partial_reason", "Reason for partial FAI"),
    ]),
    ("Units && Title-Block Tolerances", [
        ("units", "Units"), ("tol_0dec", "X (whole)  ±"), ("tol_1dec", ".X  ±"), ("tol_2dec", ".XX  ±"), ("tol_3dec", ".XXX  ±"),
        ("tol_4dec", ".XXXX  ±"), ("tol_angle", "Angles ± (deg)"),
    ]),
    ("Sign-off (19–25)", [
        ("prepared_by", "20. FAI complete / inspector"), ("prepared_date", "21. Date"),
        ("reviewed_by", "22. Reviewed by"), ("reviewed_date", "23. Date"),
        ("customer_approval", "24. Customer approval"), ("customer_approval_date", "25. Date"),
    ]),
]
HEADER_COMBOS = {"report_type": REPORT_TYPES, "detail_or_assembly": ["Detail", "Assembly"],
                 "fai_type": ["Full", "Partial"], "units": ["in", "mm"]}
DATE_FIELDS = {"prepared_date", "reviewed_date", "customer_approval_date", "coc_date_shipped", "coc_date"}


def human_size(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


class FocusPlainText(QPlainTextEdit):
    committed = Signal()

    def focusOutEvent(self, e):
        super().focusOutEvent(e)
        self.committed.emit()


class CharTable(ClipTable):
    """Enter commits and moves down — fast result entry. Copy / paste / fill down like a spreadsheet."""

    def closeEditor(self, editor, hint):
        if hint == QAbstractItemDelegate.SubmitModelCache:
            super().closeEditor(editor, QAbstractItemDelegate.NoHint)
            cur = self.currentIndex()
            nxt = cur.sibling(cur.row() + 1, cur.column())
            while nxt.isValid() and self.isRowHidden(nxt.row()):
                nxt = nxt.sibling(nxt.row() + 1, nxt.column())
            if nxt.isValid():
                self.setCurrentIndex(nxt)
                self.edit(nxt)
            return
        super().closeEditor(editor, hint)


class ReportWidget(QWidget):
    titleChanged = Signal(int, str)

    def __init__(self, db, report_id: int, cfg, parent=None):
        super().__init__(parent)
        self.db = db
        self.cfg = cfg
        self.rid = report_id
        self.rep: dict = db.get_report(report_id)
        self._pending: list[dict] = []
        self._preview_dirty = True
        self._history_dirty = True
        self.setAcceptDrops(True)

        self._event_timer = QTimer(self)
        self._event_timer.setSingleShot(True)
        self._event_timer.timeout.connect(self._process_events)
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.timeout.connect(self._auto_preview)

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        if getattr(db, "is_local", False):
            src = (f"checked out from the server as {self.rep['report_number']}" if self.rep.get("server_id")
                   else "created offline - it gets its real report number when it syncs")
            banner = QLabel(f"💼 <b>Offline copy on this PC</b> — {src}. Everything works here; changes upload "
                            "when you <b>⇅ Sync</b>. Approval is done online after syncing.")
            banner.setWordWrap(True)
            banner.setStyleSheet("background:#FFF4D6; border:1px solid #E8C66A; border-radius:4px; padding:5px 8px;")
            root.addWidget(banner)
        root.addWidget(self._build_header_bar())
        self.tabs = QTabWidget()
        self.tabs.currentChanged.connect(self._tab_changed)
        root.addWidget(self.tabs, 1)
        self.tabs.addTab(self._build_form1(), "Form 1 · Header && Parts")
        self.tabs.addTab(self._build_form2(), "Form 2 · Materials && Processes")
        self.tabs.addTab(self._build_form3(), "Form 3 · Characteristics && Drawing")
        self.tabs.addTab(self._build_attachments(), "Attachments")
        self.coc_tab = self._build_coc()
        self.tabs.addTab(self.coc_tab, "Certificate of Conformance")
        self.preview_tab = self._build_preview()
        self.tabs.addTab(self.preview_tab, "Report Preview && Export")
        self.history_tab = self._build_history()
        self.tabs.addTab(self.history_tab, "History")
        from .common import is_small_screen
        full = ["Form 1 · Header && Parts", "Form 2 · Materials && Processes", "Form 3 · Characteristics && Drawing",
                "Attachments", "Certificate of Conformance", "Report Preview && Export", "History"]
        short = ["Form 1", "Form 2", "Form 3 · Characteristics", "Attachments", "CoC", "Preview && Export", "History"]
        compact = is_small_screen(self)
        for i, (f, sh) in enumerate(zip(full, short)):
            self.tabs.setTabText(i, sh if compact else f)
            self.tabs.setTabToolTip(i, f.replace("&&", "&"))
        self.tabs.setCurrentIndex(2)
        self.reload_all()

    # ======================================================================= header bar
    def _build_header_bar(self):
        f = QFrame()
        f.setStyleSheet("QFrame#hb { background: white; border: 1px solid #D0D3D6; border-radius: 5px; }")
        f.setObjectName("hb")
        lay = QHBoxLayout(f)
        self.title_lbl = QLabel()
        self.title_lbl.setStyleSheet("font-size: 15px; font-weight: bold;")
        self.status_lbl = QLabel()
        self.progress_lbl = QLabel()
        self.progress_lbl.setStyleSheet("color:#4a4f52;")
        self.lastchg_lbl = QLabel()
        self.lastchg_lbl.setStyleSheet("color:#6b6f72; font-size: 11px;")
        self.btn_check = QPushButton("✔ Completeness Check")
        self.btn_check.clicked.connect(self.check)
        self.btn_submit = QPushButton("Submit for Review")
        self.btn_submit.clicked.connect(lambda: self.set_status("In Review"))
        self.btn_approve = QPushButton("Approve && Lock")
        self.btn_approve.setObjectName("primary")
        self.btn_approve.clicked.connect(lambda: self.set_status("Approved"))
        self.btn_return = QPushButton("Return for Correction")
        self.btn_return.clicked.connect(lambda: self.set_status("Rejected"))
        self.btn_reopen = QPushButton("Reopen")
        self.btn_reopen.clicked.connect(lambda: self.set_status("Draft"))
        self.btn_pdf = QPushButton("⬇ Export PDF")
        self.btn_pdf.clicked.connect(self.export_pdf)
        self.title_lbl.setWordWrap(True)
        grid = ResponsiveGrid(min_col_width=520, max_cols=2, stretches=(3, 2))
        info = QWidget()
        col = QVBoxLayout(info)
        col.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        top.addWidget(self.title_lbl, 1)
        top.addWidget(self.status_lbl)
        col.addLayout(top)
        bot = FlowRow(spacing=16)
        bot.addWidget(self.progress_lbl)
        bot.addWidget(self.lastchg_lbl)
        col.addWidget(bot)
        btns = FlowRow()
        for b in (self.btn_check, self.btn_submit, self.btn_return, self.btn_approve, self.btn_reopen, self.btn_pdf):
            btns.addWidget(b)
        grid.add(info)
        grid.add(btns)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.addWidget(grid)
        return f

    def _refresh_header_bar(self):
        r = self.rep
        self.title_lbl.setText(f"{r['report_number']}  ·  {r['part_number']}  Rev {r['part_rev'] or '—'}  ·  "
                               f"{r['part_name']}")
        color = STATUS_COLORS.get(r["status"], "#6b6f72")
        lock = "  🔒" if r["locked"] else ""
        self.status_lbl.setText(f"<span style='background:{color}; color:white; padding:2px 8px; border-radius:3px;'>"
                                f"&nbsp;{r['status']}{lock}&nbsp;</span> &nbsp; <span style='color:#4a4f52'>"
                                f"{r['report_type']}</span>")
        upd = r["updated_at"].astimezone().strftime("%Y-%m-%d %H:%M") if r.get("updated_at") else ""
        self.lastchg_lbl.setText(f"Last change: {r['updated_by']} · {upd}")
        chars = self.char_model.rows if hasattr(self, "char_model") else []
        n = len(chars)
        acc = sum(1 for c in chars if c["disposition"] == "Accept")
        rej = sum(1 for c in chars if c["disposition"] == "Reject")
        na = sum(1 for c in chars if c["disposition"] == "N/A")
        unb = sum(1 for c in chars if c.get("balloon_x") is None)
        self.progress_lbl.setText(
            f"<b>{n}</b> characteristics · <span style='color:#146c2e'><b>{acc}</b> accepted</span> · "
            f"<span style='color:#b42318'><b>{rej}</b> rejected</span> · {na} N/A · <b>{n - acc - rej - na}</b> open"
            f" · {unb} un-ballooned")
        st = r["status"]
        self.btn_submit.setEnabled(st in ("Draft", "Rejected"))
        local = getattr(self.db, "is_local", False)
        self.btn_approve.setEnabled(st in ("Draft", "In Review") and not local)
        self.btn_approve.setToolTip("Approval is done online after syncing this offline copy" if local else "")
        self.btn_return.setEnabled(st == "In Review")
        self.btn_reopen.setEnabled(st in ("Approved", "In Review", "Rejected"))
        for b in (self.btn_return, self.btn_reopen, self.btn_submit):      # only show what can be done now
            b.setVisible(b.isEnabled())
        self.titleChanged.emit(self.rid, f"{r['report_number']} · {r['part_number']}")

    # ======================================================================= Form 1
    def _build_form1(self):
        w = QWidget()
        outer = QVBoxLayout(w)
        grid = ResponsiveGrid(min_col_width=360, max_cols=3)
        self.hdr_widgets: dict[str, QWidget] = {}
        for gi, (title, fields) in enumerate(HEADER_GROUPS):
            g = QGroupBox(title)
            fl = QFormLayout(g)
            fl.setLabelAlignment(Qt.AlignRight)
            for key, label in fields:
                if key in HEADER_COMBOS:
                    wdg = QComboBox()
                    wdg.addItems(HEADER_COMBOS[key])
                    wdg.activated.connect(lambda _i, k=key: self._hdr_commit(k))
                    fl.addRow(label, wdg)
                elif key in DATE_FIELDS:
                    wdg = QLineEdit()
                    wdg.setPlaceholderText("YYYY-MM-DD")
                    wdg.editingFinished.connect(lambda k=key: self._hdr_commit(k))
                    today = QToolButton()
                    today.setText("Today")
                    today.clicked.connect(lambda _=False, k=key, e=wdg: self._set_today(k, e))
                    row = QHBoxLayout()
                    row.addWidget(wdg, 1)
                    row.addWidget(today)
                    fl.addRow(label, row)
                else:
                    wdg = QLineEdit()
                    wdg.editingFinished.connect(lambda k=key: self._hdr_commit(k))
                    fl.addRow(label, wdg)
                self.hdr_widgets[key] = wdg
            grid.add(g)
        cg = QGroupBox("26. Comments")
        cl = QVBoxLayout(cg)
        self.comments_edit = FocusPlainText()
        self.comments_edit.setMaximumHeight(90)
        self.comments_edit.committed.connect(lambda: self._hdr_commit("comments"))
        cl.addWidget(self.comments_edit)
        self.hdr_widgets["comments"] = self.comments_edit
        grid.add(cg)
        self._hdr_seen: dict[str, str] = {}
        for key, wdg in self.hdr_widgets.items():
            wdg.setProperty("hdr_key", key)
            wdg.installEventFilter(self)
        top = grid
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(top)
        scroll.setFrameShape(QFrame.NoFrame)

        comp_box = QGroupBox("Index of part numbers / sub-assemblies (Form 1: 15–18) — parts list")
        cl = QVBoxLayout(comp_box)
        tb = FlowRow()
        self.comp_model = RecordModel(COMP_COLS, lambda r, k, v, e: self._edit_simple("components", r, k, v, e))
        self.comp_view = self._make_table(self.comp_model, COMP_COLS)
        for text, fn in (("➕ Add", lambda: self._add_simple("components", self.comp_model)),
                         ("🗑 Delete", lambda: self._delete_simple("components", self.comp_view, self.comp_model)),
                         ("📥 From BOM / Parts List", self.index_from_bom),
                         ("📥 Import Parts List…", self.import_parts_list)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            tb.addWidget(b)
        tb.addStretch(1)
        cl.addWidget(tb)
        cl.addWidget(self.comp_view)
        split = QSplitter(Qt.Vertical)
        split.addWidget(scroll)
        split.addWidget(comp_box)
        split.setSizes([520, 260])
        outer.addWidget(split)
        return w

    def _set_today(self, key, edit):
        from datetime import datetime
        self._hdr_seen[key] = str(self.rep.get(key) or "")
        edit.setText(datetime.now().strftime("%Y-%m-%d"))
        self._hdr_commit(key)

    def _hdr_value(self, key):
        w = self.hdr_widgets[key]
        if isinstance(w, QComboBox):
            return w.currentText()
        if isinstance(w, QPlainTextEdit):
            return w.toPlainText().strip()
        return w.text().strip()

    def eventFilter(self, obj, ev):
        from PySide6.QtCore import QEvent
        if ev.type() == QEvent.FocusIn:
            key = obj.property("hdr_key")
            if key:
                self._hdr_seen[key] = str(self.rep.get(key) or "")
        return super().eventFilter(obj, ev)

    def _hdr_commit(self, key):
        val = self._hdr_value(key)
        seen = self._hdr_seen.pop(key, str(self.rep.get(key) or ""))
        if val == seen:
            return                                   # user didn't change it — never write back stale text
        new = guard(self, lambda: self.db.update_field("reports", self.rid, key, val, expected_old=seen),
                    on_conflict=lambda e: self.reload_header())
        if new:
            self.rep = new
            self._refresh_header_bar()
            self._mark_dirty()
            if key == "report_type":
                self._rejudge_all()
                self._doc_follow = True
                self._sync_doc_combo()
                self.tabs.setTabVisible(1, is_fai(val))
        else:
            self._fill_header()

    def _fill_header(self, force=False):
        for key, w in self.hdr_widgets.items():
            if w.hasFocus() and not force:
                continue
            v = str(self.rep.get(key) or "")
            w.blockSignals(True)
            if isinstance(w, QComboBox):
                i = w.findText(v)
                if i < 0 and v:
                    w.addItem(v)
                    i = w.findText(v)
                w.setCurrentIndex(max(i, 0))
            elif isinstance(w, QPlainTextEdit):
                if w.toPlainText() != v:
                    w.setPlainText(v)
            else:
                if w.text() != v:
                    w.setText(v)
                    w.setCursorPosition(0)
            w.blockSignals(False)
        if not self.rep.get("organization_name"):
            self.hdr_widgets["organization_name"].setPlaceholderText(self.db.get_meta().get("company_name", ""))
            self.hdr_widgets["supplier_code"].setPlaceholderText(self.db.get_meta().get("cage_code", ""))
        self._apply_lock()

    def reload_header(self):
        self.rep = self.db.get_report(self.rid) or self.rep
        self._fill_header()
        self._refresh_header_bar()
        if self._rebuild_char_cols():
            self.reload_chars()
        self._sync_doc_combo()

    # ======================================================================= Form 2
    def _build_form2(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.mat_model = RecordModel(MAT_COLS, lambda r, k, v, e: self._edit_simple("materials", r, k, v, e))
        self.mat_view = self._make_table(self.mat_model, MAT_COLS)
        tb = FlowRow()
        for text, fn in (("➕ Add Material", lambda: self._add_simple("materials", self.mat_model, {"category": "Material"})),
                         ("➕ Add Special Process", lambda: self._add_simple("materials", self.mat_model,
                                                                          {"category": "Special Process"})),
                         ("➕ Add Functional Test", lambda: self._add_simple("materials", self.mat_model,
                                                                          {"category": "Functional Test"})),
                         ("🗑 Delete", lambda: self._delete_simple("materials", self.mat_view, self.mat_model)),
                         ("📥 Import from file…", self.import_parts_list_to_form2),
                         ("🔎 Pull specs from Form 3 notes", self.specs_from_notes)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            tb.addWidget(b)
        tb.addStretch(1)
        info = QLabel("Functional Test rows print in the Form 2 functional-testing block: put the procedure number in "
                      "<i>Specification No.</i> and the acceptance report number in <i>C of C / Report No.</i>")
        info.setStyleSheet("color:#4a4f52;")
        info.setWordWrap(True)
        lay.addWidget(tb)
        lay.addWidget(self.mat_view, 1)
        lay.addWidget(info)
        return w

    # ======================================================================= Form 3
    def _build_form3(self):
        split = QSplitter(Qt.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        tb1 = FlowRow(spacing=4)
        self._cols_sig = None
        self.char_model = RecordModel(list(CHAR_COLS), self._char_edit, self._char_style)
        self.char_view = self._make_table(self.char_model, CHAR_COLS, cls=CharTable)
        self.char_view.selectionModel().currentRowChanged.connect(self._char_row_changed)
        self.char_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.char_view.customContextMenuRequested.connect(self._char_menu)
        self.char_view.itemDelegate().extra_rows_fn = \
            lambda r, reqs: QTimer.singleShot(200, lambda: self._add_composite_rows(self.char_model.rows[r], reqs))
        self.char_view.setToolTip("Type the requirement exactly as on the drawing — limits are calculated. Enter "
                                  "results; Accept/Reject is set automatically. Press Enter to move down.")
        # one compact toolbar: the everyday buttons, everything else in three menus (keeps the table tall on
        # laptops and scaled displays)
        from PySide6.QtWidgets import QMenu, QToolButton

        def menu_button(text, tip, items):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.setPopupMode(QToolButton.InstantPopup)
            m = QMenu(b)
            for it in items:
                if it is None:
                    m.addSeparator()
                    continue
                m.addAction(it[0], it[1])
            b.setMenu(m)
            return b
        for text, fn, tip in (("➕ Add", self.add_char, "Add a characteristic (Ctrl+N)"),
                              ("🗑 Delete", self.delete_chars, "Delete the selected characteristics"),
                              ("▲", lambda: self.move_char(-1), "Move up"), ("▼", lambda: self.move_char(1), "Move down")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            tb1.addWidget(b)
        tb1.addWidget(menu_button("🎈 Balloons ▾", "Renumber, undo or restart the ballooning", [
            ("🔢 Renumber", self.renumber), ("📍 Renumber by balloon position", self.renumber_by_position), None,
            ("↶ Undo Last Ballooning", self.undo_last_balloons), ("🔄 Start Over…", self.start_over)]))
        tb1.addWidget(menu_button("📥 Import ▾", "Import notes or measured results", [
            ("📥 Notes List…", self.import_notes), ("📥 Results (CMM/Excel)…", self.import_results)]))
        tb1.addWidget(menu_button("🛠 Tools ▾", "GD&T, gages, bulk accept, reader rules", [
            ("⌖ GD&T frame for this row…", self.gdt_for_current), ("🔧 Set tooling / gage…", self.bulk_tooling),
            ("✅ Accept selected", lambda: self.bulk_set("disposition", "Accept")), None,
            ("📚 Drawing Reader Rules…", self.reader_rules)]))
        self.btn_drawing = QToolButton()
        self.btn_drawing.setText("◧ Drawing")
        self.btn_drawing.setCheckable(True)
        self.btn_drawing.setToolTip("Show / hide the drawing next to the table (hide it to give the table the "
                                    "whole width)")
        self.btn_drawing.toggled.connect(self._toggle_drawing)
        tb1.addWidget(self.btn_drawing)
        self.char_search = QLineEdit()
        self.char_search.setPlaceholderText("Search…")
        self.char_search.setClearButtonEnabled(True)
        self.char_search.setMinimumWidth(110)
        self.char_search.setMaximumWidth(170)
        self.char_search.textChanged.connect(self._apply_char_filter)
        self.char_filter = QComboBox()
        self.char_filter.addItems(["Show: All", "Open (no disposition)", "Rejected", "Un-ballooned", "Key / Critical"])
        self.char_filter.currentIndexChanged.connect(self._apply_char_filter)
        # search / filter share the second line with the parts controls (Inspection / Standard reports)
        self.parts_bar = self._build_parts_bar()
        ll.setSpacing(3)
        ll.addWidget(tb1)
        ll.addWidget(self.parts_bar)
        ll.addWidget(self.char_view, 1)

        self.viewer = DrawingViewer(self.db.attachment_bytes)
        self.viewer.balloon_scale = self.cfg.balloon_scale
        self.viewer.size_spin.setValue(self.cfg.balloon_scale)
        self.viewer.balloonClicked.connect(self._balloon_clicked)
        self.viewer.balloonMoved.connect(self._balloon_moved)
        self.viewer.addBalloonAt.connect(self._add_balloon_at)
        self.viewer.autoBalloonRequested.connect(self.auto_balloon)
        self.viewer.areaBalloonRequested.connect(self.balloon_area)
        self.viewer.balloonPartRequested.connect(lambda: self.balloon_part())
        self.viewer.ocrSetupRequested.connect(self.ocr_setup)
        self.viewer.balloonContext.connect(self._balloon_menu)
        split.addWidget(left)
        split.addWidget(self.viewer)
        split.setChildrenCollapsible(False)
        split.setSizes([760, 640])
        split.setStretchFactor(0, 11)
        split.setStretchFactor(1, 9)
        self._f3_split = split
        split.setProperty("sized", False)
        split.splitterMoved.connect(lambda *_: self._remember_split())
        self.btn_drawing.setChecked(bool(getattr(self.cfg, "form3_drawing", True)))
        self.viewer.setVisible(self.btn_drawing.isChecked())
        QShortcut(QKeySequence("Ctrl+B"), self, activated=lambda: self.viewer.set_mode(
            "select" if self.viewer.mode == "add" else "add"))
        QShortcut(QKeySequence("Ctrl+N"), self, activated=self.add_char)
        return split

    def _char_style(self, row, key):
        bg = fg = None
        bold = False
        pk = mp.key_of(key)
        if pk is not None:
            r = row.get("_readings", {}).get(pk[0], {})
            if r.get("d") == "Reject":
                # the whole part is shaded; the reading that is out of tolerance is in red bold
                if mp.value_rejected(r.get(pk[1], ""), row, "Reject"):
                    return REJECT_BG, REJECT_FG, True
                return REJECT_BG, None, False
            if r.get("d") == "Accept" and r.get(pk[1]):
                return None, ACCEPT_FG, False
            return None, None, False
        d = row.get("disposition")
        if d == "Reject":
            bg = REJECT_BG
        if key == "disposition":
            fg = ACCEPT_FG if d == "Accept" else REJECT_FG if d == "Reject" else NA_FG
            bold = True
            if d == "Accept":
                bg = ACCEPT_BG
        elif key == "results" and row.get("results"):
            if out_of_tolerance_values(row["results"], row.get("lower_limit"), row.get("upper_limit"),
                                       has_bonus(row.get("requirement") or "")):
                fg, bold = REJECT_FG, True
        elif key == "char_no":
            bold = True
        elif key == "tooling" and (row.get("tooling") or "").strip():
            gm = getattr(self, "_gage_map", None)
            if gm is None:
                self._gage_list()
                gm = self._gage_map
            g = gm.get(row["tooling"].strip().upper())
            if g is None:
                fg = QColor("#B45309")            # not in the Gage Library
            elif str(g.get("cal_due") or "") and str(g["cal_due"]) < __import__("datetime").date.today().isoformat():
                fg, bold = REJECT_FG, True        # calibration overdue
        elif key == "designator" and row.get("designator") in ("Key", "Critical"):
            fg, bold = REJECT_FG, True
        elif key in ("nominal", "tol_plus", "tol_minus", "lower_limit", "upper_limit", "_balloon"):
            fg = NA_FG
        return bg, fg, bold

    def _decorate(self, row: dict) -> dict:
        row = dict(row)
        if mp.applies(self.rep):
            rd = mp.parse(row.get("part_readings"))
            row["_readings"] = rd
            for i in range(1, mp.part_count(self.rep) + 1):
                row[f"_p{i}_lo"] = rd.get(i, {}).get("lo", "")
                row[f"_p{i}_hi"] = rd.get(i, {}).get("hi", "")
        if row.get("balloon_x") is None:
            row["_balloon"] = "—"
        else:
            doc = next((a for a in getattr(self, "att_model", None).rows if a["id"] == row["balloon_attachment_id"]),
                       None) if hasattr(self, "att_model") else None
            row["_balloon"] = f"Sh {(row.get('balloon_page') or 0) + 1}" + (f" · {doc['filename'][:12]}" if doc and
                                                                          len(self.viewer.docs) > 1 else "")
        return row

    def _put_char(self, row, flash=False):
        row = self._decorate(row)
        self.char_model.replace_row(row, flash)
        self.viewer.update_characteristic(row)

    def _defaults(self):
        return {k: self.rep.get(k) for k in ("tol_0dec", "tol_1dec", "tol_2dec", "tol_3dec", "tol_4dec", "tol_angle")}

    def _compute(self, row: dict, key: str, value: Any) -> dict:
        """Derived-field logic shared by table edits and imports."""
        changes = {key: value}
        merged = dict(row)
        merged[key] = value
        if key == "requirement":
            p = parse_requirement(value, self._defaults(), self.rep["units"])
            if p.nominal is not None or p.upper is not None or p.lower is not None:
                changes.update(p.as_fields())
            else:
                changes.update(nominal="", tol_plus="", tol_minus="", lower_limit="", upper_limit="")
            cur_type = row.get("char_type") or "Dimension"
            auto_types = ("Dimension", "GD&T", "Angle", "Thread", "Reference", "Basic", "Note")
            if cur_type == "Note" and p.char_type in ("Dimension", "Note"):
                pass                                   # notes stay notes
            elif cur_type in auto_types and p.char_type != cur_type:
                changes["char_type"] = p.char_type
            if p.bonus and not (row.get("comments") or "").strip():
                changes["comments"] = ("MMC/LMC – bonus tolerance applies: results over the stated value are not "
                                       "auto-rejected; work out the bonus and disposition")
            elif not p.bonus and (row.get("comments") or "").startswith("MMC/LMC – bonus"):
                changes["comments"] = ""
            if p.reference_only and not merged.get("results"):
                changes["disposition"] = "N/A"
                changes["results"] = "REF" if p.char_type == "Reference" else "BASIC"
            elif not p.reference_only and row.get("results") in ("REF", "BASIC") and row.get("disposition") == "N/A":
                changes["results"] = ""          # was auto-filled for a reference dim; now it must be measured
                changes["disposition"] = ""
            merged.update(changes)
        if key in ("nominal", "tol_plus", "tol_minus"):
            lo, hi = recompute_limits(merged["nominal"], merged["tol_plus"], merged["tol_minus"])
            changes.update(lower_limit=lo, upper_limit=hi)
            merged.update(changes)
        if key in ("results", "requirement", "nominal", "tol_plus", "tol_minus", "lower_limit", "upper_limit"):
            if merged.get("disposition") != "N/A" or key == "results":
                ev = evaluate(merged.get("results", ""), merged.get("lower_limit"), merged.get("upper_limit"),
                              has_bonus(merged.get("requirement") or ""))
                if ev:
                    changes["disposition"] = ev
                elif key == "results" and not (merged.get("results") or "").strip():
                    changes["disposition"] = ""
        if mp.applies(self.rep) and key in ("requirement", "nominal", "tol_plus", "tol_minus", "lower_limit",
                                            "upper_limit") and merged.get("part_readings"):
            merged.update(changes)
            rd = mp.recompute(mp.parse(merged["part_readings"]), merged, mp.minmax(self.rep))
            changes["part_readings"] = mp.dump(rd)
            txt, disp = mp.rollup(rd, mp.part_count(self.rep), mp.minmax(self.rep))
            changes["results"] = txt
            if merged.get("disposition") != "N/A":
                changes["disposition"] = disp
        return changes

    def _char_edit(self, row, key, value, expected=None):
        pk = mp.key_of(key)
        if pk is not None:
            return self._part_edit(row, pk[0], pk[1], value)
        changes = self._compute(row, key, value)
        exp = {key: expected if expected is not None else row.get(key)}
        new = guard(self, lambda: self.db.update_fields("characteristics", row["id"], changes, expected=exp),
                    on_conflict=lambda e: self._conflict_reload("characteristics", row["id"]))
        if new:
            if key == "requirement" and (row.get("ocr_read") or row.get("ocr_fix_ids")):
                new = self._learn_from_edit(new) or new
            new = self._decorate(new)
            self.viewer.update_characteristic(new)
            self._after_change()
            if key == "char_no":
                QTimer.singleShot(0, lambda: self.char_model.sort_rows(lambda r: (r["char_no"], r["id"])))
        return new

    def _part_edit(self, row, part: int, which: str, value):
        """One reading for one part. The readings of all parts live in one field, so a simultaneous edit of another
        part on another workstation is merged (re-read, re-apply this cell, retry) instead of being refused."""
        from ..db import ConflictError
        value = (value or "").strip()
        new = None
        for _attempt in range(4):
            fresh = self.db.row("characteristics", row["id"])
            if fresh is None:
                return None
            rd = mp.parse(fresh.get("part_readings"))
            r = rd.setdefault(part, {"lo": "", "hi": "", "d": ""})
            r[which] = value
            rd = mp.recompute(rd, fresh, mp.minmax(self.rep))
            txt, disp = mp.rollup(rd, mp.part_count(self.rep), mp.minmax(self.rep))
            changes = {"part_readings": mp.dump(rd), "results": txt}
            if fresh.get("disposition") != "N/A":
                changes["disposition"] = disp
            try:
                new = self.db.update_fields("characteristics", row["id"], changes,
                                            expected={"part_readings": fresh.get("part_readings")})
                break
            except ConflictError:
                continue
            except Exception as e:  # noqa: BLE001
                guard(self, lambda e=e: (_ for _ in ()).throw(e))
                return None
        if new:
            new = self._decorate(new)
            self.viewer.update_characteristic(new)
            self._after_change()
            self._refresh_parts_status()
        return new

    # ---------------------------------------------------------------- multi-part inspection
    def _build_parts_bar(self):
        from PySide6.QtWidgets import QSpinBox
        bar = QFrame()
        bar.setObjectName("partsbar")
        bar.setStyleSheet("QFrame#partsbar { background:#F4F6F8; border:1px solid #D0D3D6; border-radius:4px; }")
        lay = QVBoxLayout(bar)
        lay.setContentsMargins(6, 2, 6, 2)
        lay.setSpacing(2)
        row = FlowRow(spacing=4)
        self.parts_row = row
        box = QWidget()
        self.parts_box = box
        bl = QHBoxLayout(box)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(4)
        lbl = QLabel("<b>Parts:</b>")
        lbl.setToolTip("How many parts are inspected on this report. More than one part (or low & high readings) "
                       "gives a result column per part.")
        bl.addWidget(lbl)
        self.part_spin = QSpinBox()
        self.part_spin.setRange(1, mp.MAX_PARTS)
        self.part_spin.setToolTip("How many parts are inspected on this report (a lot / sample of e.g. 5–7). "
                                  "Each part gets its own result column.")
        self.part_spin.editingFinished.connect(self._parts_changed)
        self.part_spin.valueChanged.connect(lambda _v: self._parts_timer.start(700))
        self._parts_timer = QTimer(self)
        self._parts_timer.setSingleShot(True)
        self._parts_timer.timeout.connect(self._parts_changed)
        bl.addWidget(self.part_spin)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("One value per part", "single")
        self.mode_combo.addItem("Low & high per part", "minmax")
        self.mode_combo.setToolTip("Low & high: record the lowest and highest reading of each feature on each part "
                                   "(e.g. a bore measured at several depths). Both must be inside the limits.")
        self.mode_combo.activated.connect(lambda _i: self._parts_changed())
        bl.addWidget(self.mode_combo)
        b = QPushButton("🏷 Serials…")
        b.setToolTip("Serial number of each inspected part")
        b.clicked.connect(self.edit_part_serials)
        bl.addWidget(b)
        self.parts_status = QLabel()
        self.parts_status.setTextFormat(Qt.RichText)
        self.parts_status.setWordWrap(True)
        bl.addWidget(self.parts_status, 1)
        for w in (getattr(self, "char_search", None), getattr(self, "char_filter", None)):
            if w is not None:
                row.addWidget(w)
        row.addWidget(box)
        lay.addWidget(row)
        return bar

    def _char_cols(self) -> list:
        if not mp.applies(self.rep):
            return list(CHAR_COLS)
        n, mm = mp.part_count(self.rep), mp.minmax(self.rep)
        sns = mp.serials(self.rep)
        cols = []
        for c in CHAR_COLS:
            if c.key == "results":
                for i in range(1, n + 1):
                    sn = sns[i - 1]
                    head = f"P{i}" + (f" {sn}" if sn else "")
                    tip = f"Part {i}" + (f" — S/N {sn}" if sn else "")
                    if mm:
                        sub = f"\n{sn}" if sn else ""
                        cols.append(Col(f"_p{i}_lo", f"P{i} Low{sub}", 70, tip=tip + " — LOW reading"))
                        cols.append(Col(f"_p{i}_hi", f"P{i} High{sub}", 70, tip=tip + " — HIGH reading"))
                    else:
                        cols.append(Col(f"_p{i}_lo", head if len(head) < 10 else f"P{i}\n{sn}", 74,
                                        tip=tip + " — reading (text such as OK is accepted)"))
                continue
            if c.key == "disposition":
                cols.append(Col("disposition", "All\nParts", 62, "combo", DISPOSITIONS,
                                "Roll-up of every part: Reject if any part is rejected, Accept when all parts are "
                                "measured and accepted. Can be overridden."))
                continue
            cols.append(c)
        return cols

    def _rebuild_char_cols(self, force: bool = False):
        rep = self.rep
        sig = (mp.applies(rep), mp.part_count(rep), mp.minmax(rep), tuple(mp.serials(rep)))
        show_bar = rep.get("report_type") in mp.MULTI_TYPES
        if hasattr(self, "parts_bar"):
            self.parts_box.setVisible(show_bar)
            self.part_spin.blockSignals(True)
            self.part_spin.setValue(mp.part_count(rep))
            self.part_spin.blockSignals(False)
            self.mode_combo.setCurrentIndex(max(0, self.mode_combo.findData(rep.get("reading_mode") or "single")))
        if sig == self._cols_sig and not force:
            return False
        self._cols_sig = sig
        cols = self._char_cols()
        m = self.char_model
        m.beginResetModel()
        m.cols = cols
        m.rows = [self._decorate(r) for r in m.rows]
        m.endResetModel()
        for i, c in enumerate(cols):
            self.char_view.setColumnWidth(i, c.width)
        self._refresh_parts_status()
        return True

    def _parts_changed(self):
        self._parts_timer.stop()
        if self.rep.get("locked"):
            return
        n, mode = self.part_spin.value(), self.mode_combo.currentData()
        if n == mp.part_count(self.rep) and mode == (self.rep.get("reading_mode") or "single"):
            return
        if n < mp.part_count(self.rep):
            lost = [c["char_no"] for c in self.char_model.rows
                    if any(i > n for i in mp.parse(c.get("part_readings")))]
            if lost and QMessageBox.question(
                    self, "Fewer parts", f"Parts {n + 1}–{mp.part_count(self.rep)} already have readings "
                    f"(characteristics {', '.join(map(str, lost[:12]))}{'…' if len(lost) > 12 else ''}).\n\n"
                    "They are kept but hidden and left out of the results. Continue?") != QMessageBox.Yes:
                self._rebuild_char_cols(force=True)
                return
        new = guard(self, lambda: self.db.update_fields("reports", self.rid, {"part_count": n, "reading_mode": mode}))
        if new:
            self.rep = new
            self._rejudge_all()
            self._refresh_header_bar()
            self._mark_dirty()

    def _rejudge_all(self):
        """Part count / reading mode changed: recompute every characteristic's roll-up and redraw the columns."""
        n = mp.part_count(self.rep)
        applies = mp.applies(self.rep)

        def work():
            for c in self.db.rows("characteristics", self.rid):
                if not c.get("part_readings"):
                    continue
                rd = mp.recompute(mp.parse(c["part_readings"]), c, mp.minmax(self.rep))
                txt, disp = mp.rollup(rd, n, mp.minmax(self.rep)) if applies else ("", "")
                ch = {"part_readings": mp.dump(rd)}
                if applies:
                    ch["results"] = txt
                    if c.get("disposition") != "N/A":
                        ch["disposition"] = disp
                if any(str(c.get(k) or "") != str(v) for k, v in ch.items()):
                    self.db.update_fields("characteristics", c["id"], ch)
            return True
        guard(self, work)
        self.reload_chars()
        self._rebuild_char_cols(force=True)

    def _refresh_parts_status(self):
        if not hasattr(self, "parts_status"):
            return
        if not mp.applies(self.rep):
            self.parts_status.setText("")
            self.parts_status.setVisible(False)
            return
        self.parts_status.setVisible(True)
        n = mp.part_count(self.rep)
        sns = mp.serials(self.rep)
        bits = []
        for st in mp.part_status(self.char_model.rows, n):
            i = st["part"]
            name = f"P{i}" + (f" {sns[i - 1]}" if sns[i - 1] else "")
            if st["status"] == "Reject":
                bits.append(f"<span style='color:#B42318'><b>{name} ✘ REJECT</b> (char "
                            f"{', '.join(map(str, st['rejected'][:8]))}{'…' if len(st['rejected']) > 8 else ''})</span>")
            elif st["status"] == "Accept":
                bits.append(f"<span style='color:#146C2E'><b>{name} ✔ Accept</b></span>")
            else:
                bits.append(f"{name}: {st['entered']}/{st['total']}")
        self.parts_status.setText(" &nbsp;·&nbsp; ".join(bits))

    def edit_part_serials(self):
        from PySide6.QtWidgets import QDialog, QDialogButtonBox, QTableWidget, QTableWidgetItem
        n = mp.part_count(self.rep)
        dlg = QDialog(self)
        dlg.setWindowTitle("Serial numbers of the inspected parts")
        t = QTableWidget(n, 1)
        t.setHorizontalHeaderLabels(["Serial number / ID"])
        t.horizontalHeader().setStretchLastSection(True)
        for i, sn in enumerate(mp.serials(self.rep)):
            t.setVerticalHeaderItem(i, QTableWidgetItem(f"Part {i + 1}"))
            t.setItem(i, 0, QTableWidgetItem(sn))
        auto = QPushButton("Auto-number from the first…")
        auto.setToolTip("Type part 1's serial number (e.g. SN-0101), then fill the rest in sequence")

        def fill():
            first = (t.item(0, 0).text() if t.item(0, 0) else "").strip()
            m = re.search(r"(\d+)(?!.*\d)", first)
            if not m:
                QMessageBox.information(dlg, "Auto-number", "Type part 1's serial number with a number in it first.")
                return
            pre, num, post = first[:m.start()], m.group(1), first[m.end():]
            for i in range(1, n):
                t.setItem(i, 0, QTableWidgetItem(f"{pre}{int(num) + i:0{len(num)}d}{post}"))
        auto.clicked.connect(fill)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay = QVBoxLayout(dlg)
        lay.addWidget(t, 1)
        lay.addWidget(auto)
        lay.addWidget(bb)
        dlg.resize(420, min(640, 140 + 30 * n))
        if dlg.exec() != QDialog.Accepted or self.rep.get("locked"):
            return
        vals = [(t.item(i, 0).text().strip() if t.item(i, 0) else "") for i in range(n)]
        ch = {"part_serials": "\n".join(vals)}
        if vals and vals[0] and not (self.rep.get("serial_number") or "").strip():
            ch["serial_number"] = vals[0]
        new = guard(self, lambda: self.db.update_fields("reports", self.rid, ch))
        if new:
            self.rep = new
            self._fill_header()
            self._rebuild_char_cols()
            self._refresh_header_bar()
            self._mark_dirty()

    def _conflict_reload(self, table, row_id):
        r = self.db.row(table, row_id)
        model = {"characteristics": self.char_model, "components": self.comp_model, "materials": self.mat_model,
                 "attachments": self.att_model}[table]
        if r is None:
            model.remove_id(row_id)
            if table == "characteristics":
                self.viewer.remove_characteristic(row_id)
        elif table == "characteristics":
            self._put_char(r, flash=True)
        else:
            model.replace_row(self._decorate_att(r) if table == "attachments" else r, flash=True)

    def selected_char_rows(self) -> list[dict]:
        rows = sorted({i.row() for i in self.char_view.selectionModel().selectedIndexes()})
        return [self.char_model.rows[r] for r in rows if not self.char_view.isRowHidden(r)]

    def current_char(self) -> Optional[dict]:
        return self.char_model.row_at(self.char_view.currentIndex().row())

    def _select_char_id(self, cid, edit_col: Optional[str] = None):
        i = self.char_model.index_of(cid)
        if i < 0:
            return
        col = next((n for n, c in enumerate(CHAR_COLS) if c.key == edit_col), 0) if edit_col else \
            max(self.char_view.currentIndex().column(), 0)
        idx = self.char_model.index(i, col)
        self.char_view.setCurrentIndex(idx)
        self.char_view.scrollTo(idx)
        if edit_col:
            self.char_view.edit(idx)

    def _char_row_changed(self, cur: QModelIndex, _prev):
        r = self.char_model.row_at(cur.row())
        self.viewer.select_char(r["id"] if r else None, center=True)

    def _balloon_clicked(self, cid):
        self._select_char_id(cid)

    def _balloon_moved(self, cid, x, y):
        new = guard(self, lambda: self.db.update_fields("characteristics", cid, {"balloon_x": x, "balloon_y": y}))
        if new:
            self._put_char(new)
            self._after_change()

    def _add_balloon_at(self, att_id, page, x, y):
        cur = self.current_char()
        bal = {"balloon_attachment_id": att_id, "balloon_page": page, "balloon_x": x, "balloon_y": y}
        if cur and cur.get("balloon_x") is None:
            new = guard(self, lambda: self.db.update_fields("characteristics", cur["id"], bal))
            if new:
                self._put_char(new)
                nxt = next((r for r in self.char_model.rows[self.char_model.index_of(cur["id"]) + 1:]
                            if r.get("balloon_x") is None), None)
                if nxt:
                    self._select_char_id(nxt["id"])
                self._after_change()
            return
        values = dict(bal, char_no=self.db.next_char_no(self.rid),
                      ref_location=f"SH {page + 1}")
        new = guard(self, lambda: self.db.insert("characteristics", self.rid, values))
        if new:
            self._put_char(new)
            self._after_change()
            if getattr(self.cfg, "ask_requirement", True):
                from .gdt import RequirementDialog
                dlg = RequirementDialog(self, new["char_no"])
                ok = dlg.exec() == RequirementDialog.Accepted
                if dlg.remember.isChecked() != self.cfg.ask_requirement:
                    self.cfg.ask_requirement = dlg.remember.isChecked()
                    self.cfg.save()
                if ok and dlg.values()["requirement"]:
                    self._set_manual_requirement(new, dlg.values())
                    QTimer.singleShot(0, lambda: self._select_char_id(new["id"], "results"))
                    return
            QTimer.singleShot(0, lambda: self._select_char_id(new["id"], "requirement"))

    def _set_manual_requirement(self, row: dict, v: dict):
        """Requirement typed / built by hand: limits are calculated like a read callout; a composite frame's
        second row becomes its own characteristic next to the same balloon."""
        base = dict(row)
        vals = self._compute(base, "requirement", v["requirement"])
        if v.get("char_type") and v["char_type"] != "Dimension":
            vals["char_type"] = v["char_type"]
        if v.get("designator"):
            vals["designator"] = v["designator"]
        vals = {k: val for k, val in vals.items() if k in ("requirement", "nominal", "tol_plus", "tol_minus",
                                                            "lower_limit", "upper_limit", "char_type", "designator",
                                                            "disposition")}
        new = guard(self, lambda: self.db.update_fields("characteristics", row["id"], vals))
        if new:
            self._put_char(new)
        self._add_composite_rows(new or row, v.get("extra_rows") or [])
        self._after_change()

    def _add_composite_rows(self, row: dict, reqs: list[str]):
        for i, req in enumerate(reqs, start=1):
            values = {"char_no": self.db.next_char_no(self.rid), "ref_location": row.get("ref_location") or "",
                      "char_type": "GD&T", "designator": row.get("designator") or ""}
            if row.get("balloon_x") is not None:
                values.update(balloon_attachment_id=row["balloon_attachment_id"], balloon_page=row["balloon_page"],
                              balloon_x=row["balloon_x"], balloon_y=min(0.99, row["balloon_y"] + 0.025 * i),
                              balloon_size=row.get("balloon_size"))
            new = guard(self, lambda v=values: self.db.insert("characteristics", self.rid, v))
            if new:
                vals = self._compute(dict(new), "requirement", req)
                vals = {k: val for k, val in vals.items() if k in ("requirement", "nominal", "tol_plus", "tol_minus",
                                                                    "lower_limit", "upper_limit")}
                vals["char_type"] = "GD&T"
                new2 = guard(self, lambda n=new, vv=vals: self.db.update_fields("characteristics", n["id"], vv))
                self._put_char(new2 or new)
        if reqs:
            self._after_change()

    def reader_rules(self):
        """Drawing Reader Rules library, with this report's drawings offered as examples."""
        from .rules_dialog import ReaderRulesDialog
        atts = [(a, (lambda a=a: self.db.attachment_bytes(a["id"]))) for a in self.att_model.rows
                if a["kind"] in ("Drawing", "Parts List", "Notes List") and is_viewable(a["filename"])]
        ReaderRulesDialog(self.db, self, atts).exec()
        from .. import rules as _rules
        _rules.load_shared(self.db)

    def gdt_for_current(self):
        """⌖ GD&T… on Form 3: build a feature control frame for the selected characteristic."""
        from .gdt import FcfBuilderDialog
        cur = self.current_char()
        if cur is None:
            QMessageBox.information(self, "GD&T", "Select a characteristic row first (or add a balloon).")
            return
        if self.rep["locked"]:
            return
        dlg = FcfBuilderDialog(self, cur.get("requirement") or "")
        if dlg.exec() != FcfBuilderDialog.Accepted:
            return
        reqs = dlg.requirements()
        if reqs:
            self._set_manual_requirement(cur, {"requirement": reqs[0], "char_type": "GD&T",
                                               "designator": cur.get("designator") or "", "extra_rows": reqs[1:]})

    def _balloon_menu(self, cid, pos):
        m = QMenu(self)
        a1 = m.addAction("Remove balloon (keep characteristic)")
        a2 = m.addAction("Delete characteristic")
        act = m.exec(pos)
        if act == a1:
            new = guard(self, lambda: self.db.update_fields("characteristics", cid, {
                "balloon_attachment_id": None, "balloon_page": None, "balloon_x": None, "balloon_y": None}))
            if new:
                self._put_char(new)
                self._after_change()
        elif act == a2:
            self._select_char_id(cid)
            self.delete_chars()

    def _char_menu(self, pos):
        m = QMenu(self)
        self.char_view.edit_actions(m)
        m.addAction("Accept", lambda: self.bulk_set("disposition", "Accept"))
        m.addAction("Reject", lambda: self.bulk_set("disposition", "Reject"))
        m.addAction("N/A", lambda: self.bulk_set("disposition", "N/A"))
        m.addSeparator()
        m.addAction("Set designator: Key", lambda: self.bulk_set("designator", "Key"))
        m.addAction("Set designator: Critical", lambda: self.bulk_set("designator", "Critical"))
        m.addAction("Clear designator", lambda: self.bulk_set("designator", ""))
        m.addSeparator()
        m.addAction("Set tooling…", self.bulk_tooling)
        m.addAction("Set reference location…", lambda: self.bulk_prompt("ref_location", "Reference location (sheet/zone)"))
        m.addAction("Remove balloon(s)", self.remove_balloons)
        m.addSeparator()
        m.addAction("Delete", self.delete_chars)
        m.exec(self.char_view.viewport().mapToGlobal(pos))

    def _apply_char_filter(self):
        mode = self.char_filter.currentIndex()
        q = self.char_search.text().strip().lower()
        for i, r in enumerate(self.char_model.rows):
            show = True
            if mode == 1:
                show = not r["disposition"]
            elif mode == 2:
                show = r["disposition"] == "Reject"
            elif mode == 3:
                show = r.get("balloon_x") is None
            elif mode == 4:
                show = r["designator"] in ("Key", "Critical")
            if show and q:
                show = q in " ".join(str(r.get(k) or "") for k in ("char_no", "requirement", "results", "comments",
                                                                    "ref_location", "tooling")).lower()
            self.char_view.setRowHidden(i, not show)

    # ---------------------------------------------------------------- characteristic actions
    def add_char(self):
        n = self.db.next_char_no(self.rid)
        new = guard(self, lambda: self.db.insert("characteristics", self.rid, {"char_no": n}))
        if new:
            self._put_char(new)
            self._after_change()
            QTimer.singleShot(0, lambda: self._select_char_id(new["id"], "requirement"))

    def delete_chars(self):
        rows = self.selected_char_rows()
        if not rows:
            return
        if QMessageBox.question(self, "Delete", f"Delete {len(rows)} characteristic(s)?") != QMessageBox.Yes:
            return
        for r in rows:
            guard(self, lambda r=r: self.db.delete("characteristics", r["id"]))
            if self.db.row("characteristics", r["id"]) is not None:
                break          # locked / failed
            self.char_model.remove_id(r["id"])
            self.viewer.remove_characteristic(r["id"])
        self._after_change()

    def renumber(self, order: Optional[list[int]] = None):
        ids = order or [r["id"] for r in self.char_model.rows]
        guard(self, lambda: self.db.renumber_characteristics(self.rid, ids))
        self.reload_chars()

    def renumber_by_position(self):
        rows = self.char_model.rows
        docs = {d["id"]: i for i, d in enumerate(self.viewer.docs)}
        balloons = [r for r in rows if r.get("balloon_x") is not None]
        others = [r for r in rows if r.get("balloon_x") is None]
        balloons.sort(key=lambda r: (docs.get(r["balloon_attachment_id"], 99), r["balloon_page"] or 0,
                                     round(r["balloon_y"] / 0.05), r["balloon_x"]))
        if QMessageBox.question(self, "Renumber", "Renumber all balloons top-to-bottom, left-to-right on each sheet "
                                "(un-ballooned rows go last)?") == QMessageBox.Yes:
            self.renumber([r["id"] for r in balloons + others])

    def move_char(self, delta):
        cur = self.current_char()
        if not cur:
            return
        rows = list(self.char_model.rows)
        i = self.char_model.index_of(cur["id"])
        j = i + delta
        if not (0 <= j < len(rows)):
            return
        rows[i], rows[j] = rows[j], rows[i]
        self.renumber([r["id"] for r in rows])
        self._select_char_id(cur["id"])

    def bulk_set(self, key, value):
        for r in self.selected_char_rows():
            changes = {key: value}
            new = guard(self, lambda r=r: self.db.update_fields("characteristics", r["id"], changes))
            if new:
                self._put_char(new)
        self._after_change()

    def bulk_prompt(self, key, label):
        rows = self.selected_char_rows()
        if not rows:
            return
        text, ok = QInputDialog.getText(self, label, f"{label} for {len(rows)} characteristic(s):",
                                        text=rows[0].get(key) or "")
        if ok:
            self.bulk_set(key, text.strip())

    def bulk_tooling(self):
        rows = self.selected_char_rows()
        if not rows:
            QMessageBox.information(self, "Set tooling", "Select one or more characteristics first.")
            return
        from PySide6.QtWidgets import QDialog, QDialogButtonBox
        from .table_model import GageCombo
        d = QDialog(self)
        d.setWindowTitle("Set tooling / gage")
        cb = GageCombo(self._gage_list(), d)
        cb.set_value(rows[0].get("tooling") or "")
        cb.setMinimumWidth(460)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(d.accept)
        bb.rejected.connect(d.reject)
        lay = QVBoxLayout(d)
        lay.addWidget(QLabel(f"Gage for {len(rows)} characteristic(s) — pick from the Gage Library or start typing:"))
        lay.addWidget(cb)
        lay.addWidget(bb)
        if d.exec() == QDialog.Accepted:
            self.bulk_set("tooling", cb.value())

    def set_status_msg(self, msg, ms=8000):
        sb = getattr(self.window(), "statusBar", None)
        if callable(sb):
            sb().showMessage(msg, ms)

    def _delete_char_ids(self, ids) -> int:
        def work():
            n = 0
            for i in ids:
                if self.db.row("characteristics", i) is not None:
                    self.db.delete("characteristics", i)
                    n += 1
            return n
        n = guard(self, work) or 0
        self.reload_chars()
        self._after_change()
        return n

    def undo_last_balloons(self):
        """Remove the characteristics added by the last 'Add Balloons' (this session) and offer to redo it."""
        if self.rep["locked"]:
            QMessageBox.information(self, "Locked", "This report is locked.")
            return
        ids = [i for i in getattr(self, "_last_batch", []) if any(r["id"] == i for r in self.char_model.rows)]
        if not ids:
            QMessageBox.information(self, "Undo Last Ballooning",
                                    "Nothing to undo from this session. To redo everything use 🔄 Start Over, or "
                                    "select the rows you don't want and 🗑 Delete them.")
            return
        rows = [r for r in self.char_model.rows if r["id"] in ids]
        nos = sorted(r["char_no"] for r in rows)
        with_results = sum(1 for r in rows if (r.get("results") or "").strip())
        msg = (f"Remove the {len(ids)} characteristic(s) added by the last ballooning "
               f"(char {nos[0]}–{nos[-1]})?")
        if with_results:
            msg += f"\n\n{with_results} of them already have results entered - those results will be lost."
        if QMessageBox.question(self, "Undo Last Ballooning", msg) != QMessageBox.Yes:
            return
        n = self._delete_char_ids(ids)
        self._last_batch = []
        if QMessageBox.question(self, "Undo Last Ballooning", f"Removed {n} characteristic(s).\n\n"
                                "Run 🎯 Balloon Part again now?") == QMessageBox.Yes:
            self.balloon_part()

    def start_over(self):
        """Delete every characteristic / balloon on this report and run Balloon Part from the beginning."""
        if self.rep["locked"]:
            QMessageBox.information(self, "Locked", "This report is locked.")
            return
        rows = list(self.char_model.rows)
        if rows:
            with_results = sum(1 for r in rows if (r.get("results") or "").strip())
            msg = (f"Delete ALL {len(rows)} characteristics and balloons on this report and start the ballooning "
                   "over?\n\nHeader, parts list, materials, attachments and the CoC are kept. The deletions are "
                   "recorded in History.")
            if with_results:
                msg += f"\n\n⚠ {with_results} characteristic(s) already have results entered - they will be lost."
            mb = QMessageBox(QMessageBox.Warning, "Start Over", msg, parent=self)
            go = mb.addButton("Delete all && re-balloon", QMessageBox.DestructiveRole)
            mb.addButton(QMessageBox.Cancel)
            mb.exec()
            if mb.clickedButton() is not go:
                return
            self._delete_char_ids([r["id"] for r in rows])
            self._last_batch = []
        self.balloon_part()

    def remove_balloons(self):
        for r in self.selected_char_rows():
            new = guard(self, lambda r=r: self.db.update_fields("characteristics", r["id"], {
                "balloon_attachment_id": None, "balloon_page": None, "balloon_x": None, "balloon_y": None}))
            if new:
                self._put_char(new)
        self._after_change()

    # ---------------------------------------------------------------- drawing intelligence
    def _drawing_atts(self):
        return [a for a in self.att_model.rows if a["kind"] == "Drawing" and is_viewable(a["filename"])]

    def ocr_setup(self, att_id=-1, page=0):
        from .ocr_prep_dialog import OcrPrepDialog
        att = next((a for a in self.att_model.rows if a["id"] == att_id), None)
        data = guard(self, lambda: self.db.attachment_bytes(att_id)) if att else None
        OcrPrepDialog(self.db, self, data, att["filename"] if att else "", page).exec()

    def _analyze(self, atts, with_options=True):
        from ..drawing_intel import analyze_cached
        from .ocr_prep_dialog import load_shared
        from .workers import run_with_progress
        load_shared(self.db)                     # pick up OCR preprocessing changed on any workstation
        from .. import rules as _rules
        _rules.load_shared(self.db)              # and the Drawing Reader Rules library
        loads = [(a, self.db.attachment_bytes(a["id"])) for a in atts]

        def work(cb):
            out = []
            for i, (a, data) in enumerate(loads):
                def sub(msg, frac, i=i, a=a):
                    cb(f"{a['filename']}\n{msg}", (i + 0.8 * frac) / len(loads))
                info = analyze_cached(data, a["filename"], True, sub)
                if with_options:
                    cb(f"{a['filename']}\nFinding views and part numbers…", (i + 0.9) / len(loads))
                    info.part_options()
                out.append((a, info))
            return out
        return run_with_progress(self, "Reading drawing…", work)

    @staticmethod
    def _merge_options(analyzed):
        merged: dict[str, dict] = {}
        for a, info in analyzed:
            for o in info.part_options():
                key = o.part_number.upper()
                m = merged.setdefault(key, dict(pn=o.part_number, name=o.name, kind=o.kind, sheets=[], views=0,
                                                note=o.note, items=[]))
                m["name"] = m["name"] or o.name
                if o.kind in ("Assembly", "Tabulated") and m["kind"] != "Drawing":
                    m["kind"] = o.kind
                m["items"].append((a, info, o))
                if o.kind != "Drawing":
                    m["views"] += len(o.views)
                m["sheets"] = sorted(set(m["sheets"]) | set(o.sheets))
        order = {"Detail": 0, "Tabulated": 0, "Assembly": 1, "Drawing": 2}
        return sorted(merged.values(), key=lambda m: (order.get(m["kind"], 1), m["pn"]))

    def balloon_part(self, preselect: Optional[str] = None):
        import dataclasses
        from .dialogs import PartPickerDialog
        from .workers import run_with_progress
        if self.rep["locked"]:
            QMessageBox.information(self, "Locked", "This report is locked.")
            return
        atts = self._drawing_atts()
        if not atts:
            QMessageBox.information(self, "Balloon part", "Upload the drawing first (Attachments → Add Drawing, or drop "
                                    "the PDF / TIFF / drawing-package ZIP onto this window).")
            return
        analyzed = guard(self, lambda: self._analyze(atts))
        if not analyzed:
            return
        merged = self._merge_options(analyzed)
        infos = [i for _, i in analyzed]
        if all(i.drawing_sheets and len(i.no_text) == len(i.drawing_sheets) for i in infos):
            QMessageBox.information(self, "Scanned drawing", "This drawing has no text layer and no OCR engine is "
                                    "installed, so callouts can't be read automatically. Install the OCR add-on "
                                    "(pip install rapidocr_onnxruntime) or balloon manually.")
        dlg = PartPickerDialog(merged, infos, preselect if preselect is not None else self.rep["part_number"], self)
        if dlg.exec() != PartPickerDialog.Accepted:
            return
        m = dlg.result_part()
        if not m:
            return
        self._pre_ids = {r["id"] for r in self.char_model.rows}

        def opt_for(info, o):
            if o.kind != "Drawing" and not o.views:
                return dataclasses.replace(o, kind="Drawing")      # no own views found: whole drawing (+ tab values)
            if m.get("manual"):
                d = re.search(r"-(\d{1,4}[A-Z]?)$", m["pn"])
                return dataclasses.replace(o, dash=d.group(1) if d else "")
            return o

        cust_note = []

        def work(cb):
            out, regions = [], {}
            for k, (a, info, o) in enumerate(m["items"]):
                cb(f"Ballooning {m['pn']} on {a['filename']}…", k / max(1, len(m["items"])))
                opt = opt_for(info, o)
                if info.preballooned:
                    # the customer already numbered every characteristic on the print (SpaceX ◇n): use theirs
                    got, extra, missing = info.balloon_customer()
                    out += [(a, c) for c in got] + [(a, c) for c in self._useful_extras(extra)]
                    cust_note.append((a["filename"], len(got), missing))
                    continue
                out += [(a, c) for c in info.balloon(opt)]
                if opt.kind != "Drawing":
                    for pg, rects in info.regions(opt).items():
                        regions[(a["id"], pg)] = rects
            return out, regions
        pairs, regions = guard(self, lambda: run_with_progress(self, "Finding callouts…", work)) or ([], {})
        a0, info0, o0 = m["items"][0]
        opt0 = opt_for(info0, o0)
        # ---- report header
        if dlg.opt_header.isChecked():
            ch = {"part_number": m["pn"], "part_name": m["name"] or info0.title,
                  "drawing_number": info0.drawing_no or self.rep["drawing_number"],
                  "detail_or_assembly": "Assembly" if m["kind"] == "Assembly" else "Detail"}
            if info0.rev:
                ch["drawing_rev"] = info0.rev
                if not self.rep["part_rev"]:
                    ch["part_rev"] = info0.rev
            ch.update(info0.tolerances)
            if not getattr(info0, "inch", True):
                ch["units"] = "mm"
            eos = [a["filename"] for a in self.att_model.rows if a["kind"] == "Engineering Order"]
            if eos and not self.rep["additional_changes"]:
                def eo_name(fn):
                    mm = re.search(r"(A?EO|ECN|DCN)[_ ]*(\d+)?", fn, re.I)
                    return (f"{mm.group(1).upper()} {mm.group(2) or ''}".strip()) if mm else re.sub(r"\.pdf$", "", fn, flags=re.I)
                ch["additional_changes"] = ", ".join(eo_name(e) for e in eos)[:250]
            ch = {k: v for k, v in ch.items() if v is not None and str(self.rep.get(k) or "") != str(v)}
            if ch:
                new = guard(self, lambda: self.db.update_fields("reports", self.rid, ch))
                if new:
                    self.rep = new
                    self._fill_header(force=True)
        if dlg.opt_clear.isChecked() and self.char_model.rows:
            if QMessageBox.question(self, "Remove existing", f"Delete the {len(self.char_model.rows)} existing "
                                    "characteristic(s) first?") == QMessageBox.Yes:
                for r in list(self.char_model.rows):
                    guard(self, lambda r=r: self.db.delete("characteristics", r["id"]), quiet=True)
                self.reload_chars()
        self.viewer.set_regions(regions)
        # ---- characteristics
        if pairs:
            src = "ocr" if any(c.source == "ocr" for _, c in pairs) else "text"
            if self._review_and_insert(pairs, src, f"<b>{m['pn']}</b> — " + self._cust_msg(cust_note)) == "restart":
                QTimer.singleShot(0, lambda: self.balloon_part(m["pn"]))
                return
        else:
            QMessageBox.information(self, "Auto-Balloon", f"No dimension callouts were found for {m['pn']}.\n\n"
                                    "Use ⬚ Balloon Area to box the views you need, or ➕ Add Balloon.")
        # ---- notes
        if dlg.opt_notes.isChecked():
            notes = [n for _, info, o in m["items"] for n in info.applicable_notes(opt_for(info, o))]
            seen, uniq = set(), []
            for n in notes:
                if (n["label"], n["text"][:40]) not in seen:
                    seen.add((n["label"], n["text"][:40]))
                    uniq.append(n)
            if not uniq:
                nl = [a for a in self.att_model.rows if a["kind"] == "Notes List" and is_viewable(a["filename"])]
                if nl:
                    got = guard(self, lambda: self._analyze(nl[:1], with_options=False), quiet=True) or []
                    if got:
                        ninfo = got[0][1]
                        for sh in ninfo.sheets:
                            sh.kind = "notes list"
                        from ..drawing_intel import _coded_notes
                        ninfo.notes = ninfo.notes or _coded_notes(ninfo)
                        uniq = ninfo.applicable_notes(dataclasses.replace(opt0, views=[]))
                    if uniq:
                        self._notes_dialog(uniq, upload=None)
                    else:
                        self.import_notes(from_bytes=(nl[0]["filename"], self.db.attachment_bytes(nl[0]["id"])))
            else:
                self._notes_dialog(uniq, upload=None)
        # ---- Form 2 material
        if dlg.opt_form2.isChecked() and opt0.entry and (opt0.entry.material or opt0.entry.size):
            e = opt0.entry
            have = {(x["name"] or "").upper() for x in self.mat_model.rows}
            if e.material.upper() not in have:
                spec = ", ".join(find_specs(e.material)) or e.material
                guard(self, lambda: self.db.insert("materials", self.rid, {
                    "seq": max([x["seq"] for x in self.mat_model.rows] + [0]) + 1, "category": "Material",
                    "name": e.material, "spec_number": spec, "comments": f"Size: {e.size}" if e.size else ""}))
        # ---- Form 1 index for assemblies
        separate = [a for a in self.att_model.rows if a["kind"] in ("Parts List", "Application List")
                    and a["filename"].lower().endswith(".pdf")]
        if dlg.opt_form1.isChecked() and separate and not self.comp_model.rows:
            self.index_from_bom(quiet=True)          # the BOM / separate parts list is the authority
        elif dlg.opt_form1.isChecked() and m["kind"] == "Assembly":
            kids = info0.children(opt0)
            have = {x["part_number"].upper() for x in self.comp_model.rows}
            rows = [dict(k, seq=i + 1) for i, k in enumerate(kids) if k["part_number"].upper() not in have]
            if rows:
                guard(self, lambda: self.db.insert_many("components", self.rid, rows))
        before = getattr(self, "_pre_ids", set())
        self.reload_all()
        self.viewer.set_regions(regions)
        self.tabs.setCurrentIndex(2)
        # jump the drawing to the part's first balloon
        first = next((r for r in self.char_model.rows if r.get("balloon_x") is not None and r["id"] not in before), None) \
            or next((r for r in self.char_model.rows if r.get("balloon_x") is not None), None)
        if first:
            QTimer.singleShot(50, lambda: self._select_char_id(first["id"]))

    # ---------------------------------------------------------------- OCR learning (ocr_learn.py)
    def _apply_ocr_memory(self, cands) -> int:
        """Apply the fixes the team has taught the reader to OCR-read candidates. Returns how many changed."""
        try:
            mem = self.db.ocr_memory()
        except Exception:
            return 0
        n = 0
        for c in cands:
            if getattr(c, "source", "") != "ocr":
                continue
            if not hasattr(c, "_ocr_read"):
                c._ocr_read = c.requirement
                c._orig_comment = getattr(c, "comment", "") or ""
            fixed, ids = mem.apply(c._ocr_read) if len(mem) else (c._ocr_read, [])
            c._fix_ids = ids
            c._fixed = fixed if ids else ""
            c.requirement = fixed if ids else c._ocr_read
            base = c._orig_comment or "OCR – verify"
            c.comment = f"{base} · {ol.fix_note(c._ocr_read)}" if ids else c._orig_comment
            n += bool(ids)
        return n

    def _learn_from_review(self, cands, added):
        """After Add Balloons: learn from requirements corrected in the review list, and count whether the learned
        fixes applied there were kept."""
        for c, row in zip(cands, added):
            if getattr(c, "source", "") != "ocr" or not getattr(c, "_ocr_read", ""):
                continue
            final = c.requirement.strip()
            ids = getattr(c, "_fix_ids", []) or []
            if ids:
                guard(self, lambda ids=ids, final=final: self.db.ocr_fix_feedback(ids, final), quiet=True)
            if ol.norm(final) != ol.norm(c._ocr_read) and not (ids and ol.norm(final) == ol.norm(c._fixed)):
                guard(self, lambda c=c, row=row, final=final: self.db.learn_ocr_correction(
                    c._ocr_read, final, f"{self.rid}:{row['id']}"), quiet=True)
            if ids and ol.norm(final) == ol.norm(c._fixed):
                # kept as fixed: remember which fixes were used, so a later correction in Form 3 counts against them
                guard(self, lambda row=row, ids=ids: self.db.update_fields(
                    "characteristics", row["id"], {"ocr_fix_ids": ",".join(map(str, ids))}), quiet=True)

    def _learn_from_edit(self, new: dict) -> Optional[dict]:
        """A requirement read by OCR was corrected in the Form 3 table."""
        ids = [int(x) for x in (new.get("ocr_fix_ids") or "").split(",") if x.strip().isdigit()]
        if ids:
            guard(self, lambda: self.db.ocr_fix_feedback(ids, new.get("requirement") or ""), quiet=True)
        read = new.get("ocr_read") or ""
        if read:
            guard(self, lambda: self.db.learn_ocr_correction(read, new.get("requirement") or "",
                                                             f"{self.rid}:{new['id']}"), quiet=True)
        if ids:
            return guard(self, lambda: self.db.update_fields("characteristics", new["id"], {"ocr_fix_ids": ""}),
                         quiet=True)
        return None

    def _review_and_insert(self, pairs, source, title_extra=""):
        """pairs: [(attachment row, Candidate)] -> review dialog -> characteristics with balloons."""
        existing = [(c["balloon_attachment_id"], c["balloon_page"] or 0, c["balloon_x"], c["balloon_y"])
                    for c in self.char_model.rows if c.get("balloon_x") is not None]
        pairs = [(a, c) for a, c in pairs if not any(aid == a["id"] and pg == c.page and abs(c.x - x) < 0.008
                                                     and abs(c.y - y) < 0.008 for aid, pg, x, y in existing)]
        if not pairs:
            QMessageBox.information(self, "Auto-Balloon", "Everything found here is already ballooned.")
            return
        n0 = self.db.next_char_no(self.rid)
        cust_max = max([getattr(c, "char_no", None) or 0 for _, c in pairs] + [0])
        if cust_max:
            n0 = max(n0, cust_max + 1)            # our own numbers continue after the customer's
        cands = [c for _, c in pairs]
        att_of = {id(c): a for a, c in pairs}
        n_learned = self._apply_ocr_memory(cands)
        if n_learned:
            title_extra += (f"<span style='color:#146C2E'><b>{n_learned}</b> OCR read(s) corrected from what the "
                            "team has taught the reader (marked 'learned OCR fix' in Note) — still verify.</span> ")
        from .thumbs import CalloutThumbs
        renderers: dict = {}

        def thumb(c, large=False):
            a = att_of[id(c)]
            r = renderers.get(a["id"])
            if r is None:
                r = renderers[a["id"]] = CalloutThumbs(self.db.attachment_bytes(a["id"]), a.get("filename", ""))
            return r.pixmap(c.page, getattr(c, "nbbox", ()), getattr(c, "rot", 0), large)

        dlg = CandidateDialog(cands, source, n0, self, title_extra, thumb_fn=thumb)
        res = dlg.exec()
        for r in renderers.values():
            r.close()
        if res == CandidateDialog.RESTART:
            return "restart"
        if res != CandidateDialog.Accepted:
            return
        rows, lessons = [], []
        have_nos = {r["char_no"] for r in self.char_model.rows}
        nxt = n0
        for i, c in enumerate(dlg.selected()):
            a = att_of[id(c)]
            cno = getattr(c, "char_no", None)
            if not cno or cno in have_nos:
                while nxt in have_nos:
                    nxt += 1
                cno, nxt = nxt, nxt + 1
            have_nos.add(cno)
            base = {"char_no": cno, "requirement": "", "results": "", "disposition": "",
                    "char_type": c.char_type if c.char_type in CHAR_TYPES else "Dimension",
                    "nominal": "", "tol_plus": "", "tol_minus": "", "lower_limit": "", "upper_limit": ""}
            vals = self._compute(base, "requirement", c.requirement)
            if c.char_type in ("Thread", "GD&T", "Basic", "Reference", "Angle") and vals.get("char_type") in (None, "Dimension", "Note"):
                vals["char_type"] = c.char_type
            vals.update(char_no=cno, ref_location=c.ref_location or f"SH {c.page + 1}",
                        balloon_size=getattr(c, "bsize", 0.0) or None,
                        designator=c.designator or "", balloon_attachment_id=a["id"], balloon_page=c.page,
                        balloon_x=c.x, balloon_y=c.y)
            note = c.comment or ("OCR – verify" if c.source == "ocr" else "")
            if note:
                vals["comments"] = note
            if c.source == "ocr" and getattr(c, "_ocr_read", ""):
                vals["ocr_read"] = c._ocr_read            # what OCR actually read, for learning from corrections
            rows.append(vals)
            lessons.append(c)
        if rows:
            added = guard(self, lambda: self.db.insert_many("characteristics", self.rid, rows))
            if added:
                self._last_batch = [r["id"] for r in added]      # for ↶ Undo Last Ballooning
                self._learn_from_review(lessons, added)
            self.reload_chars()

    @staticmethod
    def _useful_extras(extra):
        """Callouts the customer did not balloon, worth showing (unchecked) as a cross-check."""
        return [c for c in extra if c.char_type != "Reference" and not re.fullmatch(r"\d{1,3}", c.requirement.strip())
                and not re.fullmatch(r"\d+\s+[A-Z ]+", c.requirement.strip())]

    @staticmethod
    def _cust_msg(notes):
        if not notes:
            return ""
        parts = []
        for fn, n, missing in notes:
            t = f"Customer-ballooned drawing: <b>{n}</b> characteristics keep the drawing's own balloon numbers"
            if missing:
                t += (f". Numbers not on the drawing (usually deleted by a revision): "
                      f"<b>{', '.join(map(str, missing))}</b>")
            parts.append(t + ". ")
        return "".join(parts) + "Callouts the customer did not balloon are listed at the end, unchecked. "

    def auto_balloon(self, att_id, page):
        """Whole sheet."""
        att = next((a for a in self.att_model.rows if a["id"] == att_id), None)
        if not att:
            return
        analyzed = guard(self, lambda: self._analyze([att], with_options=False))
        if not analyzed:
            return
        info = analyzed[0][1]
        extra_msg = ""
        if info.preballooned:
            got, extra, _ = guard(self, lambda: info.balloon_customer(pages=[page])) or ([], [], [])
            cands = got + self._useful_extras(extra)
            extra_msg = self._cust_msg([(att["filename"], len(got), [])])
        else:
            cands = guard(self, lambda: info.balloon_sheet(page)) or []
        if not cands:
            from .. import ocr
            msg = "No dimension callouts were found on this sheet."
            if info.no_text and not ocr.available():
                msg += "\n\nThis is a scanned sheet and no OCR engine is installed."
            QMessageBox.information(self, "Auto-Balloon", msg)
            return
        if self._review_and_insert([(att, c) for c in cands], "ocr" if info.ocr_used else "text", extra_msg) == "restart":
            QTimer.singleShot(0, lambda: self.auto_balloon(att_id, page))

    def balloon_area(self, att_id, page, rect):
        att = next((a for a in self.att_model.rows if a["id"] == att_id), None)
        if not att:
            return
        analyzed = guard(self, lambda: self._analyze([att], with_options=False))
        if not analyzed:
            return
        info = analyzed[0][1]
        if info.preballooned:
            s = info.sheets[page]
            prect = (rect[0] * s.W, rect[1] * s.H, rect[2] * s.W, rect[3] * s.H)
            got, _, _ = guard(self, lambda: info.balloon_customer(pages=[page], rect=prect)) or ([], [], [])
            cands = got
        else:
            cands = guard(self, lambda: info.balloon(None, page=page, rect=rect)) or []
        if not cands:
            QMessageBox.information(self, "Balloon Area", "No callouts were found inside that box.")
            return
        if self._review_and_insert([(att, c) for c in cands], "ocr" if info.ocr_used else "text",
                                   "Selected area — ") == "restart":
            self.viewer.set_mode("area")
            self.set_status_msg("Drag a new box around the view to balloon it again.")

    # ======================================================================= attachments
    def _build_attachments(self):
        w = QWidget()
        lay = QHBoxLayout(w)
        left = QVBoxLayout()
        tb = FlowRow()
        btns = [("📐 Add Drawing(s)…", lambda: self.upload_dialog("Drawing")),
                ("📷 Add Photo(s)…", lambda: self.upload_dialog("Photo")),
                ("📋 Add Parts List…", self.import_parts_list),
                ("📝 Add Notes List…", self.import_notes),
                ("📎 Add Other File(s)…", lambda: self.upload_dialog("Other")),
                ("↗ Open", self.open_attachment), ("💾 Save As…", self.save_attachment),
                ("🗑 Delete", self.delete_attachment)]
        for i, (t, fn) in enumerate(btns):
            b = QPushButton(t)
            b.clicked.connect(fn)
            tb.addWidget(b)
        left.addWidget(tb)
        self.att_model = RecordModel(ATT_COLS, self._att_edit)
        self.att_view = self._make_table(self.att_model, ATT_COLS)
        self.att_view.selectionModel().currentRowChanged.connect(self._att_preview)
        self.att_view.doubleClicked.connect(lambda _: self.open_attachment())
        left.addWidget(self.att_view, 1)
        hint = QLabel("Drag and drop files anywhere on this report: PDF → Drawing, images → Photo, "
                      "XLSX/CSV → Parts List, TXT/DOCX → Notes List. Files are stored in the database and "
                      "available on every workstation.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#4a4f52;")
        left.addWidget(hint)
        lay.addLayout(left, 3)
        self.att_prev = QLabel("Select a file to preview")
        self.att_prev.setAlignment(Qt.AlignCenter)
        self.att_prev.setMinimumWidth(320)
        self.att_prev.setStyleSheet("background:#5A5F63; color:white; border-radius:4px;")
        lay.addWidget(self.att_prev, 2)
        return w

    def _decorate_att(self, r):
        r = dict(r)
        r["_size"] = human_size(r.get("size_bytes") or 0)
        return r

    def _att_edit(self, row, key, value, expected=None):
        exp = expected if expected is not None else row.get(key)
        new = guard(self, lambda: self.db.update_field("attachments", row["id"], key, value, expected_old=exp),
                    on_conflict=lambda e: self._conflict_reload("attachments", row["id"]))
        if new:
            new = self._decorate_att(new)
            self._after_change()
            QTimer.singleShot(0, self._refresh_viewer_docs)
        return new

    def _att_preview(self, cur, _prev):
        a = self.att_model.row_at(cur.row())
        if not a:
            return
        if not is_viewable(a["filename"]):
            self.att_prev.setText(f"{a['filename']}\n(no preview — use Open)")
            return
        try:
            png = render_page(self.db.attachment_bytes(a["id"]), a["filename"], 0, dpi=60, key=a["sha256"] + "t")
            pm = QPixmap.fromImage(QImage.fromData(png))
            self.att_prev.setPixmap(pm.scaled(self.att_prev.width() - 10, self.att_prev.height() - 10,
                                              Qt.KeepAspectRatio, Qt.SmoothTransformation))
        except Exception as e:  # noqa: BLE001
            self.att_prev.setText(f"Preview failed: {e}")

    def selected_attachment(self):
        return self.att_model.row_at(self.att_view.currentIndex().row())

    def upload_dialog(self, kind):
        filt = {"Drawing": DRAWING_FILTER, "Photo": PHOTO_FILTER}.get(kind, "All files (*)")
        paths, _ = QFileDialog.getOpenFileNames(self, f"Add {kind}", self.cfg.last_export_dir or "", filt)
        zips = [p for p in paths if p.lower().endswith(".zip")]
        for z in zips:
            self.upload_package(z)
        rest = [p for p in paths if p not in zips]
        if rest:
            self.upload_files(rest, kind)

    def upload_files(self, paths, kind) -> list[dict]:
        out = []
        prog = QProgressDialog("Uploading…", None, 0, len(paths), self)
        prog.setWindowModality(Qt.WindowModal)
        prog.setMinimumDuration(400)
        for i, p in enumerate(paths):
            prog.setValue(i)
            prog.setLabelText(f"Uploading {Path(p).name}…")
            r = guard(self, lambda p=p: self.db.add_attachment(self.rid, p, kind))
            if r:
                out.append(r)
                self.att_model.replace_row(self._decorate_att(r))
        prog.setValue(len(paths))
        self._refresh_viewer_docs()
        self._after_change()
        if kind == "Drawing" and out:
            self.tabs.setCurrentIndex(2)
        return out

    def open_attachment(self):
        a = self.selected_attachment()
        if not a:
            return
        def work():
            d = Path(tempfile.mkdtemp(prefix="fai_open_"))       # unique: avoids files locked by Excel/Acrobat
            p = d / a["filename"]
            p.write_bytes(self.db.attachment_bytes(a["id"]))
            return p
        p = guard(self, work)
        if p:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    def save_attachment(self):
        a = self.selected_attachment()
        if not a:
            return
        p, _ = QFileDialog.getSaveFileName(self, "Save attachment", str(Path(self.cfg.last_export_dir or "") /
                                                                         a["filename"]))
        if p:
            guard(self, lambda: Path(p).write_bytes(self.db.attachment_bytes(a["id"])))

    def delete_attachment(self):
        a = self.selected_attachment()
        if not a:
            return
        n = sum(1 for c in self.char_model.rows if c.get("balloon_attachment_id") == a["id"])
        msg = f"Delete {a['filename']}?"
        if n:
            msg += f"\n\n{n} balloon(s) are placed on this file; the characteristics are kept but lose their balloons."
        if QMessageBox.question(self, "Delete attachment", msg) == QMessageBox.Yes:
            guard(self, lambda: self.db.delete("attachments", a["id"]))
            self.reload_attachments()
            self.reload_chars()

    def _refresh_viewer_docs(self):
        docs = [a for a in self.att_model.rows if a["kind"] in ("Drawing", "Photo") and is_viewable(a["filename"])]
        docs.sort(key=lambda a: (0 if a["kind"] == "Drawing" else 1, a["seq"], a["id"]))
        self.viewer.set_documents(docs)

    # ---------------------------------------------------------------- drag & drop
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if self.rep["locked"]:
            QMessageBox.information(self, "Locked", "This report is locked.")
            return
        for p in paths:
            ext = Path(p).suffix.lower()
            if ext == ".zip":
                self.upload_package(p)
            elif ext == ".pdf":
                self.upload_files([p], "Drawing")
            elif is_image(p):
                self.upload_files([p], "Photo")
            elif ext in (".xlsx", ".xlsm", ".csv", ".tsv"):
                self.import_parts_list(p)
            elif ext in (".txt", ".docx"):
                self.import_notes(p)
            else:
                self.upload_files([p], "Other")

    # ---------------------------------------------------------------- imports
    def import_parts_list(self, path: Optional[str] = None, target: Optional[str] = None):
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, "Import parts list", "", TABLE_FILTER)
            if not path:
                return
        rows = guard(self, lambda: read_table(path))
        if not rows:
            QMessageBox.information(self, "Import", "No table rows were found in that file.")
            return
        t1, t2 = "Form 1 – Index of parts / sub-assemblies", "Form 2 – Materials & special processes"
        targets = {t1: COMPONENT_FIELDS, t2: MATERIAL_FIELDS} if target != "form2" else {t2: MATERIAL_FIELDS, t1: COMPONENT_FIELDS}
        dlg = MappingDialog(f"Import parts list — {Path(path).name}", rows, targets, self)
        if dlg.exec() != MappingDialog.Accepted:
            return
        recs = dlg.records()
        table = "components" if dlg.target() == t1 else "materials"
        model = self.comp_model if table == "components" else self.mat_model
        start = max([r["seq"] for r in model.rows] + [0]) + 1
        for i, r in enumerate(recs):
            r["seq"] = start + i
            if table == "materials":
                r.setdefault("category", classify_spec(" ".join(str(v) for v in r.values())) if r else "Material")
                if r["category"] == "Other":
                    r["category"] = "Material"
        guard(self, lambda: self.db.insert_many(table, self.rid, recs))
        if not any(a["filename"] == Path(path).name and a["kind"] == "Parts List" for a in self.att_model.rows):
            guard(self, lambda: self.db.add_attachment(self.rid, path, "Parts List"), quiet=True)
        self.reload_all()
        self.tabs.setCurrentIndex(0 if table == "components" else 1)

    def import_parts_list_to_form2(self):
        self.import_parts_list(target="form2")

    def import_notes(self, path: Optional[str] = None, from_bytes: Optional[tuple] = None):
        """path: notes file.  from_bytes: (filename, data) — e.g. the drawing PDF."""
        if from_bytes:
            fname, data = from_bytes
            tmp = Path(tempfile.gettempdir()) / f"fai_notes_{os.getpid()}_{fname}"
            tmp.write_bytes(data)
            text = read_notes_text(tmp)
            upload = None
        else:
            if not path:
                path, _ = QFileDialog.getOpenFileName(self, "Import notes list", "", NOTES_FILTER)
                if not path:
                    return
            text = guard(self, lambda: read_notes_text(path)) or ""
            upload = path
        notes = parse_notes(text)
        if not notes:
            QMessageBox.information(self, "Notes", "No numbered notes were found (expected lines like "
                                    "\"1. BREAK ALL SHARP EDGES…\").")
            return
        self._notes_dialog(notes, upload)

    def _notes_dialog(self, notes, upload=None):
        specs = {n["label"]: find_specs(n["text"]) for n in notes}
        n0 = self.db.next_char_no(self.rid)
        dlg = NotesDialog(notes, specs, n0, self)
        if dlg.exec() != NotesDialog.Accepted:
            return
        chosen = dlg.selected()
        rows = []
        for i, n in enumerate(chosen):
            r = {"char_no": n0 + i, "ref_location": f"NOTE {n['label']}", "char_type": "Note",
                 "requirement": n["text"]}
            if dlg.skip_general.isChecked() and GENERAL_NOTE_RE.search(n["text"]):
                r.update(results="N/A", disposition="N/A", comments="General note — not an inspectable requirement")
            rows.append(r)
        if rows:
            guard(self, lambda: self.db.insert_many("characteristics", self.rid, rows))
        if dlg.mk_form2.isChecked():
            have = {m["spec_number"].upper() for m in self.mat_model.rows}
            mats = []
            seq = max([m["seq"] for m in self.mat_model.rows] + [0]) + 1
            for n in chosen:
                for sp in find_specs(n["text"]):
                    if sp in have:
                        continue
                    have.add(sp)
                    mats.append({"seq": seq, "category": classify_spec(n["text"]), "spec_number": sp,
                                 "name": n["text"][:90], "comments": f"From note {n['label']}"})
                    seq += 1
            if mats:
                guard(self, lambda: self.db.insert_many("materials", self.rid, mats))
        if upload and not any(a["filename"] == Path(upload).name and a["kind"] == "Notes List"
                              for a in self.att_model.rows):
            guard(self, lambda: self.db.add_attachment(self.rid, upload, "Notes List"), quiet=True)
        self.reload_all()
        self.tabs.setCurrentIndex(2)

    def specs_from_notes(self):
        have = {m["spec_number"].upper() for m in self.mat_model.rows}
        mats = []
        seq = max([m["seq"] for m in self.mat_model.rows] + [0]) + 1
        for c in self.char_model.rows:
            for s in find_specs(c["requirement"]):
                if s not in have:
                    have.add(s)
                    mats.append({"seq": seq, "category": classify_spec(c["requirement"]), "spec_number": s,
                                 "name": c["requirement"][:90], "comments": f"From char {c['char_no']}"})
                    seq += 1
        if not mats:
            QMessageBox.information(self, "Specifications", "No new specification numbers found in Form 3.")
            return
        guard(self, lambda: self.db.insert_many("materials", self.rid, mats))
        self.reload_all()

    def import_results(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import measured results", "",
                                              "Results (*.xlsx *.xlsm *.csv *.tsv *.txt *.pdf);;All files (*)")
        if not path:
            return
        rows = guard(self, lambda: read_table(path))
        if not rows:
            return
        multi = mp.applies(self.rep)
        fields = dict(RESULT_FIELDS, **mp.IMPORT_FIELDS) if multi else RESULT_FIELDS
        dlg = MappingDialog(f"Import results — {Path(path).name}", rows, {"Results": fields}, self,
                            labels={"char_no": "Char / balloon no.",
                                    "results": "Measured value" + (" (or low reading)" if multi else ""),
                                    "tooling": "Gage", "part": "Part no. / serial (optional)",
                                    "results_hi": "High reading (optional)"})
        if dlg.exec() != MappingDialog.Accepted:
            return
        by_no = {}
        for rec in dlg.records():
            m = re.search(r"\d+", str(rec.get("char_no", "")))
            if m and rec.get("results", "") != "":
                by_no.setdefault(int(m.group(0)), []).append(rec)
        updated, missing = 0, []
        for no, recs in by_no.items():
            row = next((r for r in self.char_model.rows if r["char_no"] == no), None)
            if not row:
                missing.append(no)
                continue
            if multi:
                # rows go to the parts (by part / serial column, otherwise in file order)
                fresh = self.db.row("characteristics", row["id"]) or row
                rd = mp.parse(fresh.get("part_readings"))
                for part, vals in mp.assign_imports(recs, self.rep).items():
                    rd[part] = {"lo": vals["lo"], "hi": vals["hi"] if mp.minmax(self.rep) else "", "d": ""}
                rd = mp.recompute(rd, fresh, mp.minmax(self.rep))
                txt, disp = mp.rollup(rd, mp.part_count(self.rep), mp.minmax(self.rep))
                changes = {"part_readings": mp.dump(rd), "results": txt}
                if fresh.get("disposition") != "N/A":
                    changes["disposition"] = disp
            else:
                res = ", ".join(str(r["results"]) for r in recs)
                changes = self._compute(row, "results", res)
            if recs[0].get("tooling"):
                changes["tooling"] = recs[0]["tooling"]
            new = guard(self, lambda row=row, changes=changes: self.db.update_fields("characteristics", row["id"], changes))
            if new:
                self._put_char(new)
                updated += 1
        self._after_change()
        if multi:
            self._refresh_parts_status()
        guard(self, lambda: self.db.add_attachment(self.rid, path, "CMM Report"), quiet=True)
        self.reload_attachments()
        msg = f"{updated} characteristic(s) updated."
        if missing:
            msg += f"\nNo matching characteristic for: {', '.join(map(str, missing[:30]))}"
        QMessageBox.information(self, "Import results", msg)

    def run_intake(self, drawings, photos, parts, notes, auto_balloon=True, notes_from_drawing=True):
        """Build a report from source files (called by the New Report wizard)."""
        for d in drawings or []:
            if d.lower().endswith(".zip"):
                self.upload_package(d)
            else:
                self.upload_files([d], "Drawing")
        if photos:
            self.upload_files(photos, "Photo")
        if parts:
            self.import_parts_list(parts)
        if notes:
            self.import_notes(notes)
        if auto_balloon and self._drawing_atts():
            # ask which part number is being ballooned, then balloon only that part (+ its notes)
            self.balloon_part()
        elif notes_from_drawing and not notes:
            from ..docrender import pdf_text
            for a in [a for a in self.att_model.rows if a["kind"] == "Drawing" and is_pdf(a["filename"])][:1]:
                data = self.db.attachment_bytes(a["id"])
                if parse_notes(pdf_text(data)):
                    self.import_notes(from_bytes=(a["filename"], data))
        self.tabs.setCurrentIndex(2)

    # ---------------------------------------------------------------- drawing packages (.zip)
    @staticmethod
    def classify_package_file(name: str, data: bytes = b"") -> str:
        n = name.upper().replace(" ", "_")
        if re.search(r"(^|_)(A?EO|ECN|DCN)(_|\d|$)|ENGINEERING[_ ]ORDER", n):
            return "Engineering Order"
        if re.search(r"(^|_)PL(_|$)|PARTS?[_ ]LIST|(^|_)C?E?BOM(_|$)|ENGINEERING[_ ]BOM", n) and not re.search(r"DRAWING", n):
            return "Parts List"
        if re.search(r"(^|_)NL(_|$)|NOTES?[_ ]LIST", n):
            return "Notes List"
        if re.search(r"(^|_)AL(_|$)|APPLICATION[_ ]LIST", n):
            return "Application List"
        if is_image(name):
            return "Drawing" if re.search(r"DWG|DRAW|SHEET|SH\d", n) else "Photo"
        if n.endswith((".XLSX", ".XLSM", ".CSV")):
            return "Parts List"
        if n.endswith(".PDF"):
            return "Drawing"
        return "Other"

    def upload_package(self, path: str):
        """Unpack a drawing package ZIP (also nested ZIPs) and file each document by type."""
        import zipfile

        def walk(zf, prefix=""):
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename.split("/")[-1]
                data = zf.read(info)
                if name.lower().endswith(".zip"):
                    try:
                        with zipfile.ZipFile(io.BytesIO(data)) as inner:
                            yield from walk(inner, prefix)
                    except zipfile.BadZipFile:
                        continue
                elif not name.startswith(".") and "__MACOSX" not in info.filename:
                    yield name, data
        try:
            with zipfile.ZipFile(path) as zf:
                files = list(walk(zf))
        except zipfile.BadZipFile:
            QMessageBox.warning(self, "Package", "That file is not a valid ZIP archive.")
            return []
        out = []
        prog = QProgressDialog("Unpacking drawing package…", None, 0, len(files), self)
        prog.setWindowModality(Qt.WindowModal)
        prog.setMinimumDuration(300)
        for i, (name, data) in enumerate(sorted(files)):
            prog.setValue(i)
            kind = self.classify_package_file(name, data)
            r = guard(self, lambda name=name, data=data, kind=kind:
                      self.db.add_attachment(self.rid, name, kind, description=f"From {Path(path).name}",
                                             data=data, filename=name))
            if r:
                out.append(r)
                if kind in ("Engineering Order", "Application List", "Parts List", "Notes List"):
                    guard(self, lambda r=r: self.db.update_field("attachments", r["id"], "include_in_report", False),
                          quiet=True)
        prog.setValue(len(files))
        self.reload_attachments()
        self._after_change()
        return out

    # ======================================================================= Certificate of Conformance
    def _build_coc(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        box = QGroupBox("Certificate details  —  part number, customer and PO come from Form 1")
        self.coc_form = ResponsiveForm(box, min_col_width=330)
        for key, label in COC_FIELDS:
            if key in DATE_FIELDS:
                wdg = QLineEdit()
                wdg.setPlaceholderText("YYYY-MM-DD")
                wdg.editingFinished.connect(lambda k=key: self._hdr_commit(k))
                today = QToolButton()
                today.setText("Today")
                today.clicked.connect(lambda _=False, k=key, e=wdg: self._set_today(k, e))
                holder = QWidget()
                hl = QHBoxLayout(holder)
                hl.setContentsMargins(0, 0, 0, 0)
                hl.addWidget(wdg, 1)
                hl.addWidget(today)
                self.coc_form.add_row(label, holder)
            else:
                wdg = QLineEdit()
                wdg.editingFinished.connect(lambda k=key: self._hdr_commit(k))
                self.coc_form.add_row(label, wdg)
            wdg.setProperty("hdr_key", key)
            wdg.installEventFilter(self)
            self.hdr_widgets[key] = wdg
        lay.addWidget(box)

        self.coc_model = RecordModel(COC_COLS, self._coc_edit)
        self.coc_view = self._make_table(self.coc_model, COC_COLS)
        self.coc_view.setWordWrap(True)
        self.coc_view.verticalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        tb = FlowRow()
        b = QPushButton("⚙ Build from drawing / BOM / notes")
        b.setObjectName("primary")
        b.setToolTip("Reads the drawing parts list (or the imported BOM), the flag / general notes and Form 2, "
                     "and proposes the material, purchased-part, process and test rows. Vendors are matched from "
                     "the Vendor Library.")
        b.clicked.connect(self.coc_build)
        tb.addWidget(b)
        for text, fn, tip in (
                ("➕ Add", lambda: self._add_simple("coc_items", self.coc_model, {"item_type": "Material"}), ""),
                ("🗑 Delete", lambda: self._delete_simple("coc_items", self.coc_view, self.coc_model), ""),
                ("▲", lambda: self.coc_move(-1), "Move the selected row up"),
                ("▼", lambda: self.coc_move(1), "Move the selected row down"),
                ("🏷 Set vendor…", self.coc_set_vendor, "Set the vendor on the selected rows"),
                ("📇 Vendor Library…", self.vendor_library, "Shared list of approved vendors and addresses"),
                ("🖨 Preview CoC", self.coc_preview, "Open the certificate in your PDF viewer"),
                ("⬇ Export CoC PDF…", self.coc_export, "")):
            bb = QPushButton(text)
            bb.setToolTip(tip)
            bb.clicked.connect(fn)
            tb.addWidget(bb)
        lay.addWidget(tb)
        lay.addWidget(self.coc_view, 1)
        hint = QLabel("Rows print in this order on the certificate. Double-click a cell to edit; the vendor address "
                      "fills in from the Vendor Library when you pick a vendor. The certification statement and footer "
                      "are set in ⚙ Settings → Company.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#4a4f52;")
        lay.addWidget(hint)
        return w

    def _coc_placeholders(self):
        r = self.rep
        ph = {"coc_part_description": r.get("part_name") or "", "coc_qty_shipped": str(r.get("quantity") or ""),
              "coc_certified_by": r.get("prepared_by") or self.db.user,
              "coc_title": self.db.get_meta().get("coc_default_title", ""),
              "coc_date": "same as date shipped"}
        for k, v in ph.items():
            if k in self.hdr_widgets:
                self.hdr_widgets[k].setPlaceholderText(v)

    def _coc_fit_rows(self):
        QTimer.singleShot(0, self.coc_view.resizeRowsToContents)

    def _coc_edit(self, row, key, value, expected=None):
        new = self._edit_simple("coc_items", row, key, value, expected)
        if new and key == "vendor_name" and value:
            v = next((x for x in self.db.vendors(False) if x["name"].strip().upper() == str(value).strip().upper()), None)
            if v and v.get("address") and not (new.get("vendor_info") or "").strip():
                new2 = guard(self, lambda: self.db.update_field("coc_items", new["id"], "vendor_info", v["address"],
                                                                 expected_old=new.get("vendor_info")))
                if new2:
                    new = new2
                    QTimer.singleShot(0, lambda n=new2: self.coc_model.replace_row(n))
        self._coc_fit_rows()
        return new

    def _coc_selected(self) -> list[dict]:
        rows = sorted({i.row() for i in self.coc_view.selectionModel().selectedIndexes()})
        return [self.coc_model.rows[i] for i in rows if i < len(self.coc_model.rows)]

    def coc_move(self, delta):
        sel = self._coc_selected()
        if len(sel) != 1 or self.rep["locked"]:
            return
        rows = self.coc_model.rows
        i = rows.index(sel[0])
        j = i + delta
        if not 0 <= j < len(rows):
            return
        order = [r["id"] for r in rows]
        order[i], order[j] = order[j], order[i]
        byid = {r["id"]: r for r in rows}

        def work():
            for n, rid in enumerate(order, start=1):
                if byid[rid]["seq"] != n:
                    self.db.update_field("coc_items", rid, "seq", n, expected_old=byid[rid]["seq"])
            return True
        guard(self, work)
        self.coc_model.set_rows(self.db.rows("coc_items", self.rid))
        self._coc_fit_rows()
        self.coc_view.selectRow(j)
        self._after_change()

    def coc_set_vendor(self):
        sel = self._coc_selected()
        if not sel or self.rep["locked"]:
            QMessageBox.information(self, "Set vendor", "Select one or more rows first.")
            return
        vendors = self.db.vendors()
        names = [v["name"] for v in vendors]
        name, ok = QInputDialog.getItem(self, "Set vendor", f"Vendor for {len(sel)} row(s):", names, 0, True)
        if not ok or not name.strip():
            return
        v = next((x for x in vendors if x["name"] == name), None)

        def work():
            for r in sel:
                vals = {"vendor_name": name.strip()}
                if v:
                    vals["vendor_info"] = v.get("address") or ""
                self.db.update_fields("coc_items", r["id"], vals)
            return True
        guard(self, work)
        self.coc_model.set_rows(self.db.rows("coc_items", self.rid))
        self._coc_fit_rows()
        self._after_change()

    def vendor_library(self):
        from .dialogs import VendorDialog
        VendorDialog(self.db, self).exec()

    def _coc_other_sources(self, rest_ids: set):
        """Technical data the drawing / list readers don't cover, for the CoC:
        - notes lists in Word / Excel / text  -> note texts
        - parts lists / BOMs in Excel / CSV   -> parts rows (like the Form 1 index)
        - engineering orders, specifications, other PDFs / documents that are not lists -> their text
        Returns (notes, [(document name, text)], parts rows)."""
        import tempfile
        from ..importers import (COMPONENT_FIELDS, apply_mapping, detect_mapping, parse_notes, read_notes_text,
                                 read_table)
        notes, docs, comps = [], [], []
        for a in self.att_model.rows:
            fn = a["filename"]
            ext = Path(fn).suffix.lower()
            kind = a["kind"]
            if kind in ("Photo", "Certificate", "CMM Report"):
                continue
            is_pdf_ = ext == ".pdf"
            if is_pdf_ and a["id"] not in rest_ids:
                continue                                  # read as a parts list / notes list already
            if is_pdf_ and kind in ("Drawing", "Parts List", "Notes List"):
                continue                                  # read by the drawing reader
            if not is_pdf_ and ext not in (".docx", ".txt", ".xlsx", ".xlsm", ".csv", ".tsv"):
                continue
            data = self.db.attachment_bytes(a["id"])
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
                f.write(data)
                tmp = f.name
            try:
                if kind == "Parts List" and ext in (".xlsx", ".xlsm", ".csv", ".tsv"):
                    rows = read_table(tmp)
                    hi, mp_ = detect_mapping(rows, COMPONENT_FIELDS)
                    if "part_number" in mp_:
                        comps += [r for r in apply_mapping(rows, hi, mp_) if r.get("part_number")]
                    continue
                if is_pdf_:
                    text = self._pdf_text_any(data)
                else:
                    text = read_notes_text(tmp)
                if kind == "Notes List":
                    notes += [n["text"] for n in parse_notes(text)]
                elif text.strip():
                    docs.append((f"{kind.lower()} {fn}", text))
            except Exception:
                continue
            finally:
                try:
                    Path(tmp).unlink()
                except Exception:
                    pass
        return notes, docs, comps

    @staticmethod
    def _pdf_text_any(data: bytes) -> str:
        """Text of a PDF: the text layer, or OCR for scanned pages (engineering orders are often scans)."""
        import hashlib
        import pymupdf
        from ..bom import PageData, _lines, repair_ocr
        h = hashlib.sha256(data).hexdigest()
        out = []
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            for p in doc:
                pd = PageData(p, h, True)
                t = "\n".join(" ".join(w.t for w in ln) for ln in _lines(pd.words())) if pd.scanned else pd.text
                t = re.sub(r"(?m)^.*(Boeing Proprietary|Export License|Document Number:|User is responsible|"
                           r"Not Valid After).*$", "", t)
                out.append(repair_ocr(t) if pd.scanned else t)
        return "\n".join(out)

    def coc_build(self):
        from .. import coc
        from ..bom import parse_pdf
        from .workers import run_with_progress
        if self.rep["locked"]:
            QMessageBox.information(self, "Locked", "This report is locked.")
            return
        res = self._gather_boms()
        if res is None:
            return
        boms, rest = res
        rest_ids = {a["id"] for a in rest}
        drawings = [a for a in self.att_model.rows if a["kind"] in ("Drawing", "Parts List", "Notes List")
                    and is_viewable(a["filename"]) and (a["id"] in rest_ids or not a["filename"].lower().endswith(".pdf"))]
        analyzed = guard(self, lambda: self._analyze(drawings)) if drawings else []
        if analyzed is None:
            return
        infos = [i for _, i in analyzed]
        notes = [c["requirement"] for c in self.char_model.rows if c.get("char_type") == "Note" and c.get("requirement")]
        extra = guard(self, lambda: self._coc_other_sources(rest_ids))
        if extra is None:
            return
        more_notes, tech_docs, more_comps = extra
        comps = list(self.comp_model.rows)
        have_pn = {(c.get("part_number") or "").upper() for c in comps}
        comps += [c for c in more_comps if (c.get("part_number") or "").upper() not in have_pn]
        items = guard(self, lambda: coc.build_items(self.rep, infos, self.mat_model.rows, self.db.vendors(),
                                                    comps, notes + more_notes, boms, tech_docs))
        if items is None:
            return
        if boms:
            from ..bom import Bom
            tree = Bom()
            for b in boms:
                tree.merge(b)
            self._coc_scope = tree.scope(self.rep["part_number"]) if tree.resolve(self.rep["part_number"]) else []
            if not tree.resolve(self.rep["part_number"]):
                QMessageBox.information(
                    self, "Build CoC", f"The parts list(s) read ({', '.join(sorted(tree.lists)) or '—'}) do not list "
                    f"part number {self.rep['part_number']}. Check the part number on Form 1 (including the dash "
                    "number) - the certificate is built for that exact part and everything below it.")
        if not items:
            QMessageBox.information(self, "Build CoC", "Nothing to certify was found - no parts list, material or "
                                    "process notes could be read. Add the rows by hand, or upload / import the "
                                    "drawing, BOM or notes list first.")
            return
        existing = self.coc_model.rows
        mode = "replace"
        if existing:
            mb = QMessageBox(self)
            mb.setWindowTitle("Build CoC")
            mb.setText(f"Found {len(items)} row(s). The certificate already has {len(existing)} row(s).")
            mb.setInformativeText("Replace them, or only add the rows that are new? (Cert / lot numbers you "
                                  "typed are kept when you add.)")
            b_add = mb.addButton("Add new rows only", QMessageBox.AcceptRole)
            b_rep = mb.addButton("Replace all", QMessageBox.DestructiveRole)
            mb.addButton(QMessageBox.Cancel)
            mb.exec()
            if mb.clickedButton() is b_add:
                mode = "append"
            elif mb.clickedButton() is not b_rep:
                return
        if mode == "append":
            have = {re.sub(r"\s+", " ", r["description"] or "").strip().upper() for r in existing}
            items = [it for it in items if re.sub(r"\s+", " ", it["description"]).strip().upper() not in have]
            start = max([r["seq"] for r in existing] + [0])
        else:
            start = 0

        def work():
            if mode == "replace":
                for r in existing:
                    self.db.delete("coc_items", r["id"])
            rows = [{**it, "seq": start + n} for n, it in enumerate(items, start=1)]
            return self.db.insert_many("coc_items", self.rid, rows) if rows else []
        if guard(self, work) is not None:
            self.coc_model.set_rows(self.db.rows("coc_items", self.rid))
            self._coc_fit_rows()
            self._after_change()
            nv = sum(1 for it in items if not it.get("vendor_name"))
            sc = getattr(self, "_coc_scope", [])
            msg = (f"CoC: {len(items)} row(s) {'added' if mode == 'append' else 'built'}"
                   + (f" for {self.rep['part_number']} and {len(sc) - 1} part(s) below it" if len(sc) > 1 else "")
                   + (f" · {nv} need a vendor" if nv else "") + " · fill in the Cert / Lot # column")
            sb = getattr(self.window(), "statusBar", None)
            if callable(sb):
                sb().showMessage(msg, 12000)

    def _gather_boms(self, kinds=("Drawing", "Parts List", "Notes List", "Application List", "Engineering Order",
                                  "Specification", "Other")):
        """Read every attached PDF that is a parts list / BOM / notes list / application list (text or scan).
        Returns (boms, attachments that were not lists) or None when cancelled / failed."""
        from ..bom import parse_pdf
        from .. import rules as _rules
        from .workers import run_with_progress
        _rules.load_shared(self.db)
        pdfs = [a for a in self.att_model.rows if a["kind"] in kinds and a["filename"].lower().endswith(".pdf")]
        loads = guard(self, lambda: [(a, self.db.attachment_bytes(a["id"])) for a in pdfs])
        if loads is None:
            return None

        def work(cb):
            boms, rest = [], []
            for i, (a, data) in enumerate(loads):
                cb(f"Reading {a['filename']}…", i / max(1, len(loads)))
                b = parse_pdf(data, a["filename"], True,
                              lambda m, f, i=i: cb(m, (i + f) / max(1, len(loads))))
                (boms.append(b) if b else rest.append(a))
            return boms, rest
        return guard(self, lambda: run_with_progress(self, "Reading parts lists / BOMs / notes lists…", work))

    def index_from_bom(self, quiet: bool = False) -> bool:
        """Form 1 index of part numbers for an assembly, straight from the attached BOM / separate parts list
        (every sub-assembly, detail and purchased part below the report's part number)."""
        from ..bom import Bom
        if self.rep["locked"]:
            if not quiet:
                QMessageBox.information(self, "Locked", "This report is locked.")
            return False
        res = self._gather_boms()
        if res is None:
            return False
        boms, _rest = res
        tree = Bom()
        for b in boms:
            tree.merge(b)
        pn = self.rep["part_number"]
        rows = tree.index_rows(pn) if tree.resolve(pn) else []
        if not rows:
            # no separate list for this part number: the parts list on the drawing
            dwg = [a for a in self.att_model.rows if a["kind"] == "Drawing" and is_viewable(a["filename"])]
            analyzed = guard(self, lambda: self._analyze(dwg)) if dwg else []
            for _a, info in analyzed or []:
                opt = next((o for o in info.part_options() if o.part_number.upper() == (pn or "").upper()), None)
                if opt is not None and opt.kind == "Assembly":
                    rows = [dict(k, level=1, kind="", material=k.get("notes") or "") for k in info.children(opt)]
                    break
        if not rows:
            if not quiet:
                lists = ", ".join(sorted(tree.lists)) or "none found"
                QMessageBox.information(self, "Index of part numbers",
                                        f"No parts were found below {pn or '(no part number)'}.\n\n"
                                        f"Parts lists read: {lists}. Check the part number on Form 1 (including the "
                                        "dash number), attach the BOM / parts list (Attachments → Parts List), or add "
                                        "the rows by hand. A detail part has no index.")
            return False
        existing = self.comp_model.rows
        mode = "replace"
        if existing:
            mb = QMessageBox(self)
            mb.setWindowTitle("Index of part numbers")
            mb.setText(f"Found {len(rows)} part number(s) below {pn}. Form 1 already lists {len(existing)}.")
            b_add = mb.addButton("Add new ones only", QMessageBox.AcceptRole)
            b_rep = mb.addButton("Replace the list", QMessageBox.DestructiveRole)
            mb.addButton(QMessageBox.Cancel)
            mb.exec()
            if mb.clickedButton() is b_add:
                mode = "append"
            elif mb.clickedButton() is not b_rep:
                return False
        have = {(r["part_number"] or "").upper() for r in existing} if mode == "append" else set()
        start = max([r["seq"] for r in existing] + [0]) if mode == "append" else 0
        out = []
        for r in rows:
            if r["part_number"].upper() in have and r.get("level", 1) == 1:
                continue
            note = " · ".join(x for x in (r.get("kind") or "", f"in {r['parent']}" if r.get("level", 1) > 1 else "",
                                          r.get("material") or "") if x)
            out.append({"seq": start + len(out) + 1, "item_no": r.get("item_no") or r.get("item_no", ""),
                        "part_number": r["part_number"],
                        "part_name": ("    " * (r.get("level", 1) - 1)) + (r.get("part_name") or ""),
                        "qty": str(r.get("qty") or ""), "notes": note[:250]})

        def work():
            if mode == "replace":
                for r in existing:
                    self.db.delete("components", r["id"])
            return self.db.insert_many("components", self.rid, out) if out else []
        if guard(self, work) is not None:
            self.comp_model.set_rows(self.db.rows("components", self.rid))
            self._after_change()
            if not quiet:
                self.set_status_msg(f"Form 1: {len(out)} part number(s) from the parts list for {pn}", 10000)
            return True
        return False

    def _coc_default_name(self):
        safe = re.sub(r"[^\w.\-]+", "_", f"{self.rep['part_number']}_COC")
        return str(Path(self.cfg.last_export_dir or Path.home()) / f"{safe}.pdf")

    def _coc_check(self) -> bool:
        rows = self.coc_model.rows
        miss = [r for r in rows if not (r.get("cert_lot") or "").strip() and r["item_type"] != "Purchased"]
        probs = []
        if not rows:
            probs.append("the certification table is empty")
        if miss:
            probs.append(f"{len(miss)} row(s) have no Cert / Lot #")
        if not (self.rep.get("coc_date_shipped") or "").strip():
            probs.append("Date shipped is blank")
        if probs:
            return QMessageBox.question(self, "Certificate of Conformance",
                                        "Before you sign it: " + "; ".join(probs) + ".\n\nCreate the PDF anyway?") \
                == QMessageBox.Yes
        return True

    def coc_preview(self):
        from .. import coc
        safe = re.sub(r"[^\w-]+", "_", self.rep["part_number"] or "part")
        fd, p = tempfile.mkstemp(prefix=f"CoC_{safe}_", suffix=".pdf")
        os.close(fd)
        if guard(self, lambda: coc.build_coc_pdf(self.db, self.rid, p)):
            QDesktopServices.openUrl(QUrl.fromLocalFile(p))

    def coc_export(self):
        from .. import coc
        if not self._coc_check():
            return
        p, _ = QFileDialog.getSaveFileName(self, "Export Certificate of Conformance", self._coc_default_name(),
                                           "PDF (*.pdf)")
        if not p:
            return
        if guard(self, lambda: coc.build_coc_pdf(self.db, self.rid, p)):
            self.cfg.last_export_dir = str(Path(p).parent)
            self.cfg.save()
            if QMessageBox.question(self, "Exported", f"Saved:\n{p}\n\nOpen it now?") == QMessageBox.Yes:
                QDesktopServices.openUrl(QUrl.fromLocalFile(p))

    # ======================================================================= preview / export
    COC_DOC = "Certificate of Conformance"

    def _build_preview(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.doc_combo = QComboBox()
        self.doc_combo.addItems(REPORT_TYPES + [self.COC_DOC])
        self.doc_combo.setToolTip("Which document to preview and export. Every inspection report type and the CoC "
                                  "can be exported on its own from the same data - the type on Form 1 is only the "
                                  "default.")
        self.doc_combo.activated.connect(self._doc_changed)
        self.opts = ReportOptionsWidget(is_fai(self.rep["report_type"]))
        top = FlowRow()
        lbl = QLabel("<b>Document:</b>")
        top.addWidget(lbl)
        top.addWidget(self.doc_combo)
        b1 = QPushButton("⟳ Refresh Preview")
        b1.clicked.connect(self.refresh_preview)
        b2 = QPushButton("⬇ Export PDF…")
        b2.setObjectName("primary")
        b2.clicked.connect(self.export_pdf)
        self.btn_xlsx = QPushButton("⬇ Export Excel…")
        self.btn_xlsx.clicked.connect(self.export_excel)
        b4 = QPushButton("🖨 Open / Print")
        b4.clicked.connect(self.open_pdf)
        b5 = QPushButton("📦 Export Several…")
        b5.setToolTip("Export any combination of the report types and the CoC in one go, each as its own file")
        b5.clicked.connect(self.export_several)
        for b in (b1, b2, self.btn_xlsx, b4, b5):
            top.addWidget(b)
        lay.addWidget(top)
        lay.addWidget(self.opts)
        self.preview_info = QLabel("Preview updates automatically while this tab is open.")
        self.preview_info.setStyleSheet("color:#4a4f52;")
        self.preview_info.setWordWrap(True)
        lay.addWidget(self.preview_info)
        self.preview_area = QScrollArea()
        self.preview_area.setWidgetResizable(True)
        self.preview_area.setStyleSheet("background:#5A5F63;")
        self.preview_host = QWidget()
        self.preview_lay = QVBoxLayout(self.preview_host)
        self.preview_lay.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.preview_area.setWidget(self.preview_host)
        lay.addWidget(self.preview_area, 1)
        self._last_preview: Optional[Path] = None
        self._doc_follow = True                          # follow the Form 1 report type until the user picks
        return w

    def current_doc(self) -> str:
        return self.doc_combo.currentText() if hasattr(self, "doc_combo") else self.rep["report_type"]

    def _sync_doc_combo(self):
        """Keep the Document picker on the report's own type unless the user chose another one."""
        if getattr(self, "_doc_follow", True) and hasattr(self, "doc_combo"):
            i = self.doc_combo.findText(self.rep["report_type"])
            if i >= 0 and i != self.doc_combo.currentIndex():
                self.doc_combo.setCurrentIndex(i)
                self._doc_changed(i, user=False)

    def _doc_changed(self, _i=None, user=True):
        if user:
            self._doc_follow = self.current_doc() == self.rep["report_type"]
        doc = self.current_doc()
        coc = doc == self.COC_DOC
        self.opts.setVisible(not coc)
        self.opts.set_fai(is_fai(doc))
        self.btn_xlsx.setEnabled(not coc)
        self._preview_dirty = True
        if self.tabs.currentWidget() is self.preview_tab:
            self.refresh_preview()

    def _opts(self, doc: Optional[str] = None):
        o = self.opts.options(self.viewer.balloon_scale)
        o.report_type = doc or self.current_doc()
        return o

    def _render_doc(self, doc: str, path) -> Optional[Path]:
        if doc == self.COC_DOC:
            from ..coc import build_coc_pdf
            return build_coc_pdf(self.db, self.rid, path)
        from ..reports import build_pdf
        return build_pdf(self.db, self.rid, path, self._opts(doc))

    def refresh_preview(self):
        doc = self.current_doc()
        tmp = Path(tempfile.gettempdir()) / f"fai_preview_{abs(self.rid)}_{re.sub(r'[^A-Za-z0-9]+', '_', doc)}.pdf"
        path = guard(self, lambda: self._render_doc(doc, tmp))
        if not path:
            return
        self._last_preview = path
        while self.preview_lay.count():
            it = self.preview_lay.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        with pymupdf.open(path) as pdf:
            n = pdf.page_count
            width = max(480, self.preview_area.viewport().width() - 70)
            for i, page in enumerate(pdf):
                if i >= 40:
                    lbl = QLabel(f"… {n - 40} more page(s) — export to see all")
                    lbl.setStyleSheet("color:white;")
                    self.preview_lay.addWidget(lbl)
                    break
                zoom = min(2.0, width / page.rect.width)
                pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
                img = QImage.fromData(pix.tobytes("png"))
                lbl = QLabel()
                lbl.setPixmap(QPixmap.fromImage(img))
                lbl.setStyleSheet("background:white; border:1px solid #333; margin:6px;")
                self.preview_lay.addWidget(lbl)
        from datetime import datetime
        self.preview_info.setText(f"<b>{doc}</b> · {n} page(s) · refreshed {datetime.now():%H:%M:%S} · preview "
                                  f"updates automatically when anyone edits this report")
        self._preview_dirty = False

    def _auto_preview(self):
        if self.tabs.currentWidget() is self.preview_tab and self._preview_dirty and self.isVisible():
            self.refresh_preview()

    DOC_SUFFIX = {"AS9102 FAI": "FAI", "AS9102 FAI - Clean": "FAI_Clean", "Standard Report": "Standard_Report",
                  "Inspection Report": "Inspection_Report", "Certificate of Conformance": "COC"}

    def _default_name(self, ext, doc: Optional[str] = None):
        r = self.rep
        doc = doc or self.current_doc()
        suffix = self.DOC_SUFFIX.get(doc, re.sub(r"\W+", "_", doc))
        if doc == self.COC_DOC:
            base = f"{r['part_number']}_{suffix}"
        else:
            rev = f"_Rev{r['part_rev']}" if (r.get("part_rev") or "").strip() else ""
            base = f"{r['report_number']}_{r['part_number']}{rev}_{suffix}"
        safe = re.sub(r"[^\w.\-]+", "_", base)
        return str(Path(self.cfg.last_export_dir or Path.home()) / f"{safe}.{ext}")

    def export_pdf(self):
        doc = self.current_doc()
        if doc == self.COC_DOC:
            return self.coc_export()
        p, _ = QFileDialog.getSaveFileName(self, f"Export {doc} PDF", self._default_name("pdf"), "PDF (*.pdf)")
        if not p:
            return
        if guard(self, lambda: self._render_doc(doc, p)):
            self.cfg.last_export_dir = str(Path(p).parent)
            self.cfg.save()
            if QMessageBox.question(self, "Exported", f"Saved:\n{p}\n\nOpen it now?") == QMessageBox.Yes:
                QDesktopServices.openUrl(QUrl.fromLocalFile(p))

    def export_excel(self):
        from ..excel_export import export_excel
        doc = self.current_doc()
        if doc == self.COC_DOC:
            return
        p, _ = QFileDialog.getSaveFileName(self, f"Export {doc} Excel", self._default_name("xlsx"), "Excel (*.xlsx)")
        if not p:
            return
        if guard(self, lambda: export_excel(self.db, self.rid, p, doc)):
            self.cfg.last_export_dir = str(Path(p).parent)
            self.cfg.save()
            QMessageBox.information(self, "Exported", f"Saved:\n{p}")

    def export_several(self):
        from .dialogs import ExportSetDialog
        dlg = ExportSetDialog(REPORT_TYPES + [self.COC_DOC], self.current_doc(),
                              self.cfg.last_export_dir or str(Path.home()), self)
        if dlg.exec() != ExportSetDialog.Accepted:
            return
        folder, picks, xlsx = dlg.result_values()
        if not picks:
            return
        if self.COC_DOC in picks and not self._coc_check():
            picks = [p for p in picks if p != self.COC_DOC]
        from ..excel_export import export_excel
        done = []

        def work():
            for doc in picks:
                p = Path(self._default_name("pdf", doc)).name
                out = Path(folder) / p
                self._render_doc(doc, out)
                done.append(str(out))
                if xlsx and doc != self.COC_DOC:
                    x = out.with_suffix(".xlsx")
                    export_excel(self.db, self.rid, x, doc)
                    done.append(str(x))
            return True
        if guard(self, work):
            self.cfg.last_export_dir = folder
            self.cfg.save()
            if QMessageBox.question(self, "Exported", f"Saved {len(done)} file(s) to\n{folder}\n\n"
                                    + "\n".join(Path(d).name for d in done) + "\n\nOpen the folder?") == QMessageBox.Yes:
                QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def open_pdf(self):
        if self._preview_dirty or not self._last_preview:
            self.refresh_preview()
        if self._last_preview:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._last_preview)))

    # ======================================================================= history
    def _build_history(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.hist = QTableWidget(0, 7)
        self.hist.setHorizontalHeaderLabels(["When", "User", "Workstation", "Action", "Record", "Field", "Change"])
        self.hist.horizontalHeader().setSectionResizeMode(6, QHeaderView.Stretch)
        self.hist.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.hist.setAlternatingRowColors(True)
        lay.addWidget(QLabel("Complete audit trail — every change, by whom, from which workstation."))
        lay.addWidget(self.hist)
        return w

    def refresh_history(self):
        rows = self.db.audit_for(self.rid)
        self.hist.setRowCount(len(rows))
        for i, r in enumerate(rows):
            chg = ""
            if r["action"] == "UPDATE":
                chg = f"{(r['old_value'] or '')[:80]!s}  →  {(r['new_value'] or '')[:80]!s}"
            elif r["new_value"]:
                chg = (r["new_value"] or "")[:160]
            elif r["old_value"]:
                chg = (r["old_value"] or "")[:160]
            vals = [r["at"].astimezone().strftime("%Y-%m-%d %H:%M:%S"), r["user_name"], r["host"], r["action"],
                    f"{r['table_name']} #{r['record_id'] or ''}", r["field"] or "", chg]
            for c, v in enumerate(vals):
                self.hist.setItem(i, c, QTableWidgetItem(str(v)))
        self.hist.resizeColumnsToContents()
        self._history_dirty = False

    # ======================================================================= generic helpers
    def _make_table(self, model, cols, cls=None):
        v = (cls or ClipTable)()
        if isinstance(v, ClipTable):
            v.on_bulk_done = lambda msg: (self.set_status_msg(msg, 5000), self._after_change())
        v.setModel(model)
        dlg = RecordDelegate(lambda: model, self._completions, v)
        dlg.gage_source = self._gage_list
        v.setItemDelegate(dlg)
        v.setAlternatingRowColors(True)
        v.setSelectionBehavior(QAbstractItemView.SelectItems)
        v.setSelectionMode(QAbstractItemView.ExtendedSelection)
        v.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed |
                          QAbstractItemView.AnyKeyPressed | QAbstractItemView.SelectedClicked)
        v.verticalHeader().setDefaultSectionSize(24)
        v.verticalHeader().setVisible(False)
        v.horizontalHeader().setMinimumSectionSize(30)
        v.horizontalHeader().setDefaultAlignment(Qt.AlignCenter)
        v.horizontalHeader().setFixedHeight(40)
        for i, c in enumerate(cols):
            v.setColumnWidth(i, c.width)
        v.horizontalHeader().setStretchLastSection(True)
        v.setWordWrap(False)
        return v

    def _gage_list(self) -> list[dict]:
        """The Gage Library, read fresh each time a gage cell is edited (so gages added on any workstation show)."""
        try:
            gs = self.db.gages(True)
        except Exception:
            gs = []
        self._gage_map = {(g["gage_id"] or "").upper(): g for g in gs}
        return gs

    def _completions(self, key):
        if key == "tooling":
            return [f"{g['gage_id']}" for g in self.db.gages(True)]
        if key == "vendor_name":
            return [v["name"] for v in self.db.vendors()]
        return []

    def _edit_simple(self, table, row, key, value, expected=None):
        exp = expected if expected is not None else row.get(key)
        new = guard(self, lambda: self.db.update_field(table, row["id"], key, value, expected_old=exp),
                    on_conflict=lambda e: self._conflict_reload(table, row["id"]))
        if new:
            self._after_change()
        return new

    def _add_simple(self, table, model, values=None):
        values = dict(values or {})
        values["seq"] = max([r["seq"] for r in model.rows] + [0]) + 1
        new = guard(self, lambda: self.db.insert(table, self.rid, values))
        if new:
            model.replace_row(new)
            self._after_change()

    def _delete_simple(self, table, view, model):
        rows = sorted({i.row() for i in view.selectionModel().selectedIndexes()})
        if not rows:
            return
        if QMessageBox.question(self, "Delete", f"Delete {len(rows)} row(s)?") != QMessageBox.Yes:
            return
        for r in [model.rows[i] for i in rows]:
            guard(self, lambda r=r: self.db.delete(table, r["id"]))
            if self.db.row(table, r["id"]) is None:
                model.remove_id(r["id"])
        self._after_change()

    def _after_change(self):
        self._refresh_header_bar()
        self._mark_dirty()

    def _mark_dirty(self):
        self._preview_dirty = True
        self._history_dirty = True
        if self.tabs.currentWidget() is self.preview_tab:
            self._preview_timer.start(1500)
        elif self.tabs.currentWidget() is self.history_tab:
            self.refresh_history()

    def showEvent(self, e):
        super().showEvent(e)
        sp = getattr(self, "_f3_split", None)
        if sp is not None and not sp.property("sized"):
            def size():
                if sp.width() > 300:
                    f = min(0.85, max(0.25, float(getattr(self.cfg, "form3_split", 0.58) or 0.58)))
                    sp.setSizes([int(sp.width() * f), int(sp.width() * (1 - f))])
                    sp.setProperty("sized", True)
            QTimer.singleShot(50, size)

    def _remember_split(self):
        sp = self._f3_split
        a, b = sp.sizes()
        if a + b > 300 and self.viewer.isVisible():
            self.cfg.form3_split = round(a / (a + b), 3)
            if not hasattr(self, "_split_save"):
                self._split_save = QTimer(self)
                self._split_save.setSingleShot(True)
                self._split_save.timeout.connect(lambda: self.cfg.save())
            self._split_save.start(1500)

    def _toggle_drawing(self, on: bool):
        if not hasattr(self, "viewer"):
            return
        self.viewer.setVisible(on)
        if getattr(self.cfg, "form3_drawing", True) != on:
            self.cfg.form3_drawing = on
            try:
                self.cfg.save()
            except Exception:
                pass
        if on:
            self._f3_split.setProperty("sized", False)
            self.showEvent(None) if False else QTimer.singleShot(0, lambda: self._f3_split.setSizes(
                [int(self._f3_split.width() * self.cfg.form3_split),
                 int(self._f3_split.width() * (1 - self.cfg.form3_split))]))

    def _tab_changed(self, i):
        w = self.tabs.widget(i)
        if w is getattr(self, "preview_tab", None) and self._preview_dirty:
            QTimer.singleShot(50, self.refresh_preview)
        if w is getattr(self, "history_tab", None) and self._history_dirty:
            self.refresh_history()
        if w is getattr(self, "coc_tab", None):
            self._coc_placeholders()

    def _apply_lock(self):
        locked = bool(self.rep.get("locked"))
        for k, w in self.hdr_widgets.items():
            if isinstance(w, QComboBox):
                w.setEnabled(not locked)
            else:
                w.setReadOnly(locked)
        for m in (self.char_model, self.comp_model, self.mat_model, self.att_model, self.coc_model):
            m.locked = locked
            m.layoutChanged.emit()
        self.viewer.set_locked(locked)

    # ======================================================================= reload / live sync
    def reload_chars(self):
        rows = [self._decorate(r) for r in self.db.rows("characteristics", self.rid)]
        cur = self.current_char()
        self.char_model.set_rows(rows)
        self._rebuild_char_cols()
        self._refresh_parts_status()
        self.viewer.set_characteristics(self.char_model.rows)
        self._apply_char_filter()
        if cur:
            self._select_char_id(cur["id"])
        self._refresh_header_bar()
        self._mark_dirty()

    def reload_attachments(self):
        self.att_model.set_rows([self._decorate_att(r) for r in self.db.rows("attachments", self.rid)])
        self._refresh_viewer_docs()

    def reload_all(self):
        self.rep = self.db.get_report(self.rid) or self.rep
        self._fill_header(force=True)
        self.comp_model.set_rows(self.db.rows("components", self.rid))
        self.mat_model.set_rows(self.db.rows("materials", self.rid))
        self.coc_model.set_rows(self.db.rows("coc_items", self.rid))
        self._coc_fit_rows()
        self.reload_attachments()
        self.reload_chars()
        self.tabs.setTabVisible(1, is_fai(self.rep["report_type"]))
        self._sync_doc_combo()
        self._apply_lock()
        self._refresh_header_bar()

    def on_remote_event(self, ev: dict):
        self._pending.append(ev)
        self._event_timer.start(150)

    def _editing(self) -> bool:
        return any(v.state() == QAbstractItemView.EditingState
                   for v in (self.char_view, self.comp_view, self.mat_view, self.att_view, self.coc_view))

    def resync(self):
        """Full reload (after live sync reconnects) — deferred while the user is typing in a cell."""
        if self._editing():
            QTimer.singleShot(1000, self.resync)
            return
        self.reload_all()

    def _process_events(self):
        evs, self._pending = self._pending, []
        if not evs:
            return
        big = sum(1 for e in evs if e["t"] in ("characteristics", "components", "materials", "coc_items")) > 20 or \
            any(e["t"] == "attachments" for e in evs)
        if big and self._editing():
            self._pending = evs + self._pending          # wait until the open editor is committed
            self._event_timer.start(700)
            return
        by_table: dict[str, dict[int, str]] = {}
        for e in evs:
            by_table.setdefault(e["t"], {})[e["id"]] = e["op"]
        if "reports" in by_table:
            if any(op == "DELETE" for op in by_table["reports"].values()):
                QMessageBox.warning(self, "Report deleted", "This report was deleted on another workstation.")
                return
            self.reload_header()
            self._apply_lock()
        if "attachments" in by_table:
            self.reload_attachments()
        for table, model in (("components", self.comp_model), ("materials", self.mat_model),
                             ("coc_items", self.coc_model)):
            if table in by_table:
                changes = by_table[table]
                if len(changes) > 20:
                    model.set_rows(self.db.rows(table, self.rid))
                else:
                    for rid, op in changes.items():
                        r = None if op == "DELETE" else self.db.row(table, rid)
                        if r is None:
                            model.remove_id(rid)
                        else:
                            model.replace_row(r, flash=True)
                    model.sort_rows(lambda r: (r["seq"], r["id"]))
        if "coc_items" in by_table:
            self._coc_fit_rows()
        if "characteristics" in by_table:
            changes = by_table["characteristics"]
            if len(changes) > 20 or "attachments" in by_table:
                self.reload_chars()
            else:
                for cid, op in changes.items():
                    r = None if op == "DELETE" else self.db.row("characteristics", cid)
                    if r is None:
                        self.char_model.remove_id(cid)
                        self.viewer.remove_characteristic(cid)
                    else:
                        self._put_char(r, flash=True)
                self.char_model.sort_rows(lambda r: (r["char_no"], r["id"]))
                self._apply_char_filter()
        self._refresh_header_bar()
        self._mark_dirty()

    # ======================================================================= workflow
    def check(self):
        issues = guard(self, lambda: self.db.completeness_issues(self.rid))
        if issues is not None:
            IssuesDialog(issues, self).exec()

    def set_status(self, status):
        if status == "Approved":
            issues = self.db.completeness_issues(self.rid)
            if issues:
                IssuesDialog(issues, self, "Cannot approve — resolve these items first").exec()
                return
            if QMessageBox.question(self, "Approve", "Approve and lock this report? It becomes read-only on every "
                                    "workstation until reopened.") != QMessageBox.Yes:
                return
        elif status == "In Review":
            issues = self.db.completeness_issues(self.rid)
            if issues and QMessageBox.question(
                    self, "Submit for review", f"{len(issues)} open item(s) remain (see Completeness Check).\n"
                    "Submit anyway?") != QMessageBox.Yes:
                return
        elif status == "Draft":
            reason, ok = QInputDialog.getText(self, "Reopen report", "Reason for reopening (recorded in history):")
            if not ok or not reason.strip():
                return
            def reopen():
                self.db.set_status(self.rid, "Draft")
                note = f"Reopened by {self.db.user}: {reason.strip()}"
                return self.db.update_fields("reports", self.rid, {
                    "comments": ((self.rep["comments"] or "") + "\n" + note).strip()})
            if guard(self, reopen):
                self.reload_all()
            return
        new = guard(self, lambda: self.db.approve(self.rid) if status == "Approved" else
                    self.db.set_status(self.rid, status))
        if new:
            self.rep = new
            self.reload_all()
