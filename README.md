# sms-gateway

Gateway SMS su modem USB (ModemManager/MBIM) con API HTTP e dashboard web.

## Sicurezza

- **Tutte le API richiedono una chiave.** Senza chiave il gateway non invia
  nulla e non mostra né storico né stato. È libero solo `GET /health`.
- Ogni chiave ha:
  - un **nome** (es. `foldable-service`), che compare nello storico invii;
  - dei **permessi**: `send` (invio), `read` (stato e storico) o entrambi;
  - un **limite di SMS all'ora** (predefinito 30);
  - facoltativamente gli **IP o le reti** da cui può essere usata.
- Le chiavi sono salvate solo come hash: quella in chiaro si vede una sola volta,
  alla creazione. Se la perdi, revocala e creane un'altra.
- La **dashboard** chiede utente e password (`ADMIN_USER` / `ADMIN_PASSWORD`).
  Dopo 5 tentativi sbagliati l'IP resta bloccato per 15 minuti. Senza password
  impostata la dashboard è disattivata, mentre le API con chiave funzionano.
- Numero e testo vengono validati prima di passare a `mmcli`, gli SMS partono
  uno alla volta e il server è `waitress` invece del server di sviluppo di Flask.

## Avvio

Mantieni le opzioni per il modem che usi già: queste sono un esempio.

```sh
docker run -d --name sms-gateway --restart unless-stopped \
  --privileged -v /dev:/dev -v sms-gateway-data:/data \
  -e ADMIN_PASSWORD='una-password-lunga' \
  -p 5000:5000 \
  <utente-dockerhub>/sms-gateway:latest
```

| Variabile               | Predefinito | Significato                                                      |
|-------------------------|-------------|------------------------------------------------------------------|
| `ADMIN_USER`            | `admin`     | Utente della dashboard                                           |
| `ADMIN_PASSWORD`        | —           | Password della dashboard                                         |
| `ADMIN_PASSWORD_HASH`   | —           | In alternativa: hash Werkzeug della password (vedi sotto)        |
| `SECRET_KEY`            | generata    | Firma del cookie di sessione; se manca viene creata in `/data`   |
| `SESSION_COOKIE_SECURE` | `false`     | `true` se la dashboard è servita solo in HTTPS                   |
| `TRUST_PROXY_HOPS`      | `0`         | Numero di reverse proxy davanti al gateway (per avere l'IP reale) |
| `MAX_TEXT_LENGTH`       | `640`       | Lunghezza massima del testo                                      |
| `PORT`                  | `5000`      | Porta HTTP                                                       |

Per non scrivere la password in chiaro:

```sh
docker exec sms-gateway python3 -c "from werkzeug.security import generate_password_hash as h; print(h('la-password'))"
```

## Chiavi API

Dalla dashboard (scheda **Chiavi API**: crea, **Modifica** IP e limite, **Revoca**) oppure da terminale:

```sh
# chiave per il sito, usabile solo dal server 192.168.1.20, max 20 SMS/ora
docker exec sms-gateway python3 /app/app.py keys create foldable-service --ips 192.168.1.20 --max-per-hour 20

docker exec sms-gateway python3 /app/app.py keys list
docker exec sms-gateway python3 /app/app.py keys revoke foldable-service

# cambiare IP ammessi e/o limite orario di una chiave esistente
docker exec sms-gateway python3 /app/app.py keys update foldable-service --ips 192.168.1.7 --max-per-hour 50
```

Aggiungi `--scopes send,read` solo per chi deve leggere anche stato e storico.

## API

Chiave nell'header `Authorization: Bearer <chiave>` (oppure `X-API-Key: <chiave>`).

```sh
curl -X POST http://192.168.1.10:5000/api/send-sms \
  -H "Authorization: Bearer smsgw_..." \
  -H "Content-Type: application/json" \
  -d '{"number": "+393331234567", "text": "Il tuo codice è 123456"}'
```

| Metodo e percorso           | Permesso | Risposta                               |
|-----------------------------|----------|----------------------------------------|
| `POST /api/send-sms`        | `send`   | `200 {"success": true, "attempts": n}` |
| `GET /api/status`           | `read`   | Stato del modem                        |
| `GET /api/history/sent`     | `read`   | Ultimi invii (`?limit=`, max 200)      |
| `GET /api/history/received` | `read`   | Ultimi SMS ricevuti                    |
| `GET /health`               | nessuno  | `{"status": "ok"}`                     |

Errori: `400` dati non validi · `401` chiave mancante o errata · `403` permesso
o IP non ammesso · `429` limite orario raggiunto · `500` invio fallito.

Il numero accetta solo cifre, con `+` iniziale facoltativo; spazi, trattini e
parentesi vengono rimossi.

Con `"private": true` (es. codici di accesso) il testo viene inviato intero ma
nello storico le sequenze di 4 o più cifre sono mascherate (`••••••`).
Dopo l'invio l'SMS viene cancellato dalla memoria del modem: lo storico resta
solo nel database del gateway.

## Aggiornare un gateway già in uso

1. Aggiorna l'immagine e ricrea il container: il database in `/data` viene
   aggiornato da solo e lo storico resta.
2. Da quel momento **le chiamate senza chiave vengono rifiutate**. Crea una
   chiave per ogni programma che invia SMS e aggiungi l'header `Authorization`.
3. Se il gateway era raggiungibile da Internet, chiudi il port forwarding sul
   router: con chiavi limitate per IP basta la rete locale. Se l'accesso da
   fuori serve davvero, passa da una VPN o da un tunnel, non da una porta aperta.

## Test

```sh
pip install flask waitress
python3 -m unittest discover -s tests
```
