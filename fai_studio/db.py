"""PostgreSQL repository layer.

* One autocommit connection per workstation (guarded by a lock).
* Field-level optimistic concurrency: an update only succeeds if the field
  still holds the value this client last saw; otherwise ConflictError is
  raised with the current row so the UI can reload it.
* Every write is recorded in audit_log (who / which machine / old / new).
* Every change fires pg_notify('fai_changes') via triggers -> live sync.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from .config import LocalConfig, cache_dir, hostname

SCHEMA_FILE = Path(__file__).with_name("schema.sql")

# Whitelisted editable columns per table (SQL-injection safe identifiers)
EDITABLE: dict[str, set[str]] = {
    "reports": {
        "report_type", "report_number", "part_number", "part_name", "part_rev", "serial_number",
        "drawing_number", "drawing_rev", "additional_changes", "mfg_process_ref",
        "organization_name", "supplier_code", "customer", "po_number", "job_number", "quantity",
        "sample_size", "detail_or_assembly", "fai_type", "partial_baseline", "partial_reason",
        "units", "tol_0dec", "tol_1dec", "tol_2dec", "tol_3dec", "tol_4dec", "tol_angle", "status",
        "prepared_by", "prepared_date", "reviewed_by", "reviewed_date", "customer_approval",
        "customer_approval_date", "comments", "locked",
        "coc_pack_list", "coc_qty_shipped", "coc_date_shipped", "coc_part_description", "coc_certified_by",
        "coc_title", "coc_date", "part_count", "reading_mode", "part_serials",
    },
    "components": {"seq", "item_no", "part_number", "part_name", "qty", "serial_number",
                   "fai_report_number", "notes"},
    "materials": {"seq", "category", "name", "spec_number", "code", "supplier", "supplier_code",
                  "customer_approval", "coc_number", "comments"},
    "characteristics": {"char_no", "ref_location", "designator", "char_type", "requirement",
                        "nominal", "tol_plus", "tol_minus", "lower_limit", "upper_limit",
                        "results", "disposition", "tooling", "nonconformance", "comments",
                        "balloon_attachment_id", "balloon_page", "balloon_x", "balloon_y", "balloon_size",
                        "part_readings", "ocr_read", "ocr_fix_ids"},
    "attachments": {"kind", "filename", "description", "include_in_report", "seq"},
    "gages": {"gage_id", "description", "gage_type", "cal_due", "active"},
    "coc_items": {"seq", "item_type", "description", "vendor_name", "vendor_info", "cert_lot", "source"},
    "vendors": {"name", "address", "cage", "vendor_type", "approved", "notes", "active"},
}
CHILD_TABLES = ("components", "materials", "characteristics", "attachments", "coc_items")
GLOBAL_TABLES = ("gages", "vendors")
ATTACH_COLS = ("id, report_id, kind, filename, mime, size_bytes, sha256, page_count, description, "
               "include_in_report, seq, uploaded_by, uploaded_at, updated_by, updated_at, version")


def _rule_json(r) -> Optional[str]:
    if not r:
        return None
    keep = ("name", "rule_type", "params", "enabled", "priority", "scope_text", "scope_producer", "notes", "version")
    return json.dumps({k: r.get(k) for k in keep}, ensure_ascii=False, default=str)


class ConflictError(Exception):
    def __init__(self, message: str, current: Optional[dict] = None):
        super().__init__(message)
        self.current = current


class LockedError(Exception):
    pass


def _s(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


class Database:
    def __init__(self, cfg: LocalConfig):
        self.cfg = cfg
        self.client_id = uuid.uuid4().hex
        self.user = cfg.user_display_name or "unknown"
        self.host = hostname()
        self._lock = threading.RLock()
        self.conn: Optional[psycopg.Connection] = None
        self.connect()

    # ------------------------------------------------------------------ connection
    def connect(self) -> None:
        self.conn = psycopg.connect(**self.cfg.conninfo(), autocommit=True, row_factory=dict_row)
        self.conn.execute("SELECT set_config('fai.client_id', %s, false)", (self.client_id,))

    def ensure(self) -> None:
        if self.conn is None or self.conn.closed or self.conn.broken:
            self.connect()

    def close(self) -> None:
        try:
            if self.conn:
                self.conn.close()
        except Exception:
            pass

    def _run(self, fn: Callable[[psycopg.Connection], Any]) -> Any:
        with self._lock:
            try:
                self.ensure()
                return fn(self.conn)
            except (psycopg.OperationalError, psycopg.InterfaceError):
                # one transparent reconnect — only if the link itself died (server restart / network blip)
                if self.conn is not None and not self.conn.closed and not self.conn.broken:
                    raise
                self.conn = None
                self.ensure()
                return fn(self.conn)

    def query(self, q, params: Iterable = ()) -> list[dict]:
        def f(c):
            cur = c.execute(q, params)
            return cur.fetchall() if cur.description else []
        return self._run(f)

    def one(self, q, params: Iterable = ()) -> Optional[dict]:
        rows = self.query(q, params)
        return rows[0] if rows else None

    # ------------------------------------------------------------------ schema
    def schema_exists(self) -> bool:
        r = self.one("SELECT to_regclass('public.reports') AS t")
        return bool(r and r["t"])

    def init_schema(self) -> None:
        text = SCHEMA_FILE.read_text(encoding="utf-8")
        self._run(lambda c: c.execute(text))
        self._seed_rules()

    # ------------------------------------------------------------------ drawing reader rules
    RULE_COLS = ("name", "rule_type", "params", "enabled", "priority", "scope_text", "scope_producer", "notes")

    def _seed_rules(self):
        from .rules import DEFAULT_RULES
        r = self.one("SELECT count(*) AS n FROM reader_rules")
        if r and r["n"] == 0:
            for d in DEFAULT_RULES:
                self.save_rule(dict(d), log_action="SEED")

    def reader_rules(self) -> list[dict]:
        return self.query("SELECT r.*, (SELECT count(*) FROM reader_rule_examples e WHERE e.rule_id = r.id) AS examples "
                          "FROM reader_rules r ORDER BY priority, id")

    def _log_rule(self, c, rule_id, name, action, before, after):
        c.execute("INSERT INTO reader_rule_log(rule_id, rule_name, action, before, after, user_name, host) "
                  "VALUES (%s,%s,%s,%s,%s,%s,%s)", (rule_id, name, action, before, after, self.user, self.host))

    def save_rule(self, d: dict, log_action: str = "") -> dict:
        """Insert (no id) or update a rule; the old and new version go to the change log."""
        vals = {k: d.get(k) for k in self.RULE_COLS if k in d}
        if isinstance(vals.get("params"), dict):
            vals["params"] = json.dumps(vals["params"], ensure_ascii=False, sort_keys=True)
        for k, dv in (("scope_text", ""), ("scope_producer", ""), ("notes", ""), ("enabled", True), ("priority", 100)):
            if vals.get(k) is None and (not d.get("id") or k in d):
                vals[k] = dv

        def f(c):
            with c.transaction():
                if d.get("id"):
                    old = c.execute("SELECT * FROM reader_rules WHERE id=%s FOR UPDATE", (d["id"],)).fetchone()
                    if old is None:
                        raise ValueError("That rule was deleted on another workstation.")
                    sets = ", ".join(f"{k}=%s" for k in vals) + ", updated_by=%s, updated_at=now(), version=version+1"
                    row = c.execute(f"UPDATE reader_rules SET {sets} WHERE id=%s RETURNING *",
                                    list(vals.values()) + [self.user, d["id"]]).fetchone()
                    self._log_rule(c, row["id"], row["name"], log_action or "UPDATE", _rule_json(old), _rule_json(row))
                else:
                    cols = list(vals) + ["created_by", "updated_by"]
                    row = c.execute(f"INSERT INTO reader_rules({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
                                    "RETURNING *", list(vals.values()) + [self.user, self.user]).fetchone()
                    self._log_rule(c, row["id"], row["name"], log_action or "ADD", None, _rule_json(row))
                return row
        return self._run(f)

    def delete_rule(self, rule_id: int) -> None:
        def f(c):
            with c.transaction():
                old = c.execute("SELECT * FROM reader_rules WHERE id=%s", (rule_id,)).fetchone()
                if old:
                    c.execute("DELETE FROM reader_rules WHERE id=%s", (rule_id,))
                    self._log_rule(c, rule_id, old["name"], "DELETE", _rule_json(old), None)
        self._run(f)

    def rule_log(self, rule_id: Optional[int] = None, limit: int = 1000) -> list[dict]:
        if rule_id:
            return self.query("SELECT * FROM reader_rule_log WHERE rule_id=%s ORDER BY at DESC, id DESC LIMIT %s",
                              (rule_id, limit))
        return self.query("SELECT * FROM reader_rule_log ORDER BY at DESC, id DESC LIMIT %s", (limit,))

    def rule_examples(self, rule_id: Optional[int] = None) -> list[dict]:
        q = ("SELECT id, rule_id, filename, expected, notes, last_result, added_by, added_at, length(data) AS size "
             "FROM reader_rule_examples")
        if rule_id is not None:
            return self.query(q + " WHERE rule_id=%s ORDER BY id", (rule_id,))
        return self.query(q + " ORDER BY rule_id, id")

    def rule_example_bytes(self, ex_id: int) -> bytes:
        r = self.one("SELECT data FROM reader_rule_examples WHERE id=%s", (ex_id,))
        return bytes(r["data"]) if r else b""

    def add_rule_example(self, rule_id: int, filename: str, data: bytes, expected: list, notes: str = "") -> dict:
        def f(c):
            with c.transaction():
                row = c.execute("INSERT INTO reader_rule_examples(rule_id, filename, data, expected, notes, added_by) "
                                "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id, rule_id, filename, expected, notes, added_by, "
                                "added_at", (rule_id, filename, data, json.dumps(expected, ensure_ascii=False), notes,
                                             self.user)).fetchone()
                name = c.execute("SELECT name FROM reader_rules WHERE id=%s", (rule_id,)).fetchone()
                self._log_rule(c, rule_id, name["name"] if name else "", "EXAMPLE ADDED", None,
                               f"{filename}: {len(expected)} expected characteristic(s)")
                return row
        return self._run(f)

    def update_rule_example(self, ex_id: int, **vals) -> None:
        vals = {k: (json.dumps(v, ensure_ascii=False) if k == "expected" else v) for k, v in vals.items()
                if k in ("expected", "notes", "last_result", "rule_id")}
        if not vals:
            return

        def f(c):
            with c.transaction():
                old = c.execute("SELECT e.rule_id, e.filename, e.expected, r.name FROM reader_rule_examples e "
                                "LEFT JOIN reader_rules r ON r.id = e.rule_id WHERE e.id=%s", (ex_id,)).fetchone()
                c.execute(f"UPDATE reader_rule_examples SET {', '.join(f'{k}=%s' for k in vals)} WHERE id=%s",
                          list(vals.values()) + [ex_id])
                if old and "expected" in vals:
                    self._log_rule(c, old["rule_id"], old["name"] or "", "EXAMPLE EXPECTED CHANGED",
                                   f"{old['filename']}: {old['expected']}", vals["expected"])
        self._run(f)

    def delete_rule_example(self, ex_id: int) -> None:
        def f(c):
            with c.transaction():
                old = c.execute("SELECT e.rule_id, e.filename, r.name FROM reader_rule_examples e LEFT JOIN "
                                "reader_rules r ON r.id = e.rule_id WHERE e.id=%s", (ex_id,)).fetchone()
                c.execute("DELETE FROM reader_rule_examples WHERE id=%s", (ex_id,))
                if old:
                    self._log_rule(c, old["rule_id"], old["name"] or "", "EXAMPLE REMOVED", old["filename"], None)
        self._run(f)

    # ------------------------------------------------------------------ meta / company
    # ------------------------------------------------------------------ OCR learning memory (ocr_learn.py)
    def ocr_learned(self, status: Optional[str] = None) -> list[dict]:
        q = 'SELECT * FROM ocr_learned' + (" WHERE status = %s" if status else "") + \
            " ORDER BY (status = 'active') DESC, hits DESC, updated_at DESC"
        return self.query(q, (status,) if status else ())

    def ocr_memory(self):
        from .ocr_learn import Memory
        return Memory(self.ocr_learned("active"))

    def ocr_learning_settings(self) -> tuple[bool, int]:
        m = self.get_meta()
        try:
            after = max(1, int(m.get("ocr_learn_after") or 2))
        except ValueError:
            after = 2
        return (m.get("ocr_learning", "1") != "0"), after

    def _ocr_log(self, c, row: dict, action: str, before: str = "", after: str = ""):
        self._log_rule(c, None, f"OCR learned: {row['wrong']} → {row['right']}", action, before, after)

    def learn_ocr_correction(self, read: str, fixed: str, source_key: str) -> list[dict]:
        """Someone corrected an OCR read. Saves the fix pairs (see ocr_learn.derive). source_key identifies the
        characteristic ('report:char'), so correcting the same characteristic again replaces its earlier lesson
        instead of counting twice. Returns the learned rows touched."""
        from .ocr_learn import derive
        on, after = self.ocr_learning_settings()
        if not on:
            return []
        pairs = derive(read, fixed)
        key = json.dumps(source_key)

        def f(c):
            touched = []
            with c.transaction():
                # this characteristic taught something else before (it was re-edited): take that back
                for r in c.execute("SELECT * FROM ocr_learned WHERE sources LIKE %s FOR UPDATE",
                                   (f"%{key}%",)).fetchall():
                    if (r["kind"], r["wrong"], r["right"]) in pairs:
                        continue
                    src = [x for x in json.loads(r["sources"] or "[]") if x != source_key]
                    if not src and r["status"] == "learning" and not r["confirmed"]:
                        c.execute("DELETE FROM ocr_learned WHERE id=%s", (r["id"],))
                    else:
                        c.execute("UPDATE ocr_learned SET sources=%s, hits=%s WHERE id=%s",
                                  (json.dumps(src), len(src), r["id"]))
                for kind, wrong, right in pairs:
                    r = c.execute('SELECT * FROM ocr_learned WHERE kind=%s AND wrong=%s AND "right"=%s FOR UPDATE',
                                  (kind, wrong, right)).fetchone()
                    if r is None:
                        status = "active" if after <= 1 else "learning"
                        r = c.execute('INSERT INTO ocr_learned(kind, wrong, "right", status, hits, sources, '
                                      'created_by, updated_by) VALUES (%s,%s,%s,%s,1,%s,%s,%s) RETURNING *',
                                      (kind, wrong, right, status, json.dumps([source_key]), self.user,
                                       self.user)).fetchone()
                        self._ocr_log(c, r, "LEARN", "", f"{status}; from {source_key}")
                    else:
                        src = json.loads(r["sources"] or "[]")
                        if source_key not in src:
                            src = (src + [source_key])[-500:]
                        status = r["status"]
                        if status == "learning" and len(src) >= after:
                            status = "active"
                            self._ocr_log(c, r, "ACTIVATE", "learning",
                                          f"active after {len(src)} matching corrections")
                        r = c.execute("UPDATE ocr_learned SET sources=%s, hits=%s, status=%s, updated_by=%s, "
                                      "updated_at=now() WHERE id=%s RETURNING *",
                                      (json.dumps(src), len(src), status, self.user, r["id"])).fetchone()
                    touched.append(dict(r))
            return touched
        return self._run(f)

    def ocr_fix_feedback(self, ids: Iterable[int], final_text: str) -> None:
        """Learned fixes were applied to a read; final_text is what the inspector kept. Each fix still present
        counts as kept, the others as undone. A fix undone at least twice and more often than kept turns itself
        off."""
        from .ocr_learn import fix_kept
        ids = [int(i) for i in ids if i]
        if not ids:
            return

        def f(c):
            with c.transaction():
                for i in ids:
                    r = c.execute("SELECT * FROM ocr_learned WHERE id=%s FOR UPDATE", (i,)).fetchone()
                    if r is None:
                        continue
                    col = "confirmed" if fix_kept(r, final_text) else "rejected"
                    r = c.execute(f"UPDATE ocr_learned SET applied = applied + 1, {col} = {col} + 1 "
                                  f"WHERE id=%s RETURNING *", (i,)).fetchone()
                    if col == "rejected" and r["status"] == "active" and r["rejected"] >= 2 \
                            and r["rejected"] > r["confirmed"]:
                        c.execute("UPDATE ocr_learned SET status='off', updated_by=%s, updated_at=now(), "
                                  "notes = trim(notes || ' Switched off automatically: undone more often than kept.')"
                                  " WHERE id=%s", (self.user, i))
                        self._ocr_log(c, r, "AUTO-OFF", "active",
                                      f"off: undone {r['rejected']}x, kept {r['confirmed']}x")
        self._run(f)

    def save_ocr_learned(self, row_id: Optional[int], values: dict) -> dict:
        """Edit (status / right / notes / wrong / kind) or add a learned fix by hand."""
        allowed = {k: v for k, v in values.items() if k in ("kind", "wrong", "right", "status", "notes")}
        from .ocr_learn import norm
        for k in ("wrong", "right"):
            if k in allowed:
                allowed[k] = norm(allowed[k])

        def f(c):
            with c.transaction():
                if row_id:
                    old = c.execute("SELECT * FROM ocr_learned WHERE id=%s FOR UPDATE", (row_id,)).fetchone()
                    if old is None:
                        raise ValueError("That learned fix was deleted on another workstation.")
                    sets = ", ".join(f'"{k}"=%s' for k in allowed)
                    r = c.execute(f"UPDATE ocr_learned SET {sets}, updated_by=%s, updated_at=now() WHERE id=%s "
                                  "RETURNING *", (*allowed.values(), self.user, row_id)).fetchone()
                    diff = {k: (old[k], r[k]) for k in allowed if old[k] != r[k]}
                    if diff:
                        self._ocr_log(c, r, "EDIT", json.dumps({k: v[0] for k, v in diff.items()}, ensure_ascii=False),
                                      json.dumps({k: v[1] for k, v in diff.items()}, ensure_ascii=False))
                    return dict(r)
                vals = dict({"kind": "whole", "status": "active", "notes": "Added by hand"}, **allowed)
                r = c.execute('INSERT INTO ocr_learned(kind, wrong, "right", status, notes, created_by, updated_by) '
                              "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                              (vals["kind"], vals["wrong"], vals["right"], vals["status"], vals["notes"],
                               self.user, self.user)).fetchone()
                self._ocr_log(c, r, "ADD", "", vals["status"])
                return dict(r)
        return self._run(f)

    def delete_ocr_learned(self, row_id: int) -> None:
        def f(c):
            with c.transaction():
                r = c.execute("DELETE FROM ocr_learned WHERE id=%s RETURNING *", (row_id,)).fetchone()
                if r:
                    self._ocr_log(c, r, "DELETE", r["status"], "")
        self._run(f)

    def get_meta(self) -> dict:
        return {r["key"]: r["value"] for r in self.query("SELECT key, value FROM app_meta")}

    def set_meta(self, values: dict) -> None:
        def f(c):
            with c.transaction():
                for k, v in values.items():
                    c.execute("INSERT INTO app_meta(key,value) VALUES (%s,%s) "
                              "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, v))
        self._run(f)

    # ------------------------------------------------------------------ audit
    def _audit(self, c, report_id, table, record_id, action, field=None, old=None, new=None):
        c.execute(
            "INSERT INTO audit_log(report_id, table_name, record_id, action, field, old_value, new_value, user_name, host) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (report_id, table, record_id, action, field, _s(old), _s(new), self.user, self.host))

    def audit_for(self, report_id: int, limit: int = 2000) -> list[dict]:
        return self.query("SELECT * FROM audit_log WHERE report_id=%s ORDER BY at DESC, id DESC LIMIT %s",
                          (report_id, limit))

    def _touch_report(self, c, report_id):
        c.execute("UPDATE reports SET updated_at=now(), updated_by=%s WHERE id=%s", (self.user, report_id))

    def _check_unlocked(self, c, report_id):
        # row lock serializes child edits against approval (no edit can slip in after Approve & Lock)
        r = c.execute("SELECT * FROM reports WHERE id=%s FOR NO KEY UPDATE", (report_id,)).fetchone()
        if r and r["locked"]:
            raise LockedError(f"Report is {r['status']} and locked. Reopen it to make changes.")
        self._check_checkout(r)

    def _check_checkout(self, r):
        """A report checked out for offline work can only be changed by the PC that has it."""
        if r and r.get("checked_out_host") and r["checked_out_host"] != self.host:
            raise LockedError(f"Report {r['report_number']} is checked out to {r['checked_out_by'] or 'another user'} "
                              f"on {r['checked_out_host']} for offline work. It can be edited again after they "
                              "sync it back.")

    # ------------------------------------------------------------------ reports
    def list_reports(self, search: str = "") -> list[dict]:
        like = f"%{search.strip()}%"
        return self.query(
            """SELECT r.id, r.report_type, r.report_number, r.part_number, r.part_rev, r.part_name,
                      r.customer, r.po_number, r.job_number, r.serial_number, r.status, r.updated_by, r.updated_at,
                      r.created_at, r.locked, r.checked_out_by, r.checked_out_host, r.checked_out_at,
                      (SELECT count(*) FROM characteristics c WHERE c.report_id=r.id) AS n_chars,
                      (SELECT count(*) FROM characteristics c WHERE c.report_id=r.id AND c.disposition <> '') AS n_done,
                      (SELECT count(*) FROM characteristics c WHERE c.report_id=r.id AND c.disposition = 'Reject') AS n_rej
               FROM reports r
               WHERE %s = '' OR r.part_number ILIKE %s OR r.part_name ILIKE %s OR r.report_number ILIKE %s
                  OR r.customer ILIKE %s OR r.po_number ILIKE %s OR r.job_number ILIKE %s OR r.serial_number ILIKE %s
                  OR r.status ILIKE %s
               ORDER BY r.updated_at DESC""",
            (search.strip(), like, like, like, like, like, like, like, like))

    def get_report(self, report_id: int) -> Optional[dict]:
        return self.one("SELECT * FROM reports WHERE id=%s", (report_id,))

    def create_report(self, values: dict) -> dict:
        values = {k: v for k, v in values.items() if k in EDITABLE["reports"]}
        values.setdefault("prepared_by", self.user)
        values.setdefault("prepared_date", datetime.now().strftime("%Y-%m-%d"))

        def f(c):
            with c.transaction():
                cols = list(values.keys()) + ["created_by", "updated_by"]
                vals = list(values.values()) + [self.user, self.user]
                q = sql.SQL("INSERT INTO reports ({}) VALUES ({}) RETURNING *").format(
                    sql.SQL(",").join(map(sql.Identifier, cols)),
                    sql.SQL(",").join(sql.Placeholder() * len(cols)))
                row = c.execute(q, vals).fetchone()
                if not row["report_number"]:
                    from .report_types import number_prefix
                    num = f"{number_prefix(row['report_type'])}-{datetime.now():%y%m}-{row['id']:04d}"
                    row = c.execute("UPDATE reports SET report_number=%s WHERE id=%s RETURNING *",
                                    (num, row["id"])).fetchone()
                self._audit(c, row["id"], "reports", row["id"], "CREATE", None, None, row["report_number"])
                return row
        return self._run(f)

    def delete_report(self, report_id: int) -> None:
        def f(c):
            with c.transaction():
                self._check_unlocked(c, report_id)
                self._audit(c, report_id, "reports", report_id, "DELETE")
                c.execute("DELETE FROM reports WHERE id=%s", (report_id,))
        self._run(f)

    def duplicate_report(self, report_id: int, new_values: dict, copy_results: bool = False) -> dict:
        src = self.get_report(report_id)
        if not src:
            raise ValueError("Report not found")
        base = {k: src[k] for k in EDITABLE["reports"] if k not in ("status", "locked", "reviewed_by",
                                                                    "reviewed_date", "customer_approval",
                                                                    "customer_approval_date", "report_number",
                                                                    "prepared_by", "prepared_date",
                                                                    "coc_pack_list", "coc_qty_shipped",
                                                                    "coc_date_shipped", "coc_date",
                                                                    "part_serials")}
        base.update(new_values)
        new = self.create_report(base)
        nid = new["id"]

        def f(c):
            with c.transaction():
                amap = {}
                for a in c.execute("SELECT * FROM attachments WHERE report_id=%s ORDER BY id", (report_id,)).fetchall():
                    r = c.execute(
                        "INSERT INTO attachments(report_id, kind, filename, mime, size_bytes, sha256, page_count, "
                        "description, include_in_report, seq, data, uploaded_by, updated_by) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                        (nid, a["kind"], a["filename"], a["mime"], a["size_bytes"], a["sha256"], a["page_count"],
                         a["description"], a["include_in_report"], a["seq"], a["data"], self.user, self.user)).fetchone()
                    amap[a["id"]] = r["id"]
                for t in ("components", "materials", "coc_items"):
                    cols = [x for x in EDITABLE[t]]
                    for row in c.execute(sql.SQL("SELECT * FROM {} WHERE report_id=%s ORDER BY seq, id")
                                         .format(sql.Identifier(t)), (report_id,)).fetchall():
                        if t == "coc_items" and not copy_results:
                            row = dict(row, cert_lot="")         # a new shipment has new certs / lots
                        vals = [row[k] for k in cols]
                        c.execute(sql.SQL("INSERT INTO {} (report_id, updated_by, {}) VALUES (%s, %s, {})").format(
                            sql.Identifier(t), sql.SQL(",").join(map(sql.Identifier, cols)),
                            sql.SQL(",").join(sql.Placeholder() * len(cols))), [nid, self.user] + vals)
                cols = [x for x in EDITABLE["characteristics"]]
                for row in c.execute("SELECT * FROM characteristics WHERE report_id=%s ORDER BY char_no, id",
                                     (report_id,)).fetchall():
                    row = dict(row)
                    if not copy_results:
                        row["results"] = ""
                        row["disposition"] = ""
                        row["nonconformance"] = ""
                        row["part_readings"] = ""
                    row["ocr_fix_ids"] = ""               # learned-fix feedback is counted once, on the original
                    row["balloon_attachment_id"] = amap.get(row["balloon_attachment_id"])
                    vals = [row[k] for k in cols]
                    c.execute(sql.SQL("INSERT INTO characteristics (report_id, updated_by, {}) VALUES (%s, %s, {})").format(
                        sql.SQL(",").join(map(sql.Identifier, cols)),
                        sql.SQL(",").join(sql.Placeholder() * len(cols))), [nid, self.user] + vals)
                self._audit(c, nid, "reports", nid, "DUPLICATE", "source_report", None, src["report_number"])
        self._run(f)
        return self.get_report(nid)

    # ------------------------------------------------------------------ generic child rows
    def rows(self, table: str, report_id: int) -> list[dict]:
        assert table in CHILD_TABLES
        order = {"components": "seq, id", "materials": "seq, id", "characteristics": "char_no, id",
                 "attachments": "seq, id", "coc_items": "seq, id"}[table]
        cols = sql.SQL(ATTACH_COLS) if table == "attachments" else sql.SQL("*")
        return self.query(sql.SQL("SELECT {} FROM {} WHERE report_id=%s ORDER BY " + order).format(
            cols, sql.Identifier(table)), (report_id,))

    def row(self, table: str, row_id: int) -> Optional[dict]:
        assert table in EDITABLE
        cols = sql.SQL(ATTACH_COLS) if table == "attachments" else sql.SQL("*")
        return self.one(sql.SQL("SELECT {} FROM {} WHERE id=%s").format(cols, sql.Identifier(table)), (row_id,))

    def insert(self, table: str, report_id: Optional[int], values: dict) -> dict:
        assert table in EDITABLE
        values = {k: v for k, v in values.items() if k in EDITABLE[table]}

        def f(c):
            with c.transaction():
                if report_id is not None:
                    self._check_unlocked(c, report_id)
                cols = list(values.keys()) + ["updated_by"]
                vals = list(values.values()) + [self.user]
                if report_id is not None:
                    cols.insert(0, "report_id")
                    vals.insert(0, report_id)
                ret = sql.SQL(ATTACH_COLS) if table == "attachments" else sql.SQL("*")
                q = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING {}").format(
                    sql.Identifier(table), sql.SQL(",").join(map(sql.Identifier, cols)),
                    sql.SQL(",").join(sql.Placeholder() * len(cols)), ret)
                row = c.execute(q, vals).fetchone()
                self._audit(c, report_id, table, row["id"], "INSERT", None, None,
                            json.dumps({k: _s(v) for k, v in values.items()})[:2000])
                if report_id is not None:
                    self._touch_report(c, report_id)
                return row
        return self._run(f)

    def insert_many(self, table: str, report_id: int, rows: list[dict]) -> list[dict]:
        out = []
        def f(c):
            with c.transaction():
                self._check_unlocked(c, report_id)
                for values in rows:
                    values = {k: v for k, v in values.items() if k in EDITABLE[table]}
                    cols = ["report_id", "updated_by"] + list(values.keys())
                    vals = [report_id, self.user] + list(values.values())
                    q = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING *").format(
                        sql.Identifier(table), sql.SQL(",").join(map(sql.Identifier, cols)),
                        sql.SQL(",").join(sql.Placeholder() * len(cols)))
                    out.append(c.execute(q, vals).fetchone())
                self._audit(c, report_id, table, None, "IMPORT", None, None, f"{len(rows)} rows")
                self._touch_report(c, report_id)
        self._run(f)
        return out

    def update_fields(self, table: str, row_id: int, changes: dict, expected: Optional[dict] = None,
                      force: bool = False) -> dict:
        """Update several fields atomically. `expected` maps field->value the client last saw."""
        assert table in EDITABLE
        for k in changes:
            if k not in EDITABLE[table]:
                raise ValueError(f"Field {k} not editable on {table}")

        def f(c):
            with c.transaction():
                # lock order everywhere: report row first, then the child row (prevents deadlocks)
                if table not in ("reports",) + GLOBAL_TABLES:
                    pre = c.execute(sql.SQL("SELECT report_id FROM {} WHERE id=%s").format(sql.Identifier(table)),
                                    (row_id,)).fetchone()
                    if pre is None:
                        raise ConflictError("This record was deleted by another user.", None)
                    self._check_unlocked(c, pre["report_id"])
                cur = c.execute(sql.SQL("SELECT * FROM {} WHERE id=%s FOR UPDATE").format(sql.Identifier(table)),
                                (row_id,)).fetchone()
                if cur is None:
                    raise ConflictError("This record was deleted by another user.", None)
                report_id = cur.get("report_id") if table != "reports" else cur["id"]
                if table == "reports":
                    self._check_checkout(cur)
                    if cur["locked"] and not (set(changes) <= {"status", "locked", "reviewed_by", "reviewed_date",
                                                               "customer_approval", "customer_approval_date"}):
                        raise LockedError(f"Report is {cur['status']} and locked. Reopen it to make changes.")
                if expected and not force:
                    for k, v in expected.items():
                        if k in changes and _s(cur.get(k)) != _s(v) and _s(cur.get(k)) != _s(changes[k]):
                            cur = dict(cur)
                            cur.pop("data", None)
                            raise ConflictError(
                                f"'{k}' was changed by {cur.get('updated_by') or 'another user'} while you were editing.\n"
                                f"Their value: {cur.get(k)!r}", cur)
                changed = {k: v for k, v in changes.items() if _s(cur.get(k)) != _s(v)}
                if not changed:
                    r = dict(cur)
                    r.pop("data", None)
                    return r
                sets = sql.SQL(",").join(
                    [sql.SQL("{}=%s").format(sql.Identifier(k)) for k in changed] +
                    [sql.SQL("updated_by=%s"), sql.SQL("updated_at=now()"), sql.SQL("version=version+1")])
                ret = sql.SQL(ATTACH_COLS) if table == "attachments" else sql.SQL("*")
                row = c.execute(sql.SQL("UPDATE {} SET {} WHERE id=%s RETURNING {}").format(
                    sql.Identifier(table), sets, ret), list(changed.values()) + [self.user, row_id]).fetchone()
                for k, v in changed.items():
                    self._audit(c, report_id, table, row_id, "UPDATE", k, cur.get(k), v)
                if table != "reports" and report_id is not None:
                    self._touch_report(c, report_id)
                return row
        return self._run(f)

    def update_field(self, table, row_id, field, value, expected_old=None, force=False) -> dict:
        exp = {field: expected_old} if expected_old is not None else None
        return self.update_fields(table, row_id, {field: value}, exp, force)

    def delete(self, table: str, row_id: int) -> None:
        assert table in EDITABLE

        def f(c):
            with c.transaction():
                cur = c.execute(sql.SQL("SELECT * FROM {} WHERE id=%s").format(sql.Identifier(table)),
                                (row_id,)).fetchone()
                if not cur:
                    return
                rid = cur.get("report_id")
                if rid:
                    self._check_unlocked(c, rid)
                summary = {k: _s(v) for k, v in cur.items() if k != "data"}
                if table == "attachments":
                    c.execute("UPDATE characteristics SET balloon_attachment_id=NULL, balloon_page=NULL, balloon_x=NULL, "
                              "balloon_y=NULL, version=version+1, updated_by=%s, updated_at=now() "
                              "WHERE balloon_attachment_id=%s", (self.user, row_id))
                c.execute(sql.SQL("DELETE FROM {} WHERE id=%s").format(sql.Identifier(table)), (row_id,))
                self._audit(c, rid, table, row_id, "DELETE", None, json.dumps(summary)[:2000], None)
                if rid:
                    self._touch_report(c, rid)
        self._run(f)

    def delete_many(self, table: str, ids: list[int]) -> None:
        for i in ids:
            self.delete(table, i)

    def renumber_characteristics(self, report_id: int, ordered_ids: list[int]) -> None:
        def f(c):
            with c.transaction():
                self._check_unlocked(c, report_id)
                for n, cid in enumerate(ordered_ids, start=1):
                    c.execute("UPDATE characteristics SET char_no=%s, version=version+1, updated_by=%s, "
                              "updated_at=now() WHERE id=%s AND char_no<>%s", (n, self.user, cid, n))
                self._audit(c, report_id, "characteristics", None, "RENUMBER", None, None,
                            f"{len(ordered_ids)} characteristics")
                self._touch_report(c, report_id)
        self._run(f)

    def reseq(self, table: str, report_id: int, ordered_ids: list[int]) -> None:
        assert table in ("components", "materials", "attachments")

        def f(c):
            with c.transaction():
                for n, i in enumerate(ordered_ids, start=1):
                    c.execute(sql.SQL("UPDATE {} SET seq=%s WHERE id=%s AND seq<>%s").format(sql.Identifier(table)),
                              (n, i, n))
        self._run(f)

    def next_char_no(self, report_id: int) -> int:
        r = self.one("SELECT coalesce(max(char_no),0)+1 AS n FROM characteristics WHERE report_id=%s", (report_id,))
        return int(r["n"])

    # ------------------------------------------------------------------ attachments
    def add_attachment(self, report_id: int, path: str | Path, kind: str, description: str = "",
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

        def f(c):
            with c.transaction():
                self._check_unlocked(c, report_id)
                seq = c.execute("SELECT coalesce(max(seq),0)+1 AS n FROM attachments WHERE report_id=%s",
                                (report_id,)).fetchone()["n"]
                row = c.execute(
                    "INSERT INTO attachments(report_id, kind, filename, mime, size_bytes, sha256, page_count, "
                    "description, include_in_report, seq, data, uploaded_by, updated_by) "
                    f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING {ATTACH_COLS}",
                    (report_id, kind, filename, mime, len(data), sha, pages, description,
                     kind in ("Drawing", "Photo"), seq, data, self.user, self.user)).fetchone()
                self._audit(c, report_id, "attachments", row["id"], "UPLOAD", "filename", None, filename)
                self._touch_report(c, report_id)
                return row
        return self._run(f)

    def attachment_bytes(self, attachment_id: int) -> bytes:
        meta = self.one("SELECT sha256 FROM attachments WHERE id=%s", (attachment_id,))
        if not meta:
            raise FileNotFoundError("Attachment no longer exists")
        cache = cache_dir() / meta["sha256"]
        if cache.exists():
            return cache.read_bytes()
        r = self.one("SELECT data FROM attachments WHERE id=%s", (attachment_id,))
        data = bytes(r["data"])
        try:
            cache.write_bytes(data)
        except Exception:
            pass
        return data

    # ------------------------------------------------------------------ vendors (COC)
    def vendors(self, active_only: bool = True) -> list[dict]:
        return self.query("SELECT * FROM vendors" + (" WHERE active" if active_only else "") + " ORDER BY upper(name)")

    # ------------------------------------------------------------------ gages
    def gages(self, active_only: bool = False) -> list[dict]:
        q = "SELECT * FROM gages" + (" WHERE active" if active_only else "") + " ORDER BY gage_id"
        return self.query(q)

    # ------------------------------------------------------------------ workflow
    def approve(self, report_id: int) -> dict:
        """Atomic approve: locks the report row, re-runs the completeness check inside the same
        transaction, then locks the report. Raises ValueError(issues) if anything is open."""
        def f(c):
            with c.transaction():
                c.execute("SELECT id FROM reports WHERE id=%s FOR UPDATE", (report_id,))
                issues = self.completeness_issues(report_id)
                if issues:
                    raise ValueError("Cannot approve:\n• " + "\n• ".join(issues[:25]))
                return self.set_status(report_id, "Approved")
        return self._run(f)

    def set_status(self, report_id: int, status: str) -> dict:
        today = datetime.now().strftime("%Y-%m-%d")
        changes: dict[str, Any] = {"status": status, "locked": status == "Approved"}
        if status == "Approved":
            changes.update(reviewed_by=self.user, reviewed_date=today)
        if status == "Draft":
            changes.update(locked=False)
        return self.update_fields("reports", report_id, changes)

    def completeness_issues(self, report_id: int) -> list[str]:
        rep = self.get_report(report_id)
        chars = self.rows("characteristics", report_id)
        issues = []
        from . import multipart as mp
        multi = mp.applies(rep)
        for fld, label in (("part_number", "Part Number"), ("part_name", "Part Name"), ("part_rev", "Part Revision"),
                           ("drawing_number", "Drawing Number"), ("serial_number", "Serial Number")):
            if fld == "serial_number" and multi and mp.part_count(rep) > 1:
                missing = [i + 1 for i, sn in enumerate(mp.serials(rep)) if not sn]
                if missing:
                    issues.append(f"Parts: serial number missing for part {', '.join(map(str, missing))}")
                continue
            if not (rep.get(fld) or "").strip():
                issues.append(f"Form 1: {label} is blank")
        if rep["fai_type"] == "Partial" and not rep["partial_reason"].strip():
            issues.append("Form 1: Partial FAI requires a reason / baseline")
        if not chars:
            issues.append("Form 3: no characteristics defined")
        nums = [c["char_no"] for c in chars]
        if len(nums) != len(set(nums)):
            issues.append("Form 3: duplicate characteristic numbers — use Renumber")
        gmap = {g["gage_id"].upper(): g for g in self.gages()}
        today = datetime.now().strftime("%Y-%m-%d")
        for c in chars:
            tag = f"Char {c['char_no']}"
            if not c["requirement"].strip():
                issues.append(f"{tag}: requirement is blank")
            if multi and c["disposition"] != "N/A":
                rd = mp.parse(c.get("part_readings"))
                n = mp.part_count(rep)
                miss = [i for i in range(1, n + 1) if not (rd.get(i, {}).get("lo")
                                                         or (mp.minmax(rep) and rd.get(i, {}).get("hi")))]
                half = []
                if mp.minmax(rep) and ((c.get("lower_limit") or "").strip() or (c.get("upper_limit") or "").strip()):
                    # a numeric low without its high (or the reverse); text readings such as OK need no pair
                    from .tolerance import D as _D
                    half = [i for i in range(1, n + 1) if i not in miss and rd.get(i)
                            and bool(rd[i].get("lo")) != bool(rd[i].get("hi"))
                            and _D((rd[i].get("lo") or rd[i].get("hi")).replace("Ø", "")) is not None]
                if miss:
                    issues.append(f"{tag}: no reading for part {', '.join(map(str, miss))}"
                                  if n > 1 else f"{tag}: no result recorded")
                if half:
                    issues.append(f"{tag}: low or high reading missing for part {', '.join(map(str, half))}")
                if miss or half:
                    continue
            if not c["results"].strip() and c["disposition"] != "N/A":
                issues.append(f"{tag}: no result recorded")
            if c["disposition"] == "Reject" and not c["nonconformance"].strip():
                issues.append(f"{tag}: rejected without a nonconformance (NCR) number")
            if c["results"].strip() and not c["disposition"]:
                issues.append(f"{tag}: result entered but not dispositioned")
            for tool in [t.strip().upper() for t in c["tooling"].replace(";", ",").split(",") if t.strip()]:
                g = gmap.get(tool)
                if g and g["cal_due"] and g["cal_due"] < today:
                    issues.append(f"{tag}: gage {g['gage_id']} calibration expired {g['cal_due']}")
                if g and not g["active"]:
                    issues.append(f"{tag}: gage {g['gage_id']} is inactive")
        from .report_types import is_fai
        if is_fai(rep["report_type"]) and not self.rows("materials", report_id):
            issues.append("Form 2: no materials / special processes listed")
        return issues


class ChangeListener:
    """Blocking LISTEN loop (run it in a worker thread). Calls `on_event(dict)`."""

    def __init__(self, cfg: LocalConfig, client_id: str, on_event: Callable[[dict], None],
                 on_status: Callable[[bool, str], None]):
        self.cfg = cfg
        self.client_id = client_id
        self.on_event = on_event
        self.on_status = on_status
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        import time
        backoff = 1
        while not self._stop.is_set():
            try:
                with psycopg.connect(**self.cfg.conninfo(), autocommit=True) as conn:
                    conn.execute("LISTEN fai_changes")
                    self.on_status(True, "Live sync connected")
                    backoff = 1
                    while not self._stop.is_set():
                        for n in conn.notifies(timeout=1.0):
                            try:
                                ev = json.loads(n.payload)
                            except Exception:
                                continue
                            ev["own"] = ev.get("client") == self.client_id
                            self.on_event(ev)
                        conn.execute("SELECT 1")  # keepalive / detect dead link
            except Exception as e:  # noqa: BLE001
                if self._stop.is_set():
                    break
                self.on_status(False, f"Live sync offline – retrying ({e.__class__.__name__})")
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
