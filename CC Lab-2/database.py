
import sqlite3
from threading import Lock

_db_init_lock = Lock()
_db_initialized = False


def _init_db_settings(conn: sqlite3.Connection) -> None:
    # These are database-level settings; doing them once avoids extra per-request cost.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")


def get_db():
    global _db_initialized

    # timeout helps under concurrent access; WAL improves read concurrency.
    conn = sqlite3.connect("fest.db", timeout=30)
    conn.row_factory = sqlite3.Row

    # Initialize database settings once per process.
    if not _db_initialized:
        with _db_init_lock:
            if not _db_initialized:
                _init_db_settings(conn)
                _db_initialized = True

    # Connection-level setting (keep per connection).
    conn.execute("PRAGMA busy_timeout=3000")
    return conn
