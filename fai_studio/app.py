from __future__ import annotations

import os
import sys
import traceback

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from .config import APP_NAME, LocalConfig
from .db import Database

SCHEMA_VERSION = "7"


def _excepthook(t, v, tb):
    traceback.print_exception(t, v, tb)
    try:
        QMessageBox.critical(None, "Unexpected error", f"{t.__name__}: {v}")
    except Exception:
        pass


def _force_light_theme(app):
    """The UI is designed light. Windows dark mode would otherwise give white text on the light
    backgrounds (invisible labels / field text), so pin a light palette regardless of OS theme."""
    from PySide6.QtGui import QColor, QPalette
    try:
        app.styleHints().setColorScheme(Qt.ColorScheme.Light)      # Qt 6.8+
    except Exception:
        pass
    pal = QPalette()
    text, base, window = QColor("#111417"), QColor("#FFFFFF"), QColor("#F7F7F8")
    for group in (QPalette.Active, QPalette.Inactive):
        pal.setColor(group, QPalette.Window, window)
        pal.setColor(group, QPalette.WindowText, text)
        pal.setColor(group, QPalette.Base, base)
        pal.setColor(group, QPalette.AlternateBase, QColor("#FAFAFB"))
        pal.setColor(group, QPalette.Text, text)
        pal.setColor(group, QPalette.Button, QColor("#FFFFFF"))
        pal.setColor(group, QPalette.ButtonText, text)
        pal.setColor(group, QPalette.ToolTipBase, QColor("#FFFFE8"))
        pal.setColor(group, QPalette.ToolTipText, text)
        pal.setColor(group, QPalette.PlaceholderText, QColor("#8A8F94"))
        pal.setColor(group, QPalette.Highlight, QColor("#FF6A00"))
        pal.setColor(group, QPalette.HighlightedText, QColor("#FFFFFF"))
        pal.setColor(group, QPalette.BrightText, QColor("#B42318"))
        pal.setColor(group, QPalette.Link, QColor("#0B5CAD"))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, QColor("#9A9EA2"))
    pal.setColor(QPalette.Disabled, QPalette.Base, QColor("#F0F0F0"))
    pal.setColor(QPalette.Disabled, QPalette.Button, QColor("#F0F0F0"))
    app.setPalette(pal)


def _gdt_font_fallback(app):
    """Make GD&T symbols (⌖ ⌓ Ⓜ ⏊ ⫽ …) render everywhere in the UI: fall back to fonts that contain them."""
    from PySide6.QtGui import QFontDatabase
    from .config import asset_path
    fams = []
    try:
        fid = QFontDatabase.addApplicationFont(str(asset_path("fonts", "FreeSerif.ttf")))
        fams = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
    except Exception:
        pass
    f = app.font()
    f.setFamilies([f.family(), "Segoe UI Symbol", "Segoe UI", "Cambria Math"] + list(fams))
    app.setFont(f)


OFFLINE = "offline"


def connect_interactive(cfg: LocalConfig):
    """Returns a Database, OFFLINE (work on this PC's offline reports) or None (quit)."""
    from .ui.dialogs import ConnectionDialog
    first = not cfg.is_configured()
    while True:
        if not first:
            try:
                return Database(cfg)
            except Exception as e:  # noqa: BLE001
                box = QMessageBox(QMessageBox.Warning, APP_NAME,
                                  f"Cannot connect to the database server:\n\n{e}\n\n"
                                  "You can work offline on the reports saved on this PC and create new ones; "
                                  "everything uploads when you ⇅ Sync back on the network.")
                retry = box.addButton("Retry", QMessageBox.AcceptRole)
                offline = box.addButton("💼 Work Offline", QMessageBox.AcceptRole)
                settings = box.addButton("Connection Settings…", QMessageBox.ActionRole)
                box.addButton("Quit", QMessageBox.RejectRole)
                box.setDefaultButton(offline)
                box.exec()
                clicked = box.clickedButton()
                if clicked is retry:
                    continue
                if clicked is offline:
                    return OFFLINE
                if clicked is not settings:
                    return None
        first = False
        dlg = ConnectionDialog(cfg)
        if dlg.exec() != ConnectionDialog.Accepted:
            return None


def _show_main(w) -> None:
    """Open at a size that fits this monitor: maximised on laptops / small screens, otherwise the preferred size
    capped to the usable screen area and centred."""
    from .ui.common import available_geometry, is_small_screen
    if is_small_screen(w):
        w.showMaximized()
        return
    g = available_geometry(w)
    w.move(g.x() + (g.width() - w.width()) // 2, g.y() + (g.height() - w.height()) // 2)
    w.show()


def main() -> int:
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    try:                                       # Settings -> Display size (per workstation) - must be set before Qt starts
        _scale = float(LocalConfig.load().ui_scale or 1.0)
        if abs(_scale - 1.0) > 0.01 and "QT_SCALE_FACTOR" not in os.environ:
            os.environ["QT_SCALE_FACTOR"] = f"{max(0.6, min(2.0, _scale)):.2f}"
    except Exception:
        pass
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("ADMI")
    app.setStyle("Fusion")
    _force_light_theme(app)
    _gdt_font_fallback(app)
    from .ui.common import STYLE, app_icon
    app.setStyleSheet(STYLE)
    app.setWindowIcon(app_icon())
    sys.excepthook = _excepthook

    cfg = LocalConfig.load()
    db = connect_interactive(cfg)
    if db is None:
        return 0
    if db == OFFLINE:
        from .ui.main_window import MainWindow
        w = MainWindow(None, cfg)
        try:
            from .branding import apply_icon
            apply_icon(w.store.get_meta(), w)
        except Exception:
            pass
        _show_main(w)
        return app.exec()
    try:
        if not db.schema_exists():
            if QMessageBox.question(None, APP_NAME, "This database has no FAI Studio tables yet.\n\n"
                                    "Create them now?") != QMessageBox.Yes:
                return 0
            db.init_schema()
        elif int((db.get_meta().get("schema_version") or "0").strip() or 0) < int(SCHEMA_VERSION):
            db.init_schema()
    except Exception as e:  # noqa: BLE001
        QMessageBox.critical(None, APP_NAME, f"Database setup failed:\n\n{e}\n\n"
                             "The first workstation must connect with a user that can create tables "
                             "(see server_setup/README).")
        return 1

    from .ui.main_window import MainWindow
    w = MainWindow(db, cfg)
    try:
        from .branding import apply_icon
        apply_icon(db.get_meta(), w)            # company icon chosen in Company settings (shared)
    except Exception:
        pass
    _show_main(w)
    rc = app.exec()
    if w.db is not None:
        w.db.close()
    return rc
