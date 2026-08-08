from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .errors import NotFoundError


class EpisodeStore:
    def __init__(self, state_dir: Path):
        state_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir = state_dir
        self.db = sqlite3.connect(state_dir / "episodes.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("""
          CREATE TABLE IF NOT EXISTS episodes (
            id TEXT PRIMARY KEY, scenario_id TEXT NOT NULL, scenario_version TEXT NOT NULL,
            status TEXT NOT NULL, seed INTEGER NOT NULL, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, data TEXT NOT NULL
          )
        """)
        self.db.commit()

    def put(self, episode: dict[str, Any]) -> None:
        self.db.execute("""
          INSERT INTO episodes VALUES (?, ?, ?, ?, ?, ?, ?, ?)
          ON CONFLICT(id) DO UPDATE SET status=excluded.status,
            updated_at=excluded.updated_at, data=excluded.data
        """, (episode["episode_id"], episode["scenario_id"], episode["scenario_version"],
              episode["status"], episode["seed"], episode["created_at"],
              episode["updated_at"], json.dumps(episode, sort_keys=True)))
        self.db.commit()

    def get(self, episode_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT data FROM episodes WHERE id = ?", (episode_id,)).fetchone()
        if not row:
            raise NotFoundError(f"episode not found: {episode_id}")
        return json.loads(row["data"])
