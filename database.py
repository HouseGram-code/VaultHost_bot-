import sqlite3
import os
import logging
import threading

logger = logging.getLogger(__name__)

DB_PATH = os.path.join('data', 'hosting.db')


class Database:
    def __init__(self):
        os.makedirs('data', exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=20)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()
        logger.info("Database initialized at %s", DB_PATH)

    def _create_tables(self):
        with self._lock:
            self.conn.executescript('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id    INTEGER PRIMARY KEY,
                    name       TEXT    NOT NULL,
                    password   TEXT    NOT NULL,
                    credits    INTEGER NOT NULL DEFAULT 25,
                    is_banned  INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS servers (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id      INTEGER NOT NULL,
                    name         TEXT    NOT NULL,
                    container_id TEXT,
                    status       TEXT    NOT NULL DEFAULT 'stopped',
                    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL DEFAULT ''
                );
            ''')
            self.conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES ('maintenance', '0')"
            )
            self.conn.commit()

    # ─── Users ────────────────────────────────────────────────────────────────

    def register_user(self, user_id: int, name: str, password: str) -> bool:
        try:
            with self._lock:
                self.conn.execute(
                    "INSERT INTO users (user_id, name, password) VALUES (?, ?, ?)",
                    (user_id, name, password),
                )
                self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def get_user(self, user_id: int):
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        return dict(row) if row else None

    def user_exists(self, user_id: int) -> bool:
        return self.get_user(user_id) is not None

    def check_password(self, user_id: int, password: str) -> bool:
        user = self.get_user(user_id)
        return bool(user and user['password'] == password)

    def update_credits(self, user_id: int, delta: int):
        with self._lock:
            self.conn.execute(
                "UPDATE users SET credits = MAX(0, credits + ?) WHERE user_id = ?",
                (delta, user_id),
            )
            self.conn.commit()

    def set_credits(self, user_id: int, amount: int):
        with self._lock:
            self.conn.execute(
                "UPDATE users SET credits = ? WHERE user_id = ?",
                (max(0, amount), user_id),
            )
            self.conn.commit()

    def ban_user(self, user_id: int):
        with self._lock:
            self.conn.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (user_id,))
            self.conn.commit()

    def unban_user(self, user_id: int):
        with self._lock:
            self.conn.execute("UPDATE users SET is_banned = 0 WHERE user_id = ?", (user_id,))
            self.conn.commit()

    def get_all_user_ids(self) -> list:
        with self._lock:
            rows = self.conn.execute("SELECT user_id FROM users").fetchall()
        return [r['user_id'] for r in rows]

    # ─── Servers ──────────────────────────────────────────────────────────────

    def create_server(self, user_id: int, name: str) -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO servers (user_id, name) VALUES (?, ?)",
                (user_id, name),
            )
            self.conn.commit()
        return cur.lastrowid

    def get_server(self, server_id: int):
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM servers WHERE id = ?", (server_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_servers(self, user_id: int) -> list:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM servers WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_server(self, server_id: int, status: str = None, container_id: str = None):
        with self._lock:
            if status and container_id:
                self.conn.execute(
                    "UPDATE servers SET status = ?, container_id = ? WHERE id = ?",
                    (status, container_id, server_id),
                )
            elif status:
                self.conn.execute(
                    "UPDATE servers SET status = ? WHERE id = ?",
                    (status, server_id),
                )
            elif container_id:
                self.conn.execute(
                    "UPDATE servers SET container_id = ? WHERE id = ?",
                    (container_id, server_id),
                )
            self.conn.commit()

    def get_running_servers_count(self, user_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS cnt FROM servers WHERE user_id = ? AND status = 'running'",
                (user_id,),
            ).fetchone()
        return row['cnt'] if row else 0

    def get_all_running(self) -> list:
        """Returns list of (user_id, server_id, container_id) for running servers."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT user_id, id, container_id FROM servers WHERE status = 'running'"
            ).fetchall()
        return [dict(r) for r in rows]

    # ─── Settings ─────────────────────────────────────────────────────────────

    def get_setting(self, key: str, default: str = '') -> str:
        with self._lock:
            row = self.conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return row['value'] if row else default

    def set_setting(self, key: str, value: str):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
            self.conn.commit()


db = Database()
