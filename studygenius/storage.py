from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "studygenius.sqlite3"
        with self.connect() as con:
            con.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                  id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
                  stage TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0,
                  options TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                  error TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                  time REAL NOT NULL, message TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS calls (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                  provider TEXT NOT NULL, model TEXT NOT NULL, task TEXT NOT NULL,
                  time REAL NOT NULL, status TEXT NOT NULL DEFAULT 'started',
                  input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
                  total_tokens INTEGER DEFAULT 0);
            """)

    def connect(self):
        con = sqlite3.connect(self.db, timeout=30)
        con.row_factory = sqlite3.Row
        return con

    def directory(self, job_id: str) -> Path:
        if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
            raise ValueError("ID lavoro non valido")
        return self.root / "jobs" / job_id

    def create(self, options: dict) -> str:
        job_id = uuid.uuid4().hex
        (self.directory(job_id) / "inputs").mkdir(parents=True)
        with self.connect() as con:
            con.execute("INSERT INTO jobs (id,title,status,stage,options,created,updated) VALUES (?,?,?,?,?,?,?)",
                        (job_id, options["title"], "ready", "Pronto", json.dumps(options), time.time(), time.time()))
        return job_id

    def get(self, job_id: str) -> dict:
        self.directory(job_id)
        with self.connect() as con:
            row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError("Lavoro non trovato")
        out = dict(row)
        out["options"] = json.loads(out["options"])
        out["usage"] = self.usage(job_id)
        return out

    def list(self) -> list[dict]:
        with self.connect() as con:
            ids = [r[0] for r in con.execute("SELECT id FROM jobs ORDER BY created DESC LIMIT 100")]
        return [self.get(i) for i in ids]

    def update(self, job_id: str, **values):
        allowed = {"status", "stage", "progress", "error", "options"}
        if not values or not values.keys() <= allowed:
            raise ValueError("Campi non validi")
        if "options" in values:
            values["options"] = json.dumps(values["options"])
        values["updated"] = time.time()
        with self.connect() as con:
            con.execute(f"UPDATE jobs SET {','.join(k+'=?' for k in values)} WHERE id=?",
                        [*values.values(), job_id])

    def event(self, job_id: str, message: str):
        with self.connect() as con:
            con.execute("INSERT INTO events (job_id,time,message) VALUES (?,?,?)", (job_id, time.time(), message))

    def events(self, job_id: str) -> list[dict]:
        with self.connect() as con:
            rows = con.execute("SELECT * FROM events WHERE job_id=? ORDER BY id DESC LIMIT 100", (job_id,)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def reserve_call(self, job_id: str, provider: str, model: str, task: str, max_calls: int, max_tokens: int) -> int:
        # Transaction includes the check: retries and interrupted requests still count.
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            count, total = con.execute("SELECT COUNT(*),COALESCE(SUM(total_tokens),0) FROM calls WHERE job_id=?", (job_id,)).fetchone()
            if count >= max_calls or total >= max_tokens:
                raise BudgetExceeded("Limite API raggiunto. Aumenta il limite del lavoro e premi Riprendi.")
            cur = con.execute("INSERT INTO calls (job_id,provider,model,task,time) VALUES (?,?,?,?,?)",
                              (job_id, provider, model, task, time.time()))
            return cur.lastrowid

    def finish_call(self, call_id: int, status: str, incoming=0, outgoing=0, total=0):
        with self.connect() as con:
            con.execute("UPDATE calls SET status=?,input_tokens=?,output_tokens=?,total_tokens=? WHERE id=?",
                        (status, incoming, outgoing, total, call_id))

    def usage(self, job_id: str) -> dict:
        with self.connect() as con:
            row = con.execute("""SELECT COUNT(*) calls, COALESCE(SUM(input_tokens),0) input_tokens,
                COALESCE(SUM(output_tokens),0) output_tokens, COALESCE(SUM(total_tokens),0) total_tokens,
                COALESCE(SUM(CASE WHEN status IN ('started','transport_error') THEN 1 ELSE 0 END),0) uncertain_calls
                FROM calls WHERE job_id=?""", (job_id,)).fetchone()
        return dict(row)

    def recover(self):
        with self.connect() as con:
            con.execute("UPDATE jobs SET status='paused',stage='Interrotto: puoi riprendere' WHERE status IN ('running','queued')")


class BudgetExceeded(RuntimeError):
    pass


class InstanceLock:
    """OS lock released automatically on process exit; works on Windows and Unix."""
    def __init__(self, root: Path):
        self.path = root / "instance.lock"
        self.stream = None

    def __enter__(self):
        self.stream = self.path.open("a+b")
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                if self.path.stat().st_size == 0:
                    self.stream.write(b"0")
                    self.stream.flush()
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            self.stream = None
            raise RuntimeError("StudyGenius è già aperto su questa cartella dati. Usa la finestra esistente o chiudila prima di riavviare.") from None
        return self

    def __exit__(self, *_):
        if self.stream:
            if os.name == "nt":
                import msvcrt
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            self.stream.close()
            self.stream = None
