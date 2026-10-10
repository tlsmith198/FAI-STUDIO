"""Company logo (on reports) and application icon, both shared by every workstation through app_meta."""
from __future__ import annotations

import base64
import io
import subprocess
import sys
from typing import Optional

from PIL import Image

LOGO_KEY = "company_logo"          # PNG, base64
ICON_KEY = "app_icon"              # PNG 256x256, base64


def image_file_to_b64(path: str, max_w: int, max_h: int, square: bool = False) -> str:
    """Load any common image (PNG, JPG, BMP, GIF, ICO, TIFF), shrink it and return base64 PNG."""
    im = Image.open(path)              # .ico files open at their largest stored size
    im = im.convert("RGBA")
    # trim fully transparent margins so the logo sits tight in its box
    bbox = im.getbbox()
    if bbox:
        im = im.crop(bbox)
    if square:
        side = max(im.size)
        canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        canvas.paste(im, ((side - im.width) // 2, (side - im.height) // 2))
        im = canvas.resize((max_w, max_w), Image.LANCZOS)
    else:
        im.thumbnail((max_w, max_h), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def b64_to_bytes(b64: Optional[str]) -> Optional[bytes]:
    if not b64:
        return None
    try:
        return base64.b64decode(b64)
    except Exception:
        return None


def logo_bytes(meta: dict) -> Optional[bytes]:
    return b64_to_bytes(meta.get(LOGO_KEY))


def icon_bytes(meta: dict) -> Optional[bytes]:
    return b64_to_bytes(meta.get(ICON_KEY))


def qicon_from_meta(meta: dict):
    """The custom application icon, or None to keep the built-in one."""
    data = icon_bytes(meta)
    if not data:
        return None
    from PySide6.QtGui import QIcon, QPixmap
    pm = QPixmap()
    if not pm.loadFromData(data):
        return None
    return QIcon(pm)


def apply_icon(meta: dict, *widgets) -> None:
    """Use the shared icon for the running app (window, taskbar) and, on Windows, the desktop shortcut."""
    from PySide6.QtWidgets import QApplication

    from .ui.common import app_icon
    icon = qicon_from_meta(meta) or app_icon()
    app = QApplication.instance()
    if app:
        app.setWindowIcon(icon)
    for w in widgets:
        if w is not None:
            w.setWindowIcon(icon)
    _update_windows_shortcut(icon_bytes(meta))


def _update_windows_shortcut(png: Optional[bytes]) -> None:
    """Best effort: save the icon as .ico next to the settings and point the desktop shortcut at it."""
    if not sys.platform.startswith("win"):
        return
    from .config import app_dir
    ico = app_dir() / "app_icon.ico"
    try:
        if png:
            im = Image.open(io.BytesIO(png)).convert("RGBA")
            im.save(ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
            target = str(ico)
        else:
            if ico.exists():
                ico.unlink()
            target = ""
        ps = ("$p=[Environment]::GetFolderPath('Desktop')+'\\FAI Studio.lnk'; if (Test-Path $p) { "
              "$s=(New-Object -ComObject WScript.Shell).CreateShortcut($p); "
              f"$s.IconLocation='{target + ',0' if target else ''}'; $s.Save() }}")
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass
