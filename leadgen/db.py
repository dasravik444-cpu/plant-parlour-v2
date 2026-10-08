"""SQLite state: plan, durable task queue, places, contacts (with evidence), runs, budgets.

Every unit of work is a row in `tasks`. Results and the follow-up tasks they
create are committed in one transaction, so a crash (Android killing the
process, a cancelled CI job) can never lose finished work or run it twice.
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager

from .util import jdump, jload, norm_text

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS parts (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  localities TEXT NOT NULL DEFAULT '[]',
  center_lat REAL, center_lng REAL,
  weight REAL NOT NULL DEFAULT 0,
  cells INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending',
  scheduled_day INTEGER,
  started_on TEXT, finished_on TEXT
);

CREATE TABLE IF NOT EXISTS open_places (
  id TEXT PRIMARY KEY,                -- Overture place id (stable across releases)
  release TEXT NOT NULL,
  name TEXT NOT NULL,
  lat REAL NOT NULL, lng REAL NOT NULL,
  code TEXT, basic TEXT, alternates TEXT,
  phones TEXT, emails TEXT, websites TEXT, socials TEXT,
  street TEXT, locality TEXT, postcode TEXT,
  confidence REAL, brand TEXT, status TEXT, datasets TEXT
);
CREATE INDEX IF NOT EXISTS idx_open_places_lat ON open_places(lat, lng);

CREATE TABLE IF NOT EXISTS cells (
  id INTEGER PRIMARY KEY,
  part_id INTEGER NOT NULL REFERENCES parts(id),
  seq INTEGER NOT NULL,
  lat REAL NOT NULL, lng REAL NOT NULL,
  size_km REAL NOT NULL, zoom INTEGER NOT NULL,
  osm_count INTEGER NOT NULL DEFAULT 0,
  name TEXT
);
CREATE INDEX IF NOT EXISTS idx_cells_part ON cells(part_id, seq);

CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  key TEXT NOT NULL UNIQUE,
  seq INTEGER NOT NULL DEFAULT 0,
  part_id INTEGER,
  place_key TEXT,
  payload TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  next_at REAL NOT NULL DEFAULT 0,
  last_error TEXT,
  result TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_ready ON tasks(kind, status, next_at, seq);
CREATE INDEX IF NOT EXISTS idx_tasks_place ON tasks(place_key);
CREATE INDEX IF NOT EXISTS idx_tasks_part ON tasks(part_id, status);

CREATE TABLE IF NOT EXISTS places (
  key TEXT PRIMARY KEY,
  lead_no INTEGER UNIQUE,
  name TEXT NOT NULL,
  norm_name TEXT NOT NULL,
  category TEXT,
  gcategories TEXT NOT NULL DEFAULT '[]',
  address TEXT, area TEXT, city TEXT, lat REAL, lng REAL,
  rating REAL, reviews INTEGER,
  website TEXT, maps_url TEXT, place_id TEXT, data_id TEXT,
  description TEXT,
  status_text TEXT,
  provider TEXT NOT NULL,
  query TEXT, part_id INTEGER, cell_id INTEGER,
  first_seen REAL NOT NULL, updated_at REAL NOT NULL, found_date TEXT NOT NULL,
  excluded TEXT,
  merged_into TEXT,
  qualified INTEGER NOT NULL DEFAULT 0,
  qualified_date TEXT,
  sync_state TEXT NOT NULL DEFAULT 'pending',
  synced_at REAL,
  sheet_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_places_pid ON places(place_id);
CREATE INDEX IF NOT EXISTS idx_places_did ON places(data_id);
CREATE INDEX IF NOT EXISTS idx_places_sync ON places(sync_state);
CREATE INDEX IF NOT EXISTS idx_places_found ON places(found_date);
CREATE INDEX IF NOT EXISTS idx_places_geo ON places(lat, lng);

CREATE TABLE IF NOT EXISTS contacts (
  id INTEGER PRIMARY KEY,
  place_key TEXT NOT NULL REFERENCES places(key),
  kind TEXT NOT NULL,
  value TEXT NOT NULL,
  label TEXT,
  source TEXT NOT NULL,
  source_url TEXT,
  confidence TEXT NOT NULL,
  evidence TEXT,
  sources TEXT NOT NULL DEFAULT '[]',
  found_at REAL NOT NULL,
  UNIQUE(place_key, kind, value)
);
CREATE INDEX IF NOT EXISTS idx_contacts_place ON contacts(place_key);
CREATE INDEX IF NOT EXISTS idx_contacts_value ON contacts(kind, value);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  run_date TEXT NOT NULL,
  started_at REAL NOT NULL,
  finished_at REAL,
  status TEXT NOT NULL DEFAULT 'running',
  summary TEXT
);

CREATE TABLE IF NOT EXISTS budget (
  name TEXT NOT NULL, period TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (name, period)
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, run_id INTEGER, level TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL
);
"""

CONF_RANK = {"low": 1, "medium": 2, "high": 3}


class DB:
    def __init__(self, path: str):
        self.path = path
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=DELETE")
        self.conn.execute("PRAGMA synchronous=FULL")
        self._depth = 0
        self.migrate()

    # -- transactions ---------------------------------------------------------
    @contextmanager
    def tx(self):
        if self._depth:
            self._depth += 1
            try:
                yield self.conn
            finally:
                self._depth -= 1
            return
        self.conn.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield self.conn
        except BaseException:
            self._depth = 0
            self.conn.execute("ROLLBACK")
            raise
        else:
            self._depth = 0
            self.conn.execute("COMMIT")

    def q(self, sql: str, params=()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params=()):
        return self.conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params=(), default=None):
        row = self.conn.execute(sql, params).fetchone()
        return default if row is None or row[0] is None else row[0]

    def migrate(self):
        # executescript manages its own transaction, so it runs outside tx().
        self.conn.executescript(SCHEMA)
        ver = self.get_meta("schema_version")
        if ver is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))
        elif int(ver) > SCHEMA_VERSION:
            raise RuntimeError(f"state database {self.path} was created by a newer version (schema {ver})")

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    # -- meta -----------------------------------------------------------------
    def get_meta(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return default if row is None else row[0]

    def set_meta(self, key: str, value) -> None:
        self.conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (key, value if isinstance(value, str) else jdump(value)))

    def integrity_ok(self) -> bool:
        try:
            return self.scalar("PRAGMA integrity_check") == "ok"
        except sqlite3.DatabaseError:
            return False

    def backup_to(self, dest: str) -> None:
        if os.path.exists(dest):
            os.remove(dest)
        self.conn.execute("VACUUM INTO ?", (dest,))

    # -- events ---------------------------------------------------------------
    def event(self, level: str, kind: str, message: str, run_id: int | None = None) -> None:
        self.conn.execute("INSERT INTO events(ts,run_id,level,kind,message) VALUES(?,?,?,?,?)",
                          (time.time(), run_id, level, kind, message[:1000]))

    # -- tasks ----------------------------------------------------------------
    def enqueue(self, kind: str, key: str, payload: dict | None = None, *, seq: int = 0, part_id: int | None = None,
                place_key: str | None = None, next_at: float = 0.0) -> bool:
        now = time.time()
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO tasks(kind,key,seq,part_id,place_key,payload,status,attempts,next_at,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,'pending',0,?,?,?)",
            (kind, key, seq, part_id, place_key, jdump(payload or {}), next_at, now, now))
        return cur.rowcount == 1

    def ready_tasks(self, kind: str, limit: int, now: float | None = None, part_id: int | None = None) -> list[sqlite3.Row]:
        now = time.time() if now is None else now
        if part_id is None:
            return self.q("SELECT * FROM tasks WHERE kind=? AND status='pending' AND next_at<=? ORDER BY seq, id LIMIT ?",
                          (kind, now, limit))
        return self.q("SELECT * FROM tasks WHERE kind=? AND status='pending' AND next_at<=? AND part_id=? ORDER BY seq, id LIMIT ?",
                      (kind, now, part_id, limit))

    def set_running(self, task_id: int) -> None:
        self.conn.execute("UPDATE tasks SET status='running', updated_at=? WHERE id=?", (time.time(), task_id))

    def complete(self, task_id: int, result: dict | None = None, status: str = "done") -> None:
        self.conn.execute("UPDATE tasks SET status=?, result=?, last_error=NULL, updated_at=? WHERE id=?",
                          (status, jdump(result or {}), time.time(), task_id))

    def fail(self, task_id: int, error: str, *, max_attempts: int = 3, backoff: tuple = (900, 6 * 3600, 24 * 3600)) -> str:
        row = self.one("SELECT attempts FROM tasks WHERE id=?", (task_id,))
        attempts = (row["attempts"] if row else 0) + 1
        if attempts >= max_attempts:
            status, next_at = "failed", 0.0
        else:
            status, next_at = "pending", time.time() + backoff[min(attempts - 1, len(backoff) - 1)]
        self.conn.execute("UPDATE tasks SET status=?, attempts=?, next_at=?, last_error=?, updated_at=? WHERE id=?",
                          (status, attempts, next_at, (error or "")[:500], time.time(), task_id))
        return status

    def defer(self, task_id: int, seconds: float, reason: str) -> None:
        """Postpone without consuming an attempt (provider down, network down, deadline)."""
        self.conn.execute("UPDATE tasks SET status='pending', next_at=?, last_error=?, updated_at=? WHERE id=?",
                          (time.time() + seconds, ("deferred: " + reason)[:500], time.time(), task_id))

    def reset_stale_running(self) -> int:
        cur = self.conn.execute("UPDATE tasks SET status='pending', updated_at=? WHERE status='running'", (time.time(),))
        return cur.rowcount

    def task_counts(self) -> dict:
        out: dict = {}
        for r in self.q("SELECT kind, status, COUNT(*) n FROM tasks GROUP BY kind, status"):
            out.setdefault(r["kind"], {})[r["status"]] = r["n"]
        return out

    def pending_for_place(self, place_key: str) -> int:
        return self.scalar("SELECT COUNT(*) FROM tasks WHERE place_key=? AND status IN ('pending','running')", (place_key,), 0)

    # -- places ---------------------------------------------------------------
    def get_place(self, key: str):
        return self.one("SELECT * FROM places WHERE key=?", (key,))

    def find_existing(self, place_id: str, data_id: str, key: str):
        row = self.one("SELECT key, merged_into FROM places WHERE key=?", (key,))
        if row is None and place_id:
            row = self.one("SELECT key, merged_into FROM places WHERE place_id=? LIMIT 1", (place_id,))
        if row is None and data_id:
            row = self.one("SELECT key, merged_into FROM places WHERE data_id=? LIMIT 1", (data_id,))
        if row is None:
            return None
        return row["merged_into"] or row["key"]

    def nearby_places(self, lat: float, lng: float, dlat: float = 0.003, dlng: float = 0.003) -> list[sqlite3.Row]:
        return self.q("SELECT key, name, norm_name, lat, lng, merged_into FROM places WHERE lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?",
                      (lat - dlat, lat + dlat, lng - dlng, lng + dlng))

    def insert_place(self, rec: dict) -> None:
        now = time.time()
        rec = dict(rec)
        rec.setdefault("first_seen", now)
        rec["updated_at"] = now
        rec["norm_name"] = norm_text(rec.get("name"))
        rec["gcategories"] = jdump(rec.get("gcategories") or [])
        cols = ",".join(rec.keys())
        marks = ",".join("?" for _ in rec)
        self.conn.execute(f"INSERT INTO places({cols}) VALUES({marks})", tuple(rec.values()))

    def update_place(self, key: str, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = time.time()
        if "gcategories" in fields and not isinstance(fields["gcategories"], str):
            fields["gcategories"] = jdump(fields["gcategories"])
        sets = ",".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE places SET {sets} WHERE key=?", (*fields.values(), key))

    def mark_dirty(self, key: str) -> None:
        self.conn.execute("UPDATE places SET sync_state='pending', updated_at=? WHERE key=? AND sync_state!='pending'",
                          (time.time(), key))

    # -- contacts -------------------------------------------------------------
    def add_contact(self, place_key: str, kind: str, value: str, *, label: str = "", source: str, source_url: str = "",
                    confidence: str = "medium", evidence: str = "") -> bool:
        """Insert or corroborate a contact. Returns True if the contact is new for this place."""
        if not value:
            return False
        src_entry = f"{source}|{source_url}"[:300]
        row = self.one("SELECT id, confidence, sources, label FROM contacts WHERE place_key=? AND kind=? AND value=?",
                       (place_key, kind, value))
        if row is None:
            self.conn.execute(
                "INSERT INTO contacts(place_key,kind,value,label,source,source_url,confidence,evidence,sources,found_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (place_key, kind, value, label, source, source_url[:500], confidence, evidence[:300], jdump([src_entry]), time.time()))
            self.mark_dirty(place_key)
            return True
        sources = jload(row["sources"], []) or []
        changed = False
        if src_entry not in sources:
            sources.append(src_entry)
            changed = True
        new_conf = row["confidence"]
        upgraded = CONF_RANK.get(confidence, 0) > CONF_RANK.get(row["confidence"], 0)
        if upgraded:
            new_conf = confidence
            changed = True
        # A confirmed find replaces the note that explained why the value was unverified.
        new_label = label if (upgraded and label) else (row["label"] or label)
        if changed or new_label != row["label"]:
            self.conn.execute("UPDATE contacts SET sources=?, confidence=?, label=? WHERE id=?",
                              (jdump(sources[:12]), new_conf, new_label, row["id"]))
            self.mark_dirty(place_key)
        return False

    def contacts_for(self, place_key: str) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM contacts WHERE place_key=? ORDER BY kind, CASE confidence WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, id",
                      (place_key,))

    def remove_contact(self, place_key: str, kind: str, value: str) -> None:
        self.conn.execute("DELETE FROM contacts WHERE place_key=? AND kind=? AND value=?", (place_key, kind, value))
        self.mark_dirty(place_key)

    # -- runs -----------------------------------------------------------------
    def start_run(self, run_date: str) -> int:
        cur = self.conn.execute("INSERT INTO runs(run_date, started_at, status) VALUES(?,?, 'running')", (run_date, time.time()))
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, summary: dict) -> None:
        self.conn.execute("UPDATE runs SET finished_at=?, status=?, summary=? WHERE id=?",
                          (time.time(), status, jdump(summary), run_id))

    def close_abandoned_runs(self) -> int:
        cur = self.conn.execute("UPDATE runs SET status='interrupted', finished_at=COALESCE(finished_at, started_at) WHERE status='running'")
        return cur.rowcount

    # -- budgets --------------------------------------------------------------
    def budget_take(self, name: str, period: str, cap: int, amount: int = 1) -> bool:
        with self.tx():
            used = self.scalar("SELECT used FROM budget WHERE name=? AND period=?", (name, period), 0)
            if used + amount > cap:
                return False
            self.conn.execute("INSERT INTO budget(name,period,used) VALUES(?,?,?) ON CONFLICT(name,period) DO UPDATE SET used=used+?",
                              (name, period, amount, amount))
            return True

    def budget_used(self, name: str, period: str) -> int:
        return int(self.scalar("SELECT used FROM budget WHERE name=? AND period=?", (name, period), 0))
