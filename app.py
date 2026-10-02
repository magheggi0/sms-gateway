from flask import Flask, request, jsonify, render_template
import subprocess
import re
import time
import sqlite3
import os
import threading

app = Flask(__name__)

DB_PATH = "/data/sms-gateway.db"


def get_control_device():
    for i in range(4):
        path = f"/dev/cdc-wdm{i}"
        if os.path.exists(path):
            return path
    return "/dev/cdc-wdm0"


def run(cmd, timeout=20):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        class R:
            stdout = ""
            stderr = "timeout"
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


def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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
    conn.commit()
    conn.close()


def log_sent(number, text, success, attempts, detail):
    conn = db()
    conn.execute(
        "INSERT INTO sent (number, text, success, attempts, detail) VALUES (?, ?, ?, ?, ?)",
        (number, text, 1 if success else 0, attempts, detail),
    )
    conn.commit()
    conn.close()


def save_received(path, number, text, sms_timestamp):
    conn = db()
    try:
        conn.execute(
            "INSERT INTO received (modem_sms_path, number, text, sms_timestamp) VALUES (?, ?, ?, ?)",
            (path, number, text, sms_timestamp),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    conn.close()


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


@app.route("/")
def dashboard():
    return render_template("index.html", device=get_control_device())


@app.route("/api/status", methods=["GET"])
def api_status():
    return jsonify(get_status())


@app.route("/api/send-sms", methods=["POST"])
def api_send_sms():
    data = request.get_json(force=True, silent=True) or {}
    number = (data.get("number") or "").strip()
    text = (data.get("text") or "").strip()

    if not number or not text:
        return jsonify({"success": False, "error": "number e text sono richiesti"}), 400

    modem = get_modem_index()
    if modem is None:
        log_sent(number, text, False, 0, "nessun modem trovato")
        return jsonify({"success": False, "error": "nessun modem trovato"}), 500

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
        log_sent(number, text, False, 0, "creazione SMS fallita: " + detail)
        return jsonify({
            "success": False,
            "error": "creazione SMS fallita",
            "detail": detail
        }), 500

    sms_id = m.group(1)

    max_attempts = 4
    last_detail = ""
    for attempt in range(1, max_attempts + 1):
        send = run(["mmcli", "-s", sms_id, "--send"], timeout=25)
        last_detail = send.stdout + send.stderr
        if "successfully sent" in send.stdout.lower():
            log_sent(number, text, True, attempt, last_detail)
            return jsonify({
                "success": True,
                "detail": last_detail,
                "attempts": attempt
            }), 200
        time.sleep(3)

    log_sent(number, text, False, max_attempts, last_detail)
    return jsonify({
        "success": False,
        "error": f"invio fallito dopo {max_attempts} tentativi",
        "detail": last_detail
    }), 500


@app.route("/api/history/sent", methods=["GET"])
def api_history_sent():
    limit = min(int(request.args.get("limit", 50)), 200)
    conn = db()
    rows = conn.execute(
        "SELECT id, number, text, success, attempts, detail, created_at "
        "FROM sent ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/history/received", methods=["GET"])
def api_history_received():
    limit = min(int(request.args.get("limit", 50)), 200)
    conn = db()
    rows = conn.execute(
        "SELECT id, number, text, sms_timestamp, imported_at "
        "FROM received ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


init_db()
threading.Thread(target=poll_received_sms, daemon=True).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
