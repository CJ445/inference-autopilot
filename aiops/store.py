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

    def __init__(self, path, readonly=False, mode=None):
        """`mode=None` is the real store (its schema is unchanged). A SIMULATION store carries a
        marker row; a real store refuses a marked database and a simulation store refuses an
        unmarked one, so the two can never be mixed (fail closed)."""
        self.path, self.readonly, self.mode = str(path), readonly, mode
        if readonly:  # status/doctor: never create, never write
            if not os.path.exists(self.path):
                raise StoreUnavailable(f"no database at {self.path}")
            try:
                self._check_mode(create=False)
            except sqlite3.Error as e:
                raise StoreUnavailable(str(e)) from e
            return
        try:
            with self._conn() as c:
                c.executescript("""
                    CREATE TABLE IF NOT EXISTS incidents (id TEXT PRIMARY KEY, data TEXT);
                    CREATE TABLE IF NOT EXISTS pending (id TEXT PRIMARY KEY, data TEXT);
                    CREATE TABLE IF NOT EXISTS audit (
                        seq INTEGER PRIMARY KEY, event TEXT, data TEXT, prev TEXT, hash TEXT);
                """)
            self._check_mode(create=True)
        except sqlite3.Error as e:
            raise StoreUnavailable(str(e)) from e

    def _check_mode(self, create):
        with self._conn() as c:
            has_meta = c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
            marker = None
            if has_meta:
                row = c.execute("SELECT value FROM meta WHERE key='mode'").fetchone()
                marker = row[0] if row else None
            if self.mode is None:
                if marker is not None:
                    raise StoreUnavailable(
                        f"{self.path} is a {marker} database; a real store will not open it")
                return
            if marker is None:
                # Only a brand-new, empty database may become a simulation store.
                in_use = (c.execute("SELECT 1 FROM incidents LIMIT 1").fetchone()
                          or c.execute("SELECT 1 FROM audit LIMIT 1").fetchone())
                if not create or in_use:
                    raise StoreUnavailable(f"{self.path} is not a {self.mode} database")
                c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
                c.execute("INSERT OR IGNORE INTO meta VALUES ('mode', ?)", (self.mode,))
            elif marker != self.mode:
                raise StoreUnavailable(f"{self.path} is a {marker} database, not {self.mode}")

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
            audit = AuditLog(self.mode)
            audit.events = [{"event": e, "data": json.loads(d), "prev": p, "hash": h}
                            for e, d, p, h in rows]
        return incidents, pending, audit
