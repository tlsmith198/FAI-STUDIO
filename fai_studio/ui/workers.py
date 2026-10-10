"""Run slow work (drawing analysis / OCR) off the UI thread with a progress dialog."""
from __future__ import annotations

import threading
import time
import traceback
from typing import Any, Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QProgressDialog


def run_with_progress(parent, title: str, fn: Callable[[Callable[[str, float], None]], Any]) -> Any:
    state = {"msg": title, "frac": 0.0, "done": False, "result": None, "error": None}

    def cb(msg: str, frac: float):
        state["msg"] = msg
        state["frac"] = max(0.0, min(1.0, frac))

    def work():
        try:
            state["result"] = fn(cb)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            state["error"] = e
        finally:
            state["done"] = True

    t = threading.Thread(target=work, daemon=True)
    t.start()
    dlg = QProgressDialog(title, None, 0, 100, parent)
    dlg.setWindowTitle("Working…")
    dlg.setWindowModality(Qt.WindowModal)
    dlg.setMinimumDuration(250)
    dlg.setMinimumWidth(420)
    while not state["done"]:
        dlg.setLabelText(state["msg"])
        dlg.setValue(int(state["frac"] * 100))
        QApplication.processEvents()
        time.sleep(0.04)
    dlg.setValue(100)
    dlg.close()
    if state["error"] is not None:
        raise state["error"]
    return state["result"]
