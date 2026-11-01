"""[AI-GENERATED] Idempotent SQLite initialization for existing and new installs."""

import sqlite3
from pathlib import Path


def initialize_database(db: sqlite3.Connection, schema_path: Path) -> None:
    with schema_path.open() as schema:
        db.executescript(schema.read())
    columns = {row[1] for row in db.execute("PRAGMA table_info(activities)")}
    additions = {
        "work_interval_distance": "REAL",
        "work_metrics_status": (
            "TEXT NOT NULL DEFAULT 'pending' "
            "CHECK(work_metrics_status IN ('pending', 'complete', 'failed'))"
        ),
        "work_metrics_error": "TEXT",
    }
    for column, definition in additions.items():
        if column not in columns:
            db.execute(f"ALTER TABLE activities ADD COLUMN {column} {definition}")
    db.execute(
        """UPDATE activities SET work_interval_distance = 0,
           work_metrics_status = 'complete'
           WHERE classification NOT IN ('interval', 'unknown')
           AND work_metrics_status = 'pending'"""
    )
    db.commit()
