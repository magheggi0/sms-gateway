"""Test delle credenziali. Avvio: python3 -m unittest discover -s tests"""
import os
import sys
import tempfile
import unittest
from unittest import mock

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")
os.environ["ADMIN_PASSWORD"] = "password-di-prova"
os.environ["SECRET_KEY"] = "test"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as gateway  # noqa: E402
import auth  # noqa: E402
from storage import init_db, db  # noqa: E402

init_db()


def fake_send(number, text):
    return True, 1, "successfully sent", None


class GatewayAuthTest(unittest.TestCase):
    def setUp(self):
        conn = db()
        conn.execute("DELETE FROM api_keys")
        conn.execute("DELETE FROM sent")
        conn.commit()
        conn.close()
        auth._login_failures.clear()
        self.client = gateway.app.test_client()
        patcher = mock.patch.object(gateway, "send_sms", side_effect=fake_send)
        self.send = patcher.start()
        self.addCleanup(patcher.stop)

    def send_with(self, key, number="+393331234567", text="ciao", **kw):
        return self.client.post("/api/send-sms", json={"number": number, "text": text},
                                headers={"Authorization": f"Bearer {key}"}, **kw)

    def test_senza_credenziali_tutto_bloccato(self):
        self.assertEqual(self.client.post("/api/send-sms", json={"number": "+393331234567", "text": "x"}).status_code, 401)
        for url in ("/api/status", "/api/history/sent", "/api/history/received", "/api/keys"):
            self.assertEqual(self.client.get(url).status_code, 401, url)
        self.assertEqual(self.client.get("/").status_code, 302)
        self.send.assert_not_called()

    def test_health_libero(self):
        self.assertEqual(self.client.get("/health").json, {"status": "ok"})

    def test_chiave_valida_invia(self):
        key = auth.create_key("foldable")
        res = self.send_with(key)
        self.assertEqual(res.status_code, 200)
        self.send.assert_called_once_with("+393331234567", "ciao")
        row = db().execute("SELECT source, api_key_id FROM sent").fetchone()
        self.assertEqual(row["source"], "foldable")

    def test_chiave_x_api_key(self):
        key = auth.create_key("foldable")
        res = self.client.post("/api/send-sms", json={"number": "3331234567", "text": "x"}, headers={"X-API-Key": key})
        self.assertEqual(res.status_code, 200)

    def test_chiave_errata_o_revocata(self):
        key = auth.create_key("foldable")
        self.assertEqual(self.send_with(key + "x").status_code, 401)
        self.assertEqual(self.send_with("smsgw_inventata").status_code, 401)
        auth.revoke_key(name="foldable")
        self.assertEqual(self.send_with(key).status_code, 401)
        self.send.assert_not_called()

    def test_permessi(self):
        send_only = auth.create_key("solo-invio", scopes="send")
        read_only = auth.create_key("solo-lettura", scopes="read")
        h = lambda k: {"Authorization": f"Bearer {k}"}
        self.assertEqual(self.client.get("/api/history/sent", headers=h(send_only)).status_code, 403)
        self.assertEqual(self.client.get("/api/history/sent", headers=h(read_only)).status_code, 200)
        self.assertEqual(self.send_with(read_only).status_code, 403)
        # le chiavi non possono gestire altre chiavi
        self.assertEqual(self.client.get("/api/keys", headers=h(read_only)).status_code, 401)

    def test_ip_ammessi(self):
        key = auth.create_key("lan", allowed_ips="192.168.1.0/24")
        self.assertEqual(self.send_with(key, environ_base={"REMOTE_ADDR": "192.168.1.50"}).status_code, 200)
        self.assertEqual(self.send_with(key, environ_base={"REMOTE_ADDR": "203.0.113.9"}).status_code, 403)

    def test_limite_orario(self):
        key = auth.create_key("limitata", max_per_hour=2)
        self.assertEqual(self.send_with(key).status_code, 200)
        self.assertEqual(self.send_with(key).status_code, 200)
        self.assertEqual(self.send_with(key).status_code, 429)
        self.assertEqual(self.send.call_count, 2)

    def test_validazione_numero_e_testo(self):
        key = auth.create_key("foldable")
        self.assertEqual(self.send_with(key, number="abc").status_code, 400)
        self.assertEqual(self.send_with(key, number="+39333',text='x").status_code, 400)
        self.assertEqual(self.send_with(key, text="x" * 1000).status_code, 400)
        # un apice nel testo non può più chiudere il valore passato a mmcli
        self.assertEqual(self.send_with(key, text="ciao',number='+3900000").status_code, 200)
        sent_text = self.send.call_args[0][1]
        self.assertNotIn("'", sent_text)
        self.assertEqual(self.send.call_args[0][0], "+393331234567")

    def test_nome_chiave(self):
        with self.assertRaises(ValueError):
            auth.create_key("nome con spazi")
        auth.create_key("doppia")
        with self.assertRaises(ValueError):
            auth.create_key("doppia")

    def test_chiave_salvata_solo_come_hash(self):
        key = auth.create_key("foldable")
        dump = "\n".join(str(tuple(r)) for r in db().execute("SELECT * FROM api_keys"))
        self.assertNotIn(key, dump)

    def test_login_dashboard(self):
        self.assertEqual(self.client.post("/login", data={"username": "admin", "password": "sbagliata"}).status_code, 200)
        res = self.client.post("/login", data={"username": "admin", "password": "password-di-prova"})
        self.assertEqual(res.status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/api/history/sent").status_code, 200)
        # dalla dashboard le richieste che modificano devono essere JSON (CSRF)
        self.assertEqual(self.client.post("/api/send-sms", data="number=1").status_code, 400)
        res = self.client.post("/api/keys", json={"name": "nuova"})
        self.assertEqual(res.status_code, 201)
        self.assertTrue(res.json["key"].startswith("smsgw_"))
        self.client.post("/logout")
        self.assertEqual(self.client.get("/api/keys").status_code, 401)

    def test_blocco_dopo_tentativi_falliti(self):
        for _ in range(5):
            self.client.post("/login", data={"username": "admin", "password": "no"})
        res = self.client.post("/login", data={"username": "admin", "password": "password-di-prova"})
        self.assertEqual(res.status_code, 200)
        self.assertIn("Troppi tentativi".encode(), res.data)

    def test_modifica_ip_e_limite(self):
        key = auth.create_key("backoffice", allowed_ips="192.168.1.7")
        self.assertEqual(self.send_with(key, environ_base={"REMOTE_ADDR": "192.168.1.99"}).status_code, 403)
        self.assertTrue(auth.update_key(name="backoffice", allowed_ips="192.168.1.99", max_per_hour=100))
        self.assertEqual(self.send_with(key, environ_base={"REMOTE_ADDR": "192.168.1.99"}).status_code, 200)
        riga = [k for k in auth.list_keys() if k["name"] == "backoffice"][0]
        self.assertEqual((riga["allowed_ips"], riga["max_per_hour"]), ("192.168.1.99/32", 100))
        with self.assertRaises(ValueError):
            auth.update_key(name="backoffice", allowed_ips="non-un-ip")
        auth.revoke_key(name="backoffice")
        self.assertFalse(auth.update_key(name="backoffice", allowed_ips=""))

    def test_modifica_dalla_dashboard(self):
        key = auth.create_key("da-modificare", allowed_ips="10.0.0.1")
        kid = [k for k in auth.list_keys() if k["name"] == "da-modificare"][0]["id"]
        self.assertEqual(self.client.post(f"/api/keys/{kid}", json={"allowed_ips": ""}).status_code, 401)
        self.client.post("/login", data={"username": "admin", "password": "password-di-prova"})
        self.assertEqual(self.client.post(f"/api/keys/{kid}", json={"allowed_ips": "", "max_per_hour": 5}).status_code, 200)
        self.assertEqual(self.send_with(key, environ_base={"REMOTE_ADDR": "203.0.113.9"}).status_code, 200)

    def test_aggiornamento_db_vecchio(self):
        conn = db()
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(sent)")}
        conn.close()
        self.assertTrue({"api_key_id", "source"} <= cols)


if __name__ == "__main__":
    unittest.main()
