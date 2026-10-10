"""Offline store: a local SQLite copy of the reports this PC is working on without the server.

* Reports checked out from the server (Take Offline) and reports created while offline live here.
* Every id in this store is NEGATIVE, so a local record can never be mistaken for a server record
  (the UI routes negative report ids to this store, positive ones to PostgreSQL).
* The tables are generated from schema.sql, so they always match the server's columns.
* Every change is written to a local audit_log with its real timestamp; on sync those entries are
  uploaded to the server's audit trail marked "(offline)".
* The interface matches db.Database, so every screen (ballooning, results, photos, PDF export) works
  unchanged on an offline report.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .config import app_dir, cache_dir, hostname
from .db import CHILD_TABLES, EDITABLE, SCHEMA_FILE, ConflictError, LockedError, _s

LOCAL_TABLES = ("reports", "components", "materials", "attachments", "characteristics", "coc_items", "gages",
                "vendors", "audit_log", "app_meta")
BOOL_COLS = {"locked", "include_in_report", "active", "checked_out", "approved"}
TS_COLS = {"updated_at", "created_at", "uploaded_at", "at", "checked_out_at"}
OFFLINE_ONLY_MSG = ("This needs the database server. Reconnect (🔌 Go Online) and try again - offline you can "
                    "create reports, balloon, enter results and export PDFs; approval and shared settings are "
                    "done online.")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sqlite_tables(schema_sql: str) -> dict[str, list[str]]:
    """Translate the PostgreSQL schema (CREATE TABLE + ALTER ... ADD COLUMN) into SQLite column definitions."""
    out: dict[str, list[str]] = {}
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\n\);", schema_sql, re.S):
        name, body = m.group(1), m.group(2)
        if name not in LOCAL_TABLES:
            continue
        cols = []
        for line in body.splitlines():
            line = line.split("--")[0].strip().rstrip(",")
            if not line or line.upper().startswith(("PRIMARY KEY", "UNIQUE", "CONSTRAINT", "CHECK")):
                continue
            cols.append(_col(line))
        out[name] = cols
    for m in re.finditer(r"ALTER TABLE (\w+) ADD COLUMN IF NOT EXISTS (.+?);", schema_sql):
        if m.group(1) in out:
            out[m.group(1)].append(_col(m.group(2)))
    # local-only bookkeeping
    for t in ("reports", "components", "materials", "attachments", "characteristics", "coc_items", "gages",
              "vendors"):
        if t in out:
            out[t].append("server_id INTEGER")
    out["reports"] += ["origin TEXT NOT NULL DEFAULT 'offline'",        # 'server' (checked out) | 'offline' (new)
                       "checked_out_at_local TEXT", "base_server_version INTEGER"]
    return {t: [c for c in cols if c] for t, cols in out.items()}


def _sqlite_ddl(schema_sql: str) -> list[str]:
    stmts = [f"CREATE TABLE IF NOT EXISTS {t} ({', '.join(cols)})" for t, cols in _sqlite_tables(schema_sql).items()]
    stmts.append("CREATE TABLE IF NOT EXISTS pending (id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT, "
                 "server_report_id INTEGER, detail TEXT, at TEXT)")
    stmts.append("CREATE TABLE IF NOT EXISTS local_meta (key TEXT PRIMARY KEY, value TEXT)")
    return stmts


def _col(defn: str) -> str:
    parts = defn.split()
    name, rest = parts[0], " ".join(parts[1:])
    low = rest.lower()
    if low.startswith(("serial", "bigserial")):
        return f"{name} INTEGER PRIMARY KEY"
    typ = ("INTEGER" if re.match(r"(integer|bigint|boolean|smallint)", low) else
           "REAL" if re.match(r"(double|real|numeric|float)", low) else
           "BLOB" if low.startswith("bytea") else "TEXT")
    default = ""
    md = re.search(r"default\s+('([^']*)'|[\w.()-]+)", rest, re.I)
    if md:
        v = md.group(1)
        if v.lower() == "now()":
            default = ""                          # timestamps are always written explicitly
        elif v.lower() in ("true", "false"):
            default = f" DEFAULT {1 if v.lower() == 'true' else 0}"
        else:
            default = f" DEFAULT {v}"
    notnull = " NOT NULL" if "not null" in low and default else ""
    return f"{name} {typ}{notnull}{default}"


class LocalStore:
    is_local = True

    def __init__(self, user: str, path: Optional[Path] = None):
        self.path = Path(path) if path else app_dir() / "offline_reports.sqlite3"
        self.user = user or "unknown"
        self.host = hostname()
        self.client_id = uuid.uuid4().hex
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=OFF")
        for st in _sqlite_ddl(SCHEMA_FILE.read_text(encoding="utf-8")):
            self.conn.execute(st)
        self._add_missing_columns()

    def _add_missing_columns(self):
        """An older offline file after an app update: add columns the schema gained since."""
        for t, cols in _sqlite_tables(SCHEMA_FILE.read_text(encoding="utf-8")).items():
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({t})")}
            for col in cols:
                if col.split()[0] not in have and "PRIMARY KEY" not in col:
                    self.conn.execute(f"ALTER TABLE {t} ADD COLUMN {col}")

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ helpers
    def _rows(self, q, params=()) -> list[dict]:
        with self._lock:
            out = []
            for r in self.conn.execute(q, params).fetchall():
                d = dict(r)
                for k in BOOL_COLS & d.keys():
                    d[k] = bool(d[k])
                for k in TS_COLS & d.keys():
                    if isinstance(d[k], str) and d[k]:
                        try:
                            d[k] = datetime.fromisoformat(d[k].replace(" ", "T", 1) if "T" not in d[k] else d[k])
                            if d[k].tzinfo is None:
                                d[k] = d[k].astimezone()
                        except ValueError:
                            pass
                out.append(d)
            return out

    def _one(self, q, params=()) -> Optional[dict]:
        rows = self._rows(q, params)
        return rows[0] if rows else None

    def _new_id(self, table: str) -> int:
        r = self.conn.execute(f"SELECT min(id) FROM {table}").fetchone()[0]
        return min(-1, (r or 0) - 1)

    def _tx(self):
        return _Tx(self)

    def _audit(self, report_id, table, record_id, action, field=None, old=None, new=None):
        self.conn.execute(
            "INSERT INTO audit_log(id, report_id, table_name, record_id, action, field, old_value, new_value, "
            "user_name, host, at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (self._new_id("audit_log"), report_id, table, record_id, action, field, _s(old), _s(new), self.user,
             self.host, _now()))

    def _touch(self, report_id):
        self.conn.execute("UPDATE reports SET updated_at=?, updated_by=? WHERE id=?", (_now(), self.user, report_id))

    def _check_unlocked(self, report_id):
        r = self._one("SELECT locked, status FROM reports WHERE id=?", (report_id,))
        if r and r["locked"]:
            raise LockedError(f"Report is {r['status']} and locked.")

    @staticmethod
    def _clean(table, values):
        return {k: (int(v) if isinstance(v, bool) else v) for k, v in values.items() if k in EDITABLE[table]}

    # ------------------------------------------------------------------ meta (snapshot of the server's settings)
    def get_meta(self) -> dict:
        return {r["key"]: r["value"] for r in self._rows("SELECT key, value FROM app_meta")}

    def set_meta(self, values: dict) -> None:
        raise PermissionError(OFFLINE_ONLY_MSG)

    def snapshot_meta(self, meta: dict, gages: list[dict], vendors: Optional[list] = None) -> None:
        """Keep a copy of the company settings, gage list and vendor list for offline use."""
        with self._tx():
            if vendors is not None:
                self.conn.execute("DELETE FROM vendors")
                for v in vendors:
                    self.conn.execute("INSERT INTO vendors(id, name, address, cage, vendor_type, approved, notes, "
                                      "active, updated_by, updated_at, version, server_id) VALUES "
                                      "(?,?,?,?,?,?,?,?,?,?,?,?)",
                                      (-int(v["id"]), v["name"], v["address"], v["cage"], v["vendor_type"],
                                       int(bool(v["approved"])), v["notes"], int(bool(v["active"])),
                                       v["updated_by"], str(v["updated_at"]), v["version"], v["id"]))
            self.conn.execute("DELETE FROM app_meta")
            for k, v in meta.items():
                self.conn.execute("INSERT INTO app_meta(key, value) VALUES (?,?)", (k, v))
            self.conn.execute("DELETE FROM gages")
            for g in gages:
                self.conn.execute("INSERT INTO gages(id, gage_id, description, gage_type, cal_due, active, "
                                  "updated_by, updated_at, version, server_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                  (-int(g["id"]), g["gage_id"], g["description"], g["gage_type"], g["cal_due"],
                                   int(bool(g["active"])), g["updated_by"], str(g["updated_at"]), g["version"],
                                   g["id"]))

    def local_meta(self, key, default=None):
        r = self.conn.execute("SELECT value FROM local_meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    # ---- drawing reader rules: read-only snapshot of the server library
    def reader_rules(self) -> list[dict]:
        import json
        try:
            return json.loads(self.local_meta("reader_rules_json", "[]") or "[]")
        except Exception:
            return []

    def snapshot_rules(self, rows: list[dict]) -> None:
        import json
        self.set_local_meta("reader_rules_json", json.dumps(rows, default=str, ensure_ascii=False))

    # ---- OCR learning memory: the active fixes are applied offline too; learning itself happens on the server
    def ocr_learned(self, status=None) -> list[dict]:
        import json
        try:
            rows = json.loads(self.local_meta("ocr_learned_json", "[]") or "[]")
        except Exception:
            rows = []
        return [r for r in rows if not status or r.get("status") == status]

    def ocr_memory(self):
        from .ocr_learn import Memory
        return Memory(self.ocr_learned("active"))

    def snapshot_ocr_learned(self, rows: list[dict]) -> None:
        import json
        self.set_local_meta("ocr_learned_json", json.dumps(rows, default=str, ensure_ascii=False))

    def ocr_learning_settings(self):
        return False, 2

    def learn_ocr_correction(self, *a, **k):
        return []                  # corrections made offline are not learned (the memory lives on the server)

    def ocr_fix_feedback(self, *a, **k):
        return None

    def save_rule(self, *a, **k):
        raise PermissionError(OFFLINE_ONLY_MSG)

    delete_rule = add_rule_example = update_rule_example = delete_rule_example = save_rule
    save_ocr_learned = delete_ocr_learned = save_rule

    def rule_log(self, *a, **k):
        return []

    def rule_examples(self, *a, **k):
        return []

    def set_local_meta(self, key, value):
        self.conn.execute("INSERT INTO local_meta(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET "
                          "value=excluded.value", (key, value))

    # ------------------------------------------------------------------ audit
    def audit_for(self, report_id: int, limit: int = 2000) -> list[dict]:
        return self._rows("SELECT * FROM audit_log WHERE report_id=? ORDER BY at DESC, id ASC LIMIT ?",
                          (report_id, limit))

    # ------------------------------------------------------------------ reports
    def list_reports(self, search: str = "") -> list[dict]:
        like = f"%{search.strip()}%"
        rows = self._rows(
            """SELECT r.*, (SELECT count(*) FROM characteristics c WHERE c.report_id=r.id) AS n_chars,
                      (SELECT count(*) FROM characteristics c WHERE c.report_id=r.id AND c.disposition <> '') AS n_done,
                      (SELECT count(*) FROM characteristics c WHERE c.report_id=r.id AND c.disposition='Reject') AS n_rej
               FROM reports r
               WHERE ?='' OR r.part_number LIKE ? OR r.part_name LIKE ? OR r.report_number LIKE ? OR r.customer LIKE ?
                  OR r.po_number LIKE ? OR r.job_number LIKE ? OR r.serial_number LIKE ?
               ORDER BY r.updated_at DESC""", (search.strip(), like, like, like, like, like, like, like))
        return rows

    def get_report(self, report_id: int) -> Optional[dict]:
        return self._one("SELECT * FROM reports WHERE id=?", (report_id,))

    def create_report(self, values: dict) -> dict:
        from .report_types import number_prefix
        values = self._clean("reports", values)
        values.setdefault("prepared_by", self.user)
        values.setdefault("prepared_date", datetime.now().strftime("%Y-%m-%d"))
        with self._tx():
            rid = self._new_id("reports")
            n = self.conn.execute("SELECT count(*) FROM reports WHERE origin='offline'").fetchone()[0] + 1
            host = re.sub(r"[^A-Z0-9]", "", self.host.upper())[:10] or "PC"
            values["report_number"] = values.get("report_number") or \
                f"{number_prefix(values.get('report_type', 'AS9102 FAI'))}-TMP-{host}-{n:03d}"
            cols = ["id"] + list(values) + ["created_by", "created_at", "updated_by", "updated_at", "origin"]
            vals = [rid] + list(values.values()) + [self.user, _now(), self.user, _now(), "offline"]
            self.conn.execute(f"INSERT INTO reports ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
            self._audit(rid, "reports", rid, "CREATE", None, None, values["report_number"])
        return self.get_report(rid)

    def delete_report(self, report_id: int) -> None:
        rep = self.get_report(report_id)
        with self._tx():
            if rep and rep.get("server_id"):
                # discarding a checked-out copy: release the server check-out at the next sync
                self.conn.execute("INSERT INTO pending(action, server_report_id, detail, at) VALUES (?,?,?,?)",
                                  ("release", rep["server_id"], rep["report_number"], _now()))
            for t in CHILD_TABLES:
                self.conn.execute(f"DELETE FROM {t} WHERE report_id=?", (report_id,))
            self.conn.execute("DELETE FROM audit_log WHERE report_id=?", (report_id,))
            self.conn.execute("DELETE FROM reports WHERE id=?", (report_id,))
        self.vacuum_soon()

    def vacuum_soon(self):
        """Deleted drawings must not linger in the file (CUI): reclaim the space right away."""
        try:
            with self._lock:
                self.conn.execute("VACUUM")
        except Exception:
            pass

    def duplicate_report(self, report_id: int, new_values: dict, copy_results: bool = False) -> dict:
        src = self.get_report(report_id)
        base = {k: src[k] for k in EDITABLE["reports"] if k not in (
            "status", "locked", "reviewed_by", "reviewed_date", "customer_approval", "customer_approval_date",
            "report_number", "prepared_by", "prepared_date")}
        base.update(new_values)
        new = self.create_report(base)
        nid = new["id"]
        with self._tx():
            amap = {}
            for a in self._rows("SELECT * FROM attachments WHERE report_id=? ORDER BY id", (report_id,)):
                aid = self._new_id("attachments")
                amap[a["id"]] = aid
                self.conn.execute(
                    "INSERT INTO attachments(id, report_id, kind, filename, mime, size_bytes, sha256, page_count, "
                    "description, include_in_report, seq, data, uploaded_by, uploaded_at, updated_by, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (aid, nid, a["kind"], a["filename"], a["mime"], a["size_bytes"], a["sha256"], a["page_count"],
                     a["description"], int(a["include_in_report"]), a["seq"], a["data"], self.user, _now(),
                     self.user, _now()))
            for t in ("components", "materials", "characteristics", "coc_items"):
                for r in self._rows(f"SELECT * FROM {t} WHERE report_id=?", (report_id,)):
                    vals = {k: r[k] for k in EDITABLE[t]}
                    if t == "coc_items" and not copy_results:
                        vals["cert_lot"] = ""
                    if t == "characteristics":
                        if not copy_results:
                            vals.update(results="", disposition="", nonconformance="")
                        vals["balloon_attachment_id"] = amap.get(r["balloon_attachment_id"])
                    self._insert_row(t, nid, vals)
        return self.get_report(nid)

    # ------------------------------------------------------------------ child rows
    def rows(self, table: str, report_id: int) -> list[dict]:
        assert table in CHILD_TABLES
        order = {"components": "seq, id DESC", "materials": "seq, id DESC", "characteristics": "char_no, id DESC",
                 "attachments": "seq, id DESC", "coc_items": "seq, id DESC"}[table]
        cols = "*" if table != "attachments" else ", ".join(
            c for c in self._colnames("attachments") if c != "data")
        return self._rows(f"SELECT {cols} FROM {table} WHERE report_id=? ORDER BY {order}", (report_id,))

    def _colnames(self, table):
        return [r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")]

    def row(self, table: str, row_id: int) -> Optional[dict]:
        r = self._one(f"SELECT * FROM {table} WHERE id=?", (row_id,))
        if r:
            r.pop("data", None)
        return r

    def _insert_row(self, table, report_id, values) -> int:
        values = self._clean(table, values)
        rid = self._new_id(table)
        cols = ["id", "report_id"] + list(values) + ["updated_by", "updated_at", "version"]
        vals = [rid, report_id] + list(values.values()) + [self.user, _now(), 1]
        self.conn.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
        return rid

    def insert(self, table: str, report_id: Optional[int], values: dict) -> dict:
        if table in ("gages", "vendors"):
            raise PermissionError(OFFLINE_ONLY_MSG)
        with self._tx():
            self._check_unlocked(report_id)
            rid = self._insert_row(table, report_id, values)
            self._audit(report_id, table, rid, "INSERT", None, None,
                        json.dumps({k: _s(v) for k, v in self._clean(table, values).items()})[:2000])
            self._touch(report_id)
        return self.row(table, rid)

    def insert_many(self, table: str, report_id: int, rows: list[dict]) -> list[dict]:
        ids = []
        with self._tx():
            self._check_unlocked(report_id)
            for v in rows:
                ids.append(self._insert_row(table, report_id, v))
            self._audit(report_id, table, None, "IMPORT", None, None, f"{len(rows)} rows")
            self._touch(report_id)
        return [self.row(table, i) for i in ids]

    def update_fields(self, table: str, row_id: int, changes: dict, expected: Optional[dict] = None,
                      force: bool = False) -> dict:
        if table in ("gages", "vendors"):
            raise PermissionError(OFFLINE_ONLY_MSG)
        for k in changes:
            if k not in EDITABLE[table]:
                raise ValueError(f"Field {k} not editable on {table}")
        with self._tx():
            cur = self._one(f"SELECT * FROM {table} WHERE id=?", (row_id,))
            if cur is None:
                raise ConflictError("This record no longer exists.", None)
            report_id = cur["id"] if table == "reports" else cur["report_id"]
            if table == "reports":
                if cur["locked"] and not (set(changes) <= {"status", "locked", "reviewed_by", "reviewed_date",
                                                           "customer_approval", "customer_approval_date"}):
                    raise LockedError(f"Report is {cur['status']} and locked.")
                if changes.get("locked") or changes.get("status") == "Approved":
                    raise PermissionError(OFFLINE_ONLY_MSG)
            else:
                self._check_unlocked(report_id)
            changed = {k: v for k, v in changes.items() if _s(cur.get(k)) != _s(v)}
            if changed:
                sets = ", ".join(f"{k}=?" for k in changed) + ", updated_by=?, updated_at=?, version=version+1"
                self.conn.execute(f"UPDATE {table} SET {sets} WHERE id=?",
                                  [int(v) if isinstance(v, bool) else v for v in changed.values()]
                                  + [self.user, _now(), row_id])
                for k, v in changed.items():
                    self._audit(report_id, table, row_id, "UPDATE", k, cur.get(k), v)
                if table != "reports":
                    self._touch(report_id)
        return self.row(table, row_id) if table != "reports" else self.get_report(row_id)

    def update_field(self, table, row_id, field, value, expected_old=None, force=False) -> dict:
        return self.update_fields(table, row_id, {field: value})

    def delete(self, table: str, row_id: int) -> None:
        with self._tx():
            cur = self._one(f"SELECT * FROM {table} WHERE id=?", (row_id,))
            if not cur:
                return
            rid = cur.get("report_id")
            self._check_unlocked(rid)
            if table == "attachments":
                self.conn.execute("UPDATE characteristics SET balloon_attachment_id=NULL, balloon_page=NULL, "
                                  "balloon_x=NULL, balloon_y=NULL WHERE balloon_attachment_id=?", (row_id,))
            self.conn.execute(f"DELETE FROM {table} WHERE id=?", (row_id,))
            summary = {k: _s(v) for k, v in cur.items() if k != "data"}
            self._audit(rid, table, cur.get("server_id") or row_id, "DELETE", None, json.dumps(summary)[:2000], None)
            self._touch(rid)

    def delete_many(self, table: str, ids: list[int]) -> None:
        for i in ids:
            self.delete(table, i)

    def renumber_characteristics(self, report_id: int, ordered_ids: list[int]) -> None:
        with self._tx():
            self._check_unlocked(report_id)
            for n, cid in enumerate(ordered_ids, start=1):
                self.conn.execute("UPDATE characteristics SET char_no=?, version=version+1, updated_by=?, "
                                  "updated_at=? WHERE id=? AND char_no<>?", (n, self.user, _now(), cid, n))
            self._audit(report_id, "characteristics", None, "RENUMBER", None, None, f"{len(ordered_ids)} characteristics")
            self._touch(report_id)

    def reseq(self, table: str, report_id: int, ordered_ids: list[int]) -> None:
        with self._tx():
            for n, i in enumerate(ordered_ids, start=1):
                self.conn.execute(f"UPDATE {table} SET seq=? WHERE id=?", (n, i))

    def next_char_no(self, report_id: int) -> int:
        r = self.conn.execute("SELECT coalesce(max(char_no),0)+1 FROM characteristics WHERE report_id=?",
                              (report_id,)).fetchone()
        return int(r[0])

    # ------------------------------------------------------------------ attachments
    def add_attachment(self, report_id: int, path, kind: str, description: str = "",
                       data: Optional[bytes] = None, filename: Optional[str] = None) -> dict:
        path = Path(path)
        data = data if data is not None else path.read_bytes()
        filename = filename or path.name
        sha = hashlib.sha256(data).hexdigest()
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        from .docrender import page_count_for
        pages = page_count_for(data, filename)
        cache = cache_dir() / sha
        if not cache.exists():
            cache.write_bytes(data)
        with self._tx():
            self._check_unlocked(report_id)
            seq = self.conn.execute("SELECT coalesce(max(seq),0)+1 FROM attachments WHERE report_id=?",
                                    (report_id,)).fetchone()[0]
            aid = self._new_id("attachments")
            self.conn.execute(
                "INSERT INTO attachments(id, report_id, kind, filename, mime, size_bytes, sha256, page_count, "
                "description, include_in_report, seq, data, uploaded_by, uploaded_at, updated_by, updated_at, "
                "version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                (aid, report_id, kind, filename, mime, len(data), sha, pages, description,
                 int(kind in ("Drawing", "Photo")), seq, data, self.user, _now(), self.user, _now()))
            self._audit(report_id, "attachments", aid, "UPLOAD", "filename", None, filename)
            self._touch(report_id)
        return self.row("attachments", aid)

    def attachment_bytes(self, attachment_id: int) -> bytes:
        r = self.conn.execute("SELECT sha256, data FROM attachments WHERE id=?", (attachment_id,)).fetchone()
        if not r:
            raise FileNotFoundError("Attachment no longer exists")
        return bytes(r["data"])

    # ------------------------------------------------------------------ gages / workflow
    def vendors(self, active_only: bool = True) -> list[dict]:
        return self._rows("SELECT * FROM vendors" + (" WHERE active=1" if active_only else "") + " ORDER BY upper(name)")

    def gages(self, active_only: bool = False) -> list[dict]:
        return self._rows("SELECT * FROM gages" + (" WHERE active=1" if active_only else "") + " ORDER BY gage_id")

    def approve(self, report_id: int) -> dict:
        raise PermissionError("Approval is done on the server so the sign-off is controlled. Sync this report "
                              "first (⇅ Sync), then Approve & Lock it online.")

    def set_status(self, report_id: int, status: str) -> dict:
        if status == "Approved":
            return self.approve(report_id)
        return self.update_fields("reports", report_id, {"status": status, "locked": False})

    def completeness_issues(self, report_id: int) -> list[str]:
        from .db import Database
        return Database.completeness_issues(self, report_id)      # same rules, local data

    # ------------------------------------------------------------------ used by sync
    def raw_rows(self, table: str, report_id: int) -> list[dict]:
        return self._rows(f"SELECT * FROM {table} WHERE report_id=? ORDER BY id DESC", (report_id,))

    def offline_reports(self) -> list[dict]:
        return self._rows("SELECT * FROM reports ORDER BY updated_at DESC")

    def by_server_id(self, server_id: int) -> Optional[dict]:
        return self._one("SELECT * FROM reports WHERE server_id=?", (server_id,))

    def pending(self) -> list[dict]:
        return self._rows("SELECT * FROM pending ORDER BY id")

    def clear_pending(self, pid: int):
        self.conn.execute("DELETE FROM pending WHERE id=?", (pid,))

    def has_changes(self, report_id: int) -> bool:
        """Anything done offline since the copy was taken (local audit entries)?"""
        return bool(self.conn.execute("SELECT 1 FROM audit_log WHERE report_id=? LIMIT 1", (report_id,)).fetchone())


class _Tx:
    def __init__(self, store: LocalStore):
        self.s = store

    def __enter__(self):
        self.s._lock.acquire()
        self.depth = getattr(self.s, "_depth", 0)
        if self.depth == 0:
            self.s.conn.execute("BEGIN")
        self.s._depth = self.depth + 1
        return self

    def __exit__(self, et, ev, tb):
        self.s._depth = self.depth
        try:
            if self.depth == 0:
                self.s.conn.execute("ROLLBACK" if et else "COMMIT")
        finally:
            self.s._lock.release()
        return False
