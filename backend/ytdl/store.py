"""The library on disk: SQLite (jobs, items, events), its backup, and the old state.json."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path

from . import config
from .config import log

# ---------------------------------------------------------------- storage

class Store:
    """The library on disk: SQLite, one row per playlist and per video, so a save only writes what changed.

    WAL journaling with synchronous=FULL survives crashes and power loss; a damaged database is
    moved aside (never deleted) and the latest backup is used instead.
    """

    SCHEMA = """
        CREATE TABLE IF NOT EXISTS jobs  (id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS items (job_id TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
                                          PRIMARY KEY (job_id, id));
        CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL NOT NULL,
                                           job_id TEXT, job_title TEXT, item_id TEXT, title TEXT, url TEXT,
                                           status TEXT, kind TEXT, error TEXT, bytes INTEGER, seconds REAL,
                                           filepath TEXT, hidden INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS events_time ON events (time);
    """
    EVENT_COLS = ("time", "job_id", "job_title", "item_id", "title", "url", "status", "kind", "error", "bytes",
                  "seconds", "filepath")

    def __init__(self, path: Path, backup: Path):
        self.path, self.backup_path = path, backup
        self._lock = threading.Lock()     # one connection, shared by the workers and the saver
        try:
            self.conn = self._connect()
        except sqlite3.DatabaseError as exc:
            aside = path.with_name(f"library.damaged-{int(time.time())}.db")
            log.error("Library database is damaged (%s); kept it as %s and loading the backup", exc, aside.name)
            print(f"Library database was damaged; kept it as {aside.name} and loaded the backup.", file=sys.stderr)
            for suffix in ("", "-wal", "-shm"):
                src = Path(str(path) + suffix)
                if src.exists():
                    src.replace(Path(str(aside) + suffix))
            if backup.exists():
                shutil.copyfile(backup, path)
            self.conn = self._connect()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.executescript(self.SCHEMA)
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("integrity check failed")
            self.version = conn.execute("PRAGMA user_version").fetchone()[0]   # 0: event log not filled yet
        except sqlite3.DatabaseError:
            conn.close()
            raise
        return conn

    def load(self) -> list[dict]:
        with self._lock:
            jobs = {jid: json.loads(data) for jid, data in self.conn.execute("SELECT id, data FROM jobs")}
            for job in jobs.values():
                job["items"] = []
            for jid, data in self.conn.execute("SELECT job_id, data FROM items"):
                if jid in jobs:
                    jobs[jid]["items"].append(json.loads(data))
        for job in jobs.values():
            job["items"].sort(key=lambda it: it.get("index", 0))
        return list(jobs.values())

    def write(self, jobs: list[tuple[str, str]], items: list[tuple[str, str, str]], deleted: list[str]):
        if not (jobs or items or deleted):
            return
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                cur.executemany("DELETE FROM items WHERE job_id = ?", [(j,) for j in deleted])
                cur.executemany("DELETE FROM jobs WHERE id = ?", [(j,) for j in deleted])
                cur.executemany("INSERT OR REPLACE INTO jobs (id, data) VALUES (?, ?)", jobs)
                cur.executemany("INSERT OR REPLACE INTO items (job_id, id, data) VALUES (?, ?, ?)", items)
                cur.execute("COMMIT")
            except BaseException:
                cur.execute("ROLLBACK")
                raise

    def backup(self):
        tmp = self.backup_path.with_name(self.backup_path.name + ".tmp")
        dst = sqlite3.connect(tmp)
        try:
            with self._lock:
                self.conn.backup(dst)
        finally:
            dst.close()
        os.replace(tmp, self.backup_path)

    # ---- event log: one row per finished or failed download, kept even when the video is retried later

    def add_events(self, rows: list[dict]) -> int:
        """Append events; returns the id of the last one."""
        if not rows:
            return self.last_event_id()
        cols = ", ".join(self.EVENT_COLS)
        marks = ", ".join("?" * len(self.EVENT_COLS))
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                cur.executemany(f"INSERT INTO events ({cols}) VALUES ({marks})",
                                [tuple(r.get(c) for c in self.EVENT_COLS) for r in rows])
                cur.execute("COMMIT")
            except BaseException:
                cur.execute("ROLLBACK")
                raise
            return cur.lastrowid

    def set_version(self, version: int):
        with self._lock:
            self.conn.execute(f"PRAGMA user_version = {int(version)}")
            self.version = version

    def last_event_id(self) -> int:
        with self._lock:
            return self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]

    def events(self, offset: int = 0, limit: int = 30, since_id: int = 0, hidden: bool = False) -> list[dict]:
        """Newest first. hidden=False leaves out rows the user cleared from "Recently finished"."""
        where = "id > ?" + ("" if hidden else " AND hidden = 0")
        with self._lock:
            cur = self.conn.execute(f"SELECT id, {', '.join(self.EVENT_COLS)} FROM events WHERE {where} "
                                    "ORDER BY time DESC, id DESC LIMIT ? OFFSET ?", (since_id, limit, offset))
            names = [d[0] for d in cur.description]
            return [dict(zip(names, row)) for row in cur.fetchall()]

    def all_events(self) -> list[dict]:
        """Every event, oldest first, with its hidden flag (for "Export all data")."""
        cols = (*self.EVENT_COLS, "hidden")
        with self._lock:
            rows = self.conn.execute(f"SELECT {', '.join(cols)} FROM events ORDER BY id").fetchall()
        return [dict(zip(cols, row)) for row in rows]

    def merge_events(self, rows: list[dict]) -> int:
        """Add imported events that aren't in the log yet (same time, playlist, video and status); returns how many."""
        cols = (*self.EVENT_COLS, "hidden")
        with self._lock:
            have = set(self.conn.execute("SELECT time, job_id, item_id, status FROM events"))
            new, seen = [], set()
            for r in sorted(rows, key=lambda r: r["time"]):
                key = (r["time"], r.get("job_id"), r.get("item_id"), r.get("status"))
                if key not in have and key not in seen:
                    seen.add(key)
                    new.append(tuple(int(bool(r.get(c))) if c == "hidden" else r.get(c) for c in cols))
            if not new:
                return 0
            cur = self.conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                cur.executemany(f"INSERT INTO events ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", new)
                cur.execute("COMMIT")
            except BaseException:
                cur.execute("ROLLBACK")
                raise
            return len(new)

    def count_events(self, hidden: bool = False) -> int:
        with self._lock:
            return self.conn.execute("SELECT COUNT(*) FROM events" + ("" if hidden else " WHERE hidden = 0")).fetchone()[0]

    def counts_since(self, since: float) -> dict[str, int]:
        with self._lock:
            return dict(self.conn.execute("SELECT status, COUNT(*) FROM events WHERE time >= ? GROUP BY status",
                                          (since,)).fetchall())

    def hide_events(self) -> int:
        with self._lock:
            return self.conn.execute("UPDATE events SET hidden = 1 WHERE hidden = 0").rowcount

    def stats(self, since: float) -> dict:
        """Totals and per-day figures for the History & stats view (all events, cleared or not)."""
        with self._lock:
            days = self.conn.execute(
                "SELECT date(time, 'unixepoch', 'localtime') AS day, "
                "SUM(status = 'done'), SUM(status = 'failed'), COALESCE(SUM(CASE WHEN status = 'done' THEN bytes END), 0) "
                "FROM events WHERE time >= ? GROUP BY day ORDER BY day", (since,)).fetchall()
            done, failed, total_bytes = self.conn.execute(
                "SELECT COALESCE(SUM(status = 'done'), 0), COALESCE(SUM(status = 'failed'), 0), "
                "COALESCE(SUM(CASE WHEN status = 'done' THEN bytes END), 0) FROM events WHERE time >= ?",
                (since,)).fetchone()
            timed_bytes, seconds = self.conn.execute(
                "SELECT COALESCE(SUM(bytes), 0), COALESCE(SUM(seconds), 0) FROM events "
                "WHERE time >= ? AND status = 'done' AND seconds > 0 AND bytes > 0", (since,)).fetchone()
            kinds = self.conn.execute(
                "SELECT COALESCE(kind, 'other'), COUNT(*) FROM events WHERE time >= ? AND status = 'failed' "
                "GROUP BY 1 ORDER BY 2 DESC LIMIT 6", (since,)).fetchall()
        return {
            "days": [{"day": d, "done": n or 0, "failed": f or 0, "bytes": b or 0} for d, n, f, b in days],
            "done": done, "failed": failed, "bytes": total_bytes,
            "success": round(100 * done / (done + failed), 1) if done + failed else None,
            "avg_speed": timed_bytes / seconds if seconds else None,
            "kinds": [{"kind": k, "count": c} for k, c in kinds],
        }


def _read_legacy_state() -> list:
    """The old JSON library (state.json), falling back to its backup if damaged."""
    for path in (config.LEGACY_STATE, config.LEGACY_BACKUP):
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            continue
    return []
