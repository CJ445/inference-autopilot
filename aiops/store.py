import json
import os
import sqlite3
from pathlib import Path

from aiops.audit import AuditLog
from aiops.incident import Incident


class AuditTampered(Exception):
    pass


class StoreUnavailable(Exception):
    """SQLite could not be read or written; control operations must fail closed (PRD §86)."""


class Store:
    """SQLite persistence for incidents, pending proposals and the audit chain (PRD §58)."""

    def __init__(self, path, readonly=False):
        self.path, self.readonly = str(path), readonly
        if readonly:  # status/doctor: never create, never write
            if not os.path.exists(self.path):
                raise StoreUnavailable(f"no database at {self.path}")
            return
        with self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS incidents (id TEXT PRIMARY KEY, data TEXT);
                CREATE TABLE IF NOT EXISTS pending (id TEXT PRIMARY KEY, data TEXT);
                CREATE TABLE IF NOT EXISTS audit (
                    seq INTEGER PRIMARY KEY, event TEXT, data TEXT, prev TEXT, hash TEXT);
            """)

    def _conn(self):
        if self.readonly:
            return sqlite3.connect(f"{Path(self.path).resolve().as_uri()}?mode=ro", uri=True)
        return sqlite3.connect(self.path)

    def save(self, incidents, pending, audit):
        try:
            self._save(incidents, pending, audit)
        except sqlite3.Error as e:
            raise StoreUnavailable(str(e)) from e

    def _save(self, incidents, pending, audit):
        with self._conn() as c:
            for inc in incidents:
                c.execute("INSERT OR REPLACE INTO incidents VALUES (?, ?)",
                          (inc.incident_id, json.dumps(inc.to_dict())))
            c.execute("DELETE FROM pending")
            c.executemany("INSERT INTO pending VALUES (?, ?)",
                          [(i, json.dumps(p)) for i, p in pending.items()])
            c.executemany(  # audit rows are insert-only
                "INSERT OR IGNORE INTO audit VALUES (?, ?, ?, ?, ?)",
                [(n, e["event"], json.dumps(e["data"], sort_keys=True), e["prev"], e["hash"])
                 for n, e in enumerate(audit.events)])

    def load(self):
        try:
            incidents, pending, audit = self._load()
        except sqlite3.Error as e:
            raise StoreUnavailable(str(e)) from e
        if not audit.verify():
            raise AuditTampered("audit hash chain does not verify")
        return incidents, pending, audit

    def _load(self):
        with self._conn() as c:
            incidents = [Incident.from_dict(json.loads(d)) for (d,) in
                         c.execute("SELECT data FROM incidents ORDER BY id")]
            pending = {i: json.loads(d) for i, d in c.execute("SELECT id, data FROM pending")}
            rows = c.execute("SELECT event, data, prev, hash FROM audit ORDER BY seq")
            audit = AuditLog()
            audit.events = [{"event": e, "data": json.loads(d), "prev": p, "hash": h}
                            for e, d, p, h in rows]
        return incidents, pending, audit
