"""Tiny SQLite layer. Rows come back as dicts."""
from __future__ import annotations
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from .config import DB_PATH

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  title TEXT DEFAULT '',
  source_url TEXT DEFAULT '',
  source_type TEXT DEFAULT 'url',
  source_path TEXT DEFAULT '',
  channel TEXT DEFAULT '',
  duration REAL DEFAULT 0,
  thumbnail TEXT DEFAULT '',
  status TEXT DEFAULT 'queued',
  stage TEXT DEFAULT 'Waiting to start',
  progress REAL DEFAULT 0,
  error TEXT DEFAULT '',
  options TEXT DEFAULT '{}',
  info TEXT DEFAULT '{}',
  created_at REAL,
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS clips (
  id TEXT PRIMARY KEY,
  project_id TEXT,
  idx INTEGER DEFAULT 0,
  start REAL, "end" REAL,
  score REAL DEFAULT 0,
  title TEXT DEFAULT '',
  status TEXT DEFAULT 'pending',
  stage TEXT DEFAULT '',
  progress REAL DEFAULT 0,
  error TEXT DEFAULT '',
  path TEXT DEFAULT '',
  thumbnail TEXT DEFAULT '',
  data TEXT DEFAULT '{}',
  settings TEXT DEFAULT '{}',
  created_at REAL,
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  kind TEXT,
  target_id TEXT,
  status TEXT DEFAULT 'queued',
  stage TEXT DEFAULT '',
  progress REAL DEFAULT 0,
  error TEXT DEFAULT '',
  created_at REAL, started_at REAL, finished_at REAL,
  owner_pid INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS templates (
  id TEXT PRIMARY KEY,
  name TEXT,
  is_default INTEGER DEFAULT 0,
  data TEXT DEFAULT '{}',
  created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS posts (
  id TEXT PRIMARY KEY,
  clip_id TEXT,
  project_id TEXT,
  status TEXT DEFAULT 'waiting',
  publish_at REAL,
  youtube_id TEXT DEFAULT '',
  title TEXT DEFAULT '',
  error TEXT DEFAULT '',
  telegram_msg TEXT DEFAULT '',
  created_at REAL, updated_at REAL
);
CREATE TABLE IF NOT EXISTS seen_videos (video_id TEXT PRIMARY KEY, title TEXT, seen_at REAL, project_id TEXT);
CREATE TABLE IF NOT EXISTS errors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at REAL, where_ TEXT, message TEXT
);
"""


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


_con = None


def con() -> sqlite3.Connection:
    global _con
    if _con is None:
        _con = connect()
    return _con


def init_db():
    with _lock:
        c = con()
        c.executescript(SCHEMA)
        cols = {r[1] for r in c.execute("PRAGMA table_info(jobs)").fetchall()}
        if "owner_pid" not in cols:
            c.execute("ALTER TABLE jobs ADD COLUMN owner_pid INTEGER DEFAULT 0")
        c.commit()


@contextmanager
def tx():
    with _lock:
        c = con()
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise


def new_id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex[:12]


def rows(sql: str, params=()) -> list[dict]:
    with _lock:
        return [dict(r) for r in con().execute(sql, params).fetchall()]


def row(sql: str, params=()) -> dict | None:
    with _lock:
        r = con().execute(sql, params).fetchone()
        return dict(r) if r else None


def execute(sql: str, params=()):
    with tx() as c:
        c.execute(sql, params)


def insert(table: str, values: dict):
    values = dict(values)
    now = time.time()
    values.setdefault("created_at", now)
    if table in ("projects", "clips", "templates", "posts"):
        values.setdefault("updated_at", now)
    cols = ", ".join(f'"{k}"' for k in values)
    qs = ", ".join("?" for _ in values)
    execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})", tuple(_enc(v) for v in values.values()))


def update(table: str, id_: str, values: dict):
    values = dict(values)
    if table in ("projects", "clips", "templates", "posts"):
        values["updated_at"] = time.time()
    sets = ", ".join(f'"{k}" = ?' for k in values)
    execute(f"UPDATE {table} SET {sets} WHERE id = ?", tuple(_enc(v) for v in values.values()) + (id_,))


def _enc(v):
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return v


def loads(row_: dict | None, *fields: str) -> dict | None:
    """Decode JSON text fields in place."""
    if row_ is None:
        return None
    for f in fields:
        v = row_.get(f)
        if isinstance(v, str):
            try:
                row_[f] = json.loads(v) if v else {}
            except json.JSONDecodeError:
                row_[f] = {}
    return row_


def log_error(where: str, message: str):
    try:
        execute("INSERT INTO errors (at, where_, message) VALUES (?, ?, ?)", (time.time(), where, message[:4000]))
        execute("DELETE FROM errors WHERE id NOT IN (SELECT id FROM errors ORDER BY id DESC LIMIT 200)")
    except Exception:
        pass


def get_setting(key: str, default=None):
    r = row("SELECT value FROM settings WHERE key=?", (key,))
    if not r:
        return default
    try:
        return json.loads(r["value"])
    except (TypeError, json.JSONDecodeError):
        return r["value"]


def set_setting(key: str, value):
    execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(value)))
