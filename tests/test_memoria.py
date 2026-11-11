"""Memoria del modem e storico a giro. Avvio: python3 -m unittest discover -s tests"""
import os
import sys
import tempfile
import unittest
from unittest import mock

_tmp = tempfile.mkdtemp()
os.environ.setdefault("DB_PATH", os.path.join(_tmp, "test.db"))
os.environ.setdefault("ADMIN_PASSWORD", "password-di-prova")
os.environ.setdefault("SECRET_KEY", "test")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as gateway  # noqa: E402
import storage  # noqa: E402

storage.init_db()
SMS = "/org/freedesktop/ModemManager1/SMS/"


class ModemMemoryTest(unittest.TestCase):
    def setUp(self):
        conn = storage.db()
        conn.execute("DELETE FROM received")
        conn.execute("DELETE FROM sent")
        conn.commit()
        conn.close()

    def fake_modem(self, sms, send_results=None):
        """sms: {id: dettaglio mmcli -s}; send_results: uscite di --send in ordine"""
        calls, sends = [], list(send_results or [])

        def run(cmd, timeout=20):
            calls.append(cmd)
            last = cmd[-1]
            if last == "--messaging-list-sms":
                out = "\n".join(f"    {SMS}{i} (x)" for i in sms)
            elif cmd[1] == "-s" and last == "--send":
                out = sends.pop(0) if sends else "successfully sent the SMS"
                return mock.Mock(stdout=out if "successfully" in out else "", stderr="" if "successfully" in out else out)
            elif cmd[1] == "-s":
                out = sms.get(cmd[2], "")
            elif last.startswith("--messaging-delete-sms="):
                sms.pop(last.split("=", 1)[1], None)
                out = "successfully deleted SMS from modem"
            elif last.startswith("--messaging-create-sms"):
                sms["99"] = "  | pdu type: submit\n  | state: stored"
                out = f"Successfully created new SMS: {SMS}99"
            else:
                out = ""
            return mock.Mock(stdout=out, stderr="")
        return run, calls

    def test_ricevuti_salvati_e_cancellati(self):
        sms = {"1": "  | number: +39333\n  | text: ciao\n  | pdu type: deliver\n  | state: received\n  | timestamp: 2026-11-11T10:00",
               "2": "  | pdu type: submit\n  | state: sent",
               "3": "  | pdu type: deliver\n  | state: receiving"}
        run, _ = self.fake_modem(sms)
        with mock.patch.object(gateway, "run", side_effect=run):
            n = gateway.purge_modem_storage("0")
        self.assertEqual(n, 2)
        self.assertEqual(list(sms), ["3"])  # quello ancora in arrivo resta
        conn = storage.db()
        rows = conn.execute("SELECT number, text FROM received").fetchall()
        conn.close()
        self.assertEqual([tuple(r) for r in rows], [("+39333", "ciao")])

    def test_memoria_piena_svuota_e_riprova(self):
        sms = {str(i): "  | pdu type: submit\n  | state: sent" for i in range(1, 6)}
        run, _ = self.fake_modem(sms, ["error: couldn't send the SMS: 'GDBus.Error:org.freedesktop.libmbim.Error.Status.MemoryFull: Couldn't send SMS part: MemoryFull'"])
        with mock.patch.object(gateway, "get_modem_index", return_value="0"), mock.patch.object(gateway, "run", side_effect=run), \
                mock.patch.object(gateway.time, "sleep"):
            ok, attempts, _, _ = gateway.send_sms("+393331234567", "x")
        self.assertTrue(ok)
        self.assertEqual(attempts, 2)
        self.assertEqual(sms, {})  # vecchi cancellati, e quello inviato cancellato dopo l'invio

    def test_storico_a_giro(self):
        with mock.patch.object(storage, "HISTORY_MAX", 3):
            for i in range(5):
                storage.log_sent(f"33300{i}", "x", True, 1, "")
        conn = storage.db()
        nums = [r["number"] for r in conn.execute("SELECT number FROM sent ORDER BY id")]
        conn.close()
        self.assertEqual(nums, ["333002", "333003", "333004"])

    def test_pulsante_dashboard(self):
        client = gateway.app.test_client()
        self.assertEqual(client.post("/api/modem/purge", json={}).status_code, 401)
        with client.session_transaction() as sess:
            sess["admin"] = True
        sms = {"4": "  | pdu type: submit\n  | state: sent"}
        run, _ = self.fake_modem(sms)
        with mock.patch.object(gateway, "get_modem_index", return_value="0"), mock.patch.object(gateway, "run", side_effect=run):
            res = client.post("/api/modem/purge", json={})
        self.assertEqual(res.get_json(), {"success": True, "deleted": 1, "left": 0})


if __name__ == "__main__":
    unittest.main()
