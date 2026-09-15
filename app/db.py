"""SQLite storage.

Deliberately synchronous. Every query here touches a handful of rows in a
local file, so it completes in microseconds - far cheaper than the overhead
of an async driver, and it keeps claim/lock races easy to reason about.
WAL mode means another process on the box (Axolotl, one day) can read the
file concurrently without blocking the bot.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import timedelta
from typing import Any, Sequence

from app import config, timeutil as t

log = logging.getLogger(__name__)

_conn: sqlite3.Connection | None = None
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
  slug       TEXT PRIMARY KEY,          -- stable key; exists before we know the Telegram id
  user_id    INTEGER UNIQUE,            -- NULL until the person taps their name at /setup
  name       TEXT NOT NULL,
  username   TEXT,
  is_parent  INTEGER NOT NULL DEFAULT 0,
  is_kid     INTEGER NOT NULL DEFAULT 0,
  is_driver  INTEGER NOT NULL DEFAULT 0,
  is_admin   INTEGER NOT NULL DEFAULT 0,
  active     INTEGER NOT NULL DEFAULT 1,
  sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS dinner_polls (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  status      TEXT NOT NULL,            -- open | locked | expired | cancelled
  opened_by   INTEGER,
  opened_at   TEXT NOT NULL,
  deadline_at TEXT NOT NULL,
  start_date  TEXT NOT NULL,            -- first candidate day
  days        INTEGER NOT NULL,         -- how many candidate days
  message_id  INTEGER,
  locked_date TEXT
);

CREATE TABLE IF NOT EXISTS dinner_votes (
  poll_id   INTEGER NOT NULL,
  user_id   INTEGER NOT NULL,
  vote_date TEXT NOT NULL,              -- ISO date, or 'NONE' for "no days work"
  PRIMARY KEY (poll_id, user_id, vote_date)
);

CREATE TABLE IF NOT EXISTS dinner_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  poll_id     INTEGER,
  dinner_date TEXT NOT NULL,
  status      TEXT NOT NULL,            -- upcoming | done | cancelled
  message_id  INTEGER,
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dinner_dropouts (
  event_id INTEGER NOT NULL,
  user_id  INTEGER NOT NULL,
  PRIMARY KEY (event_id, user_id)
);

CREATE TABLE IF NOT EXISTS help_requests (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  kind         TEXT NOT NULL,           -- dog_accompany | dog_takeover | coverage
  task_label   TEXT,                    -- coverage only: dogs | house | both
  requested_by INTEGER NOT NULL,
  req_date     TEXT NOT NULL,
  time_window  TEXT NOT NULL,           -- morning | afternoon | evening
  status       TEXT NOT NULL,           -- open | claimed | done | expired | cancelled
  claimed_by   INTEGER,
  claimed_at   TEXT,
  message_id   INTEGER,
  created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS roster_rounds (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  status      TEXT NOT NULL,            -- collecting | closed | cancelled
  opened_at   TEXT NOT NULL,
  deadline_at TEXT NOT NULL,
  dates       TEXT NOT NULL,            -- JSON list of ISO Sundays
  message_id  INTEGER
);

CREATE TABLE IF NOT EXISTS roster_slots (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  duty_date  TEXT NOT NULL UNIQUE,
  assignee   INTEGER,                   -- NULL = unfilled
  status     TEXT NOT NULL,             -- collecting | assigned | open_swap | unfilled | done
  message_id INTEGER
);

CREATE TABLE IF NOT EXISTS roster_availability (
  duty_date TEXT NOT NULL,
  user_id   INTEGER NOT NULL,
  available INTEGER NOT NULL,
  round_id  INTEGER,
  PRIMARY KEY (duty_date, user_id)
);

CREATE TABLE IF NOT EXISTS roster_responses (
  round_id INTEGER NOT NULL,
  user_id  INTEGER NOT NULL,
  PRIMARY KEY (round_id, user_id)
);

CREATE TABLE IF NOT EXISTS duty_log (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id   INTEGER NOT NULL,
  duty_date TEXT NOT NULL,
  kind      TEXT NOT NULL,              -- roster | coverage | dog_takeover | dog_accompany
  ref_id    INTEGER,
  UNIQUE (user_id, duty_date, kind, ref_id)
);

CREATE TABLE IF NOT EXISTS jobs (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  due_at  TEXT NOT NULL,                -- UTC ISO
  kind    TEXT NOT NULL,
  ref_id  INTEGER,
  payload TEXT,
  done    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_jobs_due ON jobs (done, due_at);
CREATE INDEX IF NOT EXISTS idx_help_status ON help_requests (status, req_date);
CREATE INDEX IF NOT EXISTS idx_duty_date ON duty_log (duty_date);
CREATE INDEX IF NOT EXISTS idx_slots_date ON roster_slots (duty_date);
"""


# --- connection -----------------------------------------------------------

def connect(path: str | None = None) -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is not None:
            return _conn
        db_path = path or config.DB_PATH
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        _conn = sqlite3.connect(db_path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.executescript(SCHEMA)
        _conn.commit()
        return _conn


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def conn() -> sqlite3.Connection:
    return _conn if _conn is not None else connect()


# --- tiny query helpers ---------------------------------------------------

def q(sql: str, args: Sequence[Any] = ()) -> list[sqlite3.Row]:
    with _lock:
        return conn().execute(sql, args).fetchall()


def q1(sql: str, args: Sequence[Any] = ()) -> sqlite3.Row | None:
    with _lock:
        return conn().execute(sql, args).fetchone()


def scalar(sql: str, args: Sequence[Any] = (), default: Any = None) -> Any:
    row = q1(sql, args)
    return default if row is None else row[0]


def x(sql: str, args: Sequence[Any] = ()) -> sqlite3.Cursor:
    """Execute and commit. Returns the cursor so callers can read rowcount."""
    with _lock:
        c = conn()
        cur = c.execute(sql, args)
        c.commit()
        return cur


@contextmanager
def tx():
    with _lock:
        c = conn()
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise


# --- settings -------------------------------------------------------------

def get_setting(key: str, default: str | None = None) -> str:
    row = q1("SELECT value FROM settings WHERE key = ?", (key,))
    if row is not None and row["value"] is not None:
        return row["value"]
    if default is not None:
        return default
    return config.DEFAULT_SETTINGS.get(key, "")


def set_setting(key: str, value: Any) -> None:
    x("INSERT INTO settings (key, value) VALUES (?, ?) "
      "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))


def get_int(key: str) -> int:
    return int(get_setting(key))


def seed_settings() -> None:
    for k, v in config.DEFAULT_SETTINGS.items():
        x("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))


def group_chat_id() -> int | None:
    raw = get_setting("group_chat_id")
    return int(raw) if raw else None


def quiet() -> tuple[str, str]:
    return get_setting("quiet_start"), get_setting("quiet_end")


# --- members --------------------------------------------------------------

def seed_members(path: str | None = None) -> int:
    """Load the roster of people from members.json - once, into an empty table.

    Roles and names are configuration; Telegram ids are captured later by
    people tapping their own name during /setup.
    """
    if scalar("SELECT COUNT(*) FROM members", default=0):
        return 0
    src = path or config.MEMBERS_FILE
    candidates = [src, "members.json", "/srv/members.example.json", "members.example.json"]
    data = None
    for cand in candidates:
        if cand and os.path.exists(cand):
            with open(cand, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            log.info("Seeding members from %s", cand)
            break
    if data is None:
        log.warning("No members file found; members table left empty")
        return 0
    n = 0
    for i, m in enumerate(data.get("members", [])):
        x("INSERT OR IGNORE INTO members "
          "(slug, name, is_parent, is_kid, is_driver, is_admin, active, sort_order) "
          "VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
          (m["slug"], m["name"], int(m.get("parent", False)), int(m.get("kid", False)),
           int(m.get("driver", False)), int(m.get("admin", False)), i))
        n += 1
    return n


def members(active_only: bool = True) -> list[sqlite3.Row]:
    sql = "SELECT * FROM members"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY sort_order, slug"
    return q(sql)


def registered() -> list[sqlite3.Row]:
    return [m for m in members() if m["user_id"] is not None]


def unregistered() -> list[sqlite3.Row]:
    return [m for m in members() if m["user_id"] is None]


def kids(registered_only: bool = True) -> list[sqlite3.Row]:
    out = [m for m in members() if m["is_kid"]]
    return [m for m in out if m["user_id"] is not None] if registered_only else out


def drivers() -> list[sqlite3.Row]:
    return [m for m in members() if m["is_driver"]]


def member_by_user(user_id: int) -> sqlite3.Row | None:
    return q1("SELECT * FROM members WHERE user_id = ?", (user_id,))


def member_by_slug(slug: str) -> sqlite3.Row | None:
    return q1("SELECT * FROM members WHERE slug = ?", (slug,))


def name_of(user_id: int | None) -> str:
    if user_id is None:
        return "someone"
    m = member_by_user(user_id)
    return m["name"] if m else "someone"


def is_admin(user_id: int) -> bool:
    m = member_by_user(user_id)
    return bool(m and m["is_admin"])


def is_driver(user_id: int) -> bool:
    m = member_by_user(user_id)
    return bool(m and m["is_driver"])


def is_kid(user_id: int) -> bool:
    m = member_by_user(user_id)
    return bool(m and m["is_kid"])


def register_member(slug: str, user_id: int, username: str | None = None) -> bool:
    """Bind a Telegram id to a seeded member. One id maps to exactly one person."""
    with tx() as c:
        c.execute("UPDATE members SET user_id = NULL, username = NULL WHERE user_id = ?",
                  (user_id,))
        cur = c.execute("UPDATE members SET user_id = ?, username = ? WHERE slug = ?",
                        (user_id, username, slug))
        return cur.rowcount > 0


# --- duty log / fairness --------------------------------------------------

def log_duty(user_id: int, duty_date: str, kind: str, ref_id: int | None = None) -> None:
    x("INSERT OR IGNORE INTO duty_log (user_id, duty_date, kind, ref_id) "
      "VALUES (?, ?, ?, ?)", (user_id, duty_date, kind, ref_id))


def unlog_duty(kind: str, ref_id: int | None, user_id: int | None = None) -> None:
    if user_id is None:
        x("DELETE FROM duty_log WHERE kind = ? AND ref_id IS ?", (kind, ref_id))
    else:
        x("DELETE FROM duty_log WHERE kind = ? AND ref_id IS ? AND user_id = ?",
          (kind, ref_id, user_id))


def duty_counts(weeks: int | None = None) -> dict[int, int]:
    """Duties per person across the trailing fairness window.

    Counts every kind, and counts already-assigned future duties too, so a
    kid who just took three Sundays is not handed a fourth.
    """
    w = weeks if weeks is not None else get_int("fairness_weeks")
    since = (t.today_local() - timedelta(weeks=w)).isoformat()
    rows = q("SELECT user_id, COUNT(*) AS n FROM duty_log "
             "WHERE duty_date >= ? GROUP BY user_id", (since,))
    return {r["user_id"]: r["n"] for r in rows}


def last_duty_dates() -> dict[int, str]:
    rows = q("SELECT user_id, MAX(duty_date) AS last FROM duty_log GROUP BY user_id")
    return {r["user_id"]: r["last"] for r in rows}


# --- jobs -----------------------------------------------------------------

def add_job(due_at, kind: str, ref_id: int | None = None,
            payload: str | None = None, respect_quiet: bool = True) -> int:
    """Queue a durable job. Quiet hours are applied at queue time."""
    if respect_quiet:
        qs, qe = quiet()
        due_at = t.shift_out_of_quiet(due_at, qs, qe)
    cur = x("INSERT INTO jobs (due_at, kind, ref_id, payload) VALUES (?, ?, ?, ?)",
            (t.iso(due_at), kind, ref_id, payload))
    return int(cur.lastrowid)


def due_jobs(limit: int = 50) -> list[sqlite3.Row]:
    return q("SELECT * FROM jobs WHERE done = 0 AND due_at <= ? ORDER BY due_at LIMIT ?",
             (t.iso(t.now_utc()), limit))


def finish_job(job_id: int) -> None:
    x("UPDATE jobs SET done = 1 WHERE id = ?", (job_id,))


def cancel_jobs(kind: str | None = None, ref_id: int | None = None) -> int:
    sql = "UPDATE jobs SET done = 1 WHERE done = 0"
    args: list[Any] = []
    if kind is not None:
        sql += " AND kind = ?"
        args.append(kind)
    if ref_id is not None:
        sql += " AND ref_id = ?"
        args.append(ref_id)
    return x(sql, args).rowcount


def has_pending_job(kind: str, ref_id: int | None = None) -> bool:
    if ref_id is None:
        return bool(scalar("SELECT COUNT(*) FROM jobs WHERE done = 0 AND kind = ?",
                           (kind,), default=0))
    return bool(scalar("SELECT COUNT(*) FROM jobs WHERE done = 0 AND kind = ? AND ref_id = ?",
                       (kind, ref_id), default=0))


def init(db_path: str | None = None, members_path: str | None = None) -> None:
    connect(db_path)
    seed_settings()
    seed_members(members_path)
