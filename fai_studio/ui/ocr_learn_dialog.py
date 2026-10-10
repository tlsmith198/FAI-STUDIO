"""Settings -> OCR Learning: what the reader has learned from the team's corrections to OCR reads."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox,
                               QTableWidget, QTableWidgetItem, QVBoxLayout)

from .. import ocr_learn as ol
from .common import FlowRow, fit_to_screen, guard

STATUS_COLOR = {"active": "#146C2E", "learning": "#8A5A00", "off": "#6b6f72"}


def _fmt_time(t) -> str:
    try:
        return t.astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(t or "")[:16]


class FixEditDialog(QDialog):
    def __init__(self, parent, row: dict | None = None):
        super().__init__(parent)
        row = row or {}
        self.setWindowTitle("Learned OCR fix" if row else "Add an OCR fix")
        self.kind = QComboBox()
        for k, label in ol.KINDS.items():
            self.kind.addItem(label, k)
        self.kind.setCurrentIndex(max(0, self.kind.findData(row.get("kind") or "token")))
        self.wrong = QLineEdit(row.get("wrong", ""))
        self.right = QLineEdit(row.get("right", ""))
        self.status = QComboBox()
        for k, label in ol.STATUSES.items():
            self.status.addItem(label, k)
        self.status.setCurrentIndex(max(0, self.status.findData(row.get("status") or "active")))
        self.notes = QLineEdit(row.get("notes", ""))
        f = QFormLayout()
        f.addRow("Applies to", self.kind)
        f.addRow("OCR reads", self.wrong)
        f.addRow("Correct to", self.right)
        f.addRow("Status", self.status)
        f.addRow("Notes", self.notes)
        help_ = QLabel("<i>Word in a callout</i> replaces that exact word wherever it appears in an OCR-read callout "
                       "(e.g. <b>0.25O</b> → <b>.250</b>). <i>Whole callout</i> only replaces a callout read exactly "
                       "like this.")
        help_.setWordWrap(True)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(f)
        lay.addWidget(help_)
        lay.addWidget(bb)
        self.resize(520, 260)

    def _ok(self):
        if not self.wrong.text().strip() or not self.right.text().strip():
            QMessageBox.information(self, "OCR fix", "Fill in what OCR reads and what it should be.")
            return
        if ol.norm(self.wrong.text()) == ol.norm(self.right.text()):
            QMessageBox.information(self, "OCR fix", "The correction is the same as the read.")
            return
        self.accept()

    def values(self) -> dict:
        return {"kind": self.kind.currentData(), "wrong": self.wrong.text(), "right": self.right.text(),
                "status": self.status.currentData(), "notes": self.notes.text().strip()}


class OcrLearningDialog(QDialog):
    COLS = ["Status", "Applies to", "OCR reads", "Corrected to", "Times corrected", "Auto-applied", "Kept",
            "Undone", "Learned by", "Updated", "Notes"]

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.readonly = not hasattr(db, "save_ocr_learned") or type(db).__name__ == "LocalStore"
        self.setWindowTitle("OCR Learning — fixes learned from the team's corrections")
        fit_to_screen(self, 1180, 680)
        on, after = db.ocr_learning_settings() if hasattr(db, "ocr_learning_settings") else (True, 2)
        intro = QLabel(
            "Whenever someone corrects a callout that was read by OCR (in the Auto-Balloon review list or in the "
            "Form 3 table), the reader remembers <b>what it read</b> and <b>what it should have been</b>. Once the "
            "same correction has been made on enough characteristics the fix is <b>Active</b> and is applied to new "
            "scans on every workstation, marked <i>learned OCR fix</i> so it is still verified. If people keep "
            "undoing a fix, it switches itself off. Changes to digits (.250 → .260) and edits that add or remove words "
            "are not learned: those depend on one picture or are real changes, not misreads.")
        intro.setWordWrap(True)
        self.chk_on = QCheckBox("Learn from corrections to OCR reads")
        self.chk_on.setChecked(on)
        self.spin_after = QSpinBox()
        self.spin_after.setRange(1, 20)
        self.spin_after.setValue(after)
        self.spin_after.setSuffix(" characteristic(s)")
        settings = QHBoxLayout()
        settings.addWidget(self.chk_on)
        settings.addSpacing(24)
        settings.addWidget(QLabel("Apply a fix automatically once it has been corrected the same way on"))
        settings.addWidget(self.spin_after)
        settings.addStretch(1)
        self.filter = QComboBox()
        self.filter.addItem("All", "")
        for k, label in ol.STATUSES.items():
            self.filter.addItem(label, k)
        self.filter.currentIndexChanged.connect(self.reload)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search…")
        self.search.textChanged.connect(self._filter_rows)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(len(self.COLS) - 1, QHeaderView.Stretch)
        self.table.doubleClicked.connect(lambda _i: self.edit())
        btns = FlowRow()
        self.b_act = QPushButton("✔ Activate now")
        self.b_act.clicked.connect(lambda: self._set_status("active"))
        self.b_off = QPushButton("⏸ Turn off")
        self.b_off.clicked.connect(lambda: self._set_status("off"))
        self.b_edit = QPushButton("✎ Edit…")
        self.b_edit.clicked.connect(self.edit)
        self.b_add = QPushButton("➕ Add fix…")
        self.b_add.clicked.connect(self.add)
        self.b_del = QPushButton("🗑 Delete")
        self.b_del.clicked.connect(self.delete)
        self.b_rule = QPushButton("📚 Make Reader Rule…")
        self.b_rule.setToolTip("Turn this fix into a Drawing Reader Rule (text fix on OCR text), e.g. to scope it "
                               "to one customer's drawings")
        self.b_rule.clicked.connect(self.make_rule)
        for b in (self.b_act, self.b_off, self.b_edit, self.b_add, self.b_del, self.b_rule):
            btns.addWidget(b)
            b.setEnabled(not self.readonly)
        self.chk_on.setEnabled(not self.readonly)
        self.spin_after.setEnabled(not self.readonly)
        top = QHBoxLayout()
        top.addWidget(QLabel("Show:"))
        top.addWidget(self.filter)
        top.addWidget(self.search, 1)
        self.stats = QLabel()
        top.addWidget(self.stats)
        # try it
        self.try_in = QLineEdit()
        self.try_in.setPlaceholderText("Try an OCR read, e.g. 0.25O +.OO5")
        self.try_out = QLabel()
        self.try_out.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.try_in.textChanged.connect(self._try)
        tr = QHBoxLayout()
        tr.addWidget(QLabel("Test:"))
        tr.addWidget(self.try_in, 1)
        tr.addWidget(self.try_out, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        lay = QVBoxLayout(self)
        lay.addWidget(intro)
        if self.readonly:
            ro = QLabel("<b>Offline:</b> the learned fixes are applied, but they can only be changed while "
                        "connected to the server.")
            ro.setStyleSheet("color:#8A5A00;")
            lay.addWidget(ro)
        lay.addLayout(settings)
        lay.addLayout(top)
        lay.addWidget(self.table, 1)
        lay.addWidget(btns)
        lay.addLayout(tr)
        lay.addWidget(bb)
        self.rows: list[dict] = []
        self.reload()

    # ---------------------------------------------------------------- data
    def reload(self):
        rows = guard(self, lambda: self.db.ocr_learned(self.filter.currentData() or None))
        self.rows = rows or []
        t = self.table
        t.setRowCount(len(self.rows))
        for i, r in enumerate(self.rows):
            vals = [ol.STATUSES.get(r["status"], r["status"]), ol.KINDS.get(r["kind"], r["kind"]), r["wrong"],
                    r["right"], r.get("hits", 0), r.get("applied", 0), r.get("confirmed", 0), r.get("rejected", 0),
                    r.get("created_by", ""), _fmt_time(r.get("updated_at")), r.get("notes", "")]
            for j, v in enumerate(vals):
                it = QTableWidgetItem(str(v if v is not None else ""))
                if j == 0:
                    from PySide6.QtGui import QColor
                    it.setForeground(QColor(STATUS_COLOR.get(r["status"], "#000000")))
                    f = it.font()
                    f.setBold(True)
                    it.setFont(f)
                if j in (4, 5, 6, 7):
                    it.setTextAlignment(Qt.AlignCenter)
                t.setItem(i, j, it)
        t.resizeColumnsToContents()
        for j in (2, 3):
            t.setColumnWidth(j, max(160, min(320, t.columnWidth(j))))
        n_act = sum(1 for r in self.rows if r["status"] == "active")
        self.stats.setText(f"{len(self.rows)} shown · {n_act} active")
        self._filter_rows()
        self._try()

    def _filter_rows(self):
        q = self.search.text().strip().lower()
        for i, r in enumerate(self.rows):
            hit = not q or q in f"{r['wrong']} {r['right']} {r.get('notes', '')} {r.get('created_by', '')}".lower()
            self.table.setRowHidden(i, not hit)

    def _try(self):
        text = self.try_in.text()
        if not text.strip():
            self.try_out.setText("")
            return
        try:
            mem = self.db.ocr_memory()
        except Exception:
            mem = ol.Memory([])
        fixed, ids = mem.apply(text)
        self.try_out.setText(f"→ <b>{fixed}</b>  ({len(ids)} fix(es) used)" if ids else "→ no learned fix applies")

    def _current(self) -> dict | None:
        i = self.table.currentRow()
        return self.rows[i] if 0 <= i < len(self.rows) else None

    def _save_settings(self):
        if self.readonly:
            return
        on, after = self.db.ocr_learning_settings()
        if on != self.chk_on.isChecked() or after != self.spin_after.value():
            guard(self, lambda: self.db.set_meta({"ocr_learning": "1" if self.chk_on.isChecked() else "0",
                                                  "ocr_learn_after": str(self.spin_after.value())}))

    def done(self, r):
        self._save_settings()
        super().done(r)

    # ---------------------------------------------------------------- actions
    def _set_status(self, status):
        r = self._current()
        if r and guard(self, lambda: self.db.save_ocr_learned(r["id"], {"status": status})):
            self.reload()

    def edit(self):
        r = self._current()
        if not r or self.readonly:
            return
        d = FixEditDialog(self, r)
        if d.exec() == QDialog.Accepted and guard(self, lambda: self.db.save_ocr_learned(r["id"], d.values())):
            self.reload()

    def add(self):
        d = FixEditDialog(self)
        if d.exec() == QDialog.Accepted and guard(self, lambda: self.db.save_ocr_learned(None, d.values())):
            self.reload()

    def delete(self):
        r = self._current()
        if not r:
            return
        if QMessageBox.question(self, "Delete learned fix", f"Delete the learned fix\n\n{r['wrong']}  →  "
                                f"{r['right']}?\n\nIt will be learned again if people keep making this "
                                "correction. Use Turn off to stop it for good.") != QMessageBox.Yes:
            return
        guard(self, lambda: self.db.delete_ocr_learned(r["id"]))
        self.reload()

    def make_rule(self):
        r = self._current()
        if not r:
            return
        import re
        find = r["wrong"] if r["kind"] == "token" else r["wrong"]
        rule = {"name": f"OCR fix: {r['wrong']} → {r['right']}", "rule_type": "replace",
                "params": {"find": (r"(?<!\S)" + re.escape(find) + r"(?!\S)"), "replace": r["right"],
                           "regex": True, "ocr_only": True},
                "notes": f"Made from a learned OCR fix (corrected {r.get('hits', 0)}x)."}
        new = guard(self, lambda: self.db.save_rule(rule, log_action="ADD"))
        if new:
            guard(self, lambda: self.db.save_ocr_learned(r["id"], {"status": "off", "notes": (
                (r.get("notes") or "") + " Moved to Reader Rule '" + rule["name"] + "'.").strip()}))
            try:
                from .. import rules as R
                R.load_shared(self.db)
            except Exception:
                pass
            QMessageBox.information(self, "Reader Rule", f"Reader Rule '{rule['name']}' created (OCR text only). "
                                    "Open Drawing Reader Rules to scope it to a customer or add example drawings.\n\n"
                                    "The learned fix is turned off so it isn't applied twice.")
            self.reload()
