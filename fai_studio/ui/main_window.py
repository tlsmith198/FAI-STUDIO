from __future__ import annotations

import threading

from PySide6.QtCore import QSortFilterProxyModel, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
                               QPushButton, QTableView, QTableWidget, QTableWidgetItem, QTabWidget, QToolBar,
                               QToolButton, QVBoxLayout, QWidget)

from typing import Optional

from ..config import APP_NAME, APP_VERSION, hostname
from ..db import ChangeListener, Database
from ..local_store import LocalStore
from .common import STATUS_COLORS, LiveBus, app_icon, fit_to_screen, guard, is_small_screen
from .dialogs import CompanyDialog, ConnectionDialog, DuplicateDialog, GageDialog, NewReportDialog
from .report_widget import ReportWidget
from .table_model import Col, RecordModel

LIST_COLS = [
    Col("report_number", "Report #", 120, "readonly"), Col("report_type", "Type", 110, "readonly"),
    Col("part_number", "Part Number", 150, "readonly"), Col("part_rev", "Rev", 45, "readonly"),
    Col("part_name", "Part Name", 200, "readonly"), Col("customer", "Customer", 130, "readonly"),
    Col("po_number", "PO", 100, "readonly"), Col("job_number", "Job", 80, "readonly"),
    Col("serial_number", "S/N", 80, "readonly"), Col("_progress", "Progress", 120, "readonly"),
    Col("status", "Status", 90, "readonly"), Col("_where", "Where", 170, "readonly"),
    Col("updated_by", "Updated By", 110, "readonly"),
    Col("updated_at", "Updated", 130, "readonly"),
]


class MainWindow(QMainWindow):
    def __init__(self, db: Optional[Database], cfg, store: Optional[LocalStore] = None):
        super().__init__()
        self.db = db                          # None = offline mode
        self.cfg = cfg
        self.store = store or LocalStore(cfg.user_display_name or "unknown")
        self.listener = None
        self.open_reports: dict[int, ReportWidget] = {}
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.setWindowIcon(app_icon())
        fit_to_screen(self, 1560, 940)

        tb = QToolBar("Main")
        tb.setMovable(False)
        self.addToolBar(tb)
        brand = QLabel("  <b style='color:#FF6A00; font-size:15px'>ADMI</b> "
                       "<span style='color:white'>FAI Studio</span>   ")
        tb.addWidget(brand)

        def act(text, fn, shortcut=None, tip=""):
            a = QAction(text, self)
            a.triggered.connect(fn)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            a.setToolTip(tip or text)
            tb.addAction(a)
            return a

        act("＋ New Report", self.new_report, "Ctrl+Shift+N", "Create a new FAI or inspection report")
        act("Open", self.open_selected)
        act("⎘ New Revision / Copy", self.duplicate)
        act("🗑 Delete", self.delete_report)
        act("⟳ Refresh", self.refresh_list, "F5")
        tb.addSeparator()
        self.act_offline = act("💼 Take Offline", self.take_offline,
                               tip="Copy the selected report to this PC so you can work on it without the server.\n"
                                   "Other workstations see it as read-only until you sync it back.")
        self.act_sync = act("⇅ Sync", self.sync_now, tip="Upload everything done offline on this PC and check the "
                                                          "reports back in")
        self.act_online = act("🔌 Go Online", self.toggle_online, tip="Connect to / disconnect from the server")
        tb.addSeparator()
        # everything that is set up once lives in one menu, so the toolbar fits a laptop screen
        menu = QMenu(self)

        def mact(text, fn, tip="", shortcut=None):
            a = QAction(text, self)
            a.triggered.connect(fn)
            a.setToolTip(tip or text)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            menu.addAction(a)
            return a
        self.act_gage = mact("🔧 Gage Library…", lambda: self._online_only(lambda: GageDialog(self.db, self).exec()))
        self.act_vendor = mact("📇 Vendor Library (CoC)…", self.vendor_library,
                               "Approved vendors and addresses printed on Certificates of Conformance")
        self.act_company = mact("🏢 Company, Logo && CoC Text…",
                                lambda: self._online_only(lambda: CompanyDialog(self.db, self).exec()))
        mact("📚 Drawing Reader Rules…", self.reader_rules, "Shared library of reading rules: teach the reader new "
                                                             "drawing formats without a software update")
        mact("🧠 OCR Learning…", self.ocr_learning, "What the reader has learned from the team's corrections to "
                                                     "OCR reads - shared by all workstations")
        mact("🧪 OCR Setup…", self.ocr_setup, "OCR preprocessing (OpenCV) for scanned drawings — shared by all "
                                               "workstations, with a before/after preview")
        menu.addSeparator()
        size_menu = menu.addMenu("🔍 Display Size")
        for pct in (80, 90, 100, 110, 125, 150, 175):
            a = QAction(f"{pct}%", self)
            a.setCheckable(True)
            a.setChecked(abs((self.cfg.ui_scale or 1.0) * 100 - pct) < 1)
            a.triggered.connect(lambda _=False, p=pct: self.set_display_size(p / 100))
            size_menu.addAction(a)
        mact("🔌 Connection…", self.edit_connection)
        self.act_release = mact("🔓 Release Check-out…", self.release_checkout,
                                "Admin: free a report checked out to another PC (e.g. a lost laptop). If that PC "
                                "syncs later, its work comes in as a separate copy.")
        menu.addSeparator()
        mact("ⓘ About", self.about)
        self.menu_btn = QToolButton()
        self.menu_btn.setText("⚙ Settings ▾")
        self.menu_btn.setMenu(menu)
        self.menu_btn.setPopupMode(QToolButton.InstantPopup)
        self.menu_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.menu_btn.setStyleSheet("QToolButton { color: white; padding: 4px 8px; } "
                                    "QToolButton::menu-indicator { image: none; }")
        tb.addWidget(self.menu_btn)

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.setCentralWidget(self.tabs)
        self.tabs.addTab(self._build_list(), "📋 Reports")
        self.tabs.tabBar().setTabButton(0, self.tabs.tabBar().ButtonPosition.RightSide, None)

        self.live_lbl = QLabel()
        self.statusBar().addPermanentWidget(QLabel(f"User: <b>{self.store.user}</b> @ {self.store.host}  ·  "
                                                   f"DB: {cfg.user}@{cfg.host}:{cfg.port}/{cfg.dbname}  "))
        self.statusBar().addPermanentWidget(self.live_lbl)
        self.bus = LiveBus()
        self.bus.event.connect(self._on_event)
        self.bus.status.connect(self._set_live)
        self._list_timer = QTimer(self)
        self._list_timer.setSingleShot(True)
        self._list_timer.timeout.connect(self.refresh_list)
        self._apply_mode()
        self.refresh_list()
        try:
            from .. import rules as _rules
            _rules.load_shared(self.db if self.db is not None else self.store)
        except Exception:
            pass
        if self.db is not None:
            from ..sync import refresh_snapshot
            refresh_snapshot(self.db, self.store)
            QTimer.singleShot(600, self._offer_sync)

    # ------------------------------------------------------------------ online / offline
    @property
    def online(self) -> bool:
        return self.db is not None

    def _apply_mode(self):
        if self.online:
            self._set_live(False, "Connecting live sync…")
            self.listener = ChangeListener(self.cfg, self.db.client_id, self.bus.event.emit, self.bus.status.emit)
            self.listener_thread = threading.Thread(target=self.listener.run, daemon=True)
            self.listener_thread.start()
            self.act_online.setText("🔌 Go Offline")
        else:
            if self.listener:
                self.listener.stop()
                self.listener = None
            self.live_lbl.setText("<span style='color:#B26B00'>●</span> <b>OFFLINE</b> — working on this PC; "
                                  "⇅ Sync when you are back on the network  ")
            self.act_online.setText("🔌 Go Online")
        for a in (self.act_offline, self.act_release, self.act_gage, self.act_company):
            a.setEnabled(self.online)
        n = len(self.store.offline_reports())
        self.act_sync.setText(f"⇅ Sync ({n})" if n else "⇅ Sync")

    def reader_rules(self):
        from .rules_dialog import ReaderRulesDialog
        ReaderRulesDialog(self.db if self.online else self.store, self).exec()

    def ocr_learning(self):
        from .ocr_learn_dialog import OcrLearningDialog
        OcrLearningDialog(self.db if self.online else self.store, self).exec()

    def vendor_library(self):
        from .dialogs import VendorDialog
        VendorDialog(self.db if self.online else self.store, self).exec()

    def set_display_size(self, scale: float):
        self.cfg.ui_scale = scale
        self.cfg.save()
        QMessageBox.information(self, "Display size", f"Display size set to {round(scale * 100)}% on this workstation.\n\n"
                                "Restart FAI Studio to apply it.")

    def _online_only(self, fn):
        if not self.online:
            QMessageBox.information(self, "Offline", "This needs the database server. Use 🔌 Go Online first.")
            return
        fn()

    def toggle_online(self):
        if self.online:
            if QMessageBox.question(self, "Go offline", "Disconnect from the server and work only on the reports "
                                    "on this PC?\n\nTake reports offline (💼) first if you need them.") \
                    != QMessageBox.Yes:
                return
            for rid in [r for r in self.open_reports if r > 0]:
                self.close_tab(self.tabs.indexOf(self.open_reports[rid]))
            try:
                self.db.close()
            except Exception:
                pass
            self.db = None
            self._apply_mode()
            self.refresh_list()
            return
        self.go_online()

    def go_online(self, quiet=False) -> bool:
        try:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            db = Database(self.cfg)
        except Exception as e:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            if not quiet:
                QMessageBox.warning(self, "Server not reachable", f"Still can't reach the database server:\n\n{e}")
            return False
        QApplication.restoreOverrideCursor()
        self.db = db
        from ..sync import refresh_snapshot
        refresh_snapshot(db, self.store)
        self._apply_mode()
        self.refresh_list()
        self._offer_sync()
        return True

    def _offer_sync(self):
        reps = self.store.offline_reports()
        pend = self.store.pending()
        if not self.online or not (reps or pend):
            return
        if QMessageBox.question(self, "Offline work on this PC",
                                f"This PC has {len(reps)} offline report(s) not yet uploaded to the server.\n\n"
                                "Sync them now?") == QMessageBox.Yes:
            self.sync_now()

    def take_offline(self):
        from ..sync import refresh_snapshot, take_offline
        rid = self.selected_report_id()
        if not rid and isinstance(self.tabs.currentWidget(), ReportWidget):
            rid = self.tabs.currentWidget().rid
        if not rid or rid < 0:
            QMessageBox.information(self, "Take offline", "Select a server report in the list first.")
            return
        if rid in self.open_reports:
            self.close_tab(self.tabs.indexOf(self.open_reports[rid]))
        loc = guard(self, lambda: take_offline(self.db, self.store, rid))
        if not loc:
            return
        refresh_snapshot(self.db, self.store)
        self._apply_mode()
        self.refresh_list()
        self.open_report(loc["id"])
        self.statusBar().showMessage(f"{loc['report_number']} is on this PC for offline work - other workstations "
                                     "see it read-only until you sync.", 8000)

    def sync_now(self):
        from ..sync import sync_all
        from .workers import run_with_progress
        if not self.online and not self.go_online():
            return
        reps = self.store.offline_reports()
        if not reps and not self.store.pending():
            QMessageBox.information(self, "Sync", "Nothing to sync - there is no offline work on this PC.")
            return
        dlg = SyncDialog(reps, self)
        if dlg.exec() != SyncDialog.Accepted:
            return
        for rid in [r for r in self.open_reports if r < 0]:
            self.close_tab(self.tabs.indexOf(self.open_reports[rid]))
        keep = dlg.keep_ids()
        res = run_with_progress(self, "Syncing with the server…",
                                lambda cb: sync_all(self.db, self.store, keep, cb))
        self._apply_mode()
        self.refresh_list()
        if res is not None:
            box = QMessageBox.warning if (res.conflicts or res.errors) else QMessageBox.information
            box(self, "Sync finished", res.summary())

    def release_checkout(self):
        from ..sync import release
        rid = self.selected_report_id()
        row = self.list_model.row_at(self.list_view.currentIndex().row()) if rid else None
        if not row or rid < 0 or not row.get("checked_out_host"):
            QMessageBox.information(self, "Release check-out", "Select a report that is checked out to another PC.")
            return
        if QMessageBox.warning(self, "Release check-out",
                               f"{row['report_number']} is checked out to {row['checked_out_by']} on "
                               f"{row['checked_out_host']}.\n\nRelease it only if that PC is lost or cannot sync. "
                               "If it syncs later, its offline work is uploaded as a SEPARATE copy (nothing is "
                               "overwritten).\n\nRelease now?", QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        guard(self, lambda: release(self.db, rid, force=True))
        self.refresh_list()

    # ------------------------------------------------------------------ report list
    def _build_list(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search part number, name, report #, customer, PO, job, serial…")
        self.search.textChanged.connect(lambda _: self._list_timer.start(250))
        self.status_filter = QComboBox()
        self.status_filter.addItems(["All statuses", "Draft", "In Review", "Approved", "Rejected"])
        self.status_filter.currentIndexChanged.connect(self.refresh_list)
        newb = QPushButton("＋ New Report")
        newb.setObjectName("primary")
        newb.clicked.connect(self.new_report)
        row.addWidget(self.search, 1)
        row.addWidget(self.status_filter)
        row.addWidget(newb)
        lay.addLayout(row)
        self.list_model = RecordModel(LIST_COLS, lambda *a: None, self._list_style)
        self.list_view = QTableView()
        self.list_view.setModel(self.list_model)
        self.list_view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.list_view.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list_view.setAlternatingRowColors(True)
        self.list_view.verticalHeader().setVisible(False)
        self.list_view.doubleClicked.connect(lambda _: self.open_selected())
        for i, c in enumerate(LIST_COLS):
            self.list_view.setColumnWidth(i, c.width)
        self.list_view.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self.list_view, 1)
        self.count_lbl = QLabel()
        lay.addWidget(self.count_lbl)
        return w

    def _list_style(self, row, key):
        if key == "_where" and row.get("_where"):
            return None, QColor("#B26B00" if row["_where"].startswith("🔒") else "#0B5CAD"), True
        if key == "status":
            return None, QColor(STATUS_COLORS.get(row["status"], "#333")), True
        if key == "_progress" and row.get("n_rej"):
            return None, QColor("#B42318"), True
        return None, None, False

    def refresh_list(self):
        local = self.store.list_reports(self.search.text())
        for r in local:
            r["_where"] = "💼 On this PC" if r.get("server_id") else "📤 New – not synced"
        local_srv = {r["server_id"] for r in local if r.get("server_id")}
        server = []
        if self.online:
            server = guard(self, lambda: self.db.list_reports(self.search.text()), quiet=True) or []
            server = [r for r in server if r["id"] not in local_srv]
            for r in server:
                h = r.get("checked_out_host") or ""
                r["_where"] = "" if not h else (f"🔒 Offline on {h}" + (f" ({r['checked_out_by']})"
                                                                         if r.get("checked_out_by") else ""))
        rows = local + server
        st = self.status_filter.currentText()
        if st != "All statuses":
            rows = [r for r in rows if r["status"] == st]
        for r in rows:
            n, d, rej = r["n_chars"], r["n_done"], r["n_rej"]
            r["_progress"] = f"{d}/{n}" + (f"  ({rej} rej)" if rej else "") if n else "—"
            if r["locked"]:
                r["status"] = r["status"]
        sel = self.selected_report_id()
        self.list_model.set_rows(rows)
        self.count_lbl.setText(f"{len(rows)} report(s)")
        if sel:
            i = self.list_model.index_of(sel)
            if i >= 0:
                self.list_view.selectRow(i)

    def selected_report_id(self):
        idx = self.list_view.currentIndex() if hasattr(self, "list_view") else None
        r = self.list_model.row_at(idx.row()) if idx is not None and idx.isValid() else None
        return r["id"] if r else None

    # ------------------------------------------------------------------ actions
    def open_report(self, rid: int) -> ReportWidget | None:
        if rid > 0:
            loc = self.store.by_server_id(rid)
            if loc:
                rid = loc["id"]                         # checked out to this PC: work on the offline copy
            elif not self.online:
                QMessageBox.information(self, "Offline", "That report is on the server. Go online to open it.")
                return None
        if rid in self.open_reports:
            self.tabs.setCurrentWidget(self.open_reports[rid])
            return self.open_reports[rid]
        src = self.store if rid < 0 else self.db
        w = guard(self, lambda: ReportWidget(src, rid, self.cfg, self))
        if not w:
            return None
        w.titleChanged.connect(self._retitle)
        self.open_reports[rid] = w
        i = self.tabs.addTab(w, "…")
        self._retitle(rid, f"{w.rep['report_number']} · {w.rep['part_number']}")
        self.tabs.setCurrentIndex(i)
        return w

    def _retitle(self, rid, title):
        w = self.open_reports.get(rid)
        if w:
            i = self.tabs.indexOf(w)
            if i >= 0:
                lock = "🔒 " if w.rep.get("locked") else ""
                self.tabs.setTabText(i, lock + title)

    def open_selected(self):
        rid = self.selected_report_id()
        if rid:
            self.open_report(rid)

    def close_tab(self, i):
        w = self.tabs.widget(i)
        if isinstance(w, ReportWidget):
            focus = self.focusWidget()
            if focus is not None and w.isAncestorOf(focus):
                focus.clearFocus()          # commit any in-progress field edit
            self.open_reports.pop(w.rid, None)
            self.tabs.removeTab(i)
            w.deleteLater()

    def new_report(self):
        dlg = NewReportDialog(self)
        if dlg.exec() != NewReportDialog.Accepted:
            return
        target = self.db if self.online else self.store
        rep = guard(self, lambda: target.create_report(dlg.values()))
        if not rep:
            return
        self._apply_mode()
        self.refresh_list()
        w = self.open_report(rep["id"])
        if w:
            QTimer.singleShot(100, lambda: w.run_intake(
                dlg.f_drawing.paths, dlg.f_photos.paths,
                dlg.f_parts.paths[0] if dlg.f_parts.paths else None,
                dlg.f_notes.paths[0] if dlg.f_notes.paths else None,
                dlg.auto_balloon.isChecked(), dlg.notes_from_drawing.isChecked()))

    def duplicate(self):
        rid = self.selected_report_id()
        if not rid and isinstance(self.tabs.currentWidget(), ReportWidget):
            rid = self.tabs.currentWidget().rid
        if not rid:
            QMessageBox.information(self, "Copy", "Select a report first.")
            return
        src = self.store if rid < 0 else self.db
        if src is None:
            QMessageBox.information(self, "Offline", "That report is on the server. Go online to copy it.")
            return
        rep = src.get_report(rid)
        dlg = DuplicateDialog(rep, self)
        if dlg.exec() != DuplicateDialog.Accepted:
            return
        new = guard(self, lambda: src.duplicate_report(rid, dlg.values(), dlg.results.isChecked()))
        if new:
            self.refresh_list()
            self.open_report(new["id"])

    def delete_report(self):
        rid = self.selected_report_id()
        if not rid:
            return
        if rid < 0:
            rep = self.store.get_report(rid)
            what = ("the offline copy of " + rep["report_number"] + ". Changes made offline are LOST; the server "
                    "copy stays as it was and is released at the next sync.") if rep.get("server_id") else \
                (rep["report_number"] + ", created offline and never uploaded. It is gone for good.")
            if QMessageBox.warning(self, "Discard offline report", f"Discard {what}", QMessageBox.Yes | QMessageBox.No) \
                    != QMessageBox.Yes:
                return
            if rid in self.open_reports:
                self.close_tab(self.tabs.indexOf(self.open_reports[rid]))
            guard(self, lambda: self.store.delete_report(rid))
            if self.online and rep.get("server_id"):
                from ..sync import sync_all
                guard(self, lambda: sync_all(self.db, self.store), quiet=True)     # releases the check-out now
            self._apply_mode()
            self.refresh_list()
            return
        rep = self.db.get_report(rid)
        if rep["locked"]:
            QMessageBox.information(self, "Locked", "Approved reports cannot be deleted. Reopen it first.")
            return
        if QMessageBox.warning(self, "Delete report",
                               f"Permanently delete {rep['report_number']} ({rep['part_number']}) and all its "
                               "characteristics and files for EVERY user?", QMessageBox.Yes | QMessageBox.No) \
                != QMessageBox.Yes:
            return
        if rid in self.open_reports:
            self.close_tab(self.tabs.indexOf(self.open_reports[rid]))
        guard(self, lambda: self.db.delete_report(rid))
        self.refresh_list()

    def edit_connection(self):
        dlg = ConnectionDialog(self.cfg, self)
        if dlg.exec() == ConnectionDialog.Accepted:
            QMessageBox.information(self, "Restart", "Restart FAI Studio to use the new connection settings.")

    def ocr_setup(self):
        from .ocr_prep_dialog import OcrPrepDialog
        OcrPrepDialog(self.db or self.store, self).exec()

    def about(self):
        QMessageBox.about(self, APP_NAME,
                          f"<h3>{APP_NAME} {APP_VERSION}</h3>"
                          "<p>AS9102 First Article Inspection and dimensional inspection reports with drawing "
                          "ballooning, shared live across workstations through PostgreSQL.</p>"
                          f"<p>Workstation: <code>{self.store.host}</code> · "
                          f"{'online' if self.online else 'offline'} · offline reports on this PC: "
                          f"{len(self.store.offline_reports())}</p>")

    # ------------------------------------------------------------------ live sync
    def _set_live(self, ok: bool, msg: str):
        if not self.online:
            return
        if ok:
            self._connects = getattr(self, "_connects", 0) + 1
            if self._connects > 1:            # reconnected: we may have missed changes while offline
                self.refresh_list()
                for w in list(self.open_reports.values()):
                    w.resync()
                msg += " (resynchronized)"
        color = "#146C2E" if ok else "#B42318"
        self.live_lbl.setText(f"<span style='color:{color}'>●</span> {msg}  ")

    def _on_event(self, ev: dict):
        self._list_timer.start(400)
        if ev.get("t") == "gages":                      # Gage Library changed (any workstation): recolour cells
            for rw in self.open_reports.values():
                rw._gage_map = None
                try:
                    rw.char_view.viewport().update()
                except Exception:
                    pass
        rid = ev.get("report_id")
        if ev.get("own"):
            return
        if rid in self.open_reports:
            self.open_reports[rid].on_remote_event(ev)
        if ev.get("t") == "reports" and ev.get("op") == "UPDATE" and rid in self.open_reports:
            self.statusBar().showMessage(f"Report {self.open_reports[rid].rep['report_number']} updated by another "
                                         "user", 4000)

    def closeEvent(self, e):
        f = self.focusWidget()
        if f is not None:
            f.clearFocus()
        if self.listener:
            self.listener.stop()
        super().closeEvent(e)


class SyncDialog(QDialog):
    """What will be uploaded; per checked-out report, whether to keep working on it offline afterwards."""
    def __init__(self, reports: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Sync offline work with the server")
        fit_to_screen(self, 760, 380)
        self.reports = reports
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("These reports on this PC will be uploaded to the server in one step each "
                             "(all or nothing), with their offline history. Reports created offline get their "
                             "real report number."))
        self.t = QTableWidget(len(reports), 4)
        self.t.setHorizontalHeaderLabels(["Report", "Part", "What happens", "Keep on this PC"])
        self.t.verticalHeader().setVisible(False)
        self.keep = []
        for i, r in enumerate(reports):
            self.t.setItem(i, 0, QTableWidgetItem(r["report_number"]))
            self.t.setItem(i, 1, QTableWidgetItem(f"{r['part_number']}  {r['part_name']}"))
            self.t.setItem(i, 2, QTableWidgetItem("update the server report and check it in" if r.get("server_id")
                                                  else "create it on the server"))
            cb = QCheckBox("keep working offline")
            cb.setToolTip("Upload now but leave it checked out to this PC (e.g. mid-way through a long inspection)")
            self.t.setCellWidget(i, 3, cb)
            self.keep.append(cb)
        self.t.resizeColumnsToContents()
        self.t.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self.t, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Sync now")
        bb.button(QDialogButtonBox.Ok).setObjectName("primary")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def keep_ids(self) -> set:
        return {r["id"] for r, cb in zip(self.reports, self.keep) if cb.isChecked()}
