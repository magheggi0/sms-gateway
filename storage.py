import os
import sqlite3

DB_PATH = os.environ.get("DB_PATH", "/data/sms-gateway.db")
# Storico a giro, come i dischi della videosorveglianza: oltre il limite si cancellano i piu' vecchi
HISTORY_MAX = int(os.environ.get("HISTORY_MAX", "20000"))


def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _rotate(conn, table):
    if HISTORY_MAX > 0:
        conn.execute(f"DELETE FROM {table} WHERE id <= (SELECT MAX(id) FROM {table}) - ?", (HISTORY_MAX,))


def _columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def init_db():
    conn = db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sent (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            number TEXT NOT NULL,
            text TEXT NOT NULL,
            success INTEGER NOT NULL,
            attempts INTEGER,
            detail TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS received (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            modem_sms_path TEXT UNIQUE,
            number TEXT,
            text TEXT,
            sms_timestamp TEXT,
            imported_at TEXT DEFAULT (datetime('now', 'localtime'))
        )
    """)
    # Chiavi API: si salva solo l'hash SHA-256, la chiave in chiaro si vede
    # una sola volta alla creazione.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            key_hash TEXT NOT NULL UNIQUE,
            key_prefix TEXT NOT NULL,
            scopes TEXT NOT NULL DEFAULT 'send',
            allowed_ips TEXT NOT NULL DEFAULT '',
            max_per_hour INTEGER NOT NULL DEFAULT 30,
            created_at TEXT DEFAULT (datetime('now', 'localtime')),
            last_used_at TEXT,
            last_used_ip TEXT,
            revoked_at TEXT
        )
    """)
    # Database creati con la versione precedente: aggiunge chi ha inviato
    if "api_key_id" not in _columns(conn, "sent"):
        conn.execute("ALTER TABLE sent ADD COLUMN api_key_id INTEGER")
    if "source" not in _columns(conn, "sent"):
        conn.execute("ALTER TABLE sent ADD COLUMN source TEXT")
    conn.commit()
    conn.close()


def log_sent(number, text, success, attempts, detail, api_key_id=None, source=None):
    conn = db()
    conn.execute(
        "INSERT INTO sent (number, text, success, attempts, detail, api_key_id, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (number, text, 1 if success else 0, attempts, detail, api_key_id, source),
    )
    _rotate(conn, "sent")
    conn.commit()
    conn.close()


def count_sent_last_hour(api_key_id):
    conn = db()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM sent WHERE api_key_id = ? "
        "AND created_at >= datetime('now', 'localtime', '-1 hour')",
        (api_key_id,),
    ).fetchone()
    conn.close()
    return row["n"]


def save_received(path, number, text, sms_timestamp):
    """Salva un SMS ricevuto. Ritorna True se e' nel database (anche se c'era gia'),
    cosi' si puo' cancellare dal modem. ModemManager riusa i percorsi /SMS/N dopo
    un riavvio: la chiave unica comprende anche data e mittente."""
    key = f"{path}|{sms_timestamp}|{number}"
    conn = db()
    try:
        conn.execute(
            "INSERT INTO received (modem_sms_path, number, text, sms_timestamp) VALUES (?, ?, ?, ?)",
            (key, number, text, sms_timestamp),
        )
        _rotate(conn, "received")
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()
