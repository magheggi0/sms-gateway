import os
import sqlite3

DB_PATH = os.environ.get("DB_PATH", "/data/sms-gateway.db")
# Storico a giro, come i dischi della videosorveglianza: oltre il limite si cancellano i piu' vecchi
HISTORY_MAX = int(os.environ.get("HISTORY_MAX", "20000"))
# Nessuno storico dei testi: degli invii restano solo esito, data e numero mascherato (servono al limite orario delle
# chiavi e alla diagnosi). Gli SMS ricevuti restano finche' l'applicazione che li legge non li conferma, al massimo
# RECEIVED_MAX_DAYS giorni. Per consegnare ogni risposta a chi ha scritto si ricorda quale chiave ha mandato l'ultimo SMS
# a ogni numero (senza testo), per LAST_SENDER_DAYS giorni.
RECEIVED_MAX_DAYS = int(os.environ.get("RECEIVED_MAX_DAYS", "14"))
LAST_SENDER_DAYS = int(os.environ.get("LAST_SENDER_DAYS", "60"))


def number_key(number):
    """Stesso numero scritto in modi diversi (+39, 0039, spazi): solo cifre, senza il prefisso italiano."""
    d = "".join(ch for ch in str(number or "") if ch.isdigit())
    if d.startswith("00"):
        d = d[2:]
    if len(d) == 12 and d.startswith("39"):
        d = d[2:]
    return d


def mask_number(number):
    d = str(number or "")
    return ("•" * max(0, len(d) - 3)) + d[-3:] if len(d) > 3 else d


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
    # Ultima chiave che ha scritto a un numero: a lei vanno le risposte di quel numero
    conn.execute("""
        CREATE TABLE IF NOT EXISTS last_sender (
            number_key TEXT PRIMARY KEY,
            api_key_id INTEGER NOT NULL,
            at TEXT DEFAULT (datetime('now', 'localtime'))
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
    """Registra l'invio senza il testo (il parametro text resta per compatibilita' e non si salva) e con il numero
    mascherato; se l'invio e' riuscito, ricorda la chiave che ha scritto a quel numero."""
    conn = db()
    conn.execute(
        "INSERT INTO sent (number, text, success, attempts, detail, api_key_id, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (mask_number(number), "", 1 if success else 0, attempts, detail, api_key_id, source),
    )
    _rotate(conn, "sent")
    if success and api_key_id is not None and number_key(number):
        conn.execute(
            "INSERT INTO last_sender (number_key, api_key_id, at) VALUES (?, ?, datetime('now', 'localtime')) "
            "ON CONFLICT(number_key) DO UPDATE SET api_key_id = excluded.api_key_id, at = excluded.at",
            (number_key(number), api_key_id),
        )
    conn.commit()
    conn.close()


def prune():
    """Cancella i ricevuti mai confermati oltre RECEIVED_MAX_DAYS e i destinatari ricordati oltre LAST_SENDER_DAYS."""
    conn = db()
    conn.execute("DELETE FROM received WHERE imported_at < datetime('now', 'localtime', ?)", (f"-{RECEIVED_MAX_DAYS} days",))
    conn.execute("DELETE FROM last_sender WHERE at < datetime('now', 'localtime', ?)", (f"-{LAST_SENDER_DAYS} days",))
    conn.commit()
    conn.close()


def _owner(conn, number):
    row = conn.execute(
        "SELECT api_key_id FROM last_sender WHERE number_key = ? AND at >= datetime('now', 'localtime', ?)",
        (number_key(number), f"-{LAST_SENDER_DAYS} days"),
    ).fetchone()
    return row["api_key_id"] if row else None


def received_for(api_key_id=None, default_inbox=False, limit=200):
    """Risposte da consegnare: quelle dei numeri a cui questa chiave ha scritto per ultima e, per la chiave
    predefinita (default_inbox), quelle dei numeri a cui non ha scritto nessuno. api_key_id None = dashboard: tutte."""
    prune()
    conn = db()
    rows = conn.execute(
        "SELECT id, number, text, sms_timestamp, imported_at FROM received ORDER BY id LIMIT 2000").fetchall()
    out = []
    for r in rows:
        if api_key_id is not None:
            owner = _owner(conn, r["number"])
            if not (owner == api_key_id or (owner is None and default_inbox)):
                continue
        out.append(dict(r))
        if len(out) >= limit:
            break
    conn.close()
    return out


def ack_received(ids, api_key_id=None, default_inbox=False):
    """Conferma (e cancella) le risposte lette: solo quelle che spettano a questa chiave."""
    wanted = {int(i) for i in ids if str(i).isdigit()}
    if not wanted:
        return 0
    mine = {r["id"] for r in received_for(api_key_id, default_inbox, limit=2000)}
    ids_ok = sorted(wanted & mine)
    if not ids_ok:
        return 0
    conn = db()
    conn.execute(f"DELETE FROM received WHERE id IN ({','.join('?' * len(ids_ok))})", ids_ok)
    conn.commit()
    conn.close()
    return len(ids_ok)


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
