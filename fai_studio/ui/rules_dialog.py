"""Drawing Reader Rules library: add / refine rules, attach example drawings, test, change log, import / export."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
                               QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from .. import rules as R
from .common import FlowRow, fit_to_screen, guard


def _fmt_time(t) -> str:
    try:
        return t.astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(t or "")[:16]


def read_drawing(data: bytes, filename: str) -> list:
    """Candidates the reader produces with the current rules (whole drawing, no cache)."""
    from ..drawing_intel import analyze_drawing
    info = analyze_drawing(data, filename, True)
    return info.balloon(None)


class ExpectedDialog(QDialog):
    """What an example drawing should produce: one requirement per line."""

    def __init__(self, parent, filename: str, expected: list[str], data: bytes):
        super().__init__(parent)
        self.data, self.filename = data, filename
        self.setWindowTitle(f"Expected characteristics — {filename}")
        self.edit = QPlainTextEdit("\n".join(expected))
        fill = QPushButton("⟳ Fill from what the reader finds now")
        fill.setToolTip("Reads the drawing with the current rules and lists the ticked characteristics. "
                        "Correct the list, then save - that becomes the answer this drawing must keep giving.")
        fill.clicked.connect(self._fill)
        info = QLabel("One characteristic per line, exactly as it should read (symbols allowed). The test compares "
                      "this list with what the reader finds: missing lines and extra lines are both reported.")
        info.setWordWrap(True)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(info)
        lay.addWidget(fill)
        lay.addWidget(self.edit, 1)
        lay.addWidget(bb)
        fit_to_screen(self, 700, 560)

    def _fill(self):
        from .workers import run_with_progress
        cands = guard(self, lambda: run_with_progress(self, "Reading drawing…",
                                                      lambda cb: read_drawing(self.data, self.filename)))
        if cands is not None:
            self.edit.setPlainText("\n".join(c.requirement for c in cands if c.selected))

    def expected(self) -> list[str]:
        return [l.strip() for l in self.edit.toPlainText().splitlines() if l.strip()]


class ReaderRulesDialog(QDialog):
    COLS = ["On", "Name", "Type", "Applies to", "Examples", "Ver.", "Updated"]

    def __init__(self, db, parent=None, attachments: Optional[list[dict]] = None):
        """attachments: [(attachment row, bytes-loader)] of the open report, offered as examples."""
        super().__init__(parent)
        self.db = db
        self.attachments = attachments or []
        self.readonly = getattr(db, "is_local", False)
        self.setWindowTitle("Drawing Reader Rules (shared by all workstations)")
        fit_to_screen(self, 1380, 860)
        self.rows: list[dict] = []
        self.cur: Optional[dict] = None
        self.param_widgets: dict = {}

        # ---- left: rule list
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search rules…")
        self.search.textChanged.connect(self._fill_table)
        self.type_filter = QComboBox()
        self.type_filter.addItem("All types", "")
        for k, v in R.RULE_TYPES.items():
            self.type_filter.addItem(v["label"], k)
        self.type_filter.currentIndexChanged.connect(self._fill_table)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(24)
        self.table.setWordWrap(False)
        self.table.setMinimumWidth(380)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for i, w in ((0, 34), (2, 170), (3, 150), (4, 64), (5, 40), (6, 120)):
            self.table.setColumnWidth(i, w)
        self.table.itemSelectionChanged.connect(self._select)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        top.addWidget(self.search, 1)
        top.addWidget(self.type_filter)
        ll.addLayout(top)
        ll.addWidget(self.table, 1)
        lb = FlowRow()
        for text, fn, tip in (("➕ New Rule…", self.new_rule, "Start a rule of any type"),
                              ("⎘ Duplicate", self.duplicate, "Copy the selected rule to adapt it for another customer"),
                              ("🗑 Delete", self.delete, "Delete the selected rule (kept in the change log)")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            b.setEnabled(not self.readonly)
            lb.addWidget(b)
        ll.addWidget(lb)

        # ---- right: editor / examples / log
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_editor(), "Rule")
        self.tabs.addTab(self._build_examples(), "Example drawings")
        self.tabs.addTab(self._build_log(), "Change log")
        split = QSplitter(Qt.Horizontal)
        split.addWidget(left)
        split.addWidget(self.tabs)
        split.setSizes([560, 820])

        # ---- bottom
        bottom = FlowRow()
        for text, fn, tip in (
                ("▶ Test All Examples", self.test_all, "Read every example drawing with the current rules and compare "
                                                       "with what it should find"),
                ("🔍 Try Rules on a Drawing…", self.try_file, "Read any drawing with the current rules and show what "
                                                              "is found (nothing is saved)"),
                ("⬇ Export Library…", self.export, "Save all rules to a .json file (backup / another site)"),
                ("⬆ Import…", self.import_, "Add rules from an exported .json file")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            if text.startswith("⬆"):
                b.setEnabled(not self.readonly)
            bottom.addWidget(b)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        lay = QVBoxLayout(self)
        head = QLabel("Rules teach the drawing reader new formats without a software update. They are stored in the "
                      "database, apply on every workstation as soon as they are saved, and every change is logged. "
                      "Attach the drawing that needed the rule as an <b>example</b> so <b>Test All Examples</b> can "
                      "prove later changes don't break it." + ("<br><b>Offline: read-only.</b>" if self.readonly else ""))
        head.setWordWrap(True)
        lay.addWidget(head)
        lay.addWidget(split, 1)
        row = QHBoxLayout()
        row.addWidget(bottom, 1)
        row.addWidget(close, 0, Qt.AlignBottom)
        lay.addLayout(row)
        self.reload()

    # ================================================================== editor
    def _build_editor(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        form = QFormLayout()
        self.name = QLineEdit()
        self.rtype = QComboBox()
        for k, v in R.RULE_TYPES.items():
            self.rtype.addItem(v["label"], k)
        self.rtype.currentIndexChanged.connect(lambda _i: self._build_params(self.rtype.currentData(), {}))
        self.enabled = QCheckBox("Enabled")
        self.priority = QSpinBox()
        self.priority.setRange(0, 999)
        self.priority.setToolTip("Lower numbers run first (matters for text fixes and 'Read line-drawn text')")
        self.scope_text = QLineEdit()
        self.scope_text.setPlaceholderText("blank = every drawing.  e.g.  99X1234  or  PMI VIEW SHEET  or  LOCKHEED|04236")
        self.scope_producer = QLineEdit()
        self.scope_producer.setPlaceholderText("blank = any.  e.g.  NX  /  SolidWorks  /  CATIA")
        self.notes = QPlainTextEdit()
        self.notes.setFixedHeight(60)
        self.help = QLabel()
        self.help.setWordWrap(True)
        self.help.setStyleSheet("color:#4a4f52; background:#F4F6F8; padding:6px; border-radius:4px;")
        form.addRow("Name", self.name)
        form.addRow("Type", self.rtype)
        er = QHBoxLayout()
        er.addWidget(self.enabled)
        er.addStretch(1)
        er.addWidget(QLabel("Priority"))
        er.addWidget(self.priority)
        form.addRow("", er)
        lay.addLayout(form)
        lay.addWidget(self.help)
        self.params_box = QGroupBox("Settings")
        self.params_form = QFormLayout(self.params_box)
        lay.addWidget(self.params_box)
        sg = QGroupBox("Applies to (leave blank for all drawings)")
        sf = QFormLayout(sg)
        sf.addRow("Drawing text or file name matches", self.scope_text)
        sf.addRow("PDF made by (Creator / Producer contains)", self.scope_producer)
        lay.addWidget(sg)
        lay.addWidget(QLabel("Notes (why the rule exists, customer, ticket …)"))
        lay.addWidget(self.notes)
        btns = FlowRow()
        self.btn_save = QPushButton("💾 Save Rule")
        self.btn_save.setObjectName("primary")
        self.btn_save.clicked.connect(self.save)
        self.btn_revert = QPushButton("↶ Discard Changes")
        self.btn_revert.clicked.connect(lambda: self._load(self.cur))
        self.btn_save_test = QPushButton("💾 Save && Test Its Examples")
        self.btn_save_test.clicked.connect(lambda: self.save() and self.test_examples(self.cur and self.cur.get("id")))
        for b in (self.btn_save, self.btn_save_test, self.btn_revert):
            b.setEnabled(not self.readonly)
            btns.addWidget(b)
        lay.addWidget(btns)
        lay.addStretch(1)
        return w

    def _build_params(self, rtype: str, params: dict):
        while self.params_form.rowCount():
            self.params_form.removeRow(0)
        self.param_widgets = {}
        spec = R.RULE_TYPES.get(rtype, {})
        self.help.setText(spec.get("help", ""))
        for key, label, kind in spec.get("params", []):
            v = params.get(key, "")
            if kind == "bool":
                wdg = QCheckBox()
                wdg.setChecked(bool(v))
            elif kind == "int":
                wdg = QSpinBox()
                wdg.setRange(72, 600)
                wdg.setValue(int(v or 200))
            elif kind.startswith("choice:"):
                wdg = QComboBox()
                wdg.addItems(kind.split(":", 1)[1].split(","))
                if str(v):
                    i = wdg.findText(str(v))
                    if i < 0:
                        wdg.addItem(str(v))
                        i = wdg.findText(str(v))
                    wdg.setCurrentIndex(i)
            elif kind == "gdt":
                wdg = QComboBox()
                wdg.setEditable(True)
                for s, n in R.GDT_SYMBOLS + R.MODIFIERS + R.OTHER_SYMBOLS:
                    wdg.addItem(f"{s}", s)
                    wdg.setItemData(wdg.count() - 1, n, Qt.ToolTipRole)
                wdg.setCurrentText(str(v))
            else:
                wdg = QLineEdit(str(v))
                if key in ("pattern", "find", "skip", "scope_text"):
                    wdg.setToolTip("Regular expression: \\d = digit, \\s = space, | = or, .* = anything")
            self.param_widgets[key] = (kind, wdg)
            self.params_form.addRow(label, wdg)

    def _params(self) -> dict:
        out = {}
        for key, (kind, w) in self.param_widgets.items():
            if kind == "bool":
                out[key] = w.isChecked()
            elif kind == "int":
                out[key] = w.value()
            elif isinstance(w, QComboBox):
                out[key] = w.currentText()
            else:
                out[key] = w.text()
        return out

    def _load(self, r: Optional[dict]):
        self.cur = r
        r = r or {"name": "", "rule_type": "replace", "params": "{}", "enabled": True, "priority": 100,
                  "scope_text": "", "scope_producer": "", "notes": ""}
        self.name.setText(r.get("name") or "")
        self.rtype.blockSignals(True)
        self.rtype.setCurrentIndex(max(0, self.rtype.findData(r.get("rule_type"))))
        self.rtype.blockSignals(False)
        params = r.get("params") or {}
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except Exception:
                params = {}
        self._build_params(r.get("rule_type"), params)
        self.enabled.setChecked(bool(r.get("enabled", True)))
        self.priority.setValue(int(r.get("priority") or 100))
        self.scope_text.setText(r.get("scope_text") or "")
        self.scope_producer.setText(r.get("scope_producer") or "")
        self.notes.setPlainText(r.get("notes") or "")
        self._fill_examples()
        self._fill_log()

    def _collect(self) -> dict:
        d = {"name": self.name.text().strip(), "rule_type": self.rtype.currentData(), "params": self._params(),
             "enabled": self.enabled.isChecked(), "priority": self.priority.value(),
             "scope_text": self.scope_text.text().strip(), "scope_producer": self.scope_producer.text().strip(),
             "notes": self.notes.toPlainText().strip()}
        if self.cur and self.cur.get("id"):
            d["id"] = self.cur["id"]
        return d

    def _validate(self, d: dict) -> Optional[str]:
        import re
        if not d["name"]:
            return "Give the rule a name."
        for k in ("pattern", "skip"):
            if d["params"].get(k):
                try:
                    re.compile(d["params"][k])
                except re.error as e:
                    return f"'{k}' is not a valid pattern: {e}"
        if d["scope_text"]:
            try:
                re.compile(d["scope_text"])
            except re.error as e:
                return f"'Applies to' is not a valid pattern: {e}"
        if d["params"].get("regex") and d["params"].get("find"):
            try:
                re.compile(d["params"]["find"])
            except re.error as e:
                return f"'Find' is not a valid pattern: {e}"
        need = {"replace": "find", "ignore": "pattern", "callout": "pattern", "table": "title", "gdt_word": "word",
                "spec": "pattern", "title": "pattern"}.get(d["rule_type"])
        if need and not str(d["params"].get(need) or "").strip():
            return f"Fill in '{need}'."
        return None

    def save(self) -> bool:
        if self.readonly:
            return False
        d = self._collect()
        err = self._validate(d)
        if err:
            QMessageBox.warning(self, "Rule", err)
            return False
        row = guard(self, lambda: self.db.save_rule(d))
        if not row:
            return False
        self.cur = row
        self.reload(select=row["id"])
        R.load_shared(self.db)
        return True

    # ================================================================== list
    def reload(self, select: Optional[int] = None):
        self.rows = guard(self, self.db.reader_rules) or []
        R.set_active([{k: v for k, v in r.items()} for r in self.rows])
        sel = select if select is not None else (self.cur or {}).get("id")
        self._fill_table(select=sel)
        fresh = next((r for r in self.rows if r["id"] == sel), None)
        if fresh is not None:
            self._load(fresh)                     # show what is stored now (after save / restore / examples)
        self._fill_log()

    def _fill_table(self, *_a, select: Optional[int] = None):
        q = self.search.text().strip().lower()
        t = self.type_filter.currentData()
        rows = [r for r in self.rows if (not t or r["rule_type"] == t) and
                (not q or q in f"{r['name']} {r['params']} {r['scope_text']} {r['notes']}".lower())]
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            scope = " · ".join(x for x in (r.get("scope_text"), r.get("scope_producer")) if x) or "all drawings"
            vals = ["✔" if r["enabled"] else "", r["name"], R.RULE_TYPES.get(r["rule_type"], {}).get("label", r["rule_type"]),
                    scope, str(r.get("examples") or ""), str(r.get("version") or 1),
                    f"{_fmt_time(r.get('updated_at'))} {r.get('updated_by') or ''}"]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                it.setData(Qt.UserRole, r["id"])
                if not r["enabled"]:
                    it.setForeground(Qt.gray)
                self.table.setItem(i, c, it)
        self.table.blockSignals(False)
        if select is not None:
            for i in range(self.table.rowCount()):
                if self.table.item(i, 0).data(Qt.UserRole) == select:
                    self.table.selectRow(i)
                    return
        if self.table.rowCount() and self.cur is None:
            self.table.selectRow(0)

    def _select(self):
        items = self.table.selectedItems()
        if not items:
            return
        rid = items[0].data(Qt.UserRole)
        r = next((x for x in self.rows if x["id"] == rid), None)
        if r is not None and (self.cur is None or self.cur.get("id") != rid):
            self._load(r)

    def new_rule(self):
        types = [v["label"] for v in R.RULE_TYPES.values()]
        t, ok = QInputDialog.getItem(self, "New rule", "What should the rule do?", types, 0, False)
        if not ok:
            return
        key = [k for k, v in R.RULE_TYPES.items() if v["label"] == t][0]
        self.table.clearSelection()
        self._load({"name": "", "rule_type": key, "params": {}, "enabled": True, "priority": 100})
        self.cur = None
        self.tabs.setCurrentIndex(0)
        self.name.setFocus()

    def duplicate(self):
        if not self.cur:
            return
        d = dict(self.cur)
        d.pop("id", None)
        d["name"] = f"{d['name']} (copy)"
        self._load(d)
        self.cur = None
        self.name.setFocus()
        self.name.selectAll()

    def delete(self):
        if not self.cur or not self.cur.get("id"):
            return
        n = self.cur.get("examples") or 0
        if QMessageBox.question(self, "Delete rule", f"Delete the rule '{self.cur['name']}'"
                                + (f" and its {n} example drawing(s)" if n else "") + "?\n\nThe rule text stays in the "
                                "change log and can be restored from there.") != QMessageBox.Yes:
            return
        guard(self, lambda: self.db.delete_rule(self.cur["id"]))
        self.cur = None
        self.reload()
        R.load_shared(self.db)

    # ================================================================== examples
    def _build_examples(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel("Drawings that needed this rule, with what they must produce. Tested with "
                             "<b>Test</b> / <b>Test All Examples</b>."))
        self.ex_table = QTableWidget(0, 5)
        self.ex_table.setHorizontalHeaderLabels(["Drawing", "Expected", "Last test", "Added by", "Notes"])
        self.ex_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.ex_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.ex_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.ex_table.verticalHeader().setVisible(False)
        self.ex_table.doubleClicked.connect(lambda _i: self.edit_expected())
        lay.addWidget(self.ex_table, 1)
        b = FlowRow()
        items = [("📎 Add Drawing File…", self.add_example_file)]
        if self.attachments:
            items.append(("📎 Add From This Report…", self.add_example_report))
        items += [("✎ Expected Result…", self.edit_expected), ("▶ Test", lambda: self.test_examples(
            (self.cur or {}).get("id"))), ("↗ Open", self.open_example), ("🗑 Remove", self.remove_example)]
        for text, fn in items:
            bb = QPushButton(text)
            bb.clicked.connect(fn)
            if text.startswith(("📎", "✎", "🗑")):
                bb.setEnabled(not self.readonly)
            b.addWidget(bb)
        lay.addWidget(b)
        return w

    def _fill_examples(self):
        exs = guard(self, lambda: self.db.rule_examples(self.cur["id"])) if self.cur and self.cur.get("id") else []
        exs = exs or []
        self.ex_rows = exs
        self.ex_table.setRowCount(len(exs))
        for i, e in enumerate(exs):
            try:
                n = len(json.loads(e["expected"] or "[]"))
            except Exception:
                n = 0
            res = e.get("last_result") or "not tested"
            vals = [e["filename"], f"{n} characteristic(s)", res, f"{e.get('added_by') or ''} {_fmt_time(e.get('added_at'))}",
                    e.get("notes") or ""]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if c == 2:
                    it.setForeground(Qt.darkGreen if res.startswith("PASS") else Qt.red if res.startswith("FAIL") else Qt.gray)
                    it.setToolTip(res)
                self.ex_table.setItem(i, c, it)
        self.tabs.setTabText(1, f"Example drawings ({len(exs)})")

    def _need_saved_rule(self) -> bool:
        if not self.cur or not self.cur.get("id"):
            QMessageBox.information(self, "Examples", "Save the rule first, then attach the example drawing.")
            return False
        return True

    def _add_example(self, filename: str, data: bytes):
        from .workers import run_with_progress
        cands = guard(self, lambda: run_with_progress(self, "Reading drawing with the current rules…",
                                                      lambda cb: read_drawing(data, filename)))
        expected = [c.requirement for c in (cands or []) if c.selected]
        dlg = ExpectedDialog(self, filename, expected, data)
        dlg.setWindowTitle(f"What should {filename} produce?  (correct the list, then Save)")
        if dlg.exec() != QDialog.Accepted:
            return
        note, _ok = QInputDialog.getText(self, "Example", "Note (optional, e.g. customer / why):")
        guard(self, lambda: self.db.add_rule_example(self.cur["id"], filename, data, dlg.expected(), note or ""))
        self.reload(select=self.cur["id"])
        self._fill_examples()

    def add_example_file(self):
        if not self._need_saved_rule():
            return
        p, _ = QFileDialog.getOpenFileName(self, "Example drawing", "", "Drawings (*.pdf *.tif *.tiff *.png *.jpg)")
        if p:
            self._add_example(Path(p).name, Path(p).read_bytes())

    def add_example_report(self):
        if not self._need_saved_rule():
            return
        names = [a["filename"] for a, _l in self.attachments]
        name, ok = QInputDialog.getItem(self, "Add from this report", "Drawing:", names, 0, False)
        if ok:
            a, loader = next(x for x in self.attachments if x[0]["filename"] == name)
            data = guard(self, loader)
            if data:
                self._add_example(name, data)

    def _sel_example(self) -> Optional[dict]:
        rows = {i.row() for i in self.ex_table.selectedIndexes()}
        if not rows:
            return None
        return self.ex_rows[min(rows)]

    def edit_expected(self):
        e = self._sel_example()
        if not e:
            return
        data = guard(self, lambda: self.db.rule_example_bytes(e["id"])) or b""
        dlg = ExpectedDialog(self, e["filename"], json.loads(e["expected"] or "[]"), data)
        if self.readonly:
            dlg.edit.setReadOnly(True)
        if dlg.exec() == QDialog.Accepted and not self.readonly:
            guard(self, lambda: self.db.update_rule_example(e["id"], expected=dlg.expected()))
            self._fill_examples()

    def open_example(self):
        e = self._sel_example()
        if not e:
            return
        import tempfile

        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        data = guard(self, lambda: self.db.rule_example_bytes(e["id"]))
        if data:
            p = Path(tempfile.gettempdir()) / f"rule_example_{e['id']}_{e['filename']}"
            p.write_bytes(data)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    def remove_example(self):
        e = self._sel_example()
        if e and QMessageBox.question(self, "Remove example", f"Remove {e['filename']} from this rule?") == QMessageBox.Yes:
            guard(self, lambda: self.db.delete_rule_example(e["id"]))
            self.reload(select=self.cur["id"])
            self._fill_examples()

    # ================================================================== testing
    def _run_tests(self, exs: list[dict]) -> list[tuple]:
        from .workers import run_with_progress
        loads = [(e, self.db.rule_example_bytes(e["id"])) for e in exs]

        def work(cb):
            out = []
            for i, (e, data) in enumerate(loads):
                cb(f"Reading {e['filename']} ({i + 1} of {len(loads)})…", i / max(1, len(loads)))
                try:
                    got = [c.requirement for c in read_drawing(data, e["filename"]) if c.selected]
                    missing, extra = R.compare(json.loads(e["expected"] or "[]"), got)
                    res = "PASS" if not missing and not extra else \
                        "FAIL — " + "; ".join(x for x in (f"missing: {', '.join(missing)}" if missing else "",
                                                           f"extra: {', '.join(extra)}" if extra else "") if x)
                except Exception as ex:  # noqa: BLE001
                    res = f"FAIL — could not read: {ex}"
                out.append((e, res))
            return out
        return guard(self, lambda: run_with_progress(self, "Testing reader rules…", work)) or []

    def _record(self, results):
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        if not self.readonly:
            for e, res in results:
                guard(self, lambda e=e, res=res: self.db.update_rule_example(e["id"], last_result=f"{res}  ({stamp})"))

    def test_examples(self, rule_id):
        if not rule_id:
            return
        exs = guard(self, lambda: self.db.rule_examples(rule_id)) or []
        if not exs:
            QMessageBox.information(self, "Test", "This rule has no example drawings yet.")
            return
        results = self._run_tests(exs)
        self._record(results)
        self._fill_examples()
        self._report(results)

    def test_all(self):
        exs = guard(self, lambda: self.db.rule_examples()) or []
        if not exs:
            QMessageBox.information(self, "Test", "No example drawings are attached to any rule yet.")
            return
        results = self._run_tests(exs)
        self._record(results)
        self.reload()
        self._fill_examples()
        self._report(results)

    def _report(self, results):
        names = {r["id"]: r["name"] for r in self.rows}
        ok = sum(1 for _e, r in results if r.startswith("PASS"))
        lines = [f"{'✔' if res.startswith('PASS') else '✘'}  {e['filename']}  [{names.get(e['rule_id'], '?')}]  {res}"
                 for e, res in results]
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Test results: {ok} of {len(results)} passed")
        t = QPlainTextEdit("\n".join(lines))
        t.setReadOnly(True)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(dlg.reject)
        bb.accepted.connect(dlg.accept)
        l = QVBoxLayout(dlg)
        l.addWidget(QLabel(f"<b>{ok} of {len(results)} example drawing(s) read as expected.</b>"))
        l.addWidget(t, 1)
        l.addWidget(bb)
        fit_to_screen(dlg, 900, 480)
        dlg.exec()

    def try_file(self):
        p, _ = QFileDialog.getOpenFileName(self, "Try the rules on a drawing", "",
                                           "Drawings (*.pdf *.tif *.tiff *.png *.jpg)")
        if not p:
            return
        from .workers import run_with_progress
        cands = guard(self, lambda: run_with_progress(self, "Reading…", lambda cb: read_drawing(Path(p).read_bytes(),
                                                                                               Path(p).name)))
        if cands is None:
            return
        txt = "\n".join(f"{'☑' if c.selected else '☐'}  {c.requirement}    [{c.char_type}, {c.ref_location}"
                        f"{', ' + c.comment if c.comment else ''}]" for c in cands)
        dlg = QDialog(self)
        dlg.setWindowTitle(f"{Path(p).name}: {len(cands)} found")
        t = QPlainTextEdit(txt or "(nothing found)")
        t.setReadOnly(True)
        l = QVBoxLayout(dlg)
        l.addWidget(t, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(dlg.reject)
        l.addWidget(bb)
        fit_to_screen(dlg, 900, 560)
        dlg.exec()

    # ================================================================== log
    def _build_log(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.log_all = QCheckBox("Show changes to all rules")
        self.log_all.toggled.connect(self._fill_log)
        lay.addWidget(self.log_all)
        self.log = QTableWidget(0, 5)
        self.log.setHorizontalHeaderLabels(["When", "Who / workstation", "Rule", "Action", "Change"])
        self.log.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.log.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.log.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.log.verticalHeader().setVisible(False)
        lay.addWidget(self.log, 1)
        b = QPushButton("↶ Restore Selected Version")
        b.setToolTip("Put the rule back the way it was before / after the selected change (saved as a new version)")
        b.clicked.connect(self.restore_version)
        b.setEnabled(not self.readonly)
        row = QHBoxLayout()
        row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        return w

    def _fill_log(self, *_a):
        if not hasattr(self, "log"):
            return
        rid = None if self.log_all.isChecked() else (self.cur or {}).get("id")
        rows = guard(self, lambda: self.db.rule_log(rid)) if (rid or self.log_all.isChecked()) else []
        rows = rows or []
        self.log_rows = rows
        self.log.setRowCount(len(rows))
        for i, r in enumerate(rows):
            change = _diff(r.get("before"), r.get("after"))
            vals = [_fmt_time(r["at"]), f"{r['user_name']} @ {r['host']}", r.get("rule_name") or "", r["action"], change]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                it.setToolTip(f"BEFORE:\n{r.get('before') or '—'}\n\nAFTER:\n{r.get('after') or '—'}")
                self.log.setItem(i, c, it)

    def restore_version(self):
        rows = sorted({i.row() for i in self.log.selectedIndexes()})
        if not rows:
            return
        r = self.log_rows[rows[0]]
        choices = [x for x in (("the version after this change", r.get("after")),
                               ("the version before this change", r.get("before"))) if x[1] and x[1].startswith("{")]
        if not choices:
            QMessageBox.information(self, "Restore", "This entry has no rule version to restore.")
            return
        label, ok = QInputDialog.getItem(self, "Restore", "Restore:", [c[0] for c in choices], 0, False)
        if not ok:
            return
        d = json.loads(dict(choices)[label])
        d.pop("version", None)
        exists = any(x["id"] == r.get("rule_id") for x in self.rows)
        if exists:
            d["id"] = r["rule_id"]
        row = guard(self, lambda: self.db.save_rule(d, log_action="RESTORE"))
        if row:
            self.cur = row
            self.reload(select=row["id"])
            R.load_shared(self.db)

    # ================================================================== import / export
    def export(self):
        p, _ = QFileDialog.getSaveFileName(self, "Export reader rules",
                                           f"FAI_Studio_reader_rules_{datetime.now():%Y%m%d}.json", "JSON (*.json)")
        if p:
            Path(p).write_text(R.export_json(self.rows), encoding="utf-8")
            QMessageBox.information(self, "Exported", f"{len(self.rows)} rule(s) saved to\n{p}\n\n"
                                    "(Example drawings are not exported - they may be export-controlled.)")

    def import_(self):
        p, _ = QFileDialog.getOpenFileName(self, "Import reader rules", "", "JSON (*.json)")
        if not p:
            return
        try:
            new = R.import_json(Path(p).read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Import", f"Not a reader-rules file:\n{e}")
            return
        have = {(r["name"], r["rule_type"]) for r in self.rows}
        add = [r for r in new if (r["name"], r["rule_type"]) not in have]
        if QMessageBox.question(self, "Import", f"{len(new)} rule(s) in the file, {len(add)} new "
                                f"({len(new) - len(add)} already here by name are skipped). Import?") != QMessageBox.Yes:
            return
        for r in add:
            guard(self, lambda r=r: self.db.save_rule(dict(r), log_action="IMPORT"))
        self.reload()
        R.load_shared(self.db)


def _diff(before: Optional[str], after: Optional[str]) -> str:
    try:
        b = json.loads(before) if before and before.startswith("{") else None
        a = json.loads(after) if after and after.startswith("{") else None
    except Exception:
        return (after or before or "")[:200]
    if b is None and a is None:
        return (after or before or "")[:200]
    if b is None:
        return "created"
    if a is None:
        return "deleted"
    ch = []
    for k in sorted(set(a) | set(b)):
        if k == "version":
            continue
        if a.get(k) != b.get(k):
            ch.append(f"{k}: {str(b.get(k))[:60]} → {str(a.get(k))[:60]}")
    return "; ".join(ch) or "no change"
