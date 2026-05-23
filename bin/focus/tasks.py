import sqlite3
import time
from const import DB_PATH
import os


class TaskStore:
    def __init__(self):
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        self._conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                label       TEXT NOT NULL,
                description TEXT DEFAULT '',
                win_id      TEXT,
                win_name    TEXT,
                desktop     INTEGER DEFAULT 0,
                created_at  REAL
            )
        """)
        # migrate existing DBs that lack the description column
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(tasks)")}
        if "description" not in cols:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN description TEXT DEFAULT ''")
        self._conn.commit()

    def add(self, label: str, description: str = "", win_id: str = "", win_name: str = "", desktop: int = 0) -> int:
        cur = self._conn.execute(
            "INSERT INTO tasks (label, description, win_id, win_name, desktop, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (label, description, win_id, win_name, desktop, time.time()),
        )
        self._conn.commit()
        return cur.lastrowid

    def remove(self, task_id: int) -> None:
        self._conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        self._conn.commit()

    def all(self) -> list:
        rows = self._conn.execute(
            "SELECT id, label, description, win_id, win_name, desktop, created_at FROM tasks ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def clear(self) -> None:
        self._conn.execute("DELETE FROM tasks")
        self._conn.commit()
