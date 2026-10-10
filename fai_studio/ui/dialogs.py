from __future__ import annotations

from datetime import datetime
from pathlib import Path

import psycopg
from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QRadioButton,
                               QScrollArea, QSpinBox,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..config import LocalConfig
from ..importers import apply_mapping, detect_mapping
from .common import fit_to_screen, multiline_delegate


# ---------------------------------------------------------------------------- connection
class ConnectionDialog(QDialog):
    def __init__(self, cfg: LocalConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Database Connection")
        import dataclasses
        self.orig = cfg
        self.cfg = dataclasses.replace(cfg)       # work on a copy; applied only on Save
        self.setMinimumWidth(460)
        f = QFormLayout()
        self.host = QLineEdit(cfg.host)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(int(cfg.port))
        self.db = QLineEdit(cfg.dbname)
        self.user = QLineEdit(cfg.user)
        self.pw = QLineEdit(cfg.password)
        self.pw.setEchoMode(QLineEdit.Password)
        self.ssl = QComboBox()
        self.ssl.addItems(["prefer", "require", "disable", "verify-ca", "verify-full"])
        self.ssl.setCurrentText(cfg.sslmode)
        self.name = QLineEdit(cfg.user_display_name)
        self.name.setToolTip("Name recorded on reports and in the audit trail for this workstation")
        f.addRow("Server host / IP", self.host)
        f.addRow("Port", self.port)
        f.addRow("Database", self.db)
        f.addRow("DB user", self.user)
        f.addRow("DB password", self.pw)
        f.addRow("SSL mode", self.ssl)
        f.addRow(QLabel(""))
        f.addRow("Your name (inspector)", self.name)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        test = QPushButton("Test Connection")
        test.clicked.connect(self.test)
        init = QPushButton("Create Database && Tables")
        init.setToolTip("Creates the database (if missing) and the FAI Studio tables. Safe to run again.")
        init.clicked.connect(self.init_db)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(test)
        row.addWidget(init)
        row.addStretch(1)
        lay = QVBoxLayout(self)
        intro = QLabel("All workstations point at the same PostgreSQL server. Changes made on one machine "
                       "appear live on every other machine.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        lay.addLayout(f)
        lay.addLayout(row)
        lay.addWidget(self.status)
        lay.addWidget(bb)

    def _apply(self):
        c = self.cfg
        c.host, c.port, c.dbname = self.host.text().strip(), self.port.value(), self.db.text().strip()
        c.user, c.password, c.sslmode = self.user.text().strip(), self.pw.text(), self.ssl.currentText()
        c.user_display_name = self.name.text().strip() or c.user_display_name

    def test(self) -> bool:
        self._apply()
        try:
            with psycopg.connect(**self.cfg.conninfo()) as conn:
                v = conn.execute("SHOW server_version").fetchone()[0]
                has = conn.execute("SELECT to_regclass('public.reports')").fetchone()[0]
            self.status.setText(f"<span style='color:#146c2e'><b>Connected.</b> PostgreSQL {v}. "
                                f"{'FAI tables found.' if has else 'Tables not created yet — click Create Database & Tables.'}</span>")
            return True
        except Exception as e:  # noqa: BLE001
            hint = ""
            if "does not exist" in str(e) and "database" in str(e):
                hint = "<br>The server is reachable but the database doesn't exist yet — click <b>Create Database & Tables</b>."
            elif "password authentication failed" in str(e):
                hint = "<br>Check the DB user / password (the password set when PostgreSQL was installed)."
            elif "Connection refused" in str(e) or "timeout" in str(e).lower():
                hint = "<br>PostgreSQL isn't reachable at that host/port — is the service running and port 5432 open?"
            self.status.setText(f"<span style='color:#b42318'><b>Connection failed:</b> {e}{hint}</span>")
            return False

    def _create_database_if_missing(self):
        """Connect to the server's 'postgres' DB and CREATE DATABASE if the named one doesn't exist."""
        from psycopg import sql
        info = dict(self.cfg.conninfo(), dbname="postgres")
        with psycopg.connect(**info, autocommit=True) as conn:
            if conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (self.cfg.dbname,)).fetchone():
                return False
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(self.cfg.dbname)))
            return True

    def init_db(self):
        self._apply()
        try:
            if self._create_database_if_missing():
                self.status.setText(f"<span style='color:#146c2e'>Database '{self.cfg.dbname}' created.</span>")
        except Exception as e:  # noqa: BLE001
            self.status.setText(f"<span style='color:#b42318'><b>Could not create database:</b> {e}</span>")
            return
        try:
            from ..db import Database
            d = Database(self.cfg)
            d.init_schema()
            d.close()
            self.status.setText("<span style='color:#146c2e'><b>Tables created / upgraded.</b></span>")
        except Exception as e:  # noqa: BLE001
            self.status.setText(f"<span style='color:#b42318'><b>Failed:</b> {e}</span>")

    def save(self):
        self._apply()
        if not self.test():
            if QMessageBox.question(self, "Save anyway?", "The connection test failed. Save these settings anyway?") \
                    != QMessageBox.Yes:
                return
        import dataclasses
        for f in dataclasses.fields(self.cfg):
            setattr(self.orig, f.name, getattr(self.cfg, f.name))
        self.orig.save()
        self.accept()


# ---------------------------------------------------------------------------- company
class CompanyDialog(QDialog):
    FIELDS = [("company_name", "Company name"), ("company_short", "Short name / logo text"),
              ("cage_code", "CAGE code (default supplier code)"), ("company_address", "Address"),
              ("company_phone", "Phone"), ("quality_statement", "Quality statement (report header)")]

    def __init__(self, db, parent=None):
        super().__init__(parent)
        from ..branding import ICON_KEY, LOGO_KEY
        self.db = db
        self.setWindowTitle("Company Settings (shared by all workstations)")
        fit_to_screen(self, 900, 860)
        meta = db.get_meta()
        f = QFormLayout()
        self.edits = {}
        for k, label in self.FIELDS:
            e = QLineEdit(meta.get(k, ""))
            self.edits[k] = e
            f.addRow(label, e)
        # branding: logo on the reports, icon for the application
        self.images = {LOGO_KEY: meta.get(LOGO_KEY, ""), ICON_KEY: meta.get(ICON_KEY, "")}
        self.previews = {}
        brand = QGroupBox("Branding")
        g = QGridLayout(brand)
        rows = [(LOGO_KEY, "Company logo",
                 "Printed on the AS9102 FAI (ADMI layout), Standard Report and Inspection Report – never on "
                 "the AS9102 FAI – Clean forms. A PNG with a transparent background works best.",
                 (1200, 400, False), QSize(260, 80)),
                (ICON_KEY, "Application icon",
                 "Window, taskbar and desktop-shortcut icon on every workstation (takes effect on each PC "
                 "the next time it starts). Square PNG or .ico, 256 × 256 px or larger.", (256, 256, True), QSize(64, 64))]
        for i, (key, title, tip, size, box) in enumerate(rows):
            lab = QLabel()
            lab.setFixedSize(box.width() + 8, box.height() + 8)
            lab.setAlignment(Qt.AlignCenter)
            lab.setStyleSheet("background:white; border:1px solid #C4C8CC")
            self.previews[key] = (lab, box)
            pick = QPushButton("Choose…")
            pick.clicked.connect(lambda _=False, k=key, sz=size: self._pick(k, sz))
            clear = QPushButton("Remove")
            clear.clicked.connect(lambda _=False, k=key: self._set(k, ""))
            hint = QLabel(tip)
            hint.setStyleSheet("color:#4a4f52")
            hint.setWordWrap(True)
            hint.setMinimumHeight(44)
            g.addWidget(QLabel(f"<b>{title}</b>"), i * 2, 0)
            g.addWidget(lab, i * 2, 1, 2, 1)
            btns = QHBoxLayout()
            btns.addWidget(pick)
            btns.addWidget(clear)
            btns.addStretch(1)
            g.addLayout(btns, i * 2, 2)
            g.addWidget(hint, i * 2 + 1, 2)
            self._show(key)
        g.setColumnStretch(2, 1)
        # Certificate of Conformance wording
        cocg = QGroupBox("Certificate of Conformance")
        cf = QFormLayout(cocg)
        self.coc_texts = {}
        for key, label, h in (("coc_statement", "Certification statement", 70),
                              ("coc_footer", "Footer (every page)", 70)):
            e = QPlainTextEdit(meta.get(key, ""))
            e.setFixedHeight(h)
            self.coc_texts[key] = e
            cf.addRow(label, e)
        self.edits["coc_default_title"] = QLineEdit(meta.get("coc_default_title", ""))
        self.edits["coc_default_title"].setPlaceholderText("e.g. QC / Quality Manager")
        cf.addRow("Default signer title", self.edits["coc_default_title"])
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.addLayout(f)
        bl.addWidget(brand)
        bl.addWidget(cocg)
        bl.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(body)
        lay = QVBoxLayout(self)
        lay.addWidget(scroll, 1)
        lay.addWidget(bb)

    def _pick(self, key, size):
        from ..branding import image_file_to_b64
        fn, _ = QFileDialog.getOpenFileName(self, "Choose image", "",
                                            "Images (*.png *.jpg *.jpeg *.bmp *.gif *.ico *.tif *.tiff *.webp)")
        if not fn:
            return
        try:
            self._set(key, image_file_to_b64(fn, size[0], size[1], square=size[2]))
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Image", f"That file could not be read as an image:\n{e}")

    def _set(self, key, b64):
        self.images[key] = b64
        self._show(key)

    def _show(self, key):
        from PySide6.QtGui import QPixmap

        from ..branding import b64_to_bytes
        lab, box = self.previews[key]
        data = b64_to_bytes(self.images.get(key))
        pm = QPixmap()
        if data and pm.loadFromData(data):
            lab.setPixmap(pm.scaled(box, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            lab.clear()
            lab.setText("(none)")

    def save(self):
        from ..branding import apply_icon
        values = {k: e.text().strip() for k, e in self.edits.items()}
        values.update({k: e.toPlainText().strip() for k, e in self.coc_texts.items()})
        values.update(self.images)
        self.db.set_meta(values)
        apply_icon(self.db.get_meta(), self.parent())
        self.accept()


# ---------------------------------------------------------------------------- gages
class GageDialog(QDialog):
    COLS = [("gage_id", "Gage ID"), ("description", "Description"), ("gage_type", "Type"),
            ("cal_due", "Cal Due (YYYY-MM-DD)"), ("active", "Active")]

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("Gage / Tooling Library")
        fit_to_screen(self, 820, 520)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels([c[1] for c in self.COLS])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.itemChanged.connect(self._changed)
        add = QPushButton("Add Gage")
        add.clicked.connect(self.add)
        rm = QPushButton("Delete Selected")
        rm.clicked.connect(self.remove)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        for b in (add, rm):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(close)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Gage IDs typed in the Tooling column of Form 3 are checked against this list; "
                             "expired calibration is flagged by the completeness check."))
        lay.addWidget(self.table)
        lay.addLayout(row)
        self.load()

    def load(self):
        self.table.blockSignals(True)
        rows = self.db.gages()
        self.table.setRowCount(len(rows))
        today = datetime.now().strftime("%Y-%m-%d")
        for r, g in enumerate(rows):
            for c, (k, _) in enumerate(self.COLS):
                it = QTableWidgetItem()
                if k == "active":
                    it.setCheckState(Qt.Checked if g[k] else Qt.Unchecked)
                    it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable)
                else:
                    it.setText(str(g[k] or ""))
                if k == "cal_due" and g[k] and g[k] < today:
                    it.setForeground(Qt.red)
                it.setData(Qt.UserRole, (g["id"], k))
                self.table.setItem(r, c, it)
        self.table.blockSignals(False)

    def _changed(self, it: QTableWidgetItem):
        gid, k = it.data(Qt.UserRole)
        v = it.checkState() == Qt.Checked if k == "active" else it.text().strip()
        try:
            self.db.update_field("gages", gid, k, v)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Save failed", str(e))

    def add(self):
        self.db.insert("gages", None, {"gage_id": "NEW", "description": ""})
        self.load()

    def remove(self):
        ids = {self.table.item(i.row(), 0).data(Qt.UserRole)[0] for i in self.table.selectedIndexes()}
        if ids and QMessageBox.question(self, "Delete", f"Delete {len(ids)} gage(s)?") == QMessageBox.Yes:
            for i in ids:
                self.db.delete("gages", i)
            self.load()


class VendorDialog(QDialog):
    """Shared approved-vendor list used on the Certificate of Conformance (name + address block + CAGE)."""
    COLS = [("name", "Vendor Name"), ("address", "Address / Info (printed under the name)"), ("cage", "CAGE"),
            ("vendor_type", "Type"), ("approved", "Approved"), ("notes", "Notes"), ("active", "Active")]
    TYPES = ["", "Material", "Process", "Test", "Purchased", "Distributor"]

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("Vendor Library (Certificate of Conformance)")
        fit_to_screen(self, 1000, 560)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels([c[1] for c in self.COLS])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        for c, w in ((0, 220), (2, 70), (3, 100), (4, 70), (5, 160), (6, 55)):
            self.table.setColumnWidth(c, w)
        self.table.setWordWrap(True)
        self.table.setItemDelegateForColumn(1, multiline_delegate(self.table))
        self.table.setItemDelegateForColumn(5, multiline_delegate(self.table))
        self.table.itemChanged.connect(self._changed)
        add = QPushButton("Add Vendor")
        add.clicked.connect(self.add)
        rm = QPushButton("Delete Selected")
        rm.clicked.connect(self.remove)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        for b in (add, rm):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(close)
        lay = QVBoxLayout(self)
        info = QLabel("Vendors here are offered in the CoC <i>Vendor</i> column and matched automatically by CAGE "
                      "code, McMaster-Carr part numbers or Form 2 supplier name. Double-click an address to edit it "
                      "(Enter = new line, click away to save).")
        info.setWordWrap(True)
        lay.addWidget(info)
        lay.addWidget(self.table)
        lay.addLayout(row)
        if getattr(db, "is_local", False):
            for b in (add, rm):
                b.setEnabled(False)
            self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            lay.insertWidget(1, QLabel("<i>Offline: the vendor list is read-only here - edit it when online.</i>"))
        self.load()

    def load(self):
        self.table.blockSignals(True)
        rows = self.db.vendors(active_only=False)
        self.table.setRowCount(len(rows))
        for r, v in enumerate(rows):
            for c, (k, _) in enumerate(self.COLS):
                it = QTableWidgetItem()
                if k in ("active", "approved"):
                    it.setCheckState(Qt.Checked if v[k] else Qt.Unchecked)
                    it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable)
                else:
                    it.setText(str(v[k] or ""))
                it.setData(Qt.UserRole, (v["id"], k))
                self.table.setItem(r, c, it)
        self.table.resizeRowsToContents()
        self.table.blockSignals(False)

    def _changed(self, it: QTableWidgetItem):
        vid, k = it.data(Qt.UserRole)
        v = it.checkState() == Qt.Checked if k in ("active", "approved") else it.text().strip()
        try:
            self.db.update_field("vendors", vid, k, v)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Save failed", str(e))
        self.table.resizeRowToContents(it.row())

    def add(self):
        name, ok = QInputDialog.getText(self, "Add vendor", "Vendor name:")
        if ok and name.strip():
            try:
                self.db.insert("vendors", None, {"name": name.strip(), "address": ""})
            except Exception as e:  # noqa: BLE001
                QMessageBox.warning(self, "Add failed", str(e))
            self.load()

    def remove(self):
        ids = {self.table.item(i.row(), 0).data(Qt.UserRole)[0] for i in self.table.selectedIndexes()}
        if ids and QMessageBox.question(self, "Delete", f"Delete {len(ids)} vendor(s)?") == QMessageBox.Yes:
            for i in ids:
                self.db.delete("vendors", i)
            self.load()


# ---------------------------------------------------------------------------- new report wizard
class FilePick(QWidget):
    def __init__(self, caption, filt, multi=False, parent=None):
        super().__init__(parent)
        self.caption, self.filt, self.multi = caption, filt, multi
        self.paths: list[str] = []
        self.edit = QLineEdit()
        self.edit.setReadOnly(True)
        self.edit.setPlaceholderText("(optional)")
        b = QPushButton("Browse…")
        b.clicked.connect(self.pick)
        c = QPushButton("✕")
        c.setFixedWidth(28)
        c.clicked.connect(self.clear)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.edit, 1)
        lay.addWidget(b)
        lay.addWidget(c)

    def pick(self):
        if self.multi:
            paths, _ = QFileDialog.getOpenFileNames(self, self.caption, "", self.filt)
        else:
            p, _ = QFileDialog.getOpenFileName(self, self.caption, "", self.filt)
            paths = [p] if p else []
        if paths:
            self.paths = paths
            self.edit.setText("; ".join(Path(p).name for p in paths))

    def clear(self):
        self.paths = []
        self.edit.clear()


DRAWING_FILTER = "Drawings / packages (*.pdf *.zip *.tif *.tiff *.jpg *.jpeg *.png *.bmp);;All files (*)"
PHOTO_FILTER = "Images (*.jpg *.jpeg *.png *.tif *.tiff *.bmp *.gif *.webp);;All files (*)"
TABLE_FILTER = "Parts lists (*.xlsx *.xlsm *.csv *.tsv *.txt *.docx *.pdf);;All files (*)"
NOTES_FILTER = "Notes (*.txt *.docx *.pdf *.xlsx *.csv);;All files (*)"


class NewReportDialog(QDialog):
    def __init__(self, parent=None, defaults: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("New Inspection Report / FAI")
        self.setMinimumWidth(640)
        d = defaults or {}
        from ..report_types import DESCRIPTIONS, REPORT_TYPES
        self.type = QComboBox()
        for t in REPORT_TYPES:
            self.type.addItem(t)
            self.type.setItemData(self.type.count() - 1, DESCRIPTIONS[t], Qt.ToolTipRole)
        self.fields = {}
        g = QGridLayout()
        g.addWidget(QLabel("Report type"), 0, 0)
        g.addWidget(self.type, 0, 1)
        specs = [("part_number", "Part number"), ("part_name", "Part name"), ("part_rev", "Part revision"),
                 ("drawing_number", "Drawing number"), ("drawing_rev", "Drawing revision"),
                 ("serial_number", "Serial / lot number"), ("customer", "Customer"), ("po_number", "PO number"),
                 ("job_number", "Job / work order"), ("quantity", "Quantity")]
        for i, (k, label) in enumerate(specs):
            e = QLineEdit(d.get(k, ""))
            self.fields[k] = e
            r, c = 1 + i // 2, (i % 2) * 2
            g.addWidget(QLabel(label), r, c)
            g.addWidget(e, r, c + 1)
        self.detail = QComboBox()
        self.detail.addItems(["Detail", "Assembly"])
        self.full = QComboBox()
        self.full.addItems(["Full", "Partial"])
        self.units = QComboBox()
        self.units.addItems(["in", "mm"])
        r = 1 + (len(specs) + 1) // 2
        g.addWidget(QLabel("Detail / Assembly"), r, 0)
        g.addWidget(self.detail, r, 1)
        g.addWidget(QLabel("Full / Partial FAI"), r, 2)
        g.addWidget(self.full, r, 3)
        g.addWidget(QLabel("Units"), r + 1, 0)
        g.addWidget(self.units, r + 1, 1)
        self.fields["part_number"].textChanged.connect(
            lambda t: self.fields["drawing_number"].setPlaceholderText(t))
        self.fields["part_number"].setPlaceholderText("optional — pick it from the drawing")

        files = QGroupBox("Source files — build the report automatically (all optional)")
        ff = QFormLayout(files)
        self.f_drawing = FilePick("Select drawing(s)", DRAWING_FILTER, True)
        self.f_photos = FilePick("Select photos", PHOTO_FILTER, True)
        self.f_parts = FilePick("Select parts list", TABLE_FILTER)
        self.f_notes = FilePick("Select notes list", NOTES_FILTER)
        self.auto_balloon = QCheckBox("Ask which part number to balloon, then auto-balloon that part "
                                      "(its views, notes, material; review before adding)")
        self.auto_balloon.setChecked(True)
        self.notes_from_drawing = QCheckBox("If no notes list is given, read NOTES from the drawing PDF")
        self.notes_from_drawing.setChecked(True)
        ff.addRow("Drawing(s) PDF/TIFF/ZIP package", self.f_drawing)
        ff.addRow("Photos", self.f_photos)
        ff.addRow("Parts list", self.f_parts)
        ff.addRow("Notes list", self.f_notes)
        ff.addRow(self.auto_balloon)
        ff.addRow(self.notes_from_drawing)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Create Report")
        bb.button(QDialogButtonBox.Ok).setObjectName("primary")
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(g)
        lay.addWidget(files)
        lay.addWidget(bb)

    def _ok(self):
        if not self.fields["part_number"].text().strip() and not self.f_drawing.paths:
            QMessageBox.warning(self, "Required", "Enter a part number, or select the drawing and pick the part "
                                "number from it.")
            return
        self.accept()

    def values(self) -> dict:
        v = {k: e.text().strip() for k, e in self.fields.items()}
        if not v["drawing_number"]:
            v["drawing_number"] = v["part_number"]
        if not v["drawing_rev"]:
            v["drawing_rev"] = v["part_rev"]
        v.update(report_type=self.type.currentText(), detail_or_assembly=self.detail.currentText(),
                 fai_type=self.full.currentText(), units=self.units.currentText())
        if v["units"] == "mm":
            v.update(tol_1dec="0.3", tol_2dec="0.1", tol_3dec="0.05", tol_4dec="0.005", tol_angle="0.5")
        return v


class DuplicateDialog(QDialog):
    def __init__(self, rep: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Revision / Copy Report")
        f = QFormLayout(self)
        self.rev = QLineEdit(rep["part_rev"])
        self.drev = QLineEdit(rep["drawing_rev"])
        self.serial = QLineEdit("")
        self.po = QLineEdit(rep["po_number"])
        from ..report_types import REPORT_TYPES
        self.typ = QComboBox()
        self.typ.addItems(REPORT_TYPES)
        self.typ.setCurrentText(rep["report_type"])
        self.partial = QCheckBox("Make this a Partial FAI (delta) against the source report")
        self.results = QCheckBox("Copy measured results")
        f.addRow(QLabel(f"Copying <b>{rep['report_number']}</b> — {rep['part_number']} Rev {rep['part_rev']}. "
                        "Characteristics, balloons, materials, parts list and attachments are copied."))
        f.addRow("Report type", self.typ)
        f.addRow("New part revision", self.rev)
        f.addRow("New drawing revision", self.drev)
        f.addRow("Serial number", self.serial)
        f.addRow("PO number", self.po)
        f.addRow(self.partial)
        f.addRow(self.results)
        self.rep = rep
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def values(self):
        v = dict(part_rev=self.rev.text().strip(), drawing_rev=self.drev.text().strip(),
                 serial_number=self.serial.text().strip(), po_number=self.po.text().strip(),
                 report_type=self.typ.currentText())
        if self.partial.isChecked():
            v.update(fai_type="Partial",
                     partial_baseline=f"{self.rep['part_number']} Rev {self.rep['part_rev']} ({self.rep['report_number']})")
        return v


# ---------------------------------------------------------------------------- column mapping
class MappingDialog(QDialog):
    """Preview a table file and map its columns to target fields."""

    def __init__(self, title: str, rows: list[list[str]], targets: dict[str, dict], parent=None,
                 labels: dict[str, str] | None = None):
        """targets: {'Form 1 – Index of parts': FIELDS, ...}"""
        super().__init__(parent)
        self.setWindowTitle(title)
        fit_to_screen(self, 980, 620)
        self.rows = rows
        self.targets = targets
        self.labels = labels or {}
        self.target_combo = QComboBox()
        self.target_combo.addItems(list(targets.keys()))
        self.target_combo.currentIndexChanged.connect(self._auto)
        self.header_spin = QSpinBox()
        self.header_spin.setRange(0, max(0, len(rows) - 1))
        self.header_spin.valueChanged.connect(self._rebuild_combos)
        self.map_box = QGroupBox("Column mapping")
        self.map_form = QFormLayout(self.map_box)
        self.combos: dict[str, QComboBox] = {}
        self.preview = QTableWidget()
        self.preview.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.count = QLabel("")
        top = QHBoxLayout()
        if len(targets) > 1:
            top.addWidget(QLabel("Import into:"))
            top.addWidget(self.target_combo)
        top.addWidget(QLabel("Header row (0-based):"))
        top.addWidget(self.header_spin)
        top.addStretch(1)
        top.addWidget(self.count)
        body = QHBoxLayout()
        body.addWidget(self.map_box, 0)
        body.addWidget(self.preview, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Import")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addLayout(body, 1)
        lay.addWidget(bb)
        self._fill_preview()
        self._auto()

    def _fill_preview(self):
        n = max((len(r) for r in self.rows), default=0)
        show = self.rows[:200]
        self.preview.setColumnCount(n)
        self.preview.setRowCount(len(show))
        self.preview.setHorizontalHeaderLabels([f"Col {i}" for i in range(n)])
        for r, row in enumerate(show):
            for c, v in enumerate(row):
                self.preview.setItem(r, c, QTableWidgetItem(v))
        self.preview.setVerticalHeaderLabels([str(i) for i in range(len(show))])
        self.preview.resizeColumnsToContents()

    def _auto(self):
        fields = self.targets[self.target_combo.currentText()]
        hi, mapping = detect_mapping(self.rows, fields)
        self._mapping = mapping
        self.header_spin.blockSignals(True)
        self.header_spin.setValue(max(hi, 0))
        self.header_spin.blockSignals(False)
        self._rebuild_combos()

    def _rebuild_combos(self):
        while self.map_form.rowCount():
            self.map_form.removeRow(0)
        self.combos.clear()
        fields = self.targets[self.target_combo.currentText()]
        hi = self.header_spin.value()
        header = self.rows[hi] if 0 <= hi < len(self.rows) else []
        n = max((len(r) for r in self.rows), default=0)
        for f in fields:
            cb = QComboBox()
            cb.addItem("(skip)", None)
            for i in range(n):
                h = header[i] if i < len(header) else ""
                cb.addItem(f"Col {i}: {h}"[:40], i)
            m = self._mapping.get(f)
            if m is not None:
                cb.setCurrentIndex(m + 1)
            cb.currentIndexChanged.connect(self._count)
            self.combos[f] = cb
            self.map_form.addRow(self.labels.get(f, f.replace("_", " ").title()), cb)
        self._count()

    def _count(self):
        self.count.setText(f"{len(self.records())} rows will be imported")

    def target(self) -> str:
        return self.target_combo.currentText()

    def records(self) -> list[dict]:
        mapping = {f: cb.currentData() for f, cb in self.combos.items() if cb.currentData() is not None}
        if not mapping:
            return []
        return apply_mapping(self.rows, self.header_spin.value(), mapping)


# ---------------------------------------------------------------------------- notes import
class NotesDialog(QDialog):
    def __init__(self, notes: list[dict], specs_by_note: dict[str, list[str]], next_char: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Import Notes List → Form 3 Characteristics")
        fit_to_screen(self, 900, 600)
        self.notes = notes
        self.table = QTableWidget(len(notes), 4)
        self.table.setHorizontalHeaderLabels(["Note", "Requirement text", "Specs found", "Applies?"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for r, n in enumerate(notes):
            it = QTableWidgetItem(n["label"])
            it.setCheckState(Qt.Checked if n.get("applies", True) else Qt.Unchecked)
            it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            self.table.setItem(r, 0, it)
            self.table.setItem(r, 1, QTableWidgetItem(n["text"]))
            s = QTableWidgetItem(", ".join(specs_by_note.get(n["label"], [])))
            s.setFlags(Qt.ItemIsEnabled)
            self.table.setItem(r, 2, s)
            w = QTableWidgetItem(n.get("why", ""))
            w.setFlags(Qt.ItemIsEnabled)
            if not n.get("applies", True):
                w.setForeground(Qt.darkGray)
            self.table.setItem(r, 3, w)
        self.table.resizeRowsToContents()
        self.mk_form2 = QCheckBox("Also add referenced specifications (AMS, MIL, ASTM, BAC, …) to Form 2")
        self.mk_form2.setChecked(True)
        self.skip_general = QCheckBox("Skip notes that are not inspectable (e.g. \"INTERPRET DRAWING PER ASME Y14.5\")"
                                      " — they still appear, marked N/A")
        self.skip_general.setChecked(False)
        allb = QPushButton("Select all")
        allb.clicked.connect(lambda: self._all(True))
        noneb = QPushButton("Select none")
        noneb.clicked.connect(lambda: self._all(False))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Import")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(allb)
        row.addWidget(noneb)
        row.addStretch(1)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"{len(notes)} notes found. Checked notes become characteristics numbered from "
                             f"{next_char}. Edit text before importing if needed."))
        lay.addWidget(self.table, 1)
        lay.addLayout(row)
        lay.addWidget(self.mk_form2)
        lay.addWidget(self.skip_general)
        lay.addWidget(bb)

    def _all(self, on):
        for r in range(self.table.rowCount()):
            self.table.item(r, 0).setCheckState(Qt.Checked if on else Qt.Unchecked)

    def selected(self) -> list[dict]:
        out = []
        for r in range(self.table.rowCount()):
            if self.table.item(r, 0).checkState() == Qt.Checked:
                out.append({"label": self.table.item(r, 0).text(), "text": self.table.item(r, 1).text().strip()})
        return out


# ---------------------------------------------------------------------------- auto-balloon review
class CandidateDialog(QDialog):
    """Review auto-balloon results before they become characteristics."""
    COLS = ["Add", "Requirement (editable)", "On drawing", "Type", "Sheet / Zone", "View", "Designator", "Note"]
    C_REQ, C_PIC, C_TYPE = 1, 2, 3
    RESTART = 2

    def __init__(self, cands, source: str, next_char: int, parent=None, title_extra: str = "",
                 thumb_fn=None):
        super().__init__(parent)
        self.setWindowTitle("Auto-Balloon — review detected characteristics")
        fit_to_screen(self, 1280, 760)
        self.cands = cands
        self.thumb_fn = thumb_fn
        self.list = QTableWidget(len(cands), len(self.COLS))
        self.list.setHorizontalHeaderLabels(self.COLS)
        self.list.verticalHeader().setVisible(False)
        n_next = max([next_char] + [(getattr(c, "char_no", None) or 0) + 1 for c in cands])
        for i, c in enumerate(cands):
            cno = getattr(c, "char_no", None)
            if not cno:
                cno, n_next = n_next, n_next + 1
            chk = QTableWidgetItem(str(cno))
            chk.setCheckState(Qt.Checked if getattr(c, "selected", True) else Qt.Unchecked)
            chk.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            self.list.setItem(i, 0, chk)
            self.list.setItem(i, self.C_REQ, QTableWidgetItem(c.requirement))
            pic = QTableWidgetItem("" if thumb_fn else "—")
            pic.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            pic.setToolTip("Double-click to enlarge")
            self.list.setItem(i, self.C_PIC, pic)
            for col, val in ((3, getattr(c, "char_type", "")), (4, getattr(c, "ref_location", "")),
                             (5, getattr(c, "view", "")), (6, getattr(c, "designator", "")),
                             (7, getattr(c, "comment", "") or ("OCR – verify" if getattr(c, "source", "") == "ocr" else ""))):
                it = QTableWidgetItem(str(val or ""))
                it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                if col == 6 and val:
                    it.setForeground(Qt.red)
                self.list.setItem(i, col, it)
        self.list.resizeColumnsToContents()
        self.list.setColumnWidth(self.C_REQ, 280)
        if thumb_fn:
            self.list.setColumnWidth(self.C_PIC, 310)
            self.list.verticalHeader().setDefaultSectionSize(52)
            self.list.setIconSize(QSize(300, 46))
        self.list.horizontalHeader().setSectionResizeMode(7, QHeaderView.Stretch)
        self.list.cellDoubleClicked.connect(self._enlarge)
        src = {"text": "PDF text layer", "ocr": "OCR (verify carefully)"}.get(source, source)
        n_ref = sum(1 for c in cands if getattr(c, "char_type", "") == "Reference")
        pic_tip = (" Check each value against the picture cut from the drawing next to it "
                   "(double-click a picture to enlarge)." if thumb_fn else "")
        info = QLabel(f"{title_extra}<b>{len(cands)}</b> callouts found ({src}). Numbered in drawing order. "
                      f"Reference dimensions ({n_ref}) are unchecked; BASIC dimensions are added as N/A. "
                      "Uncheck anything that is not an inspection characteristic. Balloons can be dragged "
                      "afterwards." + pic_tip)
        info.setWordWrap(True)
        allb = QPushButton("Select all")
        allb.clicked.connect(lambda: self._all(True))
        noneb = QPushButton("Select none")
        noneb.clicked.connect(lambda: self._all(False))
        nob = QPushButton("Uncheck BASIC")
        nob.clicked.connect(lambda: self._type_off("Basic"))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Add Balloons")
        bb.button(QDialogButtonBox.Ok).setObjectName("primary")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        restart = bb.addButton("↺ Start Over", QDialogButtonBox.ResetRole)
        restart.setToolTip("Discard this list without adding anything and run the ballooning again "
                           "(pick the part / sheet again)")
        restart.clicked.connect(lambda: self.done(self.RESTART))
        row = QHBoxLayout()
        for b in (allb, noneb, nob):
            row.addWidget(b)
        row.addStretch(1)
        lay = QVBoxLayout(self)
        lay.addWidget(info)
        lay.addWidget(self.list, 1)
        lay.addLayout(row)
        lay.addWidget(bb)
        # pictures are cut in small batches so the list shows immediately
        self._next_pic = 0
        if thumb_fn and cands:
            from PySide6.QtCore import QTimer
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._load_some)
            self._timer.start(0)

    def _load_some(self):
        end = min(self._next_pic + 8, len(self.cands))
        for i in range(self._next_pic, end):
            try:
                pm = self.thumb_fn(self.cands[i], False)
            except Exception:
                pm = None
            it = self.list.item(i, self.C_PIC)
            if pm is not None and not pm.isNull():
                it.setData(Qt.DecorationRole, pm)
            else:
                it.setText("(no picture)")
        self._next_pic = end
        if end >= len(self.cands):
            self._timer.stop()

    def _enlarge(self, row, col):
        if not self.thumb_fn or col != self.C_PIC:
            return
        try:
            pm = self.thumb_fn(self.cands[row], True)
        except Exception:
            pm = None
        if pm is None or pm.isNull():
            return
        d = QDialog(self)
        c = self.cands[row]
        d.setWindowTitle(f"Balloon {self.list.item(row, 0).text()} — {self.list.item(row, self.C_REQ).text()}")
        lab = QLabel()
        lab.setPixmap(pm)
        lab.setAlignment(Qt.AlignCenter)
        cap = QLabel(f"Read as: <b>{self.list.item(row, self.C_REQ).text()}</b>   ·   "
                     f"{getattr(c, 'ref_location', '')}  {getattr(c, 'view', '')}")
        lay = QVBoxLayout(d)
        lay.addWidget(lab, 1)
        lay.addWidget(cap)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(d.reject)
        lay.addWidget(bb)
        d.exec()

    def _all(self, on):
        for r in range(self.list.rowCount()):
            self.list.item(r, 0).setCheckState(Qt.Checked if on else Qt.Unchecked)

    def _type_off(self, typ):
        for r in range(self.list.rowCount()):
            if self.list.item(r, self.C_TYPE).text() == typ:
                self.list.item(r, 0).setCheckState(Qt.Unchecked)

    def selected(self):
        out = []
        for i, c in enumerate(self.cands):
            if self.list.item(i, 0).checkState() == Qt.Checked:
                c.requirement = self.list.item(i, self.C_REQ).text().strip()
                out.append(c)
        return out


class IssuesDialog(QDialog):
    def __init__(self, issues: list[str], parent=None, title="Completeness Check"):
        super().__init__(parent)
        self.setWindowTitle(title)
        fit_to_screen(self, 640, 460)
        lay = QVBoxLayout(self)
        if issues:
            lay.addWidget(QLabel(f"<b style='color:#b42318'>{len(issues)} item(s) need attention:</b>"))
            lw = QListWidget()
            for i in issues:
                QListWidgetItem("•  " + i, lw)
            lay.addWidget(lw, 1)
        else:
            lay.addWidget(QLabel("<b style='color:#146c2e'>✔ Report is complete — every characteristic is "
                                 "accounted for and dispositioned.</b>"))
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        lay.addWidget(bb)


class ReportOptionsWidget(QGroupBox):
    def __init__(self, is_fai: bool, parent=None):
        super().__init__("Include in report", parent)
        lay = QHBoxLayout(self)
        self.form1 = QCheckBox("Form 1")
        self.form2 = QCheckBox("Form 2")
        self.form3 = QCheckBox("Form 3")
        self.drawings = QCheckBox("Ballooned drawings")
        self.photos = QCheckBox("Photos")
        self.append = QCheckBox("Append other PDFs (certs)")
        self.watermark = QCheckBox("Status watermark")
        for c in (self.form1, self.form2, self.form3, self.drawings, self.photos, self.watermark):
            c.setChecked(True)
        for c in (self.form1, self.form2, self.form3):
            c.setVisible(is_fai)
        for c in (self.form1, self.form2, self.form3, self.drawings, self.photos, self.append, self.watermark):
            lay.addWidget(c)
        lay.addStretch(1)

    def set_fai(self, is_fai):
        for c in (self.form1, self.form2, self.form3):
            c.setVisible(is_fai)

    def options(self, balloon_scale=1.0):
        from ..reports import ReportOptions
        return ReportOptions(form1=self.form1.isChecked(), form2=self.form2.isChecked(), form3=self.form3.isChecked(),
                             drawings=self.drawings.isChecked(), photos=self.photos.isChecked(),
                             append_pdfs=self.append.isChecked(), watermark_draft=self.watermark.isChecked(),
                             balloon_scale=balloon_scale)


class ExportSetDialog(QDialog):
    """Export several documents (report types + CoC) at once, each as its own file."""

    def __init__(self, docs: list[str], current: str, folder: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Export several documents")
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Each document is built from the same report data and saved as its own file:"))
        self.checks = []
        for d in docs:
            c = QCheckBox(d)
            c.setChecked(d == current or d == "Certificate of Conformance")
            self.checks.append(c)
            lay.addWidget(c)
        self.xlsx = QCheckBox("Also save an Excel copy of each inspection report")
        lay.addWidget(self.xlsx)
        row = QHBoxLayout()
        self.folder = QLineEdit(folder)
        b = QPushButton("Browse…")
        b.clicked.connect(self._browse)
        row.addWidget(QLabel("Folder:"))
        row.addWidget(self.folder, 1)
        row.addWidget(b)
        lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Export")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        fit_to_screen(self, 560, 360)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Export to folder", self.folder.text())
        if d:
            self.folder.setText(d)

    def result_values(self):
        return self.folder.text().strip(), [c.text() for c in self.checks if c.isChecked()], self.xlsx.isChecked()


# ---------------------------------------------------------------------------- part picker
class PartPickerDialog(QDialog):
    """Pick the part number to balloon from what the drawing(s) define."""

    def __init__(self, merged: list[dict], infos: list, preselect: str = "", parent=None):
        """merged: [{'pn','name','kind','sheets','views','note','items':[(att, info, opt)]}]"""
        super().__init__(parent)
        self.setWindowTitle("Which part number are we ballooning?")
        fit_to_screen(self, 1000, 680)
        self.merged = merged
        info = infos[0] if infos else None
        head = QLabel()
        head.setWordWrap(True)
        if info is not None:
            tol = info.tolerances
            tol_s = ", ".join(f"{k.replace('tol_', '').replace('dec', ' pl')} ±{v}" for k, v in sorted(tol.items())) or "not found"
            head.setText(
                f"<b>Drawing:</b> {info.drawing_no or '?'} &nbsp; <b>Rev:</b> {info.rev or '?'} &nbsp; "
                f"<b>CAGE:</b> {info.cage or '?'} &nbsp; <b>Title:</b> {info.title or '?'}<br>"
                f"<b>Title-block tolerances:</b> {tol_s}<br>"
                f"<span style='color:#4a4f52'>{' | '.join(i.summary() for i in infos)}</span>")
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter…")
        self.search.textChanged.connect(self._filter)
        self.table = QTableWidget(len(merged), 6)
        self.table.setHorizontalHeaderLabels(["Part number", "Name", "Type", "Sheets", "Views found", "Notes"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        sel_row = None
        for r, m in enumerate(merged):
            vals = [m["pn"], m["name"], m["kind"], ", ".join(map(str, m["sheets"])) or "—",
                    str(m["views"]) if m["kind"] != "Drawing" else "all", m["note"]]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                if m["kind"] != "Drawing" and not m["views"]:
                    it.setForeground(Qt.darkGray)
                self.table.setItem(r, c, it)
            if preselect and m["pn"].upper() == preselect.upper():
                sel_row = r
        self.table.resizeColumnsToContents()
        self.table.setColumnWidth(1, 300)
        self.table.itemSelectionChanged.connect(self._sel)
        self.table.doubleClicked.connect(lambda _: self.accept())
        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        self.detail.setStyleSheet("color:#2a2f31;")
        self.manual = QLineEdit(preselect if sel_row is None else "")
        self.manual.setPlaceholderText("…or type a part number that is not listed (balloons the whole drawing)")
        self.opt_header = QCheckBox("Update report header from the drawing (part number / name, drawing no. / rev, "
                                    "title-block tolerances, detail/assembly)")
        self.opt_header.setChecked(True)
        self.opt_notes = QCheckBox("Import the drawing notes that apply to this part (general + its flag notes)")
        self.opt_notes.setChecked(True)
        self.opt_form2 = QCheckBox("Add material and referenced specifications to Form 2")
        self.opt_form2.setChecked(True)
        self.opt_form1 = QCheckBox("Assemblies: fill the Form 1 index from the drawing parts list")
        self.opt_form1.setChecked(True)
        self.opt_clear = QCheckBox("Remove existing characteristics first")
        self.opt_clear.setChecked(False)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Auto-Balloon This Part")
        bb.button(QDialogButtonBox.Ok).setObjectName("primary")
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(head)
        lay.addWidget(self.search)
        lay.addWidget(self.table, 1)
        lay.addWidget(self.detail)
        lay.addWidget(self.manual)
        for c in (self.opt_header, self.opt_notes, self.opt_form2, self.opt_form1, self.opt_clear):
            lay.addWidget(c)
        lay.addWidget(bb)
        if sel_row is not None:
            self.table.selectRow(sel_row)
        elif merged:
            first = next((i for i, m in enumerate(merged) if m["kind"] != "Drawing" and m["views"]), 0)
            self.table.selectRow(first)

    def _filter(self, t):
        t = t.strip().lower()
        for r in range(self.table.rowCount()):
            txt = " ".join(self.table.item(r, c).text().lower() for c in range(3))
            self.table.setRowHidden(r, bool(t) and t not in txt)

    def _sel(self):
        m = self.selected()
        if not m:
            return
        views = []
        for att, info, opt in m["items"]:
            for v in opt.views[:12]:
                sn = info.sheets[v.page].sheet_no or v.page + 1
                views.append(f"{v.title} (SH {sn})")
        txt = "; ".join(views[:14]) + (" …" if len(views) > 14 else "")
        if m["kind"] == "Drawing":
            txt = "Every sheet of the drawing is ballooned."
        elif not m["views"]:
            txt = ("No view titled for this part was found — the whole drawing will be ballooned. Tip: use "
                   "⬚ Balloon Area on the drawing to balloon just the views you want.")
        self.detail.setText(f"<b>Views:</b> {txt}")

    def selected(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.merged[rows[0].row()]

    def _ok(self):
        if not self.selected() and not self.manual.text().strip():
            QMessageBox.warning(self, "Select", "Select a part number or type one.")
            return
        self.accept()

    def result_part(self):
        if self.manual.text().strip():
            pn = self.manual.text().strip()
            m = next((m for m in self.merged if m["pn"].upper() == pn.upper()), None)
            if m:
                return m
            drawing = next((m for m in self.merged if m["kind"] == "Drawing"), None)
            return dict(drawing or {}, pn=pn, name=(drawing or {}).get("name", ""), kind="Drawing", manual=True)
        return self.selected()
