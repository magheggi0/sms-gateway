from datetime import timedelta
from flask import Flask, request, jsonify, render_template, session, redirect, url_for, g
from werkzeug.middleware.proxy_fix import ProxyFix
import argparse
import subprocess
import re
import sys
import time
import os
import threading

import auth
from storage import DB_PATH, init_db, log_sent, save_received, count_sent_last_hour, db

app = Flask(__name__)
app.config.update(
    SECRET_KEY=auth.load_secret_key(os.path.dirname(DB_PATH)),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=64 * 1024,
)

# Dietro un reverse proxy: numero di proxy fidati da cui leggere X-Forwarded-For
TRUST_PROXY_HOPS = int(os.environ.get("TRUST_PROXY_HOPS", "0"))
if TRUST_PROXY_HOPS > 0:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=TRUST_PROXY_HOPS, x_proto=TRUST_PROXY_HOPS)

MAX_TEXT_LENGTH = int(os.environ.get("MAX_TEXT_LENGTH", "640"))
NUMBER_RE = re.compile(r"^\+?[0-9]{6,15}$")

# Il modem invia un SMS alla volta
send_lock = threading.Lock()


def get_control_device():
    for i in range(4):
        path = f"/dev/cdc-wdm{i}"
        if os.path.exists(path):
            return path
    return "/dev/cdc-wdm0"


def run(cmd, timeout=20):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        class R:
            stdout = ""
            stderr = "timeout" if isinstance(exc, subprocess.TimeoutExpired) else str(exc)
        return R()


def get_modem_index():
    out = run(["mmcli", "-L"]).stdout
    m = re.search(r"/Modem/(\d+)", out)
    return m.group(1) if m else None


def parse_mmcli_table(text):
    fields = {}
    for raw_line in text.splitlines():
        line = raw_line
        if "|" in line:
            line = line.split("|", 1)[1]
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key and value:
            fields.setdefault(key, value)
    return fields


def enable_modem(modem, attempts=3, wait_between=3):
    for i in range(attempts):
        res = run(["mmcli", "-m", modem, "--enable"], timeout=25)
        if "successfully enabled" in (res.stdout or "").lower():
            return True
        time.sleep(wait_between)
    return False


def parse_sms_detail(text):
    fields = {}
    for raw_line in text.splitlines():
        line = raw_line
        if "|" in line:
            line = line.split("|", 1)[1]
        line = line.strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields.setdefault(key.strip().lower(), value.strip())
    return fields


def poll_received_sms():
    while True:
        try:
            modem = get_modem_index()
            if modem:
                listing = run(["mmcli", "-m", modem, "--messaging-list-sms"], timeout=20).stdout
                for sms_path_match in re.finditer(r"(/org/freedesktop/ModemManager1/SMS/\d+)", listing):
                    path = sms_path_match.group(1)
                    sms_id = path.rsplit("/", 1)[-1]
                    detail_out = run(["mmcli", "-s", sms_id], timeout=15).stdout
                    fields = parse_sms_detail(detail_out)

                    pdu_type = fields.get("pdu type", "")
                    state = fields.get("state", "")
                    if "deliver" in pdu_type.lower() or state.lower() == "received":
                        number = fields.get("number", "")
                        text_val = fields.get("text", "")
                        ts = fields.get("timestamp", "")
                        save_received(path, number, text_val, ts)
        except Exception:
            pass
        time.sleep(20)


def get_status():
    modem = get_modem_index()
    if modem is None:
        return {
            "modem_found": False,
            "signal": None,
            "registration": None,
            "operator": None,
            "access_tech": None,
            "state": None,
        }

    info = run(["mmcli", "-m", modem]).stdout
    fields = parse_mmcli_table(info)

    state = fields.get("state")

    if state == "disabled":
        if enable_modem(modem, attempts=1, wait_between=0):
            info = run(["mmcli", "-m", modem]).stdout
            fields = parse_mmcli_table(info)
            state = fields.get("state")

    signal_raw = fields.get("signal quality", "")
    signal_match = re.search(r"(\d+)\s*%", signal_raw)
    signal = signal_match.group(1) if signal_match else None

    return {
        "modem_found": True,
        "modem_index": modem,
        "signal": signal,
        "state": state,
        "registration": fields.get("registration"),
        "operator": fields.get("operator name"),
        "access_tech": fields.get("access tech"),
        "lock": fields.get("lock"),
    }


def normalize_number(number):
    return re.sub(r"[\s\-().]", "", number or "")


def clean_text(text):
    # mmcli riceve testo e numero come text='...',number='...': un apice nel
    # testo chiuderebbe il valore e potrebbe cambiare il destinatario.
    return (text or "").strip().replace("'", "\u2019")


def send_sms(number, text):
    """Invia un SMS tramite il modem. Ritorna (successo, tentativi, dettaglio, errore)."""
    modem = get_modem_index()
    if modem is None:
        return False, 0, "nessun modem trovato", "nessun modem trovato"

    info = run(["mmcli", "-m", modem]).stdout
    fields = parse_mmcli_table(info)
    if fields.get("state") == "disabled":
        enable_modem(modem, attempts=2, wait_between=3)

    create = run([
        "mmcli", "-m", modem,
        f"--messaging-create-sms=text='{text}',number='{number}'"
    ])
    m = re.search(r"/SMS/(\d+)", create.stdout)
    if not m:
        detail = create.stdout + create.stderr
        return False, 0, "creazione SMS fallita: " + detail, "creazione SMS fallita"

    sms_id = m.group(1)

    max_attempts = 4
    last_detail = ""
    for attempt in range(1, max_attempts + 1):
        send = run(["mmcli", "-s", sms_id, "--send"], timeout=25)
        last_detail = send.stdout + send.stderr
        if "successfully sent" in send.stdout.lower():
            return True, attempt, last_detail, None
        time.sleep(3)

    return False, max_attempts, last_detail, f"invio fallito dopo {max_attempts} tentativi"


# --- pagine -----------------------------------------------------------------

@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/login", methods=["GET", "POST"])
def login():
    if not auth.admin_configured():
        return render_template("login.html", error="Dashboard disattivata: imposta ADMIN_PASSWORD o ADMIN_PASSWORD_HASH.", disabled=True), 503

    error = None
    if request.method == "POST":
        ip = request.remote_addr
        if auth.login_locked(ip):
            error = "Troppi tentativi falliti. Riprova tra 15 minuti."
        elif auth.check_login(ip, request.form.get("username", ""), request.form.get("password", "")):
            session.clear()
            session.permanent = True
            session["admin"] = True
            return redirect(url_for("dashboard"))
        else:
            error = "Credenziali non valide."
    return render_template("login.html", error=error, disabled=False)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@auth.require_admin
def dashboard():
    return render_template("index.html", device=get_control_device())


# --- API ----------------------------------------------------------------------

@app.route("/api/status", methods=["GET"])
@auth.require_auth("read")
def api_status():
    return jsonify(get_status())


@app.route("/api/send-sms", methods=["POST"])
@auth.require_auth("send")
def api_send_sms():
    data = request.get_json(force=True, silent=True) or {}
    number = normalize_number(data.get("number"))
    text = clean_text(data.get("text"))

    if not number or not text:
        return jsonify({"success": False, "error": "number e text sono richiesti"}), 400
    if not NUMBER_RE.match(number):
        return jsonify({"success": False, "error": "numero non valido (solo cifre, eventualmente con + iniziale)"}), 400
    if len(text) > MAX_TEXT_LENGTH:
        return jsonify({"success": False, "error": f"testo troppo lungo (max {MAX_TEXT_LENGTH} caratteri)"}), 400

    key = g.api_key
    key_id = key["id"] if key else None
    source = key["name"] if key else "dashboard"

    with send_lock:
        if key and count_sent_last_hour(key_id) >= key["max_per_hour"]:
            return jsonify({"success": False, "error": "limite orario di SMS raggiunto per questa chiave"}), 429

        success, attempts, detail, error = send_sms(number, text)
        log_sent(number, text, success, attempts, detail, api_key_id=key_id, source=source)

    if success:
        return jsonify({"success": True, "detail": detail, "attempts": attempts}), 200
    return jsonify({"success": False, "error": error, "detail": detail}), 500


@app.route("/api/history/sent", methods=["GET"])
@auth.require_auth("read")
def api_history_sent():
    limit = min(request.args.get("limit", 50, type=int) or 50, 200)
    conn = db()
    rows = conn.execute(
        "SELECT id, number, text, success, attempts, detail, source, created_at "
        "FROM sent ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/history/received", methods=["GET"])
@auth.require_auth("read")
def api_history_received():
    limit = min(request.args.get("limit", 50, type=int) or 50, 200)
    conn = db()
    rows = conn.execute(
        "SELECT id, number, text, sms_timestamp, imported_at "
        "FROM received ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# --- gestione chiavi (solo dashboard) ---------------------------------------

@app.route("/api/keys", methods=["GET"])
@auth.require_admin
def api_keys_list():
    return jsonify(auth.list_keys())


@app.route("/api/keys", methods=["POST"])
@auth.require_admin
def api_keys_create():
    data = request.get_json(silent=True) or {}
    try:
        key = auth.create_key(
            data.get("name"),
            scopes=data.get("scopes") or "send",
            allowed_ips=data.get("allowed_ips") or "",
            max_per_hour=data.get("max_per_hour") or 30,
        )
    except (ValueError, TypeError) as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    return jsonify({"success": True, "key": key}), 201


@app.route("/api/keys/<int:key_id>", methods=["POST"])
@auth.require_admin
def api_keys_update(key_id):
    data = request.get_json(silent=True) or {}
    try:
        updated = auth.update_key(
            key_id=key_id,
            allowed_ips=data.get("allowed_ips"),
            max_per_hour=data.get("max_per_hour"),
        )
    except (ValueError, TypeError) as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    if not updated:
        return jsonify({"success": False, "error": "chiave non trovata o revocata"}), 404
    return jsonify({"success": True})


@app.route("/api/keys/<int:key_id>/revoke", methods=["POST"])
@auth.require_admin
def api_keys_revoke(key_id):
    if not auth.revoke_key(key_id=key_id):
        return jsonify({"success": False, "error": "chiave non trovata o già revocata"}), 404
    return jsonify({"success": True})


# --- riga di comando ----------------------------------------------------------

def cli(argv):
    parser = argparse.ArgumentParser(prog="app.py", description="SMS gateway")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("serve", help="avvia il server (predefinito)")

    keys = sub.add_parser("keys", help="gestione chiavi API").add_subparsers(dest="action", required=True)
    create = keys.add_parser("create", help="crea una chiave (la mostra una sola volta)")
    create.add_argument("name")
    create.add_argument("--scopes", default="send", help="send, read o send,read (predefinito: send)")
    create.add_argument("--ips", default="", help="IP o reti ammesse, es. 192.168.1.20,10.0.0.0/24")
    create.add_argument("--max-per-hour", type=int, default=30)
    keys.add_parser("list", help="elenca le chiavi")
    revoke = keys.add_parser("revoke", help="revoca una chiave")
    revoke.add_argument("name")
    update = keys.add_parser("update", help="cambia IP ammessi e/o limite orario di una chiave")
    update.add_argument("name")
    update.add_argument("--ips", default=None, help="nuovi IP/reti ammessi; stringa vuota = tutti")
    update.add_argument("--max-per-hour", type=int, default=None)

    args = parser.parse_args(argv)
    init_db()

    if args.command == "keys":
        if args.action == "create":
            try:
                key = auth.create_key(args.name, args.scopes, args.ips, args.max_per_hour)
            except ValueError as exc:
                print(f"Errore: {exc}", file=sys.stderr)
                return 1
            print(f"Chiave '{args.name}' creata. Copiala ora, non verra' piu' mostrata:\n\n{key}\n")
        elif args.action == "list":
            for k in auth.list_keys():
                stato = f"REVOCATA {k['revoked_at']}" if k["revoked_at"] else "attiva"
                print(f"{k['id']:>3}  {k['name']:<20} {k['key_prefix']}...  {k['scopes']:<10} "
                      f"{k['max_per_hour']}/h  ip: {k['allowed_ips'] or 'tutti'}  "
                      f"ultimo uso: {k['last_used_at'] or '-'} da {k['last_used_ip'] or '-'}  {stato}")
        elif args.action == "update":
            try:
                if not auth.update_key(name=args.name, allowed_ips=args.ips, max_per_hour=args.max_per_hour):
                    print("Chiave non trovata o revocata", file=sys.stderr)
                    return 1
            except ValueError as exc:
                print(f"Errore: {exc}", file=sys.stderr)
                return 1
            print(f"Chiave '{args.name}' aggiornata")
        elif args.action == "revoke":
            if not auth.revoke_key(name=args.name):
                print("Chiave non trovata o gia' revocata", file=sys.stderr)
                return 1
            print(f"Chiave '{args.name}' revocata")
        return 0

    serve()
    return 0


def serve():
    from waitress import serve as waitress_serve

    if not auth.admin_configured():
        print("[app] ATTENZIONE: ADMIN_PASSWORD non impostata, dashboard disattivata (le API con chiave funzionano)")
    threading.Thread(target=poll_received_sms, daemon=True).start()
    port = int(os.environ.get("PORT", "5000"))
    print(f"[app] in ascolto sulla porta {port}")
    waitress_serve(app, host="0.0.0.0", port=port, threads=8)


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
