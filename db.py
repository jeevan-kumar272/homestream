"""The HomeStream database: one small SQLite file next to main.py."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "homestream.db"


@contextmanager
def open_db():
    """Open the database, save changes if everything worked, undo them if it didn't."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row          # lets us read columns by name, like row["username"]
    conn.execute("PRAGMA foreign_keys = ON")  # needed so deleting a user also deletes their sessions
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create the tables the first time the server starts. Safe to run again."""
    with open_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                is_admin      INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at INTEGER NOT NULL
            );

            -- A copy of what is in media/movies, so we can look movies up fast and
            -- connect them to other tables. The files on disk stay the real source.
            CREATE TABLE IF NOT EXISTS movies (
                id            TEXT PRIMARY KEY,
                relative_path TEXT NOT NULL UNIQUE,
                title         TEXT NOT NULL,
                year          INTEGER,
                filename      TEXT NOT NULL,
                format        TEXT NOT NULL,
                size_mb       REAL NOT NULL
            );

            -- One row per person per movie: where they stopped watching.
            -- The pair (user_id, movie_id) is the primary key, so each person
            -- can have only one row per movie.
            CREATE TABLE IF NOT EXISTS progress (
                user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                movie_id         TEXT NOT NULL REFERENCES movies(id) ON DELETE CASCADE,
                position_seconds REAL NOT NULL,
                duration_seconds REAL NOT NULL,
                finished         INTEGER NOT NULL DEFAULT 0,
                updated_at       INTEGER NOT NULL,
                PRIMARY KEY (user_id, movie_id)
            );

            -- Makes "this person's most recently watched movies" fast to find
            CREATE INDEX IF NOT EXISTS idx_progress_recent ON progress (user_id, updated_at DESC);
            """
        )