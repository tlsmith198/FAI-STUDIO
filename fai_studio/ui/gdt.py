"""GD&T entry: symbol palette for any requirement field + a feature-control-frame builder.

The requirement text uses the same Unicode symbols the reader produces (⌖ ⏥ ⌓ Ⓜ Ø ...), so what is built here
prints on every report and is parsed by the tolerance engine (limits / accept-reject) like a read callout.
"""
from __future__ import annotations

import re
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGridLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton, QToolButton, QVBoxLayout, QWidget)

from ..rules import GDT_SYMBOLS, MODIFIERS, OTHER_SYMBOLS
from .common import FlowRow

CHAR_TYPES_NEW = ["Dimension", "GD&T", "Angle", "Note", "Thread", "Surface Finish", "Material", "Visual", "Functional",
                  "Reference", "Basic", "Other"]
DATUM_MODS = [("", "RFS / none"), ("Ⓜ", "MMB"), ("Ⓛ", "LMB")]
ZONE_MODS = [("", "none"), ("Ⓜ", "MMC"), ("Ⓛ", "LMC")]


def text_icon(t: str, size: int = 18) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    f = QFont()
    f.setPixelSize(int(size * 0.85))
    p.setFont(f)
    p.setPen(QColor("#1d4ed8"))
    p.drawText(pm.rect(), Qt.AlignCenter, t)
    p.end()
    return QIcon(pm)


def insert_text(edit: QLineEdit, t: str):
    edit.insert(t)
    edit.setFocus()


def symbol_menu(parent, edit: QLineEdit, builder: bool = True) -> QMenu:
    """Popup with every symbol; picking one inserts it at the cursor. 'Feature control frame…' builds a whole frame."""
    m = QMenu(parent)
    if builder:
        a = m.addAction("⌖  Feature control frame builder…")
        a.triggered.connect(lambda: _build_into(parent, edit))
        m.addSeparator()
    for title, items in (("Geometric characteristic", GDT_SYMBOLS), ("Modifiers", MODIFIERS),
                         ("Dimension symbols", OTHER_SYMBOLS)):
        sub = m.addMenu(title)
        for sym, name in items:
            act = sub.addAction(f"{sym}    {name}")
            act.triggered.connect(lambda _=False, s=sym: insert_text(edit, s + ("" if s in ("Ø", "R", "SR", "SØ", "CR")
                                                                                   else " ")))
    return m


def _build_into(parent, edit: QLineEdit):
    dlg = FcfBuilderDialog(parent, edit.text())
    if dlg.exec() == QDialog.Accepted:
        reqs = dlg.requirements()
        edit.setText(reqs[0] if reqs else "")
        if len(reqs) > 1 and callable(getattr(edit, "_on_extra", None)):
            edit._on_extra(reqs[1:])
        edit.setFocus()


def add_symbol_button(edit: QLineEdit) -> None:
    """Adds a small ⌖ button inside a line edit that opens the symbol menu."""
    act = QAction(text_icon("⌖"), "Insert GD&T symbol / build a feature control frame", edit)
    act.triggered.connect(lambda: symbol_menu(edit, edit).exec(edit.mapToGlobal(edit.rect().bottomRight())))
    edit.addAction(act, QLineEdit.TrailingPosition)


class SymbolBar(QWidget):
    """One-click row of the common symbols, for dialogs (inserts into the given line edit)."""

    def __init__(self, edit: QLineEdit, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        row = FlowRow(spacing=2)
        for sym, name in GDT_SYMBOLS + MODIFIERS[:3] + [s for s in OTHER_SYMBOLS if s[0] in ("Ø", "±", "°", "⌴", "⌵", "↧", "□")]:
            b = QToolButton()
            b.setText(sym)
            b.setToolTip(name)
            b.setMinimumWidth(28)
            f = b.font()
            f.setPointSizeF(f.pointSizeF() + 2)
            b.setFont(f)
            b.clicked.connect(lambda _=False, s=sym: insert_text(edit, s))
            row.addWidget(b)
        lay.addWidget(row)


class FcfBuilderDialog(QDialog):
    """Build '⌖ Ø.010Ⓜ A B C' (optionally a composite second row) without typing symbols."""

    def __init__(self, parent=None, current: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Feature control frame")
        self.sym = QComboBox()
        for s, n in GDT_SYMBOLS:
            self.sym.addItem(f"{s}   {n}", s)
        self.dia = QCheckBox("Ø diameter zone")
        self.tol = QLineEdit()
        self.tol.setPlaceholderText(".010")
        self.zmod = QComboBox()
        for s, n in ZONE_MODS + [("Ⓟ", "Projected zone"), ("Ⓕ", "Free state"), ("Ⓣ", "Tangent plane"),
                                 ("Ⓤ", "Unequal profile")]:
            self.zmod.addItem(f"{s}  {n}".strip(), s)
        self.extra = QLineEdit()
        self.extra.setPlaceholderText("e.g. projected height .50 or /1.00 per unit")
        self.datums = []
        dg = QGroupBox("Datum references")
        gl = QGridLayout(dg)
        for i, lab in enumerate(("Primary", "Secondary", "Tertiary")):
            e = QLineEdit()
            e.setMaxLength(8)
            e.setPlaceholderText("A-B" if i == 0 else "")
            e.setFixedWidth(70)
            m = QComboBox()
            for s, n in DATUM_MODS:
                m.addItem(f"{s}  {n}".strip(), s)
            gl.addWidget(QLabel(lab), 0, i)
            gl.addWidget(e, 1, i)
            gl.addWidget(m, 2, i)
            self.datums.append((e, m))
        self.composite = QCheckBox("Composite / second row (same symbol)")
        self.tol2 = QLineEdit()
        self.tol2.setPlaceholderText(".000")
        self.zmod2 = QComboBox()
        for s, n in ZONE_MODS:
            self.zmod2.addItem(f"{s}  {n}".strip(), s)
        self.dat2 = QLineEdit()
        self.dat2.setPlaceholderText("A")
        self.prefix = QLineEdit()
        self.prefix.setPlaceholderText("e.g. 4X  or  2X Ø.201 THRU (optional, goes in front)")
        self.preview = QLabel()
        f = self.preview.font()
        f.setPointSizeF(f.pointSizeF() + 4)
        self.preview.setFont(f)
        self.preview.setStyleSheet("padding:6px; border:1px solid #9aa0a6; background:white;")
        form = QFormLayout()
        form.addRow("Characteristic", self.sym)
        z = QHBoxLayout()
        z.addWidget(self.dia)
        z.addWidget(self.tol, 1)
        z.addWidget(self.zmod)
        form.addRow("Tolerance zone", z)
        form.addRow("Extra (optional)", self.extra)
        form.addRow("In front (optional)", self.prefix)
        r2 = QHBoxLayout()
        r2.addWidget(self.tol2, 1)
        r2.addWidget(self.zmod2)
        r2.addWidget(QLabel("datums"))
        r2.addWidget(self.dat2)
        self.row2 = QWidget()
        self.row2.setLayout(r2)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Use this")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(dg)
        lay.addWidget(self.composite)
        lay.addWidget(self.row2)
        lay.addWidget(QLabel("Result (printed on the report and used for accept / reject):"))
        lay.addWidget(self.preview)
        lay.addWidget(bb)
        for w in (self.tol, self.extra, self.tol2, self.dat2, self.prefix):
            w.textChanged.connect(self._update)
        for w in (self.sym, self.zmod, self.zmod2):
            w.currentIndexChanged.connect(self._update)
        for w in (self.dia, self.composite):
            w.toggled.connect(self._update)
        for e, m in self.datums:
            e.textChanged.connect(self._update)
            m.currentIndexChanged.connect(self._update)
        self._load(current)
        self._update()

    def _load(self, t: str):
        t = (t or "").strip()
        if not t:
            return
        syms = [s for s, _n in GDT_SYMBOLS]
        m = re.match(r"^(\d+X\s+.*?)?\s*([" + "".join(syms) + r"])\s*(Ø)?\s*([\d.]+)\s*([ⓂⓁⓅⒻⓉⓊ])?\s*(.*)$", t)
        if not m:
            return
        if m.group(1):
            self.prefix.setText(m.group(1).strip())
        self.sym.setCurrentIndex(max(0, self.sym.findData(m.group(2))))
        self.dia.setChecked(bool(m.group(3)))
        self.tol.setText(m.group(4))
        if m.group(5):
            self.zmod.setCurrentIndex(max(0, self.zmod.findData(m.group(5))))
        rest = m.group(6).split()
        for (e, mm), d in zip(self.datums, rest):
            mod = d[-1] if d and d[-1] in "ⓂⓁ" else ""
            e.setText(d.rstrip("ⓂⓁ"))
            mm.setCurrentIndex(max(0, mm.findData(mod)))

    def _row1(self) -> str:
        s = self.sym.currentData()
        zone = ("Ø" if self.dia.isChecked() else "") + self.tol.text().strip() + (self.zmod.currentData() or "")
        extra = self.extra.text().strip()
        dats = [e.text().strip().upper() + (m.currentData() or "") for e, m in self.datums if e.text().strip()]
        return " ".join(x for x in [s, zone + (f" {extra}" if extra else "")] + dats if x)

    def requirements(self) -> list[str]:
        """One requirement per frame row: a composite frame is two characteristics (inspected separately)."""
        pre = self.prefix.text().strip()
        out = [f"{pre} {self._row1()}".strip() if pre else self._row1()]
        if self.composite.isChecked() and self.tol2.text().strip():
            r2 = (f"{self.sym.currentData()} {'Ø' if self.dia.isChecked() else ''}{self.tol2.text().strip()}"
                  f"{self.zmod2.currentData() or ''}")
            if self.dat2.text().strip():
                r2 += " " + " ".join(self.dat2.text().upper().replace(",", " ").split())
            out.append(f"{pre} {r2}".strip() if pre else r2)
        return [r for r in out if r]

    def requirement(self) -> str:
        return (self.requirements() or [""])[0]

    def _update(self):
        self.row2.setEnabled(self.composite.isChecked())
        reqs = self.requirements()
        self.preview.setText("\n".join(reqs) + ("\n(composite: two characteristics)" if len(reqs) > 1 else "") or " ")


class RequirementDialog(QDialog):
    """Asked when a balloon is added by hand: requirement with symbol bar / frame builder, type and designator."""

    def __init__(self, parent=None, char_no: Optional[int] = None, current: str = ""):
        super().__init__(parent)
        self.setWindowTitle(f"Characteristic {char_no}" if char_no else "Characteristic")
        self.req = QLineEdit(current)
        self.req.setPlaceholderText("e.g. 1.250 ±.005   Ø.250 +.002/-.000   ⌖ Ø.010Ⓜ A B C")
        add_symbol_button(self.req)
        self.typ = QComboBox()
        self.typ.addItems(CHAR_TYPES_NEW)
        self.desig = QComboBox()
        self.desig.addItems(["", "Key", "Critical", "Major", "Minor"])
        fcf = QPushButton("⌖ Feature control frame…")
        fcf.clicked.connect(self._fcf)
        form = QFormLayout()
        form.addRow("Requirement", self.req)
        form.addRow("", SymbolBar(self.req))
        row = QHBoxLayout()
        row.addWidget(fcf)
        row.addStretch(1)
        form.addRow("", row)
        form.addRow("Type", self.typ)
        form.addRow("Designator", self.desig)
        self.remember = QCheckBox("Ask for the requirement every time I add a balloon")
        self.remember.setChecked(True)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Save")
        bb.button(QDialogButtonBox.Cancel).setText("Leave blank")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.remember)
        lay.addWidget(bb)
        self.req.textChanged.connect(self._auto_type)
        self.req._on_extra = lambda rows: setattr(self, "extra_rows", rows)
        self.extra_rows: list[str] = []
        self.resize(640, 260)

    def _fcf(self):
        dlg = FcfBuilderDialog(self, self.req.text())
        if dlg.exec() == QDialog.Accepted:
            reqs = dlg.requirements()
            self.req.setText(reqs[0] if reqs else "")
            self.extra_rows = reqs[1:]

    def _auto_type(self, t):
        if any(s in t for s, _n in GDT_SYMBOLS):
            self.typ.setCurrentText("GD&T")

    def values(self) -> dict:
        return {"requirement": self.req.text().strip(), "char_type": self.typ.currentText(),
                "designator": self.desig.currentText(), "extra_rows": list(self.extra_rows)}
