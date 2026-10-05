"""Credenziali del gateway.

- Chiavi API (header ``Authorization: Bearer <chiave>`` o ``X-API-Key``) per i
  programmi che inviano SMS. Ogni chiave ha un nome, dei permessi (``send``,
  ``read``), un limite di SMS all'ora e, facoltativamente, gli IP/reti da cui
  può essere usata.
- Login con utente e password per la dashboard web (sessione con cookie).
"""

import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import threading
import time
from functools import wraps

from flask import g, jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash

from storage import db

KEY_PREFIX = "smsgw_"
VALID_SCOPES = ("send", "read")

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH", "")

# Blocco dei tentativi di login falliti, per IP
LOGIN_MAX_FAILURES = 5
LOGIN_LOCK_SECONDS = 15 * 60
_login_failures = {}
_login_lock = threading.Lock()


# --- chiavi API -------------------------------------------------------------

def hash_key(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def parse_scopes(value):
    scopes = sorted({s.strip() for s in (value or "send").split(",") if s.strip()})
    invalid = [s for s in scopes if s not in VALID_SCOPES]
    if invalid or not scopes:
        raise ValueError(f"permessi non validi: {', '.join(invalid) or '(vuoto)'} (ammessi: send, read)")
    return ",".join(scopes)


def parse_allowed_ips(value):
    networks = []
    for item in (value or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(str(ipaddress.ip_network(item, strict=False)))
        except ValueError:
            raise ValueError(f"IP o rete non valida: {item}")
    return ",".join(networks)


def create_key(name, scopes="send", allowed_ips="", max_per_hour=30):
    name = (name or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name):
        raise ValueError("nome non valido: usa lettere, numeri, punto, trattino o underscore (max 64)")
    max_per_hour = int(max_per_hour)
    if max_per_hour < 1:
        raise ValueError("max_per_hour deve essere almeno 1")

    key = KEY_PREFIX + secrets.token_urlsafe(32)
    conn = db()
    try:
        exists = conn.execute("SELECT 1 FROM api_keys WHERE name = ?", (name,)).fetchone()
        if exists:
            raise ValueError(f"esiste già una chiave con nome '{name}'")
        conn.execute(
            "INSERT INTO api_keys (name, key_hash, key_prefix, scopes, allowed_ips, max_per_hour) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, hash_key(key), key[: len(KEY_PREFIX) + 6], parse_scopes(scopes),
             parse_allowed_ips(allowed_ips), max_per_hour),
        )
        conn.commit()
    finally:
        conn.close()
    return key


def list_keys():
    conn = db()
    rows = conn.execute(
        "SELECT id, name, key_prefix, scopes, allowed_ips, max_per_hour, created_at, "
        "last_used_at, last_used_ip, revoked_at FROM api_keys ORDER BY id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def revoke_key(key_id=None, name=None):
    conn = db()
    if key_id is not None:
        cur = conn.execute(
            "UPDATE api_keys SET revoked_at = datetime('now', 'localtime') "
            "WHERE id = ? AND revoked_at IS NULL", (key_id,))
    else:
        cur = conn.execute(
            "UPDATE api_keys SET revoked_at = datetime('now', 'localtime') "
            "WHERE name = ? AND revoked_at IS NULL", (name,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def update_key(key_id=None, name=None, allowed_ips=None, max_per_hour=None):
    """Cambia IP ammessi e/o limite orario di una chiave attiva."""
    fields, values = [], []
    if allowed_ips is not None:
        fields.append("allowed_ips = ?")
        values.append(parse_allowed_ips(allowed_ips))
    if max_per_hour is not None:
        max_per_hour = int(max_per_hour)
        if max_per_hour < 1:
            raise ValueError("max_per_hour deve essere almeno 1")
        fields.append("max_per_hour = ?")
        values.append(max_per_hour)
    if not fields:
        raise ValueError("niente da modificare")
    where, ident = ("id = ?", key_id) if key_id is not None else ("name = ?", name)
    conn = db()
    cur = conn.execute(
        f"UPDATE api_keys SET {', '.join(fields)} WHERE {where} AND revoked_at IS NULL", (*values, ident))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def _extract_key():
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.headers.get("X-API-Key", "").strip()


def _ip_allowed(allowed_ips, remote_addr):
    if not allowed_ips:
        return True
    try:
        addr = ipaddress.ip_address(remote_addr)
    except ValueError:
        return False
    return any(addr in ipaddress.ip_network(net) for net in allowed_ips.split(","))


def _find_key(key):
    if not key.startswith(KEY_PREFIX):
        return None
    conn = db()
    row = conn.execute(
        "SELECT * FROM api_keys WHERE key_hash = ? AND revoked_at IS NULL", (hash_key(key),)
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE api_keys SET last_used_at = datetime('now', 'localtime'), last_used_ip = ? "
            "WHERE id = ?", (request.remote_addr, row["id"]))
        conn.commit()
    conn.close()
    return dict(row) if row else None


def require_auth(scope):
    """Accetta una chiave API con il permesso ``scope`` oppure la sessione
    dell'amministratore della dashboard."""

    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            g.api_key = None

            key = _extract_key()
            if key:
                row = _find_key(key)
                if row is None:
                    return jsonify({"success": False, "error": "chiave API non valida"}), 401
                if scope not in row["scopes"].split(","):
                    return jsonify({"success": False, "error": f"la chiave non ha il permesso '{scope}'"}), 403
                if not _ip_allowed(row["allowed_ips"], request.remote_addr):
                    return jsonify({"success": False, "error": f"IP non autorizzato per questa chiave (richiesta da {request.remote_addr})"}), 403
                g.api_key = row
                return view(*args, **kwargs)

            if session.get("admin"):
                # Protezione CSRF: le richieste della dashboard che modificano
                # qualcosa arrivano solo come JSON via fetch (il cookie è SameSite=Strict).
                if request.method != "GET" and not request.is_json:
                    return jsonify({"success": False, "error": "richiesta non valida"}), 400
                return view(*args, **kwargs)

            return jsonify({"success": False, "error": "autenticazione richiesta"}), 401

        return wrapper

    return decorator


def require_admin(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            if request.path.startswith("/api/"):
                return jsonify({"success": False, "error": "autenticazione richiesta"}), 401
            return redirect(url_for("login"))
        if request.method != "GET" and not request.is_json:
            return jsonify({"success": False, "error": "richiesta non valida"}), 400
        return view(*args, **kwargs)

    return wrapper


# --- login dashboard ----------------------------------------------------------

def admin_configured():
    return bool(ADMIN_PASSWORD or ADMIN_PASSWORD_HASH)


def login_locked(ip):
    with _login_lock:
        failures, until = _login_failures.get(ip, (0, 0))
        return until > time.time()


def check_login(ip, username, password):
    if not admin_configured() or login_locked(ip):
        return False

    user_ok = hmac.compare_digest(username.encode(), ADMIN_USER.encode())
    if ADMIN_PASSWORD_HASH:
        password_ok = check_password_hash(ADMIN_PASSWORD_HASH, password)
    else:
        password_ok = hmac.compare_digest(password.encode(), ADMIN_PASSWORD.encode())

    with _login_lock:
        if user_ok and password_ok:
            _login_failures.pop(ip, None)
            return True
        failures, _ = _login_failures.get(ip, (0, 0))
        failures += 1
        until = time.time() + LOGIN_LOCK_SECONDS if failures >= LOGIN_MAX_FAILURES else 0
        _login_failures[ip] = (0 if until else failures, until)
    return False


def load_secret_key(data_dir):
    """SECRET_KEY dall'ambiente, altrimenti generata una volta e salvata in /data."""
    env_key = os.environ.get("SECRET_KEY")
    if env_key:
        return env_key
    path = os.path.join(data_dir, "secret_key")
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    os.makedirs(data_dir, exist_ok=True)
    key = secrets.token_hex(32)
    with open(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600), "w") as f:
        f.write(key)
    return key
