"""Local (per-machine) configuration: database connection + user preferences.

Shared settings (company info, default tolerances) live in the database so every
workstation sees the same values. Only connection details and personal
preferences are stored locally.
"""
from __future__ import annotations

import getpass
import json
import os
import socket
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_NAME = "ADMI FAI Studio"
APP_VERSION = "1.9.1"
KEYRING_SERVICE = "ADMI_FAI_Studio"


def app_dir() -> Path:
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        d = base / "ADMI_FAI_Studio"
    else:
        d = Path.home() / ".fai_studio"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_dir() -> Path:
    d = app_dir() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def asset_path(*parts: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    p = base / "fai_studio" / "assets" / Path(*parts)
    if not p.exists():
        p = Path(__file__).resolve().parent / "assets" / Path(*parts)
    return p


def _keyring():
    try:
        import keyring  # type: ignore
        return keyring
    except Exception:
        return None


@dataclass
class LocalConfig:
    host: str = "localhost"
    port: int = 5432
    dbname: str = "fai_studio"
    user: str = "fai_app"
    password: str = ""
    sslmode: str = "prefer"
    user_display_name: str = field(default_factory=lambda: getpass.getuser())
    balloon_scale: float = 1.0
    last_export_dir: str = ""
    use_keyring: bool = True
    ui_scale: float = 1.0                 # Settings -> Display size (applied at start-up, per workstation)
    ask_requirement: bool = True          # ask for the requirement (with GD&T builder) when a balloon is added by hand
    form3_split: float = 0.58             # Form 3: share of the width given to the table (rest = drawing)
    form3_drawing: bool = True            # Form 3: drawing pane shown

    # ------------------------------------------------------------------
    @property
    def path(self) -> Path:
        return app_dir() / "config.json"

    @classmethod
    def load(cls) -> "LocalConfig":
        p = app_dir() / "config.json"
        cfg = cls()
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                for k, v in data.items():
                    if hasattr(cfg, k):
                        setattr(cfg, k, v)
            except Exception:
                pass
        if cfg.use_keyring and not cfg.password:
            kr = _keyring()
            if kr:
                try:
                    pw = kr.get_password(KEYRING_SERVICE, f"{cfg.user}@{cfg.host}")
                    if pw:
                        cfg.password = pw
                except Exception:
                    pass
        return cfg

    def save(self) -> None:
        data = asdict(self)
        kr = _keyring() if self.use_keyring else None
        if kr:
            try:
                kr.set_password(KEYRING_SERVICE, f"{self.user}@{self.host}", self.password)
                data["password"] = ""  # stored in the OS credential vault instead
            except Exception:
                pass
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def is_configured(self) -> bool:
        return (app_dir() / "config.json").exists()

    def conninfo(self) -> dict:
        return dict(
            host=self.host,
            port=int(self.port),
            dbname=self.dbname,
            user=self.user,
            password=self.password,
            sslmode=self.sslmode,
            connect_timeout=8,
            application_name=f"FAI Studio ({socket.gethostname()})",
        )


def hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return "unknown"
