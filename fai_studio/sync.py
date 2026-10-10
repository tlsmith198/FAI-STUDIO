"""Check-out / check-in between the PostgreSQL server and this PC's offline store.

take_offline(): mark the server report as checked out to this PC (others can no longer change it) and
                copy it - with drawings, photos, balloons and results - into the local store.
push():         send one offline report back in ONE server transaction: header, parts list, materials,
                characteristics, attachments (new, changed, deleted), then the offline audit entries with
                their original timestamps marked "(offline)". Reports created offline get their real
                FAI-/IR- number here. Afterwards the local copy is removed (or refreshed when kept offline).
sync_all():     push every offline report, apply queued releases, refresh the settings snapshot.

If a check-out was released on the server while this PC was away (e.g. the laptop was lost and an admin
freed the report), nothing is overwritten: the offline work is uploaded as a NEW report that points to the
original, for someone to review.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from psycopg import sql

from .db import CHILD_TABLES, EDITABLE, Database, LockedError, _s
from .local_store import LocalStore, _now

PUSH_ORDER = ("attachments", "components", "materials", "characteristics", "coc_items")
REPORT_SYNC_FIELDS = sorted(EDITABLE["reports"] - {"locked", "report_number"})


@dataclass
class SyncResult:
    uploaded: list[tuple[str, str]] = field(default_factory=list)      # (offline number, server number)
    released: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        for a, b in self.uploaded:
            parts.append(f"✔ {a} → {b}" if a != b else f"✔ {b}")
        parts += [f"✔ released {r}" for r in self.released]
        parts += [f"⚠ {c}" for c in self.conflicts]
        parts += [f"✖ {e}" for e in self.errors]
        return "\n".join(parts) or "Nothing to sync."


# ============================================================================== take offline
def take_offline(db: Database, store: LocalStore, report_id: int) -> dict:
    if store.by_server_id(report_id):
        raise ValueError("This report is already on this PC for offline work.")

    def f(c):
        with c.transaction():
            r = c.execute("SELECT * FROM reports WHERE id=%s FOR UPDATE", (report_id,)).fetchone()
            if r is None:
                raise ValueError("Report not found.")
            if r["locked"]:
                raise LockedError(f"{r['report_number']} is {r['status']} and locked - approved reports are not "
                                  "taken offline. Reopen it first if it must change.")
            if r["checked_out_host"] and r["checked_out_host"] != db.host:
                raise LockedError(f"{r['report_number']} is already checked out to {r['checked_out_by']} on "
                                  f"{r['checked_out_host']}.")
            r = c.execute("UPDATE reports SET checked_out_by=%s, checked_out_host=%s, checked_out_at=now() "
                          "WHERE id=%s RETURNING *", (db.user, db.host, report_id)).fetchone()
            db._audit(c, report_id, "reports", report_id, "CHECK OUT", "offline", None, f"{db.user} on {db.host}")
            return _snapshot(c, r)
    snap = db._run(f)
    try:
        return _store_snapshot(store, snap)
    except Exception:
        release(db, report_id, force=False)          # could not save the copy: do not leave it locked
        raise


def _snapshot(c, r) -> dict:
    snap = {"report": dict(r)}
    for t in CHILD_TABLES:
        snap[t] = [dict(x) for x in c.execute(sql.SQL("SELECT * FROM {} WHERE report_id=%s ORDER BY id")
                                             .format(sql.Identifier(t)), (r["id"],)).fetchall()]
    return snap


def _store_snapshot(store: LocalStore, snap: dict) -> dict:
    r = snap["report"]
    with store._tx():
        lid = store._new_id("reports")
        cols = [k for k in store._colnames("reports") if k in r and k != "id"]
        vals = [_local_val(r[k]) for k in cols]
        store.conn.execute(
            f"INSERT INTO reports (id, {', '.join(cols)}, server_id, origin, checked_out_at_local, "
            f"base_server_version) VALUES ({', '.join('?' * (len(cols) + 5))})",
            [lid] + vals + [r["id"], "server", _now(), r["version"]])
        amap = {}
        for t in PUSH_ORDER:
            tcols = [k for k in store._colnames(t) if k not in ("id", "report_id", "server_id")]
            for row in snap[t]:
                nid = store._new_id(t)
                if t == "attachments":
                    amap[row["id"]] = nid
                vals = {k: row.get(k) for k in tcols if k in row}
                if t == "characteristics" and vals.get("balloon_attachment_id"):
                    vals["balloon_attachment_id"] = amap.get(vals["balloon_attachment_id"])
                keys = list(vals)
                store.conn.execute(
                    f"INSERT INTO {t} (id, report_id, server_id, {', '.join(keys)}) "
                    f"VALUES ({', '.join('?' * (len(keys) + 3))})",
                    [nid, lid, row["id"]] + [_local_val(vals[k]) for k in keys])
    return store.get_report(lid)


def _local_val(v):
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, memoryview):
        return bytes(v)
    if hasattr(v, "isoformat") and not isinstance(v, (int, float)):
        return v.isoformat()
    return v


# ============================================================================== push back
def push(db: Database, store: LocalStore, local_id: int, keep_offline: bool = False) -> tuple[str, str, str]:
    """Returns (offline number, server number, note)."""
    L = store.get_report(local_id)
    if L is None:
        raise ValueError("Offline report not found.")
    local_rows = {t: store.raw_rows(t, local_id) for t in PUSH_ORDER}
    audit = sorted(store.audit_for(local_id, 100000), key=lambda a: (a["at"], -a["id"]))

    def f(c):
        with c.transaction():
            note = ""
            sid = L.get("server_id")
            srv = None
            if sid:
                srv = c.execute("SELECT * FROM reports WHERE id=%s FOR UPDATE", (sid,)).fetchone()
                if srv is None or srv["checked_out_host"] != db.host:
                    why = "was deleted on the server" if srv is None else \
                        "was released on the server while this PC was offline"
                    note = (f"The check-out of {L['report_number']} {why}. Your offline work was uploaded as a NEW "
                            f"report so nothing is overwritten - compare the two and keep the right one.")
                    srv, sid = None, None
            if srv is None:
                # new report on the server (created offline, or conflict copy)
                vals = {k: L[k] for k in REPORT_SYNC_FIELDS if k in L}
                vals["status"] = vals.get("status") if vals.get("status") in ("Draft", "In Review") else "Draft"
                if note:
                    vals["comments"] = (f"OFFLINE COPY of {L['report_number']} (check-out conflict). "
                                        + (vals.get("comments") or "")).strip()
                cols = list(vals) + ["created_by", "updated_by", "report_number"]
                row = c.execute(sql.SQL("INSERT INTO reports ({}) VALUES ({}) RETURNING *").format(
                    sql.SQL(",").join(map(sql.Identifier, cols)), sql.SQL(",").join(sql.Placeholder() * len(cols))),
                    list(vals.values()) + [L["created_by"] or db.user, db.user, ""]).fetchone()
                from datetime import datetime

                from .report_types import number_prefix
                num = f"{number_prefix(row['report_type'])}-{datetime.now():%y%m}-{row['id']:04d}"
                srv = c.execute("UPDATE reports SET report_number=%s WHERE id=%s RETURNING *",
                                (num, row["id"])).fetchone()
                sid = srv["id"]
                db._audit(c, sid, "reports", sid, "CREATE", "offline", L["report_number"], num)
                server_rows = {t: {} for t in PUSH_ORDER}
            else:
                changes = {k: L[k] for k in REPORT_SYNC_FIELDS if k in L and _s(L[k]) != _s(srv.get(k))}
                if changes.get("status") == "Approved":
                    changes.pop("status")
                if changes:
                    sets = sql.SQL(",").join([sql.SQL("{}=%s").format(sql.Identifier(k)) for k in changes] +
                                             [sql.SQL("updated_by=%s"), sql.SQL("updated_at=now()"),
                                              sql.SQL("version=version+1")])
                    c.execute(sql.SQL("UPDATE reports SET {} WHERE id=%s").format(sets),
                              list(changes.values()) + [db.user, sid])
                server_rows = {t: {r["id"]: r for r in c.execute(
                    sql.SQL("SELECT * FROM {} WHERE report_id=%s").format(sql.Identifier(t)), (sid,)).fetchall()}
                    for t in PUSH_ORDER}
            idmap: dict[tuple, int] = {("reports", local_id): sid}     # (table, local id) -> server id
            keep: dict[str, set] = {t: set() for t in PUSH_ORDER}
            for t in PUSH_ORDER:
                for lr in local_rows[t]:
                    vals = {k: lr[k] for k in EDITABLE[t] if k in lr}
                    if t == "characteristics" and vals.get("balloon_attachment_id") is not None:
                        vals["balloon_attachment_id"] = idmap.get(("attachments", vals["balloon_attachment_id"]))
                    if t == "attachments":
                        vals["include_in_report"] = bool(vals.get("include_in_report"))
                    tsid = lr.get("server_id")
                    if tsid and tsid in server_rows[t]:
                        cur = server_rows[t][tsid]
                        diff = {k: v for k, v in vals.items() if _s(cur.get(k)) != _s(v)}
                        if diff:
                            sets = sql.SQL(",").join([sql.SQL("{}=%s").format(sql.Identifier(k)) for k in diff] +
                                                     [sql.SQL("updated_by=%s"), sql.SQL("updated_at=now()"),
                                                      sql.SQL("version=version+1")])
                            c.execute(sql.SQL("UPDATE {} SET {} WHERE id=%s").format(sql.Identifier(t), sets),
                                      list(diff.values()) + [lr["updated_by"] or db.user, tsid])
                        keep[t].add(tsid)
                        idmap[(t, lr["id"])] = tsid
                    else:
                        if t == "attachments":
                            data = store.attachment_bytes(lr["id"])
                            extra = {"mime": lr["mime"], "size_bytes": lr["size_bytes"], "sha256": lr["sha256"],
                                     "page_count": lr["page_count"], "data": data,
                                     "uploaded_by": lr["uploaded_by"] or db.user}
                            vals = {**vals, **extra}
                        cols = ["report_id", "updated_by"] + list(vals)
                        new = c.execute(sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING id").format(
                            sql.Identifier(t), sql.SQL(",").join(map(sql.Identifier, cols)),
                            sql.SQL(",").join(sql.Placeholder() * len(cols))),
                            [sid, lr["updated_by"] or db.user] + list(vals.values())).fetchone()["id"]
                        keep[t].add(new)
                        idmap[(t, lr["id"])] = new
            # rows deleted offline (characteristics first so balloon references go before their drawing)
            for t in ("characteristics", "components", "materials", "coc_items", "attachments"):
                gone = [i for i in server_rows[t] if i not in keep[t]]
                if gone:
                    c.execute(sql.SQL("DELETE FROM {} WHERE id = ANY(%s)").format(sql.Identifier(t)), (gone,))
            # the offline history, with the times it really happened
            for a in audit:
                if a["action"] == "CREATE" and a["table_name"] == "reports":
                    continue
                c.execute(
                    "INSERT INTO audit_log(report_id, table_name, record_id, action, field, old_value, new_value, "
                    "user_name, host, at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (sid, a["table_name"], idmap.get((a["table_name"], a["record_id"]),
                                                     a["record_id"] if (a["record_id"] or 0) > 0 else None),
                     a["action"], a["field"], a["old_value"], a["new_value"], a["user_name"],
                     f"{a['host']} (offline)", a["at"]))
            db._audit(c, sid, "reports", sid, "SYNC", "offline", L["report_number"],
                      f"{sum(len(v) for v in local_rows.values())} rows, {len(audit)} offline changes")
            if keep_offline and not note:
                c.execute("UPDATE reports SET checked_out_by=%s, checked_out_host=%s WHERE id=%s",
                          (db.user, db.host, sid))
                snap = _snapshot(c, c.execute("SELECT * FROM reports WHERE id=%s", (sid,)).fetchone())
            else:
                c.execute("UPDATE reports SET checked_out_by='', checked_out_host='', checked_out_at=NULL, "
                          "updated_at=now() WHERE id=%s", (sid,))
                db._audit(c, sid, "reports", sid, "CHECK IN", "offline", f"{db.user} on {db.host}", None)
                snap = None
            return srv["report_number"], note, snap

    server_number, note, snap = db._run(f)
    # the server has it now: remove the offline copy (drawings are CUI - do not leave them on the laptop)
    with store._tx():
        for t in CHILD_TABLES:
            store.conn.execute(f"DELETE FROM {t} WHERE report_id=?", (local_id,))
        store.conn.execute("DELETE FROM audit_log WHERE report_id=?", (local_id,))
        store.conn.execute("DELETE FROM reports WHERE id=?", (local_id,))
    if snap is not None:
        _store_snapshot(store, snap)
    store.vacuum_soon()
    return L["report_number"], server_number, note


# ============================================================================== release / sync all
def release(db: Database, report_id: int, force: bool = False) -> None:
    """Give a checked-out report back without uploading anything (discarded offline copy, or an admin
    freeing a report whose laptop is gone - force=True)."""
    def f(c):
        with c.transaction():
            r = c.execute("SELECT * FROM reports WHERE id=%s FOR UPDATE", (report_id,)).fetchone()
            if r is None or not r["checked_out_host"]:
                return
            if r["checked_out_host"] != db.host and not force:
                raise LockedError(f"{r['report_number']} is checked out to {r['checked_out_host']}.")
            c.execute("UPDATE reports SET checked_out_by='', checked_out_host='', checked_out_at=NULL WHERE id=%s",
                      (report_id,))
            db._audit(c, report_id, "reports", report_id, "RELEASE" if force else "CHECK IN", "offline",
                      f"{r['checked_out_by']} on {r['checked_out_host']}",
                      f"released by {db.user} on {db.host}" if force else "offline copy discarded")
    db._run(f)


def refresh_snapshot(db: Database, store: LocalStore) -> None:
    """Company settings, logo, OCR settings and the gage list, for use while offline."""
    try:
        store.snapshot_meta(db.get_meta(), db.gages(), db.vendors(active_only=False))
        try:
            store.snapshot_rules([{k: v for k, v in r.items() if k != "examples"} for r in db.reader_rules()])
        except Exception:
            pass
        try:
            store.snapshot_ocr_learned([{k: v for k, v in r.items() if k != "sources"}
                                        for r in db.ocr_learned("active")])
        except Exception:
            pass
        store.set_local_meta("snapshot_at", _now())
    except Exception:
        pass


def sync_all(db: Database, store: LocalStore, keep_offline: Optional[set] = None,
             progress: Optional[Callable[[str, float], None]] = None) -> SyncResult:
    res = SyncResult()
    keep_offline = keep_offline or set()
    for p in store.pending():
        try:
            if p["action"] == "release":
                release(db, p["server_report_id"])
                res.released.append(p["detail"] or str(p["server_report_id"]))
            store.clear_pending(p["id"])
        except Exception as e:  # noqa: BLE001
            res.errors.append(f"release {p['detail']}: {e}")
    reps = store.offline_reports()
    for i, r in enumerate(reps):
        if progress:
            progress(f"Uploading {r['report_number']}…", i / max(1, len(reps)))
        try:
            a, b, note = push(db, store, r["id"], keep_offline=r["id"] in keep_offline)
            res.uploaded.append((a, b))
            if note:
                res.conflicts.append(note)
        except Exception as e:  # noqa: BLE001
            res.errors.append(f"{r['report_number']}: {e}")
    refresh_snapshot(db, store)
    return res
