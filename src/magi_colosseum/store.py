from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .errors import NotFoundError


class EpisodeStore:
    def __init__(self, state_dir: Path):
        state_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir = state_dir
        # Textual runs lifecycle operations in worker threads so Docker work
        # cannot freeze the UI. Keep one connection, but serialize every use.
        self._lock = threading.RLock()
        self.db = sqlite3.connect(state_dir / "episodes.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self._lock:
            self.db.execute("""
              CREATE TABLE IF NOT EXISTS episodes (
                id TEXT PRIMARY KEY, scenario_id TEXT NOT NULL, scenario_version TEXT NOT NULL,
                status TEXT NOT NULL, seed INTEGER NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, data TEXT NOT NULL
              )
            """)
            self.db.commit()

    def put(self, episode: dict[str, Any]) -> None:
        with self._lock:
            self.db.execute("""
              INSERT INTO episodes VALUES (?, ?, ?, ?, ?, ?, ?, ?)
              ON CONFLICT(id) DO UPDATE SET status=excluded.status,
                updated_at=excluded.updated_at, data=excluded.data
            """, (episode["episode_id"], episode["scenario_id"], episode["scenario_version"],
                  episode["status"], episode["seed"], episode["created_at"],
                  episode["updated_at"], json.dumps(episode, sort_keys=True)))
            self.db.commit()

    def get(self, episode_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.db.execute(
                "SELECT data FROM episodes WHERE id = ?", (episode_id,)
            ).fetchone()
        if not row:
            raise NotFoundError(f"episode not found: {episode_id}")
        return json.loads(row["data"])

    def list(self, include_stopped: bool = False) -> list[dict[str, Any]]:
        query = "SELECT data FROM episodes"
        params: tuple[str, ...] = ()
        if not include_stopped:
            query += " WHERE status IN (?, ?)"
            params = ("ready", "starting")
        query += " ORDER BY updated_at DESC"
        with self._lock:
            rows = self.db.execute(query, params).fetchall()
        return [json.loads(row["data"]) for row in rows]
