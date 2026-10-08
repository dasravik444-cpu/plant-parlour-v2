"""Outreach state: its own SQLite file, separate from the lead-generation database."""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager

from ..util import jdump, jload

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
-- one e-mail conversation per recipient address
CREATE TABLE IF NOT EXISTS threads (
  email TEXT PRIMARY KEY,
  lead_key TEXT NOT NULL,
  lead_id TEXT, business TEXT, domain TEXT,
  stage TEXT NOT NULL,                 -- active | replied | opted_out | bounced | completed | stopped
  step INTEGER NOT NULL DEFAULT 0,     -- e-mails sent so far in this conversation
  subject TEXT, msgids TEXT NOT NULL DEFAULT '[]',
  first_sent REAL, last_sent REAL, next_due REAL,
  reply_kind TEXT, reply_at REAL, reply_snippet TEXT,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS threads_lead ON threads(lead_key);
CREATE INDEX IF NOT EXISTS threads_domain ON threads(domain);
CREATE TABLE IF NOT EXISTS sends (
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL, lead_key TEXT NOT NULL, step INTEGER NOT NULL,
  msgid TEXT, sent_at REAL NOT NULL, day TEXT NOT NULL,
  status TEXT NOT NULL,                -- sent | invalid | bounced | failed
  error TEXT
);
CREATE INDEX IF NOT EXISTS sends_day ON sends(day);
CREATE TABLE IF NOT EXISTS replies (
  id INTEGER PRIMARY KEY,
  email TEXT, lead_key TEXT, kind TEXT NOT NULL, received_at REAL NOT NULL, snippet TEXT, imap_uid INTEGER
);
CREATE TABLE IF NOT EXISTS suppression (
  value TEXT PRIMARY KEY,              -- lower-case e-mail, E.164 phone, or @domain
  kind TEXT NOT NULL, reason TEXT, source TEXT, added_at REAL NOT NULL,
  in_sheet INTEGER NOT NULL DEFAULT 0  -- already listed in the Do Not Contact tab
);
-- one WhatsApp queue entry per number
CREATE TABLE IF NOT EXISTS wa (
  phone TEXT PRIMARY KEY,
  lead_key TEXT NOT NULL, lead_id TEXT, business TEXT,
  reason TEXT NOT NULL,                -- opted_in | listed | mobile
  queued_day TEXT NOT NULL,
  result TEXT NOT NULL DEFAULT '',     -- '' (to do) | sent | not_on_whatsapp | replied | interested | not_interested | skipped
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS wa_lead ON wa(lead_key);
"""


class OutreachStore:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=DELETE")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(SCHEMA)
        self._depth = 0

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

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def integrity_ok(self) -> bool:
        try:
            return self.scalar("PRAGMA integrity_check") == "ok"
        except sqlite3.DatabaseError:
            return False

    def backup_to(self, dest: str) -> None:
        if os.path.exists(dest):
            os.remove(dest)
        self.conn.execute("VACUUM INTO ?", (dest,))

    # -- meta ---------------------------------------------------------------------
    def get(self, key: str, default=None):
        row = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return default if row is None else jload(row[0], row[0])

    def set(self, key: str, value) -> None:
        self.conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (key, jdump(value)))

    # -- suppression ----------------------------------------------------------------
    def suppress(self, value: str, kind: str, reason: str, source: str, in_sheet: bool = False) -> bool:
        """Add to the do-not-contact list. Returns True if it was not listed yet."""
        value = (value or "").strip().lower()
        if not value:
            return False
        cur = self.conn.execute("INSERT OR IGNORE INTO suppression(value,kind,reason,source,added_at,in_sheet) VALUES(?,?,?,?,?,?)",
                                (value, kind, reason, source, time.time(), 1 if in_sheet else 0))
        return cur.rowcount > 0

    def suppressed(self, *values: str) -> bool:
        vals = [v.strip().lower() for v in values if v]
        if not vals:
            return False
        marks = ",".join("?" for _ in vals)
        return self.scalar(f"SELECT COUNT(*) FROM suppression WHERE value IN ({marks})", vals, 0) > 0

    # -- threads --------------------------------------------------------------------
    def thread(self, email: str):
        return self.one("SELECT * FROM threads WHERE email=?", (email.lower(),))

    def msgids(self, row) -> list[str]:
        return jload(row["msgids"], []) or []
