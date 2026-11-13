"""Risposte consegnate a chi ha scritto, conferma che cancella, nessun testo negli invii.
Avvio: python3 -m unittest discover -s tests"""
import os
import sys
import tempfile
import unittest

_tmp = tempfile.mkdtemp()
os.environ.setdefault("DB_PATH", os.path.join(_tmp, "test.db"))
os.environ.setdefault("ADMIN_PASSWORD", "password-di-prova")
os.environ.setdefault("SECRET_KEY", "test")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import storage  # noqa: E402

storage.init_db()


class RisposteTest(unittest.TestCase):
    def setUp(self):
        conn = storage.db()
        for t in ("received", "sent", "last_sender"):
            conn.execute(f"DELETE FROM {t}")
        conn.commit()
        conn.close()

    def test_numero_normalizzato(self):
        self.assertEqual(storage.number_key("+39 333 1234567"), "3331234567")
        self.assertEqual(storage.number_key("0039 3331234567"), "3331234567")
        self.assertEqual(storage.number_key("3331234567"), "3331234567")

    def test_invio_senza_testo_e_numero_mascherato(self):
        storage.log_sent("+393331234567", "Il tuo codice e' 123456", True, 1, "ok", api_key_id=7, source="gestionale")
        conn = storage.db()
        r = conn.execute("SELECT number, text FROM sent").fetchone()
        conn.close()
        self.assertEqual(r["text"], "")
        self.assertTrue(r["number"].endswith("567"))
        self.assertNotIn("3331234", r["number"])

    def test_risposta_a_chi_ha_scritto(self):
        storage.log_sent("3331234567", "", True, 1, "ok", api_key_id=1, source="gestionale")
        storage.log_sent("3339999999", "", True, 1, "ok", api_key_id=2, source="foldable")
        storage.save_received("/SMS/1", "+393331234567", "Va bene, passo domani", "t1")
        storage.save_received("/SMS/2", "+393339999999", "Ok grazie", "t2")
        storage.save_received("/SMS/3", "+393330000000", "Chi siete?", "t3")
        g = storage.received_for(1, default_inbox=True)
        f = storage.received_for(2, default_inbox=False)
        self.assertEqual([x["text"] for x in g], ["Va bene, passo domani", "Chi siete?"])
        self.assertEqual([x["text"] for x in f], ["Ok grazie"])
        self.assertEqual(len(storage.received_for(None)), 3)  # dashboard

    def test_invio_fallito_non_assegna_le_risposte(self):
        storage.log_sent("3331234567", "", False, 1, "errore", api_key_id=2, source="foldable")
        storage.save_received("/SMS/1", "3331234567", "?", "t1")
        self.assertEqual(storage.received_for(2), [])

    def test_conferma_cancella_solo_le_proprie(self):
        storage.log_sent("3331234567", "", True, 1, "ok", api_key_id=1)
        storage.log_sent("3339999999", "", True, 1, "ok", api_key_id=2)
        storage.save_received("/SMS/1", "3331234567", "a", "t1")
        storage.save_received("/SMS/2", "3339999999", "b", "t2")
        ids = [r["id"] for r in storage.received_for(None)]
        self.assertEqual(storage.ack_received(ids, api_key_id=1), 1)
        rest = storage.received_for(None)
        self.assertEqual([r["text"] for r in rest], ["b"])

    def test_ricevuti_vecchi_cancellati(self):
        storage.save_received("/SMS/1", "3331234567", "vecchio", "t1")
        conn = storage.db()
        conn.execute("UPDATE received SET imported_at = datetime('now', 'localtime', '-30 days')")
        conn.commit()
        conn.close()
        storage.prune()
        self.assertEqual(storage.received_for(None), [])


if __name__ == "__main__":
    unittest.main()
