from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, QTimer
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QApplication, QComboBox, QCompleter, QLineEdit, QStyledItemDelegate,
                               QStyleOptionViewItem, QTableView, QWidget)

from .common import REMOTE_FLASH


@dataclass
class Col:
    key: str
    header: str
    width: int = 100
    kind: str = "text"            # text | combo | int | bool | readonly | complete | multiline | gdt | gage
    choices: list = field(default_factory=list)
    tip: str = ""


class RecordModel(QAbstractTableModel):
    """Editable table of DB rows. Edits are pushed through
    `on_edit(row, key, value, expected) -> new_row | None` where `expected` is the value the user saw when the
    editor opened (used for field-level conflict detection even while live sync updates the row)."""

    def __init__(self, cols: list[Col], on_edit: Callable[[dict, str, Any], Optional[dict]],
                 style_fn: Callable[[dict, str], tuple] | None = None, parent=None):
        super().__init__(parent)
        self.cols = cols
        self.rows: list[dict] = []
        self.on_edit = on_edit
        self.style_fn = style_fn
        self.locked = False
        self._flash: dict[int, int] = {}
        self.edit_origin: dict[tuple, Any] = {}     # (row_id, key) -> value when editor opened

    # --- basic model
    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.cols)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal:
            if role == Qt.DisplayRole:
                return self.cols[section].header
            if role == Qt.ToolTipRole:
                return self.cols[section].tip or self.cols[section].header
        elif role == Qt.DisplayRole:
            return section + 1
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        col = self.cols[index.column()]
        v = row.get(col.key)
        if col.kind == "bool":
            if role == Qt.CheckStateRole:
                return Qt.Checked if v else Qt.Unchecked
            return None
        if role in (Qt.DisplayRole, Qt.EditRole):
            if v is None:
                return ""
            if hasattr(v, "strftime"):
                if getattr(v, "tzinfo", None) is not None:
                    v = v.astimezone()              # show in this workstation's local time
                return v.strftime("%Y-%m-%d %H:%M")
            return v if role == Qt.EditRole or not isinstance(v, float) else f"{v:.4f}"
        if role == Qt.ToolTipRole and isinstance(v, str) and len(v) > 40:
            return v
        if self.style_fn and role in (Qt.BackgroundRole, Qt.ForegroundRole, Qt.FontRole):
            bg, fg, bold = self.style_fn(row, col.key)
            if role == Qt.BackgroundRole:
                if row.get("id") in self._flash:
                    return REMOTE_FLASH
                return bg
            if role == Qt.ForegroundRole:
                return fg
            if role == Qt.FontRole and bold:
                f = QFont()
                f.setBold(True)
                return f
        if role == Qt.BackgroundRole and row.get("id") in self._flash:
            return REMOTE_FLASH
        return None

    def flags(self, index):
        f = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        col = self.cols[index.column()]
        if self.locked or col.kind == "readonly":
            return f
        if col.kind == "bool":
            return f | Qt.ItemIsUserCheckable
        return f | Qt.ItemIsEditable

    def setData(self, index, value, role=Qt.EditRole):
        if not index.isValid():
            return False
        row = self.rows[index.row()]
        col = self.cols[index.column()]
        if col.kind == "bool" and role == Qt.CheckStateRole:
            value = (value == Qt.Checked or value == Qt.Checked.value or value == 2)
        elif role != Qt.EditRole:
            return False
        if col.kind == "int":
            try:
                value = int(str(value).strip())
            except ValueError:
                return False
        elif isinstance(value, str):
            value = value.strip()
        expected = self.edit_origin.pop((row.get("id"), col.key), row.get(col.key))
        if row.get(col.key) == value and expected == value:
            return False
        new = self.on_edit(row, col.key, value, expected)
        if new is not None:
            self.replace_row(new)
        return True

    # --- row management
    def set_rows(self, rows: list[dict]):
        self.beginResetModel()
        self.rows = [dict(r) for r in rows]
        self.endResetModel()

    def index_of(self, row_id) -> int:
        for i, r in enumerate(self.rows):
            if r.get("id") == row_id:
                return i
        return -1

    def replace_row(self, new: dict, flash: bool = False):
        i = self.index_of(new["id"])
        if i < 0:
            self.beginInsertRows(QModelIndex(), len(self.rows), len(self.rows))
            self.rows.append(dict(new))
            self.endInsertRows()
            i = len(self.rows) - 1
        else:
            self.rows[i] = dict(new)
            self.dataChanged.emit(self.index(i, 0), self.index(i, len(self.cols) - 1))
        if flash:
            self._flash[new["id"]] = 1
            QTimer.singleShot(1800, lambda rid=new["id"]: self._unflash(rid))

    def _unflash(self, rid):
        self._flash.pop(rid, None)
        i = self.index_of(rid)
        if i >= 0:
            self.dataChanged.emit(self.index(i, 0), self.index(i, len(self.cols) - 1))

    def remove_id(self, row_id):
        i = self.index_of(row_id)
        if i >= 0:
            self.beginRemoveRows(QModelIndex(), i, i)
            self.rows.pop(i)
            self.endRemoveRows()

    def row_at(self, i) -> Optional[dict]:
        return self.rows[i] if 0 <= i < len(self.rows) else None

    def sort_rows(self, key):
        """Re-sort while keeping open editors / selection attached to the same records."""
        self.layoutAboutToBeChanged.emit()
        old_ids = [r.get("id") for r in self.rows]
        persist = self.persistentIndexList()
        self.rows.sort(key=key)
        pos = {r.get("id"): i for i, r in enumerate(self.rows)}
        new = [self.index(pos[old_ids[p.row()]], p.column()) if 0 <= p.row() < len(old_ids) else QModelIndex()
               for p in persist]
        self.changePersistentIndexList(persist, new)
        self.layoutChanged.emit()


def gage_label(g: dict, today: str = "") -> str:
    bits = [g.get("gage_id") or ""]
    if g.get("description"):
        bits.append(f"— {g['description']}")
    if g.get("gage_type"):
        bits.append(f"· {g['gage_type']}")
    due = str(g.get("cal_due") or "")
    if due:
        bits.append(f"· cal due {due}" + ("  ⚠ OVERDUE" if today and due < today else ""))
    return " ".join(bits)


class GageCombo(QComboBox):
    """Gage Library picker: drop-down list of every active gage, and type to find one (by ID, description or
    type - any part of it). Stores the gage ID; text that isn't in the library is kept as typed."""

    def __init__(self, gages: list[dict], parent=None):
        super().__init__(parent)
        from datetime import date
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        self.setMaxVisibleItems(18)
        today = date.today().isoformat()
        self._by_label: dict[str, str] = {}
        self._ids: dict[str, str] = {}
        self.addItem("", "")
        for g in gages:
            if (g.get("gage_id") or "").upper() in self._ids or not g.get("gage_id"):
                continue                                  # one entry per gage ID
            lab = gage_label(g, today)
            self.addItem(lab, g["gage_id"])
            self._by_label[lab] = g["gage_id"]
            self._ids[(g["gage_id"] or "").upper()] = g["gage_id"]
            if str(g.get("cal_due") or "") and str(g["cal_due"]) < today:
                self.setItemData(self.count() - 1, QColor("#B42318"), Qt.ForegroundRole)
        comp = QCompleter(self.model(), self)
        comp.setCaseSensitivity(Qt.CaseInsensitive)
        comp.setFilterMode(Qt.MatchContains)
        comp.setCompletionMode(QCompleter.PopupCompletion)
        self.setCompleter(comp)
        self.lineEdit().setPlaceholderText("Type to find a gage…")
        self.view().setMinimumWidth(460)                 # the list shows ID, description, type and cal date
        comp.popup().setMinimumWidth(460)
        self.setToolTip("Pick from the Gage Library, or start typing any part of the gage ID / description")

    def set_value(self, v: str):
        v = (v or "").strip()
        gid = self._ids.get(v.upper())
        i = self.findData(gid) if gid else -1
        if i >= 0:
            self.setCurrentIndex(i)
            self.lineEdit().setText(gid)             # the cell holds the ID; the list shows the details
        else:
            self.setCurrentIndex(0)
            self.lineEdit().setText(v)
        self.lineEdit().selectAll()

    def value(self) -> str:
        t = self.lineEdit().text().strip()
        if t in self._by_label:
            return self._by_label[t]
        if t.upper() in self._ids:
            return self._ids[t.upper()]
        # typed the start of a description / ID that matches exactly one gage: take it
        hits = [gid for lab, gid in self._by_label.items() if t and t.upper() in lab.upper()]
        return hits[0] if len(set(hits)) == 1 else t


class RecordDelegate(QStyledItemDelegate):
    """Combo boxes for choice columns, completer for 'complete' columns."""

    def __init__(self, model_getter: Callable[[], RecordModel], completer_source: Callable[[str], list] | None = None,
                 parent=None):
        super().__init__(parent)
        self.model_getter = model_getter
        self.completer_source = completer_source

    def _col(self, index) -> Col:
        m = index.model()
        while isinstance(m, QSortFilterProxyModel):
            index = m.mapToSource(index)
            m = m.sourceModel()
        return m.cols[index.column()]

    def createEditor(self, parent: QWidget, option: QStyleOptionViewItem, index):
        col = self._col(index)
        if col.kind == "combo":
            cb = QComboBox(parent)
            cb.setEditable(False)
            cb.addItems([str(c) for c in col.choices])
            return cb
        if col.kind == "gage":
            src = getattr(self, "gage_source", None)
            cb = GageCombo(src() if src else [], parent)
            QTimer.singleShot(0, cb.showPopup) if getattr(self, "gage_popup", False) else None
            return cb
        if col.kind == "complete":
            le = QLineEdit(parent)
            items = self.completer_source(col.key) if self.completer_source else []
            comp = QCompleter(items, le)
            comp.setCaseSensitivity(Qt.CaseInsensitive)
            comp.setFilterMode(Qt.MatchContains)
            le.setCompleter(comp)
            return le
        if col.kind == "gdt":
            from .gdt import add_symbol_button
            le = QLineEdit(parent)
            add_symbol_button(le)
            le.setToolTip("Type the callout; ⌖ inserts GD&T symbols or builds a feature control frame")
            fn = getattr(self, "extra_rows_fn", None)
            if fn is not None:
                le._on_extra = lambda reqs, r=index.row(): fn(r, reqs)
            return le
        if col.kind == "multiline":
            from PySide6.QtWidgets import QPlainTextEdit
            ed = QPlainTextEdit(parent)
            ed.setTabChangesFocus(True)
            ed.setToolTip("Enter = new line · click another cell (or Tab) to save")
            return ed
        return super().createEditor(parent, option, index)

    def updateEditorGeometry(self, editor, option, index):
        from PySide6.QtWidgets import QPlainTextEdit
        if isinstance(editor, QPlainTextEdit):
            r = option.rect
            editor.setGeometry(r.x(), r.y(), max(r.width(), 260), max(r.height(), 96))
            return
        super().updateEditorGeometry(editor, option, index)

    def _source(self, index):
        m = index.model()
        while isinstance(m, QSortFilterProxyModel):
            index = m.mapToSource(index)
            m = m.sourceModel()
        return m, index

    def setEditorData(self, editor, index):
        m, si = self._source(index)
        if isinstance(m, RecordModel) and not editor.property("_orig_set"):
            row = m.row_at(si.row())
            if row is not None:
                key = m.cols[si.column()].key
                m.edit_origin[(row.get("id"), key)] = row.get(key)
                editor.setProperty("_orig_set", True)
        if isinstance(editor, GageCombo):
            editor.set_value(str(index.data(Qt.EditRole) or ""))
            return
        if isinstance(editor, QComboBox):
            v = str(index.data(Qt.EditRole) or "")
            i = editor.findText(v)
            editor.setCurrentIndex(max(i, 0))
            return
        from PySide6.QtWidgets import QPlainTextEdit
        if isinstance(editor, QPlainTextEdit):
            editor.setPlainText(str(index.data(Qt.EditRole) or ""))
            return
        super().setEditorData(editor, index)

    def setModelData(self, editor, model, index):
        if isinstance(editor, GageCombo):
            model.setData(index, editor.value(), Qt.EditRole)
            return
        if isinstance(editor, QComboBox):
            model.setData(index, editor.currentText(), Qt.EditRole)
            return
        from PySide6.QtWidgets import QPlainTextEdit
        if isinstance(editor, QPlainTextEdit):
            model.setData(index, editor.toPlainText().strip(), Qt.EditRole)
            return
        super().setModelData(editor, model, index)


# ============================================================================== copy / paste like a spreadsheet
def _source_model(view):
    m = view.model()
    while isinstance(m, QSortFilterProxyModel):
        m = m.sourceModel()
    return m


class ClipTable(QTableView):
    """Table with spreadsheet-style editing:
      Ctrl+C  copy the selected cells (tab-separated, so they also paste into Excel / Word)
      Ctrl+V  paste: one value into every selected cell, or a block from the current cell down / right
              (rows hidden by the filter are skipped, read-only columns are left alone)
      Ctrl+D  fill down: the first selected row's value into the selected cells below it
      Delete  clear the selected cells
      Ctrl+Z  undo the last paste / fill / clear
    Every cell goes through the normal edit path, so limits, Accept / Reject and the audit trail all update."""

    TRUE_WORDS = {"1", "TRUE", "YES", "Y", "X", "✔", "✓", "ON"}

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._undo: list[list[tuple]] = []
        self.on_bulk_done = None                 # callback(message) after a paste / fill / clear

    # ---------------------------------------------------------------- helpers
    def _visible_selection(self):
        idx = [i for i in self.selectionModel().selectedIndexes() if not self.isRowHidden(i.row())]
        if not idx and self.currentIndex().isValid():
            idx = [self.currentIndex()]
        return idx

    def _is_editing(self) -> bool:
        return self.state() == QTableView.EditingState

    def _col(self, c):
        return _source_model(self).cols[c]

    def _editable(self, index) -> bool:
        return bool(self.model().flags(index) & (Qt.ItemIsEditable | Qt.ItemIsUserCheckable))

    def _value(self, index) -> str:
        col = self._col(index.column())
        if col.kind == "bool":
            return "TRUE" if index.data(Qt.CheckStateRole) == Qt.Checked else "FALSE"
        v = index.data(Qt.EditRole)
        return "" if v is None else str(v)

    def _write(self, index, text: str, log: list) -> bool:
        """Put one value into one cell through the model (validated like typing it)."""
        if not self._editable(index):
            return False
        col = self._col(index.column())
        m = _source_model(self)
        row = m.row_at(index.row()) if hasattr(m, "row_at") else None
        old = self._value(index)
        text = (text or "").strip()
        if col.kind == "bool":
            want = text.upper() in self.TRUE_WORDS
            if (old == "TRUE") == want:
                return True
            ok = self.model().setData(index, Qt.Checked if want else Qt.Unchecked, Qt.CheckStateRole)
        else:
            if col.kind == "combo" and col.choices:
                match = next((c for c in col.choices if str(c).upper() == text.upper()), None)
                if match is None and text:
                    return False
                text = "" if match is None else str(match)
            if col.kind == "int" and text and not text.lstrip("-").isdigit():
                return False
            if old == text:
                return True
            ok = self.model().setData(index, text, Qt.EditRole)
        if ok and row is not None:
            log.append((row.get("id"), col.key, old))
        return bool(ok)

    def _finish(self, log, msg):
        if log:
            self._undo.append(log)
            del self._undo[:-20]
        if self.on_bulk_done:
            self.on_bulk_done(msg)

    @staticmethod
    def _parse_clip(text: str) -> list[list[str]]:
        import csv
        import io
        text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        if text.endswith("\n"):
            text = text[:-1]
        if not text:
            return []
        if "\t" not in text and "\n" not in text:
            return [[text]]
        if '"' in text:
            try:
                return [r for r in csv.reader(io.StringIO(text), delimiter="\t")]
            except Exception:
                pass
        return [line.split("\t") for line in text.split("\n")]

    # ---------------------------------------------------------------- actions
    def copy_cells(self):
        idx = self._visible_selection()
        if not idx:
            return
        rows = sorted({i.row() for i in idx})
        cols = sorted({i.column() for i in idx})
        sel = {(i.row(), i.column()) for i in idx}
        lines = []
        for r in rows:
            cells = []
            for c in cols:
                v = self._value(self.model().index(r, c)) if (r, c) in sel else ""
                if "\t" in v or "\n" in v or '"' in v:
                    v = '"' + v.replace('"', '""') + '"'
                cells.append(v)
            lines.append("\t".join(cells))
        QApplication.clipboard().setText("\n".join(lines))
        if self.on_bulk_done:
            self.on_bulk_done(f"Copied {len(sel)} cell(s)")

    def paste_cells(self):
        block = self._parse_clip(QApplication.clipboard().text())
        if not block:
            return
        idx = self._visible_selection()
        if not idx:
            return
        log, done, skipped = [], 0, 0
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            if len(block) == 1 and len(block[0]) == 1 and len(idx) > 1:
                # one value -> every selected cell
                for i in sorted(idx, key=lambda i: (i.row(), i.column())):
                    if self._write(i, block[0][0], log):
                        done += 1
                    else:
                        skipped += 1
            else:
                r0 = min(i.row() for i in idx)
                c0 = min(i.column() for i in idx)
                sel_rows = sorted({i.row() for i in idx})
                # rows to fill: visible rows from the top-left cell down (skipping filtered-out rows); a single
                # copied row is repeated on every selected row
                if len(block) == 1 and len(sel_rows) > 1:
                    targets = sel_rows
                    blk = [block[0]] * len(targets)
                else:
                    targets, r = [], r0
                    while len(targets) < len(block) and r < self.model().rowCount():
                        if not self.isRowHidden(r):
                            targets.append(r)
                        r += 1
                    blk = block[:len(targets)]
                ncols = self.model().columnCount()
                for r, vals in zip(targets, blk):
                    for k, v in enumerate(vals):
                        c = c0 + k
                        if c >= ncols:
                            break
                        if self.isColumnHidden(c):
                            continue
                        if self._write(self.model().index(r, c), v, log):
                            done += 1
                        else:
                            skipped += 1
        finally:
            QApplication.restoreOverrideCursor()
        self._finish(log, f"Pasted {done} cell(s)" + (f" · {skipped} skipped (read-only or not a valid choice)"
                                                       if skipped else ""))

    def fill_down(self):
        idx = self._visible_selection()
        if not idx:
            return
        log, done = [], 0
        by_col: dict[int, list] = {}
        for i in idx:
            by_col.setdefault(i.column(), []).append(i)
        for c, cells in by_col.items():
            cells.sort(key=lambda i: i.row())
            if len(cells) < 2:
                continue
            v = self._value(cells[0])
            for i in cells[1:]:
                done += self._write(i, v, log)
        self._finish(log, f"Filled {done} cell(s) down")

    def clear_cells(self):
        idx = self._visible_selection()
        log, done = [], 0
        for i in idx:
            col = self._col(i.column())
            if col.kind in ("bool", "int"):
                continue
            done += self._write(i, "", log)
        self._finish(log, f"Cleared {done} cell(s)")

    def undo_last(self):
        if not self._undo:
            if self.on_bulk_done:
                self.on_bulk_done("Nothing to undo")
            return
        log = self._undo.pop()
        m = _source_model(self)
        keys = [c.key for c in m.cols]
        n = 0
        for rid, key, old in reversed(log):
            r = m.index_of(rid) if hasattr(m, "index_of") else -1
            if r < 0 or key not in keys:
                continue
            index = m.index(r, keys.index(key))
            if self.model() is not m:
                continue
            n += self._write(index, old, [])
        if self.on_bulk_done:
            self.on_bulk_done(f"Undid {n} cell change(s)")

    def edit_actions(self, menu):
        """Copy / Paste / Fill Down / Clear / Undo entries for a right-click menu."""
        from PySide6.QtGui import QKeySequence
        for text, fn, key in (("📄 Copy", self.copy_cells, QKeySequence.Copy),
                              ("📋 Paste", self.paste_cells, QKeySequence.Paste),
                              ("⬇ Fill Down", self.fill_down, QKeySequence("Ctrl+D")),
                              ("⌫ Clear cells", self.clear_cells, QKeySequence.Delete),
                              ("↶ Undo paste / fill / clear", self.undo_last, QKeySequence.Undo)):
            a = menu.addAction(text, fn)
            a.setShortcut(key)
        menu.addSeparator()

    # ---------------------------------------------------------------- keys
    def keyPressEvent(self, e):
        from PySide6.QtGui import QKeySequence
        if not self._is_editing():
            if e.matches(QKeySequence.Copy):
                self.copy_cells()
                return
            if e.matches(QKeySequence.Paste):
                self.paste_cells()
                return
            if e.matches(QKeySequence.Undo):
                self.undo_last()
                return
            if e.key() == Qt.Key_D and e.modifiers() == Qt.ControlModifier:
                self.fill_down()
                return
            if e.key() == Qt.Key_Delete and e.modifiers() == Qt.NoModifier:
                self.clear_cells()
                return
        super().keyPressEvent(e)
